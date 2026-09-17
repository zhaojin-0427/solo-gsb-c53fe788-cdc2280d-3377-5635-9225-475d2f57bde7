"""Standalone smoke test using only stdlib (engine + storage logic)."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

from app import storage
from app.engine import compare_states, simulate
from app.rules import validate_rules
from app.timeutil import parse_dt

tmp = tempfile.mkdtemp()
storage.init_db(os.path.join(tmp, "test.db"))

with storage.read_conn() as conn:
    branch, rules, events = storage.branch_state_inputs(conn, "main")

as_of = parse_dt("2026-03-12T00:00:00Z")
res = simulate(rules, events, as_of)
print("=== summary:", res["summary"])
for o in res["obligations"]:
    print(f"{o['obligation']:10s} {o['status']:4s} due={o['effective_deadline']} "
          f"performed={o['performed_at']} on_time={o['on_time']} future={o['future']}")
    print("   reason:", o["reason"])
print("notices:", res["notices"])

# expected:
# PAY 被替代; DELIVER 已履行(on time); ONSITE 已履行; PAY_NOTE 已履行;
# TAX 逾期; AUDIT 待触发(blocked)
status = {o["obligation"]: o["status"] for o in res["obligations"]}
assert status["PAY"] == "被替代", status
assert status["DELIVER"] == "已履行", status
assert status["ONSITE"] == "已履行", status
assert status["PAY_NOTE"] == "已履行", status
assert status["TAX"] == "逾期", status
assert status["AUDIT"] == "待触发", status

d = next(o for o in res["obligations"] if o["obligation"] == "DELIVER")
# due_base 2026-01-25, paused 01-15..01-20 (5 days) -> effective 01-30; performed 01-29 on time
assert d["deadline_base"] == "2026-01-25T09:00:00Z", d["deadline_base"]
assert d["effective_deadline"] == "2026-01-30T09:00:00Z", d["effective_deadline"]
assert d["on_time"] is True

tax = next(o for o in res["obligations"] if o["obligation"] == "TAX")
assert tax["effective_deadline"] == "2026-03-11T09:30:00Z", tax["effective_deadline"]
assert tax["status"] == "逾期"

# evidence chains present
assert len(d["evidence"]) >= 4, [e["kind"] for e in d["evidence"]]
kinds = [e["kind"] for e in d["evidence"]]
assert "暂停" in kinds and "恢复" in kinds and "履行" in kinds, kinds

# ---- withdraw 票据开具 (EV-0007): TAX 消失；再撤回付款方式变更 (EV-0006): PAY 转逾期 ----
storage.withdraw_event("EV-0007", 1, "撤回票据开具")
storage.withdraw_event("EV-0006", 2, "test withdraw")
with storage.read_conn() as conn:
    branch2, rules2, events2 = storage.branch_state_inputs(conn, "main")
res2 = simulate(rules2, events2, as_of)
st2 = {o["obligation"]: o["status"] for o in res2["obligations"]}
print("after withdraw:", st2)
assert "PAY_NOTE" not in st2
assert "TAX" not in st2
assert st2["PAY"] == "逾期", st2

diff = compare_states(res, res2)
changed_ob = {c["obligation"] for c in diff["changes"]}
print("diff changes:", [(c["obligation"], c["change"]) for c in diff["changes"]])
assert "PAY" in changed_ob and "PAY_NOTE" in changed_ob and "TAX" in changed_ob

# ---- CAS conflict: stale baseline ----
try:
    storage.add_events("main", [{"etype": "x", "valid_at": "2026-03-07T00:00:00Z"}], 1)
    raise AssertionError("expected ConflictError")
except storage.ConflictError as e:
    print("CAS ok:", e)

# ---- fork from an event, modify, compare ----
b = storage.create_branch("fork1", None, "EV-0001", "分叉测试")
# in fork: add a performance for PAY at day 10 -> PAY 已履行
storage.add_events(b["id"], [
    {"etype": "履行", "valid_at": "2026-01-20T00:00:00Z",
     "seq": 0, "payload": {"obligation": "PAY"}}], 1)
with storage.read_conn() as conn:
    fb, fr, fe = storage.branch_state_inputs(conn, b["id"])
resf = simulate(fr, fe, as_of)
stf = {o["obligation"]: o["status"] for o in resf["obligations"]}
print("fork:", stf)
assert stf.get("PAY") == "已履行", stf

# ---- determinism across "restart": rebuild engine inputs and compare JSON ----
with storage.read_conn() as conn:
    _b, _r, _e = storage.branch_state_inputs(conn, "main")
res3 = simulate(_r, _e, as_of)
assert json.dumps(res2, sort_keys=True, ensure_ascii=False) == json.dumps(
    res3, sort_keys=True, ensure_ascii=False)
print("deterministic restart: OK")

# ---- rule version CAS: fork switch to a new rule version ----
nv = storage.create_rule_version("test", storage.DEMO_RULES, 1)
assert nv["version"] == 2
try:
    storage.create_rule_version("stale", storage.DEMO_RULES, 1)
    raise AssertionError("expected conflict")
except storage.ConflictError:
    print("rule CAS ok")

print("\nALL SMOKE TESTS PASSED")
