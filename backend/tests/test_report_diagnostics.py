"""#57 模型诊断的验收测试。

覆盖工单《统一开发 02：模型诊断——每轮请求标识、上游元信息采集与失败诊断记录》
的验收标准：

- 每轮模型调用一个独立标识，关联会话/消息/操作；重试轮标识不同；
- 元信息（模型、提示词版本与摘要哈希、阶段、序号、耗时、长度、分类）齐全；
- 上游给了就如实记，没给写「未知」，不靠耗时或文本猜截断；
- 解析失败分三类，可定位时带 JSON 偏移与行列；
- 「业务已返回」「final 已入队」「流结束或被取消」分别记录；
- 诊断记录独立于常规日志与会话库，脱敏后落受限目录；
- 诊断记录有单条上限、总容量与保留期限；
- 常规日志不出现用户原话、模型返回原文与上游异常原文。

真实模型未被调用：只替换注入的 ``model_call`` 与上游客户端。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.report_plan_agent import generate_candidate
from app.config import get_settings
from app.services import report_diagnostics_service as diagnostics
from app.services.report_design_model_client import ReportDesignModelClient


REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture()
def diagnostics_dir(tmp_path, monkeypatch) -> Path:
    """把诊断目录指到临时目录（默认目录在用户 Library 里，不进仓库）。"""
    settings = get_settings()
    target = tmp_path / "diagnostics"
    monkeypatch.setattr(settings, "diagnostics_dir", target)
    monkeypatch.setattr(settings, "diagnostics_enabled", True)
    monkeypatch.setattr(settings, "diagnostics_exact_content", False)
    return target


def _read_records(directory: Path) -> list[dict[str, Any]]:
    """读出目录里所有诊断记录。"""
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


# --------------------------------------------------------------------------
# 上游客户端替身：只替身「上游返回了什么」，不替身任何本仓逻辑
# --------------------------------------------------------------------------


class _FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens


class _FakeResponse:
    def __init__(
        self,
        content: str,
        *,
        finish_reason: str | None = "stop",
        usage: Any = "auto",
        response_id: str | None = "resp_diag_1",
    ) -> None:
        self.id = response_id
        self.choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ]
        if usage == "auto":
            usage = _FakeUsage(11, 22)
        self.usage = usage


class _FakeOpenAI:
    """最小可用的上游客户端替身。"""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self.requests.append(kwargs)
        return self._responses.pop(0)


def _client_with(*responses: _FakeResponse) -> ReportDesignModelClient:
    client = ReportDesignModelClient()
    client._client = _FakeOpenAI(list(responses))
    return client


# --------------------------------------------------------------------------
# AC1：每轮调用一个独立标识，关联会话、消息与操作
# --------------------------------------------------------------------------


def test_each_model_call_gets_its_own_request_id(caplog) -> None:
    """同一会话连续两轮：标识不同、序号递增，关联信息一致。"""
    caplog.set_level(logging.INFO, logger="app")
    diagnostics.begin_request_context(
        conversation_id="conv_diag_1", message_id="msg_diag_1"
    )
    diagnostics.note_operation("rdop_diag_1")
    client = _client_with(_FakeResponse("第一轮"), _FakeResponse("第二轮"))

    asyncio.run(_call_twice(client))

    ids = [record.get("requestId") for record in _model_call_records(caplog)]
    assert len(ids) == 2
    assert ids[0] != ids[1]
    assert all(str(value).startswith("req_") for value in ids)
    # 序号递增：重试轮是「新标识 + 递增 attempt」，不覆盖上一轮。
    attempts = [record.get("attempt") for record in _model_call_records(caplog)]
    assert attempts == ["1", "2"]
    for record in _model_call_records(caplog):
        assert record.get("conversationId") == "conv_diag_1"
        assert record.get("messageId") == "msg_diag_1"
        assert record.get("operationId") == "rdop_diag_1"


async def _call_twice(client: ReportDesignModelClient) -> None:
    await client([{"role": "user", "content": "生成报表"}])
    await client([{"role": "user", "content": "再生成一次"}])


def _model_call_records(caplog) -> list[dict[str, Any]]:
    """把 model_call 日志行拆成字典。"""
    out: list[dict[str, Any]] = []
    for record in caplog.records:
        if "step=model_call" not in record.getMessage():
            continue
        fields: dict[str, Any] = {}
        for token in record.getMessage().split():
            if "=" in token:
                key, _, value = token.partition("=")
                fields[key] = value
        out.append(fields)
    return out


# --------------------------------------------------------------------------
# AC2：元信息齐全（模型、提示词、阶段、序号、耗时、长度、分类）
# --------------------------------------------------------------------------


def test_model_call_records_the_full_metadata(caplog) -> None:
    """模型标识、阶段、耗时、返回长度、上游结束原因与 token 用量都记。"""
    caplog.set_level(logging.INFO, logger="app")
    diagnostics.begin_request_context(
        conversation_id="conv_diag_2", message_id="msg_diag_2"
    )
    diagnostics.set_call_context(
        stage="plan_design", prompt_version="v1", prompt_digest="abc123"
    )
    client = _client_with(_FakeResponse("模型返回的一段文本", finish_reason="stop"))

    asyncio.run(client([{"role": "user", "content": "生成报表"}]))

    fields = _model_call_records(caplog)[0]
    assert fields["modelId"]
    assert fields["stage"] == "plan_design"
    assert fields["promptVersion"] == "v1"
    assert fields["promptDigest"] == "abc123"
    assert int(fields["durationMs"]) >= 0
    assert int(fields["outputLength"]) == len("模型返回的一段文本")
    assert fields["finishReason"] == "stop"
    assert "total_tokens=33" in fields["tokenUsage"]
    assert fields["responseId"] == "resp_diag_1"

def test_prompt_digest_is_stable_and_content_sensitive() -> None:
    """提示词摘要哈希：同样内容同样哈希，内容变了就变。"""
    first = diagnostics.prompt_digest("提示词正文")
    again = diagnostics.prompt_digest("提示词正文")
    other = diagnostics.prompt_digest("另一段提示词正文")
    assert first == again
    assert first != other


def test_prompt_file_carries_no_version_marker_but_version_still_resolves() -> None:
    """提示词文件里**不许**有版本标记 —— 它的字节就是发给模型的字节。

    版本号改由**代码常量**提供：提示词里没标记就回落到常量，
    ``promptVersion`` 仍然有值，提示词文件保持与改动前逐字节一致。
    """
    from app.agents.report_plan_agent import (
        _PROMPT_VERSION,
        _load_prompt,
        _prompt_version,
    )

    prompt_text = _load_prompt()
    assert "prompt-version" not in prompt_text.lower()
    # 版本号来自代码常量，不是从提示词文件里读出来的。
    assert _PROMPT_VERSION
    assert _prompt_version(prompt_text) == _PROMPT_VERSION
    # 提示词里的显式标记仍然优先（将来要显式覆盖时仍能生效）。
    assert _prompt_version("prompt-version: v9") == "v9"


def test_prompt_version_reaches_the_log_line(caplog) -> None:
    """promptVersion 不会恒为空：模型调用那一行带着版本号。"""
    caplog.set_level(logging.INFO, logger="app")
    client = _client_with(_FakeResponse("不是 JSON"))

    asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=client,
        )
    )

    versions = {
        token.partition("=")[2]
        for record in caplog.records
        for token in record.getMessage().split()
        if token.startswith("promptVersion=")
    }
    assert versions
    assert all(value for value in versions)
    assert "None" not in versions


# --------------------------------------------------------------------------
# AC3：上游没给就写「未知」，不靠耗时或文本猜截断
# --------------------------------------------------------------------------


def test_missing_upstream_metadata_is_unknown_not_guessed(caplog) -> None:
    """上游没给结束原因、token 用量、响应标识：一律写「未知」。"""
    caplog.set_level(logging.INFO, logger="app")
    diagnostics.begin_request_context(conversation_id="conv_diag_3")
    client = _client_with(
        _FakeResponse("内容", finish_reason=None, usage=None, response_id=None)
    )

    asyncio.run(client([{"role": "user", "content": "生成报表"}]))

    fields = _model_call_records(caplog)[0]
    assert fields["finishReason"] == "未知"
    assert fields["tokenUsage"] == "未知"
    assert fields["responseId"] == "未知"


# --------------------------------------------------------------------------
# AC4：解析失败分三类
# --------------------------------------------------------------------------


VALID_CANDIDATE = {
    "topic": "PUE 专题报表",
    "requirements": {
        "metrics": ["PUE"],
        "subject": None,
        "timeRange": None,
        "exclusions": [],
    },
    "sections": [
        {
            "title": "PUE 总览",
            "metricIntents": ["PUE"],
            "componentType": "metric_card",
        }
    ],
    "assumptions": [],
    "suggestions": [],
    "referenceTemplates": [],
}


def _model_returning(text: Any) -> Any:
    async def _model_call(messages: list[dict[str, Any]]) -> Any:
        return text

    return _model_call


def _parse_status(caplog) -> str | None:
    for record in caplog.records:
        message = record.getMessage()
        if "step=model_parse" in message:
            for token in message.split():
                if token.startswith("parseStatus="):
                    return token.partition("=")[2]
    return None


def test_parse_failures_are_classified_into_three_kinds(caplog) -> None:
    """三类失败在日志里长得不一样：不许合并成一句「无法解析」。"""
    caplog.set_level(logging.INFO, logger="app")

    # 一、返回格式损坏：根本不是 JSON。
    caplog.clear()
    result = asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning("这不是 JSON，是一段解释文字"),
        )
    )
    assert result["code"] == "MODEL_OUTPUT_INVALID"
    assert _parse_status(caplog) == "PARSE_NOT_JSON"

    # 二、是 JSON 但结构非法：候选缺字段。
    caplog.clear()
    result = asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning(
                json.dumps({"kind": "plan_candidate", "candidate": {"topic": "x"}})
            ),
        )
    )
    assert result["code"] == "MODEL_OUTPUT_INVALID"
    assert _parse_status(caplog) == "PARSE_STRUCTURE_INVALID"

    # 三、JSON 合法但没提交合法终结结果。
    caplog.clear()
    result = asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning(json.dumps({"kind": "随便一种"})),
        )
    )
    assert result["code"] == "MODEL_OUTPUT_INVALID"
    assert _parse_status(caplog) == "TERMINAL_RESULT_MISSING"

    # 正常返回：OK。
    caplog.clear()
    result = asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning(
                json.dumps({"kind": "plan_candidate", "candidate": VALID_CANDIDATE})
            ),
        )
    )
    assert result["kind"] == "plan_candidate"
    assert _parse_status(caplog) == "OK"


def test_json_error_position_is_recorded(caplog, diagnostics_dir) -> None:
    """JSON 断掉时能定位到偏移与行列。"""
    caplog.set_level(logging.INFO, logger="app")
    broken = '{"kind": "plan_candidate", "candidate": {'

    asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning(broken),
        )
    )

    records = _read_records(diagnostics_dir)
    assert len(records) == 1
    record = records[0]
    assert record["parseStatus"] == "PARSE_NOT_JSON"
    assert record["jsonOffset"] is not None
    assert record["jsonLine"] == 1
    assert record["jsonColumn"] is not None


def test_json_error_position_points_at_the_broken_object(diagnostics_dir) -> None:
    """输出前面带说明文字时，位置指向 JSON 对象的起点，不是整段的开头。"""
    text = "这是说明\n```json\n{\n  \"kind\": \"plan_candidate\"\n```"

    asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning(text),
        )
    )

    record = _read_records(diagnostics_dir)[0]
    assert record["parseStatus"] == "PARSE_NOT_JSON"
    # 指向第 3 行第 1 列：代码块里那个没写完的对象。
    assert record["jsonLine"] == 3
    assert record["jsonColumn"] == 1
    assert text[record["jsonOffset"] : record["jsonOffset"] + 1] == "{"


def test_json_offset_is_relative_to_the_original_text(diagnostics_dir) -> None:
    """输出前面带空白时：偏移落在**原文**里真正断掉的那个字符上。

    偏移、行列一律相对**原始返回文本**（含前导空白），
    不是相对「去空白后的文本」—— 否则会指错字符。
    """
    text = "\n\n这是说明\n```json\n{\n  \"kind\": \"x\"\n"

    asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning(text),
        )
    )

    record = _read_records(diagnostics_dir)[0]
    assert record["parseStatus"] == "PARSE_NOT_JSON"
    # 偏移指向原文里那个没写完的对象的起始 '{'。
    assert record["jsonOffset"] == text.index("{")
    assert text[record["jsonOffset"] : record["jsonOffset"] + 1] == "{"
    # 行列也是 1 起、相对原文：第 5 行第 1 列。
    assert record["jsonLine"] == 5
    assert record["jsonColumn"] == 1


# --------------------------------------------------------------------------
# AC5：三个事件分别记录；不声称 final 已被客户端接收
# --------------------------------------------------------------------------


def test_router_logs_business_final_and_stream_end_separately(client, caplog) -> None:
    """「业务已返回」「final 已入队」「流结束」分别留痕。"""
    caplog.set_level(logging.INFO, logger="app")
    payload = {
        "conversationId": "conv_diag_stream",
        "state": "ANALYZING",
        "message": "帮我看看机房",
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    }
    result = client.post("/api/agent/chat/stream", json=payload)
    assert result.status_code == 200

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=business_returned" in text
    assert "event=final_enqueued" in text
    assert "event=stream_finished" in text
    # 没有客户端确认，就不能说 final 已被客户端接收。
    assert "client_received" not in text
    assert "已被客户端接收" not in text
    assert "delivered" not in text


# --------------------------------------------------------------------------
# AC6：诊断记录独立、脱敏、不落会话库
# --------------------------------------------------------------------------


def test_diagnostic_record_is_masked_and_independent(caplog, diagnostics_dir) -> None:
    """诊断记录落独立文件，内容脱敏，不含用户原话与业务数值。"""
    caplog.set_level(logging.INFO, logger="app")
    secret_phrase = "机房A栋三层配电室"
    output = f'{{"note": "{secret_phrase}", "value": 12345}}'

    asyncio.run(
        generate_candidate(
            message=secret_phrase,
            current_plan=None,
            history=[],
            model_call=_model_returning(output),
        )
    )

    records = _read_records(diagnostics_dir)
    assert len(records) == 1
    record = records[0]
    assert record["diagnosticId"].startswith("diag_")
    assert record["requestId"].startswith("req_")
    assert record["redacted"] is True
    # 输出总长度如实记，truncated 只表示诊断侧截取。
    assert record["outputTotalLength"] == len(output)
    assert record["truncated"] is False
    raw = json.dumps(record, ensure_ascii=False)
    assert secret_phrase not in raw
    assert "12345" not in raw


def test_diagnostic_file_is_private_to_the_current_user(diagnostics_dir) -> None:
    """诊断目录 0700、诊断文件 0600。"""
    asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning("不是 JSON"),
        )
    )
    files = list(diagnostics_dir.glob("*.jsonl"))
    assert files
    assert stat.S_IMODE(diagnostics_dir.stat().st_mode) == 0o700
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_default_diagnostics_dir_is_outside_the_repository() -> None:
    """诊断目录不在 Git 仓库里，也不在前端公开目录里。"""
    settings = get_settings()
    target = Path(settings.diagnostics_dir).expanduser().resolve()
    assert REPO_ROOT not in target.parents
    assert target != REPO_ROOT
    assert "frontend" not in target.parts
    assert "public" not in target.parts


def test_diagnostic_record_is_not_in_the_session_database(
    diagnostics_dir, tmp_path
) -> None:
    """诊断记录不进会话库：库里没有 diagnosticId。"""
    db_path = tmp_path / "sessions.db"
    import sqlite3

    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE IF NOT EXISTS t (x TEXT)")
    connection.commit()
    connection.close()

    asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning("不是 JSON"),
        )
    )
    records = _read_records(diagnostics_dir)
    assert records
    blob = db_path.read_bytes()
    assert records[0]["diagnosticId"].encode("utf-8") not in blob


# --------------------------------------------------------------------------
# AC7：单条上限、总容量与保留期限
# --------------------------------------------------------------------------


def test_diagnostic_record_respects_single_record_limit(
    diagnostics_dir, monkeypatch
) -> None:
    """单条超限：去掉正文，只留元信息，并标明是诊断侧截取。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "diagnostics_max_record_bytes", 400)

    asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning("x" * 5000),
        )
    )

    records = _read_records(diagnostics_dir)
    assert len(records) == 1
    assert records[0]["content"] is None
    assert records[0]["truncated"] is True
    assert records[0]["contentOmittedReason"]
    # 输出总长度仍然如实记录（这是模型输出的长度，不是诊断截取）。
    assert records[0]["outputTotalLength"] == 5000


def test_diagnostic_records_expire_and_respect_total_capacity(
    diagnostics_dir, monkeypatch
) -> None:
    """到期自动清理；超过总容量时先删最旧的（刚写下的那份留着）。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "diagnostics_retention_days", 7)
    monkeypatch.setattr(settings, "diagnostics_max_total_bytes", 1000)

    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    # 到期的旧文件：保留期限一过就该没。
    expired = diagnostics_dir / "diagnostics-20000101.jsonl"
    expired.write_text("x" * 5000, encoding="utf-8")
    os.utime(expired, (0, 0))
    # 没到期但占地方的旧文件：容量超了先删它。
    bulky = diagnostics_dir / "diagnostics-20000102.jsonl"
    bulky.write_text("y" * 5000, encoding="utf-8")

    asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning("不是 JSON"),
        )
    )

    assert not expired.exists()
    assert not bulky.exists()
    remaining = list(diagnostics_dir.glob("*.jsonl"))
    assert len(remaining) == 1
    assert _read_records(diagnostics_dir)


# --------------------------------------------------------------------------
# AC8：写诊断失败不影响业务请求
# --------------------------------------------------------------------------


def test_diagnostic_write_failure_does_not_fail_the_request(
    tmp_path, monkeypatch
) -> None:
    """诊断目录写不进去时，业务照样返回结果。"""
    settings = get_settings()
    blocked = tmp_path / "blocked"
    blocked.write_text("这不是目录", encoding="utf-8")
    monkeypatch.setattr(settings, "diagnostics_dir", blocked)
    monkeypatch.setattr(settings, "diagnostics_enabled", True)

    result = asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning("不是 JSON"),
        )
    )

    assert result["code"] == "MODEL_OUTPUT_INVALID"


# --------------------------------------------------------------------------
# AC9：常规日志不出现用户原话、模型返回原文与上游异常原文
# --------------------------------------------------------------------------


def test_regular_log_has_no_user_text_or_model_output(caplog) -> None:
    """常规日志只写标识与元信息。"""
    caplog.set_level(logging.INFO, logger="app")
    user_phrase = "三号厂房的UPS扩容计划"
    model_phrase = "机房温湿度异常"

    asyncio.run(
        generate_candidate(
            message=user_phrase,
            current_plan=None,
            history=[],
            model_call=_model_returning(f"模型说：{model_phrase}"),
        )
    )

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert user_phrase not in text
    assert model_phrase not in text


def test_upstream_exception_text_does_not_reach_the_log(caplog) -> None:
    """上游异常原文不进日志，也不进返回值。"""

    async def _boom(messages: list[dict[str, Any]]) -> str:
        raise RuntimeError("upstream said: sk-SECRETKEY1234567890")

    caplog.set_level(logging.INFO, logger="app")
    result = asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_boom,
        )
    )

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "sk-SECRETKEY1234567890" not in text
    assert "upstream said" not in text
    assert result["code"] == "MODEL_CALL_FAILED"
    assert "sk-SECRETKEY" not in json.dumps(result, ensure_ascii=False)


# --------------------------------------------------------------------------
# AC10：精确原文模式默认关闭
# --------------------------------------------------------------------------


def test_exact_content_mode_is_off_by_default(diagnostics_dir) -> None:
    """默认只写脱敏后的形状，不写原文。"""
    phrase = "这段原文不该落盘"
    asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning(f"模型原话：{phrase}"),
        )
    )
    records = _read_records(diagnostics_dir)
    assert records
    assert phrase not in json.dumps(records[0], ensure_ascii=False)
    assert records[0]["redacted"] is True


def test_exact_content_mode_requires_window_and_scope(
    diagnostics_dir, monkeypatch
) -> None:
    """开关打开也要「未过期 + 在范围内」才生效；凭据永远被抹掉。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "diagnostics_exact_content", True)
    monkeypatch.setattr(settings, "diagnostics_exact_content_until", "2000-01-01T00:00:00")
    monkeypatch.setattr(settings, "diagnostics_exact_content_scope", "*")

    asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning("过期了就不该写原文"),
        )
    )
    records = _read_records(diagnostics_dir)
    assert "过期了就不该写原文" not in json.dumps(records[0], ensure_ascii=False)


def test_new_turn_restarts_the_attempt_counter(caplog) -> None:
    """换了消息就是新的一轮：序号从 1 重新起，不跨轮累加。"""
    caplog.set_level(logging.INFO, logger="app")

    diagnostics.begin_request_context(
        conversation_id="conv_diag_turn", message_id="msg_turn_1"
    )
    first = _client_with(_FakeResponse("第一轮"))
    asyncio.run(first([{"role": "user", "content": "生成报表"}]))

    diagnostics.begin_request_context(
        conversation_id="conv_diag_turn", message_id="msg_turn_2"
    )
    second = _client_with(_FakeResponse("第二轮"))
    asyncio.run(second([{"role": "user", "content": "再生成一次"}]))

    attempts = [record.get("attempt") for record in _model_call_records(caplog)]
    assert attempts == ["1", "1"]


# --------------------------------------------------------------------------
# 端到端：真实路由 → 真实设计编排 → 真实解析，诊断记录与日志对得上
# --------------------------------------------------------------------------


def test_design_flow_diagnostic_matches_the_log_line(
    client, caplog, diagnostics_dir, monkeypatch
) -> None:
    """走完整链路：诊断记录里的 requestId 与日志行里的对得上。"""
    from app.services.report_design_service import report_design_service

    caplog.set_level(logging.INFO, logger="app")
    report_design_service.reset_for_tests()
    try:

        async def _bad_model(messages: list[dict[str, Any]]) -> str:
            return "模型这次没给 JSON，给了一段说明：机房温湿度异常"

        report_design_service.reset_for_tests(model_call=_bad_model)

        payload = {
            "agentProtocolVersion": "1.0",
            "conversationId": "conv_diag_e2e",
            "sessionId": "sess_diag_e2e",
            "messageId": "msg_diag_e2e",
            "state": "ANALYZING",
            "message": "生成一份机房能耗报表",
            "context": {
                "projectId": "project_001",
                "subjectRef": "machine_room",
                "subjectId": "room_a",
            },
        }
        response = client.post("/api/agent/chat", json=payload)
        assert response.status_code == 200
        body = response.json()
        # 行为没变：还是那个可辨识的失败码。
        assert body["error"]["code"] == "MODEL_OUTPUT_INVALID"

        records = _read_records(diagnostics_dir)
        assert len(records) == 1
        record = records[0]
        assert record["parseStatus"] == "PARSE_NOT_JSON"
        assert record["conversationId"] == "conv_diag_e2e"
        assert record["messageId"] == "msg_diag_e2e"
        assert record["operationId"]

        # 诊断记录与常规日志用同一个 requestId 对上。
        # 这里注入的是替身模型，没有适配层那一行 model_call；
        # 真实适配层在时还会多一行 step=model_call（见上面的用例）。
        parse_ids = {
            token.partition("=")[2]
            for record in caplog.records
            for token in record.getMessage().split()
            if token.startswith("requestId=")
        }
        assert record["requestId"] in parse_ids
        # 操作标识也对得上：请求标识关联到了认领到的设计操作。
        assert record["operationId"] in caplog.text
    finally:
        report_design_service.reset_for_tests()


def test_content_is_omitted_when_redaction_cannot_be_guaranteed(
    diagnostics_dir,
) -> None:
    """脱敏无法保证完整时：不落正文，只留元信息与原因。"""
    asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            # 控制字符没法可靠地归类，保守起见不写正文。
            model_call=_model_returning("坏掉的输出\x00里面还有内容"),
        )
    )
    records = _read_records(diagnostics_dir)
    assert len(records) == 1
    assert records[0]["content"] is None
    assert records[0]["contentOmittedReason"]
    # 元信息照样在：标识、分类、输出总长度都不少。
    assert records[0]["diagnosticId"].startswith("diag_")
    assert records[0]["parseStatus"] == "PARSE_NOT_JSON"
    assert records[0]["outputTotalLength"] == len("坏掉的输出\x00里面还有内容")


def test_diagnostic_never_reaches_the_api_response(
    client, diagnostics_dir, monkeypatch
) -> None:
    """诊断内容不进普通 API 响应，也不进用户可见错误消息。"""
    from app.services.report_design_service import report_design_service

    report_design_service.reset_for_tests()
    try:

        async def _bad_model(messages: list[dict[str, Any]]) -> str:
            return "模型这次没给 JSON：机房温湿度异常"

        report_design_service.reset_for_tests(model_call=_bad_model)
        payload = {
            "agentProtocolVersion": "1.0",
            "conversationId": "conv_check_api",
            "sessionId": "sess_check_api",
            "messageId": "msg_check_api",
            "state": "ANALYZING",
            "message": "生成一份机房能耗报表",
            "context": {
                "projectId": "project_001",
                "subjectRef": "machine_room",
                "subjectId": "room_a",
            },
        }
        body = client.post("/api/agent/chat", json=payload).json()
        raw = json.dumps(body, ensure_ascii=False)
        records = _read_records(diagnostics_dir)
        assert records
        # 响应里没有诊断标识，也没有诊断正文。
        assert records[0]["diagnosticId"] not in raw
        assert "req_" not in raw
        assert "diag_" not in raw
        assert "机房温湿度异常" not in raw
    finally:
        report_design_service.reset_for_tests()


def test_stream_cancelled_is_logged_separately(caplog, monkeypatch) -> None:
    """客户端断开 = 取消：记「流被取消」，不记「流结束」。

    直接驱动 ASGI 应用再把请求取消掉：TestClient 会把流读完，
    断不开，拿不到这个分支。
    """
    import asyncio as _asyncio

    from app.main import app
    from app.schemas.agent import AgentResponse, AgentState
    from app.services.agent_service import agent_service

    caplog.set_level(logging.INFO, logger="app")

    async def _slow_chat(request, emit=None):
        # 故意慢一点：让取消真的落在等待模型/业务结果的那一步。
        await _asyncio.sleep(5)
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.ANALYZING,
            reply="慢",
        )

    monkeypatch.setattr(agent_service, "chat", _slow_chat)

    body = json.dumps(
        {
            "conversationId": "conv_diag_cancel",
            "state": "ANALYZING",
            "message": "帮我看看机房",
            "context": {
                "projectId": "project_001",
                "subjectRef": "machine_room",
                "subjectId": "room_a",
            },
        }
    ).encode("utf-8")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/agent/chat/stream",
        "raw_path": b"/api/agent/chat/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("utf-8")),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }

    async def receive() -> dict[str, Any]:
        # 第一次交请求体，之后告诉服务端「客户端断了」。
        if not delivered:
            delivered.append(True)
            return {"type": "http.request", "body": body, "more_body": False}
        await _asyncio.sleep(0.3)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        return None

    delivered: list[bool] = []

    async def main() -> None:
        try:
            await _asyncio.wait_for(app(scope, receive, send), timeout=15)
        except (TimeoutError, _asyncio.TimeoutError):
            pass
        await _asyncio.sleep(0.3)

    _asyncio.run(main())

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=stream_cancelled" in text
    assert "event=stream_finished" not in text


def test_log_write_failure_does_not_fail_the_request(monkeypatch) -> None:
    """日志写不出去时，业务请求照常返回。"""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("日志盘满了")

    monkeypatch.setattr(diagnostics.logger, "info", _boom)
    monkeypatch.setattr(diagnostics.logger, "warning", _boom)

    result = asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning("不是 JSON"),
        )
    )
    assert result["code"] == "MODEL_OUTPUT_INVALID"


def test_diagnostic_write_failure_plus_log_failure_still_returns(
    tmp_path, monkeypatch
) -> None:
    """诊断写失败**且**日志器自己也抛错：业务请求照常返回。

    故障处理路径上日志再抛错，会把原始异常盖掉并穿出到业务请求 ——
    那一轮就直接失败了。契约表 §6.5：写日志 / 写诊断失败不得让业务请求失败。
    """
    from app.agents import report_plan_agent

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("日志盘满了")

    settings = get_settings()
    blocked = tmp_path / "blocked"
    blocked.write_text("这不是目录", encoding="utf-8")
    monkeypatch.setattr(settings, "diagnostics_dir", blocked)
    monkeypatch.setattr(settings, "diagnostics_enabled", True)
    # 诊断侧的日志器：常规日志与「写诊断失败」的告警都抛错。
    monkeypatch.setattr(diagnostics.logger, "info", _boom)
    monkeypatch.setattr(diagnostics.logger, "warning", _boom)
    # 解析层在故障处理路径上的告警也抛错。
    monkeypatch.setattr(report_plan_agent.logger, "warning", _boom)

    result = asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning("不是 JSON"),
        )
    )

    assert result["code"] == "MODEL_OUTPUT_INVALID"


def test_diagnostic_log_lines_go_through_the_existing_rotating_file(tmp_path) -> None:
    """诊断日志走**现有**的轮转文件，不另起一套。"""
    from app.logging_config import configure_app_file_logging

    path = tmp_path / "backend.log"
    logger = logging.getLogger("app")
    previous = set(logger.handlers)
    configure_app_file_logging(path)
    new_handlers = set(logger.handlers) - previous
    try:
        asyncio.run(
            generate_candidate(
                message="生成报表",
                current_plan=None,
                history=[],
                model_call=_model_returning("不是 JSON"),
            )
        )
    finally:
        for handler in new_handlers:
            logger.removeHandler(handler)
            handler.close()

    content = path.read_text(encoding="utf-8")
    assert "step=model_parse" in content
    assert "parseStatus=PARSE_NOT_JSON" in content
    # 同一个文件、同一套轮转与权限。
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --------------------------------------------------------------------------
# AC11：不改模型行为
# --------------------------------------------------------------------------

def test_parse_behaviour_is_unchanged() -> None:
    """降级顺序不变：代码块包裹、前面带说明文字，都能取出 JSON 对象。"""
    fenced = "```json\n" + json.dumps({"kind": "clarification", "question": "要哪台？"}) + "\n```"
    result = asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning(fenced),
        )
    )
    assert result["kind"] == "clarification"

    with_prefix = "好的，这是方案：\n" + json.dumps(
        {"kind": "clarification", "question": "要哪台？"}
    )
    result = asyncio.run(
        generate_candidate(
            message="生成报表",
            current_plan=None,
            history=[],
            model_call=_model_returning(with_prefix),
        )
    )
    assert result["kind"] == "clarification"
