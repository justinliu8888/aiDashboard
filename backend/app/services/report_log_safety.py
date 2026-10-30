"""#61 既有日志泄漏接管：日志、持久化记录与用户可见消息里只留标识与原因。

这一轮接管三类既有泄漏：

- **用户原话**：用户在输入框里说的话，原先直接进了常规日志。
- **模型返回原文**：模型返回的 JSON / 自由文本，原先进了常规日志。
- **上游异常原文**：上游抛出的异常文本，原先进了常规日志、持久化的操作记录
  原因字段，以及用户可见的错误消息。

接管之后常规日志里留下的只有：标识（会话号 / 操作号）、原因类型、长度与耗时。

本模块只提供「怎么写才安全」的公共件：**不改模型行为、不改提示词、不改解析策略、
不改重试次数、不改失败码**。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.services.report_diagnostics_service import mask_text_shape

#: 原因类文本的长度上限。原因是要给人看的，但不能无限长。
MAX_REASON_CHARS = 200

#: 「一眼就是标识」的形状：字母开头，只含字母数字下划线，最长 40 字。
#:
#: 工具名（``submit_draft_plan``）、模式名（``GENERATE``）、房间号（``room_a``）
#: 都是这个形状 —— 排查时必须看得见。但模型可以往这些字段里塞任意文本，
#: 所以形状对不上的，只留形状摘要。
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")

#: 给用户看的失败文案：说清楚「发生了什么、用户能做什么」，不带技术细节。
USER_VISIBLE_ERROR_MESSAGE = "服务暂时不可用，请稍后重试"

#: 给用户看的失败文案（超时）：同样不带技术细节。
USER_VISIBLE_TIMEOUT_MESSAGE = "服务响应超时，请稍后重试"

#: 用户可见错误消息的失败码。前端按它区分，后端不因此改变既有失败码。
USER_VISIBLE_ERROR_CODE = "INTERNAL_ERROR"


def safe_reason(exc: BaseException | None) -> str:
    """异常 → 原因类型名。**不返回异常原文**。"""
    if exc is None:
        return "UnknownError"
    return type(exc).__name__


def omitted_user_text(text: str | None) -> str:
    """用户原话 → 只留长度。日志里看不到用户说了什么。"""
    return f"[用户原话已省略，长度={len(text or '')}]"


def model_text_shape(text: str | None, *, limit: int = MAX_REASON_CHARS) -> str:
    """模型返回原文 → 形状级摘要（字符类别图），并按上限截断。

    一个字的内容都不留，但能看出「像不像 JSON、断在哪」——
    这比只留长度多了一点排查价值，又不会把原文写出去。
    """
    shape = mask_text_shape(text or "")
    if len(shape) > limit:
        return f"{shape[:limit]}...<已截断 {len(shape) - limit} 字>"
    return shape


def capped_reason(text: str | None, *, limit: int = MAX_REASON_CHARS) -> str:
    """固定文案的原因 → 截断到上限后保留。

    只用于**我们自己写的固定文案**（规则阻断说明之类）。
    不能用于用户原话或模型原文 —— 那两个走 :func:`omitted_user_text`
    与 :func:`omitted_model_text`。
    """
    value = (text or "").strip()
    if len(value) > limit:
        return f"{value[:limit]}...<已截断 {len(value) - limit} 字>"
    return value


def safe_identifier(value: Any) -> str:
    """标识：形状对得上就原样保留，对不上只留形状摘要。

    标识（工具名、模式名、房间号）排查时必须看得见 —— 这是「常规日志只剩
    标识与原因」里的「标识」。但这些字段的值可能来自模型，所以只放行
    「一眼就是标识」的形状：字母开头、只含字母数字下划线、最长 40 字。
    """
    text = "" if value is None else str(value)
    if _SAFE_IDENTIFIER.match(text):
        return text
    return model_text_shape(text)


def log_failure(logger: logging.Logger, message: str, exc: BaseException) -> None:
    """记一条失败日志：**只有原因类型与调用栈，没有异常原文**。

    ``exc_info=(type, None, tb)`` 让调用栈还在（能看出哪一行炸的），
    但异常对象被换掉 —— 默认的 ``logger.exception`` 会把异常原文一起写进日志
    （``ValueError: 用户原话`` 这种）。

    写日志本身失败也不许影响业务请求（契约表 §6.5）：故障处理路径上已经在
    ``except`` 里了，日志再抛错会把原始异常盖掉，一路穿出到业务请求。
    """
    try:
        logger.warning(
            "%s errorType=%s",
            message,
            safe_reason(exc),
            exc_info=(type(exc), None, exc.__traceback__),
        )
    except Exception:  # noqa: BLE001
        pass


def log_info(logger: logging.Logger, message: str, *args: Any) -> None:
    """写一条常规日志；日志器自己抛错也不许影响业务请求。"""
    try:
        logger.info(message, *args)
    except Exception:  # noqa: BLE001
        pass


def log_warning(logger: logging.Logger, message: str, *args: Any) -> None:
    """写一条告警日志；日志器自己抛错也不许影响业务请求。"""
    try:
        logger.warning(message, *args)
    except Exception:  # noqa: BLE001
        pass


def user_visible_error_message(exc: BaseException | None = None) -> str:
    """用户可见的失败文案。

    只说「出了什么事、用户能做什么」，不带异常原文、不带堆栈、不带内部标识。
    """
    if isinstance(exc, TimeoutError):
        return USER_VISIBLE_TIMEOUT_MESSAGE
    return USER_VISIBLE_ERROR_MESSAGE
