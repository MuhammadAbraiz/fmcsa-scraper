from flask import Flask, request, send_file, render_template
import requests
import csv
import os
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
import re
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# API key and base URL for SAFER
api_key = os.environ.get('SAFER_API_KEY')
if not api_key:
    raise RuntimeError('SAFER_API_KEY environment variable not set. Please set it in your environment.')
base_url = "https://saferwebapi.com/v2/mcmx/snapshot/"
headers = {"x-api-key": api_key}

# Path to your Chrome WebDriver (configurable via environment variable)
chromedriver_path = os.environ.get('CHROMEDRIVER_PATH', '/usr/bin/chromedriver')

def get_driver():
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    service = Service(chromedriver_path)
    return webdriver.Chrome(service=service, options=options)

def format_phone_number(phone):
    """Remove all non-digit characters and prepend +1 if it's a 10-digit US number."""
    phone = re.sub(r'\D', '', phone)
    if len(phone) == 10:
        phone = '+1' + phone
    return phone

def scrape_usdot_email(usdot):
    """
    Scrape the carrier's email from FMCSA's website:
      https://ai.fmcsa.dot.gov/SMS/Carrier/{usdot}/CarrierRegistration.aspx

    Returns the email as a string (or '' if not found).
    """
    url = f"https://ai.fmcsa.dot.gov/SMS/Carrier/{usdot}/CarrierRegistration.aspx"
    driver = get_driver()
    driver.get(url)

    try:
        # Page structure: 
        #   <ul class="col1">
        #       <li>
        #           <label>Email:</label>
        #           <span class="dat">somecarrier@example.com</span>
        #       </li>
        #   </ul>
        ul_element_col1 = driver.find_element(By.CLASS_NAME, 'col1')
        li_elements_col1 = ul_element_col1.find_elements(By.TAG_NAME, 'li')

        email = ''
        for li in li_elements_col1:
            label = li.find_element(By.TAG_NAME, 'label').text.strip()
            data = li.find_element(By.CLASS_NAME, 'dat').text.strip()
            if label.lower() == 'email:':
                email = data
                break

        return email

    except Exception as e:
        print(f"USDOT={usdot} - Email scrape error: {e}")
        return ''
    finally:
        driver.quit()

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
        'Cargo Carried',
        'Scraped Email'
    ]

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

        If the record qualifies, also scrape the email from FMCSA.
        """
        if not data:
            return None

        power_units = data.get('power_units', 0)
        operating_status = data.get('operating_status', '').lower()

        # Filter: 1 power unit + "authorized for property"
        if power_units == 1 and 'authorized for property' in operating_status:
            # Scrape email via Selenium
            usdot = data.get('usdot', '')
            scraped_email = scrape_usdot_email(usdot)

            # Collect fields
            mileage_info = data.get('mcs_150_mileage_year', {})
            return {
                'legal_name': data.get('legal_name', ''),
                'usdot': usdot,
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
                'cargo_carried': ', '.join(data.get('cargo_carried', [])),
                'scraped_email': scraped_email
            }

        return None

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
                    carrier_data['scraped_email']  # from scrape_usdot_email()
                ])

    # If we found any carriers, the CSV should exist
    if os.path.exists(output_csv_file):
        return send_file(output_csv_file, as_attachment=True)
    else:
        return "No valid data found matching the criteria.", 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
