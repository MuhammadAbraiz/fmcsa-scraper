import os
import queue
import threading

import pymysql
import pymysql.cursors

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INT PRIMARY KEY AUTO_INCREMENT,
    username VARCHAR(191) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role ENUM('admin', 'agent') NOT NULL,
    full_name TEXT,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS search_jobs (
    id INT PRIMARY KEY AUTO_INCREMENT,
    job_uuid VARCHAR(64) NOT NULL UNIQUE,
    agent_id INT NOT NULL,
    start_mc INT NOT NULL,
    end_mc INT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'running',
    processed INT NOT NULL DEFAULT 0,
    total INT NOT NULL DEFAULT 0,
    found INT NOT NULL DEFAULT 0,
    message TEXT,
    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at DATETIME,
    FOREIGN KEY (agent_id) REFERENCES users(id)
);
CREATE INDEX idx_search_jobs_agent ON search_jobs(agent_id);

CREATE TABLE IF NOT EXISTS leads (
    id INT PRIMARY KEY AUTO_INCREMENT,
    usdot VARCHAR(64) NOT NULL UNIQUE,
    mc_number INT,
    legal_name TEXT,
    mc_mx_ff_numbers TEXT,
    entity_type TEXT,
    address TEXT,
    phone TEXT,
    email TEXT,
    power_units INT,
    drivers INT,
    mcs_150_form_date TEXT,
    mcs_150_mileage TEXT,
    mcs_150_mileage_year TEXT,
    out_of_service_date TEXT,
    operating_status TEXT,
    operation_classification TEXT,
    carrier_operation TEXT,
    cargo_carried TEXT,
    likely_equipment TEXT,
    first_found_job_id INT,
    first_found_by_agent_id INT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (first_found_job_id) REFERENCES search_jobs(id),
    FOREIGN KEY (first_found_by_agent_id) REFERENCES users(id)
);
CREATE INDEX idx_leads_mc_number ON leads(mc_number);
CREATE INDEX idx_leads_first_found_job ON leads(first_found_job_id);

CREATE TABLE IF NOT EXISTS call_logs (
    id INT PRIMARY KEY AUTO_INCREMENT,
    lead_id INT NOT NULL,
    agent_id INT NOT NULL,
    called_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    outcome TEXT,
    note TEXT,
    updated_at DATETIME,
    FOREIGN KEY (lead_id) REFERENCES leads(id),
    FOREIGN KEY (agent_id) REFERENCES users(id)
);
CREATE INDEX idx_call_logs_agent ON call_logs(agent_id);
CREATE INDEX idx_call_logs_lead ON call_logs(lead_id);

CREATE TABLE IF NOT EXISTS login_attempts (
    id INT PRIMARY KEY AUTO_INCREMENT,
    username VARCHAR(191) NOT NULL,
    ip_address VARCHAR(45) NOT NULL,
    attempted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_login_attempts_username ON login_attempts(username, attempted_at);
CREATE INDEX idx_login_attempts_ip ON login_attempts(ip_address, attempted_at);
"""

DB_CONFIG = dict(
    host=os.environ.get('DB_HOST', '127.0.0.1'),
    port=int(os.environ.get('DB_PORT', '3306')),
    user=os.environ.get('DB_USER', ''),
    password=os.environ.get('DB_PASSWORD', ''),
    database=os.environ.get('DB_NAME', ''),
    cursorclass=pymysql.cursors.DictCursor,
    autocommit=False,
)


class Connection:
    """Thin wrapper so callers can keep using sqlite3-style conn.execute(sql, params)
    with '?' placeholders, instead of rewriting every call site for pymysql."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        cur = self._raw.cursor()
        cur.execute(sql.replace('?', '%s'), params)
        return cur

    def commit(self):
        self._raw.commit()

    def close(self):
        _release(self._raw)


def _set_utc(raw):
    raw.cursor().execute("SET time_zone = '+00:00'")


def _connect():
    raw = pymysql.connect(**DB_CONFIG)
    # Force UTC regardless of the server's configured timezone: historical
    # data was migrated in from SQLite's always-UTC datetime('now'), and the
    # shift-date math (models._shift_date_expr) assumes NOW()/CURRENT_TIMESTAMP
    # and stored timestamps share one consistent baseline.
    _set_utc(raw)
    return raw


# Every model-layer call used to open (and immediately close) its own fresh
# TCP+auth connection. That's cheap locally but hosted MySQL providers meter
# new connections per hour per DB user (Hostinger: 500/hr) - a single bulk
# operation of a few hundred rows was enough to exhaust it and take the
# whole app down, since every other request needs a connection too. Pool a
# small number of long-lived connections instead, reused across calls.
POOL_SIZE = int(os.environ.get('DB_POOL_SIZE', '5'))
_pool = queue.Queue(maxsize=POOL_SIZE)
_pool_lock = threading.Lock()
_created = 0


def _acquire():
    global _created
    try:
        raw = _pool.get_nowait()
    except queue.Empty:
        with _pool_lock:
            if _created < POOL_SIZE:
                _created += 1
                return _connect()
        raw = _pool.get()  # at capacity: block until one is released
    try:
        raw.ping(reconnect=True)  # may silently reconnect if the server dropped it
        _set_utc(raw)  # reconnect resets the session, so timezone must be re-applied
    except Exception:
        raw = _connect()
    return raw


def _release(raw):
    try:
        _pool.put_nowait(raw)
    except queue.Full:
        raw.close()


def get_connection():
    return Connection(_acquire())


DUPLICATE_KEY_NAME = 1061  # ER_DUP_KEYNAME: CREATE INDEX has no IF NOT EXISTS in MySQL


def init_db():
    conn = _connect()
    try:
        cur = conn.cursor()
        for statement in SCHEMA.split(';'):
            statement = statement.strip()
            if not statement:
                continue
            try:
                cur.execute(statement)
            except pymysql.err.OperationalError as e:
                if e.args[0] != DUPLICATE_KEY_NAME:
                    raise
        conn.commit()
    finally:
        conn.close()
