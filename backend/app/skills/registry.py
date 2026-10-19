"""Skill 注册表：集中声明所有可用 Skill。

对外提供：
    - list_skills(): 原始 Skill 模块列表
    - to_openai_tools(): OpenAI/火山方舟 tool-calling schema（旧实现保留，备用）
    - get_runner(name): 按名字拿到底层执行函数
    - to_agentscope_toolkit_async(): 把所有 Skill 注册进 AgentScope Toolkit（必须在事件循环中 await）
"""
from __future__ import annotations

from typing import Any, Callable

from app.skills import (
    capability_skill,
    catalog_skill,
    definition_builder_skill,
    metric_skill,
)


# Skill 模块列表（每个模块需提供 NAME / DESCRIPTION / PARAMETERS / run()）
_SKILLS: list[Any] = [
    catalog_skill,
    metric_skill,
    capability_skill,
    definition_builder_skill,
]


def list_skills() -> list[Any]:
    return list(_SKILLS)


def to_openai_tools() -> list[dict[str, Any]]:
    """转成 OpenAI/火山方舟 tool-calling 格式（旧实现保留，备用）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": skill.NAME,
                "description": skill.DESCRIPTION,
                "parameters": skill.PARAMETERS,
            },
        }
        for skill in _SKILLS
    ]


def get_runner(name: str) -> Callable[..., Any] | None:
    for skill in _SKILLS:
        if skill.NAME == name:
            return skill.run
    return None


async def to_agentscope_toolkit_async():
    """把所有 Skill 注册到一个 AgentScope 2.0 Toolkit 并返回。

    每个 Skill 包成 FunctionTool：
      - 用 Skill 原生 PARAMETERS 作为 input_schema
      - run() 返回 dict，FunctionTool 自动 json.dumps 包成 ToolChunk
      - 显式 permission=ALLOW，跳过交互式确认
    """
    from agentscope.permission import PermissionBehavior, PermissionDecision
    from agentscope.tool import FunctionTool, Toolkit

    toolkit = Toolkit()
    allow = PermissionDecision(
        behavior=PermissionBehavior.ALLOW,
        message="内部报表技能，默认允许执行。",
    )
    for skill in _SKILLS:
        await toolkit.add_tool(_make_function_tool(skill, FunctionTool, allow))
    return toolkit


def _make_function_tool(skill, FunctionTool, permission):
    """为单个 Skill 生成 FunctionTool（闭包绑定 skill）。"""
    runner = skill.run
    name = skill.NAME
    description = skill.DESCRIPTION
    parameters = skill.PARAMETERS

    async def _invoke(**kwargs: Any):
        """Skill 执行入口；返回 dict / str，交由 FunctionTool 转成 ToolChunk。"""
        try:
            return runner(**kwargs)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    return FunctionTool(
        _invoke,
        name=name,
        description=description,
        input_schema=parameters,
        is_concurrency_safe=True,
        is_read_only=True,
        permission=permission,
    )
