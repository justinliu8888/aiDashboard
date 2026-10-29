"""模板态的试运行结果不落库 —— 含**历史遗留**数据的沿用路径。

规格 #46：「模拟业务数据只存在于响应与内存，不写入任何持久化存储」；
「已有历史数据不迁移、不删改」；「**新写入**适用上述规则」。

本文件盯的是**沿用**这条路：``SessionService._carry_over_business_objects``
会把「本轮没带回来」的业务对象从上一轮快照里搬过来。如果不区分来源，
模板态会话里**历史遗留**的模拟 ``previewPayload`` 会被一轮又一轮地
写回持久化存储 —— 那不是「不删改历史」，那是继续写。
"""

from __future__ import annotations

from app.services.session_service import SessionService


def _template_definition() -> dict:
    """一份模板态的定义（带 templateBinding）。"""
    return {
        "id": "def_hist_001",
        "revision": 1,
        "metadata": {
            "id": "def_hist_001",
            "revision": 1,
            "templateBinding": {
                "definitionId": "rplan_example",
                "definitionVersion": 1,
            },
        },
        "regions": [],
    }


def test_template_mode_does_not_carry_over_a_legacy_preview_payload() -> None:
    """模板态：库里那份历史遗留的模拟试运行结果**不能**被沿用写回。"""
    previous = {
        "definition": _template_definition(),
        "previewPayload": {
            "reportRunId": "run_old",
            "snapshot": {"totalEnergyKwh": 37357.8},
            "regions": [
                {
                    "regionId": "region_1",
                    "payload": {"value": 37357.8, "unit": "kWh"},
                }
            ],
        },
    }
    snapshot = {"definition": _template_definition()}

    carried = SessionService._carry_over_business_objects(snapshot, previous)

    assert carried is not None
    assert "previewPayload" not in carried


def test_non_template_mode_still_carries_over_the_preview_payload() -> None:
    """非模板态：试运行结果的原语义不变（这一键仍承载真实试运行产物）。"""
    previous = {
        "definition": {"id": "def_real_001", "revision": 3, "metadata": {"id": "def_real_001", "revision": 3}},
        "previewPayload": {
            "reportRunId": "run_real",
            "snapshot": {"totalEnergyKwh": 306.1},
        },
    }
    snapshot = {"definition": previous["definition"]}

    carried = SessionService._carry_over_business_objects(snapshot, previous)

    assert carried is not None
    assert carried["previewPayload"]["reportRunId"] == "run_real"


def test_other_business_objects_are_still_carried_over_in_template_mode() -> None:
    """沿用能力本身不能被这次收口误伤：草稿、策略、运行标识照旧沿用。"""
    previous = {
        "definition": _template_definition(),
        "policy": {"name": "策略"},
        "lastReportRun": {"reportRunId": "run_1"},
        "designPhase": "DRAFT_READY",
        "reportPlan": {"id": "plan_1"},
    }
    snapshot: dict = {}

    carried = SessionService._carry_over_business_objects(snapshot, previous)

    assert carried is not None
    assert carried["policy"] == {"name": "策略"}
    assert carried["lastReportRun"] == {"reportRunId": "run_1"}
    assert carried["designPhase"] == "DRAFT_READY"
    assert carried["reportPlan"] == {"id": "plan_1"}
