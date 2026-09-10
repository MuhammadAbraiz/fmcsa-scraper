import time

from werkzeug.security import generate_password_hash, check_password_hash

from .db import get_connection

LEAD_FIELDS = [
    'mc_number', 'legal_name', 'mc_mx_ff_numbers', 'entity_type', 'address',
    'phone', 'email', 'power_units', 'drivers', 'mcs_150_form_date',
    'mcs_150_mileage', 'mcs_150_mileage_year', 'out_of_service_date',
    'operating_status', 'operation_classification', 'carrier_operation',
    'cargo_carried', 'likely_equipment',
]

CALL_OUTCOMES = ['Interested', 'Not Interested', 'No Answer', 'Voicemail', 'Callback Later', 'Other']


# --- users ---

def create_user(username, password, role, full_name=''):
    conn = get_connection()
    try:
        cur = conn.execute(
            'INSERT INTO users (username, password_hash, role, full_name) VALUES (?, ?, ?, ?)',
            (username, generate_password_hash(password), role, full_name),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def verify_login(username, password):
    user = get_user_by_username(username)
    if not user:
        return None
    if not check_password_hash(user['password_hash'], password):
        return None
    return user


def get_user_by_username(username):
    conn = get_connection()
    try:
        row = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_id(user_id):
    conn = get_connection()
    try:
        row = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# The hosted DB now sits across a real network hop from the app (see
# app/db.py's pooling comment) - every query costs a meaningful round trip.
# before_app_request calls this on *every single request* just to populate
# g.user, which was free with local SQLite but adds up badly now. A few
# seconds of staleness on an admin deactivating someone mid-session is an
# acceptable trade for not paying a DB round trip on every click.
_session_user_cache = {}
SESSION_USER_CACHE_TTL_SECONDS = 5


def get_user_for_session(user_id):
    cached = _session_user_cache.get(user_id)
    if cached and time.monotonic() - cached[1] < SESSION_USER_CACHE_TTL_SECONDS:
        return cached[0]
    user = get_user_by_id(user_id)
    _session_user_cache[user_id] = (user, time.monotonic())
    return user


def list_agents():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM users WHERE role = 'agent' ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_user_active(user_id, is_active):
    conn = get_connection()
    try:
        conn.execute('UPDATE users SET is_active = ? WHERE id = ?', (1 if is_active else 0, user_id))
        conn.commit()
    finally:
        conn.close()


def set_user_password(user_id, new_password):
    conn = get_connection()
    try:
        conn.execute(
            'UPDATE users SET password_hash = ? WHERE id = ?',
            (generate_password_hash(new_password), user_id),
        )
        conn.commit()
    finally:
        conn.close()


# --- login rate limiting ---
#
# The app is now reachable from the public internet (not just the tailnet),
# so an unlimited login form is an open door for credential-stuffing bots.
# Tracked per (username, IP) pair rather than just IP, so one attacker
# working through a list of usernames from a single IP is caught by the IP
# limit, while a distributed attack hammering one specific username from many
# IPs is caught by the username limit.

LOGIN_ATTEMPT_WINDOW_MINUTES = 15
MAX_ATTEMPTS_PER_USERNAME = 5
MAX_ATTEMPTS_PER_IP = 20


def record_failed_login(username, ip_address):
    conn = get_connection()
    try:
        conn.execute(
            'INSERT INTO login_attempts (username, ip_address) VALUES (?, ?)',
            (username, ip_address),
        )
        # Opportunistic cleanup instead of a cron job - keeps the table from
        # growing unbounded under sustained bot noise, without adding a
        # scheduling dependency for a table that's naturally self-limiting.
        conn.execute(
            f"DELETE FROM login_attempts WHERE attempted_at < NOW() - INTERVAL {LOGIN_ATTEMPT_WINDOW_MINUTES * 4} MINUTE",
        )
        conn.commit()
    finally:
        conn.close()


def clear_failed_logins(username):
    conn = get_connection()
    try:
        conn.execute('DELETE FROM login_attempts WHERE username = ?', (username,))
        conn.commit()
    finally:
        conn.close()


def is_login_rate_limited(username, ip_address):
    conn = get_connection()
    try:
        window = f'NOW() - INTERVAL {LOGIN_ATTEMPT_WINDOW_MINUTES} MINUTE'
        by_username = conn.execute(
            f'SELECT COUNT(*) AS cnt FROM login_attempts WHERE username = ? AND attempted_at >= {window}',
            (username,),
        ).fetchone()['cnt']
        if by_username >= MAX_ATTEMPTS_PER_USERNAME:
            return True
        by_ip = conn.execute(
            f'SELECT COUNT(*) AS cnt FROM login_attempts WHERE ip_address = ? AND attempted_at >= {window}',
            (ip_address,),
        ).fetchone()['cnt']
        return by_ip >= MAX_ATTEMPTS_PER_IP
    finally:
        conn.close()


# --- search jobs ---

def create_search_job(job_uuid, agent_id, start_mc, end_mc, total):
    conn = get_connection()
    try:
        cur = conn.execute(
            'INSERT INTO search_jobs (job_uuid, agent_id, start_mc, end_mc, total) VALUES (?, ?, ?, ?, ?)',
            (job_uuid, agent_id, start_mc, end_mc, total),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_search_job(job_uuid, **fields):
    if not fields:
        return
    auto_finish = fields.get('status') in ('done', 'error')

    cols = [f'{k} = ?' for k in fields]
    params = list(fields.values())
    if auto_finish:
        cols.append('finished_at = NOW()')
    params.append(job_uuid)

    conn = get_connection()
    try:
        conn.execute(f"UPDATE search_jobs SET {', '.join(cols)} WHERE job_uuid = ?", params)
        conn.commit()
    finally:
        conn.close()


def get_search_job(job_uuid):
    conn = get_connection()
    try:
        row = conn.execute('SELECT * FROM search_jobs WHERE job_uuid = ?', (job_uuid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_search_jobs(agent_id=None, limit=50):
    conn = get_connection()
    try:
        if agent_id is not None:
            rows = conn.execute(
                'SELECT sj.*, u.username AS agent_username FROM search_jobs sj '
                'JOIN users u ON u.id = sj.agent_id '
                'WHERE sj.agent_id = ? ORDER BY sj.started_at DESC LIMIT ?',
                (agent_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT sj.*, u.username AS agent_username FROM search_jobs sj '
                'JOIN users u ON u.id = sj.agent_id '
                'ORDER BY sj.started_at DESC LIMIT ?',
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- leads ---

def upsert_lead(fields, job_row_id, agent_id):
    """fields must include 'usdot' plus any of LEAD_FIELDS. Returns the lead row (dict)."""
    usdot = fields.get('usdot')
    if not usdot:
        return None

    conn = get_connection()
    try:
        existing = conn.execute('SELECT id FROM leads WHERE usdot = ?', (usdot,)).fetchone()
        cols = [c for c in LEAD_FIELDS if c in fields]
        if existing:
            set_clause = ', '.join(f'{c} = ?' for c in cols)
            params = [fields[c] for c in cols] + [usdot]
            conn.execute(
                f'UPDATE leads SET {set_clause}, updated_at = NOW() WHERE usdot = ?',
                params,
            )
        else:
            insert_cols = ['usdot'] + cols + ['first_found_job_id', 'first_found_by_agent_id']
            placeholders = ', '.join('?' for _ in insert_cols)
            params = [usdot] + [fields[c] for c in cols] + [job_row_id, agent_id]
            conn.execute(
                f"INSERT INTO leads ({', '.join(insert_cols)}) VALUES ({placeholders})",
                params,
            )
        conn.commit()
        row = conn.execute('SELECT * FROM leads WHERE usdot = ?', (usdot,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


EQUIPMENT_FILTERS = ['Dry Van', 'Reefer', 'Flatbed', 'Box Truck', 'Unknown']


def _lead_filter_clauses(q=None, equipment=None, mc_min=None, mc_max=None):
    clauses = []
    params = []
    if q:
        like = f'%{q}%'
        clauses.append('(legal_name LIKE ? OR usdot LIKE ? OR mc_number LIKE ?)')
        params += [like, like, like]
    if equipment:
        clauses.append('likely_equipment LIKE ?')
        params.append(f'{equipment}%')
    if mc_min is not None:
        clauses.append('mc_number >= ?')
        params.append(mc_min)
    if mc_max is not None:
        clauses.append('mc_number <= ?')
        params.append(mc_max)
    return clauses, params


def list_leads(q=None, equipment=None, mc_min=None, mc_max=None, limit=500, offset=0):
    conn = get_connection()
    try:
        clauses, params = _lead_filter_clauses(q, equipment, mc_min, mc_max)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
        rows = conn.execute(
            f'SELECT l.*, EXISTS(SELECT 1 FROM call_logs cl WHERE cl.lead_id = l.id) AS been_called '
            f'FROM leads l {where} ORDER BY l.created_at DESC LIMIT ? OFFSET ?',
            params + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_leads(q=None, equipment=None, mc_min=None, mc_max=None):
    conn = get_connection()
    try:
        clauses, params = _lead_filter_clauses(q, equipment, mc_min, mc_max)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
        row = conn.execute(f'SELECT COUNT(*) AS cnt FROM leads {where}', params).fetchone()
        return row['cnt']
    finally:
        conn.close()


def _uncalled_filter_clauses(q=None, equipment=None, mc_min=None, mc_max=None, job_row_id=None):
    clauses = ["cl.id IS NULL", "l.phone IS NOT NULL", "l.phone != ''"]
    params = []
    if job_row_id is not None:
        clauses.append('l.first_found_job_id = ?')
        params.append(job_row_id)
    if q:
        like = f'%{q}%'
        clauses.append('(l.legal_name LIKE ? OR l.usdot LIKE ? OR l.mc_number LIKE ?)')
        params += [like, like, like]
    if equipment:
        clauses.append('l.likely_equipment LIKE ?')
        params.append(f'{equipment}%')
    if mc_min is not None:
        clauses.append('l.mc_number >= ?')
        params.append(mc_min)
    if mc_max is not None:
        clauses.append('l.mc_number <= ?')
        params.append(mc_max)
    return clauses, params


def list_uncalled_leads(limit=300, q=None, equipment=None, mc_min=None, mc_max=None, job_row_id=None):
    """Leads with no call_logs row at all — the default calling queue.

    Fetched in batches (not all at once — the pool can be tens of thousands
    of rows) and re-queried as each batch is worked through, so the agent
    never has to think about a page size.
    """
    conn = get_connection()
    try:
        clauses, params = _uncalled_filter_clauses(q, equipment, mc_min, mc_max, job_row_id)
        rows = conn.execute(
            'SELECT l.* FROM leads l '
            'LEFT JOIN call_logs cl ON cl.lead_id = l.id '
            f"WHERE {' AND '.join(clauses)} "
            'ORDER BY l.created_at ASC LIMIT ?',
            params + [limit],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_uncalled_leads(q=None, equipment=None, mc_min=None, mc_max=None, job_row_id=None):
    conn = get_connection()
    try:
        clauses, params = _uncalled_filter_clauses(q, equipment, mc_min, mc_max, job_row_id)
        row = conn.execute(
            'SELECT COUNT(*) AS cnt FROM leads l '
            'LEFT JOIN call_logs cl ON cl.lead_id = l.id '
            f"WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        return row['cnt']
    finally:
        conn.close()


def get_lead(lead_id):
    conn = get_connection()
    try:
        row = conn.execute(
            'SELECT l.*, EXISTS(SELECT 1 FROM call_logs cl WHERE cl.lead_id = l.id) AS been_called '
            'FROM leads l WHERE l.id = ?',
            (lead_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_new_leads_for_job(job_row_id, after_id=0):
    conn = get_connection()
    try:
        rows = conn.execute(
            'SELECT * FROM leads WHERE first_found_job_id = ? AND id > ? ORDER BY id ASC',
            (job_row_id, after_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_all_leads_for_export():
    conn = get_connection()
    try:
        rows = conn.execute('SELECT * FROM leads ORDER BY created_at DESC').fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- call logs ---

def create_call_log(lead_id, agent_id):
    conn = get_connection()
    try:
        cur = conn.execute(
            'INSERT INTO call_logs (lead_id, agent_id) VALUES (?, ?)',
            (lead_id, agent_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_call_log(call_id):
    conn = get_connection()
    try:
        row = conn.execute('SELECT * FROM call_logs WHERE id = ?', (call_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_call_outcome(call_id, outcome, note, requesting_user):
    call = get_call_log(call_id)
    if not call:
        return False
    if requesting_user['role'] != 'admin' and requesting_user['id'] != call['agent_id']:
        return False
    conn = get_connection()
    try:
        conn.execute(
            'UPDATE call_logs SET outcome = ?, note = ?, updated_at = NOW() WHERE id = ?',
            (outcome, note, call_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def _call_log_filter_clauses(agent_id=None, shift_date=None):
    clauses = []
    params = []
    if agent_id is not None:
        clauses.append('cl.agent_id = ?')
        params.append(agent_id)
    if shift_date:
        date_sql, date_params = _period_clause('cl.called_at', custom_date=shift_date)
        clauses.append(date_sql)
        params += date_params
    return clauses, params


def list_call_logs(agent_id=None, limit=200, offset=0, shift_date=None):
    conn = get_connection()
    try:
        clauses, params = _call_log_filter_clauses(agent_id, shift_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
        rows = conn.execute(
            'SELECT cl.*, u.username AS agent_username, l.legal_name, l.usdot, l.phone AS lead_phone '
            'FROM call_logs cl '
            'JOIN users u ON u.id = cl.agent_id '
            'JOIN leads l ON l.id = cl.lead_id '
            f'{where} ORDER BY cl.called_at DESC LIMIT ? OFFSET ?',
            params + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_call_logs(agent_id=None, shift_date=None):
    conn = get_connection()
    try:
        clauses, params = _call_log_filter_clauses(agent_id, shift_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
        row = conn.execute(f'SELECT COUNT(*) AS cnt FROM call_logs cl {where}', params).fetchone()
        return row['cnt']
    finally:
        conn.close()


def get_call_logs_for_lead(lead_id):
    conn = get_connection()
    try:
        rows = conn.execute(
            'SELECT cl.*, u.username AS agent_username FROM call_logs cl '
            'JOIN users u ON u.id = cl.agent_id '
            'WHERE cl.lead_id = ? ORDER BY cl.called_at DESC',
            (lead_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- dashboard summary / call stats ---

# Timestamps are stored in UTC (MySQL's NOW() on a UTC-configured server). The
# team works a night shift in Pakistan — 5PM to 3AM PKT — that covers US
# daytime. A shift that starts Monday 5PM and runs past midnight into Tuesday
# 3AM is still "Monday's calling," so a plain PKT calendar-day boundary is
# wrong: it would split one shift across two dates. Instead we compute a
# "shift date" per call: anything before 3AM PKT counts toward the previous
# calendar day. PKT itself is a fixed UTC+5 with no DST, which keeps this
# arithmetic simple (no seasonal offset changes to worry about).
SHIFT_CUTOFF = '03:00:00'  # calls before this PKT time belong to the previous day's shift

CALL_STAT_PERIODS = ('today', 'week', 'all_time')


def _shift_date_expr(column):
    """SQL expression: the shift-day (as a date) that `column` falls into."""
    shifted = f'DATE_ADD({column}, INTERVAL 5 HOUR)'
    return (
        f"(CASE WHEN TIME({shifted}) < '{SHIFT_CUTOFF}' "
        f"THEN DATE(DATE_SUB({shifted}, INTERVAL 1 DAY)) "
        f'ELSE DATE({shifted}) END)'
    )


def _period_clause(column, period='today', custom_date=None):
    """Returns (sql_clause, params) filtering `column` by shift-day period."""
    shift_expr = _shift_date_expr(column)
    if custom_date:
        return f'{shift_expr} = ?', [custom_date]
    current_shift_expr = _shift_date_expr('NOW()')
    if period == 'week':
        return f'{shift_expr} >= DATE_SUB({current_shift_expr}, INTERVAL 6 DAY)', []
    if period == 'all_time':
        return '1=1', []
    return f'{shift_expr} = {current_shift_expr}', []  # 'today' (current shift) / default


def current_shift_date():
    """Today's shift date (as the admin would label it), e.g. for defaulting a date picker."""
    conn = get_connection()
    try:
        row = conn.execute(f"SELECT {_shift_date_expr('NOW()')} AS shift_date").fetchone()
        return row['shift_date']
    finally:
        conn.close()


def _outcome_sum_columns(column_prefix='cl'):
    parts = [f'COUNT({column_prefix}.id) AS total']
    for outcome in CALL_OUTCOMES:
        alias = outcome.lower().replace(' ', '_')
        parts.append(f"SUM(CASE WHEN {column_prefix}.outcome = '{outcome}' THEN 1 ELSE 0 END) AS {alias}")
    # id IS NOT NULL excludes the LEFT JOIN "no match" row (agent_call_stats)
    # from being miscounted as a pending call — only a real call_logs row
    # with a genuinely unset outcome should count here.
    parts.append(f'SUM(CASE WHEN {column_prefix}.id IS NOT NULL AND {column_prefix}.outcome IS NULL THEN 1 ELSE 0 END) AS pending')
    return ', '.join(parts)


def call_outcome_breakdown(period='today', agent_id=None, custom_date=None):
    """Team-wide (or single-agent) call totals + outcome breakdown for a shift-day period."""
    if period not in CALL_STAT_PERIODS:
        period = 'today'
    conn = get_connection()
    try:
        period_sql, period_params = _period_clause('called_at', period, custom_date)
        clauses = [period_sql]
        params = list(period_params)
        if agent_id is not None:
            clauses.append('agent_id = ?')
            params.append(agent_id)
        where = ' AND '.join(clauses)
        row = conn.execute(
            f"SELECT {_outcome_sum_columns('call_logs')} FROM call_logs WHERE {where}",
            params,
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def call_outcome_breakdown_all_periods(agent_id=None):
    """Same shape as calling call_outcome_breakdown() once per CALL_STAT_PERIODS
    entry ({period: {total, <outcome>: n, ..., pending: n}}), but in a single
    round trip instead of three - the admin agent-detail page needs all three
    periods at once, and each separate query pays the hosted DB's network
    latency independently."""
    conn = get_connection()
    try:
        period_conditions = {p: _period_clause('called_at', p)[0] for p in CALL_STAT_PERIODS}
        parts = []
        for period, cond in period_conditions.items():
            parts.append(f'SUM(CASE WHEN {cond} THEN 1 ELSE 0 END) AS {period}__total')
            for outcome in CALL_OUTCOMES:
                alias = outcome.lower().replace(' ', '_')
                parts.append(
                    f"SUM(CASE WHEN {cond} AND outcome = '{outcome}' THEN 1 ELSE 0 END) AS {period}__{alias}"
                )
            parts.append(f'SUM(CASE WHEN {cond} AND outcome IS NULL THEN 1 ELSE 0 END) AS {period}__pending')

        where = 'agent_id = ?' if agent_id is not None else '1=1'
        params = [agent_id] if agent_id is not None else []
        row = conn.execute(f"SELECT {', '.join(parts)} FROM call_logs WHERE {where}", params).fetchone()

        result = {}
        for period in period_conditions:
            result[period] = {'total': row[f'{period}__total'], 'pending': row[f'{period}__pending']}
            for outcome in CALL_OUTCOMES:
                alias = outcome.lower().replace(' ', '_')
                result[period][alias] = row[f'{period}__{alias}']
        return result
    finally:
        conn.close()


def agent_call_stats(period='today', custom_date=None):
    """Per-agent call totals + outcome breakdown for a shift-day period.

    Uses a LEFT JOIN with the period filter in the ON clause (not WHERE) so
    agents with zero calls in the period still show up with all-zero counts,
    instead of disappearing from the table.
    """
    if period not in CALL_STAT_PERIODS:
        period = 'today'
    conn = get_connection()
    try:
        join_sql, join_params = _period_clause('cl.called_at', period, custom_date)
        rows = conn.execute(
            f"SELECT u.id AS agent_id, u.username, u.full_name, {_outcome_sum_columns('cl')} "
            'FROM users u '
            f'LEFT JOIN call_logs cl ON cl.agent_id = u.id AND {join_sql} '
            "WHERE u.role = 'agent' "
            'GROUP BY u.id, u.username, u.full_name '
            'ORDER BY total DESC, u.username ASC',
            join_params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def dashboard_summary():
    conn = get_connection()
    try:
        # One round trip instead of four - each query used to open its own
        # connection borrow and pay the hosted DB's network latency
        # separately; correlated subqueries let the server compute all four
        # in a single request/response.
        jobs_today_sql, _ = _period_clause('started_at', 'today')
        calls_today_sql, _ = _period_clause('called_at', 'today')
        row = conn.execute(
            'SELECT '
            '(SELECT COUNT(*) FROM leads) AS total_leads, '
            "(SELECT COUNT(*) FROM users WHERE role='agent' AND is_active=1) AS active_agents, "
            f'(SELECT COUNT(*) FROM search_jobs WHERE {jobs_today_sql}) AS jobs_today, '
            f'(SELECT COUNT(*) FROM call_logs WHERE {calls_today_sql}) AS calls_today'
        ).fetchone()
        return dict(row)
    finally:
        conn.close()
