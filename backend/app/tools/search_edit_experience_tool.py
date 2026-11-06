"""search_edit_experience：检索已批准且未禁用的编辑经验（只读事实）。

契约 0003 §3.4：

* **只返回** ``status='approved'`` 的经验；``disabled`` **不返回**；
* **最多 3 条**；无匹配 → 返回空结果（正常继续，**不报错**）；
* 技术错误 → **明确报错**（供追踪记 ``tool_error``），
  **不能冒充「没有相似案例」**；
* 返回体只含：id / method / conditions / capabilityVersion / score /
  来源 recordId。

经验里**没有**别人的区块 ID —— 方法与适用条件用的是**目标角色**
（``targetRegion`` / ``newBlock``）。复用时由服务端
（``edit_experience_service.bind_experience_to_target``）把角色绑到
**当前**目标上。
"""
from __future__ import annotations

from typing import Any

from app.services import edit_experience_service
from app.tools.base import ToolCategory


NAME = "search_edit_experience"
DESCRIPTION = "检索已批准且未禁用的编辑经验（方法 + 适用条件）。"
PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "用户本次修改要求的关键词"},
        "maxResults": {"type": "integer", "description": "最多返回几条，默认 3，上限 3"},
    },
    "required": ["query"],
}
CATEGORY = ToolCategory.FACT
IS_READ_ONLY = True
APPLICABLE_STATES = ("USER_EDITING_TEMPLATE", "WAITING_USER_CONFIRM")


#: 没有命中时给模型的话（**不阻塞**正常分析）。
NO_MATCH_MESSAGE = "没有匹配的已批准经验，继续正常分析。"


def run(query: str, maxResults: int = 3) -> dict[str, Any]:
    """检索经验。

    * 无匹配 → ``{"matches": [], "count": 0, "status": "empty", ...}``；
    * 技术错误 → ``{"error": "...", "status": "error"}``（**不冒充空结果**）。
    """
    text = (query or "").strip()
    if not text:
        edit_experience_service.trace_experience_event(
            "tool_call",
            name=NAME,
            params={"query": ""},
            result={"count": 0},
            status="ok",
        )
        return {
            "matches": [],
            "count": 0,
            "status": "empty",
            "message": NO_MATCH_MESSAGE,
        }

    try:
        matches = edit_experience_service.search_experiences(
            query=text, limit=maxResults
        )
    except Exception as exc:  # noqa: BLE001 — 技术错误要**明确**报出去
        logger_msg = f"编辑经验检索失败：{type(exc).__name__}: {exc}"
        edit_experience_service.trace_experience_event(
            "tool_call",
            name=NAME,
            params={"query": text},
            result={"error": type(exc).__name__},
            status="error",
            errorCategory="tool_error",
        )
        return {
            "error": logger_msg,
            "matches": [],
            "count": 0,
            "status": "error",
            "message": "编辑经验检索出错，本次不要复用经验，按正常流程分析。",
        }

    edit_experience_service.trace_experience_event(
        "tool_call",
        name=NAME,
        params={"query": text, "maxResults": maxResults},
        result={"count": len(matches)},
        status="ok",
    )

    if not matches:
        return {
            "matches": [],
            "count": 0,
            "status": "empty",
            "message": NO_MATCH_MESSAGE,
        }
    return {"matches": matches, "count": len(matches), "status": "ok"}


__all__ = [
    "APPLICABLE_STATES",
    "CATEGORY",
    "DESCRIPTION",
    "IS_READ_ONLY",
    "NAME",
    "PARAMETERS",
    "run",
]
