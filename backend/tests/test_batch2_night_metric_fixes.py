"""第二批夜间修复：指标卡改名 / 标题同步 / 格式合并 / 能力格式入口归一。

对应验收反馈 08 的已确认问题：

* **08-01** 改名接受后「没生效」：``SET_METRIC_ITEM`` 只改了 ``metric.label``，
  外层区块业务名称 ``region.name`` 还停在旧指标含义上；而且同一个名字改两次，
  第二次也说「已改：名称」，和「没有改变区块内容」自相矛盾。
* **08-02** 外层标题与内部指标含义不一致（``9 台`` 挂在「昨日总耗电」下）。
* **08-03** 只改整数 + 千分位时，单位标签被整份格式替换掉（单位串掉）。
* **08-05** 单指标卡转多指标组：模型提交的目标键写错、``metrics`` 不是列表、
  布尔写成字符串 —— 整轮试做失败，用户只拿到一句笼统失败。

这里的断言全部落在**能力层的真实执行**上（``run_trial`` 在草稿副本上跑），
不碰正式草稿。
"""
from __future__ import annotations

import json

import pytest

from app.schemas.report_definition import ReportDefinitionDraft
from app.services.edit_trial_service import run_trial, summarize_facts


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

def _draft() -> ReportDefinitionDraft:
    """一份带「昨日总耗电」指标卡与「总告警数」指标卡的草稿。"""
    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "A 机房昨日能耗日报", "revision": 13},
            "regions": [
                {
                    "id": "region_2",
                    "name": "昨日总耗电",
                    "role": "SUMMARY",
                    "component": {
                        "type": "METRIC_CARD",
                        "metric": {
                            "id": "m_total",
                            "label": "昨日总耗电",
                            "fieldRef": "energyKwh",
                            "format": {
                                "kind": "NUMBER",
                                "precision": 1,
                                "unitLabel": "kWh",
                            },
                        },
                    },
                    "data": {
                        "inputs": {"main": {"type": "SIMULATED"}},
                        "pipeline": {"nodes": [], "outputs": {"main": {"nodeRef": "node:n"}}},
                    },
                    "binding": {"outputRef": "output:main"},
                    "templateBinding": {"previewSpec": {"kind": "energy", "unit": "kWh"}},
                },
                {
                    "id": "region_总告警数",
                    "name": "总告警数",
                    "role": "SUMMARY",
                    "component": {
                        "type": "METRIC_CARD",
                        "metric": {
                            "id": "m_1",
                            "label": "总告警数",
                            "fieldRef": "totalAlarmCount",
                            "format": {"kind": "NUMBER", "unitLabel": "条"},
                        },
                    },
                    "data": {
                        "inputs": {"main": {"type": "SIMULATED"}},
                        "pipeline": {"nodes": [], "outputs": {"main": {"nodeRef": "node:n"}}},
                    },
                    "binding": {"outputRef": "output:main"},
                    "templateBinding": {"previewSpec": {"kind": "generic"}},
                },
            ],
        }
    )


@pytest.fixture()
def draft():
    return _draft()


def _metric_of(definition, region_id: str) -> dict:
    for region in definition.regions or []:
        if region.get("id") == region_id:
            return (region.get("component") or {}).get("metric") or {}
    raise AssertionError(f"区域 {region_id} 不在定义里")


def _region_of(definition, region_id: str) -> dict:
    for region in definition.regions or []:
        if region.get("id") == region_id:
            return region
    raise AssertionError(f"区域 {region_id} 不在定义里")


# --------------------------------------------------------------------------
# 08-01：改名要真的看得见
# --------------------------------------------------------------------------

def test_rename_syncs_region_name_and_keeps_binding(draft):
    """改名：指标名称与区块业务名称**同步**，数值绑定与单位不变。"""
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "SET_METRIC_ITEM",
                "target": {"regionId": "region_2"},
                "params": {"label": "昨日总耗电量"},
            }
        ],
    )
    assert result.ok, result.errors
    region = _region_of(result.definition, "region_2")
    metric = _metric_of(result.definition, "region_2")
    assert metric["label"] == "昨日总耗电量"
    # 外层区块业务名称同步 —— 用户看到的卡片标题跟着改。
    assert region["name"] == "昨日总耗电量"
    # 数值绑定、单位、格式**没有**被动过。
    assert metric["fieldRef"] == "energyKwh"
    assert metric["format"]["unitLabel"] == "kWh"
    assert metric["format"]["precision"] == 1


def test_rename_summary_does_not_claim_unchanged(draft):
    """改名后待确认说明**不能**说「没有改变区块内容」。"""
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "SET_METRIC_ITEM",
                "target": {"regionId": "region_2"},
                "params": {"label": "昨日总耗电量"},
            }
        ],
    )
    assert result.ok, result.errors
    summary = summarize_facts(result.facts)
    assert "没有改变区块内容" not in summary
    assert "昨日总耗电量" in summary


def test_second_identical_rename_reports_no_actual_change(draft):
    """同一个名字改第二次：**如实说没有实际改动**，不说「已改：名称」。"""
    first = run_trial(
        definition=draft,
        operations=[
            {
                "op": "SET_METRIC_ITEM",
                "target": {"regionId": "region_2"},
                "params": {"label": "昨日总耗电量"},
            }
        ],
    )
    assert first.ok, first.errors
    second = run_trial(
        definition=first.definition,
        operations=[
            {
                "op": "SET_METRIC_ITEM",
                "target": {"regionId": "region_2"},
                "params": {"label": "昨日总耗电量"},
            }
        ],
    )
    assert second.ok, second.errors
    message = second.resolved_operations[0]["message"]
    assert "没有实际改动" in message
    assert "已改" not in message
    # 没有实际差异 → 事实里没有 modified → 摘要如实说没变。
    assert not second.facts.get("modified")


# --------------------------------------------------------------------------
# 08-03：只改格式时，单位标签不能被整份替换掉
# --------------------------------------------------------------------------

def test_format_only_change_keeps_unit_label(draft):
    """「改成整数并加千分位」：没提单位 → 单位标签**沿用**。"""
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "SET_METRIC_ITEM",
                "target": {"regionId": "region_2"},
                "params": {
                    "format": {"kind": "NUMBER", "thousandSeparator": True}
                },
            }
        ],
    )
    assert result.ok, result.errors
    metric = _metric_of(result.definition, "region_2")
    assert metric["format"]["unitLabel"] == "kWh"
    assert metric["format"]["thousandSeparator"] is True
    # 精度没提 → **整数显示**（小数位不再出现），这正是「改成整数」的意思。
    assert metric["format"].get("precision") in (None, 0)


def test_format_accepts_string_boolean(draft):
    """``"true"`` 是等价写法 —— 不该换来一次整轮失败。"""
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "SET_METRIC_ITEM",
                "target": {"regionId": "region_2"},
                "params": {"format": {"thousandSeparator": "true"}},
            }
        ],
    )
    assert result.ok, result.errors
    metric = _metric_of(result.definition, "region_2")
    assert metric["format"]["thousandSeparator"] is True


# --------------------------------------------------------------------------
# 08-05：单指标卡转多指标组 + 能力格式入口归一
# --------------------------------------------------------------------------

def test_single_card_to_metric_group(draft):
    """单指标卡 → 同一区域的三指标组（08-05 的原话场景）。"""
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "SET_METRIC_GROUP",
                "target": {"regionId": "region_总告警数"},
                "params": {
                    "create": True,
                    "metrics": [
                        {
                            "id": "m_1",
                            "label": "总告警数",
                            "fieldRef": "totalAlarmCount",
                            "format": {"kind": "NUMBER", "unitLabel": "条"},
                        },
                        {
                            "label": "严重告警数",
                            "fieldRef": "seriousAlarmCount",
                            "format": {"kind": "NUMBER", "unitLabel": "条"},
                        },
                        {
                            "label": "处理中告警数",
                            "fieldRef": "processingAlarmCount",
                            "format": {"kind": "NUMBER", "unitLabel": "条"},
                        },
                    ],
                },
            }
        ],
    )
    assert result.ok, result.errors
    component = _region_of(result.definition, "region_总告警数")["component"]
    assert component["type"] == "METRIC_GROUP"
    labels = [item["label"] for item in component["metrics"]]
    assert labels == ["总告警数", "严重告警数", "处理中告警数"]
    # 组名沿用原来的区块业务名称（多指标组**不强制**跟组内项名同步）。
    assert _region_of(result.definition, "region_总告警数")["name"] == "总告警数"
    # 其它区块不受影响。
    assert _metric_of(result.definition, "region_2")["fieldRef"] == "energyKwh"


def test_target_key_alias_is_normalised(draft):
    """目标键写成 ``target`` / ``id`` —— 说的是同一件事，归一掉。"""
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "SET_METRIC_ITEM",
                "target": {"target": "region_2"},
                "params": {"label": "昨日总耗电量"},
            }
        ],
    )
    assert result.ok, result.errors
    assert _metric_of(result.definition, "region_2")["label"] == "昨日总耗电量"


def test_metrics_as_json_string_is_normalised(draft):
    """``metrics`` 写成 JSON 字符串 —— 归一后照样能执行。"""
    metrics = [
        {
            "id": "m_1",
            "label": "总告警数",
            "fieldRef": "totalAlarmCount",
            "format": {"kind": "NUMBER", "unitLabel": "条"},
        },
    ]
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "SET_METRIC_GROUP",
                "target": {"regionId": "region_总告警数"},
                "params": {"create": True, "metrics": json.dumps(metrics, ensure_ascii=False)},
            }
        ],
    )
    assert result.ok, result.errors
    component = _region_of(result.definition, "region_总告警数")["component"]
    assert component["type"] == "METRIC_GROUP"
    assert len(component["metrics"]) == 1


def test_missing_target_lists_available_regions(draft):
    """目标没写对时，错误里要**列出可选区块** —— 用户/模型才知道选哪个。"""
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "SET_METRIC_ITEM",
                "target": {},
                "params": {"label": "随便改个名"},
            }
        ],
    )
    assert not result.ok
    message = result.errors[0]["message"]
    assert "region_2" in message
    assert "昨日总耗电" in message


# --------------------------------------------------------------------------
# 08-05 的**服务层**闭环：试做失败 → 草稿不变 → 仍有恢复入口
# --------------------------------------------------------------------------

from app.services.agent_service import agent_service  # noqa: E402
from app.agents.agentscope_report_agent import AgentScopeReportAgent  # noqa: E402
from app.tools import registry  # noqa: E402
from app.tools.context import ToolContext, bind_tool_context, clear_tool_context  # noqa: E402
from app.tools.schemas import EditPlanPayload  # noqa: E402
from app.tools.submission import bind_submission, take_submission  # noqa: E402
from app.schemas.agent import AgentRequest, AgentResponse  # noqa: E402


class _StubAgent:
    """桩 Agent：按固定清单提交操作。

    走的是**真实**的试做、错误反馈与候选组装 —— 只有「模型选了哪份清单」是固定的。
    """

    def __init__(self, operations):
        self.operations = operations
        self.last_tool_result = None

    def _tool_context(self, request):
        ctx = request.context
        return ToolContext(
            state=request.state,
            subject_ref=ctx.subjectRef,
            subject_id=ctx.subjectId,
            project_id=ctx.projectId,
            selected_region_id=ctx.selectedRegionId,
            base_definition_revision=ctx.baseDefinitionRevision,
            definition=request.snapshots.reportDefinition,
            policy=request.snapshots.reportPolicy,
        )

    def handle(self, request: AgentRequest) -> AgentResponse:
        bind_tool_context(self._tool_context(request))
        bind_submission()
        try:
            result = registry.get_runner("submit_edit_plan")(
                operations=self.operations,
                originalRequest=request.message,
            )
            self.last_tool_result = result
            submission = take_submission()
        finally:
            clear_tool_context()
        real = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
        if submission is None:
            # 试做失败：没有提交 → 走「没有可解析结论」那条路（08-05 的现场）。
            return real._unavailable_edit_response(request)
        plan = EditPlanPayload.model_validate(submission["payload"])
        return real._assemble_capability_candidate(request, plan)


def _snap(definition):
    return {"reportDefinition": definition.model_dump(mode="json")}


def test_failed_trial_keeps_draft_and_rebuilds_recovery_entry(post_chat, draft, monkeypatch):
    """08-05 的失败形状：草稿不变，且**必须留下能继续的入口**。"""
    monkeypatch.setattr(
        agent_service,
        "_agent",
        _StubAgent(
            [
                {
                    "op": "SET_METRIC_GROUP",
                    # 目标键写错（模型把 target 当成 regionId 用）、metrics 不是列表。
                    "target": {"target": ""},
                    "params": {"create": True, "metrics": "不是列表"},
                }
            ]
        ),
    )
    response = post_chat(
        "把「总告警数」这个区块改成一个多指标组，组里有「总告警数」「严重告警数」「处理中告警数」三个指标项。",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(draft),
    ).json()

    # 正式草稿与版本都没动。
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert response["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 13
    # 失败原因要具体，不能只给一句笼统失败。
    assert response["error"] is not None
    assert "草稿没有修改" in response["reply"] or "没有完成" in response["reply"]
    # **恢复入口**：受限修改输入卡要真的在响应里（08-05 的核心问题）。
    kinds = [q.get("stepKind") for q in response["questions"]]
    assert "edit_request" in kinds, response["questions"]


def test_normalised_operation_reaches_candidate_and_applies(post_chat, draft, monkeypatch):
    """形状归一后：同样的诉求要能形成候选，接受后真的变成三指标组。"""
    monkeypatch.setattr(
        agent_service,
        "_agent",
        _StubAgent(
            [
                {
                    "op": "SET_METRIC_GROUP",
                    "target": {"regionId": "region_总告警数"},
                    "params": {
                        "create": True,
                        "metrics": [
                            {
                                "id": "m_1",
                                "label": "总告警数",
                                "fieldRef": "totalAlarmCount",
                                "format": {"kind": "NUMBER", "unitLabel": "条"},
                            },
                            {
                                "label": "严重告警数",
                                "fieldRef": "seriousAlarmCount",
                                "format": {"kind": "NUMBER", "unitLabel": "条"},
                            },
                            {
                                "label": "处理中告警数",
                                "fieldRef": "processingAlarmCount",
                                "format": {"kind": "NUMBER", "unitLabel": "条"},
                            },
                        ],
                    },
                }
            ]
        ),
    )
    candidate = post_chat(
        "把「总告警数」这个区块改成一个多指标组，组里有「总告警数」「严重告警数」「处理中告警数」三个指标项。",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(draft),
    ).json()
    assert candidate["domainObjects"]["pendingEditPlan"] is not None, candidate["reply"]

    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(draft),
    ).json()
    regions = {
        r["id"]: r for r in accepted["domainObjects"]["reportDefinitionDraft"]["regions"]
    }
    component = regions["region_总告警数"]["component"]
    assert component["type"] == "METRIC_GROUP"
    assert [m["label"] for m in component["metrics"]] == [
        "总告警数",
        "严重告警数",
        "处理中告警数",
    ]


def test_metric_group_request_is_not_hijacked_by_legacy_rule(post_chat, draft, monkeypatch):
    """07 第 14/16 步：「指标组」不能被规则层当成「更换指标」抢走。

    规则层认「指标」→ 会产出一个目录外的指标候选，用户点接受时直接失败
    （验收 08-05）。指标组是**能力层**（``SET_METRIC_GROUP``）的概念，
    规则层必须放行，让能力层按能力格式处理。
    """
    stub = _StubAgent(
        [
            {
                "op": "SET_METRIC_GROUP",
                "target": {"regionId": "region_总告警数"},
                "params": {
                    "create": True,
                    "metrics": [
                        {
                            "id": "m_1",
                            "label": "总告警数",
                            "fieldRef": "totalAlarmCount",
                            "format": {"kind": "NUMBER", "unitLabel": "条"},
                        },
                    ],
                },
            }
        ]
    )
    monkeypatch.setattr(agent_service, "_agent", stub)
    response = post_chat(
        "把「总告警数」指标组改成横向排列、两列，间距 16 像素。",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(draft),
    ).json()
    # 规则层没有抢走：模型（能力层）被调用了。
    assert stub.last_tool_result is not None
    # 出来的是能力格式的候选，不是旧的「更换指标」。
    pending = response["domainObjects"].get("pendingEditPlan")
    assert pending is not None, response["reply"]
    assert "更换指标" not in pending["summary"]


def test_unit_follows_new_field_when_binding_changes(draft):
    """换字段 → 单位跟着**新字段**走，不把旧字段的单位标签带过去（验收 08-03）。

    先把卡片绑到「运行设备数」（单位 台），再换成「昨日总耗电」（单位 kWh）：
    单位标签必须变成 kWh，不能停在「台」。
    """
    first = run_trial(
        definition=draft,
        operations=[
            {
                "op": "SET_METRIC_ITEM",
                "target": {"regionId": "region_2"},
                "params": {
                    "fieldRef": "runningDeviceCount",
                    "label": "运行设备数",
                    "format": {"kind": "NUMBER", "unitLabel": "台"},
                },
            }
        ],
    )
    assert first.ok, first.errors
    assert _metric_of(first.definition, "region_2")["format"]["unitLabel"] == "台"

    result = run_trial(
        definition=first.definition,
        operations=[
            {
                "op": "SET_METRIC_ITEM",
                "target": {"regionId": "region_2"},
                "params": {
                    "fieldRef": "energyKwh",
                    "label": "耗电格式验证",
                    "format": {"kind": "NUMBER", "precision": 1},
                },
            }
        ],
    )
    assert result.ok, result.errors
    fmt = _metric_of(result.definition, "region_2")["format"]
    assert fmt["unitLabel"] == "kWh", fmt
    assert fmt["precision"] == 1


def test_mismatched_unit_label_is_rejected(draft):
    """新字段的单位是 kWh，却写「台」 → **明确拒绝**，不能悄悄换。"""
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "SET_METRIC_ITEM",
                "target": {"regionId": "region_2"},
                "params": {
                    "fieldRef": "energyKwh",
                    "format": {"kind": "NUMBER", "unitLabel": "台"},
                },
            }
        ],
    )
    assert not result.ok
    assert "单位" in result.errors[0]["message"]


def test_energy_kwh_is_registered_with_its_unit():
    """``energyKwh``（模板指标卡绑的取值键）必须在模拟字段目录里登记单位。"""
    from app.services.display_binding_service import simulated_field

    spec = simulated_field("energyKwh")
    assert spec is not None, "energyKwh 没登记 —— 查不到单位就会串单位"
    assert spec["unit"] == "kWh"


# --------------------------------------------------------------------------
# 08-05 第二层：模型提交的对象形状、目标别名、空目标
# --------------------------------------------------------------------------

def test_object_shaped_metrics_is_normalised(draft):
    """模型把 ``metrics`` 序列化成**对象** —— 归一成列表后照样执行。

    现场证据：edit-trace 里模型连续两轮提交
    ``{"op": "SET_METRIC_GROUP", "target": {"target": ""}, ...}``，
    trial 报「metrics 必须是列表…你提交的是一个对象」。
    """
    metrics = {
        "m_1": {
            "id": "m_1",
            "label": "总告警数",
            "fieldRef": "totalAlarmCount",
            "format": {"kind": "NUMBER", "unitLabel": "条"},
        },
        "m_2": {
            "label": "严重告警数",
            "fieldRef": "seriousAlarmCount",
            "format": {"kind": "NUMBER", "unitLabel": "条"},
        },
    }
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "SET_METRIC_GROUP",
                "target": {"regionId": "region_总告警数"},
                "params": {"create": True, "metrics": metrics},
            }
        ],
    )
    assert result.ok, result.errors
    component = _region_of(result.definition, "region_总告警数")["component"]
    assert component["type"] == "METRIC_GROUP"
    assert [m["label"] for m in component["metrics"]] == ["总告警数", "严重告警数"]


def test_target_alias_is_kept_by_the_contract():
    """目标写成 ``{"target": "region_x"}`` —— 契约层就归一，别半路丢掉。"""
    from app.schemas.edit_plan import EditOperationTarget

    target = EditOperationTarget.model_validate({"target": "region_2"})
    assert target.regionId == "region_2"


def test_empty_target_is_filled_from_user_message(post_chat, draft, monkeypatch):
    """目标空着，但用户原话**点名了唯一一块** → 补上，别让用户重说一遍。"""
    from app.services.agent_service import agent_service

    stub = _StubAgent(
        [
            {
                "op": "SET_METRIC_GROUP",
                # 模型把目标键写错、值也空（现场就是这个形状）。
                "target": {"target": ""},
                "params": {
                    "create": True,
                    "metrics": {
                        "m_1": {
                            "id": "m_1",
                            "label": "总告警数",
                            "fieldRef": "totalAlarmCount",
                            "format": {"kind": "NUMBER", "unitLabel": "条"},
                        },
                    },
                },
            }
        ]
    )
    monkeypatch.setattr(agent_service, "_agent", stub)
    response = post_chat(
        "把「总告警数」这个区块改成一个多指标组。",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(draft),
    ).json()
    pending = response["domainObjects"].get("pendingEditPlan")
    assert pending is not None, response["reply"]
    assert "更换指标" not in pending["summary"]


# --------------------------------------------------------------------------
# 08-05 的第三层：模型提交**形状**还有几种等价写法（验收现场实证）
# --------------------------------------------------------------------------

def _metric_group_draft() -> ReportDefinitionDraft:
    """一份已经是多指标组的草稿（组里两项）。"""
    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "A 机房昨日能耗日报", "revision": 3},
            "regions": [
                {
                    "id": "region_总告警数",
                    "name": "总告警数",
                    "role": "SUMMARY",
                    "component": {
                        "type": "METRIC_GROUP",
                        "title": "总告警数",
                        "metrics": [
                            {
                                "id": "m_1",
                                "label": "总告警数",
                                "fieldRef": "totalAlarmCount",
                                "format": {"kind": "NUMBER", "unitLabel": "条"},
                            },
                            {
                                "id": "m_2",
                                "label": "严重告警数",
                                "fieldRef": "seriousAlarmCount",
                                "format": {"kind": "NUMBER", "unitLabel": "条"},
                            },
                        ],
                        "arrangement": {
                            "mode": "GRID",
                            "columns": 2,
                            "direction": "HORIZONTAL",
                            "gap": {"value": 12, "unit": "PX"},
                        },
                    },
                    "data": {
                        "inputs": {"main": {"type": "SIMULATED"}},
                        "pipeline": {"nodes": [], "outputs": {"main": {"nodeRef": "node:n"}}},
                    },
                    "binding": {"outputRef": "output:main"},
                    "templateBinding": {"previewSpec": {"kind": "generic"}},
                }
            ],
        }
    )


def test_arrangement_accepts_numeric_strings():
    """``columns`` / ``gap.value`` 写成字符串（"2" / "16"）—— 同一个值，换个写法。

    现场实证：模型连着两轮提交 ``columns: "2"``，都被「必须是整数」判死，
    第三轮干脆放弃能力格式改交旧格式。数字字符串不是别的值，收下它。
    """
    result = run_trial(
        definition=_metric_group_draft(),
        operations=[
            {
                "op": "SET_METRIC_GROUP",
                "target": {"target": "region_总告警数"},
                "params": {
                    "arrangement": {
                        "mode": "GRID",
                        "columns": "2",
                        "direction": "HORIZONTAL",
                        "gap": {"value": "16", "unit": "PX"},
                    }
                },
            }
        ],
    )
    assert result.ok, result.errors
    arrangement = result.definition.model_dump()["regions"][0]["component"]["arrangement"]
    assert arrangement["columns"] == 2, arrangement
    assert arrangement["gap"]["value"] == 16, arrangement


def test_region_id_written_into_ref_is_accepted():
    """模型把**区块 id** 写进 ``ref``（或两个键一起写）→ 认得出来。

    现场实证：目标键写错时用户只看到「引用必须属于本次修改现场」，
    不知道只是键名写错。值在当前草稿里真实存在，就按区块 id 认。
    """
    result = run_trial(
        definition=_metric_group_draft(),
        operations=[
            {
                "op": "SET_METRIC_GROUP",
                "target": {"ref": "region_总告警数"},
                "params": {
                    "arrangement": {
                        "mode": "GRID",
                        "columns": 2,
                        "direction": "HORIZONTAL",
                        "gap": {"value": 16, "unit": "PX"},
                    }
                },
            }
        ],
    )
    assert result.ok, result.errors
    arrangement = result.definition.model_dump()["regions"][0]["component"]["arrangement"]
    assert arrangement["gap"]["value"] == 16, arrangement


def test_unknown_ref_is_still_rejected():
    """**放水不得越界**：既不存在的区块、也不是本次现场的引用名 → 明确拒绝。"""
    result = run_trial(
        definition=_metric_group_draft(),
        operations=[
            {
                "op": "SET_METRIC_GROUP",
                "target": {"ref": "ref_根本不存在的引用"},
                "params": {
                    "arrangement": {"mode": "GRID", "columns": 2}
                },
            }
        ],
    )
    assert not result.ok
    assert result.errors[0]["reason"] == "unknown_ref", result.errors


def test_metrics_wrapped_in_a_shell_object_is_normalised():
    """``metrics`` 写成 ``{"items": [...]}`` 这种**壳** —— 拆壳就是那份清单。

    现场实证：模型连着 5 轮提交 ``{"items": [...]}``，每一轮都被
    「metrics 必须是列表」判死，最后放弃能力格式。
    """
    from app.services.edit_capabilities.base import tolerate_params
    from app.services.edit_capabilities.text_metric import SetMetricGroupParams

    items = [
        {"id": "m_1", "label": "总告警数", "fieldRef": "totalAlarmCount"},
        {"id": "m_2", "label": "严重告警数", "fieldRef": "seriousAlarmCount"},
    ]
    out = tolerate_params(SetMetricGroupParams, {"metrics": {"items": items}})
    assert isinstance(out["metrics"], list), out["metrics"]
    assert out["metrics"] == items, out["metrics"]

    result = run_trial(
        definition=_metric_group_draft(),
        operations=[
            {
                "op": "SET_METRIC_GROUP",
                "target": {"target": "region_总告警数"},
                "params": {"metrics": {"items": items}},
            }
        ],
    )
    assert result.ok, result.errors
    metrics = result.definition.model_dump()["regions"][0]["component"]["metrics"]
    assert [item["id"] for item in metrics] == ["m_1", "m_2"], metrics


def test_format_precision_accepts_numeric_strings():
    """``precision`` 写成 ``"1"`` / ``1.0`` —— 同一个值，换个写法。

    现场实证（08-03 那一步）：模型连续 4 轮提交 ``precision`` 都被
    「precision 必须是整数」判死，最后整轮放弃、用户只看到
    「本次编辑没有完成：修改计划在预执行校验时没有通过」。
    """
    draft = ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "A", "revision": 13},
            "regions": [
                {
                    "id": "region_2",
                    "name": "昨日总耗电",
                    "role": "SUMMARY",
                    "component": {
                        "type": "METRIC_CARD",
                        "metric": {
                            "id": "m_total",
                            "label": "运行设备数",
                            "fieldRef": "runningDeviceCount",
                            "format": {"kind": "NUMBER", "precision": 0, "unitLabel": "台"},
                        },
                    },
                    "data": {
                        "inputs": {"main": {"type": "SIMULATED"}},
                        "pipeline": {"nodes": [], "outputs": {"main": {"nodeRef": "node:n"}}},
                    },
                    "binding": {"outputRef": "output:main"},
                    "templateBinding": {"previewSpec": {"kind": "generic"}},
                }
            ],
        }
    )
    for precision in ("1", 1.0, 1):
        result = run_trial(
            definition=draft,
            operations=[
                {
                    "op": "SET_METRIC_ITEM",
                    "target": {"target": "region_2"},
                    "params": {
                        "fieldRef": "energyKwh",
                        "label": "耗电格式验证",
                        "format": {"kind": "NUMBER", "precision": precision, "unitLabel": "kWh"},
                    },
                }
            ],
        )
        assert result.ok, (precision, result.errors)
        fmt = result.definition.model_dump()["regions"][0]["component"]["metric"]["format"]
        assert fmt["precision"] == 1, (precision, fmt)
        assert fmt["unitLabel"] == "kWh", (precision, fmt)

    # 收不下就照旧明确拒绝。
    bad = run_trial(
        definition=draft,
        operations=[
            {
                "op": "SET_METRIC_ITEM",
                "target": {"target": "region_2"},
                "params": {"format": {"kind": "NUMBER", "precision": "一位"}},
            }
        ],
    )
    assert not bad.ok
    assert bad.errors[0]["reason"] == "invalid_format", bad.errors


# --------------------------------------------------------------------------
# 本轮新发现并实证的两个真实缺陷
# --------------------------------------------------------------------------
#
# **缺陷 A：引号里的指标名被规则层丢掉。**
# 现场实证（session ``sess_7145aca742ab`` / draft ``draft_97f84ed4dfb5``）：
# 用户说「把「昨日总耗电」指标卡改为显示「季度累计能耗」这个指标。」——
# 规则层把引号**里的内容**删掉了，抽出来的意图成了「显示这个」这种碎片，
# 于是指标名被写成「显示这个」、``fieldRef`` 写成空。
#
# **缺陷 B：``SET_METRIC_GROUP`` 只给排版就把整组指标项清成 0 项。**
# 现场实证（session ``sess_0017cdfb7bdf`` / draft ``draft_ca7be1809836``
# rev4→rev5，主链 ``draft_346545045849`` rev15→rev16）：模型只提交了
# ``arrangement``（横向两列、间距 16px），组里的三项指标被抹成 0 项，
# 而回复却说「组内指标均未改动」。


def test_quoted_metric_name_survives_the_rule_layer():
    """用户圈出来的名字（「季度累计能耗」）就是指标名 —— 不许被丢掉。

    丢掉的实锤：意图成了「显示这个」，指标名写进草稿，字段写空。
    """
    from app.services.out_of_catalog_candidate import (
        extract_metric_intent,
        split_metric_intent,
    )

    intent, answer = split_metric_intent(
        "显示「季度累计能耗」这个指标。", strip_add_prefix=False,
    )
    assert intent == "季度累计能耗", intent
    assert answer == "", answer

    assert extract_metric_intent(
        "显示「运行设备数」，单位用台。", strip_add_prefix=False,
    ) == "运行设备数"

    # 新增请求：引号里的名字照样是指标名。
    assert extract_metric_intent("增加一个「电脑耗电率」") == "电脑耗电率"
    # 引号里没抽出来 → 整句照旧抽，澄清回答还在。
    assert split_metric_intent("增加一个电脑耗电率 「耗电量」") == (
        "电脑耗电率", "耗电量",
    )
    # 没有引号的老口径一个都不许变。
    assert extract_metric_intent("增加一个电脑耗电率") == "电脑耗电率"
    assert extract_metric_intent("加个") == ""


def test_quoted_metric_name_reaches_the_edit_plan(draft):
    """整条链路：规则层抽出的指标名要原样进到修改计划里。

    判据不是「有没有抽出来」，而是「抽出来的是不是用户说的那个名字」。
    """
    from app.schemas.edit_plan import EditIntent
    from app.services.template_edit_engine import template_edit_engine
    from app.tools.context import ToolScope

    plan = template_edit_engine.parse(
        message="把「昨日总耗电」指标卡改为显示「季度累计能耗」这个指标。",
        definition=draft,
        selected_region_id=None,
        scope=ToolScope(
            project_id="project_001", subject_ref="machine_room", subject_id="room_a",
        ),
    )

    assert plan.intent == EditIntent.CHANGE_METRIC, plan.intent
    intent = str(plan.operation.metricIntent or "")
    assert intent == "季度累计能耗", intent
    assert "显示这个" not in intent, intent


def test_metric_group_without_metrics_keeps_the_existing_items():
    """只给排版（列数 / 间距）→ 组里的指标项**原样保留**。

    缺省不等于「清空」：把用户没提的指标项悄悄抹掉，还回一句
    「组内指标均未改动」，是最难查的一类错。
    """
    result = run_trial(
        definition=_metric_group_draft(),
        operations=[
            {
                "op": "SET_METRIC_GROUP",
                "target": {"target": "region_总告警数"},
                "params": {
                    "arrangement": {
                        "mode": "GRID",
                        "columns": 2,
                        "direction": "HORIZONTAL",
                        "gap": {"value": 16, "unit": "PX"},
                    }
                },
            }
        ],
    )
    assert result.ok, result.errors
    component = result.definition.model_dump()["regions"][0]["component"]
    labels = [item["label"] for item in component["metrics"]]
    assert labels == ["总告警数", "严重告警数"], component["metrics"]
    assert [item["id"] for item in component["metrics"]] == ["m_1", "m_2"]
    assert component["arrangement"]["gap"]["value"] == 16, component["arrangement"]
    # 回复要**如实**说清楚：原有指标项保留下来了。
    message = str(result.resolved_operations[0].get("message") or "")
    assert "原样保留" in message, message
    assert "2 个指标项" in message, message


def test_metric_group_with_an_explicit_full_list_still_replaces_the_items():
    """给了完整清单 = 清单说了算（协议语义：清单里没有的项移除）。

    这条守住「缺省」与「显式空清单」的区别：前者保留，后者才是清空。
    """
    result = run_trial(
        definition=_metric_group_draft(),
        operations=[
            {
                "op": "SET_METRIC_GROUP",
                "target": {"target": "region_总告警数"},
                "params": {
                    "metrics": [
                        {
                            "id": "m_2",
                            "label": "严重告警数",
                            "fieldRef": "seriousAlarmCount",
                            "format": {"kind": "NUMBER", "unitLabel": "条"},
                        }
                    ],
                    "arrangement": {
                        "mode": "GRID", "columns": 1, "direction": "VERTICAL",
                        "gap": {"value": 12, "unit": "PX"},
                    },
                },
            }
        ],
    )
    assert result.ok, result.errors
    component = result.definition.model_dump()["regions"][0]["component"]
    assert [item["id"] for item in component["metrics"]] == ["m_2"], component["metrics"]


# --------------------------------------------------------------------------
# 本轮又发现的一个服务端缺陷：工具声明的字段 ≠ 工具签名能收的字段
# --------------------------------------------------------------------------
#
# 现场实证（session ``sess_8ac0f01a11c6``，04:25:42）：模型照工具的
# JSON Schema 填了 ``baseCombination`` → ``run()`` 抛
# ``TypeError: run() got an unexpected keyword argument 'baseCombination'``
# → **整轮修改全废**（草稿一个字没改，用户只看到一句「没能把修改提交成
# 待确认候选」，模型还替服务端把责任揽了：「这属于服务端工具自身的缺陷」）。


def test_every_tool_schema_is_accepted_by_its_own_runner():
    """工具**声明**的字段，工具自己必须**收得下**。

    收不下的后果不是「这一项没生效」，而是整轮调用抛异常、整轮修改作废。
    """
    import inspect

    from app.tools.registry import _TOOLS

    broken: list[str] = []
    for module in _TOOLS:
        params = getattr(module, "PARAMETERS", None)
        runner = getattr(module, "run", None)
        name = getattr(module, "NAME", getattr(module, "__name__", "?"))
        if not isinstance(params, dict) or runner is None:
            continue
        properties = params.get("properties") or {}
        accepted = set(inspect.signature(runner).parameters)
        extra = sorted(set(properties) - accepted)
        if extra:
            broken.append(f"{name}: {extra}")
    assert not broken, broken


def test_submit_edit_plan_accepts_the_combination_it_advertises():
    """模型带上 ``baseCombination`` / ``editFacts`` 提交 → 不许抛异常。

    判据是**签名收得下**（现场实证：收不下就是 ``TypeError``，
    整轮修改全废）；「收到之后按服务端自己的口径算」是另一件事。
    """
    import inspect

    from app.tools.submit_edit_plan_tool import run

    accepted = set(inspect.signature(run).parameters)
    assert "baseCombination" in accepted, sorted(accepted)
    assert "editFacts" in accepted, sorted(accepted)


def test_the_tool_call_drops_keys_the_runner_does_not_take():
    """工具调用多带一个签名之外的键 → **丢掉它**，不是让整轮崩掉。"""
    import asyncio

    from app.tools.registry import _make_function_tool

    class _FakeTool:
        NAME = "probe_tool"
        DESCRIPTION = "探针"
        PARAMETERS = {"type": "object", "properties": {"a": {"type": "string"}}}
        CATEGORY = "MUTATION"
        IS_READ_ONLY = False

        @staticmethod
        def run(a: str = "") -> dict:
            return {"a": a}

    captured: dict = {}

    class _FakeFunctionTool:
        def __init__(self, fn, name, description, input_schema=None, **kwargs):
            captured["fn"] = fn
            captured["name"] = name

    tool = _make_function_tool(_FakeTool, _FakeFunctionTool, None)
    result = asyncio.run(captured["fn"](a="值", 多余的键="随便写"))
    assert result == {"a": "值"}, result


def test_empty_string_list_is_read_as_an_empty_list():
    """**空数组被模型写成空字符串** —— 收下它，别让整轮试做死在这里。

    现场实证（主链最后一轮）：模型提交 ``presentationFilters: ""`` →
    契约判「必须是列表」→ 整轮试做失败 → 用户只看到一句
    「修改计划在预执行校验时没有通过，原草稿保持不变」。
    """
    from app.services.edit_capabilities.base import tolerate_params
    from app.services.edit_capabilities.binding_component import SetDataBindingParams

    params = tolerate_params(
        SetDataBindingParams,
        {"presentationFilters": "", "sort": "", "fieldAliases": {}},
    )
    assert params["presentationFilters"] == [], params
    assert params["sort"] == [], params


def test_empty_string_on_an_optional_list_means_not_given():
    """「可以不���」的列表字段：空字符串 = **没给**，不是空清单。

    指标组的 ``metrics`` 缺省 = 保留组里已有的指标项；把 ``""`` 当成
    空清单就会把整组抹掉（那正是缺陷 B）。
    """
    from app.services.edit_capabilities.base import tolerate_params
    from app.services.edit_capabilities.text_metric import SetMetricGroupParams

    params = tolerate_params(
        SetMetricGroupParams, {"metrics": "", "arrangement": {}},
    )
    assert params["metrics"] is None, params


def _table_draft() -> ReportDefinitionDraft:
    """一份带「设备耗电明细」表格的草稿（列顺序刻意不是 id 顺序）。"""
    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "A 机房昨日能耗日报", "revision": 16},
            "regions": [
                {
                    "id": "region_3",
                    "name": "设备耗电明细",
                    "role": "DETAIL",
                    "component": {
                        "type": "TABLE",
                        "columns": [
                            {
                                "id": "col_1",
                                "label": "设备名称",
                                "fieldRef": "deviceName",
                                "format": {"kind": "TEXT"},
                            },
                            {
                                "id": "col_3",
                                "label": "设备总数",
                                "fieldRef": "deviceCount",
                                "format": {"kind": "NUMBER"},
                            },
                            {
                                "id": "col_2",
                                "label": "耗电量",
                                "fieldRef": "energyKwh",
                                "format": {"kind": "NUMBER"},
                            },
                        ],
                        "rowNumber": True,
                    },
                    "data": {
                        "inputs": {"main": {"type": "SIMULATED"}},
                        "pipeline": {"nodes": [], "outputs": {"main": {"nodeRef": "node:n"}}},
                    },
                    "binding": {"outputRef": "output:main"},
                    "templateBinding": {"previewSpec": {"kind": "generic"}},
                }
            ],
        }
    )


def test_table_columns_are_stored_in_the_submitted_order():
    """表格列清单**按提交的顺序**落库 —— 协议写明「顺序即显示顺序」。

    现场实证（主链第 17–18 轮）：第 17 轮把「设备总数」插到「设备名称」后面
    （列清单变成 ``col_1 / col_3 / col_2``，位置正确）；第 18 轮只改两列的标题
    与字段绑定，模型把完整清单**按 id 重排**成 ``col_1 / col_2 / col_3``，
    「设备总数」就被挪到了最后一位。

    接口层把提交的顺序原样落库（不按 id 排序），所以这里钉住这一点：
    顺序由**提交的清单**决定，能力层不做任何"顺手整理"。
    """
    result = run_trial(
        definition=_table_draft(),
        operations=[
            {
                "op": "SET_TABLE_COLUMNS",
                "target": {"regionId": "region_3"},
                "params": {
                    "columns": [
                        {
                            "id": "col_1",
                            "label": "时间",
                            "fieldRef": "logicalDate",
                            "format": {"kind": "DATE"},
                        },
                        {
                            "id": "col_3",
                            "label": "设备总数",
                            "fieldRef": "deviceCount",
                        },
                        {
                            "id": "col_2",
                            "label": "昨日耗电量",
                            "fieldRef": "energyKwh",
                        },
                    ]
                },
            }
        ],
    )
    assert result.ok, result.errors
    component = _region_of(result.definition, "region_3")["component"]
    assert [c["id"] for c in component["columns"]] == ["col_1", "col_3", "col_2"]
    assert [c["label"] for c in component["columns"]] == ["时间", "设备总数", "昨日耗电量"]
    # 没给的项保持原样：设备总���这一列沿用原来的格式。
    total = component["columns"][1]
    assert total["format"] == {"kind": "NUMBER"}


def test_table_column_order_contract_tells_the_model_not_to_reorder():
    """能力契约里必须写明「别按 id 重排」——否则模型会自己把列挪走。

    模型是按 id 顺序思考列的；不写清楚，它每次提交完整清单都会重排一次，
    用户刚放好的列位置就被冲掉（主链第 17 → 18 轮的现场）。
    """
    from app.services.edit_capabilities.base import get_capability
    from app.services.edit_capabilities import table_component  # noqa: F401  (触发注册)

    capability = get_capability("SET_TABLE_COLUMNS")
    assert capability is not None
    assert "顺序即显示顺序" in capability.description
    assert "不要按 id 重排" in capability.description


def _renamed_metric_card_draft() -> dict:
    """一份「卡片改过名字」的指标卡草稿。

    故意保留 ``previewSpec.metrics[0].label`` 的**旧目录标签**（``总耗电``），
    与 ``component.metric.label``（``耗电格式验证``）不一致 ——
    这正是主链第 7 版草稿（``draft_8b59b0d29b06`` rev7）的真实形状。
    """
    return {
        "metadata": {"name": "A 机房昨日能耗日报", "revision": 7},
        "regions": [
            {
                "id": "region_2",
                "name": "耗电格式验证",
                "role": "SUMMARY",
                "component": {
                    "type": "METRIC_CARD",
                    "metric": {
                        "id": "m_total",
                        "label": "耗电格式验证",
                        "fieldRef": "energyKwh",
                        "format": {
                            "kind": "NUMBER",
                            "precision": 1,
                            "unitLabel": "kWh",
                            "thousandSeparator": False,
                        },
                    },
                },
                "data": {
                    "inputs": {"main": {"type": "SIMULATED"}},
                    "pipeline": {
                        "nodes": [
                            {
                                "id": "node:main",
                                "type": "METRIC",
                                "metricRef": "metric.machine_room.total_energy",
                            }
                        ],
                        "outputs": {"main": {"nodeRef": "node:main"}},
                    },
                },
                "binding": {"outputRef": "output:main"},
                "templateBinding": {
                    "previewSpec": {
                        "role": "SUMMARY",
                        "kind": "energy",
                        "unit": "kWh",
                        "sourceMode": "simulated",
                        "capabilityPending": False,
                        "clarification": None,
                        "columns": [],
                        "metrics": [
                            {
                                "metricKey": "metric.machine_room.total_energy",
                                "label": "总耗电",
                                "kind": "energy",
                                "unit": "kWh",
                                "capabilityPending": False,
                                "pendingReason": None,
                                "bindingKind": "catalog",
                            }
                        ],
                    }
                },
            }
        ],
    }


def test_renamed_metric_card_shows_the_new_name_in_the_preview():
    """卡片改过名字之后，**预览载荷里那一项的标签要跟着定义走**。

    现场实证（主链 ``draft_8b59b0d29b06`` rev7）：定义里
    ``component.metric.label`` 已经是「耗电格式验证」，外层标题也同步了，
    可 ``templateBinding.previewSpec.metrics[0].label`` 还是**目录快照**里的
    「总耗电」；前端渲染的是 ``metrics[]``（优先于载荷顶层的 ``label``），
    于是右侧指标卡上**仍显示旧名字**。

    这一条钉住：清单里那一条**就是**主指标时，名字与单位按**定义**说了算。
    """
    from app.services.preview_service import (
        build_preview_context,
        build_report_preview_payload,
    )

    definition = _renamed_metric_card_draft()
    context = build_preview_context(definition)
    payload = build_report_preview_payload(definition, context)

    region = next(
        item for item in payload["regions"] if item.get("regionId") == "region_2"
    )
    card = region["payload"]
    metrics = card.get("metrics") or []
    assert metrics, card
    assert metrics[0]["label"] == "耗电格式验证", metrics
    # 载荷顶层与清单里那一项要**说同一个名字**。
    assert card.get("label") == metrics[0]["label"], card
    assert metrics[0]["unit"] == "kWh", metrics
