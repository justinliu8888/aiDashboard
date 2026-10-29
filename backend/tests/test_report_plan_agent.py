"""B 子任务：模型设计适配（``generate_candidate``）的行为测试。

测试只替换注入的外部 ``model_call`` —— 这是本模块唯一的外部边界。
消息构建、JSON 解析、结构校验和安全检查都是真实实现，不是替身。

本文件里的候选样例来自冻结契约的**共同测试样例**，用于验证结构，
不是生产数据、模板库资产，也不是固定模型回复。
真实模型未被调用，见交付报告。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.agents.report_plan_agent import generate_candidate


def run_async(coro: Any) -> Any:
    """本仓库没有 pytest-asyncio，同步用例里直接驱动协程。"""
    return asyncio.run(coro)


def model_returning(payload: Any) -> Any:
    """只回放一段模型文本的替身；不替换模块内部任何逻辑。"""

    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    async def _model_call(messages: list[dict[str, Any]]) -> str:
        return text

    return _model_call


#: 冻结契约里的共同测试样例（结构样例，不是生产数据）。
CONTRACT_CANDIDATE: dict[str, Any] = {
    "topic": "PUE 专题报表",
    "requirements": {
        "metrics": ["PUE"],
        "subject": None,
        "timeRange": None,
        "exclusions": ["设备耗电排名"],
    },
    "sections": [
        {
            "id": "section_pue",
            "title": "PUE 总览",
            "metricIntents": ["PUE"],
            "componentType": "metric_card",
        }
    ],
    "assumptions": ["PUE 统计口径待核实，数据待连接"],
    "suggestions": [],
    "referenceTemplates": [],
}


def test_model_receives_prompt_history_current_plan_and_new_message() -> None:
    """模型看到的东西是固定的：提示 → 历史 → 当前方案 → 本轮需求。"""
    captured: dict[str, Any] = {}

    async def _model_call(messages: list[dict[str, Any]]) -> str:
        captured["messages"] = messages
        return json.dumps({"kind": "plan_candidate", "candidate": CONTRACT_CANDIDATE})

    run_async(
        generate_candidate(
            message="再加一个趋势板块",
            current_plan={"id": "plan_1", "revision": 1, "topic": "PUE 专题报表"},
            history=[
                {"role": "user", "text": "生成 PUE 专题报表"},
                {"role": "assistant", "text": "好的"},
            ],
            model_call=_model_call,
        )
    )

    messages = captured["messages"]
    assert [item["role"] for item in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "user",
    ]
    assert "文字方案" in messages[0]["content"]
    assert messages[1]["content"] == "生成 PUE 专题报表"
    assert messages[2]["content"] == "好的"
    assert "PUE 专题报表" in messages[3]["content"]
    assert messages[4]["content"] == "再加一个趋势板块"


def test_necessary_clarification_is_returned_as_clarification() -> None:
    """主题或用途判断不了时，模型可以要一次具体澄清。"""
    result = run_async(
        generate_candidate(
            message="帮我出个报表",
            current_plan=None,
            history=[],
            model_call=model_returning(
                {
                    "kind": "clarification",
                    "question": "这份方案主要用来做日常巡检，还是给管理层汇报？",
                }
            ),
        )
    )

    assert result == {
        "kind": "clarification",
        "question": "这份方案主要用来做日常巡检，还是给管理层汇报？",
    }


def test_candidate_never_carries_submission_id_or_revision() -> None:
    """模型自己写的 id/revision 不能进到提交用的候选里。"""
    candidate = {**CONTRACT_CANDIDATE, "id": "plan_example_pue", "revision": 7}

    result = run_async(
        generate_candidate(
            message="生成 PUE 专题报表",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": candidate}),
        )
    )

    assert result["kind"] == "plan_candidate"
    assert "id" not in result["candidate"]
    assert "revision" not in result["candidate"]
    assert result["candidate"]["topic"] == "PUE 专题报表"
    # 板块 id 是对已有板块的引用，要留着给领域层核对。
    assert result["candidate"]["sections"][0]["id"] == "section_pue"


def test_modification_keeps_confirmed_scope_and_returns_updated_candidate() -> None:
    """改方案时，已确认的范围和排除项要留在更新后的候选里。"""
    current_plan = {
        "id": "plan_example_pue",
        "revision": 1,
        "topic": "PUE 专题报表",
        "requirements": {
            "metrics": ["PUE"],
            "subject": None,
            "timeRange": None,
            "exclusions": ["设备耗电排名"],
        },
        "sections": [
            {
                "id": "section_pue",
                "title": "PUE 总览",
                "metricIntents": ["PUE"],
                "componentType": "metric_card",
            }
        ],
        "assumptions": ["PUE 统计口径待核实，数据待连接"],
        "suggestions": [],
        "referenceTemplates": [],
    }
    updated = {
        "topic": "PUE 专题报表",
        "requirements": {
            "metrics": ["PUE"],
            "subject": None,
            "timeRange": None,
            "exclusions": ["设备耗电排名"],
        },
        "sections": [
            {
                "id": "section_pue",
                "title": "PUE 总览",
                "metricIntents": ["PUE"],
                "componentType": "metric_card",
            },
            {"title": "PUE 趋势", "metricIntents": ["PUE"], "componentType": "line_chart"},
        ],
        "assumptions": ["PUE 统计口径待核实，数据待连接"],
        "suggestions": [],
        "referenceTemplates": [],
    }

    result = run_async(
        generate_candidate(
            message="再加一个 PUE 趋势图",
            current_plan=current_plan,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": updated}),
        )
    )

    assert result["kind"] == "plan_candidate"
    candidate = result["candidate"]
    assert candidate["requirements"]["exclusions"] == ["设备耗电排名"]
    assert [item["componentType"] for item in candidate["sections"]] == [
        "metric_card",
        "line_chart",
    ]
    # 已有板块沿用原 id；新板块不自己编号，编号由领域层生成。
    assert candidate["sections"][0]["id"] == "section_pue"
    assert "id" not in candidate["sections"][1]


def test_candidate_without_topic_is_invalid_output() -> None:
    """结构不完整（缺主题）的候选不能当成方案交给下游。"""
    candidate = {
        key: value for key, value in CONTRACT_CANDIDATE.items() if key != "topic"
    }

    result = run_async(
        generate_candidate(
            message="生成 PUE 专题报表",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": candidate}),
        )
    )

    assert result["kind"] == "failure"
    assert result["code"] == "MODEL_OUTPUT_INVALID"
    # 安全错误：失败说明不回显模型原文。
    assert "PUE" not in result["message"]


def test_output_that_is_not_a_json_object_is_invalid_output() -> None:
    """模型说人话、给数组或给不认识的 kind，都算输出看不懂。"""
    for text in (
        "好的，我来帮你生成 PUE 报表，稍等。",
        json.dumps([{"kind": "plan_candidate"}]),
        json.dumps({"kind": "plan", "candidate": CONTRACT_CANDIDATE}),
        json.dumps({"kind": "clarification"}),
    ):
        result = run_async(
            generate_candidate(
                message="生成 PUE 专题报表",
                current_plan=None,
                history=[],
                model_call=model_returning(text),
            )
        )

        assert result["kind"] == "failure", text
        assert result["code"] == "MODEL_OUTPUT_INVALID", text


def test_json_wrapped_in_a_code_fence_is_still_parsed() -> None:
    """模型爱用代码块包裹 JSON，不能因此就当失败。"""
    text = "```json\n" + json.dumps(
        {"kind": "plan_candidate", "candidate": CONTRACT_CANDIDATE}, ensure_ascii=False
    ) + "\n```"

    result = run_async(
        generate_candidate(
            message="生成 PUE 专题报表",
            current_plan=None,
            history=[],
            model_call=model_returning(text),
        )
    )

    assert result["kind"] == "plan_candidate"
    assert result["candidate"]["topic"] == "PUE 专题报表"


def test_model_call_error_returns_safe_failure() -> None:
    """模型调用抛错时，返回可辨识的安全失败，不把上游细节带给调用方。"""

    async def _boom(messages: list[dict[str, Any]]) -> str:
        raise RuntimeError("upstream 500: engine token sk-abcdefg")

    result = run_async(
        generate_candidate(
            message="生成 PUE 专题报表",
            current_plan=None,
            history=[],
            model_call=_boom,
        )
    )

    assert result["kind"] == "failure"
    assert result["code"] == "MODEL_CALL_FAILED"
    assert "sk-abcdefg" not in result["message"]
    assert "upstream" not in result["message"]


def test_sql_in_candidate_is_rejected_as_unsafe() -> None:
    """方案里出现可执行查询，就是不安全内容，不能当设计意图交出去。"""
    candidate = {
        **CONTRACT_CANDIDATE,
        "sections": [
            {
                "title": "PUE 总览",
                "metricIntents": ["SELECT * FROM energy_record"],
                "componentType": "metric_card",
            }
        ],
    }

    result = run_async(
        generate_candidate(
            message="生成 PUE 专题报表",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": candidate}),
        )
    )

    assert result["kind"] == "failure"
    assert result["code"] == "UNSAFE_CONTENT"


def test_data_source_and_execution_fields_are_rejected_as_unsafe() -> None:
    """数据源绑定、凭证和执行计划字段都不该出现在方案里。"""
    for extra in (
        {"sql": "SELECT 1 FROM energy"},
        {"dataSource": "catalog.machine_room.energy"},
        {"sourceRef": "catalog.machine_room.energy"},
        {"schedule": "0 0 * * *"},
        {"credential": "ak-1234567890"},
        {"executionPlan": "先取数再渲染"},
    ):
        candidate = {**CONTRACT_CANDIDATE, **extra}
        result = run_async(
            generate_candidate(
                message="生成 PUE 专题报表",
                current_plan=None,
                history=[],
                model_call=model_returning(
                    {"kind": "plan_candidate", "candidate": candidate}
                ),
            )
        )

        assert result["kind"] == "failure", extra
        assert result["code"] == "UNSAFE_CONTENT", extra


def test_credentials_in_values_are_rejected_as_unsafe() -> None:
    """凭证藏在文本字段里同样要拦下来。"""
    candidate = {**CONTRACT_CANDIDATE, "assumptions": ["取数用 password=hunter2"]}

    result = run_async(
        generate_candidate(
            message="生成 PUE 专题报表",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": candidate}),
        )
    )

    assert result["kind"] == "failure"
    assert result["code"] == "UNSAFE_CONTENT"


def test_real_business_values_are_rejected_as_unsafe() -> None:
    """方案里出现真实业务数值，就是拿数据冒充设计意图。"""
    candidate = {
        **CONTRACT_CANDIDATE,
        "sections": [{**CONTRACT_CANDIDATE["sections"][0], "value": 1.35}],
    }

    result = run_async(
        generate_candidate(
            message="生成 PUE 专题报表",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": candidate}),
        )
    )

    assert result["kind"] == "failure"
    assert result["code"] == "UNSAFE_CONTENT"


def test_catalog_codes_are_rejected_as_unsafe() -> None:
    """指标要写用户说的意图，不能写数据目录里的编码。"""
    candidate = {
        **CONTRACT_CANDIDATE,
        "requirements": {
            **CONTRACT_CANDIDATE["requirements"],
            "metrics": ["metric.machine_room.total_energy"],
        },
    }

    result = run_async(
        generate_candidate(
            message="生成 PUE 专题报表",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": candidate}),
        )
    )

    assert result["kind"] == "failure"
    assert result["code"] == "UNSAFE_CONTENT"


def test_inferred_formula_is_rejected_as_unsafe() -> None:
    """没有确认口径的指标不能自己补公式。"""
    candidate = {
        **CONTRACT_CANDIDATE,
        "requirements": {
            **CONTRACT_CANDIDATE["requirements"],
            "metrics": ["PUE = IT负载/总能耗"],
        },
    }

    result = run_async(
        generate_candidate(
            message="生成 PUE 专题报表",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": candidate}),
        )
    )

    assert result["kind"] == "failure"
    assert result["code"] == "UNSAFE_CONTENT"


def test_reference_template_version_is_allowed() -> None:
    """参考模板版本号是契约允许的正整数，不算真实业务数值。"""
    candidate = {
        **CONTRACT_CANDIDATE,
        "referenceTemplates": [
            {
                "assetId": "tpl_pue",
                "version": 2,
                "name": "PUE 模板",
                "reason": "板块结构接近",
            }
        ],
    }

    result = run_async(
        generate_candidate(
            message="生成 PUE 专题报表",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": candidate}),
        )
    )

    assert result["kind"] == "plan_candidate"
    assert result["candidate"]["referenceTemplates"][0]["version"] == 2


def test_emit_reports_progress_without_changing_the_result() -> None:
    """可选的进度回调只报告进度，不改变本轮结论。"""
    events: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    result = run_async(
        generate_candidate(
            message="生成 PUE 专题报表",
            current_plan=None,
            history=[],
            model_call=model_returning(
                {"kind": "plan_candidate", "candidate": CONTRACT_CANDIDATE}
            ),
            emit=_emit,
        )
    )

    assert result["kind"] == "plan_candidate"
    assert [event["event"] for event in events] == [
        "plan_design_started",
        "plan_design_finished",
    ]


def test_emit_failure_does_not_break_the_result() -> None:
    """进度回调自己出错，不能把设计结果一起弄挂。"""

    def _boom(event: dict[str, Any]) -> None:
        raise RuntimeError("SSE 断了")

    result = run_async(
        generate_candidate(
            message="生成 PUE 专题报表",
            current_plan=None,
            history=[],
            model_call=model_returning(
                {"kind": "plan_candidate", "candidate": CONTRACT_CANDIDATE}
            ),
            emit=_boom,
        )
    )

    assert result["kind"] == "plan_candidate"


def test_system_prompt_states_the_rules_the_plan_contract_relies_on() -> None:
    """提示词本身是交付物，必须写清契约依赖的那几条规则。"""
    captured: dict[str, Any] = {}

    async def _model_call(messages: list[dict[str, Any]]) -> str:
        captured["messages"] = messages
        return json.dumps({"kind": "plan_candidate", "candidate": CONTRACT_CANDIDATE})

    run_async(
        generate_candidate(
            message="生成 PUE 专题报表",
            current_plan=None,
            history=[],
            model_call=_model_call,
        )
    )

    prompt = captured["messages"][0]["content"]
    for expected in (
        "revision",  # 模型不分配提交 ID 和版本
        "真实数值",  # 不写真实业务数值
        "数据源",  # 不绑定数据源
        "SQL",  # 不写可执行内容
        "凭证",  # 不写凭证
        "原样保留",  # 用户明确的条件原样保留
        "待核实",  # 未知口径标为待核实
        "缺数据不阻断设计",  # 缺数据也要给设计
        "metric_card",
        "line_chart",
        "bar_chart",
        "pie_chart",
        "clarification",
    ):
        assert expected in prompt, expected


def test_missing_data_does_not_block_the_design() -> None:
    """没有数据、目录里查不到，也不该把设计拦下来。"""
    candidate = {
        **CONTRACT_CANDIDATE,
        "requirements": {
            "metrics": ["PUE"],
            "subject": "A 机房",
            "timeRange": "昨日",
            "exclusions": ["设备耗电排名"],
        },
        "assumptions": ["PUE 统计口径待核实，数据待连接"],
    }

    result = run_async(
        generate_candidate(
            message="生成 A 机房昨日的 PUE 报表",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": candidate}),
        )
    )

    assert result["kind"] == "plan_candidate"
    assert result["candidate"]["assumptions"] == ["PUE 统计口径待核实，数据待连接"]
    assert result["candidate"]["requirements"]["timeRange"] == "昨日"


def test_clarification_without_a_question_is_invalid_output() -> None:
    """要澄清就得给出具体问题，空问题等于没结论。"""
    result = run_async(
        generate_candidate(
            message="帮我出个报表",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "clarification", "question": "   "}),
        )
    )

    assert result["kind"] == "failure"
    assert result["code"] == "MODEL_OUTPUT_INVALID"


def test_emit_reports_a_failure_event_when_the_turn_fails() -> None:
    """失败也要给回调一个明确的结束事件，免得界面一直转。"""
    events: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    result = run_async(
        generate_candidate(
            message="生成 PUE 专题报表",
            current_plan=None,
            history=[],
            model_call=model_returning("我不知道你在说什么"),
            emit=_emit,
        )
    )

    assert result["kind"] == "failure"
    assert [event["event"] for event in events] == [
        "plan_design_started",
        "plan_design_finished",
        "plan_design_failed",
    ]


def test_model_returning_something_other_than_text_is_invalid_output() -> None:
    """模型替身返回的不是文本时，按输出看不懂处理，不抛异常。"""

    async def _model_call(messages: list[dict[str, Any]]) -> Any:
        return {"kind": "plan_candidate"}

    result = run_async(
        generate_candidate(
            message="生成 PUE 专题报表",
            current_plan=None,
            history=[],
            model_call=_model_call,
        )
    )

    assert result["kind"] == "failure"
    assert result["code"] == "MODEL_OUTPUT_INVALID"


def test_valid_candidate_comes_back_as_plan_candidate() -> None:
    """模型给出合法候选时，返回 plan_candidate 且内容原样保留。"""
    result = run_async(
        generate_candidate(
            message="生成 PUE 专题报表，不要设备耗电排名",
            current_plan=None,
            history=[],
            model_call=model_returning(
                {"kind": "plan_candidate", "candidate": CONTRACT_CANDIDATE}
            ),
        )
    )

    assert result["kind"] == "plan_candidate"
    candidate = result["candidate"]
    assert candidate["topic"] == "PUE 专题报表"
    assert candidate["requirements"]["metrics"] == ["PUE"]
    assert candidate["requirements"]["exclusions"] == ["设备耗电排名"]
    assert candidate["sections"][0]["componentType"] == "metric_card"
    assert candidate["assumptions"] == ["PUE 统计口径待核实，数据待连接"]


# ---------- 选型与栏目（20261029 规格「跨工单契约」） ----------


def _table_candidate(**overrides) -> dict[str, Any]:
    candidate = {
        "topic": "机房运行日报",
        "requirements": {
            "metrics": ["告警"],
            "subject": None,
            "timeRange": "最近 7 天",
            "exclusions": [],
        },
        "sections": [
            {
                "title": "当前告警列表",
                "metricIntents": ["告警"],
                "componentType": "table",
                "displayIntent": "records",
            }
        ],
        "assumptions": [],
        "suggestions": [],
        "referenceTemplates": [],
    }
    candidate.update(overrides)
    return candidate


def test_the_display_purpose_and_the_grain_unit_survive_normalization() -> None:
    """模型给的语义候选原样带进候选，不在适配层丢掉。"""
    candidate = _table_candidate()
    candidate["requirements"]["grainUnit"] = "HOUR"

    result = run_async(
        generate_candidate(
            message="给我一份告警列表",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": candidate}),
        )
    )

    assert result["kind"] == "plan_candidate"
    assert result["candidate"]["sections"][0]["displayIntent"] == "records"
    assert result["candidate"]["requirements"]["grainUnit"] == "HOUR"


def test_the_model_columns_are_kept_in_the_candidate() -> None:
    candidate = _table_candidate()
    candidate["sections"][0]["columns"] = [{"label": "设备名称"}, {"label": "告警级别"}]

    result = run_async(
        generate_candidate(
            message="给我一份告警列表",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": candidate}),
        )
    )

    assert result["kind"] == "plan_candidate"
    assert [column["label"] for column in result["candidate"]["sections"][0]["columns"]] == [
        "设备名称",
        "告警级别",
    ]


def test_missing_columns_are_completed_by_the_system_and_written_into_the_candidate() -> None:
    """模型漏写栏目时系统替他补上，补的内容要出现在方案里，用户能看见。"""
    result = run_async(
        generate_candidate(
            message="给我一份告警列表",
            current_plan=None,
            history=[],
            model_call=model_returning(
                {"kind": "plan_candidate", "candidate": _table_candidate()}
            ),
        )
    )

    assert result["kind"] == "plan_candidate"
    columns = result["candidate"]["sections"][0]["columns"]
    assert [column["label"] for column in columns] == ["设备名称", "时间", "告警级别"]
    assert any(
        line.startswith("栏目：") and "告警级别" in line
        for line in result["candidate"]["assumptions"]
    )


def test_columns_that_cannot_be_guessed_come_back_as_a_clarification() -> None:
    """猜不出来就追问：走澄清通道，问题要具体。"""
    candidate = _table_candidate()
    candidate["sections"][0]["title"] = "运维工单明细"
    candidate["sections"][0]["metricIntents"] = ["工单"]

    result = run_async(
        generate_candidate(
            message="给我一份运维工单明细",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": candidate}),
        )
    )

    assert result["kind"] == "clarification"
    assert "运维工单明细" in result["question"]
    assert "栏目" in result["question"]


def test_a_broken_column_structure_comes_back_as_a_retryable_failure() -> None:
    """修正仍失败时明确报生成失败并允许重试，不返回空表、不宣称成功。"""
    candidate = _table_candidate()
    candidate["sections"][0]["columns"] = "设备名称,时间"

    result = run_async(
        generate_candidate(
            message="给我一份告警列表",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": candidate}),
        )
    )

    assert result["kind"] == "failure"
    assert result["code"] == "PLAN_COLUMNS_UNRESOLVED"
    assert "重试" in result["message"]


def test_the_model_never_marks_a_component_type_as_user_specified() -> None:
    """模型自己选的类型不算「用户明确指定」。"""
    candidate = _table_candidate()
    candidate["sections"][0]["explicitComponentType"] = "table"

    result = run_async(
        generate_candidate(
            message="给我一份告警列表",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": candidate}),
        )
    )

    assert result["kind"] == "plan_candidate"
    assert "explicitComponentType" not in result["candidate"]["sections"][0]


def test_an_explicit_type_that_matches_no_section_comes_back_as_a_clarification() -> None:
    """指令对不上板块就澄清，不偷偷改其他板块。"""
    candidate = _table_candidate()
    candidate["sections"] = [
        {"title": "总耗电", "metricIntents": ["总耗电"], "componentType": "metric_card"},
        {"title": "各机房耗电对比", "metricIntents": ["总耗电"], "componentType": "metric_card"},
    ]

    result = run_async(
        generate_candidate(
            message="用柱状图看",
            current_plan=None,
            history=[],
            model_call=model_returning({"kind": "plan_candidate", "candidate": candidate}),
        )
    )

    assert result["kind"] == "clarification"
    assert "柱状图" in result["question"]
