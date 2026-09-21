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
from app.db import get_connection

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

    # Reuse the folder if a previous (interrupted) run already created it, so
    # re-running finishes the job instead of failing on the unique job_uuid.
    job_uuid = f'import-{os.path.basename(csv_path)}'
    job = models.get_search_job(job_uuid)
    if job:
        job_row_id = job['id']
    else:
        job_row_id = models.create_search_job(
            job_uuid, agent['id'], start_mc, end_mc, total=len(rows), name=folder_name,
        )

    lead_cols = [c for c in models.LEAD_FIELDS if c in rows[0]]
    prepared = []
    for row in rows:
        if not row.get('usdot'):
            continue
        fields = dict(row)
        for k in INT_FIELDS:
            if fields.get(k):
                try:
                    fields[k] = int(fields[k])
                except ValueError:
                    fields[k] = None
            else:
                fields[k] = None
        prepared.append(fields)

    # Bulk path: one query to learn which USDOTs already exist, then batched
    # multi-row INSERT/UPDATE - per-row upsert_lead() costs several network
    # round trips per lead, which is minutes-to-an-hour at thousands of rows.
    conn = get_connection()
    try:
        existing = {r['usdot'] for r in conn.execute('SELECT usdot FROM leads').fetchall()}
        new_rows = [f for f in prepared if f['usdot'] not in existing]
        old_rows = [f for f in prepared if f['usdot'] in existing]

        insert_cols = ['usdot'] + lead_cols + ['first_found_job_id', 'first_found_by_agent_id']
        insert_sql = (f"INSERT INTO leads ({', '.join(insert_cols)}) "
                      f"VALUES ({', '.join(['%s'] * len(insert_cols))})")
        cur = conn._raw.cursor()
        for i in range(0, len(new_rows), 500):
            cur.executemany(insert_sql, [
                [f['usdot']] + [f.get(c) for c in lead_cols] + [job_row_id, agent['id']]
                for f in new_rows[i:i + 500]
            ])
            conn.commit()
        set_clause = ', '.join(f'{c} = %s' for c in lead_cols)
        update_sql = f'UPDATE leads SET {set_clause}, updated_at = NOW() WHERE usdot = %s'
        for i in range(0, len(old_rows), 500):
            cur.executemany(update_sql, [
                [f.get(c) for c in lead_cols] + [f['usdot']] for f in old_rows[i:i + 500]
            ])
            conn.commit()
    finally:
        conn.close()

    models.update_search_job(job_uuid, status='done', processed=len(rows), found=len(rows))
    print(f'Imported {len(new_rows)} new + updated {len(old_rows)} existing leads '
          f'into folder "{folder_name}" (job id {job_row_id}).')


if __name__ == '__main__':
    main()
