"""SQLite storage.

一致性保证：
* 所有写操作在同一把进程内互斥锁 + 单事务中完成，冲突时整事务回滚，绝不部分写入；
* 并发模型为 CAS：客户端持有的分支/版本基线与库内不一致即抛 ConflictError（HTTP 409）；
* 结果只依赖库内持久数据，进程重启后完全一致。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any

from .rules import RuleError, validate_rules
from .timeutil import parse_dt

_write_lock = threading.RLock()
_DB_PATH: str | None = None

SEED_AT = "2026-09-17T00:00:00Z"
MAIN_BRANCH = "main"


class ConflictError(Exception):
    """CAS baseline mismatch (HTTP 409)."""


class NotFoundError(Exception):
    """Referenced row missing (HTTP 404)."""


class BadRequestError(Exception):
    """Invalid client input (HTTP 400)."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_versions (
    version INTEGER PRIMARY KEY,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL REFERENCES rule_versions(version),
    rid TEXT NOT NULL,
    rtype TEXT NOT NULL,
    params TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS branches (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    rule_version INTEGER NOT NULL REFERENCES rule_versions(version),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    root_id TEXT NOT NULL,
    lineage_root_id TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    etype TEXT NOT NULL,
    valid_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL DEFAULT '{}',
    withdrawn INTEGER NOT NULL DEFAULT 0,
    withdraw_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_version ON rules(version);
CREATE INDEX IF NOT EXISTS idx_events_branch ON events(branch_id);
"""


# ---------------------------------------------------------------------------
# connection helpers
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> None:
    global _DB_PATH
    _DB_PATH = db_path
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.commit()
        row = conn.execute("SELECT COUNT(*) FROM rule_versions").fetchone()
        if row[0] == 0:
            _seed(conn)
        conn.commit()
    finally:
        conn.close()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class _Tx:
    def __init__(self):
        self.conn = _connect()

    def __enter__(self):
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()


def write_tx():
    """Serialize all mutating transactions process-wide."""
    _write_lock.acquire()
    tx = _Tx()
    conn = tx.__enter__()

    class _Guard:
        def __enter__(self_inner):
            return conn

        def __exit__(self_inner, exc_type, exc, tb):
            try:
                tx.__exit__(exc_type, exc, tb)
            finally:
                _write_lock.release()

    return _Guard()


def read_conn():
    return _Tx()


# ---------------------------------------------------------------------------
# row mapping
# ---------------------------------------------------------------------------

def _rule_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "rid": r["rid"],
        "rtype": r["rtype"],
        "params": json.loads(r["params"]),
        "description": r["description"],
    }


def _event_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "eid": r["id"],
        "root_event_id": r["root_id"],
        "lineage_root_event_id": r["lineage_root_id"],
        "code": r["code"],
        "etype": r["etype"],
        "valid_at": r["valid_at"],
        "recorded_at": r["recorded_at"],
        "seq": r["seq"],
        "payload": json.loads(r["payload"]),
        "withdrawn": bool(r["withdrawn"]),
        "withdraw_reason": r["withdraw_reason"],
        "created_at": r["created_at"],
    }


def _branch_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "name": r["name"],
        "version": r["version"],
        "rule_version": r["rule_version"],
        "note": r["note"],
        "created_at": r["created_at"],
    }


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------

def list_rule_versions() -> list[dict[str, Any]]:
    with read_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM rule_versions ORDER BY version").fetchall()
        out = []
        for r in rows:
            rules = [_rule_row(x) for x in conn.execute(
                "SELECT * FROM rules WHERE version=? ORDER BY rid", (r["version"],))]
            out.append({"version": r["version"], "note": r["note"],
                        "created_at": r["created_at"], "rules": rules})
        return out


def get_rule_version(version: int) -> dict[str, Any] | None:
    with read_conn() as conn:
        r = conn.execute("SELECT * FROM rule_versions WHERE version=?",
                         (version,)).fetchone()
        if r is None:
            return None
        rules = [_rule_row(x) for x in conn.execute(
            "SELECT * FROM rules WHERE version=? ORDER BY rid", (version,))]
        return {"version": r["version"], "note": r["note"],
                "created_at": r["created_at"], "rules": rules}


def create_rule_version(note: str, raw_rules: list[dict[str, Any]],
                        expected_version: int | None) -> dict[str, Any]:
    norm = validate_rules(raw_rules)  # 校验失败直接抛错，无任何写入
    with write_tx() as conn:
        maxrow = conn.execute(
            "SELECT COALESCE(MAX(version),0) AS m FROM rule_versions").fetchone()
        latest = maxrow["m"]
        if expected_version != latest:
            raise ConflictError(
                f"规则版本基线冲突：期望 {expected_version}，当前最新 {latest}")
        new_version = latest + 1
        conn.execute(
            "INSERT INTO rule_versions(version, note, created_at) VALUES (?,?,?)",
            (new_version, note, _now()))
        for r in norm:
            conn.execute(
                "INSERT INTO rules(version, rid, rtype, params, description)"
                " VALUES (?,?,?,?,?)",
                (new_version, r["rid"], r["rtype"],
                 json.dumps(r["params"], ensure_ascii=False), r["description"]))
        return {"version": new_version, "note": note,
                "created_at": _now(), "rules": norm}


# ---------------------------------------------------------------------------
# branches
# ---------------------------------------------------------------------------

def list_branches() -> list[dict[str, Any]]:
    with read_conn() as conn:
        return [_branch_row(r) for r in conn.execute(
            "SELECT * FROM branches ORDER BY created_at, id")]


def get_branch(conn, branch_id: str) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM branches WHERE id=?", (branch_id,)).fetchone()
    if r is None:
        raise NotFoundError(f"分支不存在: {branch_id}")
    return r


def branch_dict(branch_id: str) -> dict[str, Any]:
    with read_conn() as conn:
        return _branch_row(get_branch(conn, branch_id))


def _replay_ordered_events(conn, branch_id: str, include_withdrawn=False):
    sql = ("SELECT * FROM events WHERE branch_id=? "
           + ("" if include_withdrawn else "AND withdrawn=0 ")
           + "ORDER BY valid_at, seq, code, id")
    return conn.execute(sql, (branch_id,)).fetchall()


def switch_branch_rules(branch_id: str, rule_version: int,
                        expected_branch_version: int) -> dict[str, Any]:
    with write_tx() as conn:
        b = get_branch(conn, branch_id)
        if b["version"] != expected_branch_version:
            raise ConflictError(
                f"分支版本基线冲突：期望 {expected_branch_version}，当前 {b['version']}")
        rv = conn.execute("SELECT 1 FROM rule_versions WHERE version=?",
                          (rule_version,)).fetchone()
        if rv is None:
            raise BadRequestError(f"规则版本不存在: {rule_version}")
        conn.execute("UPDATE branches SET rule_version=?, version=version+1 WHERE id=?",
                     (rule_version, branch_id))
        return _branch_row(get_branch(conn, branch_id))


def create_branch(name: str, rule_version: int | None,
                  fork_from_event_id: str | None, note: str) -> dict[str, Any]:
    with write_tx() as conn:
        if fork_from_event_id is not None:
            src = conn.execute(
                "SELECT * FROM events WHERE id=?", (fork_from_event_id,)).fetchone()
            if src is None:
                raise NotFoundError(f"分叉起点事件不存在: {fork_from_event_id}")
            src_branch_id = src["branch_id"]
            src_branch = get_branch(conn, src_branch_id)
            rv = rule_version or src_branch["rule_version"]
            ordered = _replay_ordered_events(conn, src_branch_id,
                                             include_withdrawn=True)
            cut_index = next(i for i, e in enumerate(ordered)
                             if e["id"] == fork_from_event_id)
            chosen = ordered[:cut_index + 1]
        else:
            src_branch_id = None
            rv = rule_version
            chosen = []
            if rv is None:
                rv = conn.execute(
                    "SELECT COALESCE(MAX(version),1) FROM rule_versions").fetchone()[0]
        if conn.execute("SELECT 1 FROM rule_versions WHERE version=?", (rv,)).fetchone() is None:
            raise BadRequestError(f"规则版本不存在: {rv}")

        new_id = _new_branch_id(conn)
        conn.execute(
            "INSERT INTO branches(id, name, version, rule_version, note, created_at)"
            " VALUES (?, ?, 1, ?, ?, ?)",
            (new_id, name, rv, note, _now()))
        for e in chosen:
            new_eid = _new_event_id(conn)
            conn.execute(
                "INSERT INTO events(id, branch_id, root_id, lineage_root_id, code,"
                " etype, valid_at, recorded_at, seq, payload, withdrawn,"
                " withdraw_reason, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_eid, new_id, new_eid, e["lineage_root_id"], e["code"],
                 e["etype"], e["valid_at"], e["recorded_at"], e["seq"], e["payload"],
                 e["withdrawn"], e["withdraw_reason"], e["created_at"]))
        return _branch_row(get_branch(conn, new_id))


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------

def list_events(branch_id: str) -> list[dict[str, Any]]:
    with read_conn() as conn:
        get_branch(conn, branch_id)
        rows = conn.execute(
            "SELECT * FROM events WHERE branch_id=? ORDER BY valid_at, seq, code, id",
            (branch_id,)).fetchall()
        return [_event_row(r) for r in rows]


def _check_branch_version(conn, branch_id: str, expected: int) -> sqlite3.Row:
    b = get_branch(conn, branch_id)
    if b["version"] != expected:
        raise ConflictError(
            f"分支版本基线冲突：期望 {expected}，当前 {b['version']}（可能已有其他人提交，请刷新后重试）")
    return b


def _parse_iso(value: str, field: str) -> str:
    """Validate ISO8601; returns the canonical UTC 'Z' string or raises 400."""
    try:
        return parse_dt(value).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError) as exc:
        raise BadRequestError(f"{field} 不是合法的 ISO8601 时间: {value!r}") from exc


def add_events(branch_id: str, events: list[dict[str, Any]],
               expected_version: int) -> dict[str, Any]:
    # 先做全量时间格式校验，保证事务内不再失败
    for ev in events:
        _parse_iso(ev["valid_at"], "valid_at")
        if ev.get("recorded_at"):
            _parse_iso(ev["recorded_at"], "recorded_at")
    with write_tx() as conn:
        _check_branch_version(conn, branch_id, expected_version)
        created = []
        for ev in events:
            eid = _new_event_id(conn)
            recorded = ev.get("recorded_at") or ev["valid_at"]
            conn.execute(
                "INSERT INTO events(id, branch_id, root_id, lineage_root_id, code,"
                " etype, valid_at, recorded_at, seq, payload, withdrawn, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?, 0, ?)",
                (eid, branch_id, eid, eid, ev.get("code", ""), ev["etype"],
                 ev["valid_at"], recorded, ev.get("seq", 0),
                 json.dumps(ev.get("payload") or {}, ensure_ascii=False), _now()))
            created.append(eid)
        conn.execute("UPDATE branches SET version=version+1 WHERE id=?", (branch_id,))
        b = get_branch(conn, branch_id)
        return {"branch": _branch_row(b), "created_event_ids": created}


def patch_event(event_id: str, changes: dict[str, Any],
                expected_version: int) -> dict[str, Any]:
    if changes.get("valid_at"):
        _parse_iso(changes["valid_at"], "valid_at")
    if changes.get("recorded_at"):
        _parse_iso(changes["recorded_at"], "recorded_at")
    with write_tx() as conn:
        r = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if r is None:
            raise NotFoundError(f"事件不存在: {event_id}")
        _check_branch_version(conn, r["branch_id"], expected_version)
        if r["withdrawn"]:
            raise BadRequestError("事件已撤回，不能修改；请在分叉中重建")
        new = dict(_event_row(r))
        for f in ("etype", "valid_at", "seq", "payload"):
            if changes.get(f) is not None:
                new[f] = changes[f]
        if changes.get("recorded_at") is not None:
            new["recorded_at"] = changes["recorded_at"]
        # root/lineage 保持不变，确保依据链与分叉对齐身份稳定
        conn.execute(
            "UPDATE events SET etype=?, valid_at=?, recorded_at=?, seq=?, payload=?"
            " WHERE id=?",
            (new["etype"], new["valid_at"], new["recorded_at"], new["seq"],
             json.dumps(new["payload"], ensure_ascii=False), event_id))
        conn.execute("UPDATE branches SET version=version+1 WHERE id=?",
                     (r["branch_id"],))
        return {"branch": _branch_row(get_branch(conn, r["branch_id"])),
                "event": _event_row(conn.execute(
                    "SELECT * FROM events WHERE id=?", (event_id,)).fetchone())}


def withdraw_event(event_id: str, expected_version: int,
                   reason: str = "") -> dict[str, Any]:
    with write_tx() as conn:
        r = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if r is None:
            raise NotFoundError(f"事件不存在: {event_id}")
        _check_branch_version(conn, r["branch_id"], expected_version)
        if r["withdrawn"]:
            raise BadRequestError("事件已经处于撤回状态")
        conn.execute(
            "UPDATE events SET withdrawn=1, withdraw_reason=? WHERE id=?",
            (reason or "撤回", event_id))
        conn.execute("UPDATE branches SET version=version+1 WHERE id=?",
                     (r["branch_id"],))
        return {"branch": _branch_row(get_branch(conn, r["branch_id"]))}


def branch_state_inputs(conn, branch_id: str):
    b = get_branch(conn, branch_id)
    rules = [_rule_row(r) for r in conn.execute(
        "SELECT * FROM rules WHERE version=? ORDER BY rid", (b["rule_version"],))]
    events = [_event_row(r) for r in _replay_ordered_events(conn, branch_id)]
    return _branch_row(b), rules, events


# ---------------------------------------------------------------------------
# ids / time
# ---------------------------------------------------------------------------

def _next_seq_id(conn, prefix: str) -> str:
    """Monotonic, collision-free id even after withdrawals/deletes."""
    row = conn.execute(
        "SELECT id FROM (SELECT id FROM events UNION ALL SELECT id FROM branches)"
        " WHERE id LIKE ? ORDER BY id DESC LIMIT 1", (prefix + "-%",)).fetchone()
    if row is None:
        n = 1
    else:
        n = int(row["id"].split("-", 1)[1]) + 1
    return f"{prefix}-{n:04d}"


def _new_event_id(conn) -> str:
    return _next_seq_id(conn, "EV")


def _new_branch_id(conn) -> str:
    return _next_seq_id(conn, "BR")


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# seed demo data
# ---------------------------------------------------------------------------

DEMO_RULES = [
    {"rid": "R-PAY", "rtype": "trigger",
     "params": {"event_type": "合同签订", "obligation": "PAY", "deadline_days": 30},
     "description": "合同签订后 30 日内付款"},
    {"rid": "R-DELIVER", "rtype": "trigger",
     "params": {"event_type": "合同签订", "obligation": "DELIVER", "deadline_days": 15},
     "description": "合同签订后 15 日内交货"},
    {"rid": "R-ONSITE", "rtype": "trigger",
     "params": {"event_type": "到货签收", "obligation": "ONSITE", "deadline_days": 7},
     "description": "到货签收后 7 日内完成现场安装"},
    {"rid": "R-PAUSE-DELIVER", "rtype": "suspend",
     "params": {"pause_event_type": "交货暂停通知",
                "resume_event_type": "交货恢复通知", "obligation": "DELIVER"},
     "description": "交货义务的暂停与恢复，暂停期间期限顺延"},
    {"rid": "R-NOTE-INSTEAD", "rtype": "substitute",
     "params": {"event_type": "付款方式变更", "old_obligation": "PAY",
                "new_obligation": "PAY_NOTE", "new_deadline_days": 10},
     "description": "付款义务被签发票据义务替代，替代后 10 日内完成"},
    {"rid": "R-TAX", "rtype": "trigger",
     "params": {"event_type": "票据开具", "obligation": "TAX", "deadline_days": 10},
     "description": "票据开具后 10 日内完成税务申报"},
    {"rid": "R-AUDIT-DEP", "rtype": "dependency",
     "params": {"obligation": "AUDIT", "depends_on": ["LICENSE"]},
     "description": "审计义务依赖许可费义务先履行"},
    {"rid": "R-AUDIT", "rtype": "trigger",
     "params": {"event_type": "审计启动", "obligation": "AUDIT", "deadline_days": 20},
     "description": "审计启动后 20 日内出具报告（依赖许可费先履行）"},
]

DEMO_EVENTS = [
    # code, etype, valid_at, seq, payload
    ("C001", "合同签订", "2026-01-10T09:00:00Z", 0, {}),
    ("E002", "交货暂停通知", "2026-01-15T10:00:00Z", 0, {"obligation": "DELIVER"}),
    ("E003", "交货恢复通知", "2026-01-20T10:00:00Z", 0, {"obligation": "DELIVER"}),
    ("E004", "到货签收", "2026-01-28T14:00:00Z", 0, {}),
    ("E005", "履行", "2026-01-29T11:00:00Z", 0, {"obligation": "DELIVER"}),
    ("E006", "付款方式变更", "2026-03-01T09:30:00Z", 0, {}),
    ("E007", "票据开具", "2026-03-01T09:30:00Z", 1, {}),
    ("E008", "履行", "2026-03-05T15:00:00Z", 0, {"obligation": "PAY_NOTE"}),
    ("E009", "履行", "2026-03-03T15:00:00Z", 0, {"obligation": "ONSITE"}),
    ("E010", "审计启动", "2026-02-10T09:00:00Z", 0, {}),
    # 截止时点事件：不命中任何规则，仅把默认推演窗口推到 03-12，
    # 使 TAX（期限 03-11）在默认视图中呈现逾期；录入时间晚于有效时间，演示双时间维度
    ("Z999", "对账截止", "2026-03-12T00:00:00Z", 0, {}),
]


def _seed(conn) -> None:
    conn.execute(
        "INSERT INTO rule_versions(version, note, created_at) VALUES (1, ?, ?)",
        ("初始基线规则（演示）", SEED_AT))
    for r in validate_rules(DEMO_RULES):
        conn.execute(
            "INSERT INTO rules(version, rid, rtype, params, description)"
            " VALUES (1, ?, ?, ?, ?)",
            (r["rid"], r["rtype"],
             json.dumps(r["params"], ensure_ascii=False), r["description"]))
    conn.execute(
        "INSERT INTO branches(id, name, version, rule_version, note, created_at)"
        " VALUES (?, ?, 1, 1, ?, ?)",
        (MAIN_BRANCH, "主干", "初始演示分支", SEED_AT))
    for i, (code, etype, valid_at, seq, payload) in enumerate(DEMO_EVENTS, start=1):
        eid = f"EV-{i:04d}"
        recorded = valid_at
        if etype == "对账截止":
            # 演示录入时间可晚于有效时间
            recorded = "2026-03-13T08:30:00Z"
        conn.execute(
            "INSERT INTO events(id, branch_id, root_id, lineage_root_id, code,"
            " etype, valid_at, recorded_at, seq, payload, withdrawn, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,? ,0,?)",
            (eid, MAIN_BRANCH, eid, eid, code, etype, valid_at, recorded, seq,
             json.dumps(payload, ensure_ascii=False), SEED_AT))
