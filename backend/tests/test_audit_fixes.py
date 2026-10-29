"""独立只读审查发现的缺陷的回归测试（B1 ~ B7）。

每一条都对应审查报告里的一条：

- **B1**（阻断）方案声明的栏目取值键被按数据源字段校验 → 复核修复是否生效。
- **B2**（严重）方案里出现一个数字就废掉整案 —— 标识位上的数值被误伤。
- **B3**（一般）模型给了坏栏目时被静默降级成「系统补齐」。
- **B4**（一般）系统补齐的「机房」列渲染成「示例值 69」。
- **B5**（一般）表格列的 ``format.unitLabel`` 契约在方案通道产不出来。
- **B6**（提示）``TemplateColumn`` 的 ``extra="allow"`` 把未知键带进落库定义。
- **B7**（提示）缺键兜底没按板块 ID 分段。

测试只替换注入的外部 ``model_call``，其余全是真实实现。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.report_plan_agent import generate_candidate
from app.schemas.report_plan import PlanColumn, PlanValidationError
from app.schemas.template_design import TemplateColumn
from app.services.report_plan_service import (
    apply_candidate,
    resolve_table_columns,
)
from app.services.report_template_service import build_template_design
from app.services.simulated_data_service import generate_simulated_data
from app.services.template_definition_service import build_template_definition
from app.services.validation_service import validate_definition


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def run_async(coro: Any) -> Any:
    """本仓库没有 pytest-asyncio，同步用例里直接驱动协程。"""
    return asyncio.run(coro)


def model_returning(payload: Any) -> Any:
    """只回放一段模型文本的替身；不替换模块内部任何逻辑。"""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    async def _model_call(messages: list[dict[str, Any]]) -> str:
        return text

    return _model_call


#: 一份最小可提交的候选（表格板块，模型不给栏目）。
_BASE_CANDIDATE: dict[str, Any] = {
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
        }
    ],
    "assumptions": [],
    "suggestions": [],
    "referenceTemplates": [],
}


def _candidate(**overrides: Any) -> dict[str, Any]:
    candidate = json.loads(json.dumps(_BASE_CANDIDATE))
    for key, value in overrides.items():
        candidate[key] = value
    return candidate


def _plan(candidate: dict[str, Any], *, plan_id: str = "plan_audit"):
    """候选 → 可提交方案（走真实的领域层）。"""
    return apply_candidate(candidate, plan_id=plan_id)


def _to_definition(plan, *, subject_id: str = "room_a"):
    """方案 → 模板 → 草稿定义（走真实的转换层三跳）。"""
    payload = plan.model_dump(mode="json") if hasattr(plan, "model_dump") else plan
    template = build_template_design(payload, subject_id=subject_id)
    draft = build_template_definition(template.model_dump(mode="json"), payload, subject_id)
    return template, draft


def _preview_context() -> dict[str, Any]:
    return {
        "definitionId": "def_audit_1",
        "definitionVersion": 1,
        "timezone": "Asia/Shanghai",
        "anchorTime": "2026-10-29T00:00:00+08:00",
        "windowStart": "2026-10-28T00:00:00+08:00",
        "windowEnd": "2026-10-29T00:00:00+08:00",
        "grainUnit": "HOUR",
        "generatorVersion": "simulated-data/1",
    }


# --------------------------------------------------------------------------
# B1 复核：模板态的行对象取值键不是数据源字段，校验不能因此判失败
# --------------------------------------------------------------------------


def _validation_codes(result) -> list[str]:
    return [issue.code for issue in result.issues]


def test_b1_system_filled_columns_pass_validation() -> None:
    """D1：模型未声明栏目 → 系统补齐 → 全链路 → 校验通过。

    方案声明的取值键（``section_1.col_1`` 这种）是**行对象取值键**，不是
    数据源字段 —— 校验层按数据源字段查它就会永远停在 VALIDATION_FAILED。
    """
    plan = _plan(_candidate())
    _template, draft = _to_definition(plan)

    result = validate_definition(draft.model_dump(mode="json"))

    assert result.passed, [issue.message for issue in result.issues]
    assert "COLUMN_FIELD_UNKNOWN" not in _validation_codes(result)


def test_b1_columns_with_only_a_label_pass_validation() -> None:
    """D2：模型只给 ``label``（不给 id / fieldRef）→ 同样要过校验。"""
    candidate = _candidate(
        sections=[
            {
                "title": "设备耗电明细",
                "metricIntents": ["设备耗电"],
                "componentType": "table",
                "columns": [{"label": "设备名称"}, {"label": "耗电量"}],
            }
        ]
    )
    plan = _plan(candidate)
    _template, draft = _to_definition(plan)

    result = validate_definition(draft.model_dump(mode="json"))

    assert result.passed, [issue.message for issue in result.issues]
    assert "COLUMN_FIELD_UNKNOWN" not in _validation_codes(result)


def test_b1_declared_row_keys_reach_the_component_unchanged() -> None:
    """方案声明的取值键原样落进组件列（取值键语义不能被改写）。"""
    candidate = _candidate(
        sections=[
            {
                "title": "设备耗电明细",
                "metricIntents": ["设备耗电"],
                "componentType": "table",
                "columns": [
                    {"id": "col_1", "label": "设备名称", "fieldRef": "section_1.col_1"},
                    {"id": "col_2", "label": "耗电量", "fieldRef": "section_1.col_2"},
                ],
            }
        ]
    )
    plan = _plan(candidate)
    _template, draft = _to_definition(plan)

    columns = draft.regions[0]["component"]["columns"]
    assert [column["fieldRef"] for column in columns] == [
        "section_1.col_1",
        "section_1.col_2",
    ]
    assert validate_definition(draft.model_dump(mode="json")).passed


# --------------------------------------------------------------------------
# B2：标识位上的数值不该废掉整案
# --------------------------------------------------------------------------


def _generate(candidate: dict[str, Any], message: str = "给我一份设备耗电明细"):
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


def test_b2_a_numeric_column_id_does_not_kill_the_turn() -> None:
    """模型给数字 id 很常见 —— 那是序号式标识，不该让整轮方案作废。"""
    candidate = _candidate(
        sections=[
            {
                "title": "设备耗电明细",
                "metricIntents": ["设备耗电"],
                "componentType": "table",
                "columns": [{"id": 1, "label": "设备名称"}, {"id": 2, "label": "耗电量"}],
            }
        ]
    )

    result = _generate(candidate)

    assert result["kind"] == "plan_candidate", result
    assert [column["label"] for column in result["candidate"]["sections"][0]["columns"]] == [
        "设备名称",
        "耗电量",
    ]


def test_b2_a_numeric_column_id_becomes_a_string_key() -> None:
    """数字 id 归一成字符串标识（可当键用），不是数值。"""
    candidate = _candidate(
        sections=[
            {
                "title": "设备耗电明细",
                "metricIntents": ["设备耗电"],
                "componentType": "table",
                "columns": [{"id": 7, "label": "设备名称"}],
            }
        ]
    )

    result = _generate(candidate)

    assert result["kind"] == "plan_candidate", result
    column = result["candidate"]["sections"][0]["columns"][0]
    assert column["id"] == "7"
    assert isinstance(column["id"], str)


def test_b2_a_numeric_label_is_still_rejected() -> None:
    """列名是给人看的文本 —— 数字列名仍然不合理，继续拒绝。"""
    candidate = _candidate(
        sections=[
            {
                "title": "设备耗电明细",
                "metricIntents": ["设备耗电"],
                "componentType": "table",
                "columns": [{"label": 12}],
            }
        ]
    )

    result = _generate(candidate)

    assert result["kind"] == "failure"
    assert result["code"] == "UNSAFE_CONTENT"


def test_b2_real_business_numbers_are_still_rejected() -> None:
    """护栏没被放宽：业务数值（``value`` / ``yValues``）照旧拒。"""
    for payload in (
        {"candidate": {"sections": [{"value": 69}]}},
        {"candidate": {"sections": [{"yValues": [1, 2, 3]}]}},
        {"candidate": {"sections": [{"metricIntents": [123]}]}},
    ):
        result = run_async(
            generate_candidate(
                message="给我一份设备耗电明细",
                current_plan=None,
                history=[],
                model_call=model_returning({"kind": "plan_candidate", **payload}),
            )
        )
        assert result["kind"] == "failure", payload
        assert result["code"] == "UNSAFE_CONTENT", payload


# --------------------------------------------------------------------------
# B3：模型给的栏目结构坏了 → 明确报失败，不静默换成系统补齐
# --------------------------------------------------------------------------


def test_b3_broken_model_columns_fail_instead_of_being_inferred() -> None:
    """模型写了栏目但结构是坏的 → 技术错误，明确失败让用户重试。"""
    resolution = resolve_table_columns(
        {
            "title": "设备耗电明细",
            "metricIntents": ["设备耗电"],
            "columns": [{"label": ""}],
        }
    )

    assert resolution.kind == "failure"
    assert resolution.code == "PLAN_COLUMNS_UNRESOLVED"
    assert "重试" in (resolution.message or "")


def test_b3_broken_columns_fail_through_the_agent() -> None:
    """走真实链路：模型给了坏栏目 → 本轮失败（不是「系统补齐」）。"""
    candidate = _candidate(
        sections=[
            {
                "title": "设备耗电明细",
                "metricIntents": ["设备耗电"],
                "componentType": "table",
                "columns": [{"label": ""}],
            }
        ]
    )

    result = _generate(candidate)

    assert result["kind"] == "failure"
    assert result["code"] == "PLAN_COLUMNS_UNRESOLVED"


def test_b3_empty_columns_are_still_inferred() -> None:
    """模型给**空数组**是「漏写」，规格要求系统自己修正 —— 仍走补齐。"""
    resolution = resolve_table_columns(
        {
            "title": "设备耗电明细",
            "metricIntents": ["设备耗电"],
            "columns": [],
        }
    )

    assert resolution.kind == "inferred"
    assert [spec.label for spec in resolution.specs]


# --------------------------------------------------------------------------
# B4：系统补齐的列必须**合理**（不能是「示例值 69」）
# --------------------------------------------------------------------------


def test_b4_the_energy_preset_has_no_unrenderable_room_column() -> None:
    """能耗表补出来的列都得能被渲染出合理内容。"""
    plan = _plan(_candidate())

    labels = [column.label for column in plan.sections[0].columns]
    assert labels == ["设备名称", "耗电量"]


def test_b4_the_simulated_payload_has_no_placeholder_value() -> None:
    """补齐的列渲染出来是真实感的内容，不是「示例值 69」这种占位观感。"""
    plan = _plan(_candidate())
    _template, draft = _to_definition(plan)

    payloads = generate_simulated_data(
        draft.model_dump(mode="json"), _preview_context()
    )

    rows = [
        row
        for payload in payloads.values()
        for row in (payload.get("rows") or [])
    ]
    assert rows, "表格区域应当出行"
    for row in rows:
        for value in row.values():
            assert not str(value).startswith("示例值"), row


# --------------------------------------------------------------------------
# B5：表格列的 format.unitLabel 在方案通道产得出来
# --------------------------------------------------------------------------


def test_b5_plan_column_accepts_an_explicit_format() -> None:
    """``PlanColumn`` 显式声明 ``format``（不是 extra="allow" 放开整类）。"""
    column = PlanColumn(
        id="col_1",
        label="耗电量",
        fieldRef="section_1.col_1",
        format={"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
    )

    assert column.format is not None
    assert column.format.unitLabel == "kWh"
    assert column.format.precision == 1
    assert column.format.kind == "NUMBER"


def test_b5_plan_column_still_forbids_unknown_keys() -> None:
    """字段封闭：未知键仍然判非法。"""
    with pytest.raises(PlanValidationError):
        PlanColumn.model_validate(
            {
                "id": "col_1",
                "label": "耗电量",
                "fieldRef": "section_1.col_1",
                "bogus": "PWNED",
            }
        )


def test_b5_a_declared_unit_reaches_the_definition() -> None:
    """模型声明的单位标注一路落到草稿定义的列上（表头才有单位）。

    方案从 ``generate_candidate()`` 的**输出**出发 —— 那才是线上链路：
    候选要先经服务端定稿再回写，列的 ``format`` 必须在那一步活下来。
    （早先这里直接拿原始模型候选做 ``apply_candidate``，绕过了出问题的那一层，
    于是这条用例恒绿而线上是坏的。）
    """
    candidate = _candidate(
        sections=[
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
        ]
    )

    result = _generate(candidate)
    assert result["kind"] == "plan_candidate", result

    # 方案层：从**服务端定稿后的候选**建方案，单位标注不能在中途丢掉。
    plan = _plan(result["candidate"])
    assert plan.sections[0].columns[0].format is not None
    assert plan.sections[0].columns[0].format.unitLabel == "kWh"

    _template, draft = _to_definition(plan)
    definition = draft.model_dump(mode="json")
    columns = definition["regions"][0]["component"]["columns"]
    assert columns[0]["format"]["unitLabel"] == "kWh"
    preview_columns = definition["regions"][0]["templateBinding"]["previewSpec"]["columns"]
    assert preview_columns[0]["format"]["unitLabel"] == "kWh"


def test_b5_a_column_without_a_format_has_no_unit() -> None:
    """没声明格式 → 没有单位标注（不写错的单位）。"""
    plan = _plan(_candidate())

    assert plan.sections[0].columns[0].format is None


# --------------------------------------------------------------------------
# B6：未知键不进模板设计，也不进落库定义
# --------------------------------------------------------------------------


def test_b6_template_column_forbids_unknown_keys() -> None:
    """白名单：``TemplateColumn`` 只认 id / label / fieldRef / format。"""
    with pytest.raises(ValidationError):
        TemplateColumn(
            id="col_1", label="耗电量", fieldRef="section_1.col_1", evilKey="PWNED"
        )


def test_b6_unknown_column_keys_do_not_reach_the_definition() -> None:
    """未知键既不进组件列，也不进 previewSpec。"""
    plan = _plan(_candidate())
    payload = plan.model_dump(mode="json")
    payload["sections"][0]["columns"][0]["evilKey"] = "PWNED"

    _template, draft = _to_definition(payload)

    definition = draft.model_dump(mode="json")
    assert "evilKey" not in str(definition)
    assert "PWNED" not in str(definition)


def test_b6_the_format_key_still_works() -> None:
    """白名单必须含 ``format``，否则打断既有的列格式行为。"""
    column = TemplateColumn(
        id="col_1",
        label="耗电量",
        fieldRef="section_1.col_1",
        format={"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
    )

    assert column.format == {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"}


# --------------------------------------------------------------------------
# B7：缺键兜底按板块 ID 分段
# --------------------------------------------------------------------------


def test_b7_the_fallback_row_key_is_scoped_by_the_section() -> None:
    """缺 ``fieldRef`` 时的兜底键形如 ``section_1_col_1``（两段都在）。"""
    payload = {
        "id": "rplan_audit",
        "revision": 1,
        "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"],
            "subject": "A 机房",
            "timeRange": "最近 7 天",
            "exclusions": [],
        },
        "sections": [
            {
                "id": "section_1",
                "title": "设备耗电明细",
                "metricIntents": ["设备耗电"],
                "componentType": "table",
                "columns": [
                    {"id": "col_1", "label": "设备名称"},
                    {"id": "col_2", "label": "耗电量"},
                ],
            }
        ],
        "assumptions": [],
        "suggestions": [],
        "referenceTemplates": [],
    }

    template = build_template_design(payload, subject_id="room_a")

    assert [column.fieldRef for column in template.regions[0].columns] == [
        "section_1_col_1",
        "section_1_col_2",
    ]


def test_b7_two_sections_with_the_same_column_id_do_not_collide() -> None:
    """兜底键按板块分段 → 不同板块的同名栏目不撞键。"""
    payload = {
        "id": "rplan_audit",
        "revision": 1,
        "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"],
            "subject": "A 机房",
            "timeRange": "最近 7 天",
            "exclusions": [],
        },
        "sections": [
            {
                "id": "section_1",
                "title": "设备耗电明细",
                "metricIntents": ["设备耗电"],
                "componentType": "table",
                "columns": [{"id": "col_1", "label": "设备名称"}],
            },
            {
                "id": "section_2",
                "title": "告警列表",
                "metricIntents": ["告警"],
                "componentType": "table",
                "columns": [{"id": "col_1", "label": "设备名称"}],
            },
        ],
        "assumptions": [],
        "suggestions": [],
        "referenceTemplates": [],
    }

    template = build_template_design(payload, subject_id="room_a")

    keys = [region.columns[0].fieldRef for region in template.regions if region.columns]
    assert keys == ["section_1_col_1", "section_2_col_1"]


# --------------------------------------------------------------------------
# 端到端回归：模型给数字 id / 给坏栏目 / 声明列 → 全链路到校验通过
# --------------------------------------------------------------------------


def test_end_to_end_numeric_id_reaches_a_valid_definition() -> None:
    """模型给数字 id → 方案 → 模板 → 草稿 → 校验通过（不再半路作废）。"""
    candidate = _candidate(
        sections=[
            {
                "title": "设备耗电明细",
                "metricIntents": ["设备耗电"],
                "componentType": "table",
                "columns": [
                    {"id": 1, "label": "设备名称", "fieldRef": 1},
                    {
                        "id": 2,
                        "label": "耗电量",
                        "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
                    },
                ],
            }
        ]
    )

    result = _generate(candidate)
    assert result["kind"] == "plan_candidate", result

    plan = _plan(result["candidate"])
    _template, draft = _to_definition(plan)

    validation = validate_definition(draft.model_dump(mode="json"))
    assert validation.passed, [issue.message for issue in validation.issues]
    assert "COLUMN_FIELD_UNKNOWN" not in _validation_codes(validation)


def test_end_to_end_system_filled_columns_reach_a_valid_definition() -> None:
    """模型不给栏目 → 系统补齐 → 全链路 → 校验通过。"""
    result = _generate(_candidate())
    assert result["kind"] == "plan_candidate", result

    plan = _plan(result["candidate"])
    _template, draft = _to_definition(plan)

    validation = validate_definition(draft.model_dump(mode="json"))
    assert validation.passed, [issue.message for issue in validation.issues]


def _chat_body(message: str, *, session_id: str = "sess_audit_1", message_id: str = "msg_1"):
    """新建设计轮要带显式 sessionId + messageId（操作认领的幂等键）。"""
    return {
        "agentProtocolVersion": "1.0",
        "conversationId": "conv_audit",
        "sessionId": session_id,
        "messageId": message_id,
        "state": "ANALYZING",
        "message": message,
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    }


def test_end_to_end_broken_columns_fail_the_turn(client, monkeypatch) -> None:
    """走真实链路：模型给了坏栏目 → 本轮失败、允许重试，方案保持原样。

    只替换外部模型调用，其余（协调者、状态机、落库）都是真实实现。
    """
    from app.services.report_design_service import report_design_service

    candidate = _candidate(
        sections=[
            {
                "title": "设备耗电明细",
                "metricIntents": ["设备耗电"],
                "componentType": "table",
                "columns": [{"label": ""}],
            }
        ]
    )

    async def _model_call(messages: list[dict[str, Any]]) -> str:
        return json.dumps({"kind": "plan_candidate", "candidate": candidate})

    report_design_service.reset_for_tests(model_call=_model_call)

    response = client.post(
        "/api/agent/chat", json=_chat_body("给我一份设备耗电明细")
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == "PLAN_COLUMNS_UNRESOLVED", body
    # 方案没有被写进去（这一版根本没进库，用户可以改了重试）。
    assert body.get("domainObjects", {}).get("reportPlan") is None, body


def test_end_to_end_numeric_id_reaches_a_confirmed_draft(client, monkeypatch) -> None:
    """走真实链路：模型给数字 id → 方案 → 模板 → 草稿 → 校验通过。

    走真实的聊天路由与编排，只替换外部模型调用。
    """
    from app.services.report_design_service import report_design_service

    candidate = _candidate(
        sections=[
            {
                "title": "设备耗电明细",
                "metricIntents": ["设备耗电"],
                "componentType": "table",
                "columns": [{"id": 1, "label": "设备名称"}, {"id": 2, "label": "耗电量"}],
            }
        ]
    )

    async def _model_call(messages: list[dict[str, Any]]) -> str:
        return json.dumps({"kind": "plan_candidate", "candidate": candidate})

    report_design_service.reset_for_tests(model_call=_model_call)

    response = client.post(
        "/api/agent/chat", json=_chat_body("给我一份设备耗电明细")
    )

    assert response.status_code == 200
    body = response.json()
    assert body.get("error") is None, body
    plan = body["domainObjects"]["reportPlan"]
    assert [column["label"] for column in plan["sections"][0]["columns"]] == [
        "设备名称",
        "耗电量",
    ]
    # 数字 id 归一成字符串标识，能当键用。
    assert plan["sections"][0]["columns"][0]["id"] == "1"
