"""权威步骤契约的测试助手（本次新增）。

「受限修改输入」这一步之后，提交必须带**步骤关联**
（``context.stepId`` / ``context.stepVersion``）—— 服务端拿它跟持久化的
权威记录核对，对不上就拒（不执行任何业务动作）。

这一组助手让用例能像真实前端那样走：先从「修改」的响应里取出步骤身份，
再把它带进下一步提交。
"""
from __future__ import annotations

from typing import Any


def step_ref(payload: dict[str, Any]) -> dict[str, Any]:
    """从响应里取出当前这一步的关联（可直接放进 ``context`` 的键值）。"""
    questions = [q for q in (payload.get("questions") or []) if q]
    step = next((q for q in questions if q.get("stepKind") == "edit_request"), None)
    if step is None:
        step = next((q for q in questions if q.get("stepId")), None)
    if step is None:
        return {}
    return {"stepId": step["stepId"], "stepVersion": step.get("stepVersion")}


def edit_request_menu(payload: dict[str, Any]) -> dict[str, Any]:
    """响应里那一张**受限修改输入**的卡。"""
    questions = [q for q in (payload.get("questions") or []) if q]
    for question in questions:
        if question.get("stepKind") == "edit_request":
            return question
    raise AssertionError("响应里没有「受限修改输入」这一步")
