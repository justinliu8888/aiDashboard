"""反馈到期清理（维护命令）。

用法（**cwd = backend**）::

    # 清理配置里的库（生产 / 部署环境）
    python scripts/cleanup_feedback.py --dry-run     # 先看会清什么
    python scripts/cleanup_feedback.py

    # 清理指定的库（隔离环境）
    python scripts/cleanup_feedback.py --db-path /tmp/preview.db --attachment-dir /tmp/preview-att

范围（20261104 确认稿「存储与权限」）：

* 只清**反馈 Module 自己的**到期记录与它们引用的附件；
* **不清**用户会话、草稿、方案、消息，也不碰任何未知路径；
* 附件按服务端生成的**安全文件名**精确删除，不扫描整个数据目录。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.services import feedback_service  # noqa: E402
from app.storage.sqlite_store import SqliteSessionRepository  # noqa: E402


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="清理到期的反馈记录与附件")
    parser.add_argument(
        "--db-path",
        default=str(settings.session_db_path),
        help="要清理的 SQLite 库（默认：配置里的会话库）",
    )
    parser.add_argument(
        "--attachment-dir",
        default=str(settings.feedback_attachment_dir),
        help="私有附件目录（默认：配置里的附件目录）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只看会清什么，不真的删",
    )
    args = parser.parse_args()

    settings.feedback_attachment_dir = Path(args.attachment_dir).expanduser()
    repo = SqliteSessionRepository(Path(args.db_path).expanduser())
    store = feedback_service.store_for(repo)
    store.ensure_schema()

    if args.dry_run:
        conn = store.connection
        now = feedback_service.now_iso()
        expired_feedbacks = conn.execute(
            "SELECT COUNT(*) FROM feedback_items WHERE expires_at <= ?", (now,)
        ).fetchone()[0]
        expired_targets = conn.execute(
            "SELECT COUNT(*) FROM feedback_targets WHERE expires_at <= ?", (now,)
        ).fetchone()[0]
        print(
            f"[dry-run] 将清理：到期反馈 {expired_feedbacks} 条、"
            f"到期目标 {expired_targets} 条",
            flush=True,
        )
        return 0

    result = feedback_service.cleanup_expired(repo, settings=settings)
    print(
        f"已清理：反馈 {result.get('feedbacks', 0)} 条、"
        f"附件记录 {result.get('attachments', 0)} 条、"
        f"目标 {result.get('targets', 0)} 条、"
        f"附件文件 {result.get('attachment_files_removed', 0)} 个",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
