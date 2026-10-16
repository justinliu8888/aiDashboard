"""ChangeSet：修改已有草稿时返回，对齐 01_接口协议.md §6。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChangeSetEntry(BaseModel):
    regionId: str
    path: str  # JSON Pointer，相对 ReportDefinition 根
    before: Any | None = None
    after: Any | None = None
    summary: str


class ChangeSetUnchanged(BaseModel):
    regionId: str
    summary: str


class ChangeSet(BaseModel):
    baseDefinitionRevision: int
    changed: list[ChangeSetEntry] = Field(default_factory=list)
    unchanged: list[ChangeSetUnchanged] = Field(default_factory=list)
    impact: list[str] = Field(default_factory=list)
    intentBindings: list[dict[str, Any]] = Field(default_factory=list)
