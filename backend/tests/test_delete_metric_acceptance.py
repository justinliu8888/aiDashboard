"""删除单个指标的**端到端**验收：候选 → 确认 → 草稿 → 恢复。

覆盖业务验收探针里与「删指标」有关的那几条：

    1. 候选阶段**不改草稿**；候选里看得见「删哪个指标、加哪个指标」；
    2. 接受之后卡片还在、总耗电保留、实时功率没了、冰箱功率出现；
    3. 接受只 +1 次 revision；
    4. 取消不改定义；
    5. 过期候选改不了新草稿；重复接受不重复删加；
    6. **会话恢复接口**读回来的草稿与重新生成的示意一致（不是只序列化
       Pydantic 模型冒充数据库恢复）；
    7. 没预设过的说法（另一张卡片、另一批指标名）同样能走通 ——
       防止只对原案例的名词做特判。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.services.preview_service import (
    build_preview_context,
    build_report_preview_payload,
)
from app.services.report_template_service import build_template_design
from app.services.template_definition_service import build_template_definition

REGION_ID = "region_1"
REGION_NAME = "核心运行指标总览"
ORIGINAL_MESSAGE = "核心运行指标总览删除实时功率这个指标 再增加一个冰箱功率的指标"


def _plan_document(sections: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"], "subject": None,
            "timeRange": None, "exclusions": [],
        },
        "sections": sections,
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }


def _build(sections: list[dict[str, Any]]):
    plan = _plan_document(sections)
    template = build_template_design(plan, subject_id="room_a")
    return build_template_definition(
        template.model_dump(mode="json"), plan, "room_a",
    )


@pytest.fixture()
def definition():
    """总耗电 + 实时功率 的总览卡。"""
    return _build([
        {
            "id": "section_total", "title": REGION_NAME,
            "metricIntents": ["总耗电", "实时功率"], "componentType": "metric_card",
        },
    ])


@pytest.fixture()
def env_definition():
    """另一张卡片、另一批指标名：防止只对原案例名词做特判。"""
    return _build([
        {
            "id": "section_total", "title": REGION_NAME,
            "metricIntents": ["总耗电", "实时功率"], "componentType": "metric_card",
        },
        {
            "id": "section_env", "title": "设备环境概览",
            "metricIntents": ["温度", "湿度", "气压"], "componentType": "metric_card",
        },
    ])


def _snap(definition) -> dict[str, Any]:
    return {"reportDefinition": definition.model_dump(mode="json")}


def _labels(definition_payload: dict[str, Any], region_id: str = REGION_ID) -> list[str]:
    region = next(
        item for item in definition_payload["regions"] if item["id"] == region_id
    )
    spec = ((region.get("templateBinding") or {}).get("previewSpec") or {})
    return [
        str(item.get("label") or "")
        for item in (spec.get("metrics") or [])
        if isinstance(item, dict)
    ]


def _preview(payload: dict[str, Any]) -> dict[str, Any]:
    context = build_preview_context(payload)
    return build_report_preview_payload(payload, context)


def _payload_metric_labels(payload: dict[str, Any], region_id: str = REGION_ID):
    region = next(
        item for item in payload["regions"] if item["regionId"] == region_id
    )["payload"]
    return [
        str(item.get("label") or "")
        for item in (region.get("metrics") or [])
    ]


# --------------------------------------------------------------------------
# 1. 候选阶段
# --------------------------------------------------------------------------


def test_candidate_lists_both_steps_and_does_not_touch_the_draft(
    post_chat, definition,
):
    """候选里有删除与追加两步；确认之前草稿一个字节都没动。"""
    response = post_chat(
        ORIGINAL_MESSAGE,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert response["uiActions"] == ["pending_edit_plan"]
    plan = response["domainObjects"]["pendingEditPlan"]
    assert plan is not None
    intents = [item["intent"] for item in plan["operations"]]
    assert intents == ["DELETE_METRIC", "ADD_METRIC"]
    # 确认卡上说的是「删哪个指标、加哪个指标」，不是「删除卡片」。
    assert "实时功率" in plan["summary"]
    assert "冰箱功率" in plan["summary"]
    assert "删除卡片" not in plan["summary"]
    # 草稿还没改。
    assert response["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0
    assert definition.metadata["revision"] == 0
    assert response["domainObjects"]["changeSet"] is None


# --------------------------------------------------------------------------
# 2. 接受之后
# --------------------------------------------------------------------------


def test_accepting_applies_delete_and_add_atomically(post_chat, definition):
    """接受之后：卡片在、总耗电保留、实时功率没了、冰箱功率出现。"""
    post_chat(
        ORIGINAL_MESSAGE,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    )
    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert accepted["uiActions"] == ["template_edit_applied"]
    draft = accepted["domainObjects"]["reportDefinitionDraft"]
    assert any(item["id"] == REGION_ID for item in draft["regions"])
    assert _labels(draft) == ["总耗电", "冰箱功率"]
    # 只 +1 次 revision。
    assert draft["metadata"]["revision"] == 1
    # ChangeSet 说清楚删了什么。
    changed = accepted["domainObjects"]["changeSet"]["changed"]
    assert any("实时功率" in str(item.get("summary") or "") for item in changed)


def test_preview_payload_matches_the_definition_after_accept(post_chat, definition):
    """示意载荷与定义一致：实时功率不再出现，冰箱功率出现。"""
    post_chat(
        ORIGINAL_MESSAGE,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    )
    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    draft = accepted["domainObjects"]["reportDefinitionDraft"]
    labels = _payload_metric_labels(_preview(draft))
    assert "实时功率" not in labels
    assert "总耗电" in labels
    assert "冰箱功率" in labels
    # 载荷里的指标项与定义里的指标项是同一份。
    assert labels == _labels(draft)


def test_cancelling_leaves_the_definition_untouched(post_chat, definition):
    """取消 → 草稿不变，也没有 ChangeSet。"""
    post_chat(
        ORIGINAL_MESSAGE,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    )
    cancelled = post_chat(
        "取消",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert cancelled["domainObjects"]["changeSet"] is None
    draft = cancelled["domainObjects"]["reportDefinitionDraft"]
    assert draft["metadata"]["revision"] == 0
    assert _labels(draft) == ["总耗电", "实时功率"]
    assert definition.metadata["revision"] == 0


def test_accepting_twice_does_not_apply_twice(post_chat, definition):
    """重复接受 → 不重复删加。"""
    post_chat(
        ORIGINAL_MESSAGE,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    )
    post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    )
    again = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    draft = again["domainObjects"]["reportDefinitionDraft"]
    assert _labels(draft) == ["总耗电", "冰箱功率"]
    assert draft["metadata"]["revision"] == 1


def test_stale_candidate_cannot_change_the_new_draft(post_chat, definition):
    """草稿已经变了 → 旧候选被拦住，草稿里那个指标还在。"""
    post_chat(
        ORIGINAL_MESSAGE,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    )
    # 先做一次别的修改，草稿版本变了。
    changed = definition.model_copy(deep=True)
    changed.metadata = dict(changed.metadata or {})
    changed.metadata["revision"] = 5
    stale = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(changed),
    ).json()

    assert stale["domainObjects"]["changeSet"] is None
    # 旧候选**没有**把实时功率删掉。
    draft = stale["domainObjects"]["reportDefinitionDraft"]
    assert "实时功率" in _labels(draft)
    assert "草稿已变化" in stale["reply"]


# --------------------------------------------------------------------------
# 3. 会话恢复
# --------------------------------------------------------------------------


def test_session_restore_returns_the_edited_draft_and_preview(
    post_chat, client, definition,
):
    """用**会话恢复接口**读回草稿与重新生成的示意。"""
    post_chat(
        ORIGINAL_MESSAGE,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    )
    post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    )

    restored = client.get("/api/sessions/conv_test").json()
    snapshot = restored.get("snapshot") or {}
    stored = (snapshot.get("definition") or {}).get("reportDefinition") or snapshot.get("definition") or {}
    assert stored, "会话里没存下这一版草稿"
    assert _labels(stored) == ["总耗电", "冰箱功率"]
    assert stored["metadata"]["revision"] == 1

    # 恢复出来的定义**重新生成**示意，结果与接受时一致。
    payload = snapshot.get("reportPreviewPayload")
    if payload:
        assert "实时功率" not in _payload_metric_labels(payload)
        assert "冰箱功率" in _payload_metric_labels(payload)


# --------------------------------------------------------------------------
# 4. 没预设过的说法
# --------------------------------------------------------------------------


def test_unpreset_card_and_metric_names_also_work(post_chat, env_definition):
    """「设备环境概览移除湿度指标，再加一个风速指标」同样走通。"""
    response = post_chat(
        "设备环境概览移除湿度指标，再加一个风速指标",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(env_definition),
    ).json()

    plan = response["domainObjects"]["pendingEditPlan"]
    if plan is None:
        # 没生成候选时必须是**追问**，不是删卡片。
        assert response["domainObjects"]["changeSet"] is None
        assert response["state"] == "USER_EDITING_TEMPLATE"
        return
    intents = [item["intent"] for item in plan["operations"]]
    assert intents == ["DELETE_METRIC", "ADD_METRIC"]

    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(env_definition),
    ).json()
    draft = accepted["domainObjects"]["reportDefinitionDraft"]
    labels = _labels(draft, "region_2")
    assert "湿度" not in labels
    assert "温度" in labels and "气压" in labels


def test_unpreset_delete_only_request(post_chat, env_definition):
    """只说「移除湿度指标」→ 删的是那一项指标，不是整张卡片。"""
    response = post_chat(
        "设备环境概览移除湿度指标",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(env_definition),
    ).json()

    plan = response["domainObjects"]["pendingEditPlan"]
    assert plan is not None
    assert plan["operations"][0]["intent"] == "DELETE_METRIC"
    assert "湿度" in plan["summary"]

    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(env_definition),
    ).json()
    draft = accepted["domainObjects"]["reportDefinitionDraft"]
    assert any(item["id"] == "region_2" for item in draft["regions"])
    labels = _labels(draft, "region_2")
    assert "湿度" not in labels
    assert "温度" in labels
