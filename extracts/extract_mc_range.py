"""One-off standalone MC-range extractor -> CSV, fully independent of the
Flask app and its DB (deliberately not importing app.scraper: that import
would run app/__init__.py, which needs FLASK_SECRET_KEY and a live DB
connection this tool has no business requiring). The handful of pure
functions below are intentionally duplicated from app/scraper.py rather than
imported, so this stays a self-contained CLI tool.

Two decoupled thread pools instead of one, so a slow FMCSA email fetch on a
match doesn't tie up a SAFER-lookup worker slot that could be moving on to the
next MC number:
  - lookup pool: SAFER API calls (fast, high concurrency)
  - email pool: FMCSA email-page scrapes on matches only (slower, rate-limited,
    lower concurrency, runs independently of the lookup pool)

Usage: SAFER_API_KEY=... python extract_mc_range.py <start_mc> <end_mc> <output_dir>
"""
import concurrent.futures
import csv
import os
import re
import sys
import threading
import time

import requests

api_key = os.environ.get('SAFER_API_KEY')
if not api_key:
    raise RuntimeError('SAFER_API_KEY environment variable not set.')
SAFER_BASE_URL = 'https://saferwebapi.com/v2/mcmx/snapshot/'
SAFER_HEADERS = {'x-api-key': api_key}

FMCSA_CARRIER_URL = 'https://ai.fmcsa.dot.gov/SMS/Carrier/{usdot}/CarrierRegistration.aspx'
FMCSA_REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
}
EMAIL_FIELD_RE = re.compile(
    r'<label>\s*Email:\s*</label>\s*<span class="dat">\s*([^<]*?)\s*</span>',
    re.IGNORECASE,
)

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


def format_phone_number(phone):
    phone = re.sub(r'\D', '', phone or '')
    if len(phone) == 10:
        phone = '+1' + phone
    return phone


def fetch_mc_data(mc_number):
    try:
        response = requests.get(f'{SAFER_BASE_URL}{mc_number}', headers=SAFER_HEADERS, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def extract_data(data):
    if not data:
        return None
    power_units = data.get('power_units', 0)
    operating_status = (data.get('operating_status') or '').lower()
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


def scrape_carrier_email(usdot):
    if not usdot:
        return ''
    try:
        response = requests.get(
            FMCSA_CARRIER_URL.format(usdot=usdot), headers=FMCSA_REQUEST_HEADERS, timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException:
        return ''
    match = EMAIL_FIELD_RE.search(response.text)
    return match.group(1).strip() if match else ''


LOOKUP_WORKERS = 20
EMAIL_WORKERS = 8

CSV_COLUMNS = [
    'mc_number', 'legal_name', 'usdot', 'mc_mx_ff_numbers', 'entity_type',
    'address', 'phone', 'email', 'power_units', 'drivers', 'mcs_150_form_date',
    'mcs_150_mileage', 'mcs_150_mileage_year', 'out_of_service_date',
    'operating_status', 'operation_classification', 'carrier_operation',
    'cargo_carried', 'likely_equipment',
]


def main():
    start_mc, end_mc, output_dir = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    os.makedirs(output_dir, exist_ok=True)
    total = end_mc - start_mc + 1

    out_path = os.path.join(output_dir, f'mc_{start_mc}-{end_mc}.csv')
    csv_file = open(out_path, 'w', newline='', encoding='utf-8')
    writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    csv_file.flush()

    write_lock = threading.Lock()
    processed = {'count': 0}
    found = {'count': 0}
    start_time = time.monotonic()

    def do_lookup(mc_number):
        carrier_data = extract_data(fetch_mc_data(mc_number))
        if carrier_data:
            carrier_data['mc_number'] = mc_number
        return carrier_data

    def do_email_and_store(carrier_data):
        carrier_data['email'] = scrape_carrier_email(carrier_data.get('usdot'))
        with write_lock:
            writer.writerow({c: carrier_data.get(c, '') for c in CSV_COLUMNS})
            csv_file.flush()
            found['count'] += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=EMAIL_WORKERS) as email_pool, \
         concurrent.futures.ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as lookup_pool:

        lookup_futures = {lookup_pool.submit(do_lookup, mc): mc for mc in range(start_mc, end_mc + 1)}
        email_futures = []

        for future in concurrent.futures.as_completed(lookup_futures):
            carrier_data = future.result()
            processed['count'] += 1
            if carrier_data:
                email_futures.append(email_pool.submit(do_email_and_store, carrier_data))
            if processed['count'] % 100 == 0 or processed['count'] == total:
                elapsed = time.monotonic() - start_time
                print(f"[{processed['count']}/{total}] processed, {found['count']} matches, {elapsed:.0f}s elapsed", flush=True)

        for f in concurrent.futures.as_completed(email_futures):
            f.result()

    csv_file.close()
    elapsed = time.monotonic() - start_time
    print(f'\nDone in {elapsed:.0f}s. {found["count"]} matching carriers found out of {total} scanned.')
    print(f'Written to {out_path} (rows arrive in completion order, not sorted by MC number)')


if __name__ == '__main__':
    main()
