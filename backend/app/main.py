"""FastAPI 应用入口。"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.routers import agent as agent_router
from app.routers import mock_capability
from app.routers import report as report_router
from app.routers import sessions as sessions_router
from app.logging_config import configure_app_file_logging


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
try:
    configure_app_file_logging()
except OSError:
    logging.getLogger(__name__).exception("无法写入后端文件日志，继续使用控制台日志")

app = FastAPI(title="智能报表 AI 后端", version="1.0.0")

# 本地联调允许任意来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agent_router.router)
app.include_router(mock_capability.router)
app.include_router(report_router.router)
app.include_router(sessions_router.router)


# 挂载前端静态目录：backend/../frontend
_FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
_WORKBENCH_URL = "/playground/pages/workbench.html"


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url=_WORKBENCH_URL)


@app.get("/playground")
@app.get("/playground/")
def playground_root() -> RedirectResponse:
    return RedirectResponse(url=_WORKBENCH_URL)


if _FRONTEND_DIR.exists():
    app.mount(
        "/playground",
        StaticFiles(directory=str(_FRONTEND_DIR), html=True),
        name="playground",
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
