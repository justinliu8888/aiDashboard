"""工具注册表：集中声明所有可用工具。

对外提供：
    - list_tools()：工具模块列表
    - get_runner(name)：按名字拿到底层执行函数
    - to_openai_tools()：OpenAI/火山方舟 tool-calling schema
    - to_agentscope_toolkit_async()：注册进 AgentScope Toolkit
    - export_manifest()：导出中台上架清单（JSON）
    - tools_for_state(state)：按当前节点筛选可用工具
    - validate_tool_contracts()：校验工具契约自洽

每个工具模块需提供 NAME / DESCRIPTION / PARAMETERS / CATEGORY /
IS_READ_ONLY / APPLICABLE_STATES / run()；
终结工具还需提供 CONCLUSION_STATES（可产生的结论状态）。
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any, Callable

from app.schemas.agent import AgentState
from app.config import get_settings
from app.tools import (
    build_definition_draft_tool,
    check_capability_tool,
    check_product_capability_tool,
    get_current_draft_tool,
    get_current_region_tool,
    get_current_session_status_tool,
    get_recorded_progress_tool,
    get_validation_result_tool,
    search_catalog_tool,
    search_metric_tool,
    submit_edit_answer_tool,
    submit_blocked_tool,
    submit_change_plan_tool,
    submit_clarification_tool,
    submit_draft_plan_tool,
    submit_edit_plan_tool,
    submit_fix_plan_tool,
    submit_query_answer_tool,
)
from app.tools.base import (
    IS_QUERY_ONLY_ATTR,
    QUERY_TERMINAL_TOOLS,
    SAME_STATE_CONCLUSION,
    TOOL_CONTRACT_VERSION,
    ToolCategory,
    is_conclusion_legal,
)


logger = logging.getLogger(__name__)


# 工具模块列表（每个模块需提供统一契约，见 app/tools/base.py）
_TOOLS: list[Any] = [
    # 只读事实
    search_catalog_tool,
    search_metric_tool,
    check_capability_tool,
    get_current_draft_tool,
    get_current_region_tool,
    get_current_session_status_tool,
    get_validation_result_tool,
    # 只读查询的领域事实工具（工单 #15 已记录进度 / #16 实际产品能力）
    get_recorded_progress_tool,
    check_product_capability_tool,
    # 确定性组装
    build_definition_draft_tool,
    # 终结工具
    submit_draft_plan_tool,
    submit_clarification_tool,
    submit_blocked_tool,
    submit_change_plan_tool,
    submit_edit_answer_tool,
    submit_edit_plan_tool,
    submit_fix_plan_tool,
    submit_query_answer_tool,
]

_lookup_failure: ContextVar[dict[str, bool] | None] = ContextVar(
    "edit_lookup_failure", default=None
)


def bind_lookup_failure() -> None:
    _lookup_failure.set({"failed": False})


def take_lookup_failure() -> bool:
    slot = _lookup_failure.get()
    return bool(slot and slot["failed"])


def clear_lookup_failure() -> None:
    _lookup_failure.set(None)


def list_tools() -> list[Any]:
    return list(_TOOLS)


def get_runner(name: str) -> Callable[..., Any] | None:
    for tool in _TOOLS:
        if tool.NAME == name:
            return tool.run
    return None


def tools_for_state(state: AgentState) -> list[Any]:
    """按当前节点筛选可用工具。

    ``APPLICABLE_STATES`` 为空表示全节点可用；非空则只有列出的节点可用。
    只读查询专用的工具（``IS_QUERY_ONLY``）在这里**不出现** ——
    普通业务轮不该有「提交只读答案」这个出口。
    """
    state_value = state.value
    return [
        tool
        for tool in _TOOLS
        if not getattr(tool, IS_QUERY_ONLY_ATTR, False)
        and (not tool.APPLICABLE_STATES or state_value in tool.APPLICABLE_STATES)
    ]


def tools_for_query() -> list[Any]:
    """只读查询模式可用的工具：事实工具 + 查询答案终结工具。

    查询链路在**结构上**就不开放执行权限：组装草稿、保存、校验、预览、
    应用修改、接受或取消计划都不在这里注册，模型调不到。
    不能只靠 Prompt 声称「请不要修改」。
    """
    terminal_names = set(QUERY_TERMINAL_TOOLS)
    return [
        tool
        for tool in _TOOLS
        if tool.CATEGORY == ToolCategory.FACT or tool.NAME in terminal_names
    ]


def conclusion_states(tool: Any) -> tuple[str, ...]:
    """取工具声明的结论状态；非终结工具返回空元组。"""
    return tuple(getattr(tool, "CONCLUSION_STATES", ()) or ())


def validate_tool_contracts() -> list[str]:
    """校验工具契约自洽，返回问题列表（空列表表示全部通过）。

    校验两条硬规则：
        1. 终结工具必须声明 CONCLUSION_STATES；非终结工具不能声明。
        2. 终结工具的每个结论状态，在它的每个适用节点上都必须是合法转移。
           违反第二条意味着模型可能挑中一个落不下去的分支。
    """
    problems: list[str] = []
    valid_states = {state.value for state in AgentState}

    for tool in _TOOLS:
        conclusions = conclusion_states(tool)
        if tool.CATEGORY == ToolCategory.TERMINAL:
            if not conclusions:
                problems.append(f"{tool.NAME} 是终结工具但没有声明 CONCLUSION_STATES")
                continue
        elif conclusions:
            problems.append(f"{tool.NAME} 不是终结工具，不该声明 CONCLUSION_STATES")

        for state in tool.APPLICABLE_STATES:
            if state not in valid_states:
                problems.append(f"{tool.NAME} 的 APPLICABLE_STATES 含非法状态 {state}")
            for conclusion in conclusions:
                if conclusion == SAME_STATE_CONCLUSION:
                    # 原地回答：状态保持不变，恒合法。
                    continue
                if conclusion not in valid_states:
                    problems.append(
                        f"{tool.NAME} 的 CONCLUSION_STATES 含非法状态 {conclusion}"
                    )
                    continue
                if not is_conclusion_legal(AgentState(state), AgentState(conclusion)):
                    problems.append(
                        f"{tool.NAME} 在 {state} 可用，"
                        f"但结论 {conclusion} 不是该节点的合法转移"
                    )
    return problems


def to_openai_tools() -> list[dict[str, Any]]:
    """转成 OpenAI/火山方舟 tool-calling 格式。"""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.NAME,
                "description": tool.DESCRIPTION,
                "parameters": tool.PARAMETERS,
            },
        }
        for tool in _TOOLS
    ]


def export_manifest() -> dict[str, Any]:
    """导出中台上架清单。

    中台按这份清单注册 skill：名字、描述、入参契约、权限语义都齐了，
    上线时不需要再改业务代码。
    """
    return {
        "contractVersion": TOOL_CONTRACT_VERSION,
        "tools": [
            {
                "name": tool.NAME,
                "description": tool.DESCRIPTION,
                "parameters": tool.PARAMETERS,
                "category": tool.CATEGORY.value,
                "readOnly": tool.IS_READ_ONLY,
                "applicableStates": list(tool.APPLICABLE_STATES),
                "conclusionStates": list(conclusion_states(tool)),
            }
            for tool in _TOOLS
        ],
    }


def export_manifest_json() -> str:
    """导出中台上架清单（JSON 文本）。"""
    return json.dumps(export_manifest(), ensure_ascii=False, indent=2)


async def to_agentscope_toolkit_async(
    tools: list[Any] | None = None,
):
    """把工具注册到一个 AgentScope 2.0 Toolkit 并返回。

    每个工具包成 FunctionTool：
      - 用工具原生 PARAMETERS 作为 input_schema
      - run() 返回 dict，FunctionTool 自动 json.dumps 包成 ToolChunk
      - 显式 permission=ALLOW，跳过交互式确认

    ``is_read_only`` 按工具自身语义标注 —— 终结工具是 False，
    不能沿用「全部只读」的旧写法。
    """
    from agentscope.permission import PermissionBehavior, PermissionDecision
    from agentscope.tool import FunctionTool, Toolkit

    toolkit = Toolkit()
    allow = PermissionDecision(
        behavior=PermissionBehavior.ALLOW,
        message="内部报表技能，默认允许执行。",
    )
    for tool in tools if tools is not None else _TOOLS:
        await toolkit.add_tool(_make_function_tool(tool, FunctionTool, allow))
    return toolkit


def _make_function_tool(tool, FunctionTool, permission):
    """为单个工具生成 FunctionTool（闭包绑定工具）。"""
    runner = tool.run
    name = tool.NAME
    description = tool.DESCRIPTION
    parameters = tool.PARAMETERS
    max_error_chars = get_settings().observation_error_max_chars

    async def _invoke(**kwargs: Any):
        """工具执行入口；返回 dict / str，交由 FunctionTool 转成 ToolChunk。"""
        try:
            result = runner(**kwargs)
            if tool.CATEGORY == ToolCategory.FACT:
                # 事实工具的调用要看得见：只读问答的可信度取决于
                # 「模型到底查没查」，而不是它自己怎么说。
                logger.info(
                    "[tool] fact_called name=%s status=%s",
                    name,
                    _fact_status_of(result),
                )
                if isinstance(result, dict) and result.get("error"):
                    slot = _lookup_failure.get()
                    if slot is not None:
                        slot["failed"] = True
            return result
        except Exception as exc:  # noqa: BLE001
            # 异常原文可能很长，截断后回给模型，避免占满上下文
            if tool.CATEGORY == ToolCategory.FACT:
                slot = _lookup_failure.get()
                if slot is not None:
                    slot["failed"] = True
            return {"error": str(exc)[:max_error_chars]}

    return FunctionTool(
        _invoke,
        name=name,
        description=description,
        input_schema=parameters,
        is_concurrency_safe=True,
        is_read_only=tool.IS_READ_ONLY,
        permission=permission,
    )


def _fact_status_of(result: Any) -> str:
    """事实工具结果的一句话状态，供调用日志用。

    领域模块各有自己的词表（availability / outcome），这里只取一个
    能一眼看懂的结论：找到了、确认没有、读不到、没权限、还是出错了。
    """
    if not isinstance(result, dict):
        return "OK"
    if result.get("error"):
        return "ERROR"
    for key in ("status", "availability", "outcome"):
        value = result.get(key)
        if value:
            return str(value)
    return "OK"


__all__ = [
    "ToolCategory",
    "bind_lookup_failure",
    "clear_lookup_failure",
    "conclusion_states",
    "export_manifest",
    "export_manifest_json",
    "get_runner",
    "list_tools",
    "take_lookup_failure",
    "tools_for_query",
    "tools_for_state",
    "validate_tool_contracts",
    "to_agentscope_toolkit_async",
    "to_openai_tools",
]
