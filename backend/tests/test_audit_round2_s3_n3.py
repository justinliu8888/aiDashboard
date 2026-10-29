"""第二轮独立审查发现的缺陷的回归测试（S3 / N3）。

- **S3**（严重）方案侧回写候选时把表格列的 ``format`` 整段丢了 ——
  模型声明了「耗电量，单位 kWh」，走到前端表头就没有单位。
  断点在 ``report_plan_agent._apply_resolution``：它重建列对象时只写
  ``label`` / ``id`` / ``fieldRef``，没写 ``spec.format``。
- **N3**（一般）旧口径「数据待连接」仍会渲染到界面上 —— 陌生指标产生的
  ``pendingReason`` 里带着旧口径，违反规格《回复口径》与 US15。

测试只替换注入的外部 ``model_call``，其余全是真实实现：
``generate_candidate`` → ``apply_candidate`` → ``build_template_design``
→ ``build_template_definition`` → ``build_report_preview_payload``。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.agents.report_plan_agent import generate_candidate
from app.services.preview_service import build_report_preview_payload
from app.services.report_plan_service import apply_candidate
from app.services.report_template_service import build_template_design
from app.services.template_definition_service import build_template_definition


#: 旧口径的四个字：规格《回复口径》要求删掉，US15 要求页面上不出现。
LEGACY_PENDING_WORDING = "数据待连接"


def run_async(coro: Any) -> Any:
    """本仓库没有 pytest-asyncio，同步用例里直接驱动协程。"""
    return asyncio.run(coro)


def model_returning(payload: Any) -> Any:
    """只回放一段模型文本的替身；不替换模块内部任何逻辑。"""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    async def _model_call(messages: list[dict[str, Any]]) -> str:
        return text

    return _model_call


def _table_candidate() -> dict[str, Any]:
    """模型声明了一个带单位标注的表格栏目。"""
    return {
        "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"],
            "subject": "A 机房",
            "timeRange": "最近 7 天",
            "exclusions": [],
        },
        "sections": [
            {
                "title": "设备耗电明细",
                "metricIntents": ["设备耗电"],
                "componentType": "table",
                "columns": [
                    {
                        "id": "col_1",
                        "label": "耗电量",
                        "fieldRef": "section_1.col_1",
                        "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
                    }
                ],
            }
        ],
        "assumptions": [],
        "suggestions": [],
        "referenceTemplates": [],
    }


def _unknown_metric_candidate() -> dict[str, Any]:
    """模型要了一个**指标目录里没有**的量（陌生指标）。"""
    return {
        "topic": "A 机房温度日报",
        "requirements": {
            "metrics": ["温度分布"],
            "subject": "A 机房",
            "timeRange": "最近 7 天",
            "exclusions": [],
        },
        "sections": [
            {
                "title": "温度分布",
                "metricIntents": ["温度分布"],
                "componentType": "metric_card",
            }
        ],
        "assumptions": [],
        "suggestions": [],
        "referenceTemplates": [],
    }


def _generate(candidate: dict[str, Any], message: str) -> dict[str, Any]:
    return run_async(
        generate_candidate(
            message=message,
            current_plan=None,
            history=[],
            model_call=model_returning(
                {"kind": "plan_candidate", "candidate": candidate}
            ),
        )
    )


def _plan_from_agent(candidate: dict[str, Any], message: str):
    """走**线上链路**：先让服务端定稿回写，再由候选建方案。"""
    result = _generate(candidate, message)
    assert result["kind"] == "plan_candidate", result
    return result["candidate"], apply_candidate(
        result["candidate"], plan_id="plan_round2"
    )


def _definition_from_agent(candidate: dict[str, Any], message: str):
    """方案 → 模板 → 草稿定义（走真实的转换层两跳）。"""
    _agent_candidate, plan = _plan_from_agent(candidate, message)
    payload = plan.model_dump(mode="json")
    template = build_template_design(payload, subject_id="room_a")
    draft = build_template_definition(
        template.model_dump(mode="json"), payload, "room_a"
    )
    return plan, draft


def _preview_context() -> dict[str, Any]:
    return {
        "definitionId": "def_round2_1",
        "definitionVersion": 1,
        "timezone": "Asia/Shanghai",
        "anchorTime": "2026-10-29T00:00:00+08:00",
        "windowStart": "2026-10-28T00:00:00+08:00",
        "windowEnd": "2026-10-29T00:00:00+08:00",
        "grainUnit": "HOUR",
        "generatorVersion": "simulated-data/1",
    }


# --------------------------------------------------------------------------
# S3：列的 format 不能在回写候选时丢
# --------------------------------------------------------------------------


def test_s3_agent_output_column_keeps_the_declared_format() -> None:
    """服务端定稿回写候选时，列的 ``format`` 必须活下来。

    这是断点本身：``_apply_resolution`` 重建列对象时只写 label / id /
    fieldRef，把 ``spec.format`` 整段丢了。
    """
    result = _generate(_table_candidate(), "给我一份设备耗电明细")
    assert result["kind"] == "plan_candidate", result

    column = result["candidate"]["sections"][0]["columns"][0]
    assert column["label"] == "耗电量"
    assert column["format"]["unitLabel"] == "kWh"
    assert column["format"]["precision"] == 1


def test_s3_declared_unit_reaches_the_plan_column() -> None:
    """从 ``generate_candidate()`` 的输出建方案，单位标注还在。"""
    _agent_candidate, plan = _plan_from_agent(
        _table_candidate(), "给我一份设备耗电明细"
    )

    assert plan.sections[0].columns[0].format is not None
    assert plan.sections[0].columns[0].format.unitLabel == "kWh"


def test_s3_declared_unit_reaches_the_preview_payload_columns() -> None:
    """端到端：模型声明的单位标注一路到 ``payload.columns[].format.unitLabel``。

    规格《契约缺口补充》A2：载荷的表格列必须带 ``format.unitLabel``，
    表头才有单位。
    """
    _plan, draft = _definition_from_agent(
        _table_candidate(), "给我一份设备耗电明细"
    )
    definition = draft.model_dump(mode="json")

    payload = build_report_preview_payload(definition, _preview_context())
    regions = payload["regions"]
    assert regions, "表格区域应当出现在示意载荷里"
    table_region = next(
        region for region in regions if region["componentType"] == "TABLE"
    )
    columns = table_region["payload"]["columns"]
    assert columns, "表格载荷里没有列"
    assert columns[0]["format"]["unitLabel"] == "kWh"


# --------------------------------------------------------------------------
# N3：旧口径「数据待连接」不能再出现在界面上
# --------------------------------------------------------------------------


def test_n3_unknown_metric_pending_reason_has_no_legacy_wording() -> None:
    """陌生指标产生的 ``pendingReason`` 里不含旧口径。"""
    _plan, draft = _definition_from_agent(
        _unknown_metric_candidate(), "给我一份温度分布"
    )
    definition = draft.model_dump(mode="json")

    region = definition["regions"][0]
    binding = region["templateBinding"]
    metrics = binding["previewSpec"]["metrics"]
    assert metrics, "陌生指标也要有明确的指标条目"

    for metric in metrics:
        reason = metric.get("pendingReason") or ""
        assert LEGACY_PENDING_WORDING not in reason, reason
        assert metric["capabilityPending"] is True


def test_n3_placeholder_reason_has_no_legacy_wording() -> None:
    """模板层的占位原因也不含旧口径（页面上要显示的就是它）。"""
    _agent_candidate, plan = _plan_from_agent(
        _unknown_metric_candidate(), "给我一份温度分布"
    )
    template = build_template_design(
        plan.model_dump(mode="json"), subject_id="room_a"
    )

    regions = [region.model_dump(mode="json") for region in template.regions]
    assert regions
    for region in regions:
        reason = region.get("placeholderReason") or ""
        assert LEGACY_PENDING_WORDING not in reason, reason


def test_n3_unknown_metric_keeps_the_settled_label_system() -> None:
    """改口径不等于改标签体系：仍是「能力待接通」，且不编造数据。"""
    _plan, draft = _definition_from_agent(
        _unknown_metric_candidate(), "给我一份温度分布"
    )
    definition = draft.model_dump(mode="json")

    spec = definition["regions"][0]["templateBinding"]["previewSpec"]
    assert spec["capabilityPending"] is True
    reason = spec["metrics"][0]["pendingReason"] or ""
    assert "能力待接通" in reason, reason
