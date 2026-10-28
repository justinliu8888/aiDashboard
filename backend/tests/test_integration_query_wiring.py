"""合流接入测试：工单 #13/#14/#15/#16 在同一个集成工作区里真正接上。

这一组测试盯的是**集成**，不是单个模块的内部实现：

    - C 的 ``get_recorded_progress`` 与 D 的 ``check_product_capability``
      真的进了查询 Toolkit（不是开发期替身注册）；
    - 两个领域模块对共用层暴露 ``status``（``FactStatus`` 四态）；
    - 领域状态折成共用事实状态时语义不塌陷：
      「登记但不支持」仍是 FOUND 但必须披露执行限制，
      「缺上下文」仍是 FOUND 但必须给澄清，
      「读取失败」「无权限」「确实没有」互不混淆；
    - 两个工具的 ``summary`` 能进入用户可见答案，
      不回退成把原始 JSON 甩给用户；
    - 响应级契约以 ``AgentResponse.turnKind`` 为准，
      领域标记（``answerKind`` / ``outcome``）不作为前端判断依据。

接口测试走聊天 HTTP 入口与真实 AgentScope Toolkit，不测私有函数。
"""
from __future__ import annotations

import pytest

from app.schemas.agent import TURN_KIND_QUERY_ANSWER, AgentState
from app.schemas.fact import FACT_STATUSES, FactStatus
from app.services.session_service import session_service
from app.tools import registry
from app.tools.base import ToolCategory


PROGRESS_TOOL = "get_recorded_progress"
CAPABILITY_TOOL = "check_product_capability"


# ---------- 夹具 ----------


def _definition():
    from app.services.report_draft_service import build_definition_draft

    return build_definition_draft("A 机房能耗日报", "room_a")


def _upsert_session(
    session_id: str = "sess_int",
    *,
    state: str = "WAITING_USER_CONFIRM",
    definition=None,
    snapshot: dict | None = None,
):
    payload = dict(snapshot or {})
    if definition is not None:
        payload["definition"] = definition.model_dump(mode="json")
    return session_service.repository.upsert_session(
        session_id=session_id,
        state=state,
        title="合流测试会话",
        snapshot=payload,
    )


@pytest.fixture()
def bound_context(monkeypatch):
    """绑定本轮工具上下文 + 会话 key，模拟一次真实请求。"""
    from app.services.session_context import bind_session_key
    from app.tools.context import ToolContext, bind_tool_context, clear_tool_context

    class _Request:
        sessionId = None
        conversationId = "conv_int"

    bind_session_key(_Request())
    bind_tool_context(
        ToolContext(
            state=AgentState.ANALYZING,
            subject_ref="machine_room",
            subject_id="room_a",
            project_id="project_001",
            definition=None,
        )
    )
    yield
    clear_tool_context()


# ---------- 1. 正式工具接入：真的注册进查询 Toolkit ----------


def test_progress_and_capability_tools_are_in_the_real_registry():
    """C / D 的工具必须在正式注册表里，不是 monkeypatch 替身。"""
    names = {tool.NAME for tool in registry.list_tools()}

    assert PROGRESS_TOOL in names
    assert CAPABILITY_TOOL in names


def test_progress_and_capability_tools_are_in_the_query_toolset():
    """查询模式的 Toolkit 必须包含这两个事实工具。"""
    names = {tool.NAME for tool in registry.tools_for_query()}

    assert PROGRESS_TOOL in names
    assert CAPABILITY_TOOL in names


def test_real_agentscope_toolkit_registers_and_runs_both_fact_tools():
    """**真实 AgentScope Toolkit** 里能查到并真的调用这两个工具。

    替身注册测试不能证明正式接入：这里用生产查询工具集建 Toolkit，
    再从 Toolkit 自己列出的 schema 里找工具名。
    """
    import asyncio
    import json

    from agentscope.message import ToolCallBlock
    from agentscope.state import AgentState as ScopeAgentState

    from app.services.session_context import bind_session_key
    from app.tools.context import ToolContext, bind_tool_context, clear_tool_context
    from app.tools.registry import to_agentscope_toolkit_async, tools_for_query

    class _Request:
        sessionId = None
        conversationId = "conv_int"

    async def _drain(gen):
        chunks = []
        async for chunk in gen:
            chunks.append(chunk)
        return chunks

    async def scenario():
        bind_session_key(_Request())
        bind_tool_context(
            ToolContext(
                state=AgentState.ANALYZING,
                subject_ref="machine_room",
                subject_id="room_a",
                project_id="project_001",
                definition=None,
            )
        )
        try:
            toolkit = await to_agentscope_toolkit_async(tools_for_query())
            schemas = await toolkit.get_tool_schemas()
            names = {
                (item.get("function") or {}).get("name")
                for item in schemas
            }
            assert PROGRESS_TOOL in names
            assert CAPABILITY_TOOL in names

            # 真调用一次：证明注册进 Toolkit 之后确实能被模型调到。
            progress_call = ToolCallBlock(
                type="tool_call",
                id="call_progress",
                name=PROGRESS_TOOL,
                input=json.dumps({}),
            )
            chunks = await _drain(
                toolkit.call_tool(progress_call, ScopeAgentState())
            )
            payload = json.loads(chunks[0].content[0].text)
            assert payload["status"] in FACT_STATUSES
            assert payload["summary"].strip()
        finally:
            clear_tool_context()

    asyncio.run(scenario())


def test_registering_both_tools_keeps_tool_contracts_valid():
    """注册之后，工具契约自洽校验仍然全通过。"""
    assert registry.validate_tool_contracts() == []


def test_registered_tools_stay_read_only_fact_tools():
    """两个工具都必须是只读事实工具，不能变成执行出口。"""
    by_name = {tool.NAME: tool for tool in registry.list_tools()}

    for name in (PROGRESS_TOOL, CAPABILITY_TOOL):
        tool = by_name[name]
        assert tool.CATEGORY is ToolCategory.FACT
        assert tool.IS_READ_ONLY is True
        assert getattr(tool, "IS_QUERY_ONLY", False) is False


# ---------- 2. 领域状态 -> 共用事实状态 ----------


def test_progress_tool_exposes_shared_fact_status(bound_context):
    """进度工具对共用层暴露 status，且取值合法。"""
    from app.tools.get_recorded_progress_tool import run

    _upsert_session("conv_int", definition=_definition())

    result = run()

    assert result["status"] in FACT_STATUSES
    assert result["status"] == FactStatus.FOUND.value


def test_capability_tool_exposes_shared_fact_status(bound_context):
    """能力工具对共用层暴露 status，且取值合法。"""
    from app.tools.check_product_capability_tool import run

    result = run(sourceRef="catalog.machine_room.energy", grainUnit="MINUTE")

    assert result["status"] in FACT_STATUSES


def test_domain_availability_is_folded_into_shared_status(bound_context):
    """C 的 availability 必须折成共用 status，不能各说各话。"""
    from app.tools.get_recorded_progress_tool import run
    from app.schemas.fact import to_fact_status

    _upsert_session("conv_int", definition=_definition())
    result = run()

    assert result["status"] == to_fact_status(result["availability"]).value


def test_domain_outcome_is_folded_into_shared_status(bound_context):
    """D 的 outcome 必须折成共用 status，不能各说各话。"""
    from app.tools.check_product_capability_tool import run
    from app.schemas.fact import to_fact_status

    result = run(sourceRef="catalog.machine_room.energy", grainUnit="MINUTE")

    assert result["status"] == to_fact_status(result["outcome"]).value


def test_registered_but_not_supported_is_found_but_discloses_the_limit(bound_context):
    """「目录登记但链路不支持」：事实查到了（FOUND），但限制必须披露。"""
    from app.tools.check_product_capability_tool import run

    result = run(sourceRef="catalog.machine_room.energy", grainUnit="MINUTE")

    assert result["outcome"] == "REGISTERED_NOT_SUPPORTED"
    assert result["status"] == FactStatus.FOUND.value
    # 限制不能因为 status=FOUND 就丢掉。
    assert result["execution"]["generation"]["supported"] is False
    assert result["execution"]["generation"]["reason"]
    assert result["mustState"]


def test_missing_context_is_found_but_asks_for_clarification(bound_context):
    """「缺上下文」：事实查到了（FOUND），但必须给必要澄清。"""
    from app.tools.check_product_capability_tool import run

    result = run()

    assert result["outcome"] == "MISSING_CONTEXT"
    assert result["status"] == FactStatus.FOUND.value
    assert result["clarification"]
    assert result["clarification"].get("question")


def test_unregistered_is_absent_and_unauthorized_is_forbidden(bound_context):
    """「确实没有」是 ABSENT，「无权限」是 FORBIDDEN，两者不能混淆。"""
    from app.tools.check_product_capability_tool import run
    from app.tools.context import ToolContext, bind_tool_context, clear_tool_context

    unknown = run(sourceRef="catalog.does.not.exist")
    assert unknown["outcome"] == "NOT_REGISTERED"
    assert unknown["status"] == FactStatus.ABSENT.value

    # 换一个业务对象范围：目录条目不可见 -> 无权限，不是「不存在」。
    bind_tool_context(
        ToolContext(
            state=AgentState.ANALYZING,
            subject_ref="other_room",
            subject_id="room_b",
            project_id="project_001",
            definition=None,
        )
    )
    try:
        unauthorized = run(sourceRef="catalog.machine_room.energy")
    finally:
        clear_tool_context()

    assert unauthorized["outcome"] == "NOT_AUTHORIZED"
    assert unauthorized["status"] == FactStatus.FORBIDDEN.value
    # 无权限时不能泄露该对象的粒度 / 字段细节。
    assert unauthorized["catalog"]["supportedGrains"] == []
    assert unauthorized["catalog"]["fields"] == []


def test_progress_unavailable_is_not_reported_as_absent(bound_context, monkeypatch):
    """读取失败是 UNAVAILABLE，不能说成「确实没有」。"""
    from app.tools.get_recorded_progress_tool import run

    def _boom(*_args, **_kwargs):
        raise RuntimeError("数据库不可用")

    monkeypatch.setattr(
        "app.services.recorded_progress_service.session_service.repository.get_session",
        _boom,
    )

    result = run()

    assert result["availability"] == "UNAVAILABLE"
    assert result["status"] == FactStatus.UNAVAILABLE.value
    assert result["status"] != FactStatus.ABSENT.value


# ---------- 4. 进度模块与 A 的可信会话事实：范围校验必须一致 ----------


def _seed_session_with_scope(scope: dict | None = None):
    """造一条会话：可选地带上存储的范围。"""
    definition = _definition().model_dump(mode="json")
    snapshot: dict = {"definition": definition}
    if scope is not None:
        snapshot["scope"] = scope
    return session_service.repository.upsert_session(
        session_id="conv_int",
        state="WAITING_USER_CONFIRM",
        title="范围校验会话",
        snapshot=snapshot,
    )


def test_progress_tool_matches_trusted_facts_when_stored_scope_conflicts(bound_context):
    """会话记录里的范围与本轮请求冲突时，两个模块必须同一个结论。

    A 的可信会话事实判 FORBIDDEN；进度工具如果还把草稿念出来，
    就是越权披露 —— 范围校验必须一致。
    """
    from app.services.session_fact_service import session_fact_service
    from app.tools.get_recorded_progress_tool import run

    _seed_session_with_scope(
        {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_b",
        }
    )

    facts = session_fact_service.load(
        session_id="conv_int",
        project_id="project_001",
        subject_ref="machine_room",
        subject_id="room_a",
    )
    result = run()

    assert facts.status is FactStatus.FORBIDDEN
    assert result["status"] == FactStatus.FORBIDDEN.value
    assert result["availability"] == "PERMISSION_DENIED"
    # 不能泄露该会话里的草稿名称。
    assert result["draft"]["name"] == ""
    assert result["draft"]["exists"] is False


def test_progress_tool_rejects_unauthorized_subject_without_workspace(bound_context):
    """会话没挂项目时也不能越权读别人的会话。

    请求的是未授权的业务对象（room_b）—— A 的可信事实与进度工具
    必须给同一个结论。
    """
    from app.services.session_fact_service import session_fact_service
    from app.tools.context import ToolContext, bind_tool_context, clear_tool_context
    from app.tools.get_recorded_progress_tool import run

    _seed_session_with_scope()

    bind_tool_context(
        ToolContext(
            state=AgentState.ANALYZING,
            subject_ref="machine_room",
            subject_id="room_b",
            project_id="project_001",
            definition=None,
        )
    )
    try:
        facts = session_fact_service.load(
            session_id="conv_int",
            project_id="project_001",
            subject_ref="machine_room",
            subject_id="room_b",
        )
        result = run()
    finally:
        clear_tool_context()

    assert facts.status is FactStatus.FORBIDDEN
    assert result["status"] == FactStatus.FORBIDDEN.value
    assert result["draft"]["name"] == ""


def test_progress_tool_still_reads_own_authorized_session(bound_context):
    """范围一致时，进度工具照常可读 —— 修复不能把正常查询也堵死。"""
    from app.tools.get_recorded_progress_tool import run

    _seed_session_with_scope(
        {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        }
    )

    result = run()

    assert result["status"] == FactStatus.FOUND.value
    assert result["availability"] == "SUCCESS"
    assert result["draft"]["name"] == "A 机房能耗日报"


# ---------- 3. summary 能进入用户可见答案 ----------


def test_progress_tool_carries_a_user_facing_summary(bound_context):
    """进度工具带一句面向用户的话，能并进回答。"""
    from app.tools.get_recorded_progress_tool import run

    _upsert_session("conv_int", definition=_definition())

    result = run()

    assert isinstance(result.get("summary"), str)
    assert result["summary"].strip()


def test_capability_tool_carries_a_user_facing_summary(bound_context):
    """能力工具带一句面向用户的话，能并进回答。"""
    from app.tools.check_product_capability_tool import run

    result = run(sourceRef="catalog.machine_room.energy", grainUnit="MINUTE")

    assert isinstance(result.get("summary"), str)
    assert result["summary"].strip()


def test_capability_summary_keeps_catalog_execution_and_data_boundary(bound_context):
    """能力回答必须保住：目录登记、执行支持、模拟数据 / 生产接通边界。"""
    from app.tools.check_product_capability_tool import run

    result = run(sourceRef="catalog.machine_room.energy", grainUnit="MINUTE")

    blob = " ".join(
        [
            result["summary"],
            " ".join(result.get("mustState") or []),
            str(result.get("catalog")),
            str(result.get("execution")),
            str(result.get("dataSource")),
        ]
    )
    # 目录登记与链路支持的区别
    assert "目录" in blob
    # 模拟业务数据 vs 生产接通
    assert "模拟" in blob or "生产" in blob
    # 数据源取数模式必须给出明确说法
    assert result["dataSource"]["mode"] in {"SIMULATED", "PRODUCTION"}
    assert result["dataSource"]["productionConnected"] is (
        result["dataSource"]["mode"] == "PRODUCTION"
    )


def test_progress_summary_marks_recorded_not_realtime(bound_context):
    """进度回答要说明是已记录阶段，不是实时监控。"""
    from app.tools.get_recorded_progress_tool import run

    _upsert_session("conv_int", definition=_definition())

    result = run()

    assert result["isRealtime"] is False
    assert "已记录" in result["summary"] or "实时" in result["summary"]


# ---------- 4. 聊天 HTTP 入口：三类查询真的可用 ----------


def test_chat_query_reply_contains_progress_facts_not_raw_json(post_chat):
    """问进度：答案里有可读事实，不是原始 JSON。"""
    _upsert_session("conv_test", definition=_definition())

    response = post_chat("刚才那份做到哪一步了？", state="ANALYZING").json()

    assert response["turnKind"] == TURN_KIND_QUERY_ANSWER
    assert response["reply"].strip()
    # 不能把工具返回体原样甩给用户。
    assert '{"answerKind"' not in response["reply"]
    assert '"availability"' not in response["reply"]
    # 要能看出是「已记录」而不是实时。
    assert "已记录" in response["reply"] or "实时" in response["reply"]


def test_chat_query_reply_contains_capability_facts_not_raw_json(post_chat):
    """问能力：答案里有可读事实，不是原始 JSON。"""
    response = post_chat("你支持分钟趋势吗？", state="ANALYZING").json()

    assert response["turnKind"] == TURN_KIND_QUERY_ANSWER
    assert response["reply"].strip()
    assert '{"outcome"' not in response["reply"]
    assert '"mustState"' not in response["reply"]


def test_chat_query_reply_keeps_capability_data_boundary(post_chat):
    """能力回答要保留模拟数据 / 生产接通的边界。"""
    response = post_chat("能耗成本和 PUE 能出吗？", state="ANALYZING").json()

    assert response["turnKind"] == TURN_KIND_QUERY_ANSWER
    assert "模拟" in response["reply"] or "生产" in response["reply"]


def test_chat_query_answer_uses_turn_kind_as_the_contract(post_chat):
    """前端判断依据是 turnKind，不是领域标记。"""
    _upsert_session("conv_test", definition=_definition())

    response = post_chat("现在进度到哪了？", state="ANALYZING").json()

    assert response["turnKind"] == TURN_KIND_QUERY_ANSWER
    # 领域标记不得顶替响应契约。
    assert "answerKind" not in response
    assert "outcome" not in response
    assert "availability" not in response


def test_chat_query_answer_does_not_advance_business(post_chat):
    """保留推进状态的查询回答不推进业务。"""
    _upsert_session("conv_test", state="VALIDATING", definition=_definition())

    response = post_chat("校验到哪一步了？", state="VALIDATING").json()

    assert response["state"] == "VALIDATING"
    assert response["turnKind"] == TURN_KIND_QUERY_ANSWER
    assert response["uiActions"] == ["query_answer"]
    assert response["toolRequests"] == []


# ---------- 5. 只读边界：查询前后业务对象逐项不变 ----------


def _business_fingerprint(session_id: str = "conv_test") -> dict:
    """业务对象的完整指纹：查询前后必须逐项相同。"""
    record = session_service.repository.get_session(session_id)
    snapshot = dict(record.snapshot or {})
    definition = snapshot.get("definition") or {}
    return {
        "state": record.state,
        "definition": definition.get("metadata"),
        "definitionRegions": len(definition.get("regions") or []),
        "savedDefinition": snapshot.get("savedDefinition"),
        "preview": snapshot.get("previewPayload"),
        "lastReportRun": snapshot.get("lastReportRun"),
        "reportDefinitionId": record.reportDefinitionId,
        "previewRunId": record.previewRunId,
        "pendingChoice": record.pendingChoice,
        "pendingPlan": session_service.load_pending_plan(session_id),
        "clarification": session_service.load_clarification(session_id),
    }


def _seed_full_business_state(session_id: str = "conv_test", *, with_edit_plan: bool):
    """造一个「什么都有」的会话：草稿 + 预览 + 待确认菜单（+ 待确认计划）。

    ``with_edit_plan`` 决定服务端等的是哪个待确认状态：
    有待确认修改计划时，服务端权威状态是 ``USER_EDITING_TEMPLATE``；
    只有菜单时是 ``WAITING_USER_CONFIRM``。请求的 state 必须与之一致，
    否则服务端按「客户端状态与待确认状态不符」处理，那不是查询轮。
    """
    definition = _definition().model_dump(mode="json")
    snapshot = {
        "definition": definition,
        "previewPayload": {"reportRunId": "run_int_1", "regions": []},
        "lastReportRun": {"reportRunId": "run_int_1", "status": "SUCCEEDED"},
    }
    state = "USER_EDITING_TEMPLATE" if with_edit_plan else "WAITING_USER_CONFIRM"
    session_service.repository.upsert_session(
        session_id=session_id,
        state=state,
        title="业务对象齐全的会话",
        reportDefinitionId="def_int_1",
        previewRunId="run_int_1",
        snapshot=snapshot,
    )
    if not with_edit_plan:
        session_service.repository.set_pending_choice(
            session_id,
            {
                "state": "WAITING_USER_CONFIRM",
                "question": {
                    "id": "template_edit_confirm",
                    "prompt": "请选择",
                    "expects": "SINGLE_CHOICE",
                    "options": [{"value": "accept", "label": "同意"}],
                },
            },
        )
    else:
        session_service.repository.save_pending_edit_plan(
            session_id,
            {
                "planId": "plan_int_1",
                "draftRevision": 0,
                "summary": "把名称改成新日报",
                "changes": [],
            },
        )
    return state


def test_query_leaves_every_business_object_untouched(post_chat):
    """一次只读查询前后，业务对象逐项不变。

    覆盖：草稿内容与 revision、已保存定义、预览与运行标识、待确认菜单、
    待确认修改计划、原业务状态、修改澄清计数。
    """
    state = _seed_full_business_state(with_edit_plan=False)
    before = _business_fingerprint()

    response = post_chat("你有未完成的草稿吗？", state=state).json()

    assert response["turnKind"] == TURN_KIND_QUERY_ANSWER
    after = _business_fingerprint()
    for key in before:
        assert after[key] == before[key], f"查询改变了业务对象：{key}"


def test_progress_query_leaves_every_business_object_untouched(post_chat):
    """问进度同样不碰任何业务对象。"""
    state = _seed_full_business_state(with_edit_plan=False)
    before = _business_fingerprint()

    post_chat("刚才那份做到哪一步了？", state=state)

    after = _business_fingerprint()
    for key in before:
        assert after[key] == before[key], f"查询改变了业务对象：{key}"


def test_capability_query_leaves_every_business_object_untouched(post_chat):
    """问能力同样不碰任何业务对象，也不建草稿。"""
    state = _seed_full_business_state(with_edit_plan=False)
    before = _business_fingerprint()

    post_chat("你支持分钟趋势吗？", state=state)

    after = _business_fingerprint()
    for key in before:
        assert after[key] == before[key], f"查询改变了业务对象：{key}"


def test_query_keeps_pending_edit_plan_and_its_state(post_chat):
    """有待确认修改计划时提问：原计划与原业务状态都保留。"""
    state = _seed_full_business_state(with_edit_plan=True)
    before = _business_fingerprint()

    response = post_chat("你有未完成的草稿吗？", state=state).json()

    assert response["turnKind"] == TURN_KIND_QUERY_ANSWER
    assert response["state"] == state
    after = _business_fingerprint()
    for key in before:
        assert after[key] == before[key], f"查询改变了业务对象：{key}"


def test_query_only_appends_chat_messages(post_chat):
    """查询允许追加聊天消息，这是它唯一的写入。"""
    state = _seed_full_business_state(with_edit_plan=False)
    before_count = len(session_service.repository.list_messages("conv_test"))

    post_chat("你有未完成的草稿吗？", state=state)

    after = session_service.repository.list_messages("conv_test")
    assert len(after) == before_count + 2  # 一条用户消息 + 一条助手回答


def test_query_does_not_create_a_pending_choice_that_did_not_exist(post_chat):
    """查询不能「顺手恢复」出一个原本不存在的选择锁。

    只读问答不是决定，也不是恢复手段：pendingChoice 查询前后必须一模一样。
    """
    _upsert_session("conv_test", state="WAITING_USER_CONFIRM", definition=_definition())
    session_service.repository.set_pending_choice("conv_test", None)
    before = session_service.repository.get_session("conv_test").pendingChoice
    assert before is None

    post_chat("你有未完成的草稿吗？", state="WAITING_USER_CONFIRM")

    after = session_service.repository.get_session("conv_test").pendingChoice
    assert after == before


def test_query_does_not_write_back_a_stale_client_snapshot(post_chat):
    """客户端带了过旧快照：服务端最新草稿不能被覆盖。"""
    state = _seed_full_business_state(with_edit_plan=False)
    stale = _definition().model_dump(mode="json")
    stale["metadata"]["name"] = "旧名字"
    stale["metadata"]["revision"] = 0

    post_chat(
        "你有未完成的草稿吗？",
        state=state,
        snapshots={"reportDefinition": stale},
    )

    stored = session_service.repository.get_session("conv_test").snapshot["definition"]
    assert stored["metadata"]["name"] == "A 机房能耗日报"


# ---------- 6. 读取失败时：会话读取与查询都要活下来 ----------

def test_session_read_survives_a_malformed_stored_draft(client):
    """服务端存的草稿结构坏掉时，会话读取接口不能 500。

    这是「读取失败」在真实链路上的样子：草稿解析不了。
    会话读取要能把这个会话读出来（草稿部分尽力还原），
    否则前端刷新就是一片空白 —— 查询答了「无法确认」，页面却打不开。
    """
    broken = {"metadata": {"id": "def_broken"}, "regions": ["这不是合法区块列表"]}
    session_service.repository.upsert_session(
        session_id="sess_broken",
        state="WAITING_USER_CONFIRM",
        title="坏草稿会话",
        snapshot={"definition": broken},
    )

    response = client.get("/api/sessions/sess_broken")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sessionId"] == "sess_broken"


def test_query_on_malformed_stored_draft_reports_unavailable_not_absent(client):
    """草稿解析不了时，查询必须说「无法确认」，不能说「没有草稿」。"""
    from app.schemas.fact import FactStatus

    broken = {"metadata": {"id": "def_broken"}, "regions": ["这不是合法区块列表"]}
    session_service.repository.upsert_session(
        session_id="sess_broken2",
        state="WAITING_USER_CONFIRM",
        title="坏草稿会话",
        snapshot={"definition": broken},
    )

    body = {
        "agentProtocolVersion": "1.0",
        "conversationId": "sess_broken2",
        "sessionId": "sess_broken2",
        "state": "WAITING_USER_CONFIRM",
        "message": "你有未完成的草稿吗？",
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    }
    response = client.post("/api/agent/chat", json=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["turnKind"] == TURN_KIND_QUERY_ANSWER
    # 事实状态：读到了会话但草稿读不懂 -> UNAVAILABLE（不是 ABSENT）。
    assert payload["debug"]["factStatus"] == FactStatus.UNAVAILABLE.value
    assert "没有报表草稿" not in payload["reply"]


def test_query_survives_a_region_with_a_non_dict_component(client):
    """区块结构不合法时，查询要能答出来，不能整个请求 500。

    regions 是自由字典，component 的类型没人保证。事实摘要读不懂
    那一项就跳过那一项，不能让整个会话事实读取崩掉。
    """
    from app.schemas.fact import FactStatus

    odd = {
        "metadata": {"id": "def_odd", "name": "A 机房能耗日报", "revision": 0},
        "regions": [
            {"id": "region_ok", "name": "总耗电", "role": "kpi",
             "component": {"type": "TEXT", "content": {}}},
            {"id": "region_odd", "name": "怪区块", "role": "chart",
             "component": "total"},
        ],
    }
    session_service.repository.upsert_session(
        session_id="sess_odd_region",
        state="WAITING_USER_CONFIRM",
        title="怪区块会话",
        snapshot={"definition": odd},
    )

    body = {
        "agentProtocolVersion": "1.0",
        "conversationId": "sess_odd_region",
        "sessionId": "sess_odd_region",
        "state": "WAITING_USER_CONFIRM",
        "message": "你有未完成的草稿吗？",
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    }
    response = client.post("/api/agent/chat", json=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["turnKind"] == TURN_KIND_QUERY_ANSWER
    assert payload["debug"]["factStatus"] == FactStatus.FOUND.value
    assert "A 机房能耗日报" in payload["reply"]


# ---------- 7. 查询轮不得改写服务端业务状态 ----------

def test_query_does_not_overwrite_server_state_with_the_client_claimed_state(client):
    """客户端声称的状态与服务端不符时，查询也不能把服务端状态改掉。

    业务状态是服务端事实。查询轮的响应状态取的是客户端带来的
    request.state（为了「本轮不推进」），如果拿它去落库，
    一次提问就能把会话从「待确认」改成「分析中」。
    """
    definition = _definition().model_dump(mode="json")
    session_service.repository.upsert_session(
        session_id="sess_state_guard",
        state="WAITING_USER_CONFIRM",
        title="待确认会话",
        snapshot={"definition": definition},
    )

    body = {
        "agentProtocolVersion": "1.0",
        "conversationId": "sess_state_guard",
        "sessionId": "sess_state_guard",
        # 客户端带的是过期状态
        "state": "ANALYZING",
        "message": "你有未完成的草稿吗？",
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    }
    response = client.post("/api/agent/chat", json=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["turnKind"] == TURN_KIND_QUERY_ANSWER
    stored = session_service.repository.get_session("sess_state_guard")
    assert stored.state == "WAITING_USER_CONFIRM"


def test_query_on_a_fresh_session_still_records_a_state(client):
    """新会话（库里还没有行）仍然要落下状态 —— 那是建会话，不是改状态。"""
    body = {
        "agentProtocolVersion": "1.0",
        "conversationId": "sess_new_state",
        "sessionId": "sess_new_state",
        "state": "ANALYZING",
        "message": "你有未完成的草稿吗？",
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    }
    response = client.post("/api/agent/chat", json=body)

    assert response.status_code == 200
    stored = session_service.repository.get_session("sess_new_state")
    assert stored is not None
    assert stored.state == "ANALYZING"


# ---------- 8. 路由器不得对能力问题提前作答 ----------


def _route_request(message: str):
    from app.schemas.agent import AgentRequest, AgentRequestContext

    return AgentRequest(
        conversationId="conv_route",
        state=AgentState.ANALYZING,
        message=message,
        context=AgentRequestContext(
            projectId="project_001",
            subjectRef="machine_room",
            subjectId="room_a",
        ),
    )


def test_direct_reply_is_downgraded_when_model_calls_a_capability_question_chitchat():
    """模型把能力问题判成 CHITCHAT 时，也不能直接作答。

    路由器手里没有能力事实。答一句「当然支持呀～」不是事实，
    而是猜 —— 规格明确要求「问能力…缺上下文时针对性澄清」，
    也要「区分目录登记、链路支持、模拟数据与生产接通」。
    """
    from app.services.context_engine import (
        ConciergeAction,
        ConciergeDecision,
        UserIntent,
        context_engine,
    )

    concierge = ConciergeDecision(
        action=ConciergeAction.DIRECT_REPLY,
        userIntent=UserIntent.CHITCHAT,
        reply="当然支持呀～我可以帮你生成各类机房能耗相关的报表。",
    )

    guarded = context_engine._guard_direct_reply(
        concierge, _route_request("你支持做报表吗？")
    )

    assert guarded.action is ConciergeAction.DEEP_ROUTE
    assert guarded.reply == ""


def test_direct_reply_is_still_allowed_for_ordinary_greetings():
    """普通问候仍然可以直接答 —— 规格允许「无需事实查询的直接回答」。"""
    from app.services.context_engine import (
        ConciergeAction,
        ConciergeDecision,
        UserIntent,
        context_engine,
    )

    concierge = ConciergeDecision(
        action=ConciergeAction.DIRECT_REPLY,
        userIntent=UserIntent.CHITCHAT,
        reply="我在。",
    )

    guarded = context_engine._guard_direct_reply(concierge, _route_request("你好"))
    assert guarded.action is ConciergeAction.DIRECT_REPLY

    guarded = context_engine._guard_direct_reply(
        concierge, _route_request("你是谁？")
    )
    assert guarded.action is ConciergeAction.DIRECT_REPLY


def test_direct_reply_is_still_allowed_for_explicit_generation_requests():
    """明确生成请求要走原来的深路由，不能被当成只读提问。"""
    from app.services.context_engine import (
        ConciergeAction,
        ConciergeDecision,
        UserIntent,
        context_engine,
    )

    concierge = ConciergeDecision(
        action=ConciergeAction.DIRECT_REPLY,
        userIntent=UserIntent.CHITCHAT,
        reply="好的。",
    )

    for message in (
        "生成 A 机房昨日能耗日报",
        "把趋势改成折线图",
        "接受修改",
        "确认报表",
    ):
        guarded = context_engine._guard_direct_reply(concierge, _route_request(message))
        assert guarded.action is not ConciergeAction.DEEP_ROUTE, message


# ---------- 9. 待确认入口：客户端状态与服务端不符时仍要能问 ----------


def test_readonly_question_is_answered_even_when_client_state_is_stale(client):
    """服务端挂着待确认修改计划时，客户端状态过期也要能提问。

    服务端知道「菜单在」—— 菜单是服务端事实。客户端状态过期不该
    让只读提问被拒：只读提问没有任何可绕过的执行权限，问完原菜单
    和计划照旧。
    """
    session_service.repository.save_pending_edit_plan(
        "sess_stale_state",
        {
            "planId": "plan_stale",
            "draftRevision": 0,
            "summary": "把名称改成新日报",
            "changes": [],
        },
    )
    session_service.repository.upsert_session(
        session_id="sess_stale_state",
        state="USER_EDITING_TEMPLATE",
        title="待确认计划会话",
        snapshot={"definition": _definition().model_dump(mode="json")},
    )

    body = {
        "agentProtocolVersion": "1.0",
        "conversationId": "sess_stale_state",
        "sessionId": "sess_stale_state",
        # 客户端带的是过期状态（服务端实际在 USER_EDITING_TEMPLATE）
        "state": "WAITING_USER_CONFIRM",
        "message": "你有未完成的草稿吗？",
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    }
    response = client.post("/api/agent/chat", json=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["turnKind"] == TURN_KIND_QUERY_ANSWER
    assert "A 机房能耗日报" in payload["reply"]
    # 原待确认计划还在
    plan = session_service.load_pending_plan("sess_stale_state")
    assert plan is not None and plan.planId == "plan_stale"


def test_non_readonly_input_is_still_rejected_on_stale_client_state(client):
    """但只读提问的例外不能变成绕过确认的入口：操作指令照样被拒。"""
    session_service.repository.save_pending_edit_plan(
        "sess_stale_state2",
        {
            "planId": "plan_stale2",
            "draftRevision": 0,
            "summary": "把名称改成新日报",
            "changes": [],
        },
    )
    session_service.repository.upsert_session(
        session_id="sess_stale_state2",
        state="USER_EDITING_TEMPLATE",
        title="待确认计划会话",
        snapshot={"definition": _definition().model_dump(mode="json")},
    )

    body = {
        "agentProtocolVersion": "1.0",
        "conversationId": "sess_stale_state2",
        "sessionId": "sess_stale_state2",
        "state": "WAITING_USER_CONFIRM",
        "message": "接受修改",
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    }
    response = client.post("/api/agent/chat", json=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["turnKind"] != TURN_KIND_QUERY_ANSWER
    assert payload["uiActions"] == ["selection_required"]
    # 原计划没被动过
    plan = session_service.load_pending_plan("sess_stale_state2")
    assert plan is not None and plan.planId == "plan_stale2"


# ---------- 10. 没有菜单时不要凭空造一个 ----------


def test_query_does_not_fabricate_a_menu_when_the_session_has_none(client):
    """库里没有待确认菜单时，查询回答不能凭空造一个菜单。

    ADR 0002 §4.2：「questions = 原有待确认菜单；没有菜单时为空」。
    规格：「查询前后保持…待确认选项…不变」—— 造一个出来就是变了。
    造出来的菜单前端会渲染成可点的确认按钮，但服务端并没有这把锁，
    用户点了会落空。
    """
    session_service.repository.upsert_session(
        session_id="sess_no_menu",
        state="WAITING_USER_CONFIRM",
        title="无菜单会话",
        snapshot={"definition": _definition().model_dump(mode="json")},
    )
    session_service.repository.set_pending_choice("sess_no_menu", None)

    body = {
        "agentProtocolVersion": "1.0",
        "conversationId": "sess_no_menu",
        "sessionId": "sess_no_menu",
        "state": "WAITING_USER_CONFIRM",
        "message": "你有未完成的草稿吗？",
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    }
    response = client.post("/api/agent/chat", json=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["turnKind"] == TURN_KIND_QUERY_ANSWER
    assert payload["questions"] == []
    assert session_service.repository.get_session("sess_no_menu").pendingChoice is None


def test_query_keeps_the_stored_menu_unchanged(client):
    """库里有的菜单，查询回答要原样带回，且库里那份一字不改。"""
    from app.services.agent_service import agent_service

    stored = {
        "state": "WAITING_USER_CONFIRM",
        "question": {
            "id": "template_edit_confirm",
            "prompt": "请选择",
            "expects": "SINGLE_CHOICE",
            "selectionOnly": True,
            "options": [
                {"value": "同意", "label": "同意"},
                {"value": "修改", "label": "修改"},
            ],
        },
    }
    session_service.repository.upsert_session(
        session_id="sess_has_menu",
        state="WAITING_USER_CONFIRM",
        title="有菜单会话",
        snapshot={"definition": _definition().model_dump(mode="json")},
    )
    session_service.repository.set_pending_choice("sess_has_menu", stored)

    body = {
        "agentProtocolVersion": "1.0",
        "conversationId": "sess_has_menu",
        "sessionId": "sess_has_menu",
        "state": "WAITING_USER_CONFIRM",
        "message": "你有未完成的草稿吗？",
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    }
    response = client.post("/api/agent/chat", json=body)

    payload = response.json()
    assert payload["turnKind"] == TURN_KIND_QUERY_ANSWER
    assert [q["id"] for q in payload["questions"]] == ["template_edit_confirm"]
    after = session_service.repository.get_session("sess_has_menu").pendingChoice
    assert after["question"]["options"] == stored["question"]["options"]
