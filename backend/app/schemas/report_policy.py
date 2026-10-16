"""ReportPolicyDraft：首功能最小子集，对齐 01_接口协议.md §4。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ReportPolicyDraft(BaseModel):
    id: str | None = None
    name: str
    description: str | None = None
    subjectRef: str
    # TIME_WINDOW / GRAIN / QUALITY / COMPUTATION
    allowedOverrides: list[str] = Field(default_factory=list)
    # POLICY_DRIVEN / MANUAL_ONLY
    executionMode: str = "MANUAL_ONLY"
