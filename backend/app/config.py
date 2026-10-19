"""后端运行时配置：从环境变量读取，`.env` 已被 .gitignore 保护。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# 加载项目 backend/.env（如果存在）
_BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BACKEND_DIR / ".env")


class Settings:
    """运行时配置。"""

    # Agent 实现选择：fake / agentscope
    agent_impl: str = os.getenv("AGENT_IMPL", "fake").lower()

    # 火山方舟
    volc_api_key: str = os.getenv("VOLC_API_KEY", "")
    volc_base_url: str = os.getenv(
        "VOLC_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
    )
    volc_model: str = os.getenv("VOLC_MODEL", "doubao-seed-1-6-250615")
    volc_temperature: float = float(os.getenv("VOLC_TEMPERATURE", "0.2"))
    volc_timeout: int = int(os.getenv("VOLC_TIMEOUT", "30"))

    # ContextEngine 两段式模型：轻量接待员 + 重路由器
    volc_context_concierge_model: str = os.getenv(
        "VOLC_CONTEXT_CONCIERGE_MODEL", os.getenv("VOLC_MODEL", "doubao-seed-1-6-250615")
    )
    volc_context_router_model: str = os.getenv(
        "VOLC_CONTEXT_ROUTER_MODEL", os.getenv("VOLC_MODEL", "doubao-seed-1-6-250615")
    )
    volc_context_concierge_timeout: int = int(os.getenv("VOLC_CONTEXT_CONCIERGE_TIMEOUT", "30"))
    volc_context_router_timeout: int = int(os.getenv("VOLC_CONTEXT_ROUTER_TIMEOUT", "30"))

    # 可靠性
    llm_repair_retries: int = int(os.getenv("LLM_REPAIR_RETRIES", "1"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
