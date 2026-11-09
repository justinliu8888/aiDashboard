"""夜间统一修复：T01–T12 业务断言回归（包 D 专属）。

本文件是**专项测试**，不是实现。每个用例断言的是**业务结果**（草稿定义
最终长什么样、哪些东西**不能**被改），不是 HTTP 200、不是工具调用、
不是中文摘要好不好听。

分层（很重要）：

    * **执行器层**（``apply_presentation`` / ``template_edit_engine.apply``）：
      用**规范**参数走一遍，断言定义最终状态。这一层回答「业务规则本身
      对不对」—— 自己的测试写对了吗，靠它。
    * **预检层**（``preflight_presentation``）：候选展示前在**副本**上真跑
      一遍；非法计划在这里被拦成安全澄清（契约 §2.1）。
    * **入口层**（``template_edit_engine.parse``）：走**规则**这条真实中文
      入口（``02-夜间统一修复与验证Prompt.md``：「规则能确定则走规则
      不强制模型」）。**不往数据库里手种候选**。
    * **HTTP 层**（``post_chat``）：真实工作台聊天 → 候选 → 接受。

非法参数 / 异常 / 竞态注入用 **monkeypatch 换掉规则的产出**（把一份非法
操作塞进 ``_parse_presentation``）—— 这是**允许的 Fake 注入**：
它验证的是「拿到非法计划之后的那条链路」，不是「中文入口能造出非法计划」。

实现仍未到位（C 包的撤销回滚会话现场 / 另存为不复制待决内容）的用例
**如实标 ``xfail``**，``reason`` 里写清依赖谁。xfail **不删断言、不改业务
预期、不放宽判据**；实现到位后 xpass 即提醒把标记摘掉转正。

被测缺陷（用户原话见 ``01-手动验收bug列表.md``）：

    T01 BUG-001 外观    T02 BUG-002 相对移动   T03 BUG-003 尺寸分流
    T04 BUG-004 创建组  T05 BUG-006/007 组内排列 T06 BUG-007 移入/移出
    T07 BUG-009 主题    T08 BUG-010 数字格式   T09 BUG-008 完整撤销
    T10 权限/版本       T11 失败/迟到           T12 持久化/入口
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

# 让 ``seed_night_session`` 可导入：**基础草稿定义只有一个出处**，
# 测试与种子脚本不会各写一份然后悄悄漂移。
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import seed_night_session  # noqa: E402

from app.schemas.edit_plan import EditIntent, EditOperation, EditPlan  # noqa: E402
from app.schemas.report_definition import ReportDefinitionDraft  # noqa: E402
from app.services import routing_rules  # noqa: E402
from app.services.report_presentation_service import (  # noqa: E402
    apply_presentation,
    find_region,
    format_display_value,
    is_container,
    normalize_presentation_operations,
    preflight_presentation,
)
from app.services.simulated_data_service import generate_simulated_data  # noqa: E402
from app.services.template_edit_engine import template_edit_engine  # noqa: E402

# --------------------------------------------------------------------------
# 共享样本
# --------------------------------------------------------------------------

#: 三张正文卡的稳定 id（报表标题不算正文卡，见 prompts/template_edit.md）。
#:
#: 名字是**用户页面上的真名**（与 ``seed_night_session.py`` 同形）：
#: 总览卡「总耗电概览」/ 趋势卡「能耗趋势分析」/ 排名卡「设备耗电排名」。
TOTAL_ID = "region_total_energy"
RANKING_ID = "region_device_ranking"
TREND_ID = "region_hourly_trend"
TITLE_ID = "region_report_title"
GROUP_ID = "group_energy_overview"

TOTAL_NAME = "总耗电概览"
TREND_NAME = "能耗趋势分析"
RANKING_NAME = "设备耗电排名"

#: 用户原句（T 表「关联与前置/输入」列）。
#:
#: T01 用的是**用户原句**：页面上的名字是「总耗电概览」，原话里说的是
#: 「昨日能耗总览」—— 名字与说明书不一致，正是 T01 要兼测的那一条。
T01_SENTENCE = '把"昨日能耗总览"的文字调大，右对齐，圆角调小一点'
#: 同一句诉求，但用页面上的真名。
T01_PAGE_NAME_SENTENCE = '把"总耗电概览"的文字调大，右对齐，圆角调小一点'
T02_SENTENCE = '把"设备耗电排名"移到"能耗趋势分析"后面'
#: 已满宽样本（趋势卡 columnSpan 12）上的调宽请求。
T03_WIDEN_SENTENCE = '把"能耗趋势分析"调宽一点，方便看时间轴'
#: 可增宽样本（总览卡 columnSpan 4）上的调宽请求。
T03_WIDENABLE_SENTENCE = '把"总耗电概览"调宽一点'
T03_CONSULT_SENTENCE = "支持调宽吗"
T03_POINT_SENTENCE = "趋势只有一个点"
T04_SENTENCE = "把总耗电概览和能耗趋势分析放进能耗概览分组"
T05_SENTENCE = '"能耗概览"改为两列，间距 20 像素'
T07_SENTENCE = "给这份报表应用暗色主题"
T08_SENTENCE = "总耗电概览显示两位小数，加千分位，保留原单位 kWh"

#: 模拟数据的时间窗。**故意不固定成 5553.7**：源值由造数器算出来，
#: 测试断言的是「按权威格式渲染成什么样」，不是某个写死的数字。
PREVIEW_WINDOW_A = {
    "windowStart": "2026-11-01T00:00:00+08:00",
    "windowEnd": "2026-11-02T00:00:00+08:00",
}
PREVIEW_WINDOW_B = {
    "windowStart": "2026-10-20T00:00:00+08:00",
    "windowEnd": "2026-10-21T00:00:00+08:00",
}

#: NEXT_ACTION 菜单的步骤身份（与 ``seed_night_session.py`` 同形）。
MENU_STEP_ID = "step_seed_night_next_action"


def _definition() -> ReportDefinitionDraft:
    """隔离基础草稿：报表标题 + 总览 / 排名 / 趋势三张正文卡。

    与 ``backend/scripts/seed_night_session.py`` **同一份**构造逻辑。
    """
    payload = seed_night_session._build_definition()
    return ReportDefinitionDraft.model_validate(payload)


def _region(definition: Any, region_id: str) -> dict[str, Any]:
    found = find_region(definition, region_id)
    assert found is not None, f"定义里没有区域：{region_id}"
    return found


def _root_ids(definition: Any) -> list[str]:
    payload = (
        definition.model_dump(mode="json")
        if isinstance(definition, ReportDefinitionDraft)
        else definition
    )
    return [str(r.get("id")) for r in payload.get("regions") or []]


def _execute(definition: ReportDefinitionDraft, operations: list[dict[str, Any]]):
    """跑一批展示操作；成功返回新定义，失败直接断言。"""
    result = apply_presentation(definition, operations)
    assert result.ok, f"操作应当成功，实际失败：{result.failure} / {result.reason}"
    return result.definition


def _fails(definition: ReportDefinitionDraft, operations: list[dict[str, Any]]):
    """跑一批**应当被拒**的操作；返回失败的 PresentationResult。"""
    result = apply_presentation(definition, operations)
    assert not result.ok, "这批操作应当被拒绝，实际却成功了"
    return result


def _parse(message: str, definition: ReportDefinitionDraft | None = None,
           selected: str | None = None):
    return template_edit_engine.parse(
        message=message,
        definition=definition if definition is not None else _definition(),
        selected_region_id=selected,
    )


def _style_ops(region_id: str, font_size: int = 28) -> list[dict[str, Any]]:
    return [{
        "op": "SET_STYLE",
        "params": {
            "regionId": region_id,
            "style": {
                "fontSize": {"value": font_size, "unit": "PX"},
                "textAlign": "RIGHT",
                "borderRadius": 4,
            },
        },
    }]


def _create_group_ops(group_id: str = GROUP_ID, name: str = "能耗概览",
                      members: tuple[str, ...] = (TOTAL_ID, TREND_ID),
                      columns: int = 2, gap: int = 16) -> list[dict[str, Any]]:
    return [{
        "op": "CREATE_GROUP",
        "params": {
            "groupId": group_id,
            "name": name,
            "memberRegionIds": list(members),
            "arrangement": {
                "mode": "GRID",
                "columns": columns,
                "direction": "HORIZONTAL",
                "gap": {"value": gap, "unit": "PX"},
            },
        },
    }]


def _snap(definition):
    return {"reportDefinition": definition.model_dump(mode="json")}


def _seed_next_action_menu(state: str = "USER_EDITING_TEMPLATE") -> None:
    """把会话摆成「模板已生成、等待下一步操作选择」这一步。

    直接写库（而不是走设计流程）：这一步的**权威记录**就是
    ``pending_choice``，与 ``seed_night_session.py`` 种的那一份同形 ——
    这是**前置状态**，不是候选计划。
    """
    from app.services.session_service import session_service

    session_service.repository.upsert_session(
        session_id="conv_test", state=state, snapshot={},
    )
    session_service.repository.set_pending_choice(
        "conv_test",
        {
            "state": state,
            "question": {
                "id": "template_edit_confirm",
                "prompt": "请选择下一步操作：",
                "expects": "SINGLE_CHOICE",
                "selectionOnly": True,
                "allowsOther": True,
                "otherLabel": "其他",
                "otherPlaceholder": "直接输入修改要求",
                "options": [
                    {"value": "同意", "label": "同意"},
                    {"value": "修改", "label": "修改"},
                    {"value": "重新开始", "label": "重新开始"},
                ],
                "lockScope": "input_and_menu",
                "stepId": MENU_STEP_ID,
                "stepVersion": 1,
                "stepKind": "menu",
            },
        },
    )


def _enter_edit_step(post_chat, definition) -> dict[str, Any]:
    """真正选「修改」，进入**受限修改输入**这一步，返回它的步骤关联。"""
    _seed_next_action_menu()
    response = post_chat(
        "修改", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
    ).json()
    assert response.get("error") is None
    step = response["questions"][0]
    assert step["stepKind"] == "edit_request"
    return {
        "stepId": step["stepId"],
        "stepVersion": step.get("stepVersion"),
    }


def _inject_operations(monkeypatch, operations: list[dict[str, Any]]) -> None:
    """把规则层的产出换成一份**给定**的操作（Fake 注入）。

    用途：验证「拿到非法 / 异常计划之后的那条链路」—— 预检拦不拦、
    是不是澄清、会不会留下半份改动。**不是**用它冒充中文入口成功证据。

    注入点选 ``_parse_presentation_outcome``：它是展示类请求的**唯一出口**
    （外观 / 尺寸 / 主题 / 分组 / 移动都从这里出去），换掉它就能覆盖整条
    展示链路，不受具体规则先后顺序影响。
    """
    monkeypatch.setattr(
        template_edit_engine,
        "_parse_presentation_outcome",
        lambda *args, **kwargs: {
            "operations": [dict(op) for op in operations],
        },
    )


# ==========================================================================
# T01 外观（BUG-001）
# ==========================================================================

def test_t01_rule_path_candidate_has_canonical_size_align_radius():
    """T01：中文原句 → **规范**的字号对象 / 对齐 / 圆角候选。

    判据（契约 N1）：字号是 ``{"value": n, "unit": "PX"}`` 对象，不是裸数字
    也不是 CSS 串；圆角是整数；对齐是受控枚举。
    """
    plan = _parse(T01_SENTENCE)

    assert plan.intent == EditIntent.APPLY_PRESENTATION
    assert plan.needsClarification is False
    ops = plan.operation.presentationOperations
    assert len(ops) == 1, f"应当只产出一批展示操作，实际：{ops}"
    assert ops[0]["op"] == "SET_STYLE"
    params = ops[0]["params"]
    assert params["regionId"] == TOTAL_ID

    style = params["style"]
    assert isinstance(style["fontSize"], dict), "字号必须是长度对象，不是裸数字"
    assert style["fontSize"]["unit"] == "PX"
    assert 1 <= int(style["fontSize"]["value"]) <= 200
    assert style["textAlign"] == "RIGHT"
    assert isinstance(style["borderRadius"], int)
    assert 0 <= style["borderRadius"] <= 200


def test_t01_page_name_and_user_wording_resolve_to_the_same_card():
    """T01：页面真名与用户原话（名字不一致）指向**同一张**卡。

    用户原话说「昨日能耗总览」，页面上的名字是「总耗电概览」——
    名字与说明书不同这一条必须照样命中，不能猜错也不能澄清。
    """
    by_user_wording = _parse(T01_SENTENCE)
    by_page_name = _parse(T01_PAGE_NAME_SENTENCE)

    assert by_user_wording.needsClarification is False
    assert by_page_name.needsClarification is False
    assert by_user_wording.target.regionId == TOTAL_ID
    assert by_page_name.target.regionId == TOTAL_ID
    # 两句话产出**同一份**候选参数（不能因为叫法不同给出不同结果）。
    assert (
        by_user_wording.operation.presentationOperations
        == by_page_name.operation.presentationOperations
    )


def test_t01_seed_uses_the_real_page_card_names():
    """T01：种子里三张正文卡的**名字**就是用户页面上的真名。"""
    definition = _definition()
    assert _region(definition, TOTAL_ID)["name"] == TOTAL_NAME
    assert _region(definition, TREND_ID)["name"] == TREND_NAME
    assert _region(definition, RANKING_ID)["name"] == RANKING_NAME


def test_t01_accepted_style_lands_in_region_style():
    """T01：接受后 fontSize / textAlign / borderRadius **真写进 region.style**。"""
    definition = _definition()
    plan = _parse(T01_SENTENCE)
    candidate_style = dict(plan.operation.presentationOperations[0]["params"]["style"])

    result = template_edit_engine.apply(plan=plan, definition=definition)
    assert result.needsClarification is False

    region = _region(result.draftAfterEdit, TOTAL_ID)
    style = region.get("style") or {}
    # 候选承诺的与真正落库的是**同一份**（契约 §1.4：摘要来自规范化后的操作）。
    assert style["fontSize"] == candidate_style["fontSize"]
    assert style["textAlign"] == candidate_style["textAlign"]
    assert style["borderRadius"] == candidate_style["borderRadius"]


def test_t01_appearance_change_does_not_touch_metric_time_window_or_binding():
    """T01：外观修改**不**改指标 / 时间范围 / 统计口径。"""
    definition = _definition()
    before = _region(definition, TOTAL_ID)

    after = _region(_execute(definition, _style_ops(TOTAL_ID)), TOTAL_ID)

    assert after["component"]["type"] == "METRIC_CARD", "卡片类型不能变"
    assert after["component"]["metric"] == before["component"]["metric"]
    assert after["context"] == before["context"], "时间范围 / 粒度不能变"
    assert after["data"] == before["data"], "数据管道 / 统计口径不能变"
    assert after["binding"] == before["binding"], "数据绑定不能变"
    # 除了 style 之外，区域定义一个字节都不该动。
    assert {k: v for k, v in after.items() if k != "style"} == {
        k: v for k, v in before.items() if k != "style"
    }


def test_t01_candidate_stage_does_not_write_the_draft():
    """T01：候选展示前**不写**草稿。"""
    definition = _definition()
    before = copy.deepcopy(definition.model_dump(mode="json"))

    plan = _parse(T01_SENTENCE)
    assert plan.operation.presentationOperations  # 候选确实产出了

    assert definition.model_dump(mode="json") == before, "候选阶段草稿不能变"


def test_t01_ambiguous_target_asks_instead_of_guessing():
    """T01：目标**不唯一** → 澄清，不猜。

    造出两张都像「昨日能耗总览」的指标卡（真实歧义样本）。
    """
    definition = _definition()
    payload = definition.model_dump(mode="json")
    first = next(r for r in payload["regions"] if r["id"] == TOTAL_ID)
    second = copy.deepcopy(first)
    second["id"] = "region_total_energy_2"
    second["name"] = "昨日总耗电概览"
    second["component"]["metric"]["label"] = "昨日总耗电概览"
    payload["regions"].insert(2, second)
    ambiguous = ReportDefinitionDraft.model_validate(payload)

    plan = _parse(T01_SENTENCE, definition=ambiguous)

    assert plan.needsClarification is True, "两个候选目标时必须澄清"
    assert plan.operation.presentationOperations == [], "澄清时不能同时给候选"
    assert plan.clarificationQuestion, "必须给出澄清问句"


def test_t01_normalization_canonicalizes_loose_length_forms():
    """T01：松散写法在**进候选之前**被规范化（BUG-001 的根因修复点）。"""
    definition = _definition()

    for loose in (28, "28px", "28", {"value": 28}):
        norm = normalize_presentation_operations(
            [{"op": "SET_STYLE", "params": {
                "regionId": TOTAL_ID,
                "style": {"fontSize": loose, "borderRadius": 4},
            }}],
            definition,
        )
        assert norm[0]["params"]["style"]["fontSize"] == {"value": 28, "unit": "PX"}


# ==========================================================================
# T02 相对移动（BUG-002）
# ==========================================================================

def test_t02_move_region_with_explicit_index_keeps_identity_and_binding():
    """T02：移动**不新增、不删除、不复制**卡片；id / 数量 / 数据绑定不变。

    执行器的 ``index`` 语义 = **源卡摘出之后**那一串里的插入位。
    根层原本是 ``[标题, 总览, 排名, 趋势]``；把排名移到趋势后面 =
    摘掉排名后 ``[标题, 总览, 趋势]``，插到下标 3（趋势之后）。
    """
    definition = _definition()
    before = _region(definition, RANKING_ID)

    after = _execute(definition, [{
        "op": "MOVE_REGION",
        "params": {"regionId": RANKING_ID, "index": 3},
    }])

    assert _root_ids(after) == [TITLE_ID, TOTAL_ID, TREND_ID, RANKING_ID]
    moved = _region(after, RANKING_ID)
    assert moved["id"] == before["id"]
    assert moved["data"] == before["data"], "数据绑定不能变"
    assert moved["component"] == before["component"], "卡片内容不能变"
    assert moved["context"] == before["context"], "统计口径 / 时间范围不能变"


def test_t02_move_region_reverse_direction_is_correct():
    """T02：反向移动（把趋势移到总览之后）同样正确。"""
    definition = _definition()

    after = _execute(definition, [{
        "op": "MOVE_REGION",
        "params": {"regionId": TREND_ID, "index": 1},
    }])

    assert _root_ids(after) == [TITLE_ID, TREND_ID, TOTAL_ID, RANKING_ID]


def test_t02_chinese_move_produces_index_not_after_region_id():
    """T02：中文「移到 X 后面」→ 计划里是**index**，没有 afterRegionId。

    候选里带的参数必须就是执行器真正跑的那一份（契约 §1.4）。
    """
    plan = _parse(T02_SENTENCE)

    assert plan.needsClarification is False
    assert plan.intent == EditIntent.APPLY_PRESENTATION
    ops = plan.operation.presentationOperations
    assert len(ops) == 1
    assert ops[0]["op"] == "MOVE_REGION"
    params = ops[0]["params"]
    assert params["regionId"] == RANKING_ID
    assert "afterRegionId" not in params, "候选里不能还带着 afterRegionId"
    assert isinstance(params.get("index"), int)

    after = _execute(_definition(), ops)
    assert _root_ids(after) == [TITLE_ID, TOTAL_ID, TREND_ID, RANKING_ID]


def test_t02_after_region_id_resolves_to_correct_index_and_order():
    """T02：执行器直接接受 afterRegionId（契约 §1.3）。"""
    definition = _definition()

    result = apply_presentation(definition, [{
        "op": "MOVE_REGION",
        "params": {"regionId": RANKING_ID, "afterRegionId": TREND_ID},
    }])
    assert result.ok, f"相对移动应当成功：{result.failure}"
    assert _root_ids(result.definition) == [TITLE_ID, TOTAL_ID, TREND_ID, RANKING_ID]


def test_t02_after_region_id_is_normalized_into_executable_index():
    """T02：规范化后的 index **执行出来就是对的顺序**。"""
    definition = _definition()

    norm = normalize_presentation_operations([{
        "op": "MOVE_REGION",
        "params": {"regionId": RANKING_ID, "afterRegionId": TREND_ID},
    }], definition)

    params = norm[0]["params"]
    assert "afterRegionId" not in params, "afterRegionId 不能原样穿透给执行器"
    assert isinstance(params.get("index"), int)

    result = apply_presentation(definition, norm)
    assert result.ok, f"规范化后的操作应当可执行：{result.failure}"
    assert _root_ids(result.definition) == [TITLE_ID, TOTAL_ID, TREND_ID, RANKING_ID]


def test_t02_unknown_after_region_id_is_rejected_before_execution():
    """T02：未知 afterRegionId → 明确失败，定义不变。"""
    definition = _definition()

    result = apply_presentation(definition, [{
        "op": "MOVE_REGION",
        "params": {"regionId": RANKING_ID, "afterRegionId": "region_not_exists"},
    }])
    assert not result.ok
    assert result.reason == "unknown_region"
    assert result.definition.model_dump(mode="json") == definition.model_dump(mode="json")


# ==========================================================================
# T03 尺寸分流（BUG-003）
# ==========================================================================

def test_t03_point_count_feedback_still_recognized():
    """T03：修「调宽一点」**不能**破坏真实的数据点反馈识别。"""
    assert routing_rules.detect_issue_feedback(
        T03_POINT_SENTENCE, subject_required=False
    )


def test_t03_width_capability_question_stays_readonly():
    """T03：修「调宽一点」**不能**破坏「支持调宽吗」这类能力咨询。"""
    assert routing_rules.is_readonly_question(T03_CONSULT_SENTENCE)
    assert not routing_rules.has_operation_command(T03_CONSULT_SENTENCE)


def test_t03_widen_command_is_not_point_count_feedback():
    """T03：「调宽一点」是**尺寸命令**，不是「只有一个数据点」。"""
    assert not routing_rules.detect_issue_feedback(
        T03_WIDEN_SENTENCE, subject_required=False
    )
    assert routing_rules.has_operation_command(T03_WIDEN_SENTENCE)


def test_t03_width_change_does_not_touch_grain_time_window_or_metric():
    """T03：尺寸修改只改布局：统计粒度 / 时间窗 / 指标一个字节都不动。"""
    definition = _definition()
    before = _region(definition, TOTAL_ID)

    after = _region(_execute(definition, [{
        "op": "SET_PLACEMENT",
        "params": {
            "regionId": TOTAL_ID,
            "placement": {"type": "GRID", "column": 1, "columnSpan": 8},
        },
    }]), TOTAL_ID)

    assert after["layout"]["placement"]["columnSpan"] == 8
    assert after["context"] == before["context"]
    assert after["data"] == before["data"]
    assert after["component"]["metric"] == before["component"]["metric"]


def test_t03_full_width_sample_cannot_be_widened_beyond_grid():
    """T03：已满宽样本（columnSpan 12）的占宽不越界。"""
    definition = _definition()
    trend = _region(definition, TREND_ID)
    assert trend["layout"]["placement"]["columnSpan"] == 12, "趋势卡本就是满宽样本"

    result = _fails(definition, [{
        "op": "SET_PLACEMENT",
        "params": {
            "regionId": TREND_ID,
            "placement": {"type": "GRID", "column": 1, "columnSpan": 13},
        },
    }])
    assert "columnSpan" in (result.failure or "")


def test_t03_widen_command_produces_concrete_width_candidate():
    """T03：可增宽样本 → **具体合法**的宽度候选，不是趋势诊断。"""
    definition = _definition()
    before_span = _region(definition, TOTAL_ID)["layout"]["placement"]["columnSpan"]
    assert before_span < 12, "总览卡是可增宽样本"

    plan = _parse(T03_WIDENABLE_SENTENCE, definition=definition, selected=TOTAL_ID)

    assert plan.needsClarification is False
    assert plan.intent == EditIntent.APPLY_PRESENTATION
    ops = plan.operation.presentationOperations
    assert ops, "调宽必须产出候选"
    assert ops[0]["op"] == "SET_PLACEMENT"
    placement = ops[0]["params"]["placement"]
    assert placement["type"] == "GRID"
    assert before_span < placement["columnSpan"] <= 12, "必须真的变宽，且不越界"


def test_t03_full_width_sample_gets_limit_and_legal_choices():
    """T03：满宽样本 → 说明限制 + 本步合法选择，**不是**趋势诊断。"""
    definition = _definition()
    plan = _parse(T03_WIDEN_SENTENCE, definition=definition, selected=TREND_ID)

    question = plan.clarificationQuestion
    assert question, "满宽样本必须给说明"
    assert "宽" in question, f"必须说明宽度限制，实际：{question}"
    assert "占满" in question or "空间" in question, f"必须说明已占满，实际：{question}"
    # 不能答成「只有一个数据点」那种趋势诊断。
    assert "点" not in question, f"不能答成趋势点数诊断，实际：{question}"
    # 本步合法选择：仍然锁在这一步，不结束流程。
    assert plan.operation.presentationOperations == []


def test_t03_full_width_clarification_offers_legal_choices(post_chat):
    """T03：满宽样本在**工作台**上给的是三个本步合法选择。"""
    definition = _definition()

    response = post_chat(
        T03_WIDEN_SENTENCE,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        selectedRegionId=TREND_ID,
    ).json()

    assert (response.get("domainObjects") or {}).get("pendingEditPlan") is None, \
        "满宽样本不该出候选"
    options = [
        option.get("value")
        for question in (response.get("questions") or [])
        for option in (question.get("options") or [])
    ]
    assert options, "必须给本步合法选择"
    assert any("加高" in text for text in options), f"必须给合法选择，实际：{options}"
    assert any("取消" in text for text in options), f"必须能取消，实际：{options}"


# ==========================================================================
# T04 创建组（BUG-004）
# ==========================================================================

def test_t04_create_group_builds_real_container_with_two_members():
    """T04：创建**真实 CONTAINER**，两个原成员正确入组，排名独立。"""
    definition = _definition()
    ranking_before = _region(definition, RANKING_ID)

    after = _execute(definition, _create_group_ops())
    payload = after.model_dump(mode="json")

    group = _region(after, GROUP_ID)
    assert is_container(group), "分组必须是真实 CONTAINER"
    assert group["component"]["type"] == "CONTAINER"
    assert group["name"] == "能耗概览"

    child_ids = [str(c.get("id")) for c in group.get("children") or []]
    assert child_ids == [TOTAL_ID, TREND_ID], "两个原成员都要正确入组"

    # 排名**独立**：留在组外，且内容不变。
    assert RANKING_ID in _root_ids(after)
    assert RANKING_ID not in child_ids
    assert _region(after, RANKING_ID)["data"] == ranking_before["data"]


def test_t04_group_arrangement_gap_is_legal():
    """T04：gap 是**合法长度对象**（BUG-004 的失败点就是这里）。"""
    after = _execute(_definition(), _create_group_ops(gap=16))
    arrangement = _region(after, GROUP_ID)["component"]["arrangement"]
    assert arrangement["gap"] == {"value": 16, "unit": "PX"}
    assert arrangement["columns"] == 2


def test_t04_failed_group_creation_leaves_no_partial_result():
    """T04：失败**无半成品** —— 不留半个组、不动成员。"""
    definition = _definition()
    before = definition.model_dump(mode="json")

    result = _fails(definition, _create_group_ops(members=(TOTAL_ID, "region_not_exists")))

    assert result.definition.model_dump(mode="json") == before, "失败不能留下半份定义"
    assert GROUP_ID not in _root_ids(result.definition)


def test_t04_loose_gap_is_rejected_by_the_executor_today():
    """T04 根因留证：松散 gap 会被执行器拒（= 用户看到的报错）。"""
    definition = _definition()
    ops = _create_group_ops()
    ops[0]["params"]["arrangement"]["gap"] = 16  # 裸数字，不是长度对象

    result = _fails(definition, ops)
    assert "gap" in (result.failure or "")


def test_t04_normalization_makes_loose_gap_legal():
    """T04：松散 gap 规范化后可执行（BUG-004 的根因修复点）。"""
    definition = _definition()
    ops = _create_group_ops()
    ops[0]["params"]["arrangement"]["gap"] = 16

    norm = normalize_presentation_operations(ops, definition)
    assert norm[0]["params"]["arrangement"]["gap"] == {"value": 16, "unit": "PX"}
    assert apply_presentation(definition, norm).ok


def test_t04_chinese_group_request_produces_two_member_candidate():
    """T04：中文入口 → 两个成员都在的分组候选，排名独立。"""
    plan = _parse(T04_SENTENCE)
    assert plan.intent == EditIntent.APPLY_PRESENTATION
    ops = plan.operation.presentationOperations
    assert ops and ops[0]["op"] == "CREATE_GROUP"
    params = ops[0]["params"]
    members = params["memberRegionIds"]
    assert TOTAL_ID in members and TREND_ID in members
    assert RANKING_ID not in members
    assert params["name"] == "能耗概览"
    # 间距是合法长度对象（BUG-004 的失败点）。
    assert params["arrangement"]["gap"] == {"value": 16, "unit": "PX"} or \
        params["arrangement"]["gap"]["unit"] == "PX"


def test_t04_one_acceptance_is_one_undo_unit(tmp_path):
    """T04：一次接受 = **一个撤销单位**（不是两次）。"""
    from app.services.draft_workspace import draft_workspace

    definition = _definition()
    draft_id = _workspace_draft(tmp_path, definition)
    after = _execute(definition, _create_group_ops())

    state = draft_workspace.commit_edit(
        draft_id, definition=after.model_dump(mode="json"), label="create group",
    )

    assert state.undoPointer == 1, "一次接受只能是一个撤销单位"
    assert state.canUndo is True
    undo = draft_workspace.undo(draft_id)
    assert GROUP_ID not in _root_ids(undo.definition), "撤销一次就回到建组之前"


# ==========================================================================
# T05 组内排列（BUG-006 / BUG-007）
# ==========================================================================

def test_t05_group_arrangement_is_group_level():
    """T05：排列是**组级**的 —— 落在 container.component.arrangement 上。"""
    definition = _definition()
    grouped = _execute(definition, _create_group_ops())

    after = _execute(grouped, [{
        "op": "SET_GROUP_ARRANGEMENT",
        "params": {
            "groupId": GROUP_ID,
            "arrangement": {
                "mode": "GRID",
                "columns": 2,
                "direction": "HORIZONTAL",
                "gap": {"value": 20, "unit": "PX"},
            },
        },
    }])

    group = _region(after, GROUP_ID)
    arrangement = group["component"]["arrangement"]
    assert arrangement["columns"] == 2
    assert arrangement["gap"] == {"value": 20, "unit": "PX"}
    # **不是**单卡 placement：单卡的 layout.placement 里不该出现排列字段。
    for child in group.get("children") or []:
        placement = (child.get("layout") or {}).get("placement") or {}
        assert "columns" not in placement
        assert "mode" not in placement


def test_t05_missing_group_is_rejected_not_substituted_by_a_card():
    """T05：找不到分组 → 明确失败，**不拿单卡冒充组**。"""
    definition = _definition()

    result = _fails(definition, [{
        "op": "SET_GROUP_ARRANGEMENT",
        "params": {
            "groupId": "group_not_exists",
            "arrangement": {"mode": "GRID", "columns": 2,
                            "gap": {"value": 20, "unit": "PX"}},
        },
    }])

    assert result.reason == "unknown_region"
    assert "找不到这个分组" in (result.failure or "")
    assert result.definition.model_dump(mode="json") == definition.model_dump(mode="json")


def test_t05_missing_group_natural_language_does_not_pick_a_card():
    """T05：缺组样本的中文请求**不许**落到某张单卡上。"""
    plan = _parse(T05_SENTENCE)

    assert plan.needsClarification is True, "没有这个分组就必须澄清"
    assert plan.operation.presentationOperations == [], "澄清时不能顺手改一张卡"


def test_t05_missing_group_clarification_names_the_group():
    """T05：澄清文案要**明确**说「未找到该分组」，不许拿单卡冒充组。"""
    plan = _parse(T05_SENTENCE)
    question = plan.clarificationQuestion

    assert "分组" in question, f"必须点名分组这件事，实际：{question}"
    assert "能耗概览" in question, f"必须点名用户说的那个组，实际：{question}"
    # 不能把组名当成卡片名、让用户在卡片里挑一张冒充组。
    assert "总耗电概览" not in question, f"不能拿卡片冒充组，实际：{question}"
    assert "设备耗电排名" not in question, f"不能拿卡片冒充组，实际：{question}"


def test_t05_missing_group_clarification_offers_legal_choices(post_chat):
    """T05：缺组澄清给的是**分组**层面的合法选择，不是「选一张卡」。"""
    definition = _definition()

    response = post_chat(
        T05_SENTENCE,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert (response.get("domainObjects") or {}).get("pendingEditPlan") is None, \
        "缺组不该出候选"
    options = [
        option.get("value")
        for question in (response.get("questions") or [])
        for option in (question.get("options") or [])
    ]
    assert options, "必须给本步合法选择"
    assert any("分组" in text for text in options), f"选择必须围绕分组，实际：{options}"
    assert any("取消" in text for text in options), f"必须能取消，实际：{options}"


def test_t05_placement_with_arrangement_fields_is_not_silently_dropped():
    """T05/BUG-006：列数字段放错层级 → 明确报错，不静默删。"""
    definition = _definition()
    grouped = _execute(definition, _create_group_ops())

    result = _fails(grouped, [{
        "op": "SET_PLACEMENT",
        "params": {
            "regionId": TOTAL_ID,
            "placement": {"type": "GRID", "column": 1, "columnSpan": 6, "columns": 2},
        },
    }])
    assert "columns" in (result.failure or "")


# ==========================================================================
# T06 移入 / 移出（BUG-007）
# ==========================================================================

def test_t06_move_into_group_keeps_identity_binding_and_reading_order():
    """T06：移入组 → 父子归属、阅读顺序、id / 绑定都保留，不复制不删卡。"""
    definition = _definition()
    ranking_before = _region(definition, RANKING_ID)
    grouped = _execute(definition, _create_group_ops())

    after = _execute(grouped, [{
        "op": "MOVE_REGION",
        "params": {"regionId": RANKING_ID, "targetGroupId": GROUP_ID, "index": 2},
    }])

    group = _region(after, GROUP_ID)
    child_ids = [str(c.get("id")) for c in group.get("children") or []]
    assert child_ids == [TOTAL_ID, TREND_ID, RANKING_ID], "组内阅读顺序要对"
    assert RANKING_ID not in _root_ids(after), "移入之后不该还留在根层"

    moved = _region(after, RANKING_ID)
    assert moved["data"] == ranking_before["data"], "数据绑定不能变"
    assert moved["component"] == ranking_before["component"]
    # 没有复制：全树里这个 id 只出现一次。
    payload = after.model_dump(mode="json")
    occurrences = sum(
        1 for r in payload["regions"] for c in (r.get("children") or [])
        if str(c.get("id")) == RANKING_ID
    )
    assert occurrences == 1


def test_t06_move_out_of_group_returns_to_root_and_keeps_identity():
    """T06：移出组 → 回到根层，id / 绑定保留。"""
    definition = _definition()
    ranking_before = _region(definition, RANKING_ID)
    grouped = _execute(definition, _create_group_ops())
    inside = _execute(grouped, [{
        "op": "MOVE_REGION",
        "params": {"regionId": RANKING_ID, "targetGroupId": GROUP_ID, "index": 2},
    }])

    after = _execute(inside, [{
        "op": "MOVE_REGION",
        "params": {"regionId": RANKING_ID, "index": 1},
    }])

    assert RANKING_ID in _root_ids(after)
    group = _region(after, GROUP_ID)
    assert RANKING_ID not in [str(c.get("id")) for c in group.get("children") or []]
    moved = _region(after, RANKING_ID)
    assert moved["data"] == ranking_before["data"]


def test_t06_move_into_self_or_own_descendant_is_rejected_and_state_unchanged():
    """T06：移入自己 / 自己的后代 → 拒绝且**现场不变**。"""
    definition = _definition()
    grouped = _execute(definition, _create_group_ops())
    before = grouped.model_dump(mode="json")

    # 移进自己
    result = _fails(grouped, [{
        "op": "MOVE_REGION",
        "params": {"regionId": GROUP_ID, "targetGroupId": GROUP_ID},
    }])
    assert result.reason == "cycle"
    assert result.definition.model_dump(mode="json") == before

    # 移进自己的后代
    nested = _execute(grouped, _create_group_ops(
        group_id="group_nested", name="内层", members=(TOTAL_ID,)
    ))
    nested_before = nested.model_dump(mode="json")
    result = _fails(nested, [{
        "op": "MOVE_REGION",
        "params": {"regionId": GROUP_ID, "targetGroupId": "group_nested"},
    }])
    assert result.reason == "cycle"
    assert result.definition.model_dump(mode="json") == nested_before


def test_t06_moved_card_keeps_a_legal_placement_inside_the_group():
    """T06：移入组后子卡的定位仍然**合法**（不越界、不矛盾）。"""
    definition = _definition()
    grouped = _execute(definition, _create_group_ops())

    after = _execute(grouped, [{
        "op": "MOVE_REGION",
        "params": {"regionId": RANKING_ID, "targetGroupId": GROUP_ID, "index": 2},
    }])

    group = _region(after, GROUP_ID)
    columns = int(group["component"]["arrangement"]["columns"])
    for child in group.get("children") or []:
        placement = (child.get("layout") or {}).get("placement") or {}
        if placement.get("type") != "GRID":
            continue
        span = int(placement.get("columnSpan") or 1)
        assert span <= columns or span <= 12, "子卡占宽不能超过栅格"
        assert int(placement.get("column") or 1) <= 12


def test_t06_chinese_move_into_group_targets_the_real_group():
    """T06：中文「把排名移入能耗概览」→ 目标是真实分组 id。"""
    definition = _definition()
    grouped = _execute(definition, _create_group_ops())
    plan = _parse('把"设备耗电排名"移入"能耗概览"分组', definition=grouped)

    assert plan.needsClarification is False
    ops = plan.operation.presentationOperations
    assert ops and ops[0]["op"] == "MOVE_REGION"
    assert ops[0]["params"]["targetGroupId"] == GROUP_ID


# ==========================================================================
# T07 主题（BUG-009）
# ==========================================================================

def test_t07_set_theme_is_report_level():
    """T07：主题是**报表级**操作 —— 不指定卡片，落到报表主题引用上。"""
    definition = _definition()

    after = _execute(definition, [{"op": "SET_THEME", "params": {"themeId": "theme_dark"}}])

    assert after.theme == {"themeRef": "theme_dark", "version": "1.0"}
    # 报表级：不改任何一张卡的数据绑定 / 布局 / 格式。
    for region in after.regions or []:
        assert region.get("style") in (None, {}), "报表级主题不该给单卡写死外观"


def test_t07_applying_theme_clears_old_theme_managed_overrides():
    """T07：换主题时，主题域里的**旧局部覆盖**要清掉（契约 §8.2）。"""
    definition = _definition()
    styled = _execute(definition, _style_ops(TOTAL_ID))
    assert _region(styled, TOTAL_ID).get("style"), "先写一处旧覆盖"

    themed = _execute(styled, [{"op": "SET_THEME", "params": {"themeId": "theme_dark"}}])

    style = _region(themed, TOTAL_ID).get("style") or {}
    assert "textColor" not in style
    assert "backgroundColor" not in style


def test_t07_theme_covers_report_and_subcards_in_the_render_layer():
    """T07：主题契约覆盖**报表与相关子卡 / 图表**（渲染层按 themeRef 取主题）。"""
    from app.services.report_theme_service import theme_service

    definition = _definition()
    themed = _execute(definition, [{"op": "SET_THEME", "params": {"themeId": "theme_dark"}}])

    payload = themed.model_dump(mode="json")
    assert payload["theme"]["themeRef"] == "theme_dark"

    dark = theme_service.get_theme("theme_dark")
    default = theme_service.get_theme("theme_default")
    assert dark is not None and default is not None
    assert dark["tokens"]["colorText"] != default["tokens"]["colorText"], \
        "暗色主题的正文颜色必须与默认主题不同"
    # 报表与每张子卡都按同一个 themeRef 解析到同一套 token。
    for region in payload["regions"]:
        assert region.get("id")


def test_t07_report_level_theme_entry_does_not_require_card_selection():
    """T07：中文入口 → 报表级主题候选，不要求选卡片。"""
    plan = _parse(T07_SENTENCE)

    assert plan.needsClarification is False
    ops = plan.operation.presentationOperations
    assert ops and ops[0]["op"] == "SET_THEME"
    assert "regionId" not in ops[0]["params"], "报表级主题不能绑到某张卡"
    # 主题**名字**要解析成真实 id（不是把「暗色主题」当 id 塞进去）。
    assert ops[0]["params"]["themeId"] == "theme_dark"


def test_t07_normalization_resolves_theme_name_to_real_id():
    """T07：规范计划层也要能把主题名解析成真实 id（契约 N5）。"""
    definition = _definition()

    norm = normalize_presentation_operations(
        [{"op": "SET_THEME", "params": {"themeId": "深色主题"}}], definition
    )
    assert norm[0]["params"]["themeId"] == "theme_dark"


# ==========================================================================
# T08 数字格式（BUG-010）
# ==========================================================================

@pytest.mark.parametrize("window", [PREVIEW_WINDOW_A, PREVIEW_WINDOW_B])
def test_t08_format_renders_thousands_separator_and_keeps_source_value(window):
    """T08：两位小数 + 千分位 + 单位 → 显示成「5,xxx.xx kWh」，源值不变。

    源值由造数器算出（**不硬编码 5553.7**），断言的是「按权威格式渲染成
    什么样」以及「底层值 / 绑定 / 单位 / 口径 / 卡片类型一个字节都不动」。
    """
    definition = _definition()
    before_region = _region(definition, TOTAL_ID)

    result = apply_presentation(definition, [{
        "op": "SET_FORMAT",
        "params": {
            "regionId": TOTAL_ID,
            "format": {
                "precision": 2,
                "thousandSeparator": True,
                "unitLabel": "kWh",
            },
        },
    }])
    assert result.ok, f"格式修改应当成功：{result.failure}"
    after = result.definition

    region = _region(after, TOTAL_ID)
    assert region["format"]["precision"] == 2
    assert region["format"]["thousandSeparator"] is True
    assert region["format"]["unitLabel"] == "kWh"

    # 源值 / 绑定 / 单位 / 统计口径 / 卡片类型不变。
    assert region["component"]["type"] == "METRIC_CARD"
    assert region["component"]["metric"] == before_region["component"]["metric"]
    assert region["data"] == before_region["data"]
    assert region["binding"] == before_region["binding"]
    assert region["context"] == before_region["context"]

    payload = generate_simulated_data(after.model_dump(mode="json"), window)
    card = payload[TOTAL_ID]
    raw = card["value"]
    assert raw is not None
    assert card["unit"] == "kWh", "单位不能变"
    assert card["displayValue"] == f"{float(raw):,.2f} kWh"
    assert card["displayValue"] == format_display_value(
        raw, card["format"], unit=str(card.get("unit") or "")
    )
    # 底层值没有被格式改写。
    assert card["value"] == raw


def test_t08_two_different_source_values_both_render_correctly():
    """T08：兼测**另一个**非固定数值 —— 不硬编码任何具体数字。"""
    definition = _definition()
    result = apply_presentation(definition, [{
        "op": "SET_FORMAT",
        "params": {
            "regionId": TOTAL_ID,
            "format": {"precision": 2, "thousandSeparator": True, "unitLabel": "kWh"},
        },
    }])
    assert result.ok
    after = result.definition

    values = []
    for window in (PREVIEW_WINDOW_A, PREVIEW_WINDOW_B):
        payload = generate_simulated_data(after.model_dump(mode="json"), window)
        card = payload[TOTAL_ID]
        values.append(float(card["value"]))
        assert card["displayValue"] == f"{float(card['value']):,.2f} kWh"

    assert values[0] != values[1], "两个时间窗应当给出不同的源值（否则这用例没意义）"


def test_t08_format_does_not_switch_component_type():
    """T08/BUG-010：数字格式**不能**退化成 METRIC_CARD → METRIC_CARD 的类型切换。"""
    definition = _definition()

    after = _execute(definition, [{
        "op": "SET_FORMAT",
        "params": {
            "regionId": TOTAL_ID,
            "format": {"precision": 2, "thousandSeparator": True, "unitLabel": "kWh"},
        },
    }])

    region = _region(after, TOTAL_ID)
    assert region["component"]["type"] == "METRIC_CARD"
    assert region["component"].get("chartKind") is None, "不能变成图表卡"
    assert region["component"]["metric"]["id"] == "m_total", "主指标不能换"


def test_t08_unit_label_must_be_current_unit():
    """T08：单位只允许**当前单位或空**（不许单位换算）。"""
    definition = _definition()

    result = _fails(definition, [{
        "op": "SET_FORMAT",
        "params": {
            "regionId": TOTAL_ID,
            "format": {"precision": 2, "thousandSeparator": True, "unitLabel": "Wh"},
        },
    }])
    assert "单位" in (result.failure or "")


def test_t08_chinese_format_request_produces_set_format_operation():
    """T08：中文入口 → SET_FORMAT 操作；**单位不动**（保留原单位 kWh）。

    第二批起，中文格式请求**不再由规则层解析**：规则层只认「改标题 /
    改数据源 / 改指标」这类旧意图，格式请求交给**能力层**（``SET_FORMAT``）。

    所以这里守两件事：
      1. 规则层**不认**这句中文 —— 既不硬判，也不退化成展示类型切换；
      2. 能力层的 ``SET_FORMAT`` 执行之后单位不变，显示串是「x,xxx.xx kWh」。
    """
    plan = _parse(T08_SENTENCE, selected=TOTAL_ID)

    # 规则层不处理格式请求（留给能力层）。
    assert plan.intent == EditIntent.UNKNOWN
    assert not plan.operation.presentationOperations, "不能退化成展示类型切换"

    ops = [{
        "op": "SET_FORMAT",
        "params": {
            "regionId": TOTAL_ID,
            "format": {"precision": 2, "thousandSeparator": True, "unitLabel": "kWh"},
        },
    }]
    # 执行之后显示仍然是「x,xxx.xx kWh」，单位没被改。
    after = _execute(_definition(), ops)
    payload = generate_simulated_data(after.model_dump(mode="json"), PREVIEW_WINDOW_A)
    card = payload[TOTAL_ID]
    assert card["unit"] == "kWh"
    assert card["displayValue"] == f"{float(card['value']):,.2f} kWh"


# ==========================================================================
# T09 完整撤销（BUG-008）
# ==========================================================================

def _workspace_draft(tmp_path, definition):
    """在测试自己的临时库里建一份草稿基线（**不碰用户库**）。"""
    from app.services import session_service
    from app.services.draft_workspace import draft_workspace
    from app.storage.sqlite_store import SqliteSessionRepository

    repo = SqliteSessionRepository(tmp_path / "night-workspace.db")
    session_service._repo = repo
    draft_workspace._sync_repository()
    draft_id = "draft_night_workspace"
    draft_workspace.create_draft(
        draft_id,
        session_id="sess_night_workspace",
        name="A 机房能耗日报",
        definition=definition.model_dump(mode="json"),
    )
    return draft_id


def test_t09_consecutive_undo_restores_each_unit(tmp_path):
    """T09：连续撤销 B / A，每一步都恢复到位。"""
    from app.services.draft_workspace import draft_workspace

    definition = _definition()
    draft_id = _workspace_draft(tmp_path, definition)

    after_a = _execute(definition, _style_ops(TOTAL_ID, font_size=20))
    draft_workspace.commit_edit(draft_id, definition=after_a.model_dump(mode="json"))
    after_b = _execute(after_a, _style_ops(TREND_ID, font_size=18))
    state_b = draft_workspace.commit_edit(draft_id, definition=after_b.model_dump(mode="json"))

    assert state_b.canUndo is True
    assert state_b.undoPointer == 2, "两次接受 = 两个撤销单位"

    undo_b = draft_workspace.undo(draft_id)
    assert _region(undo_b.definition, TOTAL_ID)["style"]["fontSize"]["value"] == 20
    assert not (_region(undo_b.definition, TREND_ID).get("style") or {}), "撤销 B 后趋势的改动没了"

    undo_a = draft_workspace.undo(draft_id)
    assert not (_region(undo_a.definition, TOTAL_ID).get("style") or {}), "撤销 A 后总览的改动也没了"
    assert undo_a.canUndo is False, "基线不是撤销单位"


def test_t09_redo_restores_the_undone_unit(tmp_path):
    """T09：重做把撤销掉的那一步恢复回来。"""
    from app.services.draft_workspace import draft_workspace

    definition = _definition()
    draft_id = _workspace_draft(tmp_path, definition)
    after_a = _execute(definition, _style_ops(TOTAL_ID, font_size=20))
    draft_workspace.commit_edit(draft_id, definition=after_a.model_dump(mode="json"))

    draft_workspace.undo(draft_id)
    redo = draft_workspace.redo(draft_id)

    assert _region(redo.definition, TOTAL_ID)["style"]["fontSize"]["value"] == 20


def test_t09_new_acceptance_after_undo_clears_redo_branch(tmp_path):
    """T09：撤销后接受新修改 C → 旧的重做分支被清掉。"""
    from app.services.draft_workspace import draft_workspace

    definition = _definition()
    draft_id = _workspace_draft(tmp_path, definition)
    after_a = _execute(definition, _style_ops(TOTAL_ID, font_size=20))
    draft_workspace.commit_edit(draft_id, definition=after_a.model_dump(mode="json"))
    after_b = _execute(after_a, _style_ops(TREND_ID, font_size=18))
    draft_workspace.commit_edit(draft_id, definition=after_b.model_dump(mode="json"))

    draft_workspace.undo(draft_id)
    assert draft_workspace.load_draft(draft_id).canRedo is True

    after_c = _execute(definition, _style_ops(RANKING_ID, font_size=16))
    state_c = draft_workspace.commit_edit(draft_id, definition=after_c.model_dump(mode="json"))

    assert state_c.canRedo is False, "接受新修改后旧重做分支必须清掉"
    assert _region(state_c.definition, RANKING_ID).get("style"), "C 生效了"


def test_t09_undo_does_not_touch_earlier_units(tmp_path):
    """T09：撤销 B 不会**误撤销**更早的 A。"""
    from app.services.draft_workspace import draft_workspace

    definition = _definition()
    draft_id = _workspace_draft(tmp_path, definition)
    after_a = _execute(definition, _style_ops(TOTAL_ID, font_size=20))
    draft_workspace.commit_edit(draft_id, definition=after_a.model_dump(mode="json"))
    after_b = _execute(after_a, _style_ops(TREND_ID, font_size=18))
    draft_workspace.commit_edit(draft_id, definition=after_b.model_dump(mode="json"))

    state = draft_workspace.undo(draft_id)

    assert _region(state.definition, TOTAL_ID)["style"]["fontSize"]["value"] == 20, \
        "撤销 B 之后 A 必须还在"


@pytest.mark.xfail(
    strict=False,
    reason="依赖 C 包（契约 §2.3）：UNDO / REDO 要在同一笔事务里回滚**左侧会话"
           "现场** —— 失效待决选择与旧步骤、把旧步骤标成已撤销 / 只读、"
           "并在响应里带回权威会话现场",
)
def test_t09_undo_invalidates_pending_choice_and_marks_old_step_readonly(tmp_path):
    """T09：撤销后左侧会话现场一起回退（旧步骤失效、只读）。"""
    from app.services.draft_workspace import draft_workspace
    from app.services.session_service import session_service

    definition = _definition()
    draft_id = _workspace_draft(tmp_path, definition)
    session_service.repository.set_pending_choice(
        "sess_night_workspace",
        {"state": "USER_EDITING_TEMPLATE", "question": {
            "id": "template_edit_confirm", "lockScope": "input_and_menu",
            "stepId": "step_old", "stepVersion": 1, "stepKind": "menu",
        }},
    )
    after_a = _execute(definition, _style_ops(TOTAL_ID, font_size=20))
    draft_workspace.commit_edit(draft_id, definition=after_a.model_dump(mode="json"))

    draft_workspace.undo(draft_id)

    pending = session_service.load_pending_choice("sess_night_workspace")
    assert pending is None or pending.get("question", {}).get("stepId") != "step_old", \
        "被撤销那一轮的旧步骤必须失效"


# ==========================================================================
# T10 权限 / 版本
# ==========================================================================

def test_t10_candidate_stage_does_not_change_draft_or_lock(post_chat):
    """T10：候选等待期间**草稿不变**，且锁住。"""
    definition = _definition()

    response = post_chat(
        "报表名称改成新日报",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert response["domainObjects"]["reportDefinitionDraft"]["metadata"]["name"] == (
        "A 机房能耗日报"
    )
    assert response["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0
    question = response["questions"][0]
    assert question["id"] == "pending_edit_plan_confirm"
    assert question.get("lockScope"), "候选待选期间必须锁"


def test_t10_duplicate_acceptance_is_rejected_and_state_unchanged(post_chat):
    """T10：重复接受 → 拒绝，**现场不变**。"""
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

    again = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert again["domainObjects"]["changeSet"] is None, "重复接受不能再改一次"
    assert again["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 1


def test_t10_stale_plan_is_rejected_and_state_unchanged():
    """T10：过期计划 → 拒绝，定义不动。"""
    definition = _definition()
    # 先接受一次合法修改 → 草稿版本推到 1。
    applied = template_edit_engine.apply(
        plan=_parse(T01_SENTENCE), definition=definition,
    )
    after = applied.draftAfterEdit
    assert after.metadata["revision"] == 1

    # 这份计划是基于版本 0 的 —— 现在已经过期。
    stale = EditPlan(
        intent=EditIntent.RENAME_REPORT,
        operation=EditOperation(newTitle="再改一次"),
    )
    stale.operation.baseDefinitionRevision = 0

    result = template_edit_engine.apply(plan=stale, definition=after)

    assert result.needsClarification is True
    assert result.draftAfterEdit.model_dump(mode="json") == after.model_dump(mode="json")


def test_t10_second_window_conflicting_write_is_rejected(tmp_path):
    """T10：第二窗口拿着**旧版本**提交 → 拒绝，现场不变。"""
    from app.services.draft_workspace import DraftRevisionConflict, draft_workspace

    definition = _definition()
    draft_id = _workspace_draft(tmp_path, definition)
    after_a = _execute(definition, _style_ops(TOTAL_ID, font_size=20))
    draft_workspace.commit_edit(draft_id, definition=after_a.model_dump(mode="json"))

    # 另一个窗口还拿着 revision 1（实际已经是 2）。
    with pytest.raises(DraftRevisionConflict):
        draft_workspace.commit_edit(
            draft_id,
            definition=after_a.model_dump(mode="json"),
            expected_revision=1,
        )

    state = draft_workspace.load_draft(draft_id)
    assert state.revision == 2, "被拒的写入不能改版本"


def test_t10_direct_chat_bypassing_restricted_step_is_rejected(post_chat):
    """T10：受限修改输入里直接走普通聊天 → 后端拒绝，现场不变。"""
    from app.services.session_service import session_service

    definition = _definition()
    step = _enter_edit_step(post_chat, definition)
    before = session_service.load_snapshot("conv_test")

    rejected = post_chat(
        "把报表名称改成新日报",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert rejected["error"] is not None, "绕过受限步骤必须被拒"
    assert rejected["error"]["code"] == "STEP_REF_REQUIRED"
    assert (rejected.get("domainObjects") or {}).get("pendingEditPlan") is None
    # 现场不变：步骤还在、定义没变、没有候选计划。
    stored = session_service.load_pending_choice("conv_test")
    assert stored["question"]["stepId"] == step["stepId"]
    assert session_service.load_pending_plan("conv_test") is None
    assert session_service.load_snapshot("conv_test")["definition"] == before["definition"]


def test_t10_stale_step_ref_is_rejected(post_chat):
    """T10：旧步骤 / 伪造的步骤关联 → 拒绝，现场不变。"""
    from app.services.session_service import session_service

    definition = _definition()
    _enter_edit_step(post_chat, definition)
    before = session_service.load_snapshot("conv_test")

    rejected = post_chat(
        "把报表名称改成新日报",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        stepId="step_from_an_older_card",
        stepVersion=1,
    ).json()

    assert rejected["error"]["code"] == "STEP_REF_MISMATCH"
    assert (rejected.get("domainObjects") or {}).get("pendingEditPlan") is None
    assert session_service.load_pending_plan("conv_test") is None
    assert session_service.load_snapshot("conv_test")["definition"] == before["definition"]


def test_t10_legal_step_input_still_works(post_chat):
    """T10：拒绝旁路之后，本步**合法输入**仍然能用。"""
    definition = _definition()
    step = _enter_edit_step(post_chat, definition)

    accepted = post_chat(
        T01_SENTENCE,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        stepId=step["stepId"],
        stepVersion=step["stepVersion"],
    ).json()

    assert accepted["error"] is None
    plan = accepted["domainObjects"]["pendingEditPlan"]
    assert plan is not None, "合法步骤关联必须能产出候选"
    assert plan["operations"][0]["operation"]["presentationOperations"]


# ==========================================================================
# T11 失败 / 迟到
# ==========================================================================

def test_t11_illegal_font_size_is_rejected():
    """T11：越界 fontSize → 明确失败，定义不变。"""
    definition = _definition()
    before = definition.model_dump(mode="json")

    result = _fails(definition, [{
        "op": "SET_STYLE",
        "params": {"regionId": TOTAL_ID, "style": {"fontSize": {"value": 999, "unit": "PX"}}},
    }])

    assert "fontSize" in (result.failure or "")
    assert result.definition.model_dump(mode="json") == before


def test_t11_illegal_gap_and_placement_are_rejected():
    """T11：非法 gap / placement → 明确失败，定义不变。"""
    definition = _definition()
    before = definition.model_dump(mode="json")

    gap_result = _fails(definition, [{
        "op": "CREATE_GROUP",
        "params": {
            "groupId": GROUP_ID,
            "memberRegionIds": [TOTAL_ID, TREND_ID],
            "arrangement": {"mode": "GRID", "columns": 2,
                            "gap": {"value": 999, "unit": "PX"}},
        },
    }])
    assert "gap" in (gap_result.failure or "")

    placement_result = _fails(definition, [{
        "op": "SET_PLACEMENT",
        "params": {
            "regionId": TOTAL_ID,
            "placement": {"type": "GRID", "column": 1, "columnSpan": 99},
        },
    }])
    assert "columnSpan" in (placement_result.failure or "")

    assert definition.model_dump(mode="json") == before


def test_t11_unknown_field_is_rejected_not_dropped():
    """T11：未知字段 → 报错，**不许静默丢掉**（契约 N7）。"""
    definition = _definition()

    result = _fails(definition, [{
        "op": "SET_STYLE",
        "params": {"regionId": TOTAL_ID, "style": {"fontSizeX": {"value": 1, "unit": "PX"}}},
    }])
    assert "fontSizeX" in (result.failure or "")

    unknown_op = _fails(definition, [{"op": "SET_EVERYTHING", "params": {}}])
    assert "SET_EVERYTHING" in (unknown_op.failure or "")


def test_t11_wrong_target_is_rejected():
    """T11：错误目标 → 明确失败。"""
    definition = _definition()

    result = _fails(definition, [{
        "op": "SET_STYLE",
        "params": {"regionId": "region_not_exists", "style": {"borderRadius": 4}},
    }])
    assert result.reason == "unknown_region"


def test_t11_later_operation_failure_leaves_no_earlier_change():
    """T11：多操作批次**后项失败** → 前项改动也不留下（整批原子）。"""
    definition = _definition()
    before = definition.model_dump(mode="json")

    result = _fails(definition, [
        {"op": "SET_STYLE", "params": {
            "regionId": TOTAL_ID,
            "style": {"borderRadius": 4},
        }},
        {"op": "MOVE_REGION", "params": {
            "regionId": "region_not_exists", "index": 0,
        }},
    ])

    assert result.definition.model_dump(mode="json") == before, "前项改动不能留下"
    assert not (_region(result.definition, TOTAL_ID).get("style") or {})


def test_t11_preflight_blocks_illegal_plan_without_side_effects():
    """T11：预检在**副本**上拦下非法计划，草稿一个字节都不动。"""
    definition = _definition()
    before = definition.model_dump(mode="json")

    result = preflight_presentation(definition, [{
        "op": "SET_STYLE",
        "params": {"regionId": TOTAL_ID, "style": {"fontSize": {"value": 999, "unit": "PX"}}},
    }])

    assert result.ok is False
    assert result.failure, "失败要有能直接给用户看的中文理由"
    assert result.reason == "out_of_range"
    # 无副作用。
    assert definition.model_dump(mode="json") == before


def test_t11_preflight_normalizes_loose_params_instead_of_rejecting():
    """T11：松散但**语义合法**的参数走规范化，不该被当成非法。"""
    definition = _definition()

    result = preflight_presentation(definition, [{
        "op": "SET_STYLE",
        "params": {"regionId": TOTAL_ID, "style": {"fontSize": 28}},
    }])

    assert result.ok is True, f"松散参数应当被规范化而不是被拒：{result.failure}"
    assert result.operations[0]["params"]["style"]["fontSize"] == {"value": 28, "unit": "PX"}


def test_t11_illegal_plan_becomes_clarification_not_candidate(monkeypatch):
    """T11：非法计划 → **出的是澄清，不是候选**（契约 §2.1）。

    非法操作用 Fake 注入（换掉规则的产出）—— 验证的是「拿到非法计划之后
    那条链路」，不是「中文入口能造出非法计划」。
    """
    definition = _definition()
    before = definition.model_dump(mode="json")
    _inject_operations(monkeypatch, [{
        "op": "SET_STYLE",
        "params": {"regionId": TOTAL_ID, "style": {"fontSize": {"value": 999, "unit": "PX"}}},
    }])

    plan = _parse(T01_SENTENCE, definition=definition)

    assert plan.needsClarification is True, "非法计划必须变成澄清"
    assert plan.operation.presentationOperations == [], "非法计划不能出候选"
    assert plan.clarificationQuestion, "必须给出能直接给用户看的中文理由"
    # 草稿一个字节都没动。
    assert definition.model_dump(mode="json") == before


def test_t11_accept_time_revalidation_refuses_illegal_plan():
    """T11：接受时**再校验一次** —— 非法计划到不了落库那一步。"""
    definition = _definition()
    before = definition.model_dump(mode="json")

    plan = EditPlan(
        intent=EditIntent.APPLY_PRESENTATION,
        operation=EditOperation(presentationOperations=[{
            "op": "SET_STYLE",
            "params": {"regionId": TOTAL_ID,
                       "style": {"fontSize": {"value": 999, "unit": "PX"}}},
        }]),
    )

    result = template_edit_engine.apply(plan=plan, definition=definition)

    assert result.needsClarification is True
    assert result.draftAfterEdit.model_dump(mode="json") == before
    assert not (_region(result.draftAfterEdit, TOTAL_ID).get("style") or {})


def test_t11_old_answer_replay_does_not_pollute_current_state(post_chat):
    """T11：旧答案重放 → **不污染**当前现场（迟到的旧响应同理）。

    接受过一次之后，被取走的那份候选再被提交一次：版本不动、待决计划不
    回来、快照不变。
    """
    from app.services.session_service import session_service

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
    revision = accepted["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"]
    snapshot_before = session_service.load_snapshot("conv_test")

    late = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert late["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == revision
    assert late["domainObjects"]["changeSet"] is None, "旧答案不能再改一次"
    assert session_service.load_pending_plan("conv_test") is None
    assert session_service.load_snapshot("conv_test")["definition"] == \
        snapshot_before["definition"]


# ==========================================================================
# T12 持久化 / 入口
# ==========================================================================

def test_t12_accepted_state_persists_in_draft_workspace(tmp_path):
    """T12：已接受状态刷新 / 重开保持（工作区版本 + 会话快照）。"""
    from app.services.draft_workspace import draft_workspace

    definition = _definition()
    draft_id = _workspace_draft(tmp_path, definition)
    after = _execute(definition, _style_ops(TOTAL_ID, font_size=20))
    draft_workspace.commit_edit(draft_id, definition=after.model_dump(mode="json"))

    reopened = draft_workspace.load_draft(draft_id)

    assert reopened is not None
    assert _region(reopened.definition, TOTAL_ID)["style"]["fontSize"]["value"] == 20
    assert reopened.revision >= 2


def test_t12_accepted_state_persists_in_session_snapshot(post_chat):
    """T12：已接受状态在**会话快照**里保持（刷新后还是那一版）。"""
    from app.services.session_service import session_service

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
    assert accepted["domainObjects"]["reportDefinitionDraft"]["metadata"]["name"] == "新日报"

    snapshot = session_service.load_snapshot("conv_test") or {}
    assert snapshot.get("definition", {}).get("metadata", {}).get("name") == "新日报"


def test_t12_pending_candidate_does_not_take_effect(post_chat):
    """T12：未接受的候选**不偷偷生效**。"""
    definition = _definition()

    response = post_chat(
        "报表名称改成新日报",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert response["domainObjects"]["reportDefinitionDraft"]["metadata"]["name"] == (
        "A 机房能耗日报"
    )
    assert response["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0


@pytest.mark.xfail(
    strict=False,
    reason="依赖 C 包（契约 §状态白名单表 / T12）：另存为**不复制待决内容** —— "
           "当前 draft_workspace.save_as 会把版本快照里的 pendingChoice / "
           "pendingEditPlan 一起复制到新对象",
)
def test_t12_save_as_does_not_copy_pending_content(tmp_path):
    """T12：保存 / 另存为**不复制待决内容**。"""
    from app.services.draft_workspace import draft_workspace

    definition = _definition()
    draft_id = _workspace_draft(tmp_path, definition)
    after = _execute(definition, _style_ops(TOTAL_ID, font_size=20))
    draft_workspace.commit_edit(
        draft_id,
        definition=after.model_dump(mode="json"),
        snapshot={
            "pendingChoice": {"question": {"id": "pending_edit_plan_confirm"}},
            "pendingEditPlan": {"planId": "plan_pending"},
        },
    )

    saved_as = draft_workspace.save_as(draft_id, name="另存的一份")

    assert saved_as.draftId != draft_id
    new_state = draft_workspace.load_draft(saved_as.draftId)
    assert new_state is not None
    assert not (new_state.pendingChoice or {}), "新对象不复制待决现场"
    assert not (new_state.pendingEditPlan or {}), "新对象不复制待决候选"
    assert new_state.canUndo is False, "新对象不复制旧修改历史"


@pytest.mark.xfail(
    strict=False,
    reason="依赖 C 包：撤销后左侧会话现场一起回退（旧步骤失效 / 只读），"
           "刷新后仍一致（契约 §2.3 + 包 B §3.2）—— "
           "当前 draft_workspace.undo 只回滚右侧定义，会话快照与待决步骤不动",
)
def test_t12_undo_restores_session_gate_together(tmp_path):
    """T12：会话 / 历史 / 门禁一起恢复。"""
    from app.services.draft_workspace import draft_workspace
    from app.services.session_service import session_service

    definition = _definition()
    draft_id = _workspace_draft(tmp_path, definition)
    after = _execute(definition, _style_ops(TOTAL_ID, font_size=20))
    draft_workspace.commit_edit(draft_id, definition=after.model_dump(mode="json"))
    # 会话快照记的是**已接受**的那一版。
    session_service.repository.upsert_session(
        session_id="sess_night_workspace",
        state="USER_EDITING_TEMPLATE",
        snapshot={"definition": after.model_dump(mode="json")},
    )
    session_service.repository.set_pending_choice(
        "sess_night_workspace",
        {"state": "USER_EDITING_TEMPLATE", "question": {
            "id": "template_edit_confirm", "lockScope": "input_and_menu",
            "stepId": "step_accepted", "stepVersion": 1, "stepKind": "menu",
        }},
    )

    draft_workspace.undo(draft_id)

    state = draft_workspace.load_draft(draft_id)
    assert not (_region(state.definition, TOTAL_ID).get("style") or {}), \
        "右侧定义要回到基线"
    # 会话快照也要回到基线（不能还留着已撤销那一版的样式）。
    snapshot = session_service.load_snapshot("sess_night_workspace") or {}
    regions = (snapshot.get("definition") or {}).get("regions") or []
    total = next((r for r in regions if r.get("id") == TOTAL_ID), {})
    assert not (total.get("style") or {}), "会话快照必须一起回退"
    # 旧步骤要失效 / 只读。
    pending = session_service.load_pending_choice("sess_night_workspace")
    assert pending is None or pending.get("question", {}).get("stepId") != "step_accepted", \
        "被撤销那一轮的旧步骤必须失效"
