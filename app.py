from flask import Flask, request, jsonify, send_from_directory, render_template
import requests
import csv
import os
import re
import json
import uuid
import threading
import time
import smtplib
from datetime import datetime
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, 'outputs')
JOBS_DIR = os.path.join(BASE_DIR, 'jobs')
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(JOBS_DIR, exist_ok=True)

# API key and base URL for SAFER
api_key = os.environ.get('SAFER_API_KEY')
if not api_key:
    raise RuntimeError('SAFER_API_KEY environment variable not set. Please set it in your environment.')
base_url = "https://saferwebapi.com/v2/mcmx/snapshot/"
headers = {"x-api-key": api_key}

CSV_COLUMNS = [
    'Legal Name', 'USDOT Number', 'MC/MX/FF Numbers', 'Entity Type', 'Address',
    'Phone', 'Email', 'Power Units', 'Drivers', 'MCS-150 Form Date', 'MCS-150 Mileage',
    'MCS-150 Mileage Year', 'Out of Service Date', 'Operating Status',
    'Operation Classification', 'Carrier Operation', 'Cargo Carried',
    'Likely Equipment (Inferred)',
]

CSV_FIELD_ORDER = [
    'legal_name', 'usdot', 'mc_mx_ff_numbers', 'entity_type', 'address',
    'phone', 'email', 'power_units', 'drivers', 'mcs_150_form_date', 'mcs_150_mileage',
    'mcs_150_mileage_year', 'out_of_service_date', 'operating_status',
    'operation_classification', 'carrier_operation', 'cargo_carried',
    'likely_equipment',
]

FMCSA_CARRIER_URL = "https://ai.fmcsa.dot.gov/SMS/Carrier/{usdot}/CarrierRegistration.aspx"
FMCSA_REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
}
EMAIL_FIELD_RE = re.compile(
    r'<label>\s*Email:\s*</label>\s*<span class="dat">\s*([^<]*?)\s*</span>',
    re.IGNORECASE,
)


def scrape_carrier_email(usdot):
    """Fetch the carrier's registered email from FMCSA's public registration page."""
    if not usdot:
        return ''
    url = FMCSA_CARRIER_URL.format(usdot=usdot)
    try:
        response = requests.get(url, headers=FMCSA_REQUEST_HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException:
        return ''
    match = EMAIL_FIELD_RE.search(response.text)
    return match.group(1).strip() if match else ''

# FMCSA's cargo_carried field is a fixed MCS-150 commodity list — it has no
# concept of trailer/equipment type. This is a best-effort guess from cargo
# keywords for prioritizing calls, not a verified fact. "Power Only" and
# "Hot Shot" specifically have no reliable signal in FMCSA data at all, so
# they're folded into the flatbed bucket with that caveat rather than guessed
# outright.
REEFER_KEYWORDS = ('refrigerated', 'meat', 'fresh produce')
FLATBED_KEYWORDS = (
    'building materials', 'machinery', 'large objects', 'metal: sheets',
    'coils', 'rolls', 'logs', 'poles', 'beams', 'lumber', 'construction',
    'oilfield',
)
BOX_TRUCK_KEYWORDS = ('household goods',)
DRY_VAN_KEYWORDS = (
    'general freight', 'paper products', 'beverages', 'us mail', 'grain',
    'feed', 'hay', 'intermodal', 'dry bulk',
)


def infer_equipment(cargo_list):
    cargo_list = cargo_list or []
    combined = ', '.join(cargo_list).lower()
    if not combined:
        return 'Unknown (no cargo data)'
    if any(k in combined for k in REEFER_KEYWORDS):
        return 'Reefer'
    if any(k in combined for k in FLATBED_KEYWORDS):
        return 'Flatbed/Stepdeck or Hot Shot (single-truck, unverified)'
    if any(k in combined for k in BOX_TRUCK_KEYWORDS):
        return 'Box Truck (possible, household goods mover)'
    if any(k in combined for k in DRY_VAN_KEYWORDS):
        return 'Dry Van (likely)'
    return f'Unknown/Other ({", ".join(cargo_list)})'


def job_path(job_id):
    return os.path.join(JOBS_DIR, f'{job_id}.json')


def write_job(job_id, **fields):
    """Persist job progress to disk so any gunicorn worker can read it."""
    path = job_path(job_id)
    data = {}
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
    data.update(fields)
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    os.replace(tmp_path, path)


def read_job(job_id):
    path = job_path(job_id)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def format_phone_number(phone):
    """Remove all non-digit characters and prepend +1 if it's a 10-digit US number."""
    phone = re.sub(r'\D', '', phone or '')
    if len(phone) == 10:
        phone = '+1' + phone
    return phone


def fetch_mc_data(mc_number):
    """Fetch JSON data from the SaferWeb API for a given MC number."""
    url = f"{base_url}{mc_number}"
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def extract_data(data):
    """
    Returns a dict with the relevant fields IF:
      - power_units == 1
      - 'authorized for property' in operating_status (case-insensitive)
    Otherwise returns None.
    """
    if not data:
        return None

    power_units = data.get('power_units', 0)
    operating_status = (data.get('operating_status') or '').lower()

    # SAFER returns statuses like "AUTHORIZED FOR: Motor Carrier of Property
    # (Except Household Goods)" rather than the literal phrase "authorized for
    # property", so match loosely instead of on an exact substring.
    is_authorized_for_property = bool(re.search(r'authorized for.*propert', operating_status))

    if power_units == 1 and is_authorized_for_property:
        mileage_info = data.get('mcs_150_mileage_year') or {}
        cargo_carried = data.get('cargo_carried') or []
        return {
            'legal_name': data.get('legal_name', ''),
            'usdot': data.get('usdot', ''),
            'mc_mx_ff_numbers': data.get('mc_mx_ff_numbers', ''),
            'entity_type': data.get('entity_type', ''),
            'address': data.get('physical_address', ''),
            'phone': format_phone_number(data.get('phone', '')),
            'power_units': power_units,
            'drivers': data.get('drivers', ''),
            'mcs_150_form_date': data.get('mcs_150_form_date', ''),
            'mcs_150_mileage': mileage_info.get('mileage', ''),
            'mcs_150_mileage_year': mileage_info.get('year', ''),
            'out_of_service_date': data.get('out_of_service_date', ''),
            'operating_status': data.get('operating_status', ''),
            'operation_classification': ', '.join(data.get('operation_classification') or []),
            'carrier_operation': ', '.join(data.get('carrier_operation') or []),
            'cargo_carried': ', '.join(cargo_carried),
            'likely_equipment': infer_equipment(cargo_carried),
        }
    return None


def send_csv_email(to_email, csv_file_path):
    gmail_user = os.environ.get('GMAIL_USER')
    gmail_pass = os.environ.get('GMAIL_PASS')
    msg = EmailMessage()
    msg['Subject'] = 'Your Requested CSV File'
    msg['From'] = gmail_user
    msg['To'] = to_email
    msg.set_content('Attached is your requested CSV file.')
    with open(csv_file_path, 'rb') as f:
        file_data = f.read()
        file_name = os.path.basename(csv_file_path)
    msg.add_attachment(file_data, maintype='application', subtype='octet-stream', filename=file_name)
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(gmail_user, gmail_pass)
        smtp.send_message(msg)


def run_scrape_job(job_id, start_mc, end_mc, user_email):
    total = end_mc - start_mc + 1
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_csv_file = os.path.join(OUTPUT_DIR, f'output_{timestamp}_{job_id[:8]}.csv')

    write_job(job_id, status='running', processed=0, total=total, found=0,
              start_mc=start_mc, end_mc=end_mc, download_url=None, message=None)

    with open(output_csv_file, 'w', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow(CSV_COLUMNS)

    found = 0
    try:
        for i, mc_number in enumerate(range(start_mc, end_mc + 1), start=1):
            carrier_data = extract_data(fetch_mc_data(mc_number))
            if carrier_data:
                carrier_data['email'] = scrape_carrier_email(carrier_data.get('usdot'))
                time.sleep(1)  # be polite to FMCSA's site; only matches hit this page
                found += 1
                with open(output_csv_file, 'a', newline='', encoding='utf-8') as f:
                    csv.writer(f).writerow([carrier_data[key] for key in CSV_FIELD_ORDER])
            write_job(job_id, processed=i, found=found)
    except Exception as e:
        write_job(job_id, status='error', message=f'Scrape failed: {e}')
        return

    if found == 0:
        os.remove(output_csv_file)
        write_job(job_id, status='done', message='No valid data found matching the criteria.')
        return

    filename = os.path.basename(output_csv_file)
    download_url = f"/download/{filename}"

    if not (os.environ.get('GMAIL_USER') and os.environ.get('GMAIL_PASS')):
        write_job(job_id, status='done', message='Email not configured.', download_url=download_url)
        return

    try:
        send_csv_email(user_email, output_csv_file)
    except Exception as e:
        write_job(job_id, status='done', message=f'Email failed to send ({e}).', download_url=download_url)
        return

    os.remove(output_csv_file)
    write_job(job_id, status='done', message=f'CSV sent to {user_email}!')


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/generate-csv', methods=['POST'])
def generate_csv():
    try:
        start_mc = int(request.form.get('start_mc', 1560000))
        end_mc = int(request.form.get('end_mc', 1560100))
    except (TypeError, ValueError):
        return jsonify({'error': 'Start and end MC numbers must be integers.'}), 400

    if end_mc < start_mc:
        return jsonify({'error': 'End MC number must be greater than or equal to start MC number.'}), 400

    user_email = request.form.get('user_email', '')

    job_id = uuid.uuid4().hex
    thread = threading.Thread(target=run_scrape_job, args=(job_id, start_mc, end_mc, user_email), daemon=True)
    thread.start()

    return jsonify({'job_id': job_id})


@app.route('/status/<job_id>')
def job_status(job_id):
    data = read_job(job_id)
    if data is None:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(data)


@app.route('/files')
def list_files():
    files = []
    for filename in os.listdir(OUTPUT_DIR):
        if not (filename.startswith('output_') and filename.endswith('.csv')):
            continue
        path = os.path.join(OUTPUT_DIR, filename)
        files.append({
            'filename': filename,
            'size_bytes': os.path.getsize(path),
            'modified_at': datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M:%S'),
            'download_url': f'/download/{filename}',
        })
    files.sort(key=lambda f: f['modified_at'], reverse=True)
    return jsonify(files)


@app.route('/download/<path:filename>')
def download_file(filename):
    safe_filename = os.path.basename(filename)
    if not safe_filename.startswith('output_') or not safe_filename.endswith('.csv'):
        return "Invalid file", 400
    if not os.path.exists(os.path.join(OUTPUT_DIR, safe_filename)):
        return "File not found", 404
    return send_from_directory(OUTPUT_DIR, safe_filename, as_attachment=True)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
