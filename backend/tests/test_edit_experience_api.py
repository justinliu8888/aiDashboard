"""编辑经验后端专项用例（契约 0003 §3）。

覆盖的硬约束：

* **准入**：待审 / 拒绝 / 禁用 → 一律检索不到；只有管理员批准且未禁用的
  经验可检索。
* **权限**：普通用户直接调审批接口 → **401**；管理员未配置 → **503**；
  跨源写 → **403**；三种拒绝**都不改变数据**。
* **可移植**：批准的方法里**没有旧区块 ID、没有凭证**。
* **幂等**：同一 ``planId`` 重复归档 → 同一个 recordId，记录数不增。
* **持久化**：重建库连接 → 状态读回一致。
* **无匹配** → 返回**正常空结果**（不是错误）。

模式：**真实 FastAPI 路由 + 隔离 SQLite**（测试库由 ``conftest.py`` 按用例
隔离；``AI_SESSION_DB_PATH`` 指临时库）。
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.services import edit_experience_service
from app.services.feedback_auth import hash_password
from app.services.session_service import session_service
from app.storage.sqlite_store import SqliteSessionRepository


# ---------- 夹具 ----------

@pytest.fixture(autouse=True)
def experience_env(monkeypatch, tmp_path):
    """隔离环境：管理员凭据 + 建表（**事务外**）。

    建表是 DDL，必须在事务外做 —— 夹具正是事务外。
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "feedback_admin_username", "keeper")
    monkeypatch.setattr(settings, "feedback_session_secret", "exp-secret-value")
    monkeypatch.setattr(
        settings, "feedback_admin_password_hash", hash_password("keeper-pass")
    )

    from app.services.edit_experience_service import init_edit_experience_storage

    init_edit_experience_storage(session_service.repository)
    return tmp_path


@pytest.fixture()
def exp_client() -> TestClient:
    """loopback 基址的客户端：这样非 Secure 的本地 cookie 才会被带上。"""
    from app.routers import edit_experiences, feedback

    test_app = FastAPI()
    # 复用反馈收件箱的登录入口（同一套 ``fb_admin`` cookie 与配置）。
    test_app.include_router(feedback.router)
    test_app.include_router(edit_experiences.router)
    return TestClient(test_app, base_url="http://127.0.0.1")


def _login(client: TestClient) -> Any:
    """走**现有**的管理员登录入口（不另建密码体系）。"""
    return client.post(
        "/api/feedback/admin/login",
        json={"username": "keeper", "password": "keeper-pass"},
    )


# ---------- 造数据 ----------

#: 别人的草稿里的具体区块 ID（**绝不能**进经验）。
OLD_REGION_ID = "region_old_9"

OPERATIONS = [
    {
        "op": "update_region",
        "regionId": OLD_REGION_ID,
        "params": {"metric": "energy", "compare": "yoy"},
    }
]


def _archive(plan_id: str = "plan_exp_1", **kwargs: Any) -> str:
    """归档一条待审记录（等价于「用户接受 + 正式事务成功」）。"""
    payload: dict[str, Any] = {
        "session_id": "sess_exp_1",
        "draft_id": "draft_exp_1",
        "plan_id": plan_id,
        "item_id": "item_exp_1",
        "original_request": "把机器房能耗区块改成同比对比",
        "supplement": "",
        "operations": OPERATIONS,
        "result": {"applied": True, "checked": True},
        "capability_version": "cap_v1",
    }
    payload.update(kwargs)
    return edit_experience_service.record_pending_edit(**payload)


# =====================================================================
# 准入：只有「管理员批准且未禁用」才检索得到
# =====================================================================

def test_pending_record_is_not_searchable():
    """待审记录**检索不到**（还没批准）。"""
    _archive(plan_id="plan_pending")

    matches = edit_experience_service.search_experiences(query="能耗 同比")
    assert matches == []


def test_rejected_record_is_not_searchable(exp_client):
    """被拒绝的记录**检索不到**。"""
    record_id = _archive(plan_id="plan_rejected")
    assert _login(exp_client).status_code == 200

    rejected = exp_client.post(
        f"/api/edit-experiences/admin/records/{record_id}/reject",
        json={"reason": "要求太特殊，不通用", "expectedVersion": 1},
    )
    assert rejected.status_code == 200

    assert edit_experience_service.search_experiences(query="能耗 同比") == []


def test_approved_experience_is_searchable(exp_client):
    """管理员批准后 → 检索得到方法。"""
    record_id = _archive(plan_id="plan_approve")
    assert _login(exp_client).status_code == 200

    approved = exp_client.post(
        f"/api/edit-experiences/admin/records/{record_id}/approve",
        json={
            "method": "把目标区块改成同比口径：先查指标，再改时间粒度",
            "conditions": "能耗类指标 + 有同比基准",
            "expectedVersion": 1,
        },
    )
    assert approved.status_code == 200, approved.text

    matches = edit_experience_service.search_experiences(query="能耗 同比")
    assert len(matches) == 1
    assert matches[0]["id"].startswith("exp_")
    assert matches[0]["recordId"] == record_id
    assert matches[0]["score"] > 0
    # 返回体只有约定的几个字段
    assert set(matches[0].keys()) == {
        "id",
        "method",
        "conditions",
        "capabilityVersion",
        "score",
        "recordId",
    }


def test_disabled_experience_is_not_returned(exp_client):
    """禁用后 → 检索**不再返回**（但管理列表里还看得到）。"""
    record_id = _archive(plan_id="plan_disable")
    assert _login(exp_client).status_code == 200
    approved = exp_client.post(
        f"/api/edit-experiences/admin/records/{record_id}/approve",
        json={"method": "同比口径改造", "conditions": "能耗", "expectedVersion": 1},
    )
    experience_id = approved.json()["experience"]["experienceId"]

    assert edit_experience_service.search_experiences(query="同比 口径") != []

    patched = exp_client.patch(
        f"/api/edit-experiences/admin/experiences/{experience_id}",
        json={"disabled": True, "expectedVersion": 1},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["experience"]["status"] == "disabled"

    assert edit_experience_service.search_experiences(query="同比 口径") == []

    listed = exp_client.get(
        "/api/edit-experiences/admin/experiences?includeDisabled=true"
    )
    assert listed.status_code == 200
    assert any(
        item["experienceId"] == experience_id for item in listed.json()["items"]
    )


# =====================================================================
# 权限：普通用户不能审批；401 / 503 / 403 语义准确且不改数据
# =====================================================================

def test_anonymous_approve_is_401_and_changes_nothing(exp_client):
    """未登录直接调审批 → **401**，记录不变、没生出经验。"""
    record_id = _archive(plan_id="plan_401")

    response = exp_client.post(
        f"/api/edit-experiences/admin/records/{record_id}/approve",
        json={"method": "同比口径改造", "conditions": "", "expectedVersion": 1},
    )
    assert response.status_code == 401

    record = edit_experience_service.get_record(record_id)
    assert record["status"] == "pending"
    assert record["version"] == 1
    assert edit_experience_service.list_experiences() == []
    assert edit_experience_service.search_experiences(query="同比") == []


def test_anonymous_list_is_401(exp_client):
    """未登录读管理列表 → **401**。"""
    assert exp_client.get("/api/edit-experiences/admin/records").status_code == 401
    assert exp_client.get("/api/edit-experiences/admin/experiences").status_code == 401


def test_not_configured_admin_is_503_and_changes_nothing(exp_client, monkeypatch):
    """管理员未配置 → **503**（安全关闭，不是放行），数据不变。"""
    record_id = _archive(plan_id="plan_503")
    settings = get_settings()
    monkeypatch.setattr(settings, "feedback_admin_username", "")
    monkeypatch.setattr(settings, "feedback_admin_password_hash", "")
    monkeypatch.setattr(settings, "feedback_session_secret", "")

    assert exp_client.get("/api/edit-experiences/admin/records").status_code == 503
    response = exp_client.post(
        f"/api/edit-experiences/admin/records/{record_id}/approve",
        json={"method": "同比口径改造", "conditions": "", "expectedVersion": 1},
    )
    assert response.status_code == 503

    record = edit_experience_service.get_record(record_id)
    assert record["status"] == "pending"
    assert edit_experience_service.list_experiences() == []


def test_cross_origin_write_is_403_and_changes_nothing(exp_client):
    """跨源写请求 → **403**，数据不变。"""
    record_id = _archive(plan_id="plan_403")
    assert _login(exp_client).status_code == 200

    response = exp_client.post(
        f"/api/edit-experiences/admin/records/{record_id}/approve",
        json={"method": "同比口径改造", "conditions": "", "expectedVersion": 1},
        headers={"origin": "https://evil.example.com"},
    )
    assert response.status_code == 403

    record = edit_experience_service.get_record(record_id)
    assert record["status"] == "pending"
    assert edit_experience_service.list_experiences() == []


# =====================================================================
# 乐观锁：版本不符 → 拒绝，数据不变
# =====================================================================

def test_version_mismatch_is_rejected(exp_client):
    """``expectedVersion`` 不符 → **409**，记录不变。"""
    record_id = _archive(plan_id="plan_version")
    assert _login(exp_client).status_code == 200

    response = exp_client.post(
        f"/api/edit-experiences/admin/records/{record_id}/approve",
        json={"method": "同比口径改造", "conditions": "", "expectedVersion": 99},
    )
    assert response.status_code == 409

    record = edit_experience_service.get_record(record_id)
    assert record["status"] == "pending"
    assert record["version"] == 1
    assert edit_experience_service.list_experiences() == []


def test_experience_version_mismatch_is_rejected(exp_client):
    """整理经验时版本不符 → **409**，方法不变。"""
    record_id = _archive(plan_id="plan_experience_version")
    assert _login(exp_client).status_code == 200
    approved = exp_client.post(
        f"/api/edit-experiences/admin/records/{record_id}/approve",
        json={"method": "同比口径改造", "conditions": "", "expectedVersion": 1},
    )
    experience_id = approved.json()["experience"]["experienceId"]

    response = exp_client.patch(
        f"/api/edit-experiences/admin/experiences/{experience_id}",
        json={"method": "改了但不该生效", "expectedVersion": 42},
    )
    assert response.status_code == 409

    kept = exp_client.get("/api/edit-experiences/admin/experiences").json()["items"][0]
    assert kept["method"] == "同比口径改造"
    assert kept["version"] == 1


# =====================================================================
# 可移植：没有旧区块 ID、没有凭证
# =====================================================================

def test_approved_method_is_portable(exp_client):
    """批准的经验：**没有旧区块 ID、没有凭证**。"""
    record_id = _archive(plan_id="plan_portable")
    assert _login(exp_client).status_code == 200

    approved = exp_client.post(
        f"/api/edit-experiences/admin/records/{record_id}/approve",
        json={
            "method": f"把 {OLD_REGION_ID} 改成同比口径，password=hunter2",
            "conditions": f"适用于 {OLD_REGION_ID} 这类能耗区块",
            "expectedVersion": 1,
        },
    )
    assert approved.status_code == 200, approved.text
    experience = approved.json()["experience"]

    assert OLD_REGION_ID not in experience["method"]
    assert OLD_REGION_ID not in experience["conditions"]
    assert "hunter2" not in experience["method"]
    assert "targetRegion" in experience["method"]

    # 记录里的操作清单同样被换成了角色（不落具体 ID）
    record = edit_experience_service.get_record(record_id)
    assert OLD_REGION_ID not in str(record["operations"])
    assert record["operations"][0]["regionId"] == "targetRegion"


def test_bind_experience_to_target_rebinds_roles():
    """复用时把角色绑到**当前**目标上。"""
    bound = edit_experience_service.bind_experience_to_target(
        {
            "id": "exp_x",
            "method": "把 targetRegion 改成同比口径，并新增 newBlock 展示趋势",
            "conditions": "targetRegion 是能耗区块时",
        },
        {"targetRegionId": "region_current_1"},
    )
    assert "region_current_1" in bound["method"]
    assert "targetRegion" not in bound["method"]
    # 没给 newBlock → 保留角色词（那仍是合法写法）
    assert "newBlock" in bound["method"]


# =====================================================================
# 幂等与持久化
# =====================================================================

def test_duplicate_archive_is_idempotent():
    """同一 ``planId`` 重复归档 → 同一个 recordId，记录数不增。"""
    first = _archive(plan_id="plan_dup")
    second = _archive(plan_id="plan_dup")

    assert first == second
    assert edit_experience_service.count_records() == 1
    records = edit_experience_service.list_records()
    assert len(records) == 1
    assert records[0]["planId"] == "plan_dup"


def test_state_readback_after_reconnect(monkeypatch, tmp_path):
    """重建库连接 → 状态读回一致。"""
    db_path = tmp_path / "exp_reconnect.db"
    repo = SqliteSessionRepository(db_path)
    monkeypatch.setattr(session_service, "_repo", repo)
    edit_experience_service.init_edit_experience_storage(repo)

    record_id = _archive(plan_id="plan_reconnect")

    # 模拟重启：换个连接对象读同一个库
    repo2 = SqliteSessionRepository(db_path)
    monkeypatch.setattr(session_service, "_repo", repo2)
    edit_experience_service.init_edit_experience_storage(repo2)

    record = edit_experience_service.get_record(record_id)
    assert record is not None
    assert record["status"] == "pending"
    assert record["planId"] == "plan_reconnect"
    assert edit_experience_service.count_records() == 1


def test_cancelled_task_is_not_archived():
    """已停止 / 取消的任务 → **不得迟到晋升**（不归档）。"""
    result = edit_experience_service.record_pending_edit(
        session_id="sess_exp_1",
        draft_id="draft_exp_1",
        plan_id="plan_cancelled",
        item_id="item_exp_1",
        original_request="把机器房能耗区块改成同比对比",
        supplement="",
        operations=OPERATIONS,
        result={"applied": True},
        capability_version="cap_v1",
        task_status="CANCELLED",
    )
    assert result == ""
    assert edit_experience_service.count_records() == 0


# =====================================================================
# 检索：无匹配 → 正常空结果
# =====================================================================

def test_no_match_returns_empty_result():
    """无匹配 → **空列表**（正常继续，不是错误）。"""
    _archive(plan_id="plan_nomatch")
    assert edit_experience_service.search_experiences(query="量子纠缠态制备") == []


def test_tool_returns_empty_result_when_no_match():
    """检索工具：无匹配 → 空结果 + 说明。"""
    from app.tools import search_edit_experience_tool

    result = search_edit_experience_tool.run(query="量子纠缠态制备")
    assert result["matches"] == []
    assert result["count"] == 0
    assert result["status"] == "empty"
    assert "继续正常分析" in result["message"]
    assert "error" not in result


def test_tool_returns_matches(exp_client):
    """检索工具：批准后能取到方法。"""
    from app.tools import search_edit_experience_tool

    record_id = _archive(plan_id="plan_tool")
    assert _login(exp_client).status_code == 200
    exp_client.post(
        f"/api/edit-experiences/admin/records/{record_id}/approve",
        json={"method": "同比口径改造步骤", "conditions": "能耗类指标", "expectedVersion": 1},
    )

    result = search_edit_experience_tool.run(query="能耗 同比")
    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["matches"][0]["method"] == "同比口径改造步骤"


def test_tool_reports_technical_error(monkeypatch):
    """技术错误 → **明确 error**，不冒充「没有相似案例」。"""
    from app.tools import search_edit_experience_tool

    def _boom(**_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("数据库不可用")

    monkeypatch.setattr(edit_experience_service, "search_experiences", _boom)

    result = search_edit_experience_tool.run(query="能耗")
    assert result["status"] == "error"
    assert result["error"]
    assert result["count"] == 0
    assert result["matches"] == []


# =====================================================================
# 管理列表
# =====================================================================

def test_admin_records_list_supports_status_filter(exp_client):
    """记录列表：按状态过滤 + 分页。"""
    pending = _archive(plan_id="plan_list_pending")
    rejected = _archive(plan_id="plan_list_rejected")
    assert _login(exp_client).status_code == 200
    exp_client.post(
        f"/api/edit-experiences/admin/records/{rejected}/reject",
        json={"reason": "不通用", "expectedVersion": 1},
    )

    everything = exp_client.get("/api/edit-experiences/admin/records")
    assert everything.status_code == 200
    body = everything.json()
    assert body["total"] == 2
    assert {item["recordId"] for item in body["items"]} == {pending, rejected}

    only_pending = exp_client.get(
        "/api/edit-experiences/admin/records?status=pending"
    )
    assert only_pending.json()["total"] == 1
    assert only_pending.json()["items"][0]["recordId"] == pending

    detail = exp_client.get(f"/api/edit-experiences/admin/records/{pending}")
    assert detail.status_code == 200
    assert detail.json()["recordId"] == pending
    assert detail.json()["status"] == "pending"

    missing = exp_client.get("/api/edit-experiences/admin/records/rec_missing")
    assert missing.status_code == 404
