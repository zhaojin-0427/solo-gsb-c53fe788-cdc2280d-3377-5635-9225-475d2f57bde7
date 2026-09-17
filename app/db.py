"""SQLite 持久层：建表、初始化、种子数据与通用查询助手。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from . import engine

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("APP_DB", os.path.join(BASE_DIR, "data", "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_versions (
  version    INTEGER PRIMARY KEY,
  rules      TEXT NOT NULL,
  note       TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  event_id     TEXT PRIMARY KEY,
  scenario_id  TEXT,
  seq          INTEGER NOT NULL,
  event_type   TEXT NOT NULL,
  scope        TEXT NOT NULL DEFAULT 'default',
  valid_time   TEXT NOT NULL,
  recorded_at  TEXT NOT NULL,
  payload      TEXT NOT NULL DEFAULT '{}',
  retracted    INTEGER NOT NULL DEFAULT 0,
  retracted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_seq ON events(seq);
CREATE INDEX IF NOT EXISTS idx_events_scenario ON events(scenario_id);
CREATE TABLE IF NOT EXISTS scenarios (
  id                TEXT PRIMARY KEY,
  name              TEXT NOT NULL,
  fork_seq          INTEGER NOT NULL,
  base_rule_version INTEGER NOT NULL,
  rules             TEXT NOT NULL,
  rev               INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenario_retractions (
  scenario_id TEXT NOT NULL,
  event_id    TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (scenario_id, event_id)
);
"""

SEED_RULES = [
    {
        "rule_id": "pay-deposit",
        "name": "支付定金",
        "trigger": {"event_type": "contract.signed"},
        "deadline": {"amount": 3, "unit": "days"},
        "fulfill_event_type": "payment.deposit",
        "pause_event_types": ["performance.suspended"],
        "resume_event_types": ["performance.resumed"],
        "substitute_event_types": ["obligation.substituted"],
        "depends_on": [],
    },
    {
        "rule_id": "deliver-goods",
        "name": "交付货物",
        "trigger": {"event_type": "contract.signed"},
        "deadline": {"amount": 10, "unit": "days"},
        "fulfill_event_type": "delivery.completed",
        "pause_event_types": ["performance.suspended"],
        "resume_event_types": ["performance.resumed"],
        "substitute_event_types": ["obligation.substituted"],
        "depends_on": ["pay-deposit"],
    },
    {
        "rule_id": "pay-balance",
        "name": "支付尾款",
        "trigger": {"event_type": "delivery.completed"},
        "deadline": {"amount": 7, "unit": "days"},
        "fulfill_event_type": "payment.balance",
        "pause_event_types": ["performance.suspended"],
        "resume_event_types": ["performance.resumed"],
        "substitute_event_types": ["obligation.substituted"],
        "depends_on": [],
    },
]

SEED_EVENTS = [
    ("seed-001", "contract.signed", "C-001", "2026-09-01T09:00:00Z",
     {"contract_no": "C-001", "amount": 100000}),
    ("seed-002", "contract.signed", "C-002", "2026-09-01T09:05:00Z",
     {"contract_no": "C-002", "amount": 50000}),
    ("seed-003", "payment.deposit", "C-001", "2026-09-02T10:00:00Z",
     {"amount": 30000}),
    ("seed-004", "performance.suspended", "C-001", "2026-09-03T00:00:00Z",
     {"reason": "不可抗力"}),
    ("seed-005", "performance.resumed", "C-001", "2026-09-05T00:00:00Z", {}),
    ("seed-006", "obligation.substituted", "C-002", "2026-09-06T09:00:00Z",
     {"rule_id": "deliver-goods", "note": "双方同意以等值服务替代交付"}),
    ("seed-007", "delivery.completed", "C-001", "2026-09-08T15:00:00Z", {}),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """启动时完成建库建表；空库时写入种子规则与示例事件。"""
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        if get_meta(conn, "state_version") is None:
            set_meta(conn, "state_version", "0")
            set_meta(conn, "event_seq", "0")
            conn.execute(
                "INSERT INTO rule_versions(version, rules, note, created_at) VALUES (1, ?, ?, ?)",
                (json.dumps(SEED_RULES, ensure_ascii=False), "初始示例规则", now_iso()),
            )
            for eid, etype, scope, vt, payload in SEED_EVENTS:
                conn.execute(
                    """INSERT INTO events(event_id, scenario_id, seq, event_type, scope,
                                          valid_time, recorded_at, payload)
                       VALUES (?, NULL, ?, ?, ?, ?, ?, ?)""",
                    (eid, next_seq(conn), etype, scope, vt, now_iso(),
                     json.dumps(payload, ensure_ascii=False)),
                )
            bump_state_version(conn)
        conn.commit()
    finally:
        conn.close()


# ---------- 通用助手 ----------

def get_meta(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def get_state_version(conn: sqlite3.Connection) -> int:
    return int(get_meta(conn, "state_version") or 0)


def bump_state_version(conn: sqlite3.Connection) -> int:
    v = get_state_version(conn) + 1
    set_meta(conn, "state_version", str(v))
    return v


def next_seq(conn: sqlite3.Connection) -> int:
    seq = int(get_meta(conn, "event_seq") or 0) + 1
    set_meta(conn, "event_seq", str(seq))
    return seq


def row_to_event(row: sqlite3.Row) -> dict:
    e = dict(row)
    e["payload"] = json.loads(e["payload"] or "{}")
    e["retracted"] = bool(e["retracted"])
    return e


def current_rules(conn: sqlite3.Connection):
    row = conn.execute(
        "SELECT version, rules, note, created_at FROM rule_versions ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if not row:
        return 0, []
    return row["version"], json.loads(row["rules"])


def mainline_events(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        "SELECT * FROM events WHERE scenario_id IS NULL ORDER BY seq"
    ).fetchall()
    return [row_to_event(r) for r in rows]


def get_scenario(conn: sqlite3.Connection, sid: str):
    row = conn.execute("SELECT * FROM scenarios WHERE id = ?", (sid,)).fetchone()
    return dict(row) if row else None


def list_scenarios(conn: sqlite3.Connection) -> list:
    rows = conn.execute("SELECT * FROM scenarios ORDER BY created_at, id").fetchall()
    return [dict(r) for r in rows]


def scenario_retractions(conn: sqlite3.Connection, sid: str) -> set:
    rows = conn.execute(
        "SELECT event_id FROM scenario_retractions WHERE scenario_id = ?", (sid,)
    ).fetchall()
    return {r["event_id"] for r in rows}


def scenario_events_view(conn: sqlite3.Connection, sc: dict) -> list:
    """分叉视角下的事件列表：继承的主线事件 + 分叉自有事件，标注撤回状态。"""
    sret = scenario_retractions(conn, sc["id"])
    rows = []
    for e in mainline_events(conn):
        if e["seq"] <= sc["fork_seq"]:
            rows.append({
                **e,
                "inherited": True,
                "scenario_retracted": e["event_id"] in sret,
                "effective_retracted": e["retracted"] or e["event_id"] in sret,
            })
    own = conn.execute(
        "SELECT * FROM events WHERE scenario_id = ? ORDER BY seq", (sc["id"],)
    ).fetchall()
    for r in own:
        e = row_to_event(r)
        rows.append({
            **e,
            "inherited": False,
            "scenario_retracted": False,
            "effective_retracted": e["retracted"],
        })
    rows.sort(key=lambda e: (engine.parse_time(e["valid_time"]), e["seq"]))
    return rows


def compute_state(conn: sqlite3.Connection, scenario_id, as_of=None) -> dict:
    """计算主线或某分叉的完整推演状态。"""
    if scenario_id is None:
        version, rules = current_rules(conn)
        events = [e for e in mainline_events(conn) if not e["retracted"]]
        result = engine.replay(rules, events, as_of)
        return {
            "scenario": "main",
            "state_version": get_state_version(conn),
            "rules_version": version,
            "rules": rules,
            **result,
        }
    sc = get_scenario(conn, scenario_id)
    if not sc:
        return None
    rules = json.loads(sc["rules"])
    events = [e for e in scenario_events_view(conn, sc) if not e["effective_retracted"]]
    result = engine.replay(rules, events, as_of)
    return {
        "scenario": sc["id"],
        "scenario_name": sc["name"],
        "fork_seq": sc["fork_seq"],
        "base_rule_version": sc["base_rule_version"],
        "rev": sc["rev"],
        "state_version": get_state_version(conn),
        "rules": rules,
        **result,
    }
