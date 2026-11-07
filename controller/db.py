from __future__ import annotations
import json
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_approvals (
    approval_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    proposed_config TEXT NOT NULL,
    created_minute INTEGER NOT NULL,
    expires_minute INTEGER NOT NULL,
    status TEXT NOT NULL,
    summary TEXT,
    diff TEXT
);

CREATE TABLE IF NOT EXISTS leads (
    lead_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    lang TEXT,
    city_tier TEXT,
    age_band TEXT,
    value_band TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_wf ON leads(workflow_id);

CREATE TABLE IF NOT EXISTS rolling_stats (
    workflow_id TEXT NOT NULL,
    cohort_id TEXT NOT NULL,
    dim TEXT NOT NULL,
    val TEXT NOT NULL,
    day INTEGER NOT NULL,
    dials INTEGER DEFAULT 0,
    connected INTEGER DEFAULT 0,
    engaged INTEGER DEFAULT 0,
    hallucinations INTEGER DEFAULT 0,
    PRIMARY KEY (workflow_id, cohort_id, dim, val, day)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_minute INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    workflow_id TEXT,
    details TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_wf_min ON audit_log(workflow_id, sim_minute);
"""

class DB:
    """Manages SQLite storage with WAL mode and atomic state operations."""

    def __init__(self, db_path: str = "controller.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self):
        with self.conn:
            self.conn.executescript(SCHEMA)
            # Migration check for new columns on existing databases
            cur = self.conn.execute("PRAGMA table_info(pending_approvals)")
            cols = {r["name"] for r in cur.fetchall()}
            if "summary" not in cols:
                self.conn.execute("ALTER TABLE pending_approvals ADD COLUMN summary TEXT")
            if "diff" not in cols:
                self.conn.execute("ALTER TABLE pending_approvals ADD COLUMN diff TEXT")

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        cur = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default

    def get_meta_int(self, key: str, default: int = 0) -> int:
        val = self.get_meta(key)
        return int(val) if val is not None else default

    def set_meta(self, key: str, value: str):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))

    def record_cursor(self, last_chunk_id: int, max_event_id: int, sim_minute: int):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_chunk_id', ?)", (str(last_chunk_id),))
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('max_event_id', ?)", (str(max_event_id),))
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('sim_minute', ?)", (str(sim_minute),))

    def insert_leads_batch(self, leads: list[tuple]):
        if not leads:
            return
        with self.conn:
            self.conn.executemany(
                """INSERT OR IGNORE INTO leads 
                   (lead_id, person_id, workflow_id, campaign_id, lang, city_tier, age_band, value_band)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                leads
            )

    def count_leads_for_workflow(self, workflow_id: str) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM leads WHERE workflow_id = ?", (workflow_id,))
        return cur.fetchone()[0]

    def upsert_stats_batch(self, rows: list[tuple]):
        if not rows:
            return
        with self.conn:
            self.conn.executemany(
                """INSERT INTO rolling_stats 
                   (workflow_id, cohort_id, dim, val, day, dials, connected, engaged, hallucinations)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(workflow_id, cohort_id, dim, val, day) DO UPDATE SET
                       dials = dials + excluded.dials,
                       connected = connected + excluded.connected,
                       engaged = engaged + excluded.engaged,
                       hallucinations = hallucinations + excluded.hallucinations""",
                rows
            )

    def query_stats(self, workflow_id: str, cohort_id: str | None = None,
                    dim: str | None = None, min_day: int | None = None) -> list[dict]:
        query = ["SELECT cohort_id, dim, val, SUM(dials) as dials, SUM(connected) as connected, "
                 "SUM(engaged) as engaged, SUM(hallucinations) as hallucinations "
                 "FROM rolling_stats WHERE workflow_id = ?"]
        params = [workflow_id]
        if cohort_id is not None:
            query.append("AND cohort_id = ?")
            params.append(cohort_id)
        if dim is not None:
            query.append("AND dim = ?")
            params.append(dim)
        if min_day is not None:
            query.append("AND day >= ?")
            params.append(min_day)
        query.append("GROUP BY cohort_id, dim, val")
        cur = self.conn.execute(" ".join(query), params)
        return [dict(r) for r in cur.fetchall()]

    def save_pending_approval(self, approval: dict):
        diff_val = approval.get("diff")
        if isinstance(diff_val, (dict, list)):
            diff_val = json.dumps(diff_val)
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO pending_approvals 
                   (approval_id, proposal_id, workflow_id, base_version, proposed_config, created_minute, expires_minute, status, summary, diff)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    approval["approval_id"],
                    approval["proposal_id"],
                    approval["workflow_id"],
                    approval["base_version"],
                    json.dumps(approval["proposed_config"]),
                    approval.get("created_minute", 0),
                    approval.get("expires_minute", 0),
                    approval.get("status", "pending"),
                    approval.get("summary", ""),
                    diff_val
                )
            )

    def get_approval(self, approval_id: str) -> dict | None:
        cur = self.conn.execute("SELECT * FROM pending_approvals WHERE approval_id = ?", (approval_id,))
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["proposed_config"] = json.loads(d["proposed_config"])
        except Exception:
            pass
        if d.get("diff"):
            try:
                d["diff"] = json.loads(d["diff"])
            except Exception:
                pass
        return d

    def update_approval_status(self, approval_id: str, status: str):
        with self.conn:
            self.conn.execute("UPDATE pending_approvals SET status = ? WHERE approval_id = ?", (status, approval_id))

    def get_active_approvals(self) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM pending_approvals WHERE status IN ('pending', 'approved')")
        res = []
        for r in cur.fetchall():
            d = dict(r)
            try:
                d["proposed_config"] = json.loads(d["proposed_config"])
            except Exception:
                pass
            if d.get("diff"):
                try:
                    d["diff"] = json.loads(d["diff"])
                except Exception:
                    pass
            res.append(d)
        return res

    def log_audit(self, sim_minute: int, event_type: str, workflow_id: str | None, details: dict):
        with self.conn:
            self.conn.execute(
                "INSERT INTO audit_log (sim_minute, event_type, workflow_id, details) VALUES (?, ?, ?, ?)",
                (sim_minute, event_type, workflow_id, json.dumps(details))
            )

    def query_audit(self, workflow_id: str | None = None, event_type: str | None = None,
                    min_sim_minute: int | None = None, max_sim_minute: int | None = None) -> list[dict]:
        query = ["SELECT id, sim_minute, event_type, workflow_id, details, created_at FROM audit_log WHERE 1=1"]
        params = []
        if workflow_id is not None:
            query.append("AND workflow_id = ?")
            params.append(workflow_id)
        if event_type is not None:
            query.append("AND event_type = ?")
            params.append(event_type)
        if min_sim_minute is not None:
            query.append("AND sim_minute >= ?")
            params.append(min_sim_minute)
        if max_sim_minute is not None:
            query.append("AND sim_minute <= ?")
            params.append(max_sim_minute)
        query.append("ORDER BY sim_minute ASC, id ASC")
        cur = self.conn.execute(" ".join(query), params)
        res = []
        for r in cur.fetchall():
            d = dict(r)
            try:
                d["details"] = json.loads(d["details"])
            except Exception:
                pass
            res.append(d)
        return res

    def close(self):
        self.conn.close()
