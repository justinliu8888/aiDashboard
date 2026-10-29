"""工单 41 / GitHub #41：网格拖拽能力下线的**公开入口守卫**。

#37 下线网格拖拽时把 ``backend/tests/test_template_layout_api.py`` 整文件删掉了
（连同它那两条用例）。于是「``PATCH /api/sessions/{id}/template-layout`` 这个
入口不存在」这件事**没有任何测试守着** —— 将来有人把路由加回来，没人会发现。

本文件**只**守这一件事：网格布局写回入口整条下线。

为什么必须是 404 而不是 405
--------------------------

路由存在但方法不对，FastAPI 会回 **405 Method Not Allowed**；路由**根本不存在**
才回 **404 Not Found**。所以断 404 断的是「这条路由没了」，而不是「这条路只
支持别的动词」—— 这正是需求稿 §2.4 / §3.1 要的下线口径。

夹具与污染控制
==============

本文件不碰设计流程，只读公开 HTTP 接口，不写库。``backend/data/haewu.db``
全程不会被打开写入。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


#: 被下线的网格布局写回路径。
GRID_LAYOUT_PATH = "/api/sessions/sess_tpl_1/template-layout"


def test_grid_layout_route_is_gone_for_patch_get_and_post(client: TestClient):
    """断言 10：网格布局写回入口整条下线 —— PATCH / GET / POST 都是 404。

    404 = 路由不存在（405 才是「路由在、动词不对」）。这条是「新链路里网格
    确实下线了」的**唯一**自动化守卫。
    """
    for method in ("patch", "get", "post"):
        response = getattr(client, method)(GRID_LAYOUT_PATH)
        assert response.status_code == 404, (
            f"{method.upper()} {GRID_LAYOUT_PATH} 应当 404（路由不存在），"
            f"实际 {response.status_code}"
        )
        assert response.json() == {"detail": "Not Found"}


def test_grid_layout_route_is_absent_from_the_openapi_contract(client: TestClient):
    """断言 10：公开接口契约里已经没有这条路径。

    OpenAPI 是对外的接口契约；网格路径留在契约里，就等于告诉调用方「这个
    接口还在」，哪怕路由碰巧 404。
    """
    paths = app.openapi()["paths"]

    assert not [path for path in paths if "template-layout" in path]
    assert "/api/sessions/{session_id}/template-layout" not in paths


def test_grid_layout_response_carries_no_grid_coordinates(client: TestClient):
    """断言 10：网格入口的响应里没有 12 列网格坐标字段。

    需求稿 §2.4 明确不再出现「12 列网格线、网格容器、拖拽手柄、落位坐标」。
    断的是**这个接口的响应体**里不再出现网格坐标字段。

    ⚠️ 不要把它扩大成「整个草稿里没有 columnSpan」：草稿区域里的
    ``layout.placement``（column / columnSpan / rowSpan）是 ``faf94a2`` 起就
    存在的**既有字段**，需求稿 §3.3 明写「不属于网格」，不许动。
    """
    response = client.patch(GRID_LAYOUT_PATH)
    assert response.status_code == 404

    body = response.json()
    assert body == {"detail": "Not Found"}
    for grid_field in ("placement", "columnSpan", "rowSpan", "gridItems", "items"):
        assert grid_field not in body
