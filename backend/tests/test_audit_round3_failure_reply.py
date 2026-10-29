"""造数失败时，回复正文不能自相矛盾。

复核报出的 F-1：`_draft_response` 先按模板的 bound 区域数写下
「N 个区域已绑定指标并出了示意数值」，随后 `_attach_preview_payload`
在造数失败时追加「（示意数据没有生成出来：…）」。

于是同一段正文**先说出了数、后说没生成出来** —— 自相矛盾。
触发条件：任何造数失败（周/月粒度、表格没列、定义非法、时间窗推不出来）。

修法：失败时把那句成功口径改成失败口径，原因单独说明。
"""
from __future__ import annotations

from tests.test_audit_round2_s1_s2 import _confirm, _plan_for


def test_failed_generation_reply_does_not_claim_numbers_were_shown(
    client, monkeypatch,
):
    """按周统计 → 造数失败：回复里不能同时说「出了示意数值」和「没生成出来」。"""
    plan = _plan_for(client, monkeypatch, "生成 A 机房最近 7 天能耗报表，按周统计")
    assert plan["requirements"]["grainUnit"] == "WEEK"

    confirmed = _confirm(client, plan)

    # 前提：这一轮确实没出数，且失败是可读的。
    assert confirmed["reportPreviewPayload"] is None
    error = confirmed["error"]
    assert error is not None
    assert error["code"] == "UNRESOLVED_GRAIN"

    reply = confirmed["reply"]
    # 关键：成功口径那句不能还在。
    assert "出了示意数值" not in reply, reply
    # 失败口径与原因都在。
    assert "示意数据没有生成出来" in reply, reply
    assert "WEEK" in reply, reply
    assert "原因" in reply, reply


def test_failed_generation_reply_keeps_the_other_wording_intact(
    client, monkeypatch,
):
    """失败轮的其他说明（草稿已生成、能力待接通等）不能被改写掉。"""
    plan = _plan_for(client, monkeypatch, "生成 A 机房最近 7 天能耗报表，按周统计")
    confirmed = _confirm(client, plan)

    reply = confirmed["reply"]
    assert "已按第" in reply and "生成报表草稿" in reply, reply
    assert "模拟业务数据" in reply, reply
