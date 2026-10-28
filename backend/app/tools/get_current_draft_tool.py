"""get_current_draft：读当前对话的报表草稿概览（只读事实）。

草稿可能很大，塞进模型参数既浪费 token 又容易被模型改坏，
所以本工具只返回**概览**（有哪些区块、分别是什么类型）。

事实来源是**服务端会话记录**，不是本轮请求带回来的快照：

    - 请求快照可能缺失、过旧，甚至和库里不一致；
      快照缺失**不能**当成「当前对话没有草稿」。
    - 读不到就说读不到（status=UNAVAILABLE），不能说成没有。

返回里的 ``status`` 是事实状态，取值见 ``app.schemas.fact.FactStatus``：
``FOUND`` 已核实 / ``ABSENT`` 确认没有 / ``UNAVAILABLE`` 读不到 / ``FORBIDDEN`` 越界。

用户说"把趋势图改成 7 天""删掉某个卡片"时，先调用它确认目标区块存在，
要细看某一块再用 get_current_region。
"""
from __future__ import annotations

from typing import Any

from app.services.session_context import current_session_key
from app.services.session_fact_service import session_fact_service
from app.schemas.fact import FactStatus
from app.tools.base import LOOKUP_STATES, ToolCategory
from app.tools.context import current_tool_context


NAME = "get_current_draft"
DESCRIPTION = (
    "读取当前对话的报表草稿概览：报表名、版本、包含哪些 region（区块）及各自类型，"
    "以及这份草稿还差什么才能往下走。"
    "事实来自服务端会话记录：返回的 status 是事实状态，"
    "ABSENT 表示当前对话确实没有草稿，UNAVAILABLE 表示暂时无法确认（读不到），"
    "两者不能混为一谈。"
    "用户在说'把趋势图改成 7 天''删掉某个卡片'时，先调用它确认目标区块是否存在，"
    "再决定改哪个 region。"
)
PARAMETERS = {
    "type": "object",
    "properties": {},
}
CATEGORY = ToolCategory.FACT
IS_READ_ONLY = True
APPLICABLE_STATES = LOOKUP_STATES


def _region_summary(region: dict[str, Any]) -> dict[str, Any]:
    """region 概览：只保留模型判断需要的信息。"""
    component = region.get("component") or {}
    metric = component.get("metric") or {}
    return {
        "regionId": region.get("id", ""),
        "name": region.get("name", ""),
        "role": region.get("role", ""),
        "componentType": component.get("type", ""),
        "chartKind": component.get("chartKind"),
        "metricRef": metric.get("id"),
        "metricLabel": metric.get("label"),
    }


def _from_request_snapshot(ctx: Any) -> dict[str, Any]:
    """没有会话标识时的兜底：用本轮请求快照，并且明确标注来源不是服务端。"""
    definition = ctx.definition
    regions = definition.regions or []
    metadata = definition.metadata or {}
    return {
        "status": FactStatus.FOUND.value,
        "scope": "current_session",
        "exists": True,
        "reportName": str(metadata.get("name") or ""),
        "revision": int(metadata.get("revision") or 0),
        "baseDefinitionRevision": ctx.base_definition_revision,
        "selectedRegionId": ctx.selected_region_id,
        "regionCount": len(regions),
        "regions": [_region_summary(region) for region in regions],
        "total": len(regions),
        "pendingSteps": [],
        "savedDefinition": None,
        "preview": None,
        "source": "request_snapshot",
        "truncated": False,
        "reason": "",
    }


def run() -> dict[str, Any]:
    ctx = current_tool_context()
    session_key = current_session_key()

    if ctx is None:
        return {
            "status": FactStatus.UNAVAILABLE.value,
            "scope": "current_session",
            "exists": False,
            "reportName": "",
            "revision": 0,
            "regionCount": 0,
            "regions": [],
            "total": 0,
            "pendingSteps": [],
            "savedDefinition": None,
            "preview": None,
            "source": "none",
            "truncated": False,
            "reason": "本轮没有绑定会话上下文，无法核对当前对话的草稿",
        }

    if session_key is None and ctx.definition is not None:
        # 没有会话标识但请求带了草稿：只能用请求快照，并标注它不是服务端事实。
        return _from_request_snapshot(ctx)

    facts = session_fact_service.load(
        session_id=session_key,
        project_id=ctx.project_id,
        subject_ref=ctx.subject_ref,
        subject_id=ctx.subject_id,
        request_has_draft=ctx.definition is not None,
    )

    draft = facts.draft
    status = draft.status if draft is not None else facts.status
    regions = list(draft.regions) if draft is not None else []
    return {
        "status": status.value,
        "scope": facts.scope,
        "exists": bool(draft.exists) if draft is not None else False,
        "reportName": draft.reportName if draft is not None else "",
        "revision": draft.revision if draft is not None else 0,
        "baseDefinitionRevision": ctx.base_definition_revision,
        "selectedRegionId": ctx.selected_region_id,
        "regionCount": draft.regionCount if draft is not None else 0,
        "regions": regions,
        "total": len(regions),
        "pendingSteps": list(draft.pendingSteps) if draft is not None else [],
        "savedDefinition": facts.savedDefinition,
        "preview": facts.preview,
        "source": draft.source if draft is not None else "server_session",
        "truncated": False,
        "reason": facts.reason,
    }
