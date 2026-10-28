"""真实模型调用适配：把模型客户端包装成 B 需要的注入式 ``model_call``。

边界（冻结契约 + 交接要求）：

    - 只在**这里**读凭证。B 不建模型、不复制配置、不碰密钥；
    - **超时在外层处理**：这里给单次调用套总时限，
      超时抛 TimeoutError，由 B 统一收敛成 ``MODEL_CALL_FAILED``；
    - 不统计 token、不统计调用次数、不做成本对比（规格明确不做这些）；
    - 不建全局单例副作用：客户端惰性创建，用完随对象走。

普通只读提问**不走这里**：提问由协调者挂回旧查询链路。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import get_settings


logger = logging.getLogger(__name__)

#: 单次方案设计的总时限（秒）。
#:
#: 方案设计是一次「想清楚再写」的完整生成 —— 实测真实模型约 45 秒，
#: 比事实查询慢得多，但也不能无限等：超时必须变成可辨识的失败，
#: 不能一直转圈，也不能让用户以为还在跑。
#:
#: 这里用**独立的**时限，不复用 ``VOLC_TIMEOUT``（30 秒）：
#: 那个时限是给短问答用的，套在完整方案生成上会稳定超时。
DEFAULT_DESIGN_TIMEOUT_SECONDS = 120


class ReportDesignModelClient:
    """把外部模型包装成 ``async model_call(messages) -> str``。

    对外只暴露一个可调用对象；凭证只在这个类里读一次，
    不外泄、不复制到别的模块。
    """

    def __init__(
        self,
        *,
        timeout: int | None = None,
        model: str | None = None,
    ) -> None:
        self._settings = get_settings()
        self._model = model or self._settings.volc_model
        self._timeout = timeout or DEFAULT_DESIGN_TIMEOUT_SECONDS
        self._client = None
        if self._settings.volc_api_key:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self._settings.volc_api_key,
                base_url=self._settings.volc_base_url,
                # 单次请求的超时也用方案设计的时限：
                # 短问答的时限套在完整方案生成上会稳定超时。
                timeout=self._timeout,
                max_retries=0,
            )
        else:
            logger.warning("[report_design] 未配置 VOLC_API_KEY，方案设计不可用")

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
        """
        if self._client is None:
            raise RuntimeError("方案设计模型不可用：未配置 VOLC_API_KEY")

        # 外层给的总时限比单次请求时限多留一点余量：
        # 真正兜住「模型端不报错但一直不返回」的情况。
        return await asyncio.wait_for(
            self._complete(messages),
            timeout=self._timeout + 10,
        )

    async def _complete(self, messages: list[dict[str, Any]]) -> str:
        assert self._client is not None
        response = await self._client.chat.completions.create(
            model=self._model,
            temperature=self._settings.volc_temperature,
            messages=messages,
        )
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError("模型没有返回内容")
        content = getattr(choices[0].message, "content", None)
        if not isinstance(content, str):
            raise RuntimeError("模型返回的内容不是文本")
        return content
