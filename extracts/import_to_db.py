"""Import a CSV produced by extract_mc_range.py into the shared database as a
named folder, instead of leaving the leads unassociated (job_row_id=None).

Unlike extract_mc_range.py, this script *does* import the app package - its
whole purpose is DB interaction, so the app/Flask/DB dependency here is
expected rather than something to avoid.

Usage: python import_to_db.py <csv_path> <folder_name> <agent_username>
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app import models

INT_FIELDS = {'mc_number', 'power_units', 'drivers'}


def main():
    csv_path, folder_name, agent_username = sys.argv[1], sys.argv[2], sys.argv[3]

    agent = models.get_user_by_username(agent_username)
    if agent is None:
        print(f'No user "{agent_username}" found.')
        sys.exit(1)

    with open(csv_path, newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print('CSV is empty, nothing to import.')
        return

    mc_numbers = [int(r['mc_number']) for r in rows if r.get('mc_number')]
    start_mc, end_mc = min(mc_numbers), max(mc_numbers)

    job_uuid = f'import-{os.path.basename(csv_path)}'
    job_row_id = models.create_search_job(
        job_uuid, agent['id'], start_mc, end_mc, total=len(rows), name=folder_name,
    )
    models.update_search_job(job_uuid, status='done', processed=len(rows), found=len(rows))

    inserted = 0
    for row in rows:
        fields = dict(row)
        for k in INT_FIELDS:
            if fields.get(k):
                try:
                    fields[k] = int(fields[k])
                except ValueError:
                    fields[k] = None
            else:
                fields[k] = None
        models.upsert_lead(fields, job_row_id=job_row_id, agent_id=agent['id'])
        inserted += 1

    print(f'Imported {inserted} leads into folder "{folder_name}" (job id {job_row_id}).')


if __name__ == '__main__':
    main()
