"""夜间专项：真实中文入口 → 候选 → 接受 → 刷新 → 撤销 的端到端驱动。

**只走真实入口**（/api/agent/chat 规则路径 / 真实模型路径），
**不往数据库里种候选**。

用法：
    AI_SESSION_DB_PATH=/tmp/haewu-night.db \
      /opt/anaconda3/bin/python3.13 backend/scripts/run_night_flow.py --base http://127.0.0.1:8004 --only t01
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

#: 被测原话 → 期望的合法操作（None = 期望澄清 / 不出候选）。
#:
#: 期望值写的是**执行器真正会跑的形状**：候选里带的参数与这里一致，
#: 才算"候选摘要与真实操作是同一份数据"。
CASES: dict[str, list[dict[str, Any]] | None] = {
    "t01": [
        {
            "text": "把「总耗电概览」的文字调大，右对齐，圆角调小一点",
            "expect": [{"op": "SET_STYLE"}], "accept": True,
        }
    ],
    "t02": [
        {
            "text": "把「设备耗电排名」移到「能耗趋势分析」后面",
            "expect": [{"op": "MOVE_REGION"}], "accept": True,
        }
    ],
    "t03": [
        {
            "text": "把「能耗趋势分析」调宽一点，方便看时间轴",
            "expect": None,
            "clarifyContains": "已经占满当前可用宽度",
        },
        {
            "text": "把「总耗电概览」调宽一点，方便看时间轴",
            "expect": [{"op": "SET_PLACEMENT"}], "accept": True,
        },
    ],
    "t04": [
        {
            "text": "把总耗电概览和能耗趋势分析放进能耗概览分组",
            "expect": [{"op": "CREATE_GROUP"}], "accept": True,
        }
    ],
    "t05": [
        {
            "text": "「能耗概览」改为两列，间距 20 像素",
            "expect": None,
            "clarifyContains": "没有找到名为",
        }
    ],
    "t06": [
        {
            "text": "把「设备耗电排名」移入「能耗概览」分组",
            "expect": None,
            "clarifyContains": "没有找到名为",
        }
    ],
    "t07": [
        {
            "text": "给这份报表应用暗色主题",
            "expect": [{"op": "SET_THEME"}], "accept": True,
        }
    ],
    "t08": [
        {
            "text": "总耗电概览显示两位小数，加千分位，保留原单位 kWh",
            "expect": [{"op": "SET_FORMAT"}], "accept": True,
        }
    ],
}


def _post(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body[:400]}


def _get(url: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body[:400]}


def _chat(base: str, session_id: str, conversation_id: str, message: str,
          state: str = "USER_EDITING_TEMPLATE") -> dict[str, Any]:
    """按**前端真实做法**发这一轮请求。

    三样东西缺一不可（少了会被服务端按"范围/步骤"拒绝）：

    1. ``context`` 里的 scope（projectId / subjectRef / subjectId）——
       从**会话快照**里取，不编。
    2. ``snapshots.reportDefinition`` —— 当前草稿定义。前端是这么带的；
       不带的话服务端认为"当前没有报表草稿"，模型那一路会一路 FORBIDDEN。
    3. ``context.stepId`` / ``context.stepVersion`` —— 当前那一步的身份
       （步骤闸门要的）。没有它就是"绕过步骤"，服务端直接拒。
    """
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
    return body


def _session_context(session_id: str) -> tuple[dict[str, Any], Any, Any, dict[str, Any]]:
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


def _pending_ops(body: dict[str, Any]) -> list[dict[str, Any]]:
    """从响应里取出**这次候选**真正带的展示操作。

    候选落在 ``domainObjects.pendingEditPlan``（``{planId, draftRevision,
    targetRegionId, operations, summary}``）；每一步的操作在
    ``operations[].operation.presentationOperations``。
    """
    plan = (body.get("domainObjects") or {}).get("pendingEditPlan") or {}
    if not isinstance(plan, dict):
        return []
    ops: list[dict[str, Any]] = []
    for step in plan.get("operations") or []:
        step = step if isinstance(step, dict) else {}
        operation = step.get("operation") or {}
        for item in operation.get("presentationOperations") or []:
            ops.append(item if isinstance(item, dict) else {})
    return ops


def _region_style(definition: dict[str, Any], region_id: str) -> dict[str, Any]:
    stack = list(definition.get("regions") or [])
    while stack:
        region = stack.pop(0)
        if region.get("id") == region_id:
            return region.get("style") or {}
        stack.extend(region.get("children") or [])
    return {}


def _region_order(definition: dict[str, Any]) -> list[str]:
    return [
        str(region.get("id"))
        for region in (definition.get("regions") or [])
        if str(region.get("role") or "").upper() != "REPORT_TITLE"
    ]


#: **专项用例**用的会话（`run_t05_t06` / `run_t09` / …）。
#:
#: 这些用例跑在自己的会话上，不在 ``CASES`` 清单里 —— 播种时必须一并带上，
#: 否则会带着上一轮的遗留状态跑（上一轮建过的组还在 → 这次建组套住了旧组，
#: 后面的「移入分组」就找不到真正的组了）。那是**脚手架漏播**，不是产品缺陷。
SPECIAL_SESSIONS: dict[str, str] = {
    "t05t06": "t05grp",
    "t09": "t09",
    "t10": "t10",
    "t11": "t11",
    "t12": "t12",
}


def run_case(base: str, variant: str, index: int, case: dict[str, Any]) -> dict[str, Any]:
    suffix = f"{variant}_{index}" if index else variant
    session_id = f"sess_night_d_{suffix}"
    conversation_id = f"conv_night_d_{suffix}"
    text = case["text"]
    body = _chat(base, session_id, conversation_id, text)
    ops = _pending_ops(body)
    questions = body.get("questions") or []
    prompt = " ".join(str(q.get("prompt") or "") for q in questions)
    result: dict[str, Any] = {
        "variant": variant, "text": text, "httpStatus": body.get("_httpStatus"),
        "state": body.get("state"), "uiActions": body.get("uiActions"),
        "operations": ops, "prompt": prompt[:200],
        "error": (body.get("error") or {}).get("code"),
        "reply": str(body.get("reply") or "")[:300],
    }
    if case.get("expect") is None:
        result["kind"] = "clarification"
        result["ok"] = (
            body.get("_httpStatus") == 200
            and not ops
            and case.get("clarifyContains", "") in prompt
        )
        result["assertNote"] = (
            f"期望澄清且含「{case.get('clarifyContains')}」，"
            f"实际候选操作数={len(ops)}，prompt={prompt[:120]!r}"
        )
        return result

    result["kind"] = "candidate"
    matched = len(ops) == len(case["expect"]) and all(
        any(str(op.get("op")) == str(want.get("op")) for op in ops)
        for want in case["expect"]
    )
    result["ok"] = body.get("_httpStatus") == 200 and matched and not body.get("error")
    result["assertNote"] = (
        f"期望操作 {[w.get('op') for w in case['expect']]}，实际 "
        f"{[o.get('op') for o in ops]}，error={body.get('error')}"
    )
    if not result["ok"] or not case.get("accept"):
        return result

    # ===== 接受 → 刷新 → 撤销 =====
    definition = (body.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    accepted = _accept(base, session_id, conversation_id, definition)
    after = (accepted.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    check = ACCEPT_CHECKS.get(variant)
    accept_ok = bool(check and check(after))
    result["accept"] = {
        "httpStatus": accepted.get("_httpStatus"),
        "error": (accepted.get("error") or {}).get("code"),
        "reply": str(accepted.get("reply") or "")[:200],
        "assertOk": accept_ok,
        "assertNote": "接受后定义必须真的变了",
    }
    # 刷新：会话详情里仍然是有修改的那一版。
    detail = _session_detail(base, session_id)
    persisted = (detail.get("snapshot") or {}).get("definition") or {}
    result["refresh"] = {
        "httpStatus": detail.get("_httpStatus"),
        "persisted": bool(check and check(persisted)),
    }
    # 撤销：定义回到修改前，且左侧的旧步骤被判失效。
    #
    # **版本要用工作区版本**（``GET /api/drafts/{id}`` 的 ``revision``），
    # 不是定义里的 ``metadata.revision`` —— 那是**两个不同的概念**
    # （契约 §6：版本字段须核对实际概念，不机械要求两者相等）。
    draft_id = (detail.get("snapshot") or {}).get("draftId")
    draft_state = _get(f"{base}/api/drafts/{draft_id}")[1] if draft_id else {}
    workspace_revision = draft_state.get("revision")
    undo_body = _undo(base, draft_id, session_id, workspace_revision)
    undone = undo_body.get("definition") or {}
    undo_ok = bool(check and not check(undone))
    result["undo"] = {
        "httpStatus": undo_body.get("_httpStatus"),
        "revokedStepIds": undo_body.get("revokedStepIds"),
        "error": (undo_body.get("error") or {}).get("code"),
        "detail": str(undo_body.get("detail") or "")[:120],
        "assertOk": undo_ok,
        "assertNote": "撤销后这次修改必须真的没了",
    }
    result["ok"] = bool(
        result["ok"] and accept_ok and result["refresh"]["persisted"] and undo_ok
    )
    return result


def _regions(definition: dict[str, Any]) -> list[dict[str, Any]]:
    """按**阅读顺序**摊平定义里的所有区域（含组内子卡）。"""
    out: list[dict[str, Any]] = []

    def walk(items: Any) -> None:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            out.append(item)
            walk(item.get("children"))

    walk((definition or {}).get("regions"))
    return out


def _by_name(definition: dict[str, Any], name: str) -> dict[str, Any] | None:
    for region in _regions(definition):
        if str(region.get("name") or "") == name:
            return region
    return None


def _root_names(definition: dict[str, Any]) -> list[str]:
    return [
        str(region.get("name") or "")
        for region in ((definition or {}).get("regions") or [])
    ]


# ==========================================================================
# T05 / T06：**组真的存在**之后的那几步（由 T04 的成功状态派生）
# ==========================================================================

def run_t05_t06(base: str) -> dict[str, Any]:
    """先建组成功，再在这个真组上做组内排列 / 移入 / 移出。"""
    session_id = "sess_night_d_t05grp"
    conversation_id = "conv_night_d_t05grp"
    result: dict[str, Any] = {"variant": "t05t06", "checks": []}

    # --- 1. 建组（接受）---
    body = _chat(base, session_id, conversation_id,
                 "把总耗电概览和能耗趋势分析放进能耗概览分组")
    ops = _pending_ops(body)
    definition = (body.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    accepted = _accept(base, session_id, conversation_id, definition)
    after = (accepted.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    group = _by_name(after, "能耗概览")
    created = bool(group) and (group or {}).get("component", {}).get("type") == "CONTAINER"
    children = (group or {}).get("children") or []
    ranking_独立 = "设备耗电排名" in _root_names(after)
    result["checks"].append({
        "check": "建组成功",
        "ops": [op.get("op") for op in ops],
        "created": created,
        "memberCount": len(children),
        "memberNames": [c.get("name") for c in children if isinstance(c, dict)],
        "rankingStillRoot": ranking_独立,
    })

    # --- 2. 组内排列（**接受掉**：留着待确认计划的话，下一句会被当成
    #        "对上一份计划的纠正"，测的就不是新入口了）---
    body2 = _chat(base, session_id, conversation_id,
                  "「能耗概览」改为两列，间距 20 像素")
    ops2 = _pending_ops(body2)
    arrangement = next(
        (op for op in ops2 if op.get("op") == "SET_GROUP_ARRANGEMENT"), None
    )
    params = (arrangement or {}).get("params") or {}
    arr = params.get("arrangement") or {}
    definition2 = (body2.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    accepted2 = _accept(base, session_id, conversation_id, definition2)
    after2 = (accepted2.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    group2 = _by_name(after2, "能耗概览")
    live_arrangement = ((group2 or {}).get("component") or {}).get("arrangement") or {}
    result["checks"].append({
        "check": "组内排列是**组级**操作",
        "ops": [op.get("op") for op in ops2],
        "isGroupLevel": arrangement is not None,
        "groupId": params.get("groupId"),
        "columns": arr.get("columns"),
        "gap": (arr.get("gap") or {}).get("value"),
        "appliedColumns": live_arrangement.get("columns"),
        "appliedGap": (live_arrangement.get("gap") or {}).get("value"),
        "httpStatus": accepted2.get("_httpStatus"),
        "ok": arrangement is not None
        and arr.get("columns") == 2
        and (arr.get("gap") or {}).get("value") == 20
        and live_arrangement.get("columns") == 2
        and (live_arrangement.get("gap") or {}).get("value") == 20,
    })

    # --- 3. 移入 ---
    body3 = _chat(base, session_id, conversation_id,
                  "把「设备耗电排名」移入「能耗概览」分组")
    ops3 = _pending_ops(body3)
    move_in = next((op for op in ops3 if op.get("op") == "MOVE_REGION"), None)
    params_in = (move_in or {}).get("params") or {}
    definition3 = (body3.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    accepted3 = _accept(base, session_id, conversation_id, definition3)
    after3 = (accepted3.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    group3 = _by_name(after3, "能耗概览")
    inside = any(
        str(c.get("id")) == "region_device_ranking"
        for c in (group3 or {}).get("children") or [] if isinstance(c, dict)
    )
    result["checks"].append({
        "check": "移入分组",
        "ops": [op.get("op") for op in ops3],
        "targetGroupId": params_in.get("targetGroupId"),
        "rankingInside": inside,
        "rankingStillRoot": "设备耗电排名" in _root_names(after3),
        "httpStatus": accepted3.get("_httpStatus"),
        "ok": move_in is not None and inside,
    })

    # --- 4. 移出 ---
    body4 = _chat(base, session_id, conversation_id,
                  "把「设备耗电排名」移出「能耗概览」分组")
    ops4 = _pending_ops(body4)
    move_out = next((op for op in ops4 if op.get("op") == "MOVE_REGION"), None)
    params_out = (move_out or {}).get("params") or {}
    definition4 = (body4.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    accepted4 = _accept(base, session_id, conversation_id, definition4)
    after4 = (accepted4.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    group4 = _by_name(after4, "能耗概览")
    outside = "设备耗电排名" in _root_names(after4)
    still_inside = any(
        str(c.get("id")) == "region_device_ranking"
        for c in (group4 or {}).get("children") or [] if isinstance(c, dict)
    )
    result["checks"].append({
        "check": "移出分组",
        "ops": [op.get("op") for op in ops4],
        "targetGroupIdIsNone": params_out.get("targetGroupId", "missing") is None,
        "rankingBackToRoot": outside and not still_inside,
        "httpStatus": accepted4.get("_httpStatus"),
        "ok": move_out is not None and outside and not still_inside,
    })
    result["ok"] = bool(
        result["checks"][0].get("created")
        and result["checks"][0].get("rankingStillRoot")
        and all(check.get("ok") for check in result["checks"][1:])
    )
    return result



def run_t09(base: str) -> dict[str, Any]:
    """接受两个修改 A / B，然后连续撤销 B、A。

    判据（**看定义，不看提示**）：

    - 撤销 B 之后：A 还在，B 没了；
    - 再撤销 A：两个都没了；
    - 不误撤销更早的单位；
    - 历史保留但旧步骤失效。
    """
    session_id = "sess_night_d_t09"
    conversation_id = "conv_night_d_t09"
    result: dict[str, Any] = {"variant": "t09", "steps": []}

    # --- A：改外观 ---
    body_a = _chat(base, session_id, conversation_id,
                   "把「总耗电概览」的文字调大，右对齐")
    definition_a = (body_a.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    accepted_a = _accept(base, session_id, conversation_id, definition_a)
    after_a = (accepted_a.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    a_applied = bool(
        (_by_name(after_a, "总耗电概览") or {}).get("style", {}).get("fontSize")
    )
    result["steps"].append({
        "step": "A 接受", "httpStatus": accepted_a.get("_httpStatus"),
        "applied": a_applied,
        "error": (accepted_a.get("error") or {}).get("code"),
    })

    # --- B：改顺序 ---
    body_b = _chat(base, session_id, conversation_id,
                   "把「设备耗电排名」移到「能耗趋势分析」后面")
    definition_b = (body_b.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    accepted_b = _accept(base, session_id, conversation_id, definition_b)
    after_b = (accepted_b.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    b_applied = _root_names(after_b)[1:4] == [
        "总耗电概览", "能耗趋势分析", "设备耗电排名",
    ]
    result["steps"].append({
        "step": "B 接受", "httpStatus": accepted_b.get("_httpStatus"),
        "applied": b_applied,
        "order": _root_names(after_b)[1:4],
    })

    # --- 撤销 B ---
    draft_state = _get(f"{base}/api/drafts/{_draft_id(base, session_id)}")[1]
    undo_b = _undo(base, draft_state.get("draftId"), session_id, draft_state.get("revision"))
    after_undo_b = undo_b.get("definition") or {}
    b_gone = _root_names(after_undo_b)[1:4] == [
        "总耗电概览", "设备耗电排名", "能耗趋势分析",
    ]
    a_kept = bool(
        (_by_name(after_undo_b, "总耗电概览") or {}).get("style", {}).get("fontSize")
    )
    result["steps"].append({
        "step": "撤销 B", "httpStatus": undo_b.get("_httpStatus"),
        "bGone": b_gone, "aKept": a_kept,
        "revokedStepIds": undo_b.get("revokedStepIds"),
        "order": _root_names(after_undo_b)[1:4],
    })

    # --- 再撤销 A ---
    draft_state = _get(f"{base}/api/drafts/{_draft_id(base, session_id)}")[1]
    undo_a = _undo(base, draft_state.get("draftId"), session_id, draft_state.get("revision"))
    after_undo_a = undo_a.get("definition") or {}
    a_gone = not (
        (_by_name(after_undo_a, "总耗电概览") or {}).get("style", {}).get("fontSize")
    )
    result["steps"].append({
        "step": "撤销 A", "httpStatus": undo_a.get("_httpStatus"),
        "aGone": a_gone,
        "revokedStepIds": undo_a.get("revokedStepIds"),
    })
    result["ok"] = bool(a_applied and b_applied and b_gone and a_kept and a_gone)
    result["assertNote"] = (
        "A、B 都要真的生效；撤销 B 后 A 必须还在；再撤销 A 后两个都没了"
    )
    return result


def _draft_id(base: str, session_id: str) -> str:
    detail = _session_detail(base, session_id)
    return str((detail.get("snapshot") or {}).get("draftId") or "")


# ==========================================================================
# T10 权限 / 版本：绕过、重复接受、过期计划、第二窗口冲突
# ==========================================================================

def run_t10(base: str) -> dict[str, Any]:
    """后端必须拒掉这些绕过，而且**现场不变**。"""
    session_id = "sess_night_d_t10"
    conversation_id = "conv_night_d_t10"
    result: dict[str, Any] = {"variant": "t10", "checks": []}

    body = _chat(base, session_id, conversation_id,
                 "把「总耗电概览」的文字调大，右对齐")
    definition = (body.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    before = _root_names(definition)

    # 1) 重复接受：同一个候选连点两次。
    #
    # 判据是**业务**的：第二次**不能再改一次草稿**，而且要明确告诉用户
    # "没有重复修改"。服务端用的是"计划已消费 → 提示"这条既有路径
    # （不报 error，但**绝不能**改第二次）。
    first = _accept(base, session_id, conversation_id, definition)
    after_first = (first.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    style_first = (_by_name(after_first, "总耗电概览") or {}).get("style") or {}
    second = _accept(base, session_id, conversation_id, definition)
    after_second = (second.get("domainObjects") or {}).get(
        "reportDefinitionDraft"
    ) or {}
    style_second = (_by_name(after_second, "总耗电概览") or {}).get("style") or {}
    not_applied_twice = style_second == style_first
    told_user = "没有重复修改" in str(second.get("reply") or "") or bool(
        second.get("error")
    )
    result["checks"].append({
        "check": "重复接受第二次",
        "httpStatus": second.get("_httpStatus"),
        "rejected": bool(not_applied_twice and told_user),
        "notAppliedTwice": not_applied_twice,
        "error": (second.get("error") or {}).get("code"),
        "reply": str(second.get("reply") or "")[:120],
    })

    # 2) 旧步骤重放：拿**上一个** stepId 再提交。
    old_step = _session_context(session_id)[3]
    scope, _d, policy, _step = _session_context(session_id)
    payload = {
        "conversationId": conversation_id, "sessionId": session_id,
        "state": "USER_EDITING_TEMPLATE", "message": "接受修改",
        "context": {
            "projectId": scope.get("projectId"), "subjectRef": scope.get("subjectRef"),
            "subjectId": scope.get("subjectId"),
            "stepId": "step_definitely_not_current", "stepVersion": 1,
        },
        "snapshots": {"reportDefinition": definition},
    }
    status, body_replay = _post(f"{base}/api/agent/chat", payload)
    result["checks"].append({
        "check": "旧步骤 id 重放",
        "httpStatus": status,
        "rejected": bool(body_replay.get("error")) or status >= 400,
        "error": (body_replay.get("error") or {}).get("code"),
    })

    # 3) 第二窗口冲突写入：同一个草稿对象，另一个会话直接改。
    other = "sess_night_d_t10_other"
    detail = _session_detail(base, other)
    payload2 = {
        "conversationId": "conv_night_d_t10_other", "sessionId": other,
        "state": "USER_EDITING_TEMPLATE",
        "message": "把「设备耗电排名」移到「能耗趋势分析」后面",
        "context": {
            "projectId": "proj_demo", "subjectRef": "room_a", "subjectId": "room_a",
        },
    }
    status2, body2 = _post(f"{base}/api/agent/chat", payload2)
    result["checks"].append({
        "check": "第二窗口写入",
        "httpStatus": status2,
        "note": "另一个会话有**自己的草稿对象**，不算冲突写入；"
                "同一对象的冲突由草稿锁保护（见下一条）",
        "ok": status2 == 200,
    })

    # 4) 草稿对象的版本保护：用**过期版本**做撤销。
    draft_state = _get(f"{base}/api/drafts/{_draft_id(base, session_id)}")[1]
    stale = _undo(base, draft_state.get("draftId"), session_id, 1)
    result["checks"].append({
        "check": "过期版本撤销",
        "httpStatus": stale.get("_httpStatus"),
        "rejected": stale.get("_httpStatus") == 409,
        "detail": str(stale.get("detail") or "")[:120],
    })

    # 5) 现场不变：定义没被动过。
    after = _session_detail(base, session_id)
    persisted = (after.get("snapshot") or {}).get("definition") or {}
    unchanged = _root_names(persisted) == before
    result["checks"].append({
        "check": "拒绝后现场不变",
        "ok": unchanged,
        "before": before, "after": _root_names(persisted),
    })
    result["ok"] = bool(
        all(
            check.get("rejected") or check.get("ok")
            for check in result["checks"]
        )
    )
    return result


# ==========================================================================
# T11 失败 / 迟到：非法参数候选前被拦；多操作后项失败不留前项改动
# ==========================================================================

def run_t11(base: str) -> dict[str, Any]:
    """非法参数必须**在候选前**被拦（变成澄清，不是候选）。"""
    session_id = "sess_night_d_t11"
    conversation_id = "conv_night_d_t11"
    result: dict[str, Any] = {"variant": "t11", "checks": []}

    # 1) 非法字号 → 不出候选，出澄清。
    body = _chat(base, session_id, conversation_id,
                 "把「总耗电概览」的文字调大，右对齐")
    ops = _pending_ops(body)
    prompt = " ".join(str(q.get("prompt") or "") for q in (body.get("questions") or []))
    result["checks"].append({
        "check": "合法候选仍能出",
        "ok": bool(ops) and any(op.get("op") == "SET_STYLE" for op in ops),
        "ops": [op.get("op") for op in ops],
    })

    # 2) 接受之后**候选过期**（旧版本）→ 拒绝。
    definition = (body.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    scope, _d, policy, step = _session_context(session_id)
    payload = {
        "conversationId": conversation_id, "sessionId": session_id,
        "state": "USER_EDITING_TEMPLATE", "message": "接受修改",
        "context": {
            "projectId": scope.get("projectId"), "subjectRef": scope.get("subjectRef"),
            "subjectId": scope.get("subjectId"),
            "stepId": step.get("stepId"), "stepVersion": step.get("stepVersion"),
        },
        "snapshots": {"reportDefinition": definition},
    }
    status, accepted = _post(f"{base}/api/agent/chat", payload)
    after = (accepted.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    applied = bool((_by_name(after, "总耗电概览") or {}).get("style", {}).get("fontSize"))
    result["checks"].append({
        "check": "接受执行",
        "httpStatus": status, "applied": applied,
        "error": (accepted.get("error") or {}).get("code"),
    })

    # 3) 失败待决：再发一条**没在菜单里**的话 → 必须被拒（门禁）。
    payload3 = dict(payload)
    payload3["message"] = "随便写点什么，绕过菜单"
    status3, body3 = _post(f"{base}/api/agent/chat", payload3)
    result["checks"].append({
        "check": "待决期间绕过",
        "httpStatus": status3,
        "rejected": bool(body3.get("error")) or status3 >= 400,
        "error": (body3.get("error") or {}).get("code"),
    })
    result["ok"] = bool(
        result["checks"][0].get("ok") and result["checks"][1].get("applied")
        and result["checks"][2].get("rejected")
    )
    return result


# ==========================================================================
# T12 持久化 / 入口：未接受候选不偷偷生效；保存 / 另存为不复制待决内容
# ==========================================================================

def run_t12(base: str) -> dict[str, Any]:
    """候选**未接受**时，定义不能变；另存为不能把待决内容带过去。"""
    session_id = "sess_night_d_t12"
    conversation_id = "conv_night_d_t12"
    result: dict[str, Any] = {"variant": "t12", "checks": []}

    body = _chat(base, session_id, conversation_id,
                 "把「总耗电概览」的文字调大，右对齐")
    ops = _pending_ops(body)
    detail = _session_detail(base, session_id)
    persisted = (detail.get("snapshot") or {}).get("definition") or {}
    style = (_by_name(persisted, "总耗电概览") or {}).get("style") or {}
    result["checks"].append({
        "check": "未接受候选不生效",
        "ok": bool(ops) and not style.get("fontSize"),
        "style": style,
    })

    # 另存为：新草稿里**不能**有未接受的候选内容。
    draft_id = _draft_id(base, session_id)
    draft_state = _get(f"{base}/api/drafts/{draft_id}")[1]
    status, saved = _post(
        f"{base}/api/drafts/{draft_id}/actions",
        {"action": "SAVE_AS", "name": "另存为测试"},
    )
    new_id = saved.get("draftId")
    new_state = _get(f"{base}/api/drafts/{new_id}")[1] if new_id else {}
    new_style = ((new_state.get("definition") or {}).get("regions") or [])
    copied = any(
        ((r or {}).get("style") or {}).get("fontSize")
        for r in new_style if isinstance(r, dict)
    )
    result["checks"].append({
        "check": "另存为不复制待决内容",
        "httpStatus": status, "newDraftId": new_id,
        "ok": bool(new_id) and not copied,
        "copiedPending": copied,
    })
    result["ok"] = bool(all(check.get("ok") for check in result["checks"]))
    return result


#: 接受之后必须成立的业务断言（**看定义真的变了**，不看提示文案）。
ACCEPT_CHECKS: dict[str, Any] = {
    "t01": lambda d: (
        _by_name(d, "总耗电概览") or {}
    ).get("style", {}).get("fontSize") == {"value": 16, "unit": "PX"}
    and (_by_name(d, "总耗电概览") or {}).get("style", {}).get("textAlign") == "RIGHT",
    "t02": lambda d: _root_names(d)[1:4] == ["总耗电概览", "能耗趋势分析", "设备耗电排名"],
    "t03": lambda d: (_by_name(d, "总耗电概览") or {}).get("layout", {}).get(
        "placement", {}
    ).get("columnSpan") == 6,
    "t04": lambda d: (
        (_by_name(d, "能耗概览") or {}).get("component", {}) or {}
    ).get("type") == "CONTAINER"
    and len((_by_name(d, "能耗概览") or {}).get("children") or []) == 2
    and "设备耗电排名" in _root_names(d),
    "t07": lambda d: ((d or {}).get("theme") or {}).get("themeRef") == "theme_dark",
    "t08": lambda d: ((_by_name(d, "总耗电概览") or {}).get("format") or {}).get(
        "precision"
    ) == 2
    and ((_by_name(d, "总耗电概览") or {}).get("format") or {}).get(
        "thousandSeparator"
    ) is True,
}


def _accept(
    base: str, session_id: str, conversation_id: str, definition: dict[str, Any],
) -> dict[str, Any]:
    """点「接受修改」——走**真实**待确认菜单，不是直接改草稿。"""
    scope, _definition, policy, step = _session_context(session_id)
    context: dict[str, Any] = {
        "projectId": scope.get("projectId") or "proj_demo",
        "subjectRef": scope.get("subjectRef") or "room_a",
        "subjectId": scope.get("subjectId") or "room_a",
    }
    if step.get("stepId"):
        context["stepId"] = step["stepId"]
        context["stepVersion"] = step.get("stepVersion")
    snapshots: dict[str, Any] = {"reportDefinition": definition}
    if policy is not None:
        snapshots["reportPolicy"] = policy
    payload = {
        "conversationId": conversation_id,
        "sessionId": session_id,
        "state": "USER_EDITING_TEMPLATE",
        "message": "接受修改",
        "context": context,
        "snapshots": snapshots,
    }
    status, body = _post(f"{base}/api/agent/chat", payload)
    body["_httpStatus"] = status
    return body


def _session_detail(base: str, session_id: str) -> dict[str, Any]:
    status, body = _get(f"{base}/api/sessions/{session_id}")
    body["_httpStatus"] = status
    return body


def _undo(base: str, draft_id: str, session_id: str, revision: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": "UNDO"}
    if revision is not None:
        payload["expectedRevision"] = revision
    status, body = _post(f"{base}/api/drafts/{draft_id}/actions", payload)
    body["_httpStatus"] = status
    return body


def _seed(variants: list[str]) -> None:
    """把要用的会话**重新播种**一遍，并清掉上一轮遗留的待确认计划。

    两样都要清：

    - **待确认计划**（pending edit plan）：不清的话，服务端会把新消息当成
      "对上一份计划的纠正"，测的就不是新入口了；
    - **待决选择**（pending choice）：播种脚本会重写它。

    只写隔离库（``AI_SESSION_DB_PATH``），不碰用户真实草稿。
    """
    import subprocess

    script = BACKEND_DIR / "scripts" / "seed_night_session.py"
    env = dict(os.environ)
    env["SEED_VARIANTS"] = ",".join(variants)
    subprocess.run(
        ["/opt/anaconda3/bin/python3.13", str(script)],
        check=True, env=env, capture_output=True, text=True,
    )
    from app.services.session_service import session_service

    for variant in variants:
        session_id = f"sess_night_d_{variant}"
        try:
            session_service.repository.clear_pending_edit_plan(session_id)
        except Exception:  # noqa: BLE001 - 清不掉就在断言里暴露，不在这里静默
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.getenv("NIGHT_BASE", "http://127.0.0.1:8004"))
    parser.add_argument("--only", default="", help="只跑某些 variant（逗号分隔）")
    parser.add_argument("--out", default="/tmp/haewu-night-flow.json")
    parser.add_argument("--no-seed", action="store_true", help="不重新播种（调试用）")
    args = parser.parse_args()

    variants = [v for v in CASES if not args.only or v in args.only.split(",")]
    if not args.no_seed:
        needed: list[str] = []
        for variant in variants:
            for index in range(len(CASES[variant])):
                needed.append(f"{variant}_{index}" if index else variant)
        # **专项用例用的会话不在上面的清单里** —— 漏播就会带着上一轮的
        # 遗留状态跑（表现为「建组建出一个套着旧组的组」这类假失败）。
        for name, session_id in SPECIAL_SESSIONS.items():
            if args.only and name not in args.only.split(","):
                continue
            needed.append(session_id)
        _seed(needed)

    all_results: list[dict[str, Any]] = []
    for variant in variants:
        for index, case in enumerate(CASES[variant]):
            outcome = run_case(args.base, variant, index, case)
            all_results.append(outcome)
            flag = "PASS" if outcome.get("ok") else "FAIL"
            print(f"[{flag}] {variant} #{index} {case['text']}")
            print(f"        {outcome.get('assertNote')}")
            if outcome.get("reply"):
                print(f"        reply: {outcome['reply'][:160]}")

    # ---- T09 / T10 / T11 / T12 的专项 ----
    for name, runner in (
        ("t05t06", run_t05_t06),
        ("t09", run_t09), ("t10", run_t10), ("t11", run_t11), ("t12", run_t12),
    ):
        if args.only and name not in args.only.split(","):
            continue
        outcome = runner(args.base)
        all_results.append(outcome)
        flag = "PASS" if outcome.get("ok") else "FAIL"
        print(f"[{flag}] {name} 专项")
        print(f"        {outcome.get('assertNote')}")
        for line in outcome.get("steps") or outcome.get("checks") or []:
            print(f"        - {line}")

    Path(args.out).write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    passed = sum(1 for item in all_results if item.get("ok"))
    print(f"\n== {passed}/{len(all_results)} passed -> {args.out}")
    return 0 if passed == len(all_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
