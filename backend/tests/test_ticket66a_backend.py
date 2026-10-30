"""工单 #66 A 段：工作台对话区的**后端接线**。

本段交付四件事，逐条钉住：

1. **会话恢复带出版本序列**：``GET /api/sessions/{sessionId}`` 的
   ``snapshot.planVersions`` 按**真实保存顺序**给出这一版一版的方案；
   老会话没有版本历史时只恢复当前那一版并标 ``recovered``，**不伪造**
   过去的版本。
2. **chat 响应带出本次新版本的身份**：``debug.planVersionId`` 是**稳定
   ID**（同一版永远是同一个），前端按它去重 —— 重复响应、乱序响应与
   重连都**不重复追加**同一份方案。
3. **只有当前待确认的最新版可确认**：服务端继续做版本校验
   （``draftRevision`` 与 ``baseCombination`` 逐字段比对），
   **不靠禁用按钮保证安全**；旧生效组合保持不变。
4. **JSON 粘贴入口的服务端侧**：在**既有** ``POST /api/agent/chat`` 上
   真的会被识别；服务端拒绝时返回**可修正的提示**，且**什么都不存、
   不调模型**。

接口层用真实 ``POST /api/agent/chat`` + 真实服务 + 独立临时 SQLite，
只替换外部模型调用（``tests/support/acceptance_harness.py``）。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.schemas.agent import PLAN_VERSION_ID_KEY
from app.services.report_draft_service import build_definition_draft
from app.services.session_service import session_service
from tests.support.acceptance_harness import (
    ScriptedModel,
    acceptance_harness,
    plan_candidate,
)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _sections(count: int) -> list[dict[str, Any]]:
    """一份带 ``count`` 块板块的方案候选。"""
    sections: list[dict[str, Any]] = [
        {
            "id": "section_total",
            "title": "总耗电概览",
            "metricIntents": ["总耗电"],
            "componentType": "metric_card",
        }
    ]
    for index in range(1, count):
        sections.append(
            {
                "id": f"section_extra_{index}",
                "title": f"补充板块 {index}",
                "metricIntents": ["总耗电"],
                "componentType": "line_chart",
            }
        )
    return sections


def _version_id_of(payload: dict[str, Any]) -> str | None:
    """响应里带的方案版本稳定 ID。"""
    value = (payload.get("debug") or {}).get(PLAN_VERSION_ID_KEY)
    return str(value) if value else None


def _json_input_rows(harness: Any) -> list[Any]:
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


def _user_messages(harness: Any) -> list[str]:
    """落库的 user 消息原文。"""
    return [
        message.content
        for message in harness.repository.list_messages(harness.session_id)
        if message.role == "user"
    ]


# --------------------------------------------------------------------------
# 1. 会话恢复：按真实保存顺序带出版本序列
# --------------------------------------------------------------------------


def test_session_restore_returns_versions_in_real_save_order():
    """重新打开会话：历史按**真实保存顺序**回来，最新的一版是当前方案。

    顺序靠 ``sequence``（同一会话里的保存序号），不靠时间戳 —— 时间戳
    只到秒，两版可能同秒。
    """
    model = ScriptedModel(
        plan_candidate(sections=_sections(1)),
        plan_candidate(sections=_sections(2)),
        plan_candidate(sections=_sections(3)),
    )
    with acceptance_harness(model=model, session_id="sess_66a_order") as harness:
        first = harness.send("生成能耗报表")
        second = harness.send("再加一块趋势")
        third = harness.send("再补一块明细")

        assert first["domainObjects"]["reportPlan"]["revision"] == 1
        assert second["domainObjects"]["reportPlan"]["revision"] == 2
        assert third["domainObjects"]["reportPlan"]["revision"] == 3

        detail = harness.read_session()
        versions = detail["snapshot"]["planVersions"]

        # ① 序列按真实保存顺序，不按时间戳。
        assert [item["sequence"] for item in versions] == [1, 2, 3]
        assert [item["planRevision"] for item in versions] == [1, 2, 3]
        # ② 每一版都带着身份、正文与读时标记。
        for item in versions:
            assert item["versionId"]
            assert item["planId"]
            assert item["reportPlan"]["revision"] == item["planRevision"]
            assert "isCurrent" in item
            assert "confirmed" in item
            assert "recovered" in item
        # ③ 只有最后一版是当前方案；旧版正文没被改写。
        assert [item["isCurrent"] for item in versions] == [False, False, True]
        assert len(versions[0]["reportPlan"]["sections"]) == 1
        assert len(versions[2]["reportPlan"]["sections"]) == 3
        # ④ 响应里带的版本 ID 与序列里那条**对得上**（前端直接比即可）。
        assert _version_id_of(first) == versions[0]["versionId"]
        assert _version_id_of(second) == versions[1]["versionId"]
        assert _version_id_of(third) == versions[2]["versionId"]


def test_version_order_follows_save_sequence_not_timestamp():
    """顺序靠**保存序号**，不靠时间戳 —— 时间戳只到秒，两版可能同秒。

    这里把最后一版的落库时间**改早**（白盒：只改临时库里的一个时间戳），
    序列顺序仍然按真实保存顺序 —— 前端还原出来的历史卡片顺序不会错。
    """
    model = ScriptedModel(
        plan_candidate(sections=_sections(1)),
        plan_candidate(sections=_sections(2)),
    )
    with acceptance_harness(model=model, session_id="sess_66a_seq") as harness:
        harness.send("生成能耗报表")
        harness.send("再加一块趋势")

        # 把第 2 版的落库时间改到第 1 版**之前**。
        harness.repository.conn.execute(
            "UPDATE report_design_plan_versions SET created_at = ?"
            " WHERE session_id = ? AND sequence = 2",
            ("2000-01-01T00:00:00", harness.session_id),
        )
        harness.repository.conn.commit()

        versions = harness.read_session()["snapshot"]["planVersions"]

        assert [item["sequence"] for item in versions] == [1, 2]
        assert [item["planRevision"] for item in versions] == [1, 2]
        # 时间戳已经被改成了「倒序」，但序列顺序仍然按真实保存顺序。
        assert versions[0]["createdAt"] > versions[1]["createdAt"]


def test_old_session_without_version_history_recovers_only_current_version():
    """老会话没有版本历史：只恢复当前那一版并标 ``recovered``，**不伪造**历史。

    老会话的形态是「当前方案还在、但版本记录表里没有」—— #63 之前的库就是
    这样。这里按那个形状**直接写一份状态**（不删任何数据），再读回来。
    """
    with acceptance_harness(
        model=ScriptedModel(plan_candidate(sections=_sections(2))),
        session_id="sess_66a_legacy",
    ) as harness:
        # 先借真实链路拿到一份**合法**的方案正文（模型输出的形状 → 领域定稿）。
        plan = harness.request_plan("生成能耗报表")
        plan_id = plan["id"]
        revision = plan["revision"]

        # 造一份「老会话」：当前方案照旧，版本记录表里没有。
        legacy_id = "sess_66a_legacy_old"
        now = "2026-01-01T00:00:00"
        harness.repository.conn.execute(
            "INSERT INTO report_design_sessions"
            " (session_id, design_phase, plan_id, plan_revision, plan_json,"
            "  created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                legacy_id,
                "PLAN_WAITING_CONFIRM",
                plan_id,
                revision,
                json.dumps(plan, ensure_ascii=False),
                now,
                now,
            ),
        )
        harness.repository.upsert_session(
            session_id=legacy_id,
            state="ANALYZING",
            snapshot={
                "designPhase": "PLAN_WAITING_CONFIRM",
                "reportPlan": plan,
            },
        )
        harness.repository.conn.commit()

        response = harness.client.get(f"/api/sessions/{legacy_id}")
        assert response.status_code == 200, response.text
        versions = response.json()["snapshot"]["planVersions"]

        # ① 只恢复当前那一版 —— 不会凭空多出「第 1 版、第 2 版」。
        assert len(versions) == 1
        # ② 标成「恢复出来的」。
        assert versions[0]["recovered"] is True
        assert versions[0]["isCurrent"] is True
        assert versions[0]["planId"] == plan_id
        assert versions[0]["planRevision"] == revision
        # ③ 正文就是实际存着的那一份。
        assert len(versions[0]["reportPlan"]["sections"]) == 2
        # ④ 恢复出来的这一版，ID 与「当初真存过这一版」时一致（派生口径唯一）。
        assert versions[0]["versionId"].startswith("planver_")


def test_session_without_any_plan_has_empty_version_list():
    """还没有方案的会话：版本序列是空的（不返回「空的一版」）。"""
    with acceptance_harness(session_id="sess_66a_empty") as harness:
        harness.repository.upsert_session(
            session_id=harness.session_id, state="ANALYZING", snapshot={}
        )
        detail = harness.read_session()
        assert detail["snapshot"]["planVersions"] == []


# --------------------------------------------------------------------------
# 2. chat 响应带出本次新版本的身份（前端按它去重）
# --------------------------------------------------------------------------


def test_chat_response_carries_stable_version_id():
    """每一轮带方案的响应都带一个**稳定**的版本 ID。"""
    model = ScriptedModel(
        plan_candidate(sections=_sections(1)),
        plan_candidate(sections=_sections(2)),
    )
    with acceptance_harness(model=model, session_id="sess_66a_id") as harness:
        first = harness.send("生成能耗报表")
        second = harness.send("再加一块趋势")

        first_id = _version_id_of(first)
        second_id = _version_id_of(second)

        assert first_id and first_id.startswith("planver_")
        assert second_id and second_id.startswith("planver_")
        # 同一版永远是同一个 ID：重算一遍还是它。
        from app.services.plan_version_service import response_plan_version_id

        plan = first["domainObjects"]["reportPlan"]
        assert response_plan_version_id("sess_66a_id", plan) == first_id
        # 两版是**两个**不同的 ID（前端才分得开「追加一张」还是「原地改」）。
        assert first_id != second_id


def test_repeated_response_keeps_one_version_id_and_one_version():
    """重复响应：同一个 ``versionId``、只有一版 —— 前端去重后不重复追加。"""
    model = ScriptedModel(plan_candidate(sections=_sections(1)))
    with acceptance_harness(model=model, session_id="sess_66a_repeat") as harness:
        first = harness.send("生成能耗报表", message_id="msg_1")
        again = harness.send("生成能耗报表", message_id="msg_1")

        assert _version_id_of(first) == _version_id_of(again)

        versions = harness.read_session()["snapshot"]["planVersions"]
        assert len(versions) == 1
        assert versions[0]["versionId"] == _version_id_of(first)


def test_out_of_order_response_does_not_take_over_the_current_version():
    """乱序响应：迟到的旧请求**不夺取**确认资格，也不新增一版。

    迟到的响应带的是**当前**那一版的 ID —— 前端按 ID 去重，不会把它当成
    一份新方案追加进去。
    """
    model = ScriptedModel(
        plan_candidate(sections=_sections(1)),
        plan_candidate(sections=_sections(2)),
    )
    with acceptance_harness(model=model, session_id="sess_66a_late") as harness:
        harness.send("生成能耗报表", message_id="msg_1")
        latest = harness.send("再加一块趋势", message_id="msg_2")

        # 迟到的旧请求重发：服务端认出「这条已经处理过」。
        late = harness.send("生成能耗报表", message_id="msg_1")

        versions = harness.read_session()["snapshot"]["planVersions"]
        # 没有多出版本。
        assert len(versions) == 2
        # 当前版本仍是第 2 版。
        assert versions[-1]["isCurrent"] is True
        assert versions[-1]["planRevision"] == 2
        # 迟到的响应带的是当前那一版的 ID —— 不是「一份新方案」。
        assert _version_id_of(late) == versions[-1]["versionId"]
        assert _version_id_of(late) == _version_id_of(latest)


def test_reconnect_returns_the_same_version_ids():
    """重连：刷新后拿到的版本 ID 与当初响应里带的是同一个。"""
    model = ScriptedModel(
        plan_candidate(sections=_sections(1)),
        plan_candidate(sections=_sections(2)),
    )
    with acceptance_harness(model=model, session_id="sess_66a_reconnect") as harness:
        first = harness.send("生成能耗报表")
        second = harness.send("再加一块趋势")

        versions = harness.read_session()["snapshot"]["planVersions"]

        assert [item["versionId"] for item in versions] == [
            _version_id_of(first),
            _version_id_of(second),
        ]
        # 重连后落库的 assistant 消息里也带着同一个 ID（回放消息时能去重）。
        payloads = [
            message.payload
            for message in harness.assistant_messages()
            if message.payload
        ]
        ids = [
            (payload.get("debug") or {}).get(PLAN_VERSION_ID_KEY)
            for payload in payloads
        ]
        assert _version_id_of(first) in ids
        assert _version_id_of(second) in ids


# --------------------------------------------------------------------------
# 3. 只有当前待确认的最新版可确认（服务端继续做版本校验）
# --------------------------------------------------------------------------


def _definition():
    return build_definition_draft("A 机房能耗日报", "room_a")


def _snap(definition):
    return {"reportDefinition": definition.model_dump(mode="json")}


def test_stale_draft_revision_is_rejected_and_old_draft_kept(post_chat):
    """草稿版本变了以后：旧计划**拒绝接受**，旧生效草稿保持不变。

    校验在**服务端**：即使客户端照样把「接受修改」发过来，服务端也拦住。
    """
    definition = _definition()

    post_chat(
        "报表名称改成新日报",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    )
    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()
    assert accepted["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 1

    # 接受后有必选菜单：先明确选择继续修改，再提第二个计划。
    post_chat(
        "修改",
        state="USER_EDITING_TEMPLATE",
        snapshots={
            "reportDefinition": accepted["domainObjects"]["reportDefinitionDraft"]
        },
    )
    candidate = post_chat(
        "把设备耗电排名改成柱状图",
        state="USER_EDITING_TEMPLATE",
        snapshots={
            "reportDefinition": accepted["domainObjects"]["reportDefinitionDraft"]
        },
    ).json()
    assert candidate["domainObjects"]["pendingEditPlan"] is not None

    # 拿旧草稿（revision 0）去接受 —— 版本对不上，必须被拦住。
    stale = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert stale["error"]["code"] == "STALE_EDIT_PLAN"
    # 旧生效草稿保持不变（没有被这份过期计划改掉）。
    stored = session_service.repository.get_session("conv_test").snapshot
    assert stored["definition"]["metadata"]["revision"] == 1


def test_stale_base_combination_is_rejected_and_old_combination_kept(post_chat):
    """生效组合变了以后：旧计划**拒绝接受**，旧生效组合保持不变。

    这里的「变了」是**来源 / 输入**变了（``baseCombination`` 的
    ``inputId`` / ``inputVersion``），而草稿版本号没变 —— 所以拦下它的
    是**生效组合**这条判据，不是草稿版本那条。

    「组合被换了」用白盒方式模拟：候选提出来以后，把**服务端存着的那份**
    草稿的生效组合换掉（等价于另一条链路切了输入），但草稿版本号不动。
    """
    candidate_definition = _definition()
    # 候选所依据的生效组合：绑了一个用户 JSON 输入。
    candidate_definition.metadata["effectiveCombination"] = {
        "sourceMode": "simulated",
        "inputId": "inp_000000000001",
        "inputVersion": 3,
        "fieldMapping": [],
        "processingRules": [],
    }

    candidate = post_chat(
        "报表名称改成新日报",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(candidate_definition),
    ).json()
    assert candidate["domainObjects"]["pendingEditPlan"] is not None

    # 服务端存着的那份草稿：生效组合换成「没有绑定输入」的一份，
    # **草稿版本号不变**（所以草稿版本那条判据不会先拦住它）。
    record = session_service.repository.get_session("conv_test")
    stored = dict(record.snapshot)
    stored_definition = dict(stored["definition"])
    metadata = dict(stored_definition["metadata"])
    metadata["effectiveCombination"] = {
        "sourceMode": "simulated",
        "inputId": None,
        "inputVersion": None,
        "fieldMapping": [],
        "processingRules": [],
    }
    stored_definition["metadata"] = metadata
    stored["definition"] = stored_definition
    session_service.repository.upsert_session(
        session_id="conv_test", state=record.state, snapshot=stored
    )

    # 接受：服务端拿**自己存着的那份**去比对 —— 组合对不上，必须拒绝。
    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(candidate_definition),
    ).json()

    assert accepted.get("error") is not None
    assert accepted["error"]["code"] in {
        "STALE_EDIT_PLAN",
        "EDIT_VALIDATION_FAILED",
    }
    # 旧生效组合保持不变：草稿版本没被推进，组合也没被改写。
    after = session_service.repository.get_session("conv_test").snapshot
    assert after["definition"]["metadata"]["revision"] == 0
    assert (
        after["definition"]["metadata"]["effectiveCombination"]["inputId"]
        is None
    )


def test_accepted_plan_cannot_be_applied_twice(post_chat):
    """同一份待确认计划不会被生效两次：第二次接受不重复改草稿。

    这是「旧版本的迟到响应不夺取确认资格」的另一半 —— 迟到的「接受」
    也不能把已经处理过的计划再执行一遍。
    """
    definition = _definition()

    post_chat(
        "报表名称改成新日报",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    )
    first = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()
    assert first["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 1

    # 迟到的第二次接受：计划已经被取走，不再生效一次。
    second = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()
    # 草稿没有被改第二次 —— 版本号没被推进。
    after = session_service.repository.get_session("conv_test").snapshot
    assert after["definition"]["metadata"]["revision"] == 1
    assert "新日报" in after["definition"]["metadata"]["name"]
    # 而且服务端没有留下第二份待确认计划。
    assert session_service.load_pending_plan("conv_test") is None
    assert second["domainObjects"]["pendingEditPlan"] is None


# --------------------------------------------------------------------------
# 4. JSON 粘贴入口的服务端侧
# --------------------------------------------------------------------------


def test_json_payload_pasted_with_natural_language_is_detected():
    """粘贴记录数组 + 自然语言：在既有聊天入口上**真的**会被识别。

    看得见的证据：原始记录数组**存了一份**，而落库的聊天消息里只有
    **安全摘要**（完整数组不复制进消息）。
    """
    rows = [
        {"ts": "2024-01-01", "device": "机柜1", "energy": 1.5},
        {"ts": "2024-01-02", "device": "机柜2", "energy": 2.5},
    ]
    message = "这是我的设备能耗记录，请帮我做一个日报：\n" + json.dumps(
        rows, ensure_ascii=False
    )

    with acceptance_harness(session_id="sess_66a_json_ok") as harness:
        harness.send(message)

        stored = _json_input_rows(harness)
        assert len(stored) == 1
        assert stored[0]["record_count"] == 2

        # 落库的 user 消息里**没有**完整数组（只有摘要与引用）。
        texts = _user_messages(harness)
        assert texts
        assert not any(json.dumps(rows, ensure_ascii=False) in text for text in texts)
        assert any("机柜1" not in text for text in texts)


def test_rejected_json_returns_actionable_hint_and_stores_nothing():
    """服务端拒绝时：给出**可修正的提示**，什么都不存、不调模型。"""
    broken = '[{"device": "机柜1", "energy": 1.5},'
    message = "这是我的设备能耗记录：\n" + broken

    with acceptance_harness(session_id="sess_66a_json_bad") as harness:
        payload = harness.send(message)

        # ① 明确拒绝，错误码是契约表 §4.3 定的那一套。
        assert payload.get("error") is not None
        assert payload["error"]["code"] == "JSON_SYNTAX_INVALID"
        # ② 提示要「哪里不对、怎么改」—— 说了位置，也说了怎么改。
        hint = payload["error"]["message"]
        assert "JSON" in hint
        assert "行" in hint and "列" in hint
        assert "重新提交" in hint
        # ③ 什么都没存：输入记录表里一行也没有。
        assert _json_input_rows(harness) == []
        # ④ 没调模型：这一轮在校验阶段就收尾了。
        assert harness.model.calls == []
        # ⑤ 原始数组没有进落库的聊天消息。
        texts = _user_messages(harness)
        assert not any(broken in text for text in texts)


def test_pending_plan_carries_its_base_combination(post_chat):
    """待确认计划带着它所基于的**生效组合快照**（契约表 §4.2 的字段）。

    空组合按契约表 §4.2 的旧数据默认行为读作「只按 ``draftRevision``
    比」—— 读的人分不出「**没记**」与「记了但对得上」。所以这一项要
    真的记上。
    """
    definition = _definition()

    post_chat(
        "报表名称改成新日报",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    )

    pending = session_service.load_pending_plan("conv_test")
    assert pending is not None
    assert pending.baseCombination, "待确认计划没有带生效组合快照"
    # 组合是契约表 §2.1 固定的那 9 键。
    assert set(pending.baseCombination) == {
        "planId",
        "planRevision",
        "definitionId",
        "definitionVersion",
        "sourceMode",
        "inputId",
        "inputVersion",
        "fieldMapping",
        "processingRules",
    }


def test_json_mode_is_marked_on_the_definition_after_confirm():
    """JSON 模式在服务端有**明确依据**：确认方案后定义上写着来源模式。

    前端要标「用户提供的数据，未经业务核验」，依据就是这里的
    ``metadata.sourceMode == "user_json"`` 与生效组合里绑着的
    ``inputId`` / ``inputVersion`` —— 不宣称已连接真实业务库。
    """
    rows = [{"ts": "2024-01-01", "device": "机柜1", "energy": 1.5}]
    message = "这是我的记录：" + json.dumps(rows, ensure_ascii=False) + " 帮我做个日报"

    with acceptance_harness(session_id="sess_66a_json_ref") as harness:
        plan = harness.request_plan(message)
        harness.confirm_plan(plan)

        snapshot = harness.read_session()["snapshot"]
        definition = snapshot["definition"]
        metadata = definition["metadata"]

        assert metadata["sourceMode"] == "user_json"
        combination = metadata["effectiveCombination"]
        assert combination["sourceMode"] == "user_json"
        assert combination["inputId"].startswith("inp_")
        assert combination["inputVersion"] == 1
        # 「未经业务核验」：字段映射没绑上的列被如实列出来，不假装绑上了。
        assert metadata["jsonInputIssues"]["unmappedColumns"]


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ('{"device": "机柜1"}', "JSON_NOT_ARRAY"),
        ('[{"a":{"b":1}}]', "JSON_NESTED_NOT_SUPPORTED"),
        ('[{"a":1}, 2]', "JSON_ROW_NOT_OBJECT"),
    ],
)
def test_other_json_failures_return_their_own_code(payload: str, code: str):
    """失败码**不合并**：各是各的，前端能据此给出对应的提示。"""
    with acceptance_harness(session_id="sess_66a_json_codes") as harness:
        result = harness.send("帮我看看这些数据：" + payload)
        assert result["error"]["code"] == code
        assert result["error"]["message"]
        assert _json_input_rows(harness) == []
        assert harness.model.calls == []
