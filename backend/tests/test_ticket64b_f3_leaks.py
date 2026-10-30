"""工单 #64 的 F3 与 F3bis：来源模式口径 + 上游异常原文不进用户可见回复。

覆盖两个未完成项：

- **F3**：用户 JSON 模式（``sourceMode = "user_json"``）下，回复不再说
  「右侧这份报表用系统生成的**模拟业务数据**渲染」—— 那是事实性错误：
  右侧画的是**用户自己提供的那份数据**，不是造出来的模拟数据。
  模拟示例模式（``simulated``）的既有文案**逐字未改**。
- **F3bis**：上游异常原文不进用户可见的回复（#61 的遗留，工单 #64 收口）。
  用户拿到的是**能行动的提示**，不是「类型名 + 栈」；可定位的原因
  （原因类型 + 调用栈 + 错误码）留在日志与操作记录里。

接口层走真实 ``POST /api/agent/chat`` + 真实编排 + 真实 SQLite（独立临时库），
只替换外部模型调用与**被测的那一处上游**（``tests/support/acceptance_harness.py``）。

敏感串在**运行时**拼出来，不写在源码字面量里：失败日志会带调用栈，
调用栈会打印源码行 —— 源码里写死的敏感串会跟着栈一起出现在日志里，
测出来的就不是「异常原文没泄漏」了。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.services import report_design_service as design_module
from app.services.report_template_service import build_template_design
from app.storage.report_design_repository import DesignPlanInvalidError
from app.storage.report_design_sqlite import SqliteReportDesignRepository
from tests.support.acceptance_harness import (
    ScriptedModel,
    acceptance_harness,
    plan_candidate,
)


#: 模拟用户数据里的一个可识别标记（运行时拼，源码里没有它的字面量）。
USER_DATA_MARKER = "USER_" + "DATA_8F3A2C"

#: 模拟模式下的既有文案（ADR 0004）。**逐字**照抄，用来证明没被改。
SIMULATED_RENDER_SENTENCE = (
    "右侧这份报表用系统生成的**模拟业务数据**渲染，是用来核对版式的"
    "示意，接入真实数据后会自动替换。"
)
SIMULATED_PLAN_SENTENCE = (
    "请在左侧对话区查看完整方案；确认后我会用系统生成的模拟业务数据"
    "把这份报表渲染出来。"
)


def _rows(count: int = 2) -> list[dict[str, Any]]:
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


def _boom(message: str, exc_type: type[BaseException] = ValueError):
    """造一个「上游抛异常」的替身：异常消息带**用户数据标记**。

    源码里只有变量名，没有标记本身 —— 调用栈打印源码行时不会带出去。
    """

    def _raise(*args: Any, **kwargs: Any):
        raise exc_type(message)

    return _raise


def _operation_rows(harness: Any) -> list[Any]:
    """这个会话里的操作记录（排查侧能查到失败原因的地方）。"""
    return harness.repository.conn.execute(
        "SELECT error_code, error_message FROM report_design_operations"
        " WHERE session_id = ?",
        (harness.session_id,),
    ).fetchall()


def _assert_no_leak(payload: dict[str, Any], caplog: Any) -> None:
    """用户可见的两处（正文与错误消息）都不带异常原文。"""
    reply = payload.get("reply") or ""
    error = payload.get("error") or {}
    message = error.get("message") or ""
    assert USER_DATA_MARKER not in reply, reply
    assert USER_DATA_MARKER not in message, message
    # 日志里能定位到**原因类型**，但没有异常原文。
    assert "errorType=" in caplog.text, caplog.text
    assert USER_DATA_MARKER not in caplog.text, caplog.text


# --------------------------------------------------------------------------
# F3 —— 来源模式口径
# --------------------------------------------------------------------------


def test_user_json_mode_reply_says_user_provided_data_not_simulated() -> None:
    """用户 JSON 模式：回复说「用户提供的数据，未经业务核验」，不说模拟数据。

    前提也要是真的：这一轮确实走的是用户 JSON 模式（定义里记录的
    ``sourceMode`` 与载荷里的 ``dataSource`` 都对得上）。
    """
    model = ScriptedModel(plan_candidate(sections=_table_sections()))
    with acceptance_harness(model=model) as harness:
        plan_payload = harness.send(f"这是昨天的数据 {_json_text()}")
        plan = plan_payload["domainObjects"]["reportPlan"]
        confirmed = harness.confirm_plan(plan)

    # 前提：这一轮确实是用户 JSON 模式（不是模拟模式）。
    definition = confirmed["domainObjects"]["reportDefinitionDraft"]
    assert definition["metadata"]["sourceMode"] == "user_json"
    preview = confirmed.get("reportPreviewPayload") or {}
    sources = {
        (region.get("payload") or {}).get("dataSource")
        for region in preview.get("regions") or []
    }
    assert "user_json" in sources

    # 方案那一轮与确认那一轮，两处回复都要按用户 JSON 口径说。
    for reply in (plan_payload["reply"], confirmed["reply"]):
        assert "用户提供的数据，未经业务核验" in reply, reply
        assert "模拟业务数据" not in reply, reply


def test_simulated_mode_reply_still_says_simulated_data() -> None:
    """模拟示例模式：既有文案**逐字未改**（不许动既有业务输出）。"""
    model = ScriptedModel(plan_candidate())
    with acceptance_harness(model=model) as harness:
        plan_payload = harness.send("生成 A 机房最近 7 天能耗报表")
        plan = plan_payload["domainObjects"]["reportPlan"]
        confirmed = harness.confirm_plan(plan)

    # 前提：这一轮确实是模拟示例模式。
    definition = confirmed["domainObjects"]["reportDefinitionDraft"]
    assert definition["metadata"]["sourceMode"] == "simulated"

    assert SIMULATED_PLAN_SENTENCE in plan_payload["reply"], plan_payload["reply"]
    assert SIMULATED_RENDER_SENTENCE in confirmed["reply"], confirmed["reply"]
    assert "用户提供的数据，未经业务核验" not in confirmed["reply"]


def test_user_json_mode_reply_does_not_claim_a_real_data_connection() -> None:
    """用户 JSON 模式：不说「接入真实数据后会自动替换」，也不说已接上真实数据。

    用户提供的数据**不是**系统接通的真实业务库 —— 那句话是事实性错误
    （工单 #66 的验收标准也点了这条）。
    """
    model = ScriptedModel(plan_candidate(sections=_table_sections()))
    with acceptance_harness(model=model) as harness:
        plan_payload = harness.send(f"这是昨天的数据 {_json_text()}")
        confirmed = harness.confirm_plan(plan_payload["domainObjects"]["reportPlan"])

    for reply in (plan_payload["reply"], confirmed["reply"]):
        assert "接入真实数据后会自动替换" not in reply, reply
        assert "已接上真实数据" not in reply, reply


def test_user_json_mode_reply_does_not_call_the_users_numbers_illustrative() -> None:
    """用户 JSON 模式：数值来自用户自己的数据，不是「示意数值」。"""
    model = ScriptedModel(plan_candidate(sections=_table_sections()))
    with acceptance_harness(model=model) as harness:
        plan_payload = harness.send(f"这是昨天的数据 {_json_text()}")
        confirmed = harness.confirm_plan(plan_payload["domainObjects"]["reportPlan"])

    reply = confirmed["reply"]
    assert "已绑定指标并出了数值" in reply, reply
    assert "示意数值" not in reply, reply


# --------------------------------------------------------------------------
# F3bis —— 上游异常原文不进用户可见回复
# --------------------------------------------------------------------------


def test_leak_template_build_failure_keeps_reason_out_of_reply(
    caplog: Any, monkeypatch: Any,
) -> None:
    """生成模板失败：用户可见的回复不带上游异常原文。"""
    caplog.set_level(logging.WARNING, logger="app")
    monkeypatch.setattr(
        design_module,
        "build_template_design",
        _boom("字段 " + USER_DATA_MARKER + " 的类型不合法"),
    )
    model = ScriptedModel(plan_candidate())
    with acceptance_harness(model=model) as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        payload = harness.confirm_plan(plan)

    assert payload["error"]["code"] == "TEMPLATE_BUILD_FAILED"
    _assert_no_leak(payload, caplog)
    assert "errorType=ValueError" in caplog.text, caplog.text
    # 用户拿到的是能行动的提示，不是一句「请重试」。
    assert "这次没能生成模板" in payload["reply"], payload["reply"]
    assert "方案保持原样" in payload["reply"], payload["reply"]


def test_leak_template_to_draft_failure_keeps_reason_out_of_reply(
    caplog: Any, monkeypatch: Any,
) -> None:
    """模板转草稿失败：用户可见的回复不带上游异常原文。"""
    caplog.set_level(logging.WARNING, logger="app")
    monkeypatch.setattr(
        design_module,
        "build_template_definition",
        _boom("字段 " + USER_DATA_MARKER + " 的类型不合法"),
    )
    model = ScriptedModel(plan_candidate())
    with acceptance_harness(model=model) as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        payload = harness.confirm_plan(plan)

    assert payload["error"]["code"] == "TEMPLATE_BUILD_FAILED"
    _assert_no_leak(payload, caplog)
    assert "errorType=ValueError" in caplog.text, caplog.text
    assert "这次没能把方案转换成报表草稿" in payload["reply"], payload["reply"]


def test_leak_commit_rejected_keeps_reason_out_of_reply(
    caplog: Any, monkeypatch: Any,
) -> None:
    """提交被拒：用户可见的回复不带上游异常原文。"""
    caplog.set_level(logging.WARNING, logger="app")

    def _reject(self: Any, *args: Any, **kwargs: Any):
        raise DesignPlanInvalidError("字段 " + USER_DATA_MARKER + " 不合法")

    monkeypatch.setattr(
        SqliteReportDesignRepository, "commit_template_and_definition", _reject
    )
    model = ScriptedModel(plan_candidate())
    with acceptance_harness(model=model) as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        payload = harness.confirm_plan(plan)

    assert payload["error"]["code"] == "TEMPLATE_COMMIT_REJECTED"
    _assert_no_leak(payload, caplog)
    assert "errorType=DesignPlanInvalidError" in caplog.text, caplog.text
    assert "这次草稿没能保存下来" in payload["reply"], payload["reply"]


def test_leak_commit_failed_keeps_reason_out_of_reply(
    caplog: Any, monkeypatch: Any,
) -> None:
    """提交失败：用户可见的回复不带上游异常原文。"""
    caplog.set_level(logging.WARNING, logger="app")

    def _fail(self: Any, *args: Any, **kwargs: Any):
        raise RuntimeError("字段 " + USER_DATA_MARKER + " 写入失败")

    monkeypatch.setattr(
        SqliteReportDesignRepository, "commit_template_and_definition", _fail
    )
    model = ScriptedModel(plan_candidate())
    with acceptance_harness(model=model) as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        payload = harness.confirm_plan(plan)

    assert payload["error"]["code"] == "TEMPLATE_COMMIT_FAILED"
    _assert_no_leak(payload, caplog)
    assert "errorType=RuntimeError" in caplog.text, caplog.text
    assert "这次草稿没能保存下来" in payload["reply"], payload["reply"]


def test_leak_legacy_template_confirm_keeps_reason_out_of_reply(
    caplog: Any, monkeypatch: Any,
) -> None:
    """历史「确认模板」入口失败：用户可见的回复不带上游异常原文。"""
    caplog.set_level(logging.WARNING, logger="app")
    monkeypatch.setattr(
        design_module,
        "build_template_definition",
        _boom("板块 2「PUE 趋势」的值 " + USER_DATA_MARKER + " 不合法"),
    )
    model = ScriptedModel(plan_candidate())
    with acceptance_harness(model=model) as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        # 造一个历史会话：模板已落库、阶段停在 TEMPLATE_READY。
        template = build_template_design(plan, subject_id="room_a").model_dump(mode="json")
        conn = harness.repository.conn
        conn.execute(
            "INSERT INTO report_design_templates"
            " (session_id, template_id, template_revision, plan_id, plan_revision,"
            "  data_status, template_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                harness.session_id, template["id"], template["revision"], plan["id"],
                plan["revision"], template.get("dataStatus"),
                json.dumps(template, ensure_ascii=False), "2026-10-29T00:00:00",
                "2026-10-29T00:00:00",
            ),
        )
        conn.execute(
            "UPDATE report_design_sessions SET design_phase = 'TEMPLATE_READY'"
            " WHERE session_id = ?",
            (harness.session_id,),
        )
        conn.commit()

        payload = harness.send(
            "确认模板",
            context={
                "designAction": "CONFIRM_TEMPLATE",
                "templateId": template["id"],
                "baseTemplateRevision": template["revision"],
            },
        )

    assert payload["error"]["code"] == "TEMPLATE_CONFIRM_FAILED"
    _assert_no_leak(payload, caplog)
    assert "errorType=ValueError" in caplog.text, caplog.text
    assert "模板未能安全转换为草稿" in payload["reply"], payload["reply"]
    assert "当前模板保持原样" in payload["reply"], payload["reply"]


def test_operation_record_keeps_the_failure_code_but_not_the_reason(
    caplog: Any, monkeypatch: Any,
) -> None:
    """诊断侧仍能查到失败原因：**错误码留着**，异常原文不留。"""
    caplog.set_level(logging.WARNING, logger="app")

    def _fail(self: Any, *args: Any, **kwargs: Any):
        raise RuntimeError("字段 " + USER_DATA_MARKER + " 写入失败")

    monkeypatch.setattr(
        SqliteReportDesignRepository, "commit_template_and_definition", _fail
    )
    model = ScriptedModel(plan_candidate())
    with acceptance_harness(model=model) as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        payload = harness.confirm_plan(plan)
        rows = _operation_rows(harness)

    assert payload["error"]["code"] == "TEMPLATE_COMMIT_FAILED"
    # 失败的操作留着：排查靠错误码，不靠异常原文。
    failed = [row for row in rows if row["error_code"] == "TEMPLATE_COMMIT_FAILED"]
    assert failed, rows
    assert USER_DATA_MARKER not in failed[0]["error_message"]
