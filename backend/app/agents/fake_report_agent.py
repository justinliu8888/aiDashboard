"""FakeReportAgent：用固定规则覆盖 00_统筹.md 第 6 章 6 个验收样例。

目的只是跑通接口契约和状态机，不验证模型质量。
后续用 AgentScope 替换时，只需实现同样的 handle() 方法签名。
"""
from __future__ import annotations

import re
import uuid

from app.schemas.agent import (
    AgentBlock,
    AgentQuestion,
    AgentRequest,
    AgentResponse,
    AgentResponseDomainObjects,
    AgentState,
    QuestionOption,
    ToolRequest,
)
from app.services.catalog_mock_service import supports_grain
from app.services.report_draft_service import (
    REGION_IDS,
    build_change_set_remove_trend,
    build_change_set_trend_7d,
    build_definition_draft,
    build_policy_draft,
)
from app.data.machine_room_energy import (
    AUTHORIZED_SCOPE,
    CATALOG_MACHINE_ROOM_ENERGY,
)


# ---------- 关键词匹配工具 ----------

_PAT_CROSS_PROJECT = re.compile(r"(所有项目|全部项目|跨项目)")
_PAT_MINUTE = re.compile(r"(分钟|分级|minute)", re.IGNORECASE)
_PAT_ANOMALY_ONLY = re.compile(r"异常设备")
_PAT_TREND_7D = re.compile(r"(趋势.*(最近|近).*7\s*天|趋势.*7\s*天)")
_PAT_REMOVE_TREND = re.compile(r"(不要趋势|去掉趋势|删除?趋势|不要.*趋势图)")
_PAT_GENERATE_ENERGY = re.compile(r"(生成|做|给我).*(耗电|能耗)")


def _new_session_id() -> str:
    return f"session_{uuid.uuid4().hex[:8]}"


class FakeReportAgent:
    """首功能 Fake 大脑；按规则匹配生成响应。"""

    def handle(self, request: AgentRequest) -> AgentResponse:
        state = request.state

        # 1. 处理修改类意图（优先匹配，不受 state 限制）
        if _PAT_TREND_7D.search(request.message):
            return self._handle_change_trend_7d(request)
        if _PAT_REMOVE_TREND.search(request.message):
            return self._handle_change_remove_trend(request)

        # 2. 按 state 分发
        if state == AgentState.ANALYZING:
            return self._handle_analyzing(request)

        if state == AgentState.WAITING_USER_CONFIRM:
            # 用户在确认阶段又提了一条新诉求，回到 analyzing
            return self._handle_analyzing(request)

        # 兜底：无法推进
        return self._fallback_clarification(request)

    # ---------- ANALYZING ----------

    def _handle_analyzing(self, request: AgentRequest) -> AgentResponse:
        message = request.message
        ctx = request.context

        # 权限越界
        if _PAT_CROSS_PROJECT.search(message):
            return self._blocked(
                request,
                code="PERMISSION_DENIED",
                reply="当前账号只能查看有权限的项目，无法生成跨项目能耗排名。",
                message_detail="跨项目范围超出 IAM 授权",
                alternatives=["限定在当前项目", "限定在有权限的项目列表"],
            )

        # 能力不支持（分钟级）
        if _PAT_MINUTE.search(message) and (
            "能耗" in message or "耗电" in message
        ):
            if not supports_grain(
                CATALOG_MACHINE_ROOM_ENERGY["sourceRef"], "MINUTE"
            ):
                return self._blocked(
                    request,
                    code="CAPABILITY_NOT_SUPPORTED",
                    reply=(
                        f"{self._room_label(ctx.subjectId)} 能耗数据目前仅登记"
                        "小时和日粒度，无法提供分钟级历史。可改为小时趋势或日趋势。"
                    ),
                    message_detail=(
                        f"{CATALOG_MACHINE_ROOM_ENERGY['sourceRef']} 不支持分钟粒度"
                    ),
                    alternatives=["改为小时趋势", "改为日趋势"],
                    extra_tool_requests=[
                        ToolRequest(
                            id="req_cat_1",
                            type="QUERY_CATALOG",
                            target="CatalogTool",
                            arguments={
                                "sourceRef": CATALOG_MACHINE_ROOM_ENERGY["sourceRef"]
                            },
                        )
                    ],
                )

        # 信息不足（异常设备）
        if _PAT_ANOMALY_ONLY.search(message) and not _PAT_GENERATE_ENERGY.search(
            message
        ):
            return self._needs_clarification_anomaly(request)

        # 正常生成（命中生成/做+耗电/能耗；或没有命中其他规则的默认分支）
        if _PAT_GENERATE_ENERGY.search(message):
            return self._generate_full_draft(request)

        # 兜底
        return self._fallback_clarification(request)

    # ---------- 正常生成 ----------

    def _generate_full_draft(self, request: AgentRequest) -> AgentResponse:
        ctx = request.context
        room_label = self._room_label(ctx.subjectId)
        report_name = f"{room_label}能耗日报"
        policy = build_policy_draft(report_name)
        definition = build_definition_draft(
            report_name=report_name,
            room_id=ctx.subjectId,
            include_trend=True,
        )
        session_id = ctx.designSessionId or _new_session_id()

        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.WAITING_USER_CONFIRM,
            reply=f"已生成 {report_name} 草稿，请确认是否按这个框架继续校验和预览。",
            designSessionId=session_id,
            domainObjects=AgentResponseDomainObjects(
                reportPolicyDraft=policy,
                reportDefinitionDraft=definition,
            ),
            toolRequests=[
                ToolRequest(
                    id="req_save_1",
                    type="SAVE_DEFINITION_DRAFT",
                    target="JavaReportService",
                )
            ],
            uiActions=[
                "preview_framework",
                "confirm_framework",
                "regenerate_framework",
            ],
        )

    # ---------- ChangeSet ----------

    def _handle_change_trend_7d(self, request: AgentRequest) -> AgentResponse:
        base_rev = request.context.baseDefinitionRevision or 0
        change_set = build_change_set_trend_7d(base_rev)
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.DEFINITION_DRAFT_READY,
            reply="我会把趋势图改为近 7 个完整自然日，总耗电卡片仍按昨日统计。",
            designSessionId=request.context.designSessionId,
            domainObjects=AgentResponseDomainObjects(changeSet=change_set),
            uiActions=["show_diff", "accept_change", "reject_change"],
        )

    def _handle_change_remove_trend(self, request: AgentRequest) -> AgentResponse:
        base_rev = request.context.baseDefinitionRevision or 0
        change_set = build_change_set_remove_trend(base_rev)
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.DEFINITION_DRAFT_READY,
            reply="已删除趋势图区域，保留总耗电卡片和设备排名表。",
            designSessionId=request.context.designSessionId,
            domainObjects=AgentResponseDomainObjects(changeSet=change_set),
            uiActions=["show_diff", "accept_change", "reject_change"],
        )

    # ---------- 追问 / 拦截 / 兜底 ----------

    def _needs_clarification_anomaly(
        self, request: AgentRequest
    ) -> AgentResponse:
        session_id = request.context.designSessionId or _new_session_id()
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.NEEDS_CLARIFICATION,
            reply=(
                "你想看哪类异常设备？能耗异常、离线异常还是告警异常？"
                "另外需要确认统计范围和时间窗口。"
            ),
            designSessionId=session_id,
            questions=[
                AgentQuestion(
                    id="q_anomaly_type",
                    prompt="哪类异常设备？",
                    expects="SINGLE_CHOICE",
                    options=[
                        QuestionOption(value="ENERGY", label="能耗异常"),
                        QuestionOption(value="OFFLINE", label="离线异常"),
                        QuestionOption(value="ALARM", label="告警异常"),
                    ],
                ),
                AgentQuestion(
                    id="q_scope", prompt="统计范围？", expects="TEXT"
                ),
                AgentQuestion(
                    id="q_time_window",
                    prompt="时间窗口？",
                    expects="DATE_RANGE",
                ),
            ],
        )

    def _blocked(
        self,
        request: AgentRequest,
        *,
        code: str,
        reply: str,
        message_detail: str,
        alternatives: list[str],
        extra_tool_requests: list[ToolRequest] | None = None,
    ) -> AgentResponse:
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.BLOCKED_OR_UNSUPPORTED,
            reply=reply,
            designSessionId=request.context.designSessionId,
            blocks=[
                AgentBlock(
                    code=code, message=message_detail, alternatives=alternatives
                )
            ],
            toolRequests=extra_tool_requests or [],
            uiActions=["continue_editing"],
        )

    def _fallback_clarification(self, request: AgentRequest) -> AgentResponse:
        session_id = request.context.designSessionId or _new_session_id()
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.NEEDS_CLARIFICATION,
            reply=(
                "没太听懂你的需求。可以告诉我想看哪个机房、什么指标、时间范围吗？"
            ),
            designSessionId=session_id,
            questions=[
                AgentQuestion(
                    id="q_free_text",
                    prompt="请补充需求描述，例如“生成 A 机房昨日能耗日报”",
                    expects="TEXT",
                )
            ],
        )

    # ---------- 工具 ----------

    @staticmethod
    def _room_label(room_id: str) -> str:
        mapping = {"room_a": "A 机房", "room_b": "B 机房"}
        return mapping.get(room_id, room_id)
