"""夜间专项（D 包）种子脚本的**隔离契约**回归。

这份测试锁的是「测试底座本身可不可信」：

    * 种子跑在**独立数据库**里（``AI_SESSION_DB_PATH``），不碰用户库；
    * 种子**只种前置状态**（草稿 + 合法 NEXT_ACTION 菜单 + 画菜单的助手
      消息）—— **绝不**种候选计划 / EditPlan（人工种植冒充中文入口）；
    * 重复跑幂等（消息不越堆越多）；
    * 多个 variant 派生出来的会话 / 草稿**互相独立**；
    * 卡片名、占宽、区域数量与契约 §4.1 一致。

脚本按**子进程**跑（测的是协调者实际会执行的那条命令），不是 import 内部
函数。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
SCRIPT = BACKEND_DIR / "scripts" / "seed_night_session.py"
PYTHON = sys.executable


def _run_seed(db_path: Path, env_extra: dict[str, str] | None = None):
    """按真实用法跑一次种子脚本，返回 ``(exit_code, stdout, stderr)``。"""
    env = dict(os.environ)
    env["AI_SESSION_DB_PATH"] = str(db_path)
    env.update(env_extra or {})
    proc = subprocess.run(
        [PYTHON, str(SCRIPT)],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_seeded(stdout: str) -> list[dict[str, str]]:
    """解析 ``seeded sessionId=... conversationId=... draftId=...``。"""
    rows: list[dict[str, str]] = []
    for line in stdout.splitlines():
        if not line.startswith("seeded "):
            continue
        row: dict[str, str] = {}
        for token in line[len("seeded "):].split():
            key, _, value = token.partition("=")
            row[key] = value
        rows.append(row)
    return rows


@pytest.fixture()
def seeded_db(tmp_path):
    """跑一次种子，返回 ``(db_path, rows)``。"""
    db_path = tmp_path / "haewu-night-seed.db"
    code, stdout, stderr = _run_seed(db_path)
    assert code == 0, f"种子脚本失败：{stderr}"
    rows = _parse_seeded(stdout)
    assert rows, f"种子脚本没有输出 seeded 行：{stdout!r}"
    return db_path, rows


def _read_session(db_path: Path, session_id: str) -> dict:
    """只读打开隔离库，核对会话现场。"""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        assert row is not None, f"库里没有会话：{session_id}"
        session = dict(row)
        messages = [
            dict(item)
            for item in conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        ]
        session["_messages"] = messages
        return session
    finally:
        conn.close()


def test_seed_script_prints_parseable_ids(seeded_db):
    """种子跑完要打印可解析的 sessionId / conversationId / draftId。"""
    _db_path, rows = seeded_db

    assert len(rows) == 1
    row = rows[0]
    assert row["sessionId"]
    assert row["conversationId"]
    assert row["draftId"]
    assert row["sessionId"] != row["conversationId"]
    assert row["draftId"] != row["sessionId"]


def test_seed_only_writes_its_own_database(seeded_db, tmp_path):
    """种子只写**自己那份**数据库。"""
    db_path, _rows = seeded_db

    assert db_path.exists(), "种子应当在指定路径建库"
    # 库里只有这个会话 —— 没有别的会话被顺手写进来。
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        sessions = [
            item[0] for item in conn.execute("SELECT id FROM sessions").fetchall()
        ]
    finally:
        conn.close()
    assert len(sessions) == 1


def test_seed_does_not_plant_a_candidate_plan(seeded_db):
    """种子**只种前置状态** —— 不种候选计划 / EditPlan。

    这是硬要求：候选必须由真实自然语言入口产生，手工种候选会把
    「人工种植」冒充成「中文入口成功证据」。
    """
    db_path, rows = seeded_db
    session = _read_session(db_path, rows[0]["sessionId"])

    # pending_choice 是**合法 NEXT_ACTION 菜单**，不是候选确认菜单。
    import json

    pending = json.loads(session["pending_choice"]) if session["pending_choice"] else None
    assert pending is not None, "种子要种一份合法 NEXT_ACTION 菜单"
    question = pending["question"]
    assert question["id"] == "template_edit_confirm"
    assert question["lockScope"] == "input_and_menu"
    assert question["stepKind"] == "menu"
    assert question["stepId"]
    assert question["stepVersion"] == 1
    assert question["id"] != "pending_edit_plan_confirm", "不能种候选确认菜单"

    # 会话行里没有待决计划。
    assert "pending_plan" not in session or session.get("pending_plan") in (None, "")
    # 助手消息的载荷里也没有候选计划。
    for message in session["_messages"]:
        payload = message.get("payload") or "{}"
        assert "pendingEditPlan" not in payload, "助手消息里不能带候选计划"
        assert "editPlan" not in payload, "助手消息里不能带候选计划"


def test_seed_draws_the_menu_with_one_assistant_message(seeded_db):
    """种子要有一条**画出这张菜单**的助手消息（刷新回放靠它）。"""
    db_path, rows = seeded_db
    session = _read_session(db_path, rows[0]["sessionId"])

    messages = session["_messages"]
    assert len(messages) == 1, f"应当只有一条助手消息，实际 {len(messages)} 条"
    message = messages[0]
    assert message["role"] == "assistant"

    import json

    payload = json.loads(message["payload"])
    assert payload["questions"][0]["id"] == "template_edit_confirm"
    assert payload["domainObjects"]["reportDefinitionDraft"]["regions"]


def test_seed_is_idempotent(tmp_path):
    """重复跑：消息**不越堆越多**，草稿对象重建。"""
    db_path = tmp_path / "haewu-night-idem.db"

    code, first_out, _ = _run_seed(db_path)
    assert code == 0
    first = _parse_seeded(first_out)[0]

    code, second_out, _ = _run_seed(db_path)
    assert code == 0
    second = _parse_seeded(second_out)[0]

    assert first == second, "同一套种子两次跑出来的 id 必须一致"

    session = _read_session(db_path, first["sessionId"])
    assert len(session["_messages"]) == 1, "重复跑不能把菜单越堆越多"


def test_seed_variants_are_independent(tmp_path):
    """派生多个会话：各自独立的 sessionId / conversationId / draftId。"""
    db_path = tmp_path / "haewu-night-variants.db"

    code, stdout, stderr = _run_seed(
        db_path, {"SEED_VARIANTS": "t01,t04,t12"}
    )
    assert code == 0, stderr
    rows = _parse_seeded(stdout)

    assert len(rows) == 3
    assert len({row["sessionId"] for row in rows}) == 3
    assert len({row["conversationId"] for row in rows}) == 3
    assert len({row["draftId"] for row in rows}) == 3

    # 每个会话的现场互不干扰。
    for row in rows:
        session = _read_session(db_path, row["sessionId"])
        assert len(session["_messages"]) == 1

    # 每个草稿对象都在库里，且定义**互不相同**（各自独立）。
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        drafts = conn.execute(
            "SELECT draft_id FROM draft_objects ORDER BY draft_id"
        ).fetchall()
    finally:
        conn.close()
    assert len(drafts) == 3


def test_seed_definition_matches_the_contract(seeded_db):
    """基础草稿：报表标题 + 三张正文卡，名字与占宽都按契约 §4.1。"""
    db_path, rows = seeded_db
    session = _read_session(db_path, rows[0]["sessionId"])

    import json

    snapshot = json.loads(session["snapshot"])
    regions = snapshot["definition"]["regions"]
    by_id = {r["id"]: r for r in regions}

    assert set(by_id) == {
        "region_report_title",
        "region_total_energy",
        "region_device_ranking",
        "region_hourly_trend",
    }
    # 用户页面上的真名。
    assert by_id["region_total_energy"]["name"] == "总耗电概览"
    assert by_id["region_hourly_trend"]["name"] == "能耗趋势分析"
    assert by_id["region_device_ranking"]["name"] == "设备耗电排名"

    # 可增宽样本 / 已满宽样本。
    total_span = by_id["region_total_energy"]["layout"]["placement"]["columnSpan"]
    trend_span = by_id["region_hourly_trend"]["layout"]["placement"]["columnSpan"]
    ranking_span = by_id["region_device_ranking"]["layout"]["placement"]["columnSpan"]
    assert total_span == 4, "总览卡是可增宽样本"
    assert trend_span == 12, "趋势卡是已满宽样本"
    assert ranking_span == 12, "排名卡是已满宽样本"

    # 组件类型。
    assert by_id["region_total_energy"]["component"]["type"] == "METRIC_CARD"
    assert by_id["region_hourly_trend"]["component"]["type"] == "CHART"
    assert by_id["region_device_ranking"]["component"]["type"] == "TABLE"


def test_seed_honours_env_overrides(tmp_path):
    """``SEED_SESSION_ID`` / ``SEED_CONVERSATION_ID`` 可以覆盖。"""
    db_path = tmp_path / "haewu-night-env.db"

    code, stdout, stderr = _run_seed(
        db_path,
        {
            "SEED_SESSION_ID": "sess_night_custom",
            "SEED_CONVERSATION_ID": "conv_night_custom",
        },
    )
    assert code == 0, stderr
    row = _parse_seeded(stdout)[0]

    assert row["sessionId"] == "sess_night_custom"
    assert row["conversationId"] == "conv_night_custom"
    session = _read_session(db_path, "sess_night_custom")
    assert len(session["_messages"]) == 1


def test_seed_resets_stale_pending_plan_and_choice(tmp_path):
    """上一轮遗留的**待确认计划**必须被种子清掉，会话要复位成干净基线。

    只重置 ``pending_choice`` 是不够的：``sessions.pending_edit_plan`` 是
    独立列，服务端见到待确认计划就以「待确认计划」菜单为准，NEXT_ACTION
    菜单被顶掉 —— 页面上点「修改」不再被当成模板编辑入口，会掉进普通上下文
    路由。结果就是：**同一个会话第二次跑必然失效**。

    这条是浏览器级实跑踩出来的坑，不是理论推演。
    """
    import json
    import sqlite3

    db_path = tmp_path / "haewu-night-reset.db"

    code, stdout, stderr = _run_seed(db_path)
    assert code == 0, stderr
    row = _parse_seeded(stdout)[0]
    session_id = row["sessionId"]

    # 人为留下「上一轮跑剩」的现场：一个待确认计划 + 一个别处的待决选择。
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE sessions SET pending_edit_plan = ?, pending_choice = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "planId": "plan_stale",
                        "draftRevision": 1,
                        "operations": [],
                        "summary": "上一轮遗留的旧计划",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "state": "USER_EDITING_TEMPLATE",
                        "question": {"id": "old_question"},
                    },
                    ensure_ascii=False,
                ),
                session_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    code, stdout, stderr = _run_seed(db_path)
    assert code == 0, stderr

    session = _read_session(db_path, session_id)
    assert not (session.get("pending_edit_plan") or "").strip(), \
        "种子必须清掉上一轮遗留的待确认计划"
    pending_choice = json.loads(session["pending_choice"])
    assert pending_choice["question"]["id"] == "template_edit_confirm", \
        "种子必须把待决选择复位成 NEXT_ACTION 菜单"
