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
except OSError as exc:
    # 异常原文不进日志（#61）：留栈不留原文 —— 异常对象换成 None，
    # 消息正文仍是不含敏感信息的事实陈述。写文件失败不影响启动，
    # 继续用控制台日志。
    logging.getLogger(__name__).exception(
        "无法写入后端文件日志，继续使用控制台日志",
        exc_info=(type(exc), None, exc.__traceback__),
    )

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


class _NoCacheStaticFiles(StaticFiles):
    """开发期静态资源：**不缓存**。

    工作台的前端脚本改得很勤。浏览器对没有 ``Cache-Control`` 的静态资源
    会按启发式规则缓存（甚至连 HTML 都缓存），于是「改了没生效」「调试
    代码跑的是旧版」这类问题极难查 —— 明明服务端已经是新文件，页面还在
    跑旧脚本。

    这里统一回 ``no-store``：改完刷新就是新的。生产部署另有 CDN / 缓存
    策略，不受这里影响。
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return response


if _FRONTEND_DIR.exists():
    app.mount(
        "/playground",
        _NoCacheStaticFiles(directory=str(_FRONTEND_DIR), html=True),
        name="playground",
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
