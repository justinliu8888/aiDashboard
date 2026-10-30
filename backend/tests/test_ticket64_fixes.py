"""工单 #64 验收后的修复项（F1–F4、F9）。

#64 已合并，独立验收结论 PARTIAL（无阻塞项），留下 4 个中危 + 若干低危。
本文件覆盖本票修掉的部分：

- **F1**：契约表 §4.3 登记的数组位置写法 ``@arrayPath data.records``
  （**无冒号**）不被识别 → 报「报文里有多个数组」；
- **F2**：处理规则的 rule→区域靠 ``split("_")`` 反解，列 id 含下划线时
  规则被丢给**幻影区域**；
- **F3**：用户 JSON 模式下聊天回复仍说「模拟业务数据」；
- **F3bis**：用户可见的失败文案带**上游异常原文**（#61 遗留）；
- **F4**：不可求和字段退回 PASSTHROUGH 时，原因没写进数据问题说明；
- **F9**：``is_summable_field`` 的裸子串匹配误伤（``grid`` / ``notify`` /
  ``postcode``）。

本文件只做**单元与接口层**断言；每条用例都对应验收报告里的一条复现。
"""
from __future__ import annotations

import json
from typing import Any

from app.services import json_input_service as json_svc
from app.services import routing_rules


# --------------------------------------------------------------------------
# F1 —— 数组位置标记：契约登记的两种写法都要认
# --------------------------------------------------------------------------


#: 契约表 §4.3 登记的写法 + 常见的等价写法。冒号可省。
_ARRAY_PATH_FORMS: tuple[str, ...] = (
    "数组位置: data.records",
    "@arrayPath data.records",
    "@arrayPath: data.records",
    "arrayPath: data.records",
    "数组位置：data.records",
)


def test_array_path_marker_accepts_every_contract_form() -> None:
    """契约表 §4.3 的两种写法都被认出来（**含无冒号那一种**）。"""
    for form in _ARRAY_PATH_FORMS:
        assert routing_rules.extract_array_path(form) == "data.records", form


def test_array_path_marker_without_colon_selects_the_array() -> None:
    """无冒号写法要真的定位到数组，而不是报「有多个数组」。"""
    payload = {
        "data": {"records": [{"energy": 1.0}, {"energy": 2.0}]},
        "other": [{"noise": 1}],
    }
    text = json.dumps(payload, ensure_ascii=False)

    for form in _ARRAY_PATH_FORMS:
        message = f"这是昨天的数据\n{form}\n{text}"
        array_path = routing_rules.extract_array_path(message)
        assert array_path == "data.records", form
        outcome = json_svc.validate_json_payload(
            routing_rules.detect_json_payload(message),
            array_path=array_path,
        )
        assert outcome.ok, (form, outcome.error)
        assert outcome.rows == [{"energy": 1.0}, {"energy": 2.0}], form


def test_array_path_marker_is_ignored_when_absent() -> None:
    """没有标记行 → 仍然返回 None（不猜）。"""
    assert routing_rules.extract_array_path("这是昨天的数据") is None
    assert routing_rules.extract_array_path("数组位置") is None
    assert routing_rules.extract_array_path("arrayPath:") is None


# --------------------------------------------------------------------------
# F2 —— 处理规则的归属：显式解析，不靠拆字符串猜
# --------------------------------------------------------------------------


def _metric_card_definition(region_id: str, column_id: str) -> dict[str, Any]:
    return {
        "regions": [
            {
                "id": region_id,
                "role": "SUMMARY",
                "component": {
                    "type": "METRIC_CARD",
                    "metric": {"id": column_id, "label": "总耗电", "fieldRef": column_id},
                    "columns": [
                        {"id": column_id, "label": "总耗电", "fieldRef": column_id}
                    ],
                },
            }
        ]
    }


def test_rule_with_underscore_column_id_does_not_land_in_a_phantom_region() -> None:
    """列 id 含下划线时，规则落到**真实区域**，不落到幻影区域。

    旧实现按 ``split("_")`` 反解：``rule_region_2_total_energy_sum`` 被拆成
    区域 ``region_2_total`` —— 这个区域**根本不存在**，规则被静默丢掉，
    汇总口径不生效也没有提示。
    """
    from app.services.effective_combination import normalize_combination
    from app.services.json_payload_service import (
        _rules_by_region,
        build_user_json_payloads,
    )

    combination = normalize_combination(
        {
            "sourceMode": "user_json",
            "inputId": "inp_1",
            "inputVersion": 1,
            "fieldMapping": [
                {
                    "regionId": "region_2",
                    "columnId": "total_energy",
                    "inputFieldKey": "energy",
                }
            ],
            "processingRules": [
                {
                    "ruleId": "rule_region_2_total_energy_sum",
                    "operation": "SUM",
                    "targetFieldKey": "energy",
                    "params": {},
                }
            ],
        }
    )

    grouped = _rules_by_region(
        combination, region_ids=["region_1", "region_2", "region_3"]
    )
    assert sorted(grouped.keys()) == ["region_2"]
    assert "region_2_total" not in grouped

    payloads = build_user_json_payloads(
        definition=_metric_card_definition("region_2", "total_energy"),
        rows=[{"energy": 1.0}, {"energy": 2.0}],
        combination=combination,
    )
    assert sorted(payloads.keys()) == ["region_2"]
    # 规则真的生效了：按全部行求和，而不是显示第一行的值。
    assert payloads["region_2"]["value"] == 3.0


def test_rule_region_resolution_is_unchanged_for_plain_ids() -> None:
    """不带下划线的列 id：归属与旧实现一致（不引入行为变化）。"""
    from app.services.effective_combination import normalize_combination
    from app.services.json_payload_service import _rules_by_region

    combination = normalize_combination(
        {
            "sourceMode": "user_json",
            "inputId": "inp_1",
            "inputVersion": 1,
            "fieldMapping": [
                {
                    "regionId": "region_1",
                    "columnId": "energyKwh",
                    "inputFieldKey": "energyKwh",
                }
            ],
            "processingRules": [
                {
                    "ruleId": "rule_region_1_energykwh_sum",
                    "operation": "SUM",
                    "targetFieldKey": "energyKwh",
                    "params": {},
                }
            ],
        }
    )
    grouped = _rules_by_region(combination, region_ids=["region_1"])
    assert list(grouped.keys()) == ["region_1"]


def test_unresolvable_rule_is_dropped_instead_of_guessing_a_region() -> None:
    """认不出归属的规则**不执行**，也不落到任何区域（更不落到幻影区域）。"""
    from app.services.effective_combination import normalize_combination
    from app.services.json_payload_service import _rules_by_region

    combination = normalize_combination(
        {
            "sourceMode": "user_json",
            "inputId": "inp_1",
            "inputVersion": 1,
            "fieldMapping": [
                {"regionId": "region_1", "columnId": "energy", "inputFieldKey": "energy"}
            ],
            "processingRules": [
                {
                    "ruleId": "rule_mystery_zone_col_sum",
                    "operation": "SUM",
                    "targetFieldKey": "energy",
                    "params": {},
                }
            ],
        }
    )
    grouped = _rules_by_region(combination, region_ids=["region_1"])
    assert grouped == {}
