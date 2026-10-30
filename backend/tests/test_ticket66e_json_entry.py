"""工单 #66E：JSON 入口两处缺口（后端）。

覆盖两件事：

1. **缺口一（🔴）**：设计流程那一轮，JSON 输入的**来源 / 引用标记**被丢掉
   了（``_handle_json_input_turn`` 里 ``if design_turn: return None``），
   前端在收到方案的那一轮拿不到「用户 JSON 模式」标记。
   修法：把标记（``accepted`` + 引用，**不含原始数组**）放进**既有的**
   ``debug`` 对象 —— 不新增顶层响应字段、不改协议版本；
   不是 JSON 输入的普通轮次，响应里**不多出**这个键。

2. **缺口二（🟠）**：``[]`` / ``[1,2,3]`` 这类**没引号**的数据形状进不了
   JSON 输入轮 —— 用户粘了数据，服务端却当普通需求处理，给了一份**模拟
   示例**方案，界面一句提示都没有。
   修法：收窄「像不像 JSON 数据」的判据（**不放宽**含引号的老判据），
   让 ``JSON_ARRAY_EMPTY`` / ``JSON_ROW_NOT_OBJECT`` / ``JSON_INPUT_EMPTY``
   三个失败码在聊天入口够得着，并给出可修正的提示。

接口层用真实 ``POST /api/agent/chat`` + 真实服务 + 独立临时 SQLite，
只替换外部模型调用（``tests/support/acceptance_harness.py``）。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.services import routing_rules
from tests.support.acceptance_harness import (
    ScriptedModel,
    acceptance_harness,
    plan_candidate,
)


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def _rows(count: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "ts": f"2024-01-0{index + 1}",
            "device": f"机柜{index + 1}",
            "energy": float(index + 1),
        }
        for index in range(count)
    ]


def _json_text(rows: list[dict[str, Any]] | None = None) -> str:
    return json.dumps(rows if rows is not None else _rows(), ensure_ascii=False)


def _table_sections() -> list[dict[str, Any]]:
    """一份带明细表板块的方案候选（栏目名与输入字段名对齐）。"""
    return [
        {
            "id": "section_total",
            "title": "总耗电概览",
            "metricIntents": ["总耗电"],
            "componentType": "metric_card",
            "displayIntent": "total",
        },
        {
            "id": "section_detail",
            "title": "能耗明细",
            "metricIntents": ["能耗明细"],
            "componentType": "table",
            "displayIntent": "records",
            "columns": [
                {"id": "ts", "label": "日期", "fieldRef": "ts"},
                {"id": "device", "label": "设备", "fieldRef": "device"},
                {"id": "energy", "label": "耗电", "fieldRef": "energy"},
            ],
        },
    ]


def _stored_input_rows(harness: Any) -> list[Any]:
    """输入记录表里的全部行。

    表是**按需建的**：校验没过时压根不建表 —— 那也是「什么都没存」。
    """
    exists = harness.repository.conn.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master"
        " WHERE type='table' AND name='report_design_json_inputs'"
    ).fetchone()
    if not exists or not exists["n"]:
        return []
    return harness.repository.conn.execute(
        "SELECT * FROM report_design_json_inputs"
    ).fetchall()


def _plan(payload: dict[str, Any]) -> Any:
    """这一轮出没出方案。"""
    return (payload.get("domainObjects") or {}).get("reportPlan")


def _marker(payload: dict[str, Any]) -> dict[str, Any]:
    """响应 ``debug`` 里的 JSON 来源标记。"""
    return (payload.get("debug") or {}).get("jsonInput") or {}


def _error_code(payload: dict[str, Any]) -> str | None:
    return (payload.get("error") or {}).get("code")


# --------------------------------------------------------------------------
# 缺口一 —— 设计轮别把来源标记丢了
# --------------------------------------------------------------------------


def test_design_turn_carries_the_json_source_marker() -> None:
    """贴 JSON 出的那一版方案，响应里带「用户 JSON 模式」标记。

    改之前：设计流程那一轮把标记丢了，前端看不出数据是用户给的。
    """
    model = ScriptedModel(plan_candidate(sections=_table_sections()))
    with acceptance_harness(model=model) as harness:
        payload = harness.send(f"这是昨天的数据 {_json_text()}")
        assert _plan(payload) is not None, "这一轮应当出方案"

        marker = _marker(payload)
        assert marker.get("accepted") is True
        assert marker.get("inputId")
        assert marker.get("inputVersion") == 1
        assert marker.get("recordCount") == 3
        assert marker.get("fieldCount") == 3
        assert marker.get("byteSize")


def test_design_turn_marker_carries_reference_but_not_the_raw_array() -> None:
    """标记里只有引用，没有原始数组；响应里也搜不到原始数据。"""
    raw = _json_text()
    model = ScriptedModel(plan_candidate(sections=_table_sections()))
    with acceptance_harness(model=model) as harness:
        payload = harness.send(f"这是昨天的数据 {raw}")
        blob = json.dumps(payload, ensure_ascii=False)
        assert raw not in blob
        assert "机柜1" not in blob
        assert "机柜1" not in json.dumps(_marker(payload), ensure_ascii=False)
        # 原始数组只落在输入记录表里（契约表 §4.3）。
        rows = _stored_input_rows(harness)
        assert len(rows) == 1
        assert "机柜1" in rows[0]["raw_json"]


def test_ordinary_turn_has_no_json_marker() -> None:
    """不是 JSON 输入的轮次，响应里不多出 ``jsonInput`` 这个键。"""
    model = ScriptedModel(plan_candidate(sections=_table_sections()))
    with acceptance_harness(model=model) as harness:
        payload = harness.send("请给我一份 A 机房上周的能耗报表")
        assert _plan(payload) is not None
        assert "jsonInput" not in (payload.get("debug") or {})
        assert _stored_input_rows(harness) == []


# --------------------------------------------------------------------------
# 缺口二 —— 三个失败码在聊天入口够得着
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "code", "hint"),
    [
        (
            "这是数据 []",
            "JSON_ARRAY_EMPTY",
            "记录数组是空的，没有可以展示的数据。",
        ),
        (
            "这是数据 [1,2,3]",
            "JSON_ROW_NOT_OBJECT",
            "第 1 条记录不是对象。每条记录都应是「字段名: 值」这样的一行。",
        ),
        (
            "这是我的数据\n数组位置: data.records",
            "JSON_INPUT_EMPTY",
            "没有收到 JSON 数据，请把记录数组贴进来再试。",
        ),
    ],
)
def test_json_failures_reach_the_user_with_a_fixable_hint(
    message: str, code: str, hint: str
) -> None:
    """失败码到了用户面前：说清哪里不对、怎么改。

    改之前：这三段根本没被当成 JSON 输入轮 —— 用户粘了数据，
    却拿到一份**模拟示例**方案，界面一句提示都没有。
    """
    model = ScriptedModel(plan_candidate(sections=_table_sections()))
    with acceptance_harness(model=model) as harness:
        payload = harness.send(message)
        assert _error_code(payload) == code
        # 文案用契约表 §4.3 登记的原文。
        assert (payload.get("error") or {}).get("message") == hint
        assert hint in (payload.get("reply") or "")
        # 不产方案、不落库、不调模型。
        assert _plan(payload) is None
        assert _stored_input_rows(harness) == []
        assert not model.calls, "校验没过不该调用模型"


def test_rejected_json_is_not_kept_in_chat_messages() -> None:
    """没过校验的原始数据不进聊天消息。"""
    model = ScriptedModel(plan_candidate(sections=_table_sections()))
    with acceptance_harness(model=model) as harness:
        harness.send("这是数据 [1,2,3]")
        for message in harness.repository.list_messages(harness.session_id):
            assert "[1,2,3]" not in (message.content or "")


# --------------------------------------------------------------------------
# 缺口二 —— 普通中文句子绝不能被误判
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "请给我一份 A 机房上周的能耗报表",
        "帮我看看这个 [重要] 的部分",
        "这个图是什么意思",
        "温度是 25 度",
    ],
)
def test_ordinary_chinese_sentence_is_not_taken_as_json(message: str) -> None:
    """普通中文句子照旧当普通需求处理，不报 JSON 错。"""
    model = ScriptedModel(plan_candidate(sections=_table_sections()))
    with acceptance_harness(model=model) as harness:
        payload = harness.send(message)
        assert not _error_code(payload)
        assert "jsonInput" not in (payload.get("debug") or {})
        assert _stored_input_rows(harness) == []


def test_data_array_detection_only_accepts_numeric_arrays() -> None:
    """「没引号但明显是数据」的判据：只认数字数组，且不猜。"""
    assert routing_rules.detect_data_array_payload("这是数据 []") == "[]"
    assert routing_rules.detect_data_array_payload("这是数据 [1,2,3]") == "[1,2,3]"
    assert routing_rules.detect_data_array_payload("[1, 2, 3]") == "[1, 2, 3]"
    # 不是数据的形状不认。
    assert routing_rules.detect_data_array_payload("帮我看看这个 [重要] 事项") is None
    assert routing_rules.detect_data_array_payload("请给我一份报表") is None
    # 两处候选：不替用户挑（契约表 §4.3 同一条道理）。
    assert routing_rules.detect_data_array_payload("我要 [1,2,3] 和 [4,5]") is None
    # 不在末尾：后面还有话，不认。
    assert routing_rules.detect_data_array_payload("数组在这里 [1,2,3] 谢谢") is None


def test_json_input_intent_needs_the_registered_marker() -> None:
    """「用户想给 JSON」的信号只认契约表 §4.3 登记的写法。"""
    assert routing_rules.has_json_input_intent("数组位置: data.records") is True
    assert routing_rules.has_json_input_intent("@arrayPath data.records") is True
    assert routing_rules.has_json_input_intent("数组位置") is True
    assert routing_rules.has_json_input_intent("请给我一份报表") is False
    assert routing_rules.has_json_input_intent("我把数组位置改成了 data.records") is False
    # 问句不是标记：后面的中文不是路径。
    assert routing_rules.has_json_input_intent("数组位置怎么填") is False


# --------------------------------------------------------------------------
# 缺口二 —— 含引号的正常 JSON 识别路径逐字不变
# --------------------------------------------------------------------------


def test_quoted_json_detection_is_unchanged() -> None:
    """含引号的老判据逐字不变：只认含引号的片段。"""
    raw = _json_text()
    assert routing_rules.detect_json_payload(f"这是数据 {raw}") == raw
    assert routing_rules.detect_json_payload('这是数据 [{"a":1},]') == '[{"a":1},]'
    assert routing_rules.detect_json_payload("这是数据 []") is None
    assert routing_rules.detect_json_payload("这是数据 [1,2,3]") is None
    assert routing_rules.detect_json_payload("请给我一份报表") is None


def test_quoted_json_still_flows_through_the_whole_way() -> None:
    """正常的记录数组：照样存一份、出方案、带来源标记。"""
    model = ScriptedModel(plan_candidate(sections=_table_sections()))
    with acceptance_harness(model=model) as harness:
        payload = harness.send(f"这是昨天的数据 {_json_text()}")
        assert _plan(payload) is not None
        rows = _stored_input_rows(harness)
        assert len(rows) == 1
        assert rows[0]["record_count"] == 3
        assert _marker(payload).get("accepted") is True


def test_quoted_json_syntax_error_is_unchanged() -> None:
    """含引号但语法错的：还是那个失败码、还是不留数据。"""
    model = ScriptedModel(plan_candidate(sections=_table_sections()))
    with acceptance_harness(model=model) as harness:
        payload = harness.send('这是数据 [{"a":1},]')
        assert _error_code(payload) == "JSON_SYNTAX_INVALID"
        assert _plan(payload) is None
        assert _stored_input_rows(harness) == []
