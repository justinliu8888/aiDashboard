"""为**预览 / 自检**造一条带评价目标的会话（隔离库专用）。

用途：K3 前端自检需要「最小seed」—— 两条合法的 reply 目标、一个停止
观察目标、一份当前受限菜单。这个脚本把它们造出来。

用法（**cwd = backend**）::

    python scripts/seed_feedback_demo.py --db-path /tmp/preview.db

**安全边界**：这个脚本**只接受隔离库**。传默认用户库会被直接拒绝 ——
预览用的数据不该落进用户真实会话库。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.services import feedback_service  # noqa: E402
from app.storage.feedback_store import dedupe_key, fingerprint  # noqa: E402
from app.storage.sqlite_store import SqliteSessionRepository  # noqa: E402


DEMO_SESSION_ID = "sess_feedback_demo"

MENU = {
    "state": "WAITING_USER_CONFIRM",
    "question": {
        "id": "template_edit_confirm",
        "prompt": "请选择下一步操作：",
        "expects": "SINGLE_CHOICE",
        "lockScope": "input_and_menu",
        "options": [
            {"value": "同意", "label": "同意"},
            {"value": "修改", "label": "修改"},
        ],
        "stepId": "step_feedback_demo",
        "stepVersion": 1,
    },
}

REPLIES = [
    ("请描述你要生成的报表，例如「A 机房昨日能耗日报」。", "ANALYZING"),
    ("已生成报表草稿，右侧可以直接编辑；满意后点「同意」。", "WAITING_USER_CONFIRM"),
]


def _refuse_default(db_path: Path) -> None:
    """用户数据目录里的库**拒绝**：预览数据不该落进真实会话库。

    判据是**用户数据目录本身**（``backend/data``），不是「当前配置指向
    哪里」—— 预览时通常会把 ``AI_SESSION_DB_PATH`` 指到别处，这时
    拿配置值当基准反而拦不住真正的用户库。
    """
    from app.config import _BACKEND_DIR

    user_data_dir = (Path(_BACKEND_DIR) / "data").resolve()
    try:
        resolved = db_path.resolve()
    except OSError:  # pragma: no cover
        resolved = db_path
    try:
        resolved.relative_to(user_data_dir)
        inside = True
    except ValueError:
        inside = False
    if inside:
        raise SystemExit(
            f"拒绝：{resolved} 在用户数据目录 {user_data_dir} 里。"
            "预览 seed 请传一个**隔离**的 --db-path。"
        )


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="为预览造带评价目标的会话（隔离库）")
    parser.add_argument("--db-path", required=True, help="隔离库的路径（必填）")
    parser.add_argument(
        "--attachment-dir",
        default=str(settings.feedback_attachment_dir),
        help="私有附件目录",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser()
    _refuse_default(db_path)
    settings.feedback_attachment_dir = Path(args.attachment_dir).expanduser()

    repo = SqliteSessionRepository(db_path)
    store = feedback_service.store_for(repo)
    store.ensure_schema()
    conn = store.connection

    now = "2026-11-04T10:00:00"
    conn.execute(
        """
        INSERT OR REPLACE INTO sessions
            (id, title, state, message_count, snapshot, pending_choice, created_at, updated_at)
        VALUES (?, '反馈预览会话', 'WAITING_USER_CONFIRM', 0, ?, ?, ?, ?)
        """,
        (
            DEMO_SESSION_ID,
            json.dumps(
                {
                    "definition": {
                        "metadata": {"id": "def_feedback_demo", "revision": 1},
                        "regions": [],
                    }
                },
                ensure_ascii=False,
            ),
            json.dumps(MENU, ensure_ascii=False),
            now,
            now,
        ),
    )

    targets = []
    seq = 0
    for index, (reply, state) in enumerate(REPLIES):
        seq += 1
        client_msg_id = f"feedback-demo-{index + 1}:reply"
        conn.execute(
            """
            INSERT OR REPLACE INTO messages
                (session_id, seq, role, content, payload, client_msg_id, created_at)
            VALUES (?, ?, 'assistant', ?, '{}', ?, ?)
            """,
            (DEMO_SESSION_ID, seq, reply, client_msg_id, now),
        )
        message_row = conn.execute(
            "SELECT id FROM messages WHERE session_id = ? AND client_msg_id = ?",
            (DEMO_SESSION_ID, client_msg_id),
        ).fetchone()
        message_id = int(message_row["id"])
        target = store.register_target(
            conn,
            kind="reply",
            dedupe_key_value=dedupe_key(
                "reply", DEMO_SESSION_ID, client_msg_id, fingerprint(reply)
            ),
            session_id=DEMO_SESSION_ID,
            message_id=message_id,
            client_msg_id=client_msg_id,
            reply_fingerprint_value=fingerprint(reply),
            request_excerpt_value="生成 A 机房昨日能耗日报",
            reply_excerpt_value=reply,
            locator={
                "sessionId": DEMO_SESSION_ID,
                "businessState": state,
                "designPhase": "PLAN_WAITING_CONFIRM",
                "backendVersion": "1.0.0",
            },
            missing_items=["draftId", "draftRevision"],
            fact_source="server",
        )
        targets.append(target)

    # 消息体里带上公开目标（历史恢复靠它）+ 这一轮真实的响应片段，
    # 这样回放出来的是一条**像样的**助手回复，而不是空气泡。
    for index, target in enumerate(targets):
        reply, state = REPLIES[index]
        conn.execute(
            "UPDATE messages SET payload = ? WHERE session_id = ? AND client_msg_id = ?",
            (
                json.dumps(
                    {
                        "state": state,
                        "reply": reply,
                        "designPhase": "PLAN_WAITING_CONFIRM",
                        "feedbackTarget": {
                            "id": target["target_id"],
                            "kind": target["kind"],
                            "createdAt": target["created_at"],
                            "available": True,
                        },
                    },
                    ensure_ascii=False,
                ),
                DEMO_SESSION_ID,
                f"feedback-demo-{index + 1}:reply",
            ),
        )

    # 一个停止观察目标（客户端事件那一类）
    stopped = store.register_target(
        conn,
        kind="client_stopped",
        dedupe_key_value=dedupe_key("client_stopped", DEMO_SESSION_ID, "feedback-demo-stop"),
        session_id=DEMO_SESSION_ID,
        client_msg_id="feedback-demo-stop",
        locator={
            "sessionId": DEMO_SESSION_ID,
            "businessState": "ANALYZING",
            "backendVersion": "1.0.0",
            "factSource": "client_observation",
        },
        missing_items=["draftId", "designPhase"],
        fact_source="client_observation",
    )
    conn.commit()

    print(
        json.dumps(
            {
                "sessionId": DEMO_SESSION_ID,
                "replyTargets": [t["target_id"] for t in targets],
                "stoppedTarget": stopped["target_id"],
                "dbPath": str(db_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
