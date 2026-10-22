"""编辑态跨轮状态单测：待确认计划、草稿版本、澄清轮次。

覆盖 20261022 方案 §八 D 行要求：
    - 按冻结契约保存 / 回读待确认计划、草稿 revision 和澄清次数
    - 接受前校验计划是否过期
    - 调整 / 取消清理待决计划
    - 技术故障不计三轮，新需求重新计数
"""
from __future__ import annotations

import threading
import multiprocessing

import pytest

from app.schemas.agent import AgentRequest, AgentResponse, AgentState
from app.schemas.session import (
    CLARIFICATION_COUNT_KEY,
    CLARIFICATION_KEY_KEY,
    PENDING_PLAN_KEY,
    PendingEditPlan,
)
from app.services.session_service import (
    CLARIFICATION_ASKED,
    CLARIFICATION_TECHNICAL_FAILURE,
    SessionService,
)
from app.storage.sqlite_store import SqliteSessionRepository


def _take_plan_in_process(db_path, barrier, results):
    repository = SqliteSessionRepository(db_path)
    try:
        barrier.wait(timeout=10)
        results.put(repository.take_pending_edit_plan("sess_1", "plan_1"))
    finally:
        repository.close()


@pytest.fixture()
def service(tmp_path) -> SessionService:
    return SessionService(SqliteSessionRepository(tmp_path / "edit_state.db"))


def _plan(**overrides) -> PendingEditPlan:
    payload = {
        "planId": "plan_1",
        "draftRevision": 3,
        "targetRegionId": "region_2",
        "operations": [{"intent": "CHANGE_TIME_RANGE", "path": "timeRange"}],
        "summary": "把「能耗趋势」的时间范围从今天改成昨天下午。",
    }
    payload.update(overrides)
    return PendingEditPlan(**payload)


# ---------- 待确认计划：存取 ----------


def test_save_and_load_pending_plan(service: SessionService):
    service.save_pending_plan("sess_1", _plan())

    loaded = service.load_pending_plan("sess_1")
    assert loaded is not None
    assert loaded.planId == "plan_1"
    assert loaded.draftRevision == 3
    assert loaded.targetRegionId == "region_2"
    assert loaded.operations == [{"intent": "CHANGE_TIME_RANGE", "path": "timeRange"}]
    assert loaded.summary.startswith("把「能耗趋势」")


def test_load_pending_plan_returns_none_when_absent(service: SessionService):
    assert service.load_pending_plan("sess_1") is None


def test_pending_plan_is_stored_in_its_own_column(service: SessionService):
    """待确认计划存在会话行的独立列，不混在 snapshot 里。

    snapshot 会被整段重写（草稿、澄清轮次都在里面），
    计划放独立列才不会在提交草稿时被覆盖掉。
    """
    service.save_pending_plan("sess_1", _plan())

    record = service.repository.get_session("sess_1")
    assert record is not None
    assert record.pendingEditPlan is not None
    assert record.pendingEditPlan["planId"] == "plan_1"
    assert not (record.snapshot or {}).get(PENDING_PLAN_KEY)


def test_pending_plan_survives_reopen(service: SessionService, tmp_path):
    """后端重启后待确认计划还在：状态在库里，不在内存。"""
    db = service.repository.db_path
    service.save_pending_plan("sess_1", _plan())
    service.repository.close()

    reopened = SessionService(SqliteSessionRepository(db))
    loaded = reopened.load_pending_plan("sess_1")
    assert loaded is not None
    assert loaded.planId == "plan_1"


def test_only_one_process_can_take_the_same_plan(service: SessionService):
    """两个独立进程并发取走同一计划，数据库事务只让一个成功。"""
    service.save_pending_plan("sess_1", _plan())
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_take_plan_in_process,
            args=(service.repository.db_path, barrier, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
    assert [process.exitcode for process in processes] == [0, 0]
    taken = [results.get(timeout=2) for _ in processes]
    assert sum(item is not None for item in taken) == 1
    assert service.load_pending_plan("sess_1") is None


def test_saving_a_second_plan_replaces_the_first(service: SessionService):
    service.save_pending_plan("sess_1", _plan())
    service.save_pending_plan("sess_1", _plan(planId="plan_2"))

    loaded = service.load_pending_plan("sess_1")
    assert loaded.planId == "plan_2"


def test_save_pending_plan_requires_plan_id(service: SessionService):
    with pytest.raises(ValueError):
        service.save_pending_plan("sess_1", _plan(planId="  "))


def test_save_pending_plan_fills_created_at(service: SessionService):
    saved = service.save_pending_plan("sess_1", _plan())
    assert saved.createdAt


# ---------- 待确认计划：过期校验 ----------


def test_plan_is_not_stale_when_draft_revision_matches(service: SessionService):
    service.save_pending_plan("sess_1", _plan(draftRevision=3))
    assert service.is_pending_plan_stale("sess_1", 3) is False


def test_plan_is_stale_when_draft_revision_changed(service: SessionService):
    """草稿在计划生成后又被改过，就不能照旧序号执行。"""
    service.save_pending_plan("sess_1", _plan(draftRevision=3))
    assert service.is_pending_plan_stale("sess_1", 4) is True


def test_plan_is_stale_when_current_revision_unknown(service: SessionService):
    """说不清当前草稿版本时按过期处理：宁可重新核对，不可照旧执行。"""
    service.save_pending_plan("sess_1", _plan(draftRevision=3))
    assert service.is_pending_plan_stale("sess_1", None) is True


def test_no_plan_is_never_stale(service: SessionService):
    assert service.is_pending_plan_stale("sess_1", 3) is False


# ---------- 待确认计划：清理 ----------


def test_clear_pending_plan_removes_it(service: SessionService):
    service.save_pending_plan("sess_1", _plan())
    service.clear_pending_plan("sess_1")

    assert service.load_pending_plan("sess_1") is None


def test_clear_pending_plan_keeps_other_snapshot_content(service: SessionService):
    service.save_pending_plan("sess_1", _plan())
    service.repository.upsert_session(
        session_id="sess_1",
        state="USER_EDITING_TEMPLATE",
        snapshot={"definition": {"metadata": {"revision": 3}}},
    )
    service.save_pending_plan("sess_1", _plan())
    service.clear_pending_plan("sess_1")

    record = service.repository.get_session("sess_1")
    assert record.snapshot["definition"]["metadata"]["revision"] == 3
    assert PENDING_PLAN_KEY not in record.snapshot


def test_clearing_plan_does_not_touch_clarification_count(service: SessionService):
    """清待决计划是用户的选择动作，跟追问轮数是两回事。"""
    service.save_pending_plan("sess_1", _plan())
    service.record_clarification("sess_1", key="change_region_2", question="改哪个？")

    service.clear_pending_plan("sess_1")

    state = service.load_clarification("sess_1")
    assert state.rounds == 1


# ---------- 澄清轮次 ----------


def test_clarification_rounds_increment_for_same_item(service: SessionService):
    first = service.record_clarification("sess_1", key="change_region_2", question="改哪个？")
    second = service.record_clarification("sess_1", key="change_region_2", question="改到几点？")
    third = service.record_clarification("sess_1", key="change_region_2", question="还是昨天？")

    assert (first.rounds, second.rounds, third.rounds) == (1, 2, 3)


def test_clarification_defaults_to_asked(service: SessionService):
    """默认按「真的追问了用户」计数。"""
    state = service.record_clarification("sess_1", key="change_region_2", question="改哪个？")
    assert state.rounds == 1
    assert state.lastQuestion == "改哪个？"

    explicit = service.record_clarification(
        "sess_1", key="change_region_2", question="再问？", outcome=CLARIFICATION_ASKED
    )
    assert explicit.rounds == 2


def test_clarification_resets_for_a_new_requirement(service: SessionService):
    """换一项待定修改 = 新需求，轮次重新计数。"""
    service.record_clarification("sess_1", key="change_region_2", question="改哪个？")
    service.record_clarification("sess_1", key="change_region_2", question="改到几点？")

    fresh = service.record_clarification("sess_1", key="remove_region_3", question="删哪张？")
    assert fresh.rounds == 1
    assert fresh.key == "remove_region_3"


def test_technical_failure_does_not_consume_a_round(service: SessionService):
    """工具或模型故障不是用户澄清，不占追问轮次。"""
    service.record_clarification("sess_1", key="change_region_2", question="改哪个？")

    failed = service.record_clarification(
        "sess_1",
        key="change_region_2",
        outcome=CLARIFICATION_TECHNICAL_FAILURE,
    )
    assert failed.rounds == 1

    after = service.record_clarification("sess_1", key="change_region_2", question="改到几点？")
    assert after.rounds == 2


def test_technical_failure_before_any_question_keeps_zero_rounds(service: SessionService):
    failed = service.record_clarification(
        "sess_1",
        key="change_region_2",
        outcome=CLARIFICATION_TECHNICAL_FAILURE,
    )
    assert failed.rounds == 0
    assert service.load_clarification("sess_1") is None


def test_technical_failure_does_not_reset_other_item_counter(service: SessionService):
    service.record_clarification("sess_1", key="change_region_2", question="改哪个？")
    service.record_clarification("sess_1", key="change_region_2", question="改到几点？")

    service.record_clarification(
        "sess_1",
        key="change_region_2",
        outcome=CLARIFICATION_TECHNICAL_FAILURE,
    )

    state = service.load_clarification("sess_1")
    assert state.rounds == 2
    assert state.key == "change_region_2"


def test_can_ask_clarification_stops_after_three_rounds(service: SessionService):
    key = "change_region_2"
    assert service.can_ask_clarification("sess_1", key=key) is True

    for _ in range(3):
        service.record_clarification("sess_1", key=key, question="再问一次？")

    assert service.can_ask_clarification("sess_1", key=key) is False


def test_can_ask_clarification_is_true_for_a_new_item(service: SessionService):
    """另一项待定修改的轮次不占这项的预算。"""
    for _ in range(3):
        service.record_clarification("sess_1", key="change_region_2", question="再问一次？")

    assert service.can_ask_clarification("sess_1", key="remove_region_3") is True


def test_clarification_requires_a_key(service: SessionService):
    with pytest.raises(ValueError):
        service.record_clarification("sess_1", key="  ", question="改哪个？")


def test_reset_clarification_clears_the_counter(service: SessionService):
    service.record_clarification("sess_1", key="change_region_2", question="改哪个？")
    service.reset_clarification("sess_1")

    assert service.load_clarification("sess_1") is None
    assert service.can_ask_clarification("sess_1", key="change_region_2") is True


def test_clarification_count_is_stored_in_session_snapshot(service: SessionService):
    service.record_clarification("sess_1", key="change_region_2", question="改哪个？")

    record = service.repository.get_session("sess_1")
    assert record.snapshot[CLARIFICATION_COUNT_KEY] == 1
    assert record.snapshot[CLARIFICATION_KEY_KEY] == "change_region_2"


# ---------- 与落库的配合 ----------


def _response(revision: int = 3) -> AgentResponse:
    return AgentResponse(
        conversationId="conv_1",
        state=AgentState.USER_EDITING_TEMPLATE,
        reply="好的",
        domainObjects={"reportDefinitionDraft": {"metadata": {"revision": revision}}},
    )


def _request() -> AgentRequest:
    return AgentRequest(
        conversationId="conv_1",
        state=AgentState.USER_EDITING_TEMPLATE,
        message="把能耗趋势改成昨天下午",
        context={"projectId": "p1", "subjectRef": "room", "subjectId": "a"},
    )


def test_next_turn_does_not_wipe_the_pending_plan(service: SessionService):
    """下一轮对话重建 snapshot 时，待确认计划必须还在。"""
    service.save_pending_plan("sess_1", _plan(draftRevision=3))
    service.record_clarification("sess_1", key="change_region_2", question="改到几点？")

    service.record_turn(
        session_id="sess_1",
        user_text="把能耗趋势改成昨天下午",
        response=_response(revision=3),
        request=_request(),
    )

    plan = service.load_pending_plan("sess_1")
    assert plan is not None
    assert plan.planId == "plan_1"
    assert plan.draftRevision == 3

    state = service.load_clarification("sess_1")
    assert state.rounds == 1


def test_next_turn_keeps_the_clarification_count_when_it_is_asked_again(
    service: SessionService,
):
    service.record_clarification("sess_1", key="change_region_2", question="改到几点？")

    service.record_turn(
        session_id="sess_1",
        user_text="昨天下午",
        response=_response(),
        request=_request(),
    )

    state = service.load_clarification("sess_1")
    assert state.rounds == 1
    assert state.key == "change_region_2"


def test_plan_written_during_a_turn_is_not_lost(service: SessionService):
    """落库重建 snapshot 时，本轮处理过程中写入的待确认计划不能被覆盖掉。

    snapshot 是整块读写的：如果编辑态状态在「读」和「写」之间取值，
    这期间存进去的计划就会被写回时冲掉。
    """
    service.save_pending_plan("sess_1", _plan(planId="plan_before"))

    original_build = service._build_snapshot

    def build_and_save(request, response):
        # 模拟：本轮处理期间另一个请求存了新的待确认计划
        service.save_pending_plan("sess_1", _plan(planId="plan_during"))
        return original_build(request, response)

    service._build_snapshot = build_and_save  # type: ignore[method-assign]
    try:
        service.record_turn(
            session_id="sess_1",
            user_text="把能耗趋势改成昨天下午",
            response=_response(revision=3),
            request=_request(),
        )
    finally:
        service._build_snapshot = original_build  # type: ignore[method-assign]

    plan = service.load_pending_plan("sess_1")
    assert plan is not None
    assert plan.planId == "plan_during"


def test_plan_survives_concurrent_turns_and_saves(service: SessionService):
    """落库和存计划并发时，刚存进去的计划不能被落库冲掉。

    snapshot 整块读写，读和写之间必须是一个原子步骤。
    """
    service.save_pending_plan("sess_1", _plan(planId="plan_before"))
    stop = threading.Event()
    errors: list[BaseException] = []

    def turns() -> None:
        try:
            while not stop.is_set():
                service.record_turn(
                    session_id="sess_1",
                    user_text="把能耗趋势改成昨天下午",
                    response=_response(revision=3),
                    request=_request(),
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def saves() -> None:
        try:
            for _ in range(50):
                service.save_pending_plan("sess_1", _plan(planId="plan_during"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    turn_thread = threading.Thread(target=turns)
    turn_thread.start()
    try:
        saves()
    finally:
        stop.set()
        turn_thread.join(timeout=10)

    assert errors == []
    plan = service.load_pending_plan("sess_1")
    assert plan is not None
    assert plan.planId == "plan_during"


def test_commit_does_not_lose_a_plan_saved_concurrently(
    service: SessionService,
):
    """落库重建 snapshot 时，并发存进去的计划不能被冲掉。

    计划已经独立成列（sessions.pending_edit_plan），落库只写 snapshot，
    两边不碰同一块数据，所以这里要断言的是最终状态：
    落库期间存进去的计划必须还在。
    """
    service.save_pending_plan("sess_1", _plan(planId="plan_before"))

    in_read = threading.Event()
    commit_finished = threading.Event()
    original_read = service._read_edit_state

    def read_edit_state(session_id):
        result = original_read(session_id)
        in_read.set()
        # 落库还没写回，给另一个线程一个机会插进来存计划
        threading.Event().wait(0.5)
        return result

    def saver() -> None:
        in_read.wait(timeout=2.0)
        service.save_pending_plan("sess_1", _plan(planId="plan_during"))
        commit_finished.wait(timeout=5.0)

    service._read_edit_state = read_edit_state  # type: ignore[method-assign]
    thread = threading.Thread(target=saver)
    thread.start()
    try:
        service.record_turn(
            session_id="sess_1",
            user_text="把能耗趋势改成昨天下午",
            response=_response(revision=3),
            request=_request(),
        )
    finally:
        commit_finished.set()
        service._read_edit_state = original_read  # type: ignore[method-assign]
        thread.join(timeout=5)

    plan = service.load_pending_plan("sess_1")
    assert plan is not None
    assert plan.planId == "plan_during"


def test_plan_and_clarification_survive_parallel_writes(service: SessionService):
    """存计划、记追问同时进行，两边都不能丢。"""
    errors: list[BaseException] = []

    def save_plan() -> None:
        try:
            for _ in range(20):
                service.save_pending_plan("sess_1", _plan(planId="plan_x"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def ask() -> None:
        try:
            for _ in range(20):
                service.record_clarification(
                    "sess_1", key="change_region_2", question="改到几点？"
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=save_plan), threading.Thread(target=ask)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    plan = service.load_pending_plan("sess_1")
    assert plan is not None and plan.planId == "plan_x"
    state = service.load_clarification("sess_1")
    assert state is not None and state.rounds == 20
