"""反馈后端专项用例（20261104 反馈功能）。

对应 K1 / K4 / K5 三条固定快检：

* **K1 提交、文件、幂等** —— 隔离库里的真实持久化目标；满意度单独提交；
  文字 + 合法小截图；同 cookie / 同目标换 submissionId 或同 ID 重试；
  超大文件与伪类型；不遗留可公开附件。
* **K4 登录、管理与重启持久化** —— 未授权全拒绝；登录后筛选 / 详情 /
  改状态备注 / 导出 CSV；重启自己的隔离服务后数据与附件仍在；CSV 不泄漏
  私有附件与完整聊天；用户输入的公式被中和；退出后不可访问管理数据。
* **K5 保留与安全字段** —— 到期与未到期各一条；执行清理入口；未配置
  维护者时安全关闭；错误密码被拒；快照里没有完整 payload / 模拟数值；
  日志里没有表单正文与凭据正文。

模式：**真实 FastAPI + 隔离 SQLite**。反馈链路不调模型 —— 这里直接
在隔离库里造真实的持久化目标（与登记时同一条写入路径）。
"""
from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.services import feedback_service
from app.services.feedback_service import store_for
from app.services.session_service import session_service
from app.storage.sqlite_store import SqliteSessionRepository


# ---------- 夹具 ----------

@pytest.fixture(autouse=True)
def feedback_env(monkeypatch, tmp_path):
    """反馈功能自己的隔离环境：附件目录、阈值、维护者凭据。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "feedback_attachment_dir", tmp_path / "attachments")
    monkeypatch.setattr(settings, "feedback_max_attachment_bytes", 5 * 1024 * 1024)
    monkeypatch.setattr(settings, "feedback_retention_days", 180)
    monkeypatch.setattr(settings, "feedback_admin_username", "keeper")
    monkeypatch.setattr(settings, "feedback_session_secret", "test-secret-value")
    from app.services.feedback_auth import hash_password

    monkeypatch.setattr(
        settings, "feedback_admin_password_hash", hash_password("keeper-pass")
    )
    return tmp_path


@pytest.fixture()
def fb_client() -> TestClient:
    """loopback 基址的客户端：这样非 Secure 的本地 cookie 才会被带上。"""
    return TestClient(app, base_url="http://127.0.0.1")


# ---------- 图片载荷 ----------

def _png_bytes(width: int = 2, height: int = 2) -> bytes:
    """造一张**合法**的小 PNG（真的能解码，不是假类型）。"""
    raw = b""
    for _ in range(height):
        raw += b"\x00" + b"\xff\x00\x00" * width

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _fake_image_bytes() -> bytes:
    """伪类型：后缀是 .png，内容不是图片。"""
    return b"GIF89a-not-really-an-image" + b"\x00" * 64


# ---------- 造真实的持久化目标 ----------

def _make_target(
    *,
    session_id: str = "sess_fb_1",
    reply: str = "已生成报表草稿，右侧可以直接编辑。",
    message_id: int | None = None,
    state: str = "WAITING_USER_CONFIRM",
) -> dict:
    """在当前隔离库里登记一个**真实持久化**的目标（走登记那条写入路径）。"""
    store = store_for(session_service.repository)
    conn = store.connection
    conn.execute(
        """
        INSERT INTO sessions (id, title, state, message_count, created_at, updated_at)
        VALUES (?, '反馈测试会话', ?, 0, '2026-11-04T10:00:00', '2026-11-04T10:00:00')
        """,
        (session_id, state),
    )
    conn.execute(
        """
        INSERT INTO messages (session_id, seq, role, content, payload, client_msg_id, created_at)
        VALUES (?, 1, 'assistant', ?, '{}', ?, '2026-11-04T10:00:00')
        """,
        (session_id, reply, f"{session_id}:reply"),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM messages WHERE session_id = ? AND client_msg_id = ?",
        (session_id, f"{session_id}:reply"),
    ).fetchone()
    message_id = int(row["id"])
    from app.storage.feedback_store import dedupe_key, fingerprint

    target = store.register_target(
        conn,
        kind="reply",
        dedupe_key_value=dedupe_key("reply", session_id, f"{session_id}:reply", fingerprint(reply)),
        session_id=session_id,
        message_id=message_id,
        client_msg_id=f"{session_id}:reply",
        reply_fingerprint_value=fingerprint(reply),
        request_excerpt_value="生成能耗日报",
        reply_excerpt_value=reply,
        locator={"sessionId": session_id, "businessState": state, "backendVersion": "1.0.0"},
        missing_items=["draftId"],
        fact_source="server",
    )
    conn.commit()
    return target


def _submit(client: TestClient, target_id: str, submission_id: str, **fields):
    data = {"targetId": target_id, "submissionId": submission_id, "rating": "satisfied"}
    data.update(fields)
    return client.post("/api/feedback", data=data)


def _login(client: TestClient, username: str = "keeper", password: str = "keeper-pass"):
    return client.post(
        "/api/feedback/admin/login", json={"username": username, "password": password}
    )


# =====================================================================
# K1 —— 提交、文件、幂等
# =====================================================================

def test_k1_rating_only_submission_persists_once(fb_client):
    """满意度单独提交：落库、编号可取、同目标只有一行。"""
    target = _make_target()
    target_id = target["target_id"]

    first = _submit(fb_client, target_id, "sub-1")
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["submitted"] is True
    assert body["feedbackId"].startswith("FB-")

    store = store_for(session_service.repository)
    rows = store.connection.execute(
        "SELECT COUNT(*) FROM feedback_items WHERE target_id = ?", (target_id,)
    ).fetchone()[0]
    assert rows == 1


def test_k1_same_target_retry_returns_existing_number(fb_client):
    """同 cookie / 同目标换 submissionId、或同 ID 重试 → 不重复插入。"""
    target = _make_target()
    target_id = target["target_id"]

    first = _submit(fb_client, target_id, "sub-a")
    assert first.status_code == 201
    feedback_id = first.json()["feedbackId"]

    retry_same_id = _submit(fb_client, target_id, "sub-a")
    assert retry_same_id.status_code == 200
    assert retry_same_id.json()["feedbackId"] == feedback_id

    retry_other_id = _submit(fb_client, target_id, "sub-b")
    assert retry_other_id.status_code == 200
    assert retry_other_id.json()["feedbackId"] == feedback_id

    store = store_for(session_service.repository)
    rows = store.connection.execute(
        "SELECT COUNT(*) FROM feedback_items WHERE target_id = ?", (target_id,)
    ).fetchone()[0]
    assert rows == 1


def test_k1_comment_and_legal_screenshot(fb_client):
    """带文字与合法截图提交：附件私有保存，管理端可读。"""
    target = _make_target(session_id="sess_fb_shot")
    target_id = target["target_id"]

    response = fb_client.post(
        "/api/feedback",
        data={"targetId": target_id, "submissionId": "sub-shot", "rating": "dissatisfied",
              "reason": "wrong_result", "comment": "草稿里的图不对", "contact": "陈工"},
        files={"screenshot": ("shot.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 201, response.text
    feedback_id = response.json()["feedbackId"]

    assert _login(fb_client).status_code == 200
    detail = fb_client.get(f"/api/feedback/admin/items/{feedback_id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["comment"] == "草稿里的图不对"
    assert payload["contact"] == "陈工"
    assert payload["reason"] == "wrong_result"
    assert payload["hasAttachment"] is True

    attachment = fb_client.get(f"/api/feedback/admin/items/{feedback_id}/attachment")
    assert attachment.status_code == 200
    assert attachment.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_k1_oversized_and_fake_type_rejected(fb_client):
    """超限 → 413；伪类型 → 422；失败不留可公开附件。"""
    target = _make_target(session_id="sess_fb_bad")
    target_id = target["target_id"]

    oversized = fb_client.post(
        "/api/feedback",
        data={"targetId": target_id, "submissionId": "sub-big", "rating": "neutral"},
        files={"screenshot": ("big.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024 + 10), "image/png")},
    )
    assert oversized.status_code == 413, oversized.text

    fake = fb_client.post(
        "/api/feedback",
        data={"targetId": target_id, "submissionId": "sub-fake", "rating": "neutral"},
        files={"screenshot": ("fake.png", _fake_image_bytes(), "image/png")},
    )
    assert fake.status_code == 422, fake.text

    store = store_for(session_service.repository)
    rows = store.connection.execute(
        "SELECT COUNT(*) FROM feedback_items WHERE target_id = ?", (target_id,)
    ).fetchone()[0]
    assert rows == 0

    attachments = store_for(session_service.repository)
    count = attachments.connection.execute(
        "SELECT COUNT(*) FROM feedback_attachments"
    ).fetchone()[0]
    assert count == 0


def test_k1_unknown_target_is_404(fb_client):
    """目标不存在 → 404（前端据此保留表单）。"""
    response = _submit(fb_client, "fbt_does_not_exist", "sub-x")
    assert response.status_code == 404


def test_k1_submit_rate_limit_returns_429(fb_client, monkeypatch):
    """提交节流 → 429（前端据此保留输入）。"""
    from app.routers import feedback as feedback_router

    monkeypatch.setattr(feedback_router._throttle, "_limit", 1)
    target = _make_target(session_id="sess_fb_rate")
    target_id = target["target_id"]
    assert _submit(fb_client, target_id, "rate-1").status_code == 201
    assert _submit(fb_client, target_id, "rate-2").status_code == 429


def test_k1_status_endpoint_only_returns_own_submissions(fb_client):
    """已提交标记只返回**自己**的。"""
    target = _make_target(session_id="sess_fb_status")
    target_id = target["target_id"]
    assert _submit(fb_client, target_id, "st-1").status_code == 201

    mine = fb_client.post("/api/feedback/status", json={"targetIds": [target_id]})
    assert mine.status_code == 200
    assert target_id in mine.json()["submitted"]
    # 只返回标记，不返回意见 / 署名 / 附件
    assert set(mine.json().keys()) == {"submitted"}

    other = TestClient(app, base_url="http://127.0.0.1")
    theirs = other.post("/api/feedback/status", json={"targetIds": [target_id]})
    assert theirs.status_code == 200
    assert theirs.json()["submitted"] == {}


# =====================================================================
# K4 —— 登录、管理与重启持久化
# =====================================================================

def test_k4_admin_endpoints_require_login(fb_client):
    """未登录：列表 / 详情 / 附件 / 导出 / PATCH 全部拒绝。"""
    target = _make_target(session_id="sess_fb_admin")
    target_id = target["target_id"]
    assert _submit(fb_client, target_id, "adm-1").status_code == 201

    assert fb_client.get("/api/feedback/admin/items").status_code == 401
    assert fb_client.get("/api/feedback/admin/items/FB-XXXX").status_code == 401
    assert fb_client.get("/api/feedback/admin/items/FB-XXXX/attachment").status_code == 401
    assert fb_client.get("/api/feedback/admin/export").status_code == 401
    assert fb_client.patch("/api/feedback/admin/items/FB-XXXX", json={"status": "resolved"}).status_code == 401
    assert fb_client.delete("/api/feedback/admin/items/FB-XXXX").status_code == 401


def test_k4_admin_full_chain(fb_client):
    """登录后：筛选 / 详情 / 改状态备注 / 导出 CSV 全可用。"""
    target = _make_target(session_id="sess_fb_chain")
    target_id = target["target_id"]
    submitted = _submit(
        fb_client, target_id, "chain-1", rating="dissatisfied", comment="不对"
    )
    assert submitted.status_code == 201
    feedback_id = submitted.json()["feedbackId"]

    assert _login(fb_client).status_code == 200

    listed = fb_client.get("/api/feedback/admin/items?rating=dissatisfied")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["feedbackId"] == feedback_id

    filtered_out = fb_client.get("/api/feedback/admin/items?rating=satisfied")
    assert filtered_out.json()["total"] == 0

    patched = fb_client.patch(
        f"/api/feedback/admin/items/{feedback_id}",
        json={"status": "in_progress", "note": "已复现，正在修"},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "in_progress"
    assert patched.json()["note"] == "已复现，正在修"

    exported = fb_client.get("/api/feedback/admin/export")
    assert exported.status_code == 200
    assert "text/csv" in exported.headers["content-type"]
    assert feedback_id in exported.text
    assert "不对" in exported.text


# =====================================================================
# K4-删除 —— 收件箱删除一条反馈
# =====================================================================

def test_k4_admin_delete_removes_feedback_and_attachment(fb_client, tmp_path, monkeypatch):
    """删除：记录没了、私有截图文件也没了；**目标与会话不动**。"""
    settings = get_settings()
    attachments = tmp_path / "attachments"
    monkeypatch.setattr(settings, "feedback_attachment_dir", attachments)

    target = _make_target(session_id="sess_fb_del")
    target_id = target["target_id"]
    submitted = fb_client.post(
        "/api/feedback",
        data={"targetId": target_id, "submissionId": "del-1", "rating": "dissatisfied",
              "comment": "这条要删掉", "contact": "陈工"},
        files={"screenshot": ("shot.png", _png_bytes(), "image/png")},
    )
    assert submitted.status_code == 201, submitted.text
    feedback_id = submitted.json()["feedbackId"]

    stored = list(attachments.iterdir()) if attachments.is_dir() else []
    assert len(stored) == 1, stored

    assert _login(fb_client).status_code == 200
    assert fb_client.get("/api/feedback/admin/items").json()["total"] == 1

    deleted = fb_client.delete(f"/api/feedback/admin/items/{feedback_id}")
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["ok"] is True
    assert body["feedbackId"] == feedback_id
    assert body["attachmentRemoved"] is True

    # 记录没了
    assert fb_client.get(f"/api/feedback/admin/items/{feedback_id}").status_code == 404
    assert fb_client.get("/api/feedback/admin/items").json()["total"] == 0
    # 私有截图文件也删了（不留半截文件）
    assert not stored[0].exists()
    assert list(attachments.iterdir()) == []

    # **目标不动**：那一轮的评价入口还在
    store = store_for(session_service.repository)
    assert store.get_target(target_id) is not None
    # 会话与消息不动
    assert store.connection.execute(
        "SELECT COUNT(*) FROM sessions WHERE id = ?", ("sess_fb_del",)
    ).fetchone()[0] == 1
    assert store.connection.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", ("sess_fb_del",)
    ).fetchone()[0] == 1


def test_k4_admin_delete_unknown_is_404(fb_client):
    """删一条不存在的 → 404（重复删也不会静默成功）。"""
    assert _login(fb_client).status_code == 200
    assert fb_client.delete("/api/feedback/admin/items/FB-NOT-EXIST").status_code == 404


def test_k4_admin_delete_keeps_other_feedback(fb_client):
    """删一条**不动**另一条。"""
    target_a = _make_target(session_id="sess_fb_del_a", reply="第一条回复")
    target_b = _make_target(session_id="sess_fb_del_b", reply="第二条回复")
    first = _submit(fb_client, target_a["target_id"], "keep-a")
    second = _submit(fb_client, target_b["target_id"], "keep-b")
    assert first.status_code == 201 and second.status_code == 201

    assert _login(fb_client).status_code == 200
    assert fb_client.get("/api/feedback/admin/items").json()["total"] == 2

    removed = fb_client.delete(f"/api/feedback/admin/items/{first.json()['feedbackId']}")
    assert removed.status_code == 200

    listed = fb_client.get("/api/feedback/admin/items").json()
    assert listed["total"] == 1
    assert listed["items"][0]["feedbackId"] == second.json()["feedbackId"]


def test_k4_admin_delete_clears_visitor_submitted_mark(fb_client):
    """删掉后，用户那边的「已反馈」标记随之消失（记录没了）。"""
    target = _make_target(session_id="sess_fb_del_mark")
    target_id = target["target_id"]
    submitted = _submit(fb_client, target_id, "mark-1")
    assert submitted.status_code == 201

    marked = fb_client.post("/api/feedback/status", json={"targetIds": [target_id]})
    assert marked.status_code == 200
    assert target_id in marked.json()["submitted"]

    assert _login(fb_client).status_code == 200
    deleted = fb_client.delete(f"/api/feedback/admin/items/{submitted.json()['feedbackId']}")
    assert deleted.status_code == 200

    after = fb_client.post("/api/feedback/status", json={"targetIds": [target_id]})
    assert after.status_code == 200
    assert after.json()["submitted"] == {}


def test_k4_logout_blocks_admin_access(fb_client):
    target = _make_target(session_id="sess_fb_logout")
    assert _submit(fb_client, target["target_id"], "lo-1").status_code == 201
    assert _login(fb_client).status_code == 200
    assert fb_client.get("/api/feedback/admin/items").status_code == 200

    assert fb_client.post("/api/feedback/admin/logout").status_code == 200
    assert fb_client.get("/api/feedback/admin/items").status_code == 401


def test_k4_data_and_attachment_survive_restart(tmp_path, monkeypatch, fb_client):
    """重启自己的隔离服务（同库）后：数据与附件仍在。"""
    settings = get_settings()
    attachments = tmp_path / "attachments"
    monkeypatch.setattr(settings, "feedback_attachment_dir", attachments)

    target = _make_target(session_id="sess_fb_restart")
    target_id = target["target_id"]
    submitted = fb_client.post(
        "/api/feedback",
        data={"targetId": target_id, "submissionId": "rs-1", "rating": "satisfied"},
        files={"screenshot": ("shot.png", _png_bytes(), "image/png")},
    )
    assert submitted.status_code == 201
    feedback_id = submitted.json()["feedbackId"]

    # 「重启」：换一个仓库实例连**同一个库文件**。
    db_path = session_service.repository.db_path
    restarted = SqliteSessionRepository(db_path)
    monkeypatch.setattr(session_service, "_repo", restarted)
    feedback_service._store = None
    feedback_service._store_repository = None
    feedback_service.init_feedback_storage(restarted)

    after = TestClient(app, base_url="http://127.0.0.1")
    assert _login(after).status_code == 200
    detail = after.get(f"/api/feedback/admin/items/{feedback_id}")
    assert detail.status_code == 200
    assert detail.json()["hasAttachment"] is True
    attachment = after.get(f"/api/feedback/admin/items/{feedback_id}/attachment")
    assert attachment.status_code == 200
    assert len(attachment.content) > 0


def test_k4_csv_neutralizes_formula_and_hides_private_data(fb_client):
    """CSV：公式被中和；不带截图与完整聊天。"""
    target = _make_target(session_id="sess_fb_csv", reply="很长的最终回复正文" * 40)
    target_id = target["target_id"]
    submitted = fb_client.post(
        "/api/feedback",
        data={"targetId": target_id, "submissionId": "csv-1", "rating": "neutral",
              "comment": "=SUM(A1:A9)", "contact": "+1-555"},
        files={"screenshot": ("shot.png", _png_bytes(), "image/png")},
    )
    assert submitted.status_code == 201
    assert _login(fb_client).status_code == 200

    exported = fb_client.get("/api/feedback/admin/export")
    assert exported.status_code == 200
    text = exported.text
    # 公式被中和：不会以 = / + 开头
    assert "'=SUM(A1:A9)" in text
    assert "'+1-555" in text
    # 不带截图内容（PNG 魔数）、也不带整段聊天正文
    assert "\x89PNG" not in text
    assert "很长的最终回复正文" * 40 not in text


# =====================================================================
# K5 —— 保留与安全字段
# =====================================================================

def test_k5_cleanup_only_removes_expired_feedback(fb_client, tmp_path, monkeypatch):
    """到期清理：只清到期反馈与它引用的附件；未到期、会话、草稿不动。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "feedback_attachment_dir", tmp_path / "attachments")

    expired = _make_target(session_id="sess_fb_expired")
    fresh = _make_target(session_id="sess_fb_fresh", reply="还没过期的那条")

    expired_sub = fb_client.post(
        "/api/feedback",
        data={"targetId": expired["target_id"], "submissionId": "exp-1", "rating": "satisfied"},
        files={"screenshot": ("shot.png", _png_bytes(), "image/png")},
    )
    fresh_sub = fb_client.post(
        "/api/feedback",
        data={"targetId": fresh["target_id"], "submissionId": "fresh-1", "rating": "satisfied"},
        files={"screenshot": ("shot.png", _png_bytes(), "image/png")},
    )
    assert expired_sub.status_code == 201
    assert fresh_sub.status_code == 201

    store = store_for(session_service.repository)
    # 把这一条挪到 180 天之前（模拟到期）
    store.connection.execute(
        "UPDATE feedback_items SET expires_at = '2020-01-01T00:00:00' WHERE feedback_id = ?",
        (expired_sub.json()["feedbackId"],),
    )
    store.connection.execute(
        "UPDATE feedback_targets SET expires_at = '2020-01-01T00:00:00' WHERE target_id = ?",
        (expired["target_id"],),
    )
    store.connection.commit()

    attachments_dir = Path(settings.feedback_attachment_dir)
    before_files = sorted(p.name for p in attachments_dir.iterdir())
    assert len(before_files) == 2

    result = feedback_service.cleanup_expired(
        session_service.repository, settings=settings
    )
    assert result["feedbacks"] == 1
    assert result["attachments"] == 1

    remaining = store.connection.execute(
        "SELECT COUNT(*) FROM feedback_items"
    ).fetchone()[0]
    assert remaining == 1
    after_files = sorted(p.name for p in attachments_dir.iterdir())
    assert len(after_files) == 1

    # 会话与草稿**没被动过**
    sessions = store.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert sessions == 2
    messages = store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert messages == 2


def test_k5_admin_login_safely_closed_when_not_configured(fb_client, monkeypatch):
    """没配置维护者凭据 → 管理登录**安全关闭**（503），不是放行。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "feedback_admin_username", "")
    monkeypatch.setattr(settings, "feedback_admin_password_hash", "")
    monkeypatch.setattr(settings, "feedback_session_secret", "")

    response = _login(fb_client)
    assert response.status_code == 503
    assert fb_client.get("/api/feedback/admin/items").status_code == 503


def test_k5_wrong_password_rejected(fb_client):
    """错误密码被拒（401），不泄露任何信息。"""
    response = _login(fb_client, password="wrong")
    assert response.status_code == 401
    assert "密码" in response.json()["detail"]


def test_k5_snapshot_has_no_full_payload_or_simulated_values(fb_client):
    """定位快照只含结构性信息：没有完整 payload / JSON / 模拟数值。"""
    target = _make_target(session_id="sess_fb_snapshot", reply="回复正文")
    target_id = target["target_id"]
    submitted = _submit(fb_client, target_id, "snap-1")
    feedback_id = submitted.json()["feedbackId"]

    assert _login(fb_client).status_code == 200
    detail = fb_client.get(f"/api/feedback/admin/items/{feedback_id}").json()
    locator = detail["locator"]
    assert set(locator) <= {
        "sessionId", "messageId", "messageSeq", "clientMsgId", "turnIndex", "attempt",
        "businessState", "designPhase", "planId", "planRevision", "draftId",
        "draftRevision", "errorCode", "diagnosticRef", "frontendVersion",
        "backendVersion", "factSource",
    }
    serialized = str(detail)
    # 快照里不带整段响应、原始 JSON、checkpoint
    assert "reportPreviewPayload" not in serialized
    assert "checkpoint" not in serialized
    assert "domainObjects" not in serialized


def test_k5_logs_do_not_contain_form_body_or_credentials(fb_client, caplog):
    """普通日志里没有表单正文与凭据正文。"""
    target = _make_target(session_id="sess_fb_logs")
    secret_comment = "秘密内容-不应出现在日志里-9f3a"
    submitted = fb_client.post(
        "/api/feedback",
        data={"targetId": target["target_id"], "submissionId": "log-1",
              "rating": "dissatisfied", "comment": secret_comment,
              "contact": "someone@example.com"},
    )
    assert submitted.status_code == 201
    assert _login(fb_client, password="keeper-pass").status_code == 200
    fb_client.get("/api/feedback/admin/items")

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_comment not in text
    assert "keeper-pass" not in text
    assert "someone@example.com" not in text
