"""ReportDefinitionDraft：对齐 report-definition-v1.md 的首功能最小子集。

内部嵌套对象（metadata / defaults / regions / datasets / pipelines 等）在首功能阶段
用松结构的 dict 承载即可，避免复述整个 1.0 协议的字段。真正校验由 Java 后端承担。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ReportDefinitionDraft(BaseModel):
    schemaVersion: str = "1.0"
    metadata: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    policyBinding: dict[str, Any] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)
    page: dict[str, Any] = Field(default_factory=dict)
    theme: dict[str, Any] = Field(default_factory=dict)
    datasets: dict[str, Any] = Field(default_factory=dict)
    pipelines: dict[str, Any] = Field(default_factory=dict)
    regions: list[dict[str, Any]] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    intentBindings: list[dict[str, Any]] = Field(default_factory=list)
    dependencies: dict[str, Any] = Field(default_factory=dict)
