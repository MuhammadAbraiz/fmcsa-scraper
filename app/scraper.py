import concurrent.futures
import json
import os
import re
import threading
import time
import uuid

import requests
from dotenv import load_dotenv

from . import models

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


SCRAPE_WORKERS = 10


def run_scrape_job(job_id, start_mc, end_mc, agent_id):
    total = end_mc - start_mc + 1
    job_row_id = models.create_search_job(job_id, agent_id, start_mc, end_mc, total)

    write_job(job_id, status='running', processed=0, total=total, found=0,
              start_mc=start_mc, end_mc=end_mc, message=None)

    def process_mc(mc_number):
        carrier_data = extract_data(fetch_mc_data(mc_number))
        if carrier_data:
            carrier_data['mc_number'] = mc_number
            carrier_data['email'] = scrape_carrier_email(carrier_data.get('usdot'))
            time.sleep(1)  # be polite to FMCSA's site; only matches hit this page
        return carrier_data

    counters = {'processed': 0, 'found': 0, 'last_write': 0.0}

    def handle_result(carrier_data):
        counters['processed'] += 1
        if carrier_data:
            lead_row = models.upsert_lead(carrier_data, job_row_id, agent_id)
            if lead_row:
                counters['found'] += 1
            # else: match had no usdot to dedupe on — still counted in processed, not in found

        now = time.monotonic()
        is_last = counters['processed'] == total
        if carrier_data is not None or is_last or now - counters['last_write'] >= 1.0:
            counters['last_write'] = now
            write_job(job_id, processed=counters['processed'], found=counters['found'])
            models.update_search_job(job_id, processed=counters['processed'], found=counters['found'])

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as executor:
            futures = [executor.submit(process_mc, mc) for mc in range(start_mc, end_mc + 1)]
            for future in concurrent.futures.as_completed(futures):
                handle_result(future.result())
    except Exception as e:
        write_job(job_id, status='error', message=f'Scrape failed: {e}')
        models.update_search_job(job_id, status='error', message=f'Scrape failed: {e}')
        return

    found = counters['found']
    message = 'No valid data found matching the criteria.' if found == 0 else f'Found {found} matching carrier(s).'
    write_job(job_id, status='done', message=message)
    models.update_search_job(job_id, status='done', message=message)


def start_scrape_job(start_mc, end_mc, agent_id):
    job_id = uuid.uuid4().hex
    thread = threading.Thread(target=run_scrape_job, args=(job_id, start_mc, end_mc, agent_id), daemon=True)
    thread.start()
    return job_id
