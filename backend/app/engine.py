"""Deterministic replay engine.

回放顺序：有效时间升序 -> 同刻 seq 升序 -> code -> eid。
每个事件内部阶段顺序：trigger -> suspend -> resume -> perform -> substitute。
依赖义务在被依赖义务履行后于同一时刻转为“履行中”。

时间轴上的结论完全由 (规则集, 事件集合, as_of) 决定，刷新与重启结果一致。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .constants import (
    ALL_STATUSES,
    STATUS_ACTIVE,
    STATUS_FULFILLED,
    STATUS_OVERDUE,
    STATUS_PENDING,
    STATUS_SUBSTITUTED,
)
from .rules import _deadline_of, suspend_directives
from .timeutil import add_days, fmt_dt, parse_dt


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _event_obligation(ev: dict[str, Any]) -> str | None:
    p = ev.get("payload") or {}
    ob = p.get("obligation")
    return ob if isinstance(ob, str) and ob else None


def _obligation_target(rule_params: dict[str, Any], ev: dict[str, Any]) -> str | None:
    if rule_params.get("obligation"):
        return rule_params["obligation"]
    if rule_params.get("obligation_from_payload"):
        return _event_obligation(ev)
    return None


def _index_rules(rules: list[dict[str, Any]]):
    triggers: dict[str, list[dict[str, Any]]] = {}
    # suspend directives: event_type -> list[(rule, action)]
    suspensions: dict[str, list[tuple[dict[str, Any], str]]] = {}
    substitutes: dict[str, list[dict[str, Any]]] = {}
    deps: dict[str, list[dict[str, Any]]] = {}

    for r in sorted(rules, key=lambda x: x["rid"]):
        p = r["params"]
        if r["rtype"] == "trigger":
            triggers.setdefault(p["event_type"], []).append(r)
        elif r["rtype"] == "suspend":
            for et, action in suspend_directives(r):
                suspensions.setdefault(et, []).append((r, action))
        elif r["rtype"] == "substitute":
            substitutes.setdefault(p["event_type"], []).append(r)
        elif r["rtype"] == "dependency":
            deps.setdefault(p["obligation"], []).append(r)
    for lst in triggers.values():
        lst.sort(key=lambda r: r["rid"])
    for lst in substitutes.values():
        lst.sort(key=lambda r: r["rid"])
    for lst in deps.values():
        lst.sort(key=lambda r: r["rid"])
    for lst in suspensions.values():
        lst.sort(key=lambda ra: ra[0]["rid"])
    return triggers, suspensions, substitutes, deps


# ---------------------------------------------------------------------------
# instance construction
# ---------------------------------------------------------------------------

def _make_instance(
    obligation: str,
    trigger_rule: dict[str, Any],
    ev: dict[str, Any],
    deps_index: dict[str, list[dict[str, Any]]],
    is_replacement: bool,
    substitute_rule: dict[str, Any] | None,
) -> dict[str, Any]:
    params = substitute_rule["params"] if is_replacement and substitute_rule else trigger_rule["params"]
    t = ev["valid_at_dt"]

    if is_replacement:
        kind, value = None, None
        if params.get("new_deadline_at"):
            kind, value = "fixed", parse_dt(params["new_deadline_at"])
        elif params.get("new_deadline_days") is not None:
            kind, value = "days", params["new_deadline_days"]
        else:
            kind, value = None, None
        dep_param = params.get("new_depends_on", "__unset__")
        if dep_param == "__unset__":
            dep_rules = deps_index.get(obligation, [])
        else:
            dep_rules = []
    else:
        kind, value = _deadline_of(trigger_rule["rid"], params)
        dep_rules = deps_index.get(obligation, [])

    due_base: datetime | None = None
    deadline_desc = "无固定期限"
    if kind == "fixed":
        due_base = value
        deadline_desc = f"固定期限 {fmt_dt(value)}"
    elif kind == "days":
        due_base = add_days(t, value)
        deadline_desc = f"触发后 {value} 日（{fmt_dt(due_base)}）"

    depends_on: list[str] = []
    if is_replacement and dep_param != "__unset__":
        depends_on = list(dep_param or [])
    elif dep_rules:
        depends_on = list(dep_rules[0]["params"]["depends_on"])

    inst = {
        "obligation": obligation,
        "status": STATUS_PENDING,
        "trigger_event_id": ev["eid"],
        "trigger_event_type": ev["etype"],
        "trigger_rule_id": trigger_rule["rid"] if not is_replacement else substitute_rule["rid"],
        "trigger_rule_kind": "trigger" if not is_replacement else "substitute",
        "triggered_at": t,
        "root_event_id": ev["root_event_id"],
        "lineage_root_event_id": ev["lineage_root_event_id"],
        "due_base": due_base,
        "deadline_desc": deadline_desc,
        "depends_on": depends_on,
        "blocked": bool(depends_on),
        "intervals": [],          # [[start, end|None]] UTC naive
        "performed_at": None,
        "perform_event_id": None,
        "on_time": None,
        "substituted_by_event_id": None,
        "substituted_by_rule_id": None,
        "replaced_obligation": None,
        "links": [],
        "notices": [],
        "seq": 0,
    }
    verb = "替代规则" if is_replacement else "触发规则"
    inst["links"].append({
        "kind": "触发依据" if not is_replacement else "替代依据",
        "rule_id": inst["trigger_rule_id"],
        "event_id": ev["eid"],
        "at": fmt_dt(t),
        "detail": f"{verb} {inst['trigger_rule_id']} 命中事件 {ev['etype']}，产生义务 {obligation}（{deadline_desc}）",
    })
    if depends_on:
        inst["links"].append({
            "kind": "依赖",
            "rule_id": dep_rules[0]["rid"] if dep_rules else (substitute_rule["rid"] if is_replacement else None),
            "event_id": None,
            "at": fmt_dt(t),
            "detail": f"义务 {obligation} 依赖 {', '.join(depends_on)} 履行后方可进入履行中",
        })
    return inst


def _all_deps_fulfilled(depends_on: list[str], done: set[str]) -> bool:
    return all(d in done for d in depends_on)


def _elapsed_closed(intervals: list[list[datetime | None]], upto: datetime) -> float:
    total = 0.0
    for start, end in intervals:
        if end is None:
            continue
        s = start
        e = min(end, upto)
        if e > s:
            total += (e - s).total_seconds()
    return total


# ---------------------------------------------------------------------------
# event processing
# ---------------------------------------------------------------------------

def _process_trigger(ev, triggers, deps_index, instances, done, create_seq,
                     trigger_only: bool):
    for rule in triggers.get(ev["etype"], []):
        obligation = rule["params"]["obligation"]
        inst = _make_instance(obligation, rule, ev, deps_index, False, None)
        inst["seq"] = create_seq[0]
        create_seq[0] += 1
        if not inst["depends_on"] or _all_deps_fulfilled(inst["depends_on"], done):
            inst["blocked"] = False
            if not trigger_only:
                inst["status"] = STATUS_ACTIVE
        else:
            inst["blocked"] = True
            if not trigger_only:
                inst["links"].append({
                    "kind": "等待依赖", "rule_id": None, "event_id": None,
                    "at": fmt_dt(ev["valid_at_dt"]),
                    "detail": f"被依赖义务 {', '.join(inst['depends_on'])} 尚未履行，保持待触发",
                })
        instances.append(inst)
        if trigger_only:
            inst["links"].append({
                "kind": "未来事件", "rule_id": None, "event_id": ev["eid"],
                "at": fmt_dt(ev["valid_at_dt"]),
                "detail": "该事件发生在推演时点之后，仅预登记义务，不推进任何状态",
            })


def _live_instances(instances):
    return [i for i in instances
            if i["status"] not in (STATUS_FULFILLED, STATUS_SUBSTITUTED)]


def _process_suspend(ev, suspensions, instances, trigger_only=False):
    if trigger_only:
        return
    for rule, action in suspensions.get(ev["etype"], []):
        target = _obligation_target(rule["params"], ev)
        if not target:
            continue
        t = ev["valid_at_dt"]
        for inst in _live_instances(instances):
            if inst["obligation"] != target:
                continue
            if action == "suspend":
                if inst["intervals"] and inst["intervals"][-1][1] is None:
                    inst["notices"].append(
                        f"事件 {ev['eid']} 再次暂停 {target}，已处于暂停中，忽略重复暂停")
                    continue
                inst["intervals"].append([t, None])
                inst["links"].append({
                    "kind": "暂停", "rule_id": rule["rid"], "event_id": ev["eid"],
                    "at": fmt_dt(t),
                    "detail": f"暂停规则 {rule['rid']} 命中，义务 {target} 自 {fmt_dt(t)} 起暂停，期限顺延",
                })
            else:  # resume
                open_iv = None
                for iv in reversed(inst["intervals"]):
                    if iv[1] is None:
                        open_iv = iv
                        break
                if open_iv is None:
                    inst["notices"].append(
                        f"事件 {ev['eid']} 试图恢复 {target}，但无生效中的暂停，忽略")
                    continue
                open_iv[1] = t
                inst["links"].append({
                    "kind": "恢复", "rule_id": rule["rid"], "event_id": ev["eid"],
                    "at": fmt_dt(t),
                    "detail": f"暂停规则 {rule['rid']} 命中，义务 {target} 于 {fmt_dt(t)} 恢复，暂停时长计入顺延",
                })


def _candidate_for_perform(inst, code, t):
    if inst["status"] in (STATUS_FULFILLED, STATUS_SUBSTITUTED):
        return False
    if inst["triggered_at"] > t:
        return False
    if code is not None and inst["obligation"] != code:
        return False
    return True


def _close_open_intervals(inst, t):
    """履行或替代时关闭未闭合的暂停区间。"""
    for iv in inst["intervals"]:
        if iv[1] is None:
            iv[1] = t


def _process_perform(ev, instances, done, suspensions, substitutes, trigger_only=False):
    if trigger_only:
        return
    t = ev["valid_at_dt"]
    # 约定：只有显式携带 payload.obligation 的事件才可能是“履行事件”；
    # 但若该事件类型已被暂停/恢复或替代规则注册，则以专门规则为准，避免误履行
    if ev["etype"] in suspensions or ev["etype"] in substitutes:
        return
    code = _event_obligation(ev)
    if code is None:
        return
    candidates = [i for i in instances if _candidate_for_perform(i, code, t)]
    if not candidates:
        return
    # 同一义务存在多个实例（含重新触发）时，履行最早产生的一个
    candidates.sort(key=lambda i: (i["triggered_at"], i["seq"]))
    inst = candidates[0]
    _close_open_intervals(inst, t)
    # 履行时点的有效期限 = 基础期限 + 基础期限之前开始的暂停时长
    effective_deadline = _effective_deadline_at(inst, t)
    on_time = effective_deadline is None or t <= effective_deadline
    inst["status"] = STATUS_FULFILLED
    inst["performed_at"] = t
    inst["perform_event_id"] = ev["eid"]
    inst["on_time"] = on_time
    inst["links"].append({
        "kind": "履行",
        "rule_id": None,
        "event_id": ev["eid"],
        "at": fmt_dt(t),
        "detail": (f"履行事件 {ev['etype']} 命中义务 {inst['obligation']}"
                   + ("" if on_time else "（履行时点已晚于有效期限，记为逾期履行）")),
    })
    if not on_time:
        pause_secs = int((effective_deadline - inst["due_base"]).total_seconds())
        inst["links"].append({
            "kind": "逾期", "rule_id": None, "event_id": None,
            "at": fmt_dt(inst["due_base"]),
            "detail": f"基础期限 {fmt_dt(inst['due_base'])}，暂停顺延 {pause_secs} 秒，"
                      f"于 {fmt_dt(t)} 履行时已逾期",
        })
    done.add(inst["obligation"])
    # 解除依赖：同一时刻把被该义务阻塞的义务推进为履行中
    for other in instances:
        if other is inst or other["status"] != STATUS_PENDING or not other["blocked"]:
            continue
        if inst["obligation"] in other["depends_on"] and _all_deps_fulfilled(
            other["depends_on"], done
        ):
            other["blocked"] = False
            other["status"] = STATUS_ACTIVE
            other["links"].append({
                "kind": "依赖解除", "rule_id": None, "event_id": ev["eid"],
                "at": fmt_dt(t),
                "detail": f"被依赖义务 {', '.join(other['depends_on'])} 已全部履行，{other['obligation']} 进入履行中",
            })


def _pick_substitution_target(rule, ev, instances, t):
    old = rule["params"]["old_obligation"]
    same = [i for i in instances
            if i["obligation"] == old
            and i["status"] not in (STATUS_FULFILLED, STATUS_SUBSTITUTED)
            and i["triggered_at"] <= t]
    if not same:
        return None
    # 同一旧义务可能存在多个实例：替代最近产生的一个；其余保留
    same.sort(key=lambda i: (i["triggered_at"], i["seq"]))
    return same[-1]


def _process_substitute(ev, substitutes, deps_index, instances, done, create_seq,
                        trigger_only=False, rule_notices=None):
    if trigger_only:
        return
    t = ev["valid_at_dt"]
    for rule in substitutes.get(ev["etype"], []):
        p = rule["params"]
        old = p["old_obligation"]
        target = _pick_substitution_target(rule, ev, instances, t)
        if target is None:
            msg = f"替代规则 {rule['rid']} 命中事件 {ev['eid']}，但无可替代的 {old} 实例，忽略"
            if rule_notices is not None:
                rule_notices.append(msg)
            continue
        new_ob = p["new_obligation"]
        # 构造替代事件（继承谱系），再生成新义务实例
        pseudo = dict(ev)
        pseudo["etype"] = ev["etype"]
        new_inst = _make_instance(new_ob, {}, pseudo, deps_index, True, rule)
        new_inst["seq"] = create_seq[0]
        create_seq[0] += 1
        new_inst["replaced_obligation"] = old
        # 旧实例终态
        _close_open_intervals(target, t)
        target["status"] = STATUS_SUBSTITUTED
        target["substituted_by_event_id"] = ev["eid"]
        target["substituted_by_rule_id"] = rule["rid"]
        link_detail = (f"替代规则 {rule['rid']} 命中事件 {ev['etype']}，"
                       f"义务 {old} 被 {new_ob} 替代，不再履行")
        target["links"].append({
            "kind": "被替代", "rule_id": rule["rid"], "event_id": ev["eid"],
            "at": fmt_dt(t), "detail": link_detail,
        })
        new_inst["links"][0]["detail"] = (
            f"替代规则 {rule['rid']} 命中事件 {ev['etype']}，以义务 {new_ob} 替代 {old}"
            + (f"（{new_inst['deadline_desc']}）" if new_inst["deadline_desc"] != "无固定期限" else "")
        )
        # 依赖判断
        if not new_inst["depends_on"] or _all_deps_fulfilled(new_inst["depends_on"], done):
            new_inst["blocked"] = False
            new_inst["status"] = STATUS_ACTIVE
        else:
            new_inst["blocked"] = True
            new_inst["links"].append({
                "kind": "等待依赖", "rule_id": rule["rid"], "event_id": None,
                "at": fmt_dt(t),
                "detail": f"被依赖义务 {', '.join(new_inst['depends_on'])} 尚未履行，保持待触发",
            })
        instances.append(new_inst)


# ---------------------------------------------------------------------------
# finalization
# ---------------------------------------------------------------------------

def _effective_deadline_at(inst, at: datetime) -> datetime | None:
    """有效期限 = 基础期限 + 所有在基础期限之前开始的暂停时长（截至 at）。"""
    if inst["due_base"] is None:
        return None
    eligible = 0.0
    for start, end in inst["intervals"]:
        if start > inst["due_base"]:
            continue
        e = at if end is None else min(end, at)
        if e > start:
            eligible += (e - start).total_seconds()
    return inst["due_base"] + timedelta(seconds=eligible)


def _finalize(inst, as_of):
    if inst["status"] == STATUS_SUBSTITUTED:
        new_name = None
        for l in inst["links"]:
            if l.get("kind") == "被替代":
                # detail 形如“...义务 OLD 被 NEW 替代...”
                detail = l.get("detail", "")
                marker = f"义务 {inst['obligation']} 被 "
                if marker in detail:
                    tail = detail.split(marker, 1)[1]
                    new_name = tail.split(" 替代", 1)[0].strip() or None
        return (f"义务 {inst['obligation']} 已被义务 {new_name or '（新义务）'} 替代"
                f"（事件 {inst['substituted_by_event_id']}）")
    if inst["status"] == STATUS_FULFILLED:
        when = fmt_dt(inst["performed_at"])
        if inst["on_time"] is False:
            return f"义务 {inst['obligation']} 已于 {when} 履行，但晚于有效期限，判定为逾期"
        return f"义务 {inst['obligation']} 已于 {when} 按时履行"
    if inst["triggered_at"] > as_of:
        return f"义务 {inst['obligation']} 由未来事件预登记，推演时点尚未触发"
    if inst["blocked"]:
        # 依赖未满足期间只等待，不计算逾期
        return (f"义务 {inst['obligation']} 已触发，但依赖 "
                f"{', '.join(inst['depends_on'])} 尚未履行，待触发")
    # 履行中 / 逾期判定（依赖解除后若有效期限已过，同样判逾期）
    eff = _effective_deadline_at(inst, as_of)
    if eff is None:
        return f"义务 {inst['obligation']} 履行中，无固定期限"
    if as_of > eff:
        inst["status"] = STATUS_OVERDUE
        pause_secs = _elapsed_closed(inst["intervals"], as_of)
        open_pause = any(iv[1] is None for iv in inst["intervals"])
        suffix = "（当前仍处于暂停中，期限持续顺延）" if open_pause else ""
        dep_note = "（依赖解除时已超过有效期限）" if inst["depends_on"] else ""
        if not any(l.get("kind") == "逾期" for l in inst["links"]):
            inst["links"].append({
                "kind": "逾期", "rule_id": None, "event_id": None,
                "at": fmt_dt(eff),
                "detail": f"有效期限 {fmt_dt(eff)} 届满时义务未履行{dep_note}",
            })
        return (f"义务 {inst['obligation']} 已逾期：有效期限 {fmt_dt(eff)}"
                f"{f'，已暂停顺延 {int(pause_secs)} 秒' if pause_secs else ''}{suffix}")
    inst["status"] = STATUS_ACTIVE
    return f"义务 {inst['obligation']} 履行中，有效期限 {fmt_dt(eff)}"


def _instance_key(inst) -> str:
    """Cross-branch alignment key: obligation + lineage root + occurrence."""
    return f"{inst['obligation']}|{inst['lineage_root_event_id']}"


def _sort_links(links):
    def key(l):
        at = l.get("at")
        return (at is None, at or "", l.get("kind") or "", l.get("event_id") or "")
    return sorted(links, key=key)


def _view(inst, as_of, occurrence_index):
    eff_due = None
    if inst["status"] not in (STATUS_FULFILLED, STATUS_SUBSTITUTED):
        eff_due = _effective_deadline_at(inst, as_of)
    elif inst["due_base"] is not None:
        eff_due = _effective_deadline_at(inst, inst["performed_at"] or as_of)
    open_pause = any(iv[1] is None for iv in inst["intervals"])
    return {
        "key": _instance_key(inst),
        "obligation": inst["obligation"],
        "status": inst["status"],
        "occurrence": occurrence_index,
        "triggered_at": fmt_dt(inst["triggered_at"]),
        "trigger_event_id": inst["trigger_event_id"],
        "trigger_rule_id": inst["trigger_rule_id"],
        "lineage_root_event_id": inst["lineage_root_event_id"],
        "deadline_base": fmt_dt(inst["due_base"]),
        "effective_deadline": fmt_dt(eff_due),
        "depends_on": list(inst["depends_on"]),
        "performed_at": fmt_dt(inst["performed_at"]),
        "perform_event_id": inst["perform_event_id"],
        "on_time": inst["on_time"],
        "substituted_by_event_id": inst["substituted_by_event_id"],
        "substituted_by_rule_id": inst["substituted_by_rule_id"],
        "open_pause": open_pause,
        "pause_intervals": [
            {"start": fmt_dt(s), "end": fmt_dt(e)} for s, e in inst["intervals"]
        ],
        "future": inst["triggered_at"] > as_of,
        "evidence": _sort_links(inst["links"]),
        "notices": list(inst["notices"]),
    }


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def simulate(rules: list[dict[str, Any]], raw_events: list[dict[str, Any]],
             as_of: datetime) -> dict[str, Any]:
    """Replay non-withdrawn events up to as_of and return obligation views."""
    (triggers, suspensions, substitutes, deps_index) = _index_rules(rules)

    events = []
    for ev in raw_events:
        if ev.get("withdrawn"):
            continue
        e = dict(ev)
        e["valid_at_dt"] = parse_dt(ev["valid_at"])
        e["recorded_at_dt"] = parse_dt(ev["recorded_at"] or ev["valid_at"])
        events.append(e)
    events.sort(key=lambda e: (e["valid_at_dt"], e["seq"], e["code"], e["eid"]))

    instances: list[dict[str, Any]] = []
    done: set[str] = set()
    create_seq = [0]
    notices: list[str] = []

    for ev in events:
        future = ev["valid_at_dt"] > as_of
        if future:
            # 未来事件：仅由触发规则预登记待触发义务，其它阶段一律不执行
            _process_trigger(ev, triggers, deps_index, instances, done,
                             create_seq, trigger_only=True)
            continue
        _process_trigger(ev, triggers, deps_index, instances, done,
                         create_seq, trigger_only=False)
        _process_suspend(ev, suspensions, instances)
        _process_perform(ev, instances, done, suspensions, substitutes)
        _process_substitute(ev, substitutes, deps_index, instances, done,
                            create_seq, rule_notices=notices)

    reasons = {}
    for inst in instances:
        reasons[id(inst)] = _finalize(inst, as_of)

    # occurrence index within an alignment key (按产生顺序稳定编号)
    key_occ: dict[str, int] = {}
    views = []
    for inst in sorted(instances, key=lambda i: (i["triggered_at"], i["seq"])):
        k = _instance_key(inst)
        key_occ[k] = key_occ.get(k, 0) + 1
        v = _view(inst, as_of, key_occ[k])
        v["reason"] = reasons[id(inst)]
        views.append(v)

    summary = {s: 0 for s in ALL_STATUSES}
    for v in views:
        summary[v["status"]] += 1

    return {
        "as_of": fmt_dt(as_of),
        "events_replayed": sum(1 for e in events if e["valid_at_dt"] <= as_of),
        "events_future": sum(1 for e in events if e["valid_at_dt"] > as_of),
        "summary": summary,
        "obligations": views,
        "notices": notices,
    }


def compare_states(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Diff two simulation results, aligning obligations by lineage key+occurrence."""
    def index(result):
        return {v["key"]: v for v in result["obligations"]}

    bm, tm = index(base), index(target)
    keys = sorted(set(bm) | set(tm))
    changes = []
    for k in keys:
        b, t = bm.get(k), tm.get(k)
        if b is None:
            changes.append({"key": k, "obligation": t["obligation"],
                            "change": "新增", "base": None, "target": _diff_fields(t)})
            continue
        if t is None:
            changes.append({"key": k, "obligation": b["obligation"],
                            "change": "消失", "base": _diff_fields(b), "target": None})
            continue
        fields = {}
        for f in ("status", "effective_deadline", "performed_at", "on_time",
                  "triggered_at", "trigger_rule_id", "reason"):
            if b.get(f) != t.get(f):
                fields[f] = {"base": b.get(f), "target": t.get(f)}
        if fields:
            changes.append({"key": k, "obligation": b["obligation"],
                            "change": "变更", "fields": fields})
    return {
        "base_as_of": base["as_of"],
        "target_as_of": target["as_of"],
        "base_summary": base["summary"],
        "target_summary": target["summary"],
        "changes": changes,
    }


def _diff_fields(v):
    return {f: v.get(f) for f in
            ("status", "effective_deadline", "performed_at", "on_time",
             "triggered_at", "trigger_rule_id", "reason")}
