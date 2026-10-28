"""探索性“先看模版”：只从当前可运行的 Mock 能力中补默认项。

关键约束（来自本轮缺陷 M-06-ROUTE-001）：
    - 不能只因为出现「模板 / 模版」就劫持请求；
    - 用户同时给出的业务条件（数据源、粒度、设备过滤、TopN、派生列……）
      必须完整传给 builder，不能被模板流的固定模板悄悄吞掉；
    - 报表名和「要不要趋势图」不能写死，要用用户已经说过的 / 已经解析出来的信息。
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from app.data.machine_room_energy import CATALOG_MACHINE_ROOM_ENERGY
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
from app.services import routing_rules
from app.services.catalog_mock_service import list_catalogs
from app.services.metric_mock_service import list_metrics
from app.services.report_draft_service import (
    DefinitionBuildError,
    build_definition_draft,
    build_policy_draft,
)
from app.services.scope_service import (
    is_project_authorized,
    is_subject_authorized,
)
from app.services.session_context import current_session_key


logger = logging.getLogger(__name__)

_TEMPLATE_REQUEST = re.compile(r"(模板|模版)")
_GENERATION_REQUEST = re.compile(r"(生成|做|看看|看个|给我|来个|来一)")
_ROOM = routing_rules.ROOM_PATTERN  # 统一到规则层，避免两套机房正则
_ROOM_TOKEN = routing_rules.ROOM_TOKEN_PATTERN

# 「非昨日时间范围」判定。
# 注意不要把「小时 / 分钟」当成时间范围：「小时趋势」「分钟级粒度」是粒度，不是时间窗。
_OTHER_TIME = re.compile(
    r"(今天|今日|明天|前天|本周|上周|本月|最近|近\s*\d+\s*(天|小时)"
    r"|\d+\s*(天|小时)|实时|20\d{2}[-年/]\d{1,2})"
)
_OTHER_METRIC = re.compile(r"(pue|成本|电费|费用|温度|水耗|碳排放)", re.IGNORECASE)
_CROSS_PROJECT = re.compile(r"(所有项目|全部项目|跨项目|所有机房|全部机房)")

_ROOM_LABEL = "A 机房"
_TIME_LABEL = "昨日"
_METRIC_LABEL = "能耗日报（总耗电、设备排名、小时趋势）"
_DEFAULT_REPORT_NAME = "A 机房能耗日报"


def _builder_capability() -> tuple[frozenset[str], bool]:
    """builder 当前接受哪些参数（带缓存，builder 被替换时缓存自动失效）。"""
    return routing_rules.builder_capability()


def _plan_builder_kwargs(slots: dict[str, Any]) -> dict[str, Any]:
    """把解析出来的业务槽位映射成 builder 能接受的参数。

    只传 builder 真正支持的参数；不支持的条件由 is_request() 提前交回正常路由，
    不会在这里被静默丢弃。
    """
    return routing_rules.plan_builder_kwargs(slots)


class TemplatePreviewFlow:
    """每会话一个轻量选择流程；真正草稿仍由现有 builder 创建。"""

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}

    @staticmethod
    def is_request(request: AgentRequest) -> bool:
        if request.state not in {AgentState.ANALYZING, AgentState.NEEDS_CLARIFICATION}:
            return False
        if not _TEMPLATE_REQUEST.search(request.message):
            return False
        if not _GENERATION_REQUEST.search(request.message):
            return False
        if "修改" in request.message:
            return False
        # 平台支持、但模板流当前表达不了的业务条件 → 交回正常路由，
        # 不要因为一个「模版」关键词就劫持整条请求。
        return not TemplatePreviewFlow._has_unespressible_conditions(request.message)

    @staticmethod
    def _has_unespressible_conditions(message: str) -> bool:
        """模板流当前表达不了的业务条件。

        builder 已经支持的参数直接透传；只有「builder 接不住」的条件
        才把请求交回正常路由，避免条件被模板流悄悄吞掉。
        """
        slots = routing_rules.explicit_slots(message)
        return bool(routing_rules.unsupported_slots(slots))

    @staticmethod
    def _is_available(request: AgentRequest) -> bool:
        if not is_project_authorized(request.context.projectId):
            return False
        if request.context.subjectRef != "machine_room":
            return False
        if not is_subject_authorized(
            request.context.subjectRef, request.context.subjectId
        ):
            return False
        required_fields = {"energyKwh", "deviceId", "deviceName", "timestamp"}
        catalogs = list_catalogs("machine_room")
        metrics = list_metrics("machine_room")
        return any(
            item["sourceRef"] == CATALOG_MACHINE_ROOM_ENERGY["sourceRef"]
            and required_fields <= set(item.get("fields", {}))
            and {"DAY", "HOUR"} <= set(item.get("supportedGrains", []))
            for item in catalogs
        ) and all(
            any(metric["metricRef"] == name and metric.get("fieldRef") == "energyKwh" for metric in metrics)
            for name in ("metric.machine_room.total_energy", "metric.machine_room.device_energy")
        )

    @staticmethod
    def _question(question_id: str, prompt: str, options: list[tuple[str, str]]) -> AgentQuestion:
        return AgentQuestion(
            id=question_id,
            prompt=prompt,
            expects="SINGLE_CHOICE",
            options=[QuestionOption(value=value, label=label) for value, label in options],
            selectionOnly=True,
            allowRestart=True,
        )

    def _ask(self, request: AgentRequest, question: AgentQuestion, reply: str) -> AgentResponse:
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.NEEDS_CLARIFICATION,
            reply=reply,
            designSessionId=request.context.designSessionId,
            questions=[question],
            uiActions=["template_preview_choice"],
        )

    def start(self, request: AgentRequest) -> AgentResponse:
        # 按会话 key 存，不按 conversationId：停止生成会换 conversationId，
        # 但那是同一条对话，流程状态不能丢。
        session_key = current_session_key() or request.conversationId
        self._sessions.pop(session_key, None)
        if not self._is_available(request):
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.BLOCKED_OR_UNSUPPORTED,
                reply="当前范围没有可用于生成模版的机房能耗 Mock 数据，请先确认范围和目录。",
                blocks=[AgentBlock(code="CAPABILITY_NOT_SUPPORTED", message="当前范围没有可用的机房能耗 Mock 数据源")],
            )
        room_match = _ROOM.search(request.message)
        requested_room = None
        if room_match:
            for group in room_match.groups():
                if group:
                    requested_room = f"room_{group.lower()}"
                    break
        else:
            token = _ROOM_TOKEN.search(request.message)
            if token:
                requested_room = f"room_{token.group(1).lower()}"
        unsupported_time = bool(_OTHER_TIME.search(request.message))
        unsupported_metric = bool(_OTHER_METRIC.search(request.message))
        unsupported = []
        if _CROSS_PROJECT.search(request.message):
            unsupported.append("跨项目范围")
        if requested_room and requested_room != "room_a":
            unsupported.append(f"{requested_room.removeprefix('room_').upper()} 机房")
        if unsupported_time:
            unsupported.append("非昨日时间范围")
        if unsupported_metric:
            unsupported.append("非能耗指标")
        if unsupported:
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.BLOCKED_OR_UNSUPPORTED,
                reply=f"当前草稿仅支持 A 机房昨日能耗日报，无法保留你指定的{'、'.join(unsupported)}。请改用支持的条件再生成。",
                blocks=[AgentBlock(
                    code="CAPABILITY_NOT_SUPPORTED",
                    message=f"当前 Mock 草稿不支持：{'、'.join(unsupported)}",
                    alternatives=["A 机房昨日能耗日报"],
                )],
            )
        info = {
            "room": _ROOM_LABEL if requested_room == "room_a" else None,
            "time": _TIME_LABEL if ("昨日" in request.message or "昨天" in request.message) and not unsupported_time else None,
            "metric": _METRIC_LABEL if ("能耗" in request.message or "耗电" in request.message) and not unsupported_metric else None,
            "specified": set(),
            # 规则层解析出来的业务条件：不再被模板流的固定模板吞掉。
            "slots": routing_rules.explicit_slots(request.message),
            "defaults": routing_rules.default_slots(request.message),
        }
        info["specified"] = {key for key in ("room", "time", "metric") if info[key]}
        if len(self._sessions) >= 256:
            self._sessions.pop(next(iter(self._sessions)))
        self._sessions[session_key] = info
        if len(info["specified"]) == 3:
            return self._generate(request, info)
        return self._ask(
            request,
            self._question(
                "template_preview_mode",
                "信息还不完整，你希望怎么继续？",
                [("补齐信息", "逐项选择对象、时间与指标"), ("直接生成模版", "用可用数据补默认项，先看草稿")],
            ),
            "当前可用的是 A 机房的昨日能耗日报（Mock 数据）。可以补齐信息，或先按可用默认项生成模版看看。",
        )

    def answer(self, request: AgentRequest, question_id: str) -> AgentResponse:
        session_key = current_session_key() or request.conversationId
        info = self._sessions.get(session_key)
        if not info:
            return self.start(request)
        if request.message == "重新开始":
            self._sessions.pop(session_key, None)
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.ANALYZING,
                reply="已重新开始。你可以描述新的报表需求。",
                uiActions=["restart_from_template_editing"],
            )
        if question_id == "template_preview_mode":
            if request.message == "直接生成模版":
                return self._generate(request, info)
            return self._ask_next(request, info)
        field = {
            "template_preview_room": ("room", _ROOM_LABEL),
            "template_preview_time": ("time", _TIME_LABEL),
            "template_preview_metric": ("metric", _METRIC_LABEL),
        }.get(question_id)
        if field:
            info[field[0]] = field[1]
            info["specified"].add(field[0])
        return self._ask_next(request, info)

    def _ask_next(self, request: AgentRequest, info: dict[str, Any]) -> AgentResponse:
        for key, question_id, prompt, label in (
            ("room", "template_preview_room", "请选择有数据且有权限的对象：", _ROOM_LABEL),
            ("time", "template_preview_time", "请选择当前报表支持的时间范围：", _TIME_LABEL),
            ("metric", "template_preview_metric", "请选择已登记且可运行的指标组合：", _METRIC_LABEL),
        ):
            if info[key] is None:
                return self._ask(request, self._question(question_id, prompt, [(label, label)]), prompt)
        return self._generate(request, info)

    @staticmethod
    def _resolve_include_trend(slots: dict[str, Any], message: str) -> bool:
        """要不要趋势图：用用户说过的 / 解析出来的，不写死 True。"""
        return routing_rules.resolve_include_trend(slots)

    def _generate(self, request: AgentRequest, info: dict[str, Any]) -> AgentResponse:
        session_key = current_session_key() or request.conversationId
        self._sessions.pop(session_key, None)
        slots: dict[str, Any] = dict(info.get("slots") or {})
        defaults: dict[str, Any] = dict(info.get("defaults") or {})

        # 报表名：用用户已经说过的名字，不再固定写死「A 机房能耗日报」。
        report_name = (
            slots.get("reportName")
            or info.get("reportName")
            or _DEFAULT_REPORT_NAME
        )
        room_id = slots.get("subjectId") or "room_a"
        include_trend = self._resolve_include_trend(slots, request.message)

        # 「只要 X」是白名单语义；「包含 X」不代表别的区块不要，
        # 这时不能把 componentRequests 当白名单传下去，否则模板自带的
        # 数据质量卡片会被静默删掉。
        builder_slots = dict(slots)
        if not routing_rules.is_exclusive_request(request.message):
            builder_slots.pop("componentRequests", None)

        policy = build_policy_draft(report_name)
        try:
            definition = build_definition_draft(
                report_name,
                room_id,
                include_trend=include_trend,
                **_plan_builder_kwargs(builder_slots),
            )
        except DefinitionBuildError as exc:
            # 组装层明确拒绝（数据源 / 粒度 / 授权 / 组件不支持）：
            # 必须阻断并说明原因，不能退回一份「看起来对、其实不是那件事」的草稿。
            logger.warning(
                "[template_preview_flow] 组装被拒绝 code=%s message=%s",
                exc.code,
                exc.message,
            )
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.BLOCKED_OR_UNSUPPORTED,
                reply=exc.message,
                designSessionId=request.context.designSessionId,
                blocks=[
                    AgentBlock(
                        code=exc.code,
                        message=exc.message,
                        alternatives=list(exc.alternatives),
                    )
                ],
                uiActions=["continue_editing"],
            )

        fields = {"room": _ROOM_LABEL, "time": _TIME_LABEL, "metric": _METRIC_LABEL}
        # generationInputs：模板流既有的三段展示位，前端按这个契约渲染。
        generation_inputs: dict[str, dict[str, Any]] = {
            field: {"value": value, "source": "用户指定" if field in info["specified"] else "系统暂选"}
            for field, value in fields.items()
        }
        definition.metadata["generationInputs"] = generation_inputs

        # resolvedInputs：结构化槽位 + 来源标记，和路由层输出的字段同名同结构，
        # 前端用同一套渲染逻辑就能展示「系统暂选 / 用户指定」。
        resolved_inputs: dict[str, dict[str, Any]] = {}
        for slot in routing_rules.SLOT_KEYS:
            if slot == "subjectName":
                continue
            if slot in slots:
                resolved_inputs[slot] = {"value": slots[slot], "source": "用户指定"}
            elif slot in defaults:
                resolved_inputs[slot] = {"value": defaults[slot], "source": "系统暂选"}
        definition.metadata["resolvedInputs"] = resolved_inputs

        defaults_used = [
            field for field, value in fields.items() if field not in info["specified"]
        ]
        note = (
            f"系统暂选：{'、'.join(defaults_used)}；可在草稿中继续修改。"
            if defaults_used
            else "条件均由你指定。"
        )
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.WAITING_USER_CONFIRM,
            reply=f"已基于当前可用的 Mock 数据生成 {report_name} 草稿。{note}",
            designSessionId=request.context.designSessionId or f"session_{uuid.uuid4().hex[:8]}",
            domainObjects=AgentResponseDomainObjects(
                reportPolicyDraft=policy,
                reportDefinitionDraft=definition,
            ),
            toolRequests=[ToolRequest(id="req_save_1", type="SAVE_DEFINITION_DRAFT", target="JavaReportService")],
            uiActions=["preview_framework", "confirm_framework", "reject_framework", "request_modification"],
        )


template_preview_flow = TemplatePreviewFlow()
