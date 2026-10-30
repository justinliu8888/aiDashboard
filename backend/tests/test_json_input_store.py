"""JSON 输入记录的存储层：建表只在初始化时做一次（code-review R2 🔵-5）。

契约表 §8 不变量 2 要求「生效组合的切换是**一次**原子提交」。
这张表与设计存储、会话存储**共用同一条连接**（同一把锁），
所以存储层的每一次读写都可能在**别人正在跑的事务**上动手脚。

改前 ``initialize_schema()`` 每次读写都跑一遍 ``executescript(_SCHEMA)`` +
``commit()``。CPython 的 ``executescript`` 会先把共享连接上**当前的事务**
提交掉 —— 实测：别人的事务 ``in_transaction`` 由 ``True`` 变 ``False``，
未提交的行再也回滚不掉。

改法（本次落地）：

- 建表 / 建索引这类 DDL **收敛成一次**（存储层初始化时做一次）；
- 逐条 ``execute``，**不用** ``executescript``（它才会顺带提交别人事务）；
- 每次读写的**数据操作**只走参数化的 INSERT / SELECT / UPDATE。

本文件只做**单元层**断言；不新增端点、不新增测试框架。
"""
from __future__ import annotations

import sqlite3
import threading

from app.storage.json_input_store import JsonInputRecord, SqliteJsonInputStore


def _connection() -> sqlite3.Connection:
    """一条内存连接（行按列名取，与运行时一致）。"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _record(
    *,
    input_id: str = "inp_probe000001",
    input_version: int = 1,
    session_id: str = "session_probe",
) -> JsonInputRecord:
    """一条输入记录（一个版本）。"""
    return JsonInputRecord(
        inputId=input_id,
        inputVersion=input_version,
        sessionId=session_id,
        arrayPath="",
        rawJson="[]",
        schema={},
        sample=[],
        recordCount=0,
        byteSize=0,
        createdAt="2026-10-30T00:00:00",
    )


def _create_statements(traced: list[str]) -> list[str]:
    """追踪到的建表 / 建索引语句。"""
    return [
        statement
        for statement in traced
        if statement.lstrip().upper().startswith("CREATE")
    ]


# --------------------------------------------------------------------------
# 建表只做一次
# --------------------------------------------------------------------------


def test_schema_is_created_only_once_across_reads_and_writes() -> None:
    """连续读写多次，**建表语句只执行一次**。

    改前：1 写 + 4 读 = 12 次 DDL（每条读写都重跑一遍建表 + 建索引）。
    """
    conn = _connection()
    traced: list[str] = []
    conn.set_trace_callback(traced.append)
    store = SqliteJsonInputStore(connection=conn, lock=threading.RLock())

    store.initialize_schema()
    store.insert(_record())
    store.get("inp_probe000001", 1)
    store.latest_for_session("session_probe")
    store.list_for_session("session_probe")
    store.next_version("session_probe", "inp_probe000001")

    creates = _create_statements(traced)
    assert len(creates) == 2, creates  # 一条建表 + 一条建索引，各一次
    assert sum("CREATE TABLE" in s.upper() for s in creates) == 1, creates
    assert sum("CREATE INDEX" in s.upper() for s in creates) == 1, creates


def test_repeated_initialize_schema_does_not_run_ddl_again() -> None:
    """反复调 ``initialize_schema()`` 也不重跑 DDL。"""
    conn = _connection()
    traced: list[str] = []
    conn.set_trace_callback(traced.append)
    store = SqliteJsonInputStore(connection=conn, lock=threading.RLock())

    for _ in range(5):
        store.initialize_schema()

    assert len(_create_statements(traced)) == 2, traced


# --------------------------------------------------------------------------
# 读路径不再顺带提交别人的事务
# --------------------------------------------------------------------------


def test_reads_do_not_commit_a_pending_transaction() -> None:
    """读**不**提交共享连接上别人正在跑的事务。

    改前：第一次读之后 ``in_transaction`` 就变成 False，未提交的行再也
    回滚不掉 —— 契约表 §8 不变量 2 的原子切换被悄悄破坏。
    """
    conn = _connection()
    conn.execute("CREATE TABLE probe_t (id INTEGER PRIMARY KEY, v TEXT)")
    store = SqliteJsonInputStore(connection=conn, lock=threading.RLock())
    store.initialize_schema()

    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO probe_t (v) VALUES ('未提交')")
    assert conn.in_transaction is True

    store.get("inp_probe000001", 1)
    store.latest_for_session("session_probe")
    store.list_for_session("session_probe")
    store.next_version("session_probe", "inp_probe000001")

    assert conn.in_transaction is True
    conn.rollback()
    remaining = conn.execute("SELECT COUNT(*) FROM probe_t").fetchone()[0]
    assert remaining == 0, "未提交的行不该被读路径提前提交"


# --------------------------------------------------------------------------
# 读写的既有行为不变
# --------------------------------------------------------------------------


def test_reads_and_writes_still_work() -> None:
    """读写仍然是原来那个样子（建表收敛成一次**不改变**读写语义）。"""
    conn = _connection()
    store = SqliteJsonInputStore(connection=conn, lock=threading.RLock())
    store.initialize_schema()

    store.insert(_record(input_version=1))
    store.insert(_record(input_version=2))

    assert store.get("inp_probe000001", 1) is not None
    assert store.get("inp_probe000001", 9) is None
    assert store.get("", 1) is None
    assert len(store.list_for_session("session_probe")) == 2
    assert store.latest_for_session("session_probe") is not None
    assert store.latest_for_session("") is None
    assert store.list_for_session("") == []
    assert store.next_version("session_probe", "inp_probe000001") == 3

    # 同一版本重复写入按幂等处理：不新增行。
    store.insert(_record(input_version=1))
    assert len(store.list_for_session("session_probe")) == 2


def test_store_can_be_created_without_explicit_initialize() -> None:
    """没先调 ``initialize_schema()`` 也不会崩 —— 第一次读写时补建一次。"""
    conn = _connection()
    store = SqliteJsonInputStore(connection=conn, lock=threading.RLock())

    store.insert(_record())
    assert store.get("inp_probe000001", 1) is not None
