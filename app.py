from flask import Flask, request, send_file, render_template
import requests
import csv
import os
from datetime import datetime
import re
from dotenv import load_dotenv
import time
import smtplib
from email.message import EmailMessage

load_dotenv()
print("GMAIL_USER:", os.environ.get('GMAIL_USER'))
print("GMAIL_PASS:", os.environ.get('GMAIL_PASS'))

app = Flask(__name__)

# API key and base URL for SAFER
api_key = os.environ.get('SAFER_API_KEY')
if not api_key:
    raise RuntimeError('SAFER_API_KEY environment variable not set. Please set it in your environment.')
base_url = "https://saferwebapi.com/v2/mcmx/snapshot/"
headers = {"x-api-key": api_key}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/generate-csv', methods=['POST'])
def generate_csv():
    """
    Generates a CSV of carriers who have:
      - Exactly 1 power unit
      - 'authorized for property' in their operating_status (case-insensitive)
    Then scrapes each carrier's email from FMCSA via Selenium.
    """
    start_mc = int(request.form.get('start_mc', 1560000))
    end_mc   = int(request.form.get('end_mc', 1560100))
    user_email = request.form.get('user_email')

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_csv_file = f'output_{timestamp}.csv'

    # CSV columns (including "Email" at the end)
    csv_columns = [
        'Legal Name',
        'USDOT Number',
        'MC/MX/FF Numbers',
        'Entity Type',
        'Address',
        'Phone',
        'Power Units',
        'Drivers',
        'MCS-150 Form Date',
        'MCS-150 Mileage',
        'MCS-150 Mileage Year',
        'Out of Service Date',
        'Operating Status',
        'Operation Classification',
        'Carrier Operation',
        'Cargo Carried'    ]

    # Write the CSV header
    with open(output_csv_file, 'w', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        writer.writerow(csv_columns)

    def fetch_mc_data(mc_number):
        """
        Fetch JSON data from the SaferWeb API for a given MC number.
        """
        url = f"{base_url}{mc_number}"
        try:
            response = requests.get(url, headers=headers)
            # Debug prints (optional)
            print(f"\nMC={mc_number} - Status Code: {response.status_code}")
            print("Response text:", response.text[:300], '...')  # truncated for brevity

            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"Error fetching data for MC={mc_number}: {e}")
            return None

    def extract_data(mc_number, data):
        """
        Returns a dict with the relevant fields IF:
          - power_units == 1
          - 'authorized for property' in operating_status (case-insensitive)
        Otherwise returns None.
        """
        if not data:
            return None

        power_units = data.get('power_units', 0)
        operating_status = data.get('operating_status', '').lower()

        # Filter: 1 power unit + "authorized for property"
        if power_units == 1 and 'authorized for property' in operating_status:
            mileage_info = data.get('mcs_150_mileage_year', {})
            return {
                'legal_name': data.get('legal_name', ''),
                'usdot': data.get('usdot', ''),
                'mc_mx_ff_numbers': data.get('mc_mx_ff_numbers', ''),
                'entity_type': data.get('entity_type', ''),
                'address': data.get('physical_address', ''),  # or 'mailing_address'
                'phone': format_phone_number(data.get('phone', '')),
                'power_units': power_units,
                'drivers': data.get('drivers', ''),
                'mcs_150_form_date': data.get('mcs_150_form_date', ''),
                'mcs_150_mileage': mileage_info.get('mileage', ''),
                'mcs_150_mileage_year': mileage_info.get('year', ''),
                'out_of_service_date': data.get('out_of_service_date', ''),
                'operating_status': data.get('operating_status', ''),
                'operation_classification': ', '.join(data.get('operation_classification', [])),
                'carrier_operation': ', '.join(data.get('carrier_operation', [])),
                'cargo_carried': ', '.join(data.get('cargo_carried', []))            }
        return None

    def format_phone_number(phone):
        """Remove all non-digit characters and prepend +1 if it's a 10-digit US number."""
        phone = re.sub(r'\D', '', phone)
        if len(phone) == 10:
            phone = '+1' + phone
        return phone

    # Loop through each MC number in the requested range
    for mc_number in range(start_mc, end_mc + 1):
        mc_json = fetch_mc_data(mc_number)
        carrier_data = extract_data(mc_number, mc_json)

        if carrier_data:
            # Write row to CSV
            with open(output_csv_file, 'a', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                writer.writerow([
                    carrier_data['legal_name'],
                    carrier_data['usdot'],
                    carrier_data['mc_mx_ff_numbers'],
                    carrier_data['entity_type'],
                    carrier_data['address'],
                    carrier_data['phone'],
                    carrier_data['power_units'],
                    carrier_data['drivers'],
                    carrier_data['mcs_150_form_date'],
                    carrier_data['mcs_150_mileage'],
                    carrier_data['mcs_150_mileage_year'],
                    carrier_data['out_of_service_date'],
                    carrier_data['operating_status'],
                    carrier_data['operation_classification'],
                    carrier_data['carrier_operation'],
                    carrier_data['cargo_carried'],
                ])

    # If we found any carriers, the CSV should exist
    if os.path.exists(output_csv_file):
        send_csv_email(user_email, output_csv_file)
        os.remove(output_csv_file)
        return f"CSV sent to {user_email}!"
    else:
        return "No valid data found matching the criteria.", 400

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
