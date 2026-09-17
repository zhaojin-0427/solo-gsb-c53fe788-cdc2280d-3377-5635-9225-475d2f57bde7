"""FastAPI 入口：REST API + 静态前端。

并发控制：所有写操作在单个 SQLite 事务内先校验基线版本（CAS），
不一致则整体回滚返回 409，绝不部分写入。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, engine


# ---------- 请求模型 ----------

class EventIn(BaseModel):
    event_id: Optional[str] = None
    event_type: str = Field(min_length=1)
    scope: str = "default"
    valid_time: str
    payload: dict = Field(default_factory=dict)


class EventsBatch(BaseModel):
    base_version: int
    events: list[EventIn]


class RulesUpdate(BaseModel):
    base_version: int
    rules: list[dict]
    note: str = ""


class VersionOnly(BaseModel):
    base_version: int


class ScenarioCreate(BaseModel):
    name: str = Field(min_length=1)
    fork_seq: int = Field(ge=0, description="从该创建序号（含）之后分叉，0 表示从头开始")


class ScenarioRules(BaseModel):
    base_rev: int
    rules: list[dict]


class ScenarioEvents(BaseModel):
    base_rev: int
    events: list[EventIn]


class ScenarioRetraction(BaseModel):
    base_rev: int
    event_id: str


# ---------- 应用 ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # 启动即完成依赖外的数据库初始化
    yield


app = FastAPI(title="合同义务状态推演台", lifespan=lifespan)


@app.exception_handler(ValueError)
async def value_error_handler(request, exc):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc):
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


def conflict(msg: str):
    raise HTTPException(status_code=409, detail=msg)


def validate_event(ev: EventIn) -> None:
    try:
        engine.parse_time(ev.valid_time)
    except Exception:
        raise ValueError(f"事件 {ev.event_type} 的 valid_time 不是合法 ISO 8601 时间")


def insert_events(conn, batch: list[EventIn], scenario_id) -> list:
    ids = []
    for ev in batch:
        validate_event(ev)
        eid = ev.event_id or f"ev-{uuid.uuid4().hex[:12]}"
        try:
            conn.execute(
                """INSERT INTO events(event_id, scenario_id, seq, event_type, scope,
                                      valid_time, recorded_at, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    eid,
                    scenario_id,
                    db.next_seq(conn),
                    ev.event_type,
                    ev.scope or "default",
                    ev.valid_time,
                    db.now_iso(),
                    json.dumps(ev.payload or {}, ensure_ascii=False),
                ),
            )
        except sqlite3.IntegrityError:
            conflict(f"事件 ID 已存在: {eid}，本次批量导入已整体回滚")
        ids.append(eid)
    return ids


# ---------- 查询 ----------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/state")
def get_state(scenario: str = "main", as_of: Optional[str] = None):
    conn = db.connect()
    try:
        sid = None if scenario == "main" else scenario
        state = db.compute_state(conn, sid, as_of)
        if state is None:
            raise HTTPException(404, "分叉不存在")
        return state
    finally:
        conn.close()


@app.get("/api/events")
def list_events(scenario: str = "main"):
    conn = db.connect()
    try:
        if scenario == "main":
            return {
                "state_version": db.get_state_version(conn),
                "events": sorted(
                    db.mainline_events(conn),
                    key=lambda e: (engine.parse_time(e["valid_time"]), e["seq"]),
                ),
            }
        sc = db.get_scenario(conn, scenario)
        if not sc:
            raise HTTPException(404, "分叉不存在")
        return {"rev": sc["rev"], "events": db.scenario_events_view(conn, sc)}
    finally:
        conn.close()


@app.get("/api/rules")
def get_rules(scenario: str = "main"):
    conn = db.connect()
    try:
        if scenario == "main":
            version, rules = db.current_rules(conn)
            return {
                "state_version": db.get_state_version(conn),
                "rules_version": version,
                "rules": rules,
            }
        sc = db.get_scenario(conn, scenario)
        if not sc:
            raise HTTPException(404, "分叉不存在")
        return {
            "rev": sc["rev"],
            "base_rule_version": sc["base_rule_version"],
            "rules": json.loads(sc["rules"]),
        }
    finally:
        conn.close()


@app.get("/api/rules/versions")
def list_rule_versions():
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT version, note, created_at FROM rule_versions ORDER BY version DESC"
        ).fetchall()
        return {"versions": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/scenarios")
def get_scenarios():
    conn = db.connect()
    try:
        return {
            "state_version": db.get_state_version(conn),
            "scenarios": [
                {k: v for k, v in sc.items() if k != "rules"}
                for sc in db.list_scenarios(conn)
            ],
        }
    finally:
        conn.close()


@app.get("/api/compare")
def compare(a: str = "main", b: str = ""):
    conn = db.connect()
    try:
        sa = db.compute_state(conn, None if a == "main" else a)
        sb = db.compute_state(conn, None if b == "main" else b)
        if sa is None or sb is None:
            raise HTTPException(404, "比较对象不存在")
        map_a = {o["key"]: o for o in sa["obligations"]}
        map_b = {o["key"]: o for o in sb["obligations"]}
        rows = []
        for key in sorted(set(map_a) | set(map_b)):
            oa, ob = map_a.get(key), map_b.get(key)
            changed = (
                (oa is None) != (ob is None)
                or (oa and ob and (
                    oa["state"] != ob["state"]
                    or oa["deadline"] != ob["deadline"]
                    or oa["fulfilled_at"] != ob["fulfilled_at"]
                    or oa["overdue_at"] != ob["overdue_at"]
                    or oa["substituted_at"] != ob["substituted_at"]
                ))
            )
            rows.append({
                "key": key,
                "rule_name": (oa or ob)["rule_name"],
                "scope": (oa or ob)["scope"],
                "changed": changed,
                "a": oa,
                "b": ob,
            })
        return {
            "a_label": "主线" if a == "main" else a,
            "b_label": "主线" if b == "main" else b,
            "a_evaluation_time": sa["evaluation_time"],
            "b_evaluation_time": sb["evaluation_time"],
            "rows": rows,
        }
    finally:
        conn.close()


# ---------- 主线写操作（CAS：base_version 必须等于当前 state_version） ----------

@app.post("/api/rules")
def update_rules(body: RulesUpdate):
    engine.validate_rules(body.rules)
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if db.get_state_version(conn) != body.base_version:
            conflict("基线版本已过期，规则未写入，请刷新后重试")
        version, _ = db.current_rules(conn)
        conn.execute(
            "INSERT INTO rule_versions(version, rules, note, created_at) VALUES (?, ?, ?, ?)",
            (version + 1, json.dumps(body.rules, ensure_ascii=False), body.note, db.now_iso()),
        )
        new_version = db.bump_state_version(conn)
        conn.commit()
        return {"ok": True, "state_version": new_version, "rules_version": version + 1}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/events")
def add_events(body: EventsBatch):
    if not body.events:
        raise ValueError("事件列表不能为空")
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if db.get_state_version(conn) != body.base_version:
            conflict("基线版本已过期，事件未写入，请刷新后重试")
        ids = insert_events(conn, body.events, None)
        new_version = db.bump_state_version(conn)
        conn.commit()
        return {"ok": True, "state_version": new_version, "event_ids": ids}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _set_retracted(conn, event_id: str, retracted: bool):
    row = conn.execute(
        "SELECT event_id FROM events WHERE event_id = ? AND scenario_id IS NULL",
        (event_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "主线事件不存在")
    conn.execute(
        "UPDATE events SET retracted = ?, retracted_at = ? WHERE event_id = ?",
        (1 if retracted else 0, db.now_iso() if retracted else None, event_id),
    )


@app.post("/api/events/{event_id}/retract")
def retract_event(event_id: str, body: VersionOnly):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if db.get_state_version(conn) != body.base_version:
            conflict("基线版本已过期，撤回未生效，请刷新后重试")
        _set_retracted(conn, event_id, True)
        new_version = db.bump_state_version(conn)
        conn.commit()
        return {"ok": True, "state_version": new_version}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/events/{event_id}/restore")
def restore_event(event_id: str, body: VersionOnly):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if db.get_state_version(conn) != body.base_version:
            conflict("基线版本已过期，恢复未生效，请刷新后重试")
        _set_retracted(conn, event_id, False)
        new_version = db.bump_state_version(conn)
        conn.commit()
        return {"ok": True, "state_version": new_version}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- 分叉 ----------

@app.post("/api/scenarios")
def create_scenario(body: ScenarioCreate):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        version, rules = db.current_rules(conn)
        sid = f"sc-{uuid.uuid4().hex[:8]}"
        conn.execute(
            """INSERT INTO scenarios(id, name, fork_seq, base_rule_version, rules, rev, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (sid, body.name, body.fork_seq, version,
             json.dumps(rules, ensure_ascii=False), db.now_iso()),
        )
        conn.commit()
        return {"ok": True, "id": sid}
    finally:
        conn.close()


@app.delete("/api/scenarios/{sid}")
def delete_scenario(sid: str):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM scenario_retractions WHERE scenario_id = ?", (sid,))
        conn.execute("DELETE FROM events WHERE scenario_id = ?", (sid,))
        cur = conn.execute("DELETE FROM scenarios WHERE id = ?", (sid,))
        if cur.rowcount == 0:
            raise HTTPException(404, "分叉不存在")
        conn.commit()
        return {"ok": True}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _get_scenario_or_404(conn, sid):
    sc = db.get_scenario(conn, sid)
    if not sc:
        raise HTTPException(404, "分叉不存在")
    return sc


def _check_rev(conn, sid, base_rev):
    sc = _get_scenario_or_404(conn, sid)
    if sc["rev"] != base_rev:
        conflict("分叉基线版本已过期，修改未写入，请刷新后重试")
    return sc


@app.put("/api/scenarios/{sid}/rules")
def update_scenario_rules(sid: str, body: ScenarioRules):
    engine.validate_rules(body.rules)
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _check_rev(conn, sid, body.base_rev)
        conn.execute(
            "UPDATE scenarios SET rules = ?, rev = rev + 1 WHERE id = ?",
            (json.dumps(body.rules, ensure_ascii=False), sid),
        )
        conn.commit()
        return {"ok": True, "rev": body.base_rev + 1}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/scenarios/{sid}/events")
def add_scenario_events(sid: str, body: ScenarioEvents):
    if not body.events:
        raise ValueError("事件列表不能为空")
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _check_rev(conn, sid, body.base_rev)
        ids = insert_events(conn, body.events, sid)
        conn.execute("UPDATE scenarios SET rev = rev + 1 WHERE id = ?", (sid,))
        conn.commit()
        return {"ok": True, "rev": body.base_rev + 1, "event_ids": ids}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/scenarios/{sid}/retract")
def scenario_retract(sid: str, body: ScenarioRetraction):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        sc = _check_rev(conn, sid, body.base_rev)
        own = conn.execute(
            "SELECT event_id FROM events WHERE event_id = ? AND scenario_id = ?",
            (body.event_id, sid),
        ).fetchone()
        if own:
            conn.execute(
                "UPDATE events SET retracted = 1, retracted_at = ? WHERE event_id = ?",
                (db.now_iso(), body.event_id),
            )
        else:
            inherited = conn.execute(
                """SELECT event_id FROM events
                   WHERE event_id = ? AND scenario_id IS NULL AND seq <= ?""",
                (body.event_id, sc["fork_seq"]),
            ).fetchone()
            if not inherited:
                raise HTTPException(404, "该事件不在此分叉的可见范围内")
            conn.execute(
                "INSERT OR IGNORE INTO scenario_retractions(scenario_id, event_id, created_at)"
                " VALUES (?, ?, ?)",
                (sid, body.event_id, db.now_iso()),
            )
        conn.execute("UPDATE scenarios SET rev = rev + 1 WHERE id = ?", (sid,))
        conn.commit()
        return {"ok": True, "rev": body.base_rev + 1}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/scenarios/{sid}/restore")
def scenario_restore(sid: str, body: ScenarioRetraction):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _check_rev(conn, sid, body.base_rev)
        conn.execute(
            "UPDATE events SET retracted = 0, retracted_at = NULL"
            " WHERE event_id = ? AND scenario_id = ?",
            (body.event_id, sid),
        )
        conn.execute(
            "DELETE FROM scenario_retractions WHERE scenario_id = ? AND event_id = ?",
            (sid, body.event_id),
        )
        conn.execute("UPDATE scenarios SET rev = rev + 1 WHERE id = ?", (sid,))
        conn.commit()
        return {"ok": True, "rev": body.base_rev + 1}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- 静态前端 ----------

import os

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
