"""夜间统一修复：包 A 的**规范计划（canonical plan）**契约测试。

被测的是共享契约 v1 §1 的两条新链路：

- :func:`normalize_presentation_operations` —— 把模型 / 规则给出的**松散**
  参数规范成执行器认得的那一种写法（N1–N7）；
- :func:`preflight_presentation` —— 在**副本**上把整批跑一遍，
  **无副作用**地告诉调用方"这批能不能进候选"。

对应的用户可见缺陷：BUG-001（字号结构）、BUG-002（相对移动）、
BUG-004（分组间距）、BUG-006（排列层级）。

这份文件**只测包 A 自己的文件**：规范化规则、下标语义、预检无副作用。
入口接线（中文原句 → 候选 → 接受）是 C 包与 D 包的事，不在这里断言。
"""
from __future__ import annotations

import copy

import pytest

from app.schemas.report_definition import ReportDefinitionDraft
from app.services import report_presentation_service as rps
from app.services.report_draft_service import build_definition_draft
from app.services.report_presentation_service import (
    PresentationError,
    apply_presentation,
    is_container,
    normalize_presentation_operations,
    preflight_presentation,
)
from app.services.report_theme_service import theme_service


# --------------------------------------------------------------------------
# 共享样本
# --------------------------------------------------------------------------


def _three_cards() -> ReportDefinitionDraft:
    """三张正文卡：**总览 / 趋势 / 排名**，都在根层（同一个容器）。

    N2 的下标推导要能一眼看懂 —— 所以用**最小**的三卡定义，
    不掺标题 / 数据质量 / 分析说明那些与本用例无关的区域。

    根层顺序固定为 ``[总览, 趋势, 排名]``（下标 0 / 1 / 2）。
    """
    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "N2 三卡报表", "revision": 0},
            "regions": [
                {
                    "id": "region_overview",
                    "name": "总览",
                    "role": "SUMMARY",
                    "component": {"type": "METRIC_CARD"},
                },
                {
                    "id": "region_trend",
                    "name": "趋势",
                    "role": "TREND",
                    "component": {"type": "CHART"},
                },
                {
                    "id": "region_ranking",
                    "name": "排名",
                    "role": "DETAIL",
                    "component": {"type": "TABLE"},
                },
            ],
        }
    )


def _grouped() -> tuple[ReportDefinitionDraft, str]:
    """三张卡装进一个分组（容器）；返回 ``(定义, 分组 id)``。

    N3 / N2 的"目标是容器"那一支要用它。
    """
    definition = _three_cards()
    result = apply_presentation(
        definition,
        [
            {
                "op": "CREATE_GROUP",
                "params": {
                    "groupId": "group_1",
                    "name": "能耗概览",
                    "memberRegionIds": ["region_overview", "region_trend"],
                    "arrangement": {
                        "mode": "GRID",
                        "columns": 2,
                        "direction": "HORIZONTAL",
                        "gap": {"value": 16, "unit": "PX"},
                    },
                },
            }
        ],
    )
    assert result.ok, result.failure
    return result.definition, "group_1"


def _root_ids(definition: ReportDefinitionDraft) -> list[str]:
    return [str(item.get("id")) for item in definition.model_dump().get("regions") or []]


def _child_ids(definition: ReportDefinitionDraft, group_id: str) -> list[str]:
    for region in definition.model_dump().get("regions") or []:
        if str(region.get("id")) == group_id:
            return [str(child.get("id")) for child in region.get("children") or []]
    return []


def _theme_id_by_name(name: str) -> str | None:
    """按**主题名**查真实 themeId（主题的唯一来源是主题服务）。"""
    for item in theme_service.list_themes():
        if str(item.get("name") or "") == name:
            return str(item.get("themeId") or "")
    return None


# ==========================================================================
# N1：长度类字段统一成 {"value": n, "unit": "PX"}
# ==========================================================================


@pytest.mark.parametrize("loose", [28, "28px", "28", {"value": 28}])
def test_n1_loose_length_forms_normalize_to_one_canonical_shape(loose):
    """N1：三种松散写法 → 同一个 ``{"value": 28, "unit": "PX"}``。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    normalized = normalize_presentation_operations(
        [
            {
                "op": "SET_STYLE",
                "params": {
                    "regionId": "region_total_energy",
                    "style": {"fontSize": loose, "borderRadius": 4},
                },
            }
        ],
        definition,
    )

    assert normalized[0]["params"]["style"]["fontSize"] == {"value": 28, "unit": "PX"}
    # 规范化出来的这一批**执行器直接吃得下**。
    assert apply_presentation(definition, normalized).ok


def test_n1_rejects_unknown_unit():
    """N1 负向：单位只允许 PX / PERCENT —— EM 报错，不猜。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    with pytest.raises(PresentationError) as excinfo:
        normalize_presentation_operations(
            [
                {
                    "op": "SET_STYLE",
                    "params": {
                        "regionId": "region_total_energy",
                        "style": {"fontSize": {"value": 28, "unit": "EM"}},
                    },
                }
            ],
            definition,
        )

    assert excinfo.value.reason == "out_of_range"
    assert "PX" in excinfo.value.message


def test_n1_rejects_non_numeric_length():
    """N1 负向：``fontSize: "abc"`` 报错（不是数字）。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    with pytest.raises(PresentationError) as excinfo:
        normalize_presentation_operations(
            [
                {
                    "op": "SET_STYLE",
                    "params": {
                        "regionId": "region_total_energy",
                        "style": {"fontSize": "abc"},
                    },
                }
            ],
            definition,
        )

    assert excinfo.value.reason == "out_of_range"


def test_n1_covers_gap_and_page_width():
    """N1：``arrangement.gap`` / ``page.webMaxWidth`` 同样走长度规范化。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    normalized = normalize_presentation_operations(
        [
            {
                "op": "CREATE_GROUP",
                "params": {
                    "groupId": "group_1",
                    "name": "能耗概览",
                    "memberRegionIds": ["region_total_energy", "region_hourly_trend"],
                    "arrangement": {"mode": "GRID", "columns": 2, "gap": 16},
                },
            },
            {"op": "SET_PAGE", "params": {"webMaxWidth": "1200px"}},
        ],
        definition,
    )

    assert normalized[0]["params"]["arrangement"]["gap"] == {"value": 16, "unit": "PX"}
    assert normalized[1]["params"]["webMaxWidth"] == {"value": 1200, "unit": "PX"}


# ==========================================================================
# N2：MOVE_REGION 的相对位置 → targetGroupId + index
# ==========================================================================


def test_n2_after_region_id_is_resolved_into_index():
    """N2 正向：「把排名移到趋势后面」→ 规范化后**没有** afterRegionId。

    推导（三卡定义，根层顺序 ``[总览(0), 趋势(1), 排名(2)]``）：

    - 源卡 = 排名(2)，参照卡 = 趋势(1)；
    - 源卡在参照卡**后面** → 摘出源卡后参照卡的下标**不变**（仍是 1）；
    - "移到后面" = 插到参照卡之后 → 下标 = 1 + 1 = **2**；
    - 参照卡在**根层** → 没有目标分组（执行器把"没有 targetGroupId"读成根层）。
    """
    definition = _three_cards()

    normalized = normalize_presentation_operations(
        [
            {
                "op": "MOVE_REGION",
                "params": {
                    "regionId": "region_ranking",
                    "afterRegionId": "region_trend",
                },
            }
        ],
        definition,
    )

    params = normalized[0]["params"]
    assert "afterRegionId" not in params, "afterRegionId 不能原样穿透给执行器"
    assert "beforeRegionId" not in params
    assert params["regionId"] == "region_ranking"
    assert params["index"] == 2
    # 参照卡在根层 → 不带 targetGroupId（根层 = 没有目标分组）。
    assert params.get("targetGroupId") is None


def test_n2_index_is_computed_after_the_source_card_is_detached():
    """N2 下标语义：源卡与参照卡**同容器**且源卡在参照卡**前面** → 下标少 1。

    推导（三卡定义，根层顺序 ``[总览(0), 趋势(1), 排名(2)]``）：

    - 源卡 = 总览(0)，参照卡 = 趋势(1)；
    - 源卡在参照卡**前面** → 把源卡摘出后，参照卡的下标从 1 **前移到 0**；
    - "移到后面" = 插到参照卡之后 → 下标 = 0 + 1 = **1**（不是 2）；
    - 若不减一（写成 2），执行结果会变成 ``[趋势, 排名, 总览]`` ——
      总览跑到趋势**后面那张**的后面了，也就是"移到 X 后面"被做成了
      "移到 X 后面那张的后面"。
    """
    definition = _three_cards()

    normalized = normalize_presentation_operations(
        [
            {
                "op": "MOVE_REGION",
                "params": {
                    "regionId": "region_overview",
                    "afterRegionId": "region_trend",
                },
            }
        ],
        definition,
    )

    assert normalized[0]["params"]["index"] == 1

    result = apply_presentation(definition, normalized)
    assert result.ok, result.failure
    # 总览紧跟在趋势后面（不是趋势后面那张的后面）。
    assert _root_ids(result.definition) == [
        "region_trend",
        "region_overview",
        "region_ranking",
    ]


def test_n2_reverse_move_index_is_correct():
    """N2 反向移动：「把总览移到排名后面」→ 下标 2。

    推导（根层顺序 ``[总览(0), 趋势(1), 排名(2)]``）：

    - 源卡 = 总览(0)，参照卡 = 排名(2)；
    - 源卡在参照卡前面 → 摘出源卡后参照卡下标从 2 前移到 **1**；
    - "移到后面" → 下标 = 1 + 1 = **2**。
    """
    definition = _three_cards()

    normalized = normalize_presentation_operations(
        [
            {
                "op": "MOVE_REGION",
                "params": {
                    "regionId": "region_overview",
                    "afterRegionId": "region_ranking",
                },
            }
        ],
        definition,
    )

    assert normalized[0]["params"]["index"] == 2

    result = apply_presentation(definition, normalized)
    assert result.ok, result.failure
    assert _root_ids(result.definition) == [
        "region_trend",
        "region_ranking",
        "region_overview",
    ]


def test_n2_relative_move_into_a_container_resolves_target_group():
    """N2：参照卡在**分组里** → 目标分组就是那个分组 id。"""
    definition, group_id = _grouped()

    normalized = normalize_presentation_operations(
        [
            {
                "op": "MOVE_REGION",
                "params": {
                    "regionId": "region_ranking",
                    "afterRegionId": "region_trend",
                },
            }
        ],
        definition,
    )

    params = normalized[0]["params"]
    assert "afterRegionId" not in params
    assert params["targetGroupId"] == group_id
    # 组内顺序是 [总览, 趋势] → 摘出源卡（本就在组外）后参照卡下标仍是 1
    # → "移到后面" = 下标 2。
    assert params["index"] == 2

    result = apply_presentation(definition, normalized)
    assert result.ok, result.failure
    assert _child_ids(result.definition, group_id) == [
        "region_overview",
        "region_trend",
        "region_ranking",
    ]


def test_n2_unknown_target_is_rejected_before_execution():
    """N2 负向：参照卡不存在 → ``unknown_region``，且**不原样透传**。"""
    definition = _three_cards()

    with pytest.raises(PresentationError) as excinfo:
        normalize_presentation_operations(
            [
                {
                    "op": "MOVE_REGION",
                    "params": {
                        "regionId": "region_ranking",
                        "afterRegionId": "region_not_exists",
                    },
                }
            ],
            definition,
        )

    assert excinfo.value.reason == "unknown_region"

    # 预检同样拦住：这一批**进不了候选**，执行器拿不到 afterRegionId。
    result = preflight_presentation(
        definition,
        [
            {
                "op": "MOVE_REGION",
                "params": {
                    "regionId": "region_ranking",
                    "afterRegionId": "region_not_exists",
                },
            }
        ],
    )
    assert result.ok is False
    assert result.reason == "unknown_region"
    assert result.failure


def test_n2_ambiguous_target_is_rejected():
    """N2 负向：同一个 id 出现多次 → ``ambiguous_target``（不自己挑一个）。"""
    definition = _three_cards()
    payload = definition.model_dump()
    # 复制一份趋势卡（同一个 id 出现两次）。
    payload["regions"].append(
        {
            "id": "region_trend",
            "name": "趋势（重复）",
            "role": "TREND",
            "component": {"type": "CHART"},
        }
    )
    ambiguous = ReportDefinitionDraft.model_validate(payload)

    with pytest.raises(PresentationError) as excinfo:
        normalize_presentation_operations(
            [
                {
                    "op": "MOVE_REGION",
                    "params": {
                        "regionId": "region_ranking",
                        "afterRegionId": "region_trend",
                    },
                }
            ],
            ambiguous,
        )

    assert excinfo.value.reason == "ambiguous_target"


# ==========================================================================
# N3：placement 里出现排列字段 = 层级错
# ==========================================================================


def test_n3_placement_with_columns_on_container_becomes_group_arrangement():
    """N3 正向：目标是**容器** → 整条转成 ``SET_GROUP_ARRANGEMENT``。"""
    definition, group_id = _grouped()

    normalized = normalize_presentation_operations(
        [
            {
                "op": "SET_PLACEMENT",
                "params": {
                    "regionId": group_id,
                    "placement": {"type": "GRID", "columns": 2, "gap": 20},
                },
            }
        ],
        definition,
    )

    assert normalized[0]["op"] == "SET_GROUP_ARRANGEMENT"
    assert normalized[0]["params"]["groupId"] == group_id
    arrangement = normalized[0]["params"]["arrangement"]
    assert arrangement["columns"] == 2
    assert arrangement["gap"] == {"value": 20, "unit": "PX"}
    assert arrangement["mode"] == "GRID"


def test_n3_placement_with_columns_on_leaf_card_is_rejected():
    """N3 负向：目标是**叶子卡** → ``arrangement_not_placement``，不许静默删字段。"""
    definition = _three_cards()

    with pytest.raises(PresentationError) as excinfo:
        normalize_presentation_operations(
            [
                {
                    "op": "SET_PLACEMENT",
                    "params": {
                        "regionId": "region_trend",
                        "placement": {"type": "GRID", "column": 1, "columns": 2},
                    },
                }
            ],
            definition,
        )

    assert excinfo.value.reason == "arrangement_not_placement"
    # 报错文案要**点名那个放错层级的字段**，不能悄悄把它删掉。
    assert "columns" in excinfo.value.message

    # 预检也拦得住（候选期就不出）。
    result = preflight_presentation(
        definition,
        [
            {
                "op": "SET_PLACEMENT",
                "params": {
                    "regionId": "region_trend",
                    "placement": {"type": "GRID", "column": 1, "columns": 2},
                },
            }
        ],
    )
    assert result.ok is False
    assert result.reason == "arrangement_not_placement"


# ==========================================================================
# N4 / N5 / N6 / N7：排列、主题、格式、未知字段
# ==========================================================================


def test_n4_unknown_arrangement_key_is_rejected():
    """N4：排列参数里的未知键报错。"""
    definition = _three_cards()

    with pytest.raises(PresentationError) as excinfo:
        normalize_presentation_operations(
            [
                {
                    "op": "SET_GROUP_ARRANGEMENT",
                    "params": {
                        "groupId": "group_1",
                        "arrangement": {"mode": "GRID", "columns": 2, "spacing": 20},
                    },
                }
            ],
            definition,
        )

    assert excinfo.value.reason == "invalid_operation"
    assert "spacing" in excinfo.value.message


def test_n4_columns_out_of_range_is_rejected():
    """N4：列数必须落在 1..12。"""
    definition = _three_cards()

    with pytest.raises(PresentationError) as excinfo:
        normalize_presentation_operations(
            [
                {
                    "op": "SET_GROUP_ARRANGEMENT",
                    "params": {
                        "groupId": "group_1",
                        "arrangement": {"mode": "GRID", "columns": 13},
                    },
                }
            ],
            definition,
        )

    assert excinfo.value.reason == "out_of_range"


def test_n5_theme_name_resolves_to_real_theme_id():
    """N5：「暗色」→ 真实 themeId（主题只认主题服务里的那一份）。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    dark_id = _theme_id_by_name("深色主题")
    if dark_id is None:
        pytest.skip("主题服务里没有「深色主题」，本用例跳过")

    normalized = normalize_presentation_operations(
        [{"op": "SET_THEME", "params": {"themeId": "暗色"}}], definition
    )

    assert normalized[0]["params"]["themeId"] == dark_id

    # 真实 id 原样保留（规范化是幂等的）。
    again = normalize_presentation_operations(
        [{"op": "SET_THEME", "params": {"themeId": dark_id}}], definition
    )
    assert again[0]["params"]["themeId"] == dark_id


def test_n5_unknown_theme_name_is_rejected():
    """N5 负向：解析不到的主题 → ``unknown_theme``。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    with pytest.raises(PresentationError) as excinfo:
        normalize_presentation_operations(
            [{"op": "SET_THEME", "params": {"themeId": "不存在的颜色主题"}}], definition
        )

    assert excinfo.value.reason == "unknown_theme"

    result = preflight_presentation(
        definition,
        [{"op": "SET_THEME", "params": {"themeId": "不存在的颜色主题"}}],
    )
    assert result.ok is False
    assert result.reason == "unknown_theme"


def test_n6_unknown_format_key_is_rejected():
    """N6：``SET_FORMAT`` 只认 ``ALLOWED_FORMAT_KEYS``。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    with pytest.raises(PresentationError) as excinfo:
        normalize_presentation_operations(
            [
                {
                    "op": "SET_FORMAT",
                    "params": {
                        "regionId": "region_total_energy",
                        "format": {"precision": 2, "decimals": 2},
                    },
                }
            ],
            definition,
        )

    assert excinfo.value.reason == "invalid_operation"
    assert "decimals" in excinfo.value.message


def test_n7_unknown_op_and_unknown_param_key_are_still_rejected():
    """N7：未知 op / 未知参数键**仍然报错** —— 规范化不放宽校验。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    with pytest.raises(PresentationError) as op_error:
        normalize_presentation_operations(
            [{"op": "SET_EVERYTHING", "params": {}}], definition
        )
    assert op_error.value.reason == "invalid_operation"

    with pytest.raises(PresentationError) as param_error:
        normalize_presentation_operations(
            [
                {
                    "op": "SET_STYLE",
                    "params": {
                        "regionId": "region_total_energy",
                        "style": {"fontSizeX": {"value": 1, "unit": "PX"}},
                    },
                }
            ],
            definition,
        )
    assert param_error.value.reason == "invalid_operation"
    assert "fontSizeX" in param_error.value.message


def test_normalization_is_idempotent():
    """规范化**幂等**：对规范化结果再跑一遍，结果不变。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    operations = [
        {
            "op": "SET_STYLE",
            "params": {
                "regionId": "region_total_energy",
                "style": {"fontSize": "28px", "borderRadius": 4},
            },
        },
        {
            "op": "MOVE_REGION",
            "params": {
                "regionId": "region_device_ranking",
                "afterRegionId": "region_hourly_trend",
            },
        },
        {"op": "SET_THEME", "params": {"themeId": "暗色"}},
    ]

    once = normalize_presentation_operations(operations, definition)
    twice = normalize_presentation_operations(once, definition)

    assert once == twice


# ==========================================================================
# 预检：无副作用
# ==========================================================================


def test_preflight_does_not_touch_the_definition():
    """预检**无副作用**：传入的定义一个字节都不许变。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    before = copy.deepcopy(definition.model_dump())

    result = preflight_presentation(
        definition,
        [
            {
                "op": "SET_STYLE",
                "params": {
                    "regionId": "region_total_energy",
                    "style": {"fontSize": 28, "textAlign": "RIGHT"},
                },
            }
        ],
    )

    assert result.ok is True
    assert definition.model_dump() == before
    # 规范化后的那一批就是将来要执行的那一批。
    assert result.operations[0]["params"]["style"]["fontSize"] == {
        "value": 28,
        "unit": "PX",
    }
    assert result.affectedRegionIds == ["region_total_energy"]
    assert result.changes


def test_preflight_returns_normalized_operations_not_the_input():
    """成功时 ``operations`` 是**规范化后**的那一份（不是入参那一份）。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    operations = [
        {
            "op": "SET_STYLE",
            "params": {
                "regionId": "region_total_energy",
                "style": {"fontSize": 28},
            },
        }
    ]

    result = preflight_presentation(definition, operations)

    assert result.ok is True
    assert result.operations != operations
    assert result.operations[0]["params"]["style"]["fontSize"] == {
        "value": 28,
        "unit": "PX",
    }
    # 入参那一批**没有被就地改写**。
    assert operations[0]["params"]["style"]["fontSize"] == 28


def test_normalization_does_not_mutate_the_callers_operations():
    """规范化**不改调用方传进来的那一批**（深拷贝上算完就还回去）。

    候选摘要是从规范化后的那一份生成的 —— 如果规范化顺手改了入参，
    "摘要与执行是同一份数据"这条接缝就糊了。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    operations = [
        {
            "op": "SET_STYLE",
            "params": {
                "regionId": "region_total_energy",
                "style": {"fontSize": "28px"},
            },
        },
        {
            "op": "MOVE_REGION",
            "params": {
                "regionId": "region_device_ranking",
                "afterRegionId": "region_hourly_trend",
            },
        },
    ]
    before = copy.deepcopy(operations)

    normalize_presentation_operations(operations, definition)

    assert operations == before, "入参那一批不能被就地改写"


def test_candidate_operations_and_executed_operations_are_the_same_data():
    """§1.4 接缝：候选里带的那一批 == 执行器真正跑的那一批。

    预检返回的 ``operations`` 拿去执行，得到的定义与摘要里的参数一致；
    并且执行器记录的变更（ChangeSet.after）与规范化后的参数**逐字相符**。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    operations = [
        {
            "op": "SET_STYLE",
            "params": {
                "regionId": "region_total_energy",
                "style": {"fontSize": 28, "textAlign": "RIGHT"},
            },
        }
    ]

    preflight = preflight_presentation(definition, operations)
    assert preflight.ok is True, preflight.failure

    executed = apply_presentation(definition, preflight.operations)
    assert executed.ok is True, executed.failure

    # 变更记录里的落盘值 == 候选里承诺的值。
    style_change = next(
        item for item in executed.changeSet.changed if item.path.endswith("/style")
    )
    assert style_change.after["fontSize"] == {"value": 28, "unit": "PX"}
    assert style_change.after["fontSize"] == preflight.operations[0]["params"]["style"][
        "fontSize"
    ]


def test_raw_plan_with_after_region_id_normalizes_into_the_executable_index():
    """规则 / 模型给出的**原始**计划（带 afterRegionId）经规范化后可执行。

    引擎的相对移动规则产出的就是 ``{"op": "MOVE_REGION",
    "params": {"regionId": ..., "afterRegionId": ...}}`` 这种形状 ——
    规范化层必须把它换成执行器认的 ``index``，而且换出来的顺序要对。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    raw = [
        {
            "op": "MOVE_REGION",
            "params": {
                "regionId": "region_device_ranking",
                "afterRegionId": "region_hourly_trend",
            },
        }
    ]

    normalized = normalize_presentation_operations(raw, definition)
    params = normalized[0]["params"]
    assert "afterRegionId" not in params
    assert isinstance(params["index"], int)

    result = apply_presentation(definition, normalized)
    assert result.ok is True, result.failure
    root_ids = [str(item.get("id")) for item in result.definition.model_dump()["regions"]]
    # 排名紧跟在趋势后面。
    assert root_ids.index("region_device_ranking") == root_ids.index(
        "region_hourly_trend"
    ) + 1


def test_preflight_failure_leaves_no_half_applied_state():
    """一批里第一条合法、第二条非法 → 整批失败，定义**不变**。

    第二条的 ``gap`` 写成 **200 像素**：形状合法（N1 会规范成
    ``{"value": 200, "unit": "PX"}``），但**越界**（间距上限 96）——
    所以它是"执行期不合法"，不会被规范化救回来。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    before = copy.deepcopy(definition.model_dump())

    result = preflight_presentation(
        definition,
        [
            {
                "op": "SET_STYLE",
                "params": {
                    "regionId": "region_total_energy",
                    "style": {"borderRadius": 4},
                },
            },
            {
                "op": "CREATE_GROUP",
                "params": {
                    "groupId": "group_1",
                    "name": "能耗概览",
                    "memberRegionIds": ["region_total_energy", "region_hourly_trend"],
                    "arrangement": {"mode": "GRID", "columns": 2, "gap": 200},
                },
            },
        ],
    )

    assert result.ok is False
    assert result.failure
    assert result.reason == "out_of_range"
    assert "gap" in result.failure
    # 第一条那点改动**不能留下**。
    assert definition.model_dump() == before


def test_preflight_rejects_illegal_placement_without_touching_definition():
    """非法 placement → 预检失败，定义不变。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    before = copy.deepcopy(definition.model_dump())

    result = preflight_presentation(
        definition,
        [
            {
                "op": "SET_PLACEMENT",
                "params": {
                    "regionId": "region_total_energy",
                    "placement": {"type": "GRID", "column": 1, "columnSpan": 99},
                },
            }
        ],
    )

    assert result.ok is False
    assert "columnSpan" in (result.failure or "")
    assert definition.model_dump() == before


def test_preflight_never_raises_even_when_the_theme_service_fails(monkeypatch):
    """预检是**闸门**：它抛异常，整个请求就炸了。

    规范化 N5 要读主题服务（按名字解析 themeId），那一步可能碰到
    **存储层**的异常（数据库不可用等）。预检必须把它变成
    ``ok=False`` + 一句澄清，而不是把异常甩给调用方。

    （这条是候选期的安全网：宁可"这批暂时没法校验"，也不要 500。）
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    before = copy.deepcopy(definition.model_dump())

    class _BrokenThemeService:
        def list_themes(self):
            raise RuntimeError("主题存储不可用")

    monkeypatch.setattr(rps, "theme_service", _BrokenThemeService())

    result = preflight_presentation(
        definition,
        [{"op": "SET_THEME", "params": {"themeId": "暗色"}}],
    )

    assert result.ok is False
    assert result.failure
    assert result.reason
    # 预检失败**不改**调用方的定义。
    assert definition.model_dump() == before


# ==========================================================================
# 执行器：直接吃 afterRegionId（不经过规范化层）
# ==========================================================================


def test_executor_accepts_after_region_id_directly():
    """执行器**直接**接受 ``afterRegionId``（绕过规范化层也不报错）。"""
    definition = _three_cards()
    before_ids = _root_ids(definition)

    result = apply_presentation(
        definition,
        [
            {
                "op": "MOVE_REGION",
                "params": {
                    "regionId": "region_ranking",
                    "afterRegionId": "region_trend",
                },
            }
        ],
    )

    assert result.ok is True, result.failure
    # 排名紧跟在趋势后面。
    assert _root_ids(result.definition) == [
        "region_overview",
        "region_trend",
        "region_ranking",
    ]
    # 卡片 id 与数量一个都不许变。
    assert len(_root_ids(result.definition)) == len(before_ids)
    assert set(_root_ids(result.definition)) == set(before_ids)


def test_executor_rejects_unknown_after_region_id_without_changing_state():
    """执行器遇到不存在的参照卡 → 整批失败，定义**不变**。"""
    definition = _three_cards()
    before = copy.deepcopy(definition.model_dump())

    result = apply_presentation(
        definition,
        [
            {
                "op": "MOVE_REGION",
                "params": {
                    "regionId": "region_ranking",
                    "afterRegionId": "region_not_exists",
                },
            }
        ],
    )

    assert result.ok is False
    assert result.reason == "unknown_region"
    assert result.definition.model_dump() == before


def test_container_target_is_still_a_container_after_normalization():
    """规范化不改变对象类型：容器还是容器。"""
    definition, group_id = _grouped()

    normalized = normalize_presentation_operations(
        [
            {
                "op": "SET_PLACEMENT",
                "params": {
                    "regionId": group_id,
                    "placement": {"type": "GRID", "columns": 2, "gap": 20},
                },
            }
        ],
        definition,
    )

    result = apply_presentation(definition, normalized)
    assert result.ok is True, result.failure
    payload = result.definition.model_dump()
    group = next(
        item for item in payload["regions"] if str(item.get("id")) == group_id
    )
    assert is_container(group)
    assert group["component"]["arrangement"]["columns"] == 2
    assert group["component"]["arrangement"]["gap"] == {"value": 20, "unit": "PX"}
