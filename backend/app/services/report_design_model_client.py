"""真实模型调用适配：把模型客户端包装成 B 需要的注入式 ``model_call``。

边界（冻结契约 + 交接要求）：

    - 只在**这里**读凭证。B 不建模型、不复制配置、不碰密钥；
    - **超时在外层处理**：这里给单次调用套总时限，
      超时抛 TimeoutError，由 B 统一收敛成 ``MODEL_CALL_FAILED``；
    - 不统计 token、不统计调用次数、不做成本对比（规格明确不做这些）；
    - 不建全局单例副作用：客户端惰性创建，用完随对象走。

诊断（#57）：这里同时是**请求标识的产地** —— 每次模型调用生成一个
``requestId``，并在**响应被丢弃之前**采集上游给的结束原因、token 用量与
响应标识。采集只做记录，不改变模型行为、提示词与重试。

普通只读提问**不走这里**：提问由协调者挂回旧查询链路。
"""
from __future__ import annotations

import asyncio
import logging
from time import perf_counter
from typing import Any

from app.config import get_settings
from app.services import report_diagnostics_service as diagnostics


logger = logging.getLogger(__name__)

#: 单次方案设计的总时限（秒）。
#:
#: 方案设计是一次「想清楚再写」的完整生成 —— 实测真实模型约 45 秒，
#: 比事实查询慢得多，但也不能无限等：超时必须变成可辨识的失败，
#: 不能一直转圈，也不能让用户以为还在跑。
#:
#: 这里用**独立的**时限，不复用通用超时（30 秒）：
#: 那个时限是给短问答用的，套在完整方案生成上会稳定超时。
#: 实际取值见 ``Settings.llm_design_timeout``（环境变量 LLM_DESIGN_TIMEOUT）。
DEFAULT_DESIGN_TIMEOUT_SECONDS = 120


class ReportDesignModelClient:
    """把外部模型包装成 ``async model_call(messages) -> str``。

    对外只暴露一个可调用对象；凭证只在这个类里读一次，
    不外泄、不复制到别的模块。
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._model = self._settings.design_model
        self._timeout = getattr(
            self._settings, "llm_design_timeout", DEFAULT_DESIGN_TIMEOUT_SECONDS
        )
        self._client = None
        if self._settings.active_api_key:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self._settings.active_api_key,
                # base_url 是 **API 根地址**，OpenAI SDK 自己拼 /chat/completions。
                base_url=self._settings.active_base_url,
                # 单次请求的超时也用方案设计的时限：
                # 短问答的时限套在完整方案生成上会稳定超时。
                timeout=self._timeout,
                # **重试一次**（SDK 自带的重试）：方案设计这一轮要跑四五十秒，
                # 期间上游偶尔会把长连接掐断（实测在 TLS 读响应头那一步断开）。
                # 不重试的话，用户看到的就是「请求失败：network error」，
                # 整轮白等。重试只覆盖连接类错误与 5xx/429，不会重试 4xx 业务错误。
                max_retries=1,
            )
        else:
            logger.warning(
                "[report_design] 未配置 %s 的 API Key，方案设计不可用",
                self._settings.provider_name,
            )

    @property
    def available(self) -> bool:
        """模型客户端是否可用。"""
        return self._client is not None

    @property
    def model(self) -> str:
        return self._model

    async def __call__(self, messages: list[dict[str, Any]]) -> str:
        """跑一次模型，返回文本。

        超时在**这一层**兜住：外层给的总时限到了就抛 TimeoutError，
        B 会把它收敛成 ``MODEL_CALL_FAILED``，界面显示可重试的失败，
        不会卡在「处理中」。

        每一次调用都有**自己的** ``requestId``（诊断契约 §6.2）：
        同一会话的连续两轮、或者同一轮里的重试，标识都不同，
        耗时与结果不会互相覆盖。
        """
        if self._client is None:
            raise RuntimeError(
                f"方案设计模型不可用：未配置 {self._settings.provider_name} 的 API Key"
            )

        state = diagnostics.begin_model_call(model_id=self._model)
        started = perf_counter()
        try:
            # 外层给的总时限比单次请求时限多留一点余量：
            # 真正兜住「模型端不报错但一直不返回」的情况。
            text = await asyncio.wait_for(
                self._complete(messages, state),
                timeout=self._timeout + 10,
            )
        except asyncio.CancelledError:
            # 本轮被取消：也要留下这一轮的标识与耗时。
            state.duration_ms = round((perf_counter() - started) * 1000)
            state.error_type = type(asyncio.CancelledError()).__name__
            diagnostics.log_model_call(state)
            raise
        except Exception as exc:  # noqa: BLE001
            state.duration_ms = round((perf_counter() - started) * 1000)
            # 只记异常类型：上游报错原文可能带凭据，不进日志。
            state.error_type = type(exc).__name__
            diagnostics.log_model_call(state)
            raise
        state.duration_ms = round((perf_counter() - started) * 1000)
        diagnostics.log_model_call(state)
        return text

    async def _complete(
        self, messages: list[dict[str, Any]], state: diagnostics.ModelCallState
    ) -> str:
        assert self._client is not None
        response = await self._client.chat.completions.create(
            model=self._model,
            temperature=self._settings.llm_temperature,
            messages=messages,
            # MiniMax 默认把思考内联进 content，会污染 JSON 解析；
            # 显式关思考，拿到的是纯 JSON 正文。
            extra_body=self._settings.request_extra_body,
        )
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError("模型没有返回内容")
        content = getattr(choices[0].message, "content", None)
        if not isinstance(content, str):
            raise RuntimeError("模型返回的内容不是文本")

        # ---- 上游元信息：在响应被丢弃之前采集 ----
        # 上游没给的写「未知」，**不靠耗时或文本猜截断**。
        state.finish_reason = diagnostics.text_or_unknown(
            getattr(choices[0], "finish_reason", None)
        )
        state.token_usage = diagnostics.token_usage_or_unknown(
            getattr(response, "usage", None)
        )
        state.response_id = diagnostics.text_or_unknown(getattr(response, "id", None))
        state.output_length = len(content)
        return content
