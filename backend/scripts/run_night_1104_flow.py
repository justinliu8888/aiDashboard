"""2026-11-04 夜间专项：能力格式编辑的 T01–T05 正式测试。

真实模型调用**总计上限 3 次**（T01 两句 + T02 一次补充）；其余用例走
隔离后端探针，不烧模型。脚本自己数，超了就直接报错停下。

用法：

    AI_SESSION_DB_PATH=/tmp/haewu-night-1104.db \
      /opt/anaconda3/bin/python3 backend/scripts/run_night_1104_flow.py \
      --base http://127.0.0.1:8005 --only t01,t02,t03,t04,t05 \
      --out /tmp/haewu-night-1104-flow.json

隔离：
- 独立数据库（``AI_SESSION_DB_PATH``）；
- 独立 sessionId / conversationId（``sess_night_1104_*``）。

**不覆盖用户正在跑的草稿**：只碰自己那几套会话。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

#: 真实模型调用上限（硬上限：超过就停，不偷偷多烧）。
MODEL_CALL_BUDGET = 3

STATE = "USER_EDITING_TEMPLATE"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _post(url: str, payload: dict[str, Any], timeout: int = 180) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body[:400]}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def _get(url: str, timeout: int = 60) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body[:400]}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------
# 会话上下文
# --------------------------------------------------------------------------

def _session_context(session_id: str) -> tuple[dict, Any, Any, dict]:
    """从**会话记录**里取 scope / 定义 / 策略 / 当前步骤。

    只读：不写用户数据，也不改会话。
    """
    from app.services.session_service import session_service

    record = session_service.repository.get_session(session_id)
    snapshot = (getattr(record, "snapshot", None) or {}) if record else {}
    scope = snapshot.get("scope") if isinstance(snapshot.get("scope"), dict) else {}
    definition = snapshot.get("definition")
    policy = snapshot.get("policy")
    choice = getattr(record, "pendingChoice", None) or {}
    question = choice.get("question") if isinstance(choice, dict) else None
    step: dict[str, Any] = {}
    if isinstance(question, dict):
        step = {
            "stepId": question.get("stepId"),
            "stepVersion": question.get("stepVersion"),
        }
    return scope or {}, definition, policy, step


def _chat(
    base: str,
    session_id: str,
    conversation_id: str,
    message: str,
    *,
    state: str = STATE,
    model_call: bool = False,
) -> dict[str, Any]:
    """按**前端真实做法**发这一轮请求。"""
    scope, definition, policy, step = _session_context(session_id)
    context: dict[str, Any] = {
        "projectId": scope.get("projectId") or "proj_demo",
        "subjectRef": scope.get("subjectRef") or "room_a",
        "subjectId": scope.get("subjectId") or "room_a",
    }
    if step.get("stepId"):
        context["stepId"] = step["stepId"]
        context["stepVersion"] = step.get("stepVersion")
    snapshots: dict[str, Any] = {}
    if definition is not None:
        snapshots["reportDefinition"] = definition
    if policy is not None:
        snapshots["reportPolicy"] = policy
    payload = {
        "conversationId": conversation_id,
        "sessionId": session_id,
        "state": state,
        "message": message,
        "context": context,
    }
    if snapshots:
        payload["snapshots"] = snapshots
    status, body = _post(f"{base}/api/agent/chat", payload)
    body["_httpStatus"] = status
    body["_message"] = message
    body["_modelCall"] = model_call
    return body


def _regions(definition: dict | None) -> list[dict]:
    if not isinstance(definition, dict):
        return []
    return [r for r in definition.get("regions") or [] if isinstance(r, dict)]


def _region_ids(definition: dict | None) -> list[str]:
    return [str(r.get("id") or "") for r in _regions(definition)]


def _chart_kinds(definition: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for region in _regions(definition):
        component = region.get("component") or {}
        if str(component.get("type") or "") == "CHART":
            out[str(region.get("name") or region.get("id"))] = str(
                component.get("chartKind") or ""
            )
    return out


def _summarize(body: dict[str, Any]) -> dict[str, Any]:
    """把一轮响应压成**可存档**的形状（脱敏：不留模型原文与内部 id 全量）。"""
    domain = body.get("domainObjects") or {}
    plan = domain.get("pendingEditPlan") or {}
    definition = domain.get("reportDefinitionDraft") or {}
    return {
        "httpStatus": body.get("_httpStatus"),
        "message": body.get("_message"),
        "reply": (body.get("reply") or "")[:600],
        "state": body.get("state"),
        "hasPendingPlan": bool(plan),
        "planId": plan.get("planId"),
        "planSummary": (plan.get("summary") or "")[:400],
        "planOperationCount": len(plan.get("operations") or []),
        "questionIds": [q.get("id") for q in body.get("questions") or []],
        "questionOptions": [
            [o.get("value") for o in q.get("options") or []]
            for q in body.get("questions") or []
        ],
        "errorCode": (body.get("error") or {}).get("code"),
        "regionIds": _region_ids(definition),
        "chartKinds": _chart_kinds(definition),
        "revision": (definition.get("metadata") or {}).get("revision"),
    }


# --------------------------------------------------------------------------
# T01：连续真实 Agent 请求
# --------------------------------------------------------------------------

def _t01_checks(
    reply1: str,
    second_plan_id: str | None,
    accepted_region_ids: list[str],
    accepted_chart_kinds: dict[str, str],
) -> dict[str, bool]:
    """T01 的判据（**唯一一份**，跑批与重判共用，避免两处口径不一致）。

    草稿里本来就有一张能耗趋势折线图 —— 本次只看**两张告警图**，
    其余图表必须**原样保留**（不被换成饼图）。
    """
    alarm_charts = {
        name: kind for name, kind in accepted_chart_kinds.items() if "告警" in name
    }
    other_charts = {
        name: kind for name, kind in accepted_chart_kinds.items() if "告警" not in name
    }
    return {
        "有文字确认": bool(reply1.strip()),
        "没有追问 metricRef": "metricRef" not in reply1 and "指标引用" not in reply1,
        "第二句仍是同一件事": bool(second_plan_id),
        "原列表被替换": "region_alarm_list" not in accepted_region_ids,
        "两张告警图": len(alarm_charts) == 2,
        "两张都是饼图": set(alarm_charts.values()) == {"PIE"},
        "其它图表没被改": all(kind == "LINE" for kind in other_charts.values()),
        "其他内容保留": any(
            rid in accepted_region_ids for rid in ("region_total_energy", "region_hourly_trend")
        ),
    }


def run_t01(base: str, budget: dict[str, int]) -> dict[str, Any]:
    """真实模型两句话 → 文字确认 → 接受 → 两张饼图。"""
    session_id = "sess_night_1104_t01"
    conversation_id = "conv_night_1104_t01"
    result: dict[str, Any] = {"name": "T01", "steps": [], "judgement": "", "passed": False}

    first = _chat(
        base, session_id, conversation_id,
        "将告警列表改成告警等级分布和告警区域分布",
        model_call=True,
    )
    budget["used"] += 1
    result["steps"].append({"step": "第一句", **_summarize(first)})
    if not first.get("domainObjects", {}).get("pendingEditPlan"):
        result["judgement"] = (
            "第一句没有形成待确认候选（"
            + str((first.get("error") or {}).get("code") or first.get("reply", "")[:120])
            + "）"
        )
        return result
    plan1 = first["domainObjects"]["pendingEditPlan"]
    reply1 = first.get("reply") or ""
    result["reply1"] = reply1[:800]
    result["plan1Summary"] = plan1.get("summary")

    # 第二句：同一事项的补充（饼图）。
    second = _chat(
        base, session_id, conversation_id, "两张都使用饼图", model_call=True
    )
    budget["used"] += 1
    result["steps"].append({"step": "第二句", **_summarize(second)})
    plan2 = (second.get("domainObjects") or {}).get("pendingEditPlan") or {}
    reply2 = second.get("reply") or ""
    result["reply2"] = reply2[:800]
    result["plan2Summary"] = plan2.get("summary")
    result["planChanged"] = plan2.get("planId") != plan1.get("planId")

    # 接受。
    accepted = _chat(base, session_id, conversation_id, "接受修改")
    result["steps"].append({"step": "接受修改", **_summarize(accepted)})
    definition = (accepted.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    result["acceptedChartKinds"] = _chart_kinds(definition)
    result["acceptedRegionIds"] = _region_ids(definition)

    # 判据。
    checks = _t01_checks(
        reply1,
        plan2.get("planId"),
        result["acceptedRegionIds"],
        result["acceptedChartKinds"],
    )
    result["checks"] = checks
    result["passed"] = all(checks.values())
    result["judgement"] = "通过" if result["passed"] else "未通过：" + str(
        [k for k, v in checks.items() if not v]
    )
    return result


# --------------------------------------------------------------------------
# T02：动态非模板输入（同一事项的补充）
# --------------------------------------------------------------------------

def _alarm_pie_ops() -> list[dict[str, Any]]:
    """「两张告警饼图 + 删掉旧列表」的能力格式操作清单。

    T02 / T04 / T05 共用同一份：**清单是这一件事的全部事实**，
    换个用例只改断言，不改操作。
    """
    return [
        {
            "op": "ADD_SECTION",
            "target": {},
            "params": {
                "title": "告警等级分布",
                "chartKind": "PIE",
                "dimension": "告警等级",
                "ref": "level_chart",
                "replaceRegionId": "region_alarm_list",
            },
        },
        {
            "op": "ADD_SECTION",
            "target": {},
            "params": {
                "title": "告警区域分布",
                "chartKind": "PIE",
                "dimension": "告警区域",
                "ref": "region_chart",
                "replaceRegionId": "region_alarm_list",
            },
        },
        {"op": "DELETE_SECTION", "target": {"regionId": "region_alarm_list"}, "params": {}},
    ]


def _set_pending_edit_menu(session_id: str, step_id: str) -> None:
    """把会话的选择锁换成「待确认修改」那张卡（与服务端同款结构）。

    服务端在 ``_finalize_template_edit`` 里就是这么挂的；探针要复现
    **同一份现场**，否则「刷新后恢复」测的就不是真实形状。
    """
    from app.services.session_service import session_service

    question = {
        "id": "pending_edit_plan_confirm",
        "prompt": "确认这次修改：",
        "expects": "SINGLE_CHOICE",
        "selectionOnly": True,
        "lockScope": "input_and_menu",
        "allowsOther": True,
        "otherLabel": "其他",
        "otherPlaceholder": "也可以直接在这里写要怎么改",
        "options": [
            {"value": "接受修改", "label": "接受修改"},
            {"value": "撤回上一步", "label": "撤回上一步"},
            {"value": "继续修改", "label": "继续修改"},
        ],
        "stepId": step_id,
        "stepVersion": 1,
        "stepKind": "menu",
    }
    session_service.repository.set_pending_choice(
        session_id, {"state": STATE, "question": question}
    )


def _build_checked_candidate(session_id: str, operations: list[dict]) -> dict[str, Any]:
    """用**隔离后端**建一份已检查候选（不烧模型）。

    走的是真实工具链：``submit_edit_plan`` → 隔离试做 → 已检查候选。
    """
    from app.services.edit_item_service import load_authoritative_definition
    from app.services.session_context import set_session_key
    from app.tools import registry
    from app.tools.context import ToolContext, bind_tool_context, clear_tool_context
    from app.tools.submission import bind_submission, take_submission
    from app.schemas.agent import AgentState

    set_session_key(session_id)
    definition, _source = load_authoritative_definition(session_id)
    bind_tool_context(
        ToolContext(
            state=AgentState.USER_EDITING_TEMPLATE,
            subject_ref="room_a",
            subject_id="room_a",
            project_id="proj_demo",
            definition=definition,
        )
    )
    bind_submission()
    try:
        result = registry.get_runner("submit_edit_plan")(
            operations=operations, originalRequest="将告警列表改成告警等级分布和告警区域分布"
        )
        submission = take_submission()
    finally:
        clear_tool_context()
    return {"toolResult": result, "submission": submission}


def _save_candidate(
    session_id: str,
    submission: dict[str, Any],
    scope: dict[str, str] | None = None,
) -> dict[str, Any]:
    """把已检查候选落库（等价于候选组装 + 服务层保存）。"""
    from app.schemas.edit_plan import EditPlan
    from app.schemas.session import PendingEditPlan
    from app.services.agent_service import agent_service
    from app.services.session_service import session_service

    scope = scope or {
        "projectId": "proj_demo",
        "subjectRef": "room_a",
        "subjectId": "room_a",
    }
    payload = submission["payload"]
    plan = EditPlan.model_validate(payload)
    facts = payload.get("editFacts") or {}
    revision = int(facts.get("baseRevision") or 0)
    candidate = PendingEditPlan(
        planId=payload.get("planId") or f"plan_{facts.get('operationCount', 0)}",
        draftRevision=revision,
        targetRegionId=str(payload.get("target", {}).get("regionId") or ""),
        operations=[step for step in payload.get("operations") or []],
        summary=str(payload.get("summary") or ""),
        editPlan=plan.model_dump(mode="json"),
        editFacts=facts,
        checked=True,
        # 作用域：与服务端 ``_finalize_template_edit`` 落库时写的一致，
        # 否则接受时会因为「修改范围与生成计划时不一致」被拒。
        projectId=scope["projectId"],
        subjectRef=scope["subjectRef"],
        subjectId=scope["subjectId"],
    )
    session_service.save_pending_plan(session_id, candidate)
    return candidate.model_dump(mode="json")


def run_t02(base: str, budget: dict[str, int]) -> dict[str, Any]:
    """隔离后端建已检查候选 → 一次真实模型补充 → 只改第二张的图表类型。"""
    session_id = "sess_night_1104_t02"
    conversation_id = "conv_night_1104_t02"
    result: dict[str, Any] = {"name": "T02", "steps": [], "judgement": "", "passed": False}

    ops = _alarm_pie_ops()
    built = _build_checked_candidate(session_id, ops)
    result["probe"] = {
        "accepted": built["toolResult"].get("accepted"),
        "checked": built["toolResult"].get("checked"),
        "summary": built["toolResult"].get("summary"),
    }
    if not built["submission"]:
        result["judgement"] = "隔离探针没能形成已检查候选：" + str(built["toolResult"])
        return result
    old_candidate = _save_candidate(session_id, built["submission"])
    result["oldPlanId"] = old_candidate.get("planId")

    # 一次真实模型请求：同一事项的补充。
    followup = _chat(
        base, session_id, conversation_id, "保留前一张圆饼，只把区域那张换成柱形",
        model_call=True,
    )
    budget["used"] += 1
    result["steps"].append({"step": "补充", **_summarize(followup)})
    result["reply"] = (followup.get("reply") or "")[:800]
    new_plan = (followup.get("domainObjects") or {}).get("pendingEditPlan") or {}
    result["newPlanId"] = new_plan.get("planId")
    result["newSummary"] = new_plan.get("summary")

    # 旧候选不可再接受。
    from app.services.session_service import session_service

    stored = session_service.load_pending_plan(session_id)
    result["storedPlanId"] = stored.planId if stored else None
    result["oldCandidateInvalid"] = bool(
        stored is None or stored.planId != old_candidate.get("planId")
    )

    # 接受新候选 → 看两张图的图表类型。
    accepted = _chat(base, session_id, conversation_id, "接受修改")
    result["steps"].append({"step": "接受修改", **_summarize(accepted)})
    definition = (accepted.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    kinds = _chart_kinds(definition)
    result["acceptedChartKinds"] = kinds
    level = next((v for k, v in kinds.items() if "等级" in k), "")
    area = next((v for k, v in kinds.items() if "区域" in k), "")
    checks = {
        "形成了新候选": bool(new_plan.get("planId")),
        "旧候选失效": result["oldCandidateInvalid"],
        "第一张仍是饼图": level == "PIE",
        "第二张改成柱状图": area == "BAR",
    }
    result["checks"] = checks
    result["passed"] = all(checks.values())
    result["judgement"] = "通过" if result["passed"] else "未通过：" + str(
        [k for k, v in checks.items() if not v]
    )
    return result


# --------------------------------------------------------------------------
# T03：失败隔离与回传（不烧模型）
# --------------------------------------------------------------------------

def run_t03(base: str, budget: dict[str, int]) -> dict[str, Any]:
    """缺图表类型 / 错误目标 / 无效引用 → 具体错误、无候选、现场不变。"""
    from app.services.edit_item_service import (
        load_authoritative_definition,
        new_item,
        save_edit_item,
    )
    from app.services.session_context import set_session_key
    from app.services.session_service import session_service
    from app.tools import registry
    from app.tools.submission import bind_submission, peek_submission

    session_id = "sess_night_1104_t05"
    result: dict[str, Any] = {"name": "T03", "probes": [], "judgement": "", "passed": False}
    set_session_key(session_id)

    # 先放一份**修改事项**（模拟「这件事已经在进行中」），再拿错误的操作
    # 清单去试做：失败之后这份事实必须**原样保留**，下一轮补充 / 纠正接着用。
    seed_item = new_item(
        original_request="将告警列表改成告警等级分布和告警区域分布",
        base_revision=0,
    )
    seed_item["operations"] = [
        {
            "op": "ADD_SECTION",
            "target": {},
            "params": {
                "title": "告警等级分布",
                "chartKind": "PIE",
                "dimension": "告警等级",
            },
        }
    ]
    seed_item["status"] = "drafting"
    save_edit_item(seed_item, session_id)

    before_definition, _ = load_authoritative_definition(session_id)
    before_pending = session_service.load_pending_plan(session_id)
    before_snapshot = session_service.load_snapshot(session_id)

    probes = [
        (
            "缺图表类型",
            [
                {
                    "op": "ADD_SECTION",
                    "target": {},
                    "params": {"title": "告警等级分布", "dimension": "告警等级"},
                }
            ],
            "missing_chart_kind",
        ),
        (
            "错误目标",
            [
                {
                    "op": "DELETE_SECTION",
                    "target": {"regionId": "region_不存在的区块"},
                    "params": {},
                }
            ],
            "unknown_region",
        ),
        (
            "无效引用",
            [
                {
                    "op": "SET_DISPLAY",
                    "target": {"ref": "never_created"},
                    "params": {"componentType": "CHART", "chartKind": "PIE"},
                }
            ],
            "unknown_ref",
        ),
    ]
    for name, ops, expect_reason in probes:
        bind_submission()
        try:
            outcome = registry.get_runner("submit_edit_plan")(operations=ops)
            slot_taken = peek_submission() is not None
        finally:
            from app.tools.submission import take_submission

            take_submission()
        reasons = [e.get("reason") for e in outcome.get("errors") or []]
        result["probes"].append(
            {
                "name": name,
                "accepted": outcome.get("accepted"),
                "reasons": reasons,
                "expectedReason": expect_reason,
                "errorText": (outcome.get("error") or "")[:300],
                "submissionSlotTaken": slot_taken,
                "ok": (
                    outcome.get("accepted") is False
                    and expect_reason in reasons
                    and not slot_taken
                ),
            }
        )

    after_definition, _ = load_authoritative_definition(session_id)
    after_pending = session_service.load_pending_plan(session_id)
    after_snapshot = session_service.load_snapshot(session_id)
    unchanged = (
        (before_definition or {}).model_dump(mode="json")
        == (after_definition or {}).model_dump(mode="json")
    )
    result["draftUnchanged"] = unchanged
    result["pendingPlanUnchanged"] = (
        (before_pending.planId if before_pending else None)
        == (after_pending.planId if after_pending else None)
    )
    result["historyUnchanged"] = (
        (before_snapshot.get("draftHistory") or [])
        == (after_snapshot.get("draftHistory") or [])
    )
    before_item = before_snapshot.get("editItem") or {}
    after_item = after_snapshot.get("editItem") or {}
    result["itemKept"] = bool(after_item)
    # 现场保留 = 事项**还在**，而且事实没被清空：原要求还在、操作清单还在
    # （失败的那次清单也记下来，下一轮接着改），状态是 drafting。
    result["itemIntact"] = bool(after_item) and (
        after_item.get("originalRequest") == before_item.get("originalRequest")
        and bool(after_item.get("originalRequest"))
        and bool(after_item.get("operations"))
        and str(after_item.get("status") or "") == "drafting"
    )
    result["itemErrors"] = [
        str(e.get("reason") or "") for e in (after_item.get("lastErrors") or [])
    ]

    checks = {
        "每个探针都报错": all(p["ok"] for p in result["probes"]),
        "草稿没变": unchanged,
        "没有产生候选": result["pendingPlanUnchanged"],
        "现场保留": result["itemIntact"],
    }
    result["checks"] = checks
    result["passed"] = all(checks.values())
    result["judgement"] = "通过" if result["passed"] else "未通过：" + str(
        [k for k, v in checks.items() if not v]
    )
    return result


# --------------------------------------------------------------------------
# T04：权限、状态与恢复（不烧模型）
# --------------------------------------------------------------------------

def run_t04(base: str, budget: dict[str, int]) -> dict[str, Any]:
    """刷新恢复 / 跨会话拒绝 / 取消候选 / 接受后撤销 / 其它 dropdown 仍锁着。

    **不烧模型**：待确认候选由**真实工具链**（隔离试做 → 已检查候选）产出，
    接受时执行的就是服务端存下来的那份清单，不重新规划。
    """
    from app.services.edit_item_service import load_authoritative_definition
    from app.services.session_service import session_service

    session_id = "sess_night_1104_t04"
    conversation_id = "conv_night_1104_t04"
    result: dict[str, Any] = {"name": "T04", "checks": {}, "judgement": "", "passed": False}

    # ---------- 1) 造一份「待候选」现场 ----------
    built = _build_checked_candidate(session_id, _alarm_pie_ops())
    if not built["submission"]:
        result["judgement"] = "没能形成已检查候选：" + str(built["toolResult"])
        return result
    candidate = _save_candidate(session_id, built["submission"])
    step_id = "step_night_1104_t04_pending"
    _set_pending_edit_menu(session_id, step_id)
    result["candidatePlanId"] = candidate.get("planId")

    # ---------- 2) 刷新后现场完整恢复（重新读库 == 刷新重开） ----------
    pending = session_service.load_pending_plan(session_id)
    snapshot = session_service.load_snapshot(session_id)
    item = snapshot.get("editItem")
    choice = session_service.load_pending_choice(session_id)
    definition_before, _ = load_authoritative_definition(session_id)
    before_dump = (definition_before or None)
    before_dump = before_dump.model_dump(mode="json") if before_dump else {}
    revision_before = int((before_dump.get("metadata") or {}).get("revision") or 0)
    regions_before = [str(r.get("id")) for r in (before_dump.get("regions") or [])]
    question = (choice or {}).get("question") if isinstance(choice, dict) else None
    result["refresh"] = {
        "候选还在": bool(pending) and pending.planId == candidate.get("planId"),
        "修改事项还在": bool(item),
        "修改事项带着原要求": bool(item and item.get("originalRequest")),
        "修改事项带着操作清单": len((item or {}).get("operations") or []) == 3,
        "菜单锁还在": bool(question) and str(question.get("lockScope")) == "input_and_menu",
        "菜单还是待确认那张卡": bool(question)
        and question.get("id") == "pending_edit_plan_confirm",
        "定义没变": revision_before == 0 and "region_alarm_list" in regions_before,
    }

    # ---------- 3) 跨会话接受被拒 ----------
    bare = "sess_night_1104_t04_other"
    session_service.repository.upsert_session(
        session_id=bare, state=STATE, snapshot={}
    )
    session_service.save_pending_plan(bare, pending)
    rejected = _chat(base, bare, "conv_night_1104_t04_other", "接受修改")
    bare_snapshot = session_service.load_snapshot(bare)
    result["crossSession"] = {
        "reply": (rejected.get("reply") or "")[:200],
        "errorCode": (rejected.get("error") or {}).get("code"),
        "对方会话仍然没有草稿": not bare_snapshot.get("definition"),
    }
    result["checks"]["跨会话接受被拒"] = (
        not bare_snapshot.get("definition")
        and (rejected.get("error") or {}).get("code") == "STALE_EDIT_PLAN"
    )
    still = session_service.load_pending_plan(session_id)
    result["checks"]["原会话候选未被动"] = (
        still is not None and still.planId == candidate.get("planId")
    )

    # ---------- 4) 取消候选：草稿不变、候选清掉 ----------
    cancelled = _chat(base, session_id, conversation_id, "撤回上一步")
    after_cancel, _ = load_authoritative_definition(session_id)
    after_cancel_dump = after_cancel.model_dump(mode="json") if after_cancel else {}
    result["cancelReply"] = (cancelled.get("reply") or "")[:200]
    result["checks"]["取消后草稿没变"] = (
        int((after_cancel_dump.get("metadata") or {}).get("revision") or 0) == revision_before
    )
    result["checks"]["取消后候选已清"] = (
        session_service.load_pending_plan(session_id) is None
    )

    # ---------- 5) 再来一份候选并接受 → 整批生效 ----------
    built2 = _build_checked_candidate(session_id, _alarm_pie_ops())
    if not built2["submission"]:
        result["checks"]["接受后两张告警饼图"] = False
        result["checks"]["接受后旧列表消失"] = False
        result["checks"]["接受后候选已消费"] = False
    else:
        _save_candidate(session_id, built2["submission"])
        _set_pending_edit_menu(session_id, step_id)
        accepted = _chat(base, session_id, conversation_id, "接受修改")
        accepted_dump, _ = load_authoritative_definition(session_id)
        accepted_dump = accepted_dump.model_dump(mode="json") if accepted_dump else {}
        kinds = _chart_kinds(accepted_dump)
        regions_accepted = [str(r.get("id")) for r in (accepted_dump.get("regions") or [])]
        result["accepted"] = {
            "chartKinds": kinds,
            "regionIds": regions_accepted,
            "revision": int((accepted_dump.get("metadata") or {}).get("revision") or 0),
            "reply": (accepted.get("reply") or "")[:300],
        }
        result["checks"]["接受后两张告警饼图"] = (
            sum(1 for name, kind in kinds.items() if "告警" in name and kind == "PIE") == 2
        )
        result["checks"]["接受后旧列表消失"] = "region_alarm_list" not in regions_accepted
        result["checks"]["接受后其它内容保留"] = (
            "region_total_energy" in regions_accepted
            and "region_hourly_trend" in regions_accepted
        )
        result["checks"]["接受后候选已消费"] = (
            session_service.load_pending_plan(session_id) is None
        )

        # ---------- 6) 撤销：回到上一版（旧列表回来） ----------
        draft_id = str(snapshot.get("draftId") or "")
        result["undo"] = {"draftId": draft_id}
        if draft_id:
            status, ws = _get(f"{base}/api/drafts/{draft_id}")
            result["undo"]["workspaceStatus"] = status
            body = ws.get("draft") if isinstance(ws, dict) else None
            body = body if isinstance(body, dict) else (ws if isinstance(ws, dict) else {})
            revision = body.get("revision")
            result["undo"]["revisionBeforeUndo"] = revision
            result["undo"]["canUndo"] = body.get("canUndo")
            undo_status, undo_body = _post(
                f"{base}/api/drafts/{draft_id}/actions",
                {"action": "UNDO", "expectedRevision": revision},
            )
            result["undo"]["status"] = undo_status
            result["undo"]["detail"] = str(undo_body.get("detail") or "")[:200]
            undone, _ = load_authoritative_definition(session_id)
            undone_dump = undone.model_dump(mode="json") if undone else {}
            undone_regions = [str(r.get("id")) for r in (undone_dump.get("regions") or [])]
            result["undo"]["regionIdsAfterUndo"] = undone_regions
            result["checks"]["撤销后回到上一版"] = (
                undo_status == 200 and "region_alarm_list" in undone_regions
            )
        else:
            result["checks"]["撤销后回到上一版"] = False

    # ---------- 7) 其它 dropdown 不放开普通聊天 ----------
    result["checks"]["其它会话未被解锁"] = (
        session_service.load_pending_choice("sess_night_1104_t01") is not None
        and session_service.load_pending_choice("sess_night_1104_t05") is not None
    )
    # 菜单锁着的时候，普通聊天不能把草稿改掉（后端拒绝，什么都不动）。
    before_lock = load_authoritative_definition("sess_night_1104_t05")[0]
    before_lock_dump = before_lock.model_dump(mode="json") if before_lock else {}
    lock_reply = _chat(base, "sess_night_1104_t05", "conv_night_1104_t05", "随便聊聊别的")
    after_lock, _ = load_authoritative_definition("sess_night_1104_t05")
    after_lock_dump = after_lock.model_dump(mode="json") if after_lock else {}
    result["lockReply"] = (lock_reply.get("reply") or "")[:200]
    result["checks"]["锁着时普通聊天不改草稿"] = (
        before_lock_dump.get("metadata", {}).get("revision")
        == after_lock_dump.get("metadata", {}).get("revision")
        and [r.get("id") for r in (before_lock_dump.get("regions") or [])]
        == [r.get("id") for r in (after_lock_dump.get("regions") or [])]
    )

    result["passed"] = all(result["checks"].values())
    result["judgement"] = "通过" if result["passed"] else "未通过：" + str(
        [k for k, v in result["checks"].items() if not v]
    )
    return result


# --------------------------------------------------------------------------
# T05：绘图与载荷（不烧模型）
# --------------------------------------------------------------------------

def run_t05(base: str, budget: dict[str, int]) -> dict[str, Any]:
    """两张饼图的**载荷**：标签、数值、模拟标记，两条渲染路径都要能画。"""
    from app.services.edit_item_service import load_authoritative_definition
    from app.services.preview_service import (
        build_preview_context,
        build_report_preview_payload,
    )
    from app.services.session_context import set_session_key
    from app.services.session_service import session_service

    session_id = "sess_night_1104_t05"
    result: dict[str, Any] = {"name": "T05", "judgement": "", "passed": False}
    set_session_key(session_id)

    # 用隔离后端能力做一份「两张饼图」的草稿，产载荷。
    from app.services.edit_trial_service import run_trial

    definition, _ = load_authoritative_definition(session_id)
    ops = _alarm_pie_ops()
    context = build_preview_context(definition.model_dump(mode="json"))
    trial = run_trial(
        definition=definition, operations=ops, preview_context=context
    )
    result["trialOk"] = trial.ok
    if not trial.ok:
        result["errors"] = trial.errors
        result["judgement"] = "试做没通过"
        return result
    payload = build_report_preview_payload(
        trial.definition.model_dump(mode="json"), context
    )
    charts = {
        region["name"]: region["payload"]
        for region in payload.get("regions") or []
        if region.get("componentType") == "CHART"
    }
    result["charts"] = {
        name: {
            "chartKind": body.get("chartKind"),
            "xLabels": body.get("xLabels"),
            "yValues": body.get("yValues"),
            "unit": body.get("unit"),
            "simulated": body.get("simulated"),
        }
        for name, body in charts.items()
    }
    level = charts.get("告警等级分布") or {}
    area = charts.get("告警区域分布") or {}
    checks = {
        # 草稿里本来就有一张能耗趋势折线图 —— 本次只看**两张告警图**。
        "两张告警图都是饼图": bool(level)
        and bool(area)
        and level.get("chartKind") == "PIE"
        and area.get("chartKind") == "PIE",
        "标签数值一一对应": all(
            len(body.get("xLabels") or []) == len(body.get("yValues") or [])
            for body in (level, area)
        ),
        "等级与区域分类不同": set(level.get("xLabels") or [])
        != set(area.get("xLabels") or []),
        "标注模拟": level.get("simulated") is True and area.get("simulated") is True,
        "不是设备能耗": not any(
            "耗电" in str(label)
            for body in (level, area)
            for label in (body.get("xLabels") or [])
        ),
        "旧告警列表已不在载荷里": not any(
            str(region.get("name") or "") == "告警列表"
            for region in payload.get("regions") or []
        ),
    }
    result["checks"] = checks
    result["passed"] = all(checks.values())
    result["judgement"] = "通过" if result["passed"] else "未通过：" + str(
        [k for k, v in checks.items() if not v]
    )
    return result


# --------------------------------------------------------------------------
# 重判：按修正后的判据重算**已记录**的结果（不新增模型调用）
# --------------------------------------------------------------------------

def rejudge_report(path: str) -> dict[str, Any]:
    """读一份已跑完的报告，按**当前**判据重算 T01 的结论。

    **不重跑模型**：T01 的真实模型调用已经用掉（预算上限 3 次），
    只能用当时记录下来的实测数据重算结论。原始结论一并留着，便于对照。
    """
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    rejudged: list[str] = []
    for result in report.get("results") or []:
        if str(result.get("name") or "").upper() != "T01":
            continue
        result.setdefault("originalJudgement", result.get("judgement"))
        second_plan_id = ""
        for step in result.get("steps") or []:
            if step.get("step") == "第二句":
                second_plan_id = str(step.get("planId") or "")
        checks = _t01_checks(
            str(result.get("reply1") or ""),
            second_plan_id,
            [str(x) for x in (result.get("acceptedRegionIds") or [])],
            {str(k): str(v) for k, v in (result.get("acceptedChartKinds") or {}).items()},
        )
        result["checks"] = checks
        result["passed"] = all(checks.values())
        result["judgement"] = (
            "通过"
            if result["passed"]
            else "未通过：" + str([k for k, v in checks.items() if not v])
        )
        result["rejudged"] = True
        result["rejudgedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        result["rejudgedNote"] = "按修正后的判据重算（基于已记录的实测数据，未新增模型调用）"
        rejudged.append("T01")
    report["rejudged"] = True
    report["rejudgedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    report["modelCallsAddedByRejudge"] = 0
    report["rejudgedResults"] = rejudged
    return report


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.getenv("NIGHT_BASE", "http://127.0.0.1:8005"))
    parser.add_argument("--only", default="t01,t02,t03,t04,t05")
    parser.add_argument("--out", default="/tmp/haewu-night-1104-flow.json")
    parser.add_argument(
        "--rejudge",
        default="",
        help="重判模式：读这份已跑完的报告，按当前判据重算 T01 结论（不烧模型）",
    )
    args = parser.parse_args()

    if args.rejudge:
        report = rejudge_report(args.rejudge)
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for result in report.get("results") or []:
            print(f"[{result.get('name')}] {result.get('judgement')}", flush=True)
        print(f"[report] {args.out} modelCalls=0/3 (rejudge)")
        return 0 if all(r.get("passed") for r in report.get("results") or []) else 1

    only = [item.strip().lower() for item in args.only.split(",") if item.strip()]
    budget = {"used": 0, "limit": MODEL_CALL_BUDGET}
    report: dict[str, Any] = {
        "base": args.base,
        "modelCallBudget": MODEL_CALL_BUDGET,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "results": [],
    }

    runners = {
        "t01": run_t01,
        "t02": run_t02,
        "t03": run_t03,
        "t04": run_t04,
        "t05": run_t05,
    }
    for key in only:
        runner = runners.get(key)
        if runner is None:
            continue
        if budget["used"] >= MODEL_CALL_BUDGET and key in {"t01", "t02"}:
            report["results"].append(
                {"name": key.upper(), "passed": False, "judgement": "模型预算用尽，未执行"}
            )
            continue
        started = time.time()
        try:
            outcome = runner(args.base, budget)
        except Exception as exc:  # noqa: BLE001
            outcome = {
                "name": key.upper(),
                "passed": False,
                "judgement": f"执行异常：{type(exc).__name__}: {exc}",
            }
        outcome["elapsedSec"] = round(time.time() - started, 1)
        report["results"].append(outcome)
        print(f"[{key.upper()}] {outcome.get('judgement')}", flush=True)

    report["modelCallsUsed"] = budget["used"]
    report["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[report] {args.out} modelCalls={budget['used']}/{MODEL_CALL_BUDGET}")
    return 0 if all(r.get("passed") for r in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
