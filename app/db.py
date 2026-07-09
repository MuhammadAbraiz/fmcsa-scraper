import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, 'app.db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'agent')),
    full_name TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS search_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uuid TEXT NOT NULL UNIQUE,
    agent_id INTEGER NOT NULL REFERENCES users(id),
    start_mc INTEGER NOT NULL,
    end_mc INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    processed INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    found INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_search_jobs_agent ON search_jobs(agent_id);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usdot TEXT NOT NULL UNIQUE,
    mc_number INTEGER,
    legal_name TEXT,
    mc_mx_ff_numbers TEXT,
    entity_type TEXT,
    address TEXT,
    phone TEXT,
    email TEXT,
    power_units INTEGER,
    drivers INTEGER,
    mcs_150_form_date TEXT,
    mcs_150_mileage TEXT,
    mcs_150_mileage_year TEXT,
    out_of_service_date TEXT,
    operating_status TEXT,
    operation_classification TEXT,
    carrier_operation TEXT,
    cargo_carried TEXT,
    likely_equipment TEXT,
    first_found_job_id INTEGER REFERENCES search_jobs(id),
    first_found_by_agent_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_leads_mc_number ON leads(mc_number);
CREATE INDEX IF NOT EXISTS idx_leads_first_found_job ON leads(first_found_job_id);

CREATE TABLE IF NOT EXISTS call_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    agent_id INTEGER NOT NULL REFERENCES users(id),
    called_at TEXT NOT NULL DEFAULT (datetime('now')),
    outcome TEXT,
    note TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_call_logs_agent ON call_logs(agent_id);
CREATE INDEX IF NOT EXISTS idx_call_logs_lead ON call_logs(lead_id);
"""


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def init_db():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
