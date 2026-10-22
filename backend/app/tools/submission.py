"""终结工具的提交槽。

终结工具（submit_*）与只读工具的根本区别：
    - 只读工具返回事实，模型据此继续推理；
    - 终结工具**提交本轮结论**，提交后模型不应再调用其他工具。

终结工具把结论写进本轮的提交槽，由业务 Agent 在 ReAct 循环结束后统一取走，
组装成 AgentResponse。这样"模型只产候选计划、确定性层负责执行"的边界保持不变。

与 tool_context 一致，用 ContextVar 做请求级隔离。
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any


_submission: ContextVar[dict[str, Any] | None] = ContextVar(
    "tool_submission",
    default=None,
)


def bind_submission() -> None:
    """开始一轮：清空提交槽。"""
    # AgentScope 在子任务中运行工具；复制的 context 会共享该容器，
    # 但子任务里的 ContextVar.set() 不会回写父任务。
    _submission.set({"result": None})


def record_submission(tool_name: str, payload: dict[str, Any]) -> None:
    """记录一次提交。

    同一轮内**只认第一次提交**：模型重复调用时保留首个结论，
    避免后一次调用把已经定好的结论覆盖掉。
    """
    slot = _submission.get()
    if slot is None:
        raise RuntimeError("未绑定本轮终结工具提交槽")
    if slot["result"] is None:
        slot["result"] = {"tool": tool_name, "payload": payload}


def take_submission() -> dict[str, Any] | None:
    """取走本轮提交结果并清空。"""
    slot = _submission.get()
    result = slot["result"] if slot else None
    if slot is not None:
        slot["result"] = None
    return result


def peek_submission() -> dict[str, Any] | None:
    """查看本轮提交结果但不清空。"""
    slot = _submission.get()
    return slot["result"] if slot else None


def clear_submission() -> None:
    """清空提交槽。"""
    _submission.set(None)


def submit_payload(
    tool_name: str,
    model: type,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """校验载荷并写入提交槽。终结工具的公共入口。

    校验不过就返回错误让模型重试，不让非法结构流到确定性层。
    """
    from pydantic import ValidationError

    try:
        validated = model.model_validate(payload)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        return {"accepted": False, "error": f"载荷不合法：{details}"}
    except Exception as exc:  # noqa: BLE001
        return {"accepted": False, "error": f"载荷不合法：{exc}"}

    record_submission(tool_name, validated.model_dump(mode="json"))
    return {"accepted": True, "tool": tool_name}
