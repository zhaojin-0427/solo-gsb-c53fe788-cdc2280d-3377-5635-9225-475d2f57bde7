"""FastAPI application: rules / branches / events / evaluation / compare."""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import storage
from .engine import compare_states, simulate
from .models import (
    BranchCreateIn,
    CompareIn,
    EventBatchIn,
    EventPatchIn,
    EventWithdrawIn,
    RuleVersionCreate,
    RuleVersionUpdate,
)
from .rules import RuleError
from .timeutil import parse_dt

DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "obligations.db"))
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
storage.init_db(DB_PATH)

app = FastAPI(title="合同义务状态推演台", version="1.0.0")


@app.exception_handler(storage.ConflictError)
async def _conflict(_, exc):
    return JSONResponse(status_code=409, content={"error": "conflict", "detail": str(exc)})


@app.exception_handler(storage.NotFoundError)
async def _not_found(_, exc):
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


@app.exception_handler(storage.BadRequestError)
async def _bad_request(_, exc):
    return JSONResponse(status_code=400, content={"error": "bad_request", "detail": str(exc)})


@app.exception_handler(RuleError)
async def _rule_error(_, exc):
    return JSONResponse(status_code=400, content={"error": "bad_rule", "detail": str(exc)})


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------- rule versions ----------------

@app.get("/api/rule-versions")
def get_rule_versions():
    return storage.list_rule_versions()


@app.post("/api/rule-versions", status_code=201)
def post_rule_version(body: RuleVersionCreate):
    # RuleError 由全局异常处理器映射为 400；ConflictError 映射为 409；均无写入
    return storage.create_rule_version(
        body.note, [r.model_dump() for r in body.rules], body.expected_version)


@app.put("/api/branches/{branch_id}/rule-version")
def put_branch_rule_version(branch_id: str, body: RuleVersionUpdate):
    if body.branch_id != branch_id:
        raise HTTPException(400, "body.branch_id 与路径不一致")
    return storage.switch_branch_rules(branch_id, body.rule_version,
                                       body.expected_branch_version)


# ---------------- branches ----------------

@app.get("/api/branches")
def get_branches():
    return storage.list_branches()


@app.post("/api/branches", status_code=201)
def post_branch(body: BranchCreateIn):
    return storage.create_branch(body.name, body.rule_version,
                                 body.fork_from_event_id, body.note)


# ---------------- events ----------------

@app.get("/api/branches/{branch_id}/events")
def get_events(branch_id: str):
    return storage.list_events(branch_id)


@app.post("/api/branches/{branch_id}/events", status_code=201)
def post_events(branch_id: str, body: EventBatchIn):
    return storage.add_events(
        branch_id, [e.model_dump() for e in body.events], body.expected_version)


@app.patch("/api/events/{event_id}")
def patch_event(event_id: str, body: EventPatchIn):
    changes = body.model_dump(exclude_none=True, exclude={"expected_version"})
    return storage.patch_event(event_id, changes, body.expected_version)


@app.post("/api/events/{event_id}/withdraw", status_code=200)
def withdraw_event(event_id: str, body: EventWithdrawIn):
    return storage.withdraw_event(event_id, body.expected_version)


# ---------------- evaluation ----------------

def _default_as_of(raw_events):
    # 默认推演时点 = 非撤回事件中最大的有效时间（确定且可复现）
    active = [e for e in raw_events if not e["withdrawn"]]
    if not active:
        return parse_dt("1970-01-01T00:00:00Z")
    return max(parse_dt(e["valid_at"]) for e in active)


def _parse_as_of(as_of: str | None):
    if as_of is None:
        return None
    try:
        return parse_dt(as_of)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"as_of 不是合法的 ISO8601 时间: {as_of!r}") from exc


@app.get("/api/branches/{branch_id}/state")
def get_state(branch_id: str, as_of: str | None = None):
    with storage.read_conn() as conn:
        branch, rules, events = storage.branch_state_inputs(conn, branch_id)
    horizon = _parse_as_of(as_of) or _default_as_of(events)
    result = simulate(rules, events, horizon)
    result["branch"] = branch
    return result


@app.post("/api/compare")
def post_compare(body: CompareIn):
    with storage.read_conn() as conn:
        base_branch, base_rules, base_events = storage.branch_state_inputs(
            conn, body.base_branch_id)
        target_branch, target_rules, target_events = storage.branch_state_inputs(
            conn, body.target_branch_id)
    if body.as_of:
        horizon = _parse_as_of(body.as_of)
        hb = ht = horizon
    else:
        # 对齐到两者共同的最远事件时间，避免纯粹因为时间窗不同产生伪差异
        def _horizon(evs):
            active = [e for e in evs if not e["withdrawn"]]
            return max((parse_dt(e["valid_at"]) for e in active),
                       default=parse_dt("1970-01-01T00:00:00Z"))
        hb, ht = _horizon(base_events), _horizon(target_events)
        horizon = max(hb, ht)
        hb = ht = horizon
    base_result = simulate(base_rules, base_events, hb)
    target_result = simulate(target_rules, target_events, ht)
    diff = compare_states(base_result, target_result)
    diff["base_branch"] = base_branch
    diff["target_branch"] = target_branch
    diff["base_obligations"] = base_result["obligations"]
    diff["target_obligations"] = target_result["obligations"]
    return diff


# ---------------- static frontend (mounted last) ----------------

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
