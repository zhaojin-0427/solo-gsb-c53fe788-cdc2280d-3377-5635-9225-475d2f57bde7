"""确定性回放引擎。

输入一组版本化规则与一组事实事件，按 (有效时间, 创建序号) 排序后回放，
推演每项义务实例的状态（待触发/履行中/已履行/逾期/被替代）及依据链。

回放结果只取决于输入的规则与事件，不依赖系统时钟，因此刷新或重启后
只要数据一致，推演结果必然一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

STATE_LABELS = {
    "pending": "待触发",
    "performing": "履行中",
    "fulfilled": "已履行",
    "overdue": "逾期",
    "substituted": "被替代",
}

UNITS = ("minutes", "hours", "days", "weeks")


def parse_time(s: str) -> datetime:
    """解析 ISO 8601 时间；无时区信息时按 UTC 处理，保证可比较。"""
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def duration_delta(spec: dict) -> timedelta:
    unit = spec.get("unit")
    if unit not in UNITS:
        raise ValueError(f"未知期限单位: {unit!r}（支持 {UNITS}）")
    amount = spec.get("amount")
    if not isinstance(amount, (int, float)) or amount < 0:
        raise ValueError("期限 amount 必须是非负数字")
    return timedelta(**{unit: amount})


def _get_path(obj: Any, path: str):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def check_condition(conditions: Optional[list], payload: dict) -> bool:
    """条件列表，全部满足才算匹配。元素形如 {"field": "a.b", "op": "eq", "value": 1}。"""
    for c in conditions or []:
        val, ok = _get_path(payload, c.get("field", ""))
        op = c.get("op", "eq")
        if op == "exists":
            if not ok:
                return False
            continue
        if not ok:
            return False
        want = c.get("value")
        try:
            if op == "eq":
                if val != want:
                    return False
            elif op == "ne":
                if val == want:
                    return False
            elif op == "gt":
                if not val > want:
                    return False
            elif op == "gte":
                if not val >= want:
                    return False
            elif op == "lt":
                if not val < want:
                    return False
            elif op == "lte":
                if not val <= want:
                    return False
            elif op == "contains":
                if want not in val:
                    return False
            else:
                return False
        except TypeError:
            return False
    return True


def _rule_targeted(rule: dict, payload: dict, extra_condition) -> bool:
    """履行/暂停/恢复/替代类事件的归属判定：

    事件 payload 若显式声明 rule_id，则必须等于本规则 id 才生效；
    未声明时作用于所有配置了该事件类型的规则；再叠加规则上的自定义条件。
    """
    rid = payload.get("rule_id") if isinstance(payload, dict) else None
    if rid is not None and rid != rule["rule_id"]:
        return False
    return check_condition(extra_condition, payload)


@dataclass
class Obligation:
    key: str
    rule_id: str
    rule_name: str
    scope: str
    state: str  # pending / performing / fulfilled / overdue / substituted
    triggered_at: datetime
    started_at: Optional[datetime] = None
    deadline: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    paused_total: timedelta = timedelta(0)
    fulfilled_at: Optional[datetime] = None
    overdue_at: Optional[datetime] = None
    substituted_at: Optional[datetime] = None
    evidence: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "scope": self.scope,
            "state": self.state,
            "state_label": STATE_LABELS[self.state],
            "triggered_at": iso(self.triggered_at),
            "started_at": iso(self.started_at),
            "deadline": iso(self.deadline),
            "paused": self.paused_at is not None,
            "paused_total_seconds": int(self.paused_total.total_seconds()),
            "fulfilled_at": iso(self.fulfilled_at),
            "overdue_at": iso(self.overdue_at),
            "substituted_at": iso(self.substituted_at),
            "evidence": self.evidence,
        }


def validate_rules(rules: Any) -> list:
    """校验规则集结构，返回规则列表；不合法时抛 ValueError。"""
    if not isinstance(rules, list) or not rules:
        raise ValueError("规则必须是非空数组")
    ids = set()
    for r in rules:
        if not isinstance(r, dict):
            raise ValueError("每条规则必须是对象")
        rid = r.get("rule_id")
        if not rid or not isinstance(rid, str):
            raise ValueError("每条规则必须包含字符串 rule_id")
        if rid in ids:
            raise ValueError(f"rule_id 重复: {rid}")
        ids.add(rid)
        trig = r.get("trigger") or {}
        if not trig.get("event_type"):
            raise ValueError(f"规则 {rid} 缺少 trigger.event_type")
        duration_delta(r.get("deadline") or {})  # 校验期限合法
        for dep in r.get("depends_on") or []:
            if dep == rid:
                raise ValueError(f"规则 {rid} 不能依赖自身")
    for r in rules:
        for dep in r.get("depends_on") or []:
            if dep not in ids:
                raise ValueError(f"规则 {r['rule_id']} 依赖了不存在的规则 {dep}")
    # 依赖环检测
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {rid: WHITE for rid in ids}
    dep_map = {r["rule_id"]: (r.get("depends_on") or []) for r in rules}

    def dfs(u):
        color[u] = GRAY
        for v in dep_map.get(u, []):
            if color[v] == GRAY:
                raise ValueError(f"依赖存在环: {u} -> {v}")
            if color[v] == WHITE:
                dfs(v)
        color[u] = BLACK

    for rid in ids:
        if color[rid] == WHITE:
            dfs(rid)
    return rules


def replay(rules: list, events: list, as_of: Optional[str] = None) -> dict:
    """回放事件，返回 {"evaluation_time": ..., "obligations": [...]}。

    events 元素需包含 event_id/seq/event_type/scope/valid_time/payload。
    排序键为 (有效时间, 创建序号)，保证同刻事件按录入先后处理。
    """
    rules = validate_rules(rules)
    rule_by_id = {r["rule_id"]: r for r in rules}
    obligations: list[Obligation] = []

    parsed = sorted(
        ((parse_time(ev["valid_time"]), ev) for ev in events),
        key=lambda x: (x[0], x[1]["seq"]),
    )

    def deps_satisfied(rule: dict, scope: str) -> bool:
        for dep in rule.get("depends_on") or []:
            if not any(
                o.rule_id == dep and o.scope == scope and o.state == "fulfilled"
                for o in obligations
            ):
                return False
        return True

    def activate_pending(scope: str, t: datetime, cause: dict) -> None:
        # 循环以支持链式依赖（A 履行激活 B，B 的履行又激活 C 的情形不会出现于此，
        # 但同一事件可能同时满足多条待触发义务的依赖）
        changed = True
        while changed:
            changed = False
            for o in obligations:
                if o.state == "pending" and o.scope == scope and deps_satisfied(
                    rule_by_id[o.rule_id], scope
                ):
                    rule = rule_by_id[o.rule_id]
                    o.state = "performing"
                    o.started_at = t
                    o.deadline = t + duration_delta(rule["deadline"])
                    o.evidence.append(
                        {
                            "at": iso(t),
                            "kind": "activated",
                            "event_id": cause.get("event_id"),
                            "seq": cause.get("seq"),
                            "detail": "依赖义务已履行，义务进入履行中，期限自此时起算",
                        }
                    )
                    changed = True

    def check_overdue(t: datetime) -> None:
        for o in obligations:
            if (
                o.state == "performing"
                and o.deadline is not None
                and o.paused_at is None  # 暂停期间时钟不走，不会逾期
                and t > o.deadline
            ):
                o.state = "overdue"
                o.overdue_at = o.deadline
                o.evidence.append(
                    {
                        "at": iso(o.deadline),
                        "kind": "overdue",
                        "detail": "有效履行时间超过相对期限，义务逾期",
                    }
                )

    last_t: Optional[datetime] = None
    for t, ev in parsed:
        last_t = t
        etype = ev["event_type"]
        scope = ev.get("scope") or "default"
        payload = ev.get("payload") or {}

        for rule in rules:
            rid = rule["rule_id"]

            # 1) 触发：创建义务实例
            trig = rule.get("trigger") or {}
            if etype == trig.get("event_type") and check_condition(
                trig.get("condition"), payload
            ):
                ob = Obligation(
                    key=f"{rid}@{scope}#{ev['event_id']}",
                    rule_id=rid,
                    rule_name=rule.get("name") or rid,
                    scope=scope,
                    state="pending",
                    triggered_at=t,
                )
                ob.evidence.append(
                    {
                        "at": iso(t),
                        "kind": "triggered",
                        "event_id": ev["event_id"],
                        "seq": ev["seq"],
                        "detail": f"事件 {etype} 触发义务「{ob.rule_name}」",
                    }
                )
                obligations.append(ob)
                if deps_satisfied(rule, scope):
                    ob.state = "performing"
                    ob.started_at = t
                    ob.deadline = t + duration_delta(rule["deadline"])
                    ob.evidence.append(
                        {
                            "at": iso(t),
                            "kind": "activated",
                            "event_id": ev["event_id"],
                            "seq": ev["seq"],
                            "detail": "依赖已满足，义务进入履行中，期限自此时起算",
                        }
                    )
                else:
                    ob.evidence.append(
                        {
                            "at": iso(t),
                            "kind": "deps_waiting",
                            "event_id": ev["event_id"],
                            "seq": ev["seq"],
                            "detail": "依赖义务尚未履行，义务待触发",
                        }
                    )

            # 2) 履行：作用于本范围内最早未完成的实例
            if (
                rule.get("fulfill_event_type")
                and etype == rule["fulfill_event_type"]
                and _rule_targeted(rule, payload, rule.get("fulfill_condition"))
            ):
                targets = [
                    o
                    for o in obligations
                    if o.rule_id == rid
                    and o.scope == scope
                    and o.state in ("performing", "overdue")
                ]
                if not targets:
                    targets = [
                        o
                        for o in obligations
                        if o.rule_id == rid and o.scope == scope and o.state == "pending"
                    ]
                if targets:
                    target = targets[0]
                    was_overdue = target.state == "overdue"
                    target.state = "fulfilled"
                    target.fulfilled_at = t
                    target.evidence.append(
                        {
                            "at": iso(t),
                            "kind": "fulfilled_late" if was_overdue else "fulfilled",
                            "event_id": ev["event_id"],
                            "seq": ev["seq"],
                            "detail": "迟延履行，义务完成"
                            if was_overdue
                            else "义务履行完成",
                        }
                    )
                    # 依赖本规则的其他义务可能被激活
                    activate_pending(scope, t, ev)

            # 3) 暂停
            if etype in (rule.get("pause_event_types") or []) and _rule_targeted(
                rule, payload, rule.get("pause_condition")
            ):
                for o in obligations:
                    if (
                        o.rule_id == rid
                        and o.scope == scope
                        and o.state == "performing"
                        and o.paused_at is None
                    ):
                        o.paused_at = t
                        o.evidence.append(
                            {
                                "at": iso(t),
                                "kind": "paused",
                                "event_id": ev["event_id"],
                                "seq": ev["seq"],
                                "detail": "履行时钟暂停",
                            }
                        )

            # 4) 恢复：截止时间顺延暂停时长
            if etype in (rule.get("resume_event_types") or []) and _rule_targeted(
                rule, payload, rule.get("resume_condition")
            ):
                for o in obligations:
                    if (
                        o.rule_id == rid
                        and o.scope == scope
                        and o.paused_at is not None
                    ):
                        delta = t - o.paused_at
                        o.paused_total += delta
                        if o.deadline is not None:
                            o.deadline += delta
                        o.paused_at = None
                        o.evidence.append(
                            {
                                "at": iso(t),
                                "kind": "resumed",
                                "event_id": ev["event_id"],
                                "seq": ev["seq"],
                                "detail": f"恢复计时，暂停 {delta}，截止时间相应顺延",
                            }
                        )

            # 5) 替代：终止本范围内所有未终结实例
            if etype in (rule.get("substitute_event_types") or []) and _rule_targeted(
                rule, payload, rule.get("substitute_condition")
            ):
                for o in obligations:
                    if (
                        o.rule_id == rid
                        and o.scope == scope
                        and o.state in ("pending", "performing", "overdue")
                    ):
                        o.state = "substituted"
                        o.substituted_at = t
                        o.evidence.append(
                            {
                                "at": iso(t),
                                "kind": "substituted",
                                "event_id": ev["event_id"],
                                "seq": ev["seq"],
                                "detail": "义务被替代，原义务终止",
                            }
                        )

        check_overdue(t)

    # 评估时刻：默认取最后一个事件的有效时间（确定性，不依赖系统时钟）
    eval_t = parse_time(as_of) if as_of else last_t
    if eval_t is not None:
        if last_t is not None and eval_t < last_t:
            eval_t = last_t
        check_overdue(eval_t)

    return {
        "evaluation_time": iso(eval_t),
        "obligations": [o.to_dict() for o in obligations],
    }
