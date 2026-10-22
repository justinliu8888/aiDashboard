"""把 app 命名空间的运行日志写入本机可轮转文件。"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding)


def configure_app_file_logging(path: Path | None = None) -> Path:
    target = path or Path(
        os.environ.get(
            "AI_BACKEND_LOG_PATH",
            str(Path.home() / "Library/Logs/haewu-ai-report/backend.log"),
        )
    ).expanduser()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    logger = logging.getLogger("app")
    if not any(
        isinstance(handler, RotatingFileHandler)
        and handler.baseFilename == str(target.resolve())
        for handler in logger.handlers
    ):
        handler = _PrivateRotatingFileHandler(
            target, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        os.chmod(target, 0o600)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s"
        ))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return target
