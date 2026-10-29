"""工单 08 / GitHub #54：模板态「报表示意」的接口层验收（缝 1：接口层主缝）。

规格权威：GitHub issue #46「Testing Decisions → 缝 1：接口层（主缝，最高）」、
ADR ``docs/adr/0004-模板态一律用模拟业务数据渲染.md``、``GLOSSARY.md``。

本文件只走**接口层**：真实 ``POST /api/agent/chat``、真实编排、真实服务、
真实 SQLite（每条用例一份独立临时库），**只替换外部模型调用**这一处。
夹具见 ``tests/support/acceptance_harness.py``。

本轮覆盖的能力（已完成的那几票）：

1. 方案在草稿阶段就带数（票05 接入后才有载荷）；
2. 模拟数据不落库（任何表都不许存模拟业务数值）；
3. 确定性（恢复会话重新生成的载荷与原响应逐字段相等）；
4. 定义层不含数值；
5. 不取真实业务数据。

**票05（GitHub #51）的占位**：下面标了「待票05」的用例依赖
``AgentResponse.reportPreviewPayload``。该字段在票05 合入前不存在，
所以这些用例用 ``requires_report_preview_payload`` 标记**跳过**（不是失败）；
票05 合入后标记自动失效，断言按票05 的契约立即生效。
断言里没有写死任何具体数值 —— 规格也明确「不断言造数器的内部函数、
不断言哈希算法与具体数值」。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest

from app.config import get_settings
from app.services.session_service import session_service
from tests.support.acceptance_harness import (
    BUSINESS_VALUE_KEYS,
    KNOWN_TABLES,
    REPORT_PREVIEW_PAYLOAD_FIELD,
    SNAPSHOT_ALLOWED_KEYS,
    acceptance_harness,
    report_preview_payload_pending_reason,
    snapshot_required_keys,
    supports_report_preview_payload,
)


#: 票05（GitHub #51）合入前，这些用例先跳过；合入后自动生效。
requires_report_preview_payload = pytest.mark.skipif(
    not supports_report_preview_payload(),
    reason=report_preview_payload_pending_reason(),
)


# --------------------------------------------------------------------------
# 小工具：递归找键 / 找数
# --------------------------------------------------------------------------


def _collect_keys(node: Any) -> set[str]:
    """递归收集所有**键名**（只看键，不看值）。"""
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(str(key))
            keys |= _collect_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _collect_keys(item)
    return keys


def _numbers_in(node: Any) -> list[float | int]:
    """递归收集所有数值。"""
    numbers: list[float | int] = []
    if isinstance(node, dict):
        for value in node.values():
            numbers.extend(_numbers_in(value))
    elif isinstance(node, list):
        for item in node:
            numbers.extend(_numbers_in(item))
    elif isinstance(node, bool):
        return numbers
    elif isinstance(node, (int, float)):
        numbers.append(node)
    return numbers


def _business_value_keys_in_definition(definition: dict[str, Any]) -> set[str]:
    """定义里**业务结果位置**上的数值字段名。

    只看区域的 ``data``（业务结果容器），不看版面配置 ——
    ``page.margins.*.value`` 这类结构数字是允许的：规格写明
    「『定义不含数值』指不含模拟业务结果；不禁止结构配置里的数字
    （列宽、版本号、时间粒度等）」。
    """
    hits: set[str] = set()
    for region in definition.get("regions") or []:
        if not isinstance(region, dict):
            continue
        data = region.get("data")
        if isinstance(data, dict):
            hits |= _collect_keys(data) & set(BUSINESS_VALUE_KEYS)
    return hits


def _walk_json(node: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], str, Any]]:
    """递归产出 JSON 里的每个「键 - 值」对（带路径）。"""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, str(key), value
            yield from _walk_json(value, path + (str(key),))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk_json(item, path + (f"[{index}]",))


#: 无歧义的业务结果键：出现即视为「模拟业务数值落库了」。
UNAMBIGUOUS_BUSINESS_VALUE_KEYS: frozenset[str] = frozenset(
    {
        "values",
        "yValues",
        "xLabels",
        "rows",
        "points",
        "yoyPercent",
        "comparisonAvailable",
    }
)


def _persisted_business_values(
    tables: dict[str, list[dict[str, Any]]],
) -> list[tuple[str, str, Any]]:
    """库里所有**业务结果位置**上的数值。

    只看 JSON 单元（会话快照 / 消息载荷 / 定义 / 方案 / 模板）。

    ``value`` 这个键名有歧义：布局配置（``page.margins.*.value``）、
    时间窗描述（``timeWindow.value``）、选项值（``options[].value``）
    都叫 ``value``，但它们**不是**业务结果。所以判定规则是：

    - 无歧义的业务键（``rows`` / ``yValues`` / …）：出现即命中；
    - ``value``：只有**数值**且不在 ``page`` 版面配置下才算命中；
    - ``page`` 整棵跳过 —— 版面配置里的数字是结构数字，不是业务结果。

    依据：规格「『定义不含数值』指不含模拟业务结果；不禁止结构配置里的数字
    （列宽、版本号、时间粒度等）」。
    """
    found: list[tuple[str, str, Any]] = []
    for table, rows in tables.items():
        for row_index, row in enumerate(rows):
            for column, cell in row.items():
                if not isinstance(cell, str):
                    continue
                text = cell.strip()
                if not (text.startswith("{") or text.startswith("[")):
                    continue
                try:
                    parsed = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    continue
                for path, key, value in _walk_json(parsed):
                    if any(part == "page" for part in path):
                        continue
                    if key in UNAMBIGUOUS_BUSINESS_VALUE_KEYS:
                        found.append(
                            (
                                f"{table}.{column}[{row_index}]",
                                ".".join(path + (key,)),
                                value,
                            )
                        )
                        continue
                    if key == "value" and isinstance(value, (int, float)) and not (
                        isinstance(value, bool)
                    ):
                        found.append(
                            (
                                f"{table}.{column}[{row_index}]",
                                ".".join(path + (key,)),
                                value,
                            )
                        )
    return found


def _payload_numbers(payload: dict[str, Any]) -> set[float]:
    """一个区域载荷里的业务数值。"""
    return {float(n) for n in _numbers_in(payload) if isinstance(n, (int, float))}


# --------------------------------------------------------------------------
# 0. 夹具自检
# --------------------------------------------------------------------------


def test_acceptance_harness_uses_an_isolated_temporary_database():
    """夹具自检：用例跑在**独立临时库**上，跑完**不留垃圾**。

    对应缝 1 的硬要求「真实 SQLite（独立临时库）」：
    每条用例一份自己的库文件，绝不指向仓库里的真实 db。
    """
    backend_dir = Path(__file__).resolve().parents[1]
    real_db = Path(get_settings().session_db_path).resolve()

    with acceptance_harness() as harness:
        assert harness.db_path.exists()
        # 库文件在夹具自己的临时目录里。
        assert harness.db_path.parent == harness.tmp_dir
        # 不是仓库里的真实 db。
        assert harness.db_path.resolve() != real_db
        assert backend_dir not in harness.db_path.resolve().parents
        # 会话存储确实换成了这份临时库（编排链走的就是它）。
        assert session_service.repository.db_path.resolve() == harness.db_path.resolve()
        tmp_dir = harness.tmp_dir

    # 用完即删：临时目录整个消失，不留垃圾。
    assert not tmp_dir.exists()


def test_plan_confirmation_runs_real_orchestration_with_only_the_model_replaced():
    """缝 1 的核心：真实编排真跑，**只**替换外部模型调用这一处。

    证明方式（三条一起看才成立）：
    - 模型替身被调用了 → 方案确实走的是「模型 → 方案服务」这条真链路；
    - 业务 Agent 一次都没被调 → 方案不是被某个替身 Agent 直接吐出来的；
    - 草稿真的生成了 → 方案服务 → 转换层 → 响应组装都真跑了。
    """
    with acceptance_harness() as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        confirmed = harness.confirm_plan(plan)

        # 模型替身被调了（真链路）。
        assert len(harness.model.calls) == 1
        # 业务 Agent 一次都没被调（真实编排，不是替身 Agent）。
        assert harness.agent_invocations == []
        # 草稿真的出来了（转换层 + 响应组装都跑了）。
        assert confirmed["designPhase"] == "DRAFT_READY"
        draft = confirmed["domainObjects"]["reportDefinitionDraft"]
        assert draft
        assert draft["regions"]


# --------------------------------------------------------------------------
# 1. 方案在草稿阶段就带数（依赖票05）
# --------------------------------------------------------------------------


@requires_report_preview_payload
def test_confirmed_draft_carries_simulated_payload_in_the_same_turn():
    """需求 1：点一次确认方案，**同一轮响应**就带回带数的示意载荷。

    规格 #46 缝 1：「点一次确认方案 → 同一轮响应就带回草稿与示意数据，
    落点为草稿就绪 + 等待用户确认；响应里没有『再请求一次才能拿到数据』的情况。」

    待票05（GitHub #51）：本用例依赖响应顶层的 ``reportPreviewPayload``。
    票05 合入前该字段不存在 → 跳过；合入后立即生效。
    """
    with acceptance_harness() as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        confirmed = harness.confirm_plan(plan)

        # 同一轮就带回来了 —— 不需要二次请求。
        preview = confirmed[REPORT_PREVIEW_PAYLOAD_FIELD]
        assert preview is not None
        assert preview["regions"], "示意载荷是空的：草稿阶段仍然画空壳"
        # 上下文也在（冻结的时间窗，恢复时要靠它复现同样的数）。
        assert preview["previewContext"]

        for region in preview["regions"]:
            assert region["regionId"]
            assert region["name"]
            assert region["componentType"]
            payload = region["payload"]
            # 没有「暂无数值」占位。
            assert payload.get("placeholder") is not True
            # 每个非标题板块都出数（数值是真的，不是破折号）。
            assert _payload_numbers(payload), (
                f"区域 {region.get('regionId')} 没有出数：{payload}"
            )


# --------------------------------------------------------------------------
# 2. 模拟数据不落库
# --------------------------------------------------------------------------


def test_temporary_database_contains_no_business_value_table():
    """需求 2（表这一半）：临时库里**没有**业务数值表。

    对应 ADR 0004 §5.1「不建业务数值表、不落盘」与规格 #46
    「模拟业务数据不写入任何业务数值表；数据库不新增数值表」。
    """
    with acceptance_harness() as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        harness.confirm_plan(plan)

        tables = harness.table_names()
        # 库里只有会话存储与设计存储的表，没有多出来的业务数值表。
        assert set(tables) <= KNOWN_TABLES, set(tables) - KNOWN_TABLES
        # 没有任何一张表带业务数值列。
        for name, columns in harness.table_columns().items():
            assert not (set(columns) & set(BUSINESS_VALUE_KEYS)), (name, columns)


@requires_report_preview_payload
def test_simulated_values_are_absent_from_every_table_in_the_temporary_database():
    """需求 2（值这一半）：模拟业务数值**一个格子都不许**落在库里。

    对应规格 #46「模拟业务数据只存在于响应与内存，不写入任何持久化存储：
    会话快照表、消息全响应、运行结果缓存、日志正文一律不落」。

    待票05（GitHub #51）：要拿到模拟数值才能反查，本用例依赖
    ``reportPreviewPayload``。
    """
    with acceptance_harness() as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        confirmed = harness.confirm_plan(plan)
        preview = confirmed[REPORT_PREVIEW_PAYLOAD_FIELD]

        # 先确认确实有模拟数值（否则这条断言是空的）。
        values: set[float] = set()
        for region in preview["regions"]:
            values |= _payload_numbers(region["payload"])
        assert values, "响应里没有模拟数值，这条用例无法验证存储隔离"

        tables = harness.dump_all_tables()
        # ⓪ 扫描器**非空**自检（两个方向都要成立，否则下面两条是空断言）：
        #    - 种进去的业务数值必须被抓到 —— 抓到才说明扫描器没失灵；
        #    - 版面配置里的数字必须**抓不到** —— 抓到就说明口径太宽，
        #      会把 ``page.margins.*.value`` 这类结构数字误判成业务数值。
        planted = {
            "sessions": [
                {
                    "snapshot": json.dumps(
                        {
                            "regions": [
                                {"data": {"value": 123.4, "rows": [{"k": 1}]}}
                            ],
                            "page": {"margins": {"top": {"value": 10}}},
                        },
                        ensure_ascii=False,
                    )
                }
            ]
        }
        planted_hits = _persisted_business_values(planted)
        assert planted_hits, "扫描器失灵：种进去的业务数值一个都没抓到"
        assert any(path.endswith("rows") for _where, path, _value in planted_hits)
        assert any(
            path.endswith("value") and value == 123.4
            for _where, path, value in planted_hits
        )
        assert not any(
            path.endswith("page.margins.top.value")
            for _where, path, _value in planted_hits
        ), "版面配置里的数字被误判成业务数值了"
        # ① 库里**根本没有**业务结果位置：落库的 JSON 里一个业务数值键都没有。
        persisted = _persisted_business_values(tables)
        assert not persisted, persisted
        # ② 模拟数值也没有落进任何单元。
        #    只跟**业务结果位置**上的数值精确比对：版本号、消息序号这类
        #    结构整数跟业务数值会撞车，按子串比对会误判。
        business_cells = {
            float(value)
            for _where, _path, value in persisted
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        assert not (values & business_cells), sorted(values & business_cells)
        # ③ 整份区域载荷也不在库里（子串比对，兜住「拆开存」的情况）。
        blob = json.dumps(tables, ensure_ascii=False)
        for region in preview["regions"]:
            serialized = json.dumps(region["payload"], ensure_ascii=False)
            assert serialized not in blob, region.get("regionId")


def test_session_snapshot_keeps_allowed_keys_and_carries_no_simulated_payload():
    """需求 2（快照这一半）：快照**含允许列表里的键**、**不含模拟载荷字段**。

    对应规格 #46 的存储隔离条款：会话持久化快照走允许列表
    （``definition`` / ``policy`` / ``reportPlan`` / ``templateDesign`` /
    ``designPhase`` / ``previewContext``，以及既有 ``previewPayload`` 的原语义），
    ``reportPreviewPayload`` **不在**持久化快照里 —— 它是响应级临时字段。
    """
    with acceptance_harness() as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        harness.confirm_plan(plan)

        snapshot = harness.persisted_snapshot()

        # ① 不含模拟载荷字段。
        assert REPORT_PREVIEW_PAYLOAD_FIELD not in snapshot
        # ② 含允许列表里的键（``previewContext`` 由票05 引入，按契约存在与否断言）。
        for key in snapshot_required_keys():
            assert key in snapshot, key
        # ③ 快照里**没有没登记的键**：除会话服务自己写的 ``scope`` /
        #    ``collectedInfo`` 外，其余键都落在规格的允许列表里
        #    （落库侧是「只写允许的键」，不是「把某些键剔掉」）。
        session_owned = {"scope", "collectedInfo"}
        unexpected = set(snapshot) - session_owned - set(SNAPSHOT_ALLOWED_KEYS)
        assert not unexpected, unexpected
        # ④ 快照里的定义没有任何业务数值字段。
        definition = snapshot.get("definition") or {}
        assert not _business_value_keys_in_definition(definition)
        # ⑤ 落库的 assistant 消息里同样没有模拟业务结果
        #    （上下文可留，区域数值不留）。
        for message in harness.assistant_messages():
            stored = (message.payload or {}).get(REPORT_PREVIEW_PAYLOAD_FIELD)
            if stored is None:
                continue
            assert "regions" not in stored


@requires_report_preview_payload
def test_session_snapshot_keeps_the_frozen_preview_context():
    """需求 2（上下文这一半）：冻结的示意上下文**允许**落库。

    上下文只有定义版本、时间窗、粒度这些**结构**，没有业务数值 ——
    所以它可以落库；恢复会话时靠它把同样的数重新生成出来。

    待票05（GitHub #51）：``previewContext`` 由票05 引入。
    """
    with acceptance_harness() as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        confirmed = harness.confirm_plan(plan)
        context = confirmed[REPORT_PREVIEW_PAYLOAD_FIELD]["previewContext"]

        snapshot = harness.persisted_snapshot()

        assert snapshot["previewContext"] == context
        # 上下文里只有结构，没有业务数值。
        assert not (_collect_keys(context) & set(BUSINESS_VALUE_KEYS))


# --------------------------------------------------------------------------
# 3. 确定性
# --------------------------------------------------------------------------


@requires_report_preview_payload
def test_restored_session_regenerates_an_identical_payload():
    """需求 3：恢复会话重新生成的载荷，与原响应**逐字段相等**。

    对应规格 #46「同一份定义重复请求 → 数字完全相同」与 ADR 0004 §5.2
    「同一份定义 + 同一时间窗 → 数字恒定」。

    待票05（GitHub #51）：依赖读回入口按定义与上下文**重新生成**载荷。
    """
    with acceptance_harness() as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        confirmed = harness.confirm_plan(plan)
        original = confirmed[REPORT_PREVIEW_PAYLOAD_FIELD]

        restored = harness.read_session_snapshot()[REPORT_PREVIEW_PAYLOAD_FIELD]

        assert restored is not None
        # 上下文一致。
        assert restored["previewContext"] == original["previewContext"]
        # 区域载荷逐字段一致（数值也一致）。
        assert restored["regions"] == original["regions"]
        # 重新生成的那份**没有写回**持久化快照。
        assert REPORT_PREVIEW_PAYLOAD_FIELD not in harness.persisted_snapshot()


@requires_report_preview_payload
def test_agreeing_the_draft_keeps_the_simulated_values_and_runs_the_trial():
    """点「同意」：校验与试运行照跑，**示意数值一点没变**。

    对应规格 #46 缝 1「点『同意』→ 校验与试运行照跑；示意数值不变」，
    以及 ADR 0004 §2.4「点同意之后右侧数字不动」。

    同时验证存储隔离的另一半：模板态的**试运行结果也不落库** ——
    它跑的是模拟业务数据，按规格 #46「模拟业务数据只存在于响应与内存」
    只留在内存与响应里；读会话时按内存里的运行结果挂回响应
    （``previewPayload`` 仍然是试运行结果的键，语义不变）。

    待票05（GitHub #51）：依赖响应顶层与快照里的 ``reportPreviewPayload``。
    """
    with acceptance_harness() as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        confirmed = harness.confirm_plan(plan)
        before = confirmed[REPORT_PREVIEW_PAYLOAD_FIELD]

        # 点「同意」→ 校验与试运行（内部控制消息按状态推进）。
        harness.agree_draft()
        for state in ("VALIDATING", "PREVIEWING", "PREVIEW_READY"):
            harness.advance(state)

        # 模板态的试运行结果**不落库**（模拟业务数据不进持久化存储）。
        snapshot = harness.persisted_snapshot()
        assert "previewPayload" not in snapshot
        # 运行标识与运行状态**照旧保存**（规格 #46：运行状态与运行标识可保存）。
        assert snapshot["lastReportRun"]["reportRunId"]
        # 模拟业务结果仍然**不落库**。
        assert REPORT_PREVIEW_PAYLOAD_FIELD not in snapshot
        # 但读会话时**仍然拿得到**试运行结果（响应级，内存里现取的）。
        restored = harness.read_session_snapshot()
        assert restored["previewPayload"]["regions"]
        # 示意数值一点没变（恢复出来的与确认那一轮逐字段相等）。
        assert restored[REPORT_PREVIEW_PAYLOAD_FIELD]["regions"] == before["regions"]
        assert restored[REPORT_PREVIEW_PAYLOAD_FIELD]["previewContext"] == before[
            "previewContext"
        ]


# --------------------------------------------------------------------------
# 4. 定义层不含数值
# --------------------------------------------------------------------------


def test_confirmed_draft_definition_contains_no_business_value_fields():
    """需求 4：报表定义里**没有**任何业务数值字段。

    对应 ADR 0004 §2.2「报表定义仍然不含数值」与规格 #46
    「报表定义里不含任何模拟业务数值；不建业务数值表」。

    数值不进 ``definition_json``，也不随草稿落库。

    **口径说明（为什么不是「整份定义里没有 ``value`` 这个键」）**：
    定义里确实有一个键名叫 ``value`` —— ``page.margins.top.value``
    （页边距，单位 MM）。规格与 ADR 都写明「『定义不含数值』指不含
    **模拟业务结果**；不禁止结构配置里的数字（列宽、版本号、时间粒度等）」
    （ADR 0004 §5.1）。所以这里分两层查：

    - **无歧义的**业务数值键（``rows`` / ``points`` / ``yValues`` /
      ``xLabels`` / ``values`` / ``yoyPercent`` / ``comparisonAvailable``）
      在**整份定义**里一个都不能出现；
    - **有歧义的** ``value`` 只在**业务结果位置**（区域的 ``data``）
      算命中 —— 这一条沿用仓库既有的判定口径
      （``tests/test_report_template_generation.py`` 断言 region data
      不含 ``value / yValues / xLabels / rows / points``）。
    """
    with acceptance_harness() as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        confirmed = harness.confirm_plan(plan)

        definition = confirmed["domainObjects"]["reportDefinitionDraft"]

        # ① 整份定义里没有**无歧义**的业务数值字段。
        unambiguous = _collect_keys(definition) & UNAMBIGUOUS_BUSINESS_VALUE_KEYS
        assert not unambiguous, unambiguous
        # ② 有歧义的 ``value``：业务结果位置上一个都没有
        #    （版面配置里的 ``page.margins.*.value`` 是允许的结构数字）。
        found = _business_value_keys_in_definition(definition)
        assert not found, found
        # ③ 也不存在小数型的业务数值（列宽、版本号这类结构数字不算）。
        assert [n for n in _numbers_in(definition) if isinstance(n, float)] == []
        # ④ 落库的那份定义同样干净。
        persisted = harness.persisted_snapshot().get("definition") or {}
        assert not (_collect_keys(persisted) & UNAMBIGUOUS_BUSINESS_VALUE_KEYS)
        assert not _business_value_keys_in_definition(persisted)
        # ⑤ 仓库既有口径：模板设计的区域 ``data`` 里没有任何业务数值，
        #    只剩时间参数与标签这类元数据。
        template = confirmed["domainObjects"]["templateDesign"]
        for region in template["regions"]:
            data = region["data"]
            assert data is not None
            for key in ("value", "values", "yValues", "xLabels", "rows", "points",
                        "yoyPercent", "comparisonAvailable"):
                assert key not in data, key
            assert [n for n in _numbers_in(data) if isinstance(n, float)] == []


# --------------------------------------------------------------------------
# 5. 不取真实业务数据
# --------------------------------------------------------------------------


def test_request_chain_never_queries_a_business_data_source():
    """需求 5：请求链路**没有**触发任何真实业务库查询。

    对应规格 #46「不引入真实业务取数通道；真实取数是后续独立决策」与
    ADR 0004 §5.9。

    证明方式（沿用仓库既有的「证明没取真数」手法，并补上接口层的观测面）：
    - ① 整条请求链路碰过的表都在「会话 + 设计存储」白名单里，没有别的来源；
    - ② 链路碰过的列里没有业务数值列；
    - ③ 确认轮没有新建任何表（不建业务数值表）；
    - ④ 响应没有发起任何取数动作（没有试运行预览、没有工具请求）。
    """
    with acceptance_harness() as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        # 设计存储的表在方案轮就建好了；确认轮不该再建任何表。
        tables_before = set(harness.table_names())
        harness.reset_sql_trace()

        confirmed = harness.confirm_plan(plan)

        # ① 只碰了会话与设计存储的表。
        referenced = harness.referenced_tables()
        assert referenced, "请求链路没有留下库访问轨迹，用例本身失效"
        assert referenced <= KNOWN_TABLES, referenced - KNOWN_TABLES
        # ② 没有碰过任何业务数值列。
        for table, column in harness.accessed_columns():
            assert column not in BUSINESS_VALUE_KEYS, (table, column)
        # ③ 没有新建任何表。
        assert set(harness.table_names()) == tables_before
        # ④ 没有发起取数动作：没有试运行预览，也没有工具请求。
        assert confirmed["domainObjects"]["reportRunPreview"] is None
        assert confirmed["toolRequests"] == []


@requires_report_preview_payload
def test_simulated_payload_comes_only_from_the_deterministic_generator():
    """需求 5（来源这一半）：数值**只可能**来自确定性造数器。

    对应规格 #46「只有一处生成示意数据的逻辑」「造数器是确定性的」。

    做法：给造数器套一层只记录、不改写的壳。响应里的区域载荷必须与
    造数器算出来的那一份**完全一致** —— 一致就说明数值不可能来自别处。

    待票05（GitHub #51）：确认轮要真的调用造数器才有这份载荷。
    """
    with acceptance_harness() as harness:
        with harness.spy_on_simulated_data_generator() as spy:
            plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
            confirmed = harness.confirm_plan(plan)

        assert spy.generator_calls, "确认轮没有调用造数器：数值不是模拟业务数据"
        assert spy.payload_calls, "确认轮没有经过载荷组装层"
        preview = confirmed[REPORT_PREVIEW_PAYLOAD_FIELD]
        # 响应里的载荷就是组装层算出来的那一份 —— 不可能来自别处。
        assert preview == spy.payload_calls[-1]
        assert preview["regions"] == spy.regions
