"""Versioned rule validation and normalization.

Rule types
----------
trigger    事件触发义务 + 相对/固定期限
suspend    暂停 / 恢复（可成对，也可单向）
substitute 替代旧义务，产生新义务（可带新期限与新依赖）
dependency 义务依赖：被依赖义务全部履行后，义务才进入履行中
"""
from __future__ import annotations

from typing import Any

from .timeutil import parse_dt


class RuleError(ValueError):
    pass


def _require(params: dict[str, Any], key: str, rid: str) -> Any:
    if key not in params or params[key] in (None, ""):
        raise RuleError(f"规则 {rid} 缺少参数 {key}")
    return params[key]


def _deadline_of(rid: str, params: dict[str, Any], base_valid_at=None):
    """Return (kind, value): ('fixed', datetime) | ('days', int) | (None, None)."""
    if "deadline_at" in params and params["deadline_at"]:
        try:
            return "fixed", parse_dt(params["deadline_at"])
        except Exception as exc:  # noqa: BLE001
            raise RuleError(f"规则 {rid} 的 deadline_at 不是合法 ISO8601: {exc}") from exc
    if "deadline_days" in params and params["deadline_days"] is not None:
        days = params["deadline_days"]
        if not isinstance(days, int) or days < 0:
            raise RuleError(f"规则 {rid} 的 deadline_days 必须是非负整数")
        return "days", days
    if base_valid_at is not None:
        # 触发规则没有期限则视为无固定期限（仅履行/替代可终结）
        return None, None
    raise RuleError(f"规则 {rid} 需要 deadline_days 或 deadline_at")


def validate_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Validate a raw rule dict; returns a normalized copy."""
    rid = rule.get("rid")
    rtype = rule.get("rtype")
    params = dict(rule.get("params") or {})
    out = {"rid": rid, "rtype": rtype, "params": params,
           "description": rule.get("description", "")}

    if rtype == "trigger":
        _require(params, "event_type", rid)
        _require(params, "obligation", rid)
        if "deadline_days" not in params and "deadline_at" not in params:
            raise RuleError(f"规则 {rid} 需要 deadline_days 或 deadline_at")
        _deadline_of(rid, params)

    elif rtype == "suspend":
        # 形式 A：pause_event_type + resume_event_type（可只给其一）
        # 形式 B：event_type + action(suspend|resume)
        if "pause_event_type" in params or "resume_event_type" in params:
            if not (params.get("pause_event_type") or params.get("resume_event_type")):
                raise RuleError(f"规则 {rid} 的暂停/恢复事件类型不能同时为空")
        else:
            _require(params, "event_type", rid)
            action = params.get("action", "suspend")
            if action not in ("suspend", "resume"):
                raise RuleError(f"规则 {rid} 的 action 必须是 suspend 或 resume")
        if "obligation" not in params and not params.get("obligation_from_payload"):
            params["obligation_from_payload"] = True

    elif rtype == "substitute":
        _require(params, "event_type", rid)
        _require(params, "old_obligation", rid)
        _require(params, "new_obligation", rid)
        if "new_deadline_days" in params and params["new_deadline_days"] is not None:
            d = params["new_deadline_days"]
            if not isinstance(d, int) or d < 0:
                raise RuleError(f"规则 {rid} 的 new_deadline_days 必须是非负整数")
        deps = params.get("new_depends_on")
        if deps is not None and not (
            isinstance(deps, list) and all(isinstance(x, str) and x for x in deps)
        ):
            raise RuleError(f"规则 {rid} 的 new_depends_on 必须是字符串数组")

    elif rtype == "dependency":
        ob = _require(params, "obligation", rid)
        deps = _require(params, "depends_on", rid)
        if not isinstance(deps, list) or not deps or not all(
            isinstance(x, str) and x for x in deps
        ):
            raise RuleError(f"规则 {rid}({ob}) 的 depends_on 必须是非空字符串数组")
        if ob in deps:
            raise RuleError(f"规则 {rid} 的义务不能依赖自身")
    else:
        raise RuleError(f"未知规则类型: {rtype}")
    return out


def validate_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    norm: list[dict[str, Any]] = []
    for r in rules:
        nr = validate_rule(r)
        if nr["rid"] in seen:
            raise RuleError(f"规则 rid 重复: {nr['rid']}")
        seen.add(nr["rid"])
        norm.append(nr)
    return norm


def suspend_directives(rule: dict[str, Any]):
    """Expand a suspend rule into [(event_type, action)] pairs."""
    p = rule["params"]
    if "pause_event_type" in p or "resume_event_type" in p:
        out = []
        if p.get("pause_event_type"):
            out.append((p["pause_event_type"], "suspend"))
        if p.get("resume_event_type"):
            out.append((p["resume_event_type"], "resume"))
        return out
    return [(p["event_type"], p.get("action", "suspend"))]
