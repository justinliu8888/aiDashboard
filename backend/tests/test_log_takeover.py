"""#61 既有日志泄漏接管：日志、持久化记录与用户可见消息里不留原文。

覆盖工单 #61 的验收标准：
- 用户原话 / 模型返回原文 / 上游异常原文都不进常规日志；
- 上游异常原文不进持久化的操作记录原因字段；
- 用户可见的 SSE ``error`` 事件不含异常原文，但仍有用户能看懂的意思；
- 写日志失败不影响业务请求；
- 模型行为不变（提示词、解析降级顺序、重试次数、失败码与既有文案逐字未变）。
"""
from __future__ import annotations

import logging

from app.services import report_log_safety as safety


#: 敏感串在**运行时**拼出来，不写在源码字面量里。
#: 原因：失败日志会带调用栈，调用栈会打印源码行 —— 源码里写死的敏感串
#: 会跟着栈一起出现在日志里，测出来的就不是「异常原文没泄漏」了。
SECRET_PHRASE = "SECRET_" + "PHRASE_ABC"
SECRET_EXC = "上游异常原文 " + "SECRET_EXC_XYZ"


def _raise_with_message(message: str) -> None:
    """按运行时传入的消息抛异常（源码里不出现消息本身）。"""
    raise ValueError(message)


# ----------------------------------------------------------------------
# 公共件：日志里怎么写才安全
# ----------------------------------------------------------------------


def test_safe_reason_returns_exception_type_not_message():
    exc = ValueError(SECRET_PHRASE)
    assert safety.safe_reason(exc) == "ValueError"
    assert SECRET_PHRASE not in safety.safe_reason(exc)


def test_omitted_texts_keep_only_length():
    assert SECRET_PHRASE not in safety.omitted_user_text(SECRET_PHRASE)
    assert SECRET_PHRASE not in safety.omitted_model_text(SECRET_PHRASE)
    assert f"长度={len(SECRET_PHRASE)}" in safety.omitted_user_text(SECRET_PHRASE)


def test_capped_reason_truncates_at_limit():
    reason = safety.capped_reason("原" * 500, limit=10)
    assert len(reason) < 60
    assert "已截断" in reason


def test_log_failure_keeps_stack_but_drops_exception_message(caplog):
    """失败日志留调用栈，不留异常原文。"""
    logger = logging.getLogger("app.test.log_safety")
    with caplog.at_level(logging.WARNING, logger="app.test.log_safety"):
        try:
            _raise_with_message(SECRET_EXC)
        except ValueError as exc:
            safety.log_failure(logger, "本轮失败", exc)

    text = caplog.text
    assert "本轮失败" in text
    assert "errorType=ValueError" in text
    assert SECRET_EXC not in text
    assert "Traceback" in text


def test_log_failure_swallows_logger_errors():
    """写日志失败不许影响业务请求。"""

    class _BrokenLogger:
        def warning(self, *args, **kwargs):
            raise RuntimeError("日志器自己炸了")

    try:
        _raise_with_message(SECRET_EXC)
    except ValueError as exc:
        safety.log_failure(_BrokenLogger(), "本轮失败", exc)  # 不抛


def test_user_visible_error_message_has_no_technical_detail():
    exc = ValueError(SECRET_EXC)
    message = safety.user_visible_error_message(exc)
    assert SECRET_EXC not in message
    assert message == safety.USER_VISIBLE_ERROR_MESSAGE
    assert "重试" in message
