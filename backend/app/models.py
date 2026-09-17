"""Pydantic models: rule definitions, fact events, API payloads."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .constants import (
    ALL_STATUSES,
    STATUS_ACTIVE,
    STATUS_FULFILLED,
    STATUS_OVERDUE,
    STATUS_PENDING,
    STATUS_SUBSTITUTED,
)

RuleType = Literal["trigger", "suspend", "substitute", "dependency"]


# ---------- rule definitions ----------

class RuleIn(BaseModel):
    rid: str = Field(min_length=1)
    rtype: RuleType
    params: dict[str, Any] = Field(default_factory=dict)
    description: str = ""

    @field_validator("rid")
    @classmethod
    def _rid_no_colon(cls, v: str) -> str:
        if ":" in v:
            raise ValueError("rid 不能包含 ':'")
        return v


class RuleVersionCreate(BaseModel):
    note: str = ""
    rules: list[RuleIn]
    # 触发 CAS：客户端基于已有版本号提交；None 表示首个版本
    expected_version: Optional[int] = None


class RuleVersionUpdate(BaseModel):
    branch_id: str
    expected_branch_version: int
    rule_version: int


# ---------- fact events ----------

class EventIn(BaseModel):
    code: str = Field(default="", description="业务编码，用于同刻稳定排序")
    etype: str = Field(min_length=1)
    valid_at: str = Field(description="有效时间 ISO8601")
    recorded_at: Optional[str] = Field(default=None, description="录入时间 ISO8601，缺省取有效时间")
    seq: int = Field(default=0, ge=0, description="同一有效时刻的创建序号")
    payload: dict[str, Any] = Field(default_factory=dict)


class EventBatchIn(BaseModel):
    events: list[EventIn] = Field(min_length=1)
    expected_version: int = Field(description="CAS 基线：当前分支版本")


class EventPatchIn(BaseModel):
    etype: Optional[str] = None
    valid_at: Optional[str] = None
    recorded_at: Optional[str] = None
    seq: Optional[int] = Field(default=None, ge=0)
    payload: Optional[dict[str, Any]] = None
    expected_version: int


class EventWithdrawIn(BaseModel):
    expected_version: int


# ---------- branches ----------

class BranchCreateIn(BaseModel):
    name: str = Field(min_length=1)
    rule_version: Optional[int] = None
    fork_from_event_id: Optional[str] = Field(
        default=None, description="仅复制该事件（按回放顺序）及其之前的事件"
    )
    note: str = ""


# ---------- evaluation ----------

class CompareIn(BaseModel):
    base_branch_id: str
    target_branch_id: str
    as_of: Optional[str] = None
