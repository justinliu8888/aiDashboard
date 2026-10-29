"""审计第二轮：模板态「点同意后的试运行」不再把模拟业务数值落库。

缺陷（GitHub #46 存储隔离条款 + ADR 0004 §1.3 / §5.1）：

    模板态点「同意」后会跑一次试运行，这次试运行用的**也是模拟数据**，
    它的结果却被写进了会话持久化快照的 ``previewPayload`` 键里 ——
    模拟业务数值因此间接入库，违反「模拟业务数据只存在于响应与内存」。

规格依据（issue #46）：

    - 「新模拟结果不得经消息全响应保存、会话快照 ``previewPayload``、
      运行结果缓存落盘或日志正文间接入库。」
    - 「运行状态与运行标识可保存，**模拟运行结果保留内存**；重启后恢复
      根据定义/上下文再生成，不依赖已消失的内存 runId。」
    - 「会话持久化快照走**允许列表**……**不得**采用『按字段名从快照里剔除
      ``previewPayload``』的实现方式 —— 应改为按允许列表只写入允许的键。」

本文件只走**接口层**：真实 ``POST /api/agent/chat``、真实编排、真实服务、
真实 SQLite（每条用例一份独立临时库），**只替换外部模型调用**这一处。
夹具与扫描器都**复用仓库既有的**：

    - 夹具：``tests/support/acceptance_harness.py``；
    - 扫描器：``tests/test_template_mode_acceptance.py`` 的
      ``_persisted_business_values``（逐表逐格找业务结果位置上的数值）。

断言里不写死任何具体数值 —— 规格明确「不断言造数器的内部函数、
不断言哈希算法与具体数值」。
"""
from __future__ import annotations

import json
from typing import Any

from app.services.preview_service import preview_service
from app.services.report_draft_service import build_definition_draft
from app.services.session_service import session_service
from tests.support.acceptance_harness import (
    REPORT_PREVIEW_PAYLOAD_FIELD,
    SNAPSHOT_ALLOWED_KEYS,
    acceptance_harness,
)
from tests.test_template_mode_acceptance import (
    _collect_keys,
    _payload_numbers,
    _persisted_business_values,
)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _legacy_definition() -> dict[str, Any]:
    """一份**非模板态**的定义（没有 ``metadata.templateBinding``）。

    这就是既有链路的定义形态：历史会话 / 旧链路的草稿不带模板绑定，
    它的试运行结果按原语义落库（规格 #46：既有 ``previewPayload`` 的原语义）。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a").model_dump(mode="json")
    assert "templateBinding" not in definition["metadata"]
    return definition


def _business_values_in_db(harness: Any) -> list[tuple[str, str, Any]]:
    """把临时库里**所有表**的所有行扫一遍，返回业务结果位置上的数值。

    用的是仓库既有的扫描器（票08 的验收用例用的同一个），
    不另起炉灶：口径、排除项、歧义键的判定都跟既有验收一致。
    """
    return _persisted_business_values(harness.dump_all_tables())


def _run_template_trial(harness: Any) -> dict[str, Any]:
    """跑完整链路：需求 → 确认方案 → 同意 → 校验 → 试运行。

    返回点同意那一轮响应里的示意载荷（``reportPreviewPayload``）。
    """
    plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
    confirmed = harness.confirm_plan(plan)
    harness.agree_draft()
    for state in ("VALIDATING", "PREVIEWING", "PREVIEW_READY"):
        harness.advance(state)
    return confirmed[REPORT_PREVIEW_PAYLOAD_FIELD]


# --------------------------------------------------------------------------
# 1. 端到端：点同意 + 试运行之后，库里一个模拟业务数值都没有
# --------------------------------------------------------------------------


def test_trial_run_in_template_mode_persists_no_simulated_business_value():
    """需求 → 确认方案 → 同意 → 校验 → 试运行之后，逐表逐格扫库。

    断言（事实，不是推测）：

    1. 试运行确实跑过了（``lastReportRun`` 落库、响应里有运行预览）；
    2. 库里**没有任何**业务结果位置上的数值；
    3. 模拟数值本身（响应里那份）一个都没进库；
    4. 整份区域载荷也不在库里（兜住「拆开存」）。
    """
    with acceptance_harness() as harness:
        before = _run_template_trial(harness)

        # ① 试运行确实跑了：运行标识与运行状态**照旧保存**。
        snapshot = harness.persisted_snapshot()
        assert snapshot["lastReportRun"]["reportRunId"]
        assert snapshot["lastReportRun"]["status"] in {"SUCCEEDED", "PARTIAL"}

        # 扫描器自检：口径没失灵（种进去的业务数值抓得到，版面配置的数字抓不到）。
        planted = {
            "sessions": [
                {
                    "snapshot": json.dumps(
                        {
                            "regions": [{"data": {"value": 123.4, "rows": [{"k": 1}]}}],
                            "page": {"margins": {"top": {"value": 10}}},
                        },
                        ensure_ascii=False,
                    )
                }
            ]
        }
        planted_hits = _persisted_business_values(planted)
        assert planted_hits, "扫描器失灵：种进去的业务数值一个都没抓到"
        assert not any(
            path.endswith("page.margins.top.value")
            for _where, path, _value in planted_hits
        ), "版面配置里的数字被误判成业务数值了"

        # ② 库里**根本没有**业务结果位置：落库的 JSON 里一个业务数值键都没有。
        persisted = _business_values_in_db(harness)
        assert not persisted, persisted

        # ③ 模拟数值也没有落进任何单元。
        values: set[float] = set()
        for region in before["regions"]:
            values |= _payload_numbers(region["payload"])
        assert values, "响应里没有模拟数值，这条用例无法验证存储隔离"
        business_cells = {
            float(value)
            for _where, _path, value in persisted
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        assert not (values & business_cells), sorted(values & business_cells)

        # ④ 整份区域载荷也不在库里（子串比对，兜住「拆开存」的情况）。
        blob = json.dumps(harness.dump_all_tables(), ensure_ascii=False)
        for region in before["regions"]:
            serialized = json.dumps(region["payload"], ensure_ascii=False)
            assert serialized not in blob, region.get("regionId")


def test_template_mode_trial_payload_is_not_written_into_the_persisted_snapshot():
    """模板态的试运行结果**不落库**：``previewPayload`` 不在持久化快照里。

    这是「按来源判断」的结果，不是「按字段名把 ``previewPayload`` 剔掉」——
    下一条用例（非模板态仍然写）就是这条的反证。
    """
    with acceptance_harness() as harness:
        _run_template_trial(harness)

        snapshot = harness.persisted_snapshot()

        assert "previewPayload" not in snapshot
        # 快照里**没有没登记的键**：允许列表机制保持不变。
        session_owned = {"scope", "collectedInfo"}
        unexpected = set(snapshot) - session_owned - set(SNAPSHOT_ALLOWED_KEYS)
        assert not unexpected, unexpected


# --------------------------------------------------------------------------
# 2. 恢复会话：模拟载荷仍然能重新生成，且与点同意前逐字段相等
# --------------------------------------------------------------------------


def test_restored_session_regenerates_the_same_simulated_payload_after_trial():
    """恢复会话时，模拟载荷**按定义与上下文重新生成**，逐字段相等。

    对应规格 #46：「恢复会话时按既有定义与上下文重新生成；确定性种子保证
    同定义同窗口生成结果完全一致」与「模拟运行结果保留内存；重启后恢复
    根据定义/上下文再生成」。
    """
    with acceptance_harness() as harness:
        before = _run_template_trial(harness)

        restored = harness.read_session_snapshot()

        # 示意载荷在恢复响应里，且与点同意前**逐字段相等**。
        payload = restored[REPORT_PREVIEW_PAYLOAD_FIELD]
        assert payload is not None
        assert payload["regions"], "恢复出来的示意载荷是空的"
        assert payload["regions"] == before["regions"]
        assert payload["previewContext"] == before["previewContext"]
        # 重新生成的那份**没有写回**持久化快照。
        assert REPORT_PREVIEW_PAYLOAD_FIELD not in harness.persisted_snapshot()


def test_restored_session_still_serves_the_trial_run_result_from_memory():
    """试运行结果不落库，但恢复会话时**仍然拿得到**（响应级，内存里现取）。

    对应规格 #46「模拟业务数据只存在于响应与内存」：响应里可以有，
    持久化存储里不行。
    """
    with acceptance_harness() as harness:
        _run_template_trial(harness)

        restored = harness.read_session_snapshot()

        # 试运行结果在响应里（``previewPayload`` 仍是试运行结果的键，语义不变）。
        assert restored["previewPayload"]["regions"]
        # 但库里没有它。
        assert "previewPayload" not in harness.persisted_snapshot()


# --------------------------------------------------------------------------
# 3. 非模板态：既有的 ``previewPayload`` 语义不变（照旧写入）
# --------------------------------------------------------------------------


def test_non_template_trial_result_is_still_persisted():
    """非模板态（既有预览链路）：``previewPayload`` **照旧按原语义写入**。

    这是「不是按字段名剔除 ``previewPayload``」的反证：如果实现是按字段名
    把这个键剔掉，这条链路会一起受害。

    做法：造一条**历史会话**（模板态改造之前落库的会话：快照里的定义不带
    ``metadata.templateBinding``），再跑一次试运行。服务端跑试运行时用的
    就是库里那份定义（设计流程会用服务端自己的草稿覆盖客户端带回的快照），
    所以这次试运行是**非模板态**的。
    """
    with acceptance_harness() as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        harness.confirm_plan(plan)
        harness.agree_draft()
        harness.advance("VALIDATING")
        harness.advance("PREVIEWING")

        # 把会话里那份模板态定义换成**非模板态**的定义（= 历史会话的形态）。
        snapshot = harness.persisted_snapshot()
        snapshot["definition"] = _legacy_definition()
        harness.repository.upsert_session(
            session_id=harness.session_id,
            state="PREVIEWING",
            snapshot=snapshot,
        )

        # 这一轮服务端拿**非模板态**的定义跑试运行。
        harness.advance("PREVIEW_READY")

        snapshot = harness.persisted_snapshot()

        # 既有语义照旧：试运行结果落进 ``previewPayload``。
        assert snapshot["previewPayload"]["regions"]
        assert snapshot["previewPayload"]["regions"][0]["payload"]
        # 运行标识与运行状态也照旧保存。
        assert snapshot["lastReportRun"]["reportRunId"]
        # 读会话时拿到的也是同一份（库里那份，不是重新生成的）。
        restored = harness.read_session_snapshot()
        assert restored["previewPayload"]["regions"][0]["payload"] == (
            snapshot["previewPayload"]["regions"][0]["payload"]
        )


def test_non_template_definition_alone_does_not_block_persisting_the_trial_result():
    """判据看的是**这次试运行跑的是哪份定义**，不是「会话是不是模板态过」。

    非模板态的定义（没有 ``metadata.templateBinding``）跑出来的结果，
    按原语义落库；模板态的定义跑出来的（模拟数据）不落库。

    这一条走服务层入口（``session_service.record_turn``）：设计流程会用
    服务端自己的草稿覆盖客户端带回的快照，所以接口层**换不掉**跑试运行的
    那份定义 —— 接口层的等价场景就是上面那条（历史会话）。
    """
    legacy = _legacy_definition()
    preview, _record = preview_service.run_preview(
        definitionId="def_legacy_audit", definitionDraft=legacy
    )

    with acceptance_harness() as harness:
        from app.schemas.agent import AgentResponse, AgentResponseDomainObjects, AgentState
        from app.schemas.report_definition import ReportDefinitionDraft

        response = AgentResponse(
            conversationId=harness.conversation_id,
            state=AgentState.PREVIEW_READY,
            reply="预览已生成。",
            domainObjects=AgentResponseDomainObjects(
                reportDefinitionDraft=ReportDefinitionDraft(**legacy),
                reportRunPreview=preview,
            ),
        )
        session_service.record_turn(
            session_id=harness.session_id,
            user_text="预览",
            response=response,
            request=None,
        )

        snapshot = harness.persisted_snapshot()

        assert snapshot["previewPayload"]["regions"]
        assert snapshot["lastReportRun"]["reportRunId"] == preview.reportRunId


# --------------------------------------------------------------------------
# 4. 上下文、定义、方案照旧落库（恢复要靠它们）
# --------------------------------------------------------------------------


def test_template_mode_context_definition_and_plan_are_still_persisted():
    """模板态不落的是**业务数值**；上下文、定义、方案照旧落库。

    对应规格 #46 的允许列表：「只写入这些键：``definition``、``policy``、
    ``reportPlan``、``templateDesign``、``designPhase``、``previewContext``」。
    它们不是业务数值 —— 恢复会话要靠它们把同样的数重新生成出来。
    """
    with acceptance_harness() as harness:
        before = _run_template_trial(harness)

        snapshot = harness.persisted_snapshot()

        # ① 上下文（冻结的时间窗等结构）落库，且与响应里那份一致。
        assert snapshot["previewContext"] == before["previewContext"]
        # 上下文里只有结构，没有业务数值。
        assert not (_collect_keys(snapshot["previewContext"]) & {"value", "values", "rows", "points"})

        # ② 定义落库（模板态的绑定信息也在，恢复靠它认版本）。
        definition = snapshot["definition"]
        assert definition["metadata"]["templateBinding"]
        assert definition["regions"]

        # ③ 方案与设计阶段落库。
        assert snapshot["reportPlan"]["id"]
        assert snapshot["designPhase"]
        assert snapshot["templateDesign"]["regions"]
        # ④ 策略也在。
        assert snapshot["policy"]
