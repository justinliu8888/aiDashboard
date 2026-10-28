"""Agent B 专属：前端状态机 / 渲染可靠性回归用的本地 Mock 后端。

为什么需要它：
    真后端的 context_engine 每次都要打真实模型（单轮 10~100s），
    交互流跑不起来也跑不稳。前端回归要的是**状态序列可复现**，不是模型质量。

它同时干两件事：
1. 把项目目录当静态站点发出来 —— /frontend/pages/workbench.html 就是真实页面，
   没有改一行 HTML，测的就是线上那一份。
2. 实现会话、对话流、预览三个接口，按固定脚本返回状态序列，
   让 DEFINITION_DRAFT_READY / RENDERING / READY 这些关键状态可以稳定复现。

用法：
    python3 flows/support/agent_b_mock_server.py 8100
    # 然后让交互流指向 http://127.0.0.1:8100/frontend/pages/workbench.html
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]

# 首轮故意慢一点：交互流要能在「生成中」这个时间窗里点「新建对话」，
# 才能验证新旧会话不串数据。
FIRST_TURN_DELAY_SECONDS = 2.5

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


# ---------------------------------------------------------------- 数据

def make_definition(revision: int = 1) -> dict:
    """右侧草稿面板要渲染的报表定义（含 generationInputs）。"""
    return {
        "schemaVersion": "1.0",
        "metadata": {
            "id": "def_mock",
            "name": "A 机房昨日能耗日报",
            "revision": revision,
            "status": "DRAFT",
            # 系统暂选 / 用户指定：前端右侧草稿面板要展示这两个来源
            "generationInputs": {
                "room": {"value": "A 机房", "source": "用户指定"},
                "time": {"value": "昨日", "source": "用户指定"},
                "metric": {"value": "总耗电", "source": "系统暂选"},
            },
        },
        "datasets": {
            "ds_room_energy": {
                "id": "ds_room_energy",
                "name": "机房能耗日汇总",
                "sourceRef": "catalog.machine_room.energy",
            }
        },
        "regions": [
            {
                "id": "region_report_title",
                "name": "A 机房 昨日能耗日报",
                "role": "REPORT_TITLE",
                "component": {"type": "TEXT", "content": {"template": "A 机房 昨日能耗日报"}},
            },
            {
                "id": "region_total_energy",
                "name": "昨日总耗电",
                "role": "SUMMARY",
                "data": {"inputs": {"main": {"type": "DATASET", "datasetRef": "ds_room_energy"}}},
                "component": {
                    "type": "METRIC_CARD",
                    "metric": {"label": "昨日总耗电", "fieldRef": "energyKwh", "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"}},
                },
            },
            {
                "id": "region_device_ranking",
                "name": "设备耗电排名",
                "role": "DETAIL",
                "data": {"inputs": {"main": {"type": "DATASET", "datasetRef": "ds_room_energy"}}},
                "component": {
                    "type": "TABLE",
                    "columns": [
                        {"fieldRef": "deviceName", "label": "设备"},
                        {"fieldRef": "energyKwh", "label": "耗电", "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"}},
                    ],
                },
            },
            {
                "id": "region_hourly_trend",
                "name": "小时趋势",
                "role": "TREND",
                "data": {"inputs": {"main": {"type": "DATASET", "datasetRef": "ds_room_energy"}}},
                "context": {"timeWindow": {"value": {"unit": "HOUR", "offset": -24, "count": 24}}},
                "component": {"type": "CHART", "chartKind": "LINE"},
            },
            {
                "id": "region_data_quality",
                "name": "数据质量",
                "role": "QUALITY",
                "component": {"type": "DATA_QUALITY", "fields": ["energyKwh", "deviceName"]},
            },
            {
                "id": "region_analysis",
                "name": "分析说明",
                "role": "ANALYSIS",
                "component": {"type": "ANALYSIS", "goal": "说明能耗结构", "mode": "TEMPLATE", "promptTemplateRef": "pt_analysis"},
            },
        ],
    }


def make_change_set() -> dict:
    return {
        "baseDefinitionRevision": 1,
        "changed": [
            {
                "regionId": "region_hourly_trend",
                "path": "/regions/region_hourly_trend/context/timeWindow",
                "after": {"mode": "REPLACE", "value": {"unit": "DAY", "offset": -7, "count": 7}},
                "summary": "趋势图窗口改为近 7 个完整自然日",
            }
        ],
        "unchanged": [{"regionId": "region_total_energy", "summary": "总耗电卡片仍使用昨日窗口"}],
        "impact": [{"regionId": "region_hourly_trend", "note": "需要重新解析窗口并重取趋势数据"}],
    }


def make_preview_payload() -> dict:
    """RENDERING 用的预览数据：里面有一张 CHART，用来验证图表库兜底。"""
    return {
        "reportRunId": "run_mock_001",
        "reportName": "A 机房昨日能耗日报",
        "logicalDate": "2026-10-25",
        "snapshot": {"roomName": "A 机房", "totalEnergyKwh": 1234.5, "yoyPercent": -3.2},
        "anomalies": [{"timeRange": "2026-10-25 09:00~10:00", "note": "采集有缺口"}],
        "regions": [
            {
                "regionId": "region_total_energy",
                "name": "昨日总耗电",
                "componentType": "METRIC_CARD",
                "payload": {"label": "昨日总耗电", "value": 1234.5, "unit": "kWh"},
            },
            {
                "regionId": "region_device_ranking",
                "name": "设备耗电排名",
                "componentType": "TABLE",
                "payload": {
                    "rowNumber": True,
                    "columns": [
                        {"fieldRef": "deviceName", "label": "设备"},
                        {"fieldRef": "energyKwh", "label": "耗电", "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"}},
                    ],
                    "rows": [
                        {"deviceName": "CRAC-01", "energyKwh": 320.4},
                        {"deviceName": "CRAC-02", "energyKwh": 288.1},
                    ],
                },
            },
            {
                "regionId": "region_hourly_trend",
                "name": "小时趋势",
                "componentType": "CHART",
                "payload": {"chartKind": "LINE", "xLabels": ["00:00", "01:00", "02:00"], "yValues": [12.3, 15.6, 11.4]},
            },
            {
                "regionId": "region_data_quality",
                "name": "数据质量",
                "componentType": "DATA_QUALITY",
                "payload": {"coverage": 0.98, "missing": 2, "duplicates": 0, "freshnessSeconds": 42},
            },
            {
                "regionId": "region_analysis",
                "name": "分析说明",
                "componentType": "ANALYSIS",
                "payload": {"analysis": "夜间时段占比偏高，可以关注空调启停策略。"},
            },
        ],
    }


CONFIRM_CHANGE_QUESTION = {
    "id": "confirm_change",
    "prompt": "请对修改方案做出选择：",
    "expects": "SINGLE_CHOICE",
    "options": [
        {"value": "同意", "label": "同意"},
        {"value": "修改", "label": "修改"},
        {"value": "重新开始", "label": "重新开始"},
    ],
    # 和业务 Agent 的 confirm_change 一样：不带 selectionOnly。
    # 后端因此不会存 pending_choice，前端必须自己把锁补上，否则下拉框是死的。
    "selectionOnly": False,
    "allowRestart": False,
}

TEMPLATE_EDIT_CONFIRM_QUESTION = {
    "id": "template_edit_confirm",
    "prompt": "请确认当前编辑后的草稿：",
    "expects": "SINGLE_CHOICE",
    "options": [
        {"value": "同意", "label": "同意"},
        {"value": "修改", "label": "修改"},
        {"value": "重新开始", "label": "重新开始"},
    ],
    "selectionOnly": True,
    "allowRestart": False,
}

READY_QUESTION = {
    "id": "ready_action",
    "prompt": "报表已就绪，请选择下一步：",
    "expects": "SINGLE_CHOICE",
    "options": [
        {"value": "继续修改", "label": "继续修改"},
        {"value": "确认并保存", "label": "确认并保存"},
    ],
    "selectionOnly": True,
    "allowRestart": False,
}


# ---------------------------------------------------------------- 会话状态

class Store:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sessions: dict[str, dict] = {}
        self.counter = 0

    def new_session(self) -> str:
        with self.lock:
            self.counter += 1
            session_id = f"sess_mock_{self.counter:03d}"
            self.sessions[session_id] = {
                "sessionId": session_id,
                "title": "新对话",
                "state": "ANALYZING",
                "turn": 0,
                "messages": [],
                "pendingChoice": None,
                "snapshot": {},
                "designSessionId": None,
                "reportDefinitionId": None,
                "workspaceId": None,
                "advanceCalls": 0,
            }
            return session_id

    def list(self, keyword: str = "") -> list[dict]:
        items = []
        for record in self.sessions.values():
            if keyword and keyword not in record["title"]:
                continue
            items.append(
                {
                    "sessionId": record["sessionId"],
                    "title": record["title"],
                    "state": record["state"],
                    "workspaceId": record["workspaceId"],
                    "messageCount": len(record["messages"]),
                    "createdAt": "2026-10-26T09:00:00",
                    "updatedAt": "2026-10-26T09:00:00",
                }
            )
        return items

    def detail(self, session_id: str) -> dict | None:
        record = self.sessions.get(session_id)
        if not record:
            return None
        return {
            "sessionId": record["sessionId"],
            "title": record["title"],
            "state": record["state"],
            "workspaceId": record["workspaceId"],
            "designSessionId": record["designSessionId"],
            "reportDefinitionId": record["reportDefinitionId"],
            "previewRunId": (record["snapshot"].get("lastReportRun") or {}).get("reportRunId"),
            "messageCount": len(record["messages"]),
            "snapshot": record["snapshot"],
            "pendingChoice": record["pendingChoice"],
            "pendingEditPlan": None,
            "messages": record["messages"],
            "advanceCalls": record.get("advanceCalls", 0),
            "createdAt": "2026-10-26T09:00:00",
            "updatedAt": "2026-10-26T09:00:00",
        }


STORE = Store()


def _remember_choice(record: dict, response: dict) -> None:
    """和真后端 AgentService._remember_choice 一个口径：
    只有 selectionOnly 且带选项的问题才存选择锁，否则清掉。
    """
    choice = None
    for question in response.get("questions") or []:
        if question.get("selectionOnly") and question.get("options"):
            choice = question
            break
    if choice:
        record["pendingChoice"] = {"state": response["state"], "question": dict(choice)}
    else:
        record["pendingChoice"] = None


def build_response(record: dict, message: str, save_pending: bool) -> dict:
    """按脚本返回下一个状态。"""
    turn = record["turn"]
    record["turn"] += 1

    if turn == 0:
        record["title"] = message or "新对话"
        record["state"] = "DEFINITION_DRAFT_READY"
        record["designSessionId"] = "design_mock_001"
        record["reportDefinitionId"] = "def_mock"
        definition = make_definition(1)
        record["snapshot"] = {
            "definition": definition,
            "policy": {"reportName": definition["metadata"]["name"]},
            "lastReportRun": None,
            "previewPayload": None,
        }
        response = {
            "state": "DEFINITION_DRAFT_READY",
            "reply": "我会把趋势图改为近 7 个完整自然日，总耗电卡片仍按昨日统计。",
            "designSessionId": record["designSessionId"],
            "domainObjects": {
                "reportDefinitionDraft": definition,
                "changeSet": make_change_set(),
            },
            "questions": [dict(CONFIRM_CHANGE_QUESTION)],
            "uiActions": ["show_diff", "accept_change", "reject_change"],
            "elapsedMs": 120,
        }
        # 消息里带「带锁」→ 强制存 pending_choice；
        # 否则按真实后端的规矩来：只有 selectionOnly 的问题才存锁。
        if save_pending:
            response["questions"] = [dict(CONFIRM_CHANGE_QUESTION, selectionOnly=True)]
        _remember_choice(record, response)
        return response

    if turn == 1:
        # 用户点了「同意」：进入保存草稿
        record["state"] = "SAVE_DEFINITION_DRAFT"
        record["snapshot"]["definition"] = make_definition(2)
        response = {
            "state": "SAVE_DEFINITION_DRAFT",
            "reply": "草稿已保存，接下来进入校验。",
            "designSessionId": record["designSessionId"],
            "domainObjects": {"reportDefinitionDraft": record["snapshot"]["definition"]},
            "toolRequests": [
                {"id": "req_save_1", "type": "SAVE_DEFINITION_DRAFT", "target": "JavaReportService",
                 "arguments": {"definitionId": "def_mock", "revision": 2}}
            ],
            "elapsedMs": 90,
        }
        _remember_choice(record, response)
        return response

    if turn == 2:
        record["state"] = "VALIDATING"
        response = {"state": "VALIDATING", "reply": "正在校验报表定义…", "elapsedMs": 80}
        _remember_choice(record, response)
        return response

    if turn == 3:
        record["state"] = "PREVIEWING"
        response = {"state": "PREVIEWING", "reply": "正在跑预览…", "elapsedMs": 70}
        _remember_choice(record, response)
        return response

    if turn == 4:
        record["state"] = "PREVIEW_READY"
        record["snapshot"]["lastReportRun"] = {"reportRunId": "run_mock_001"}
        response = {
            "state": "PREVIEW_READY",
            "reply": "预览已就绪。",
            "domainObjects": {"reportRunPreview": {"reportRunId": "run_mock_001"}},
            "elapsedMs": 60,
        }
        _remember_choice(record, response)
        return response

    if turn == 5:
        record["state"] = "RENDERING"
        record["snapshot"]["previewPayload"] = make_preview_payload()
        response = {"state": "RENDERING", "reply": "正在渲染报表界面…", "elapsedMs": 50}
        _remember_choice(record, response)
        return response

    record["state"] = "READY"
    response = {
        "state": "READY",
        "reply": "报表已就绪。",
        "questions": [READY_QUESTION],
        "elapsedMs": 40,
    }
    _remember_choice(record, response)
    return response


# ---------------------------------------------------------------- 只读问答

# 只读问答的判定（前端无关，只为了让交互流可复现）：
#   疑问句            → 只读事实回答
#   含糊说法          → 只读意图澄清
#   含「读取失败」     → 读取不可得，答「无法确认」
#   其余              → 走原来的业务脚本
CLARIFY_TRIGGERS = {"继续弄一下", "继续一下", "再弄一下", "弄一下"}
READ_FAIL_MARKERS = ("读取失败", "查不到服务端")


def classify_user_message(text: str) -> str:
    stripped = text.strip()
    if stripped in CLARIFY_TRIGGERS:
        return "clarify"
    if any(marker in stripped for marker in READ_FAIL_MARKERS):
        return "read_fail"
    if stripped.endswith("吗") or stripped.endswith("?") or stripped.endswith("？"):
        return "query"
    return "business"


def build_read_only_response(record: dict, kind: str) -> dict:
    """只读问答的响应：只回答，不推进业务。

    形状对齐后端共用契约（docs/adr/0002）：
      turnKind = "query_answer"          → 前端据此判定「本轮不推进」
      uiActions = ["query_answer"]        → 同义标记
      state 原样保留（业务状态不变）
      questions = 原待确认菜单（后端原样带回）
      toolRequests = []                   → 不发起任何执行动作
      debug.queryOutcome = answer | clarification

    关键约束：
      - 不带 domainObjects 的改动（不改草稿、不改预览）
      - 不动 pendingChoice（原待确认操作保留）
      - 不消耗 turn（不占用业务脚本的推进位）
    """
    state = record["state"]
    definition = record["snapshot"].get("definition") or {}
    metadata = definition.get("metadata") or {}

    # 后端契约：查询轮把原待确认菜单原样带回
    preserved_menu = []
    if record.get("pendingChoice"):
        preserved_menu = [record["pendingChoice"]["question"]]
    elif state == "WAITING_USER_CONFIRM":
        preserved_menu = [TEMPLATE_EDIT_CONFIRM_QUESTION]

    if kind == "clarify":
        return {
            "state": state,
            "reply": "我想先确认一下你要问什么，免得答错对象。",
            "turnKind": "query_answer",
            "uiActions": ["query_answer"],
            "questions": preserved_menu,
            "toolRequests": [],
            "debug": {"queryMode": True, "queryOutcome": "clarification", "needsClarification": True},
            "elapsedMs": 30,
        }

    if kind == "read_fail":
        # 读取不可得：答「无法确认」，不能答「没有」
        return {
            "state": state,
            "reply": "我现在没法确认当前对话的草稿状态，暂时不能给你准确答复。",
            "turnKind": "query_answer",
            "uiActions": ["query_answer"],
            "questions": preserved_menu,
            "toolRequests": [],
            "debug": {"queryMode": True, "queryOutcome": "answer", "factStatus": "UNAVAILABLE"},
            "elapsedMs": 30,
        }

    if metadata.get("name"):
        reply = (
            f"当前对话里有一份草稿「{metadata['name']}」"
            f"（revision {metadata.get('revision')}），还没有确认。下一步请从上面的菜单里选怎么处理。"
        )
    else:
        reply = "当前对话里没有报表草稿。"
    return {
        "state": state,
        "reply": reply,
        "turnKind": "query_answer",
        "uiActions": ["query_answer"],
        "questions": preserved_menu,
        "toolRequests": [],
        "debug": {"queryMode": True, "queryOutcome": "answer", "factStatus": "FOUND"},
        "elapsedMs": 30,
    }


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "AgentBMock/1.0"

    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    # ---- 工具 ----
    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str):
        rel = path.lstrip("/") or "index.html"
        target = (ROOT / rel).resolve()
        if not str(target).startswith(str(ROOT.resolve())) or not target.is_file():
            self._send_json({"detail": "not found"}, 404)
            return
        self._send_bytes(target.read_bytes(), MIME.get(target.suffix, "application/octet-stream"))

    # ---- 路由 ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            self._send_json({"status": "ok"})
            return
        if path == "/api/sessions":
            keyword = (query.get("q") or [""])[0]
            self._send_json({"items": STORE.list(keyword), "total": len(STORE.list(keyword))})
            return
        if path.startswith("/api/sessions/"):
            detail = STORE.detail(path.rsplit("/", 1)[-1])
            if not detail:
                self._send_json({"detail": "会话不存在"}, 404)
                return
            self._send_json(detail)
            return
        if path.startswith("/api/report/preview/"):
            self._send_json(make_preview_payload())
            return
        self._serve_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}

        if path == "/api/sessions":
            session_id = STORE.new_session()
            self._send_json({"sessionId": session_id, "title": "新对话", "state": "ANALYZING", "messageCount": 0})
            return

        if path == "/api/agent/chat/stream" or path == "/api/agent/chat":
            message = str(payload.get("message") or "")
            session_id = str(payload.get("sessionId") or "") or STORE.new_session()
            record = STORE.sessions.get(session_id)
            if record is None:
                session_id = STORE.new_session()
                record = STORE.sessions[session_id]
            # 首轮慢一点，交互流才抓得住「生成中」这个时间窗
            if record["turn"] == 0 and message != "__advance__":
                time.sleep(FIRST_TURN_DELAY_SECONDS)
            # 消息里带「带锁」→ 后端存 pending_choice；否则不存（模拟真实 AgentScope 的 confirm_change）
            save_pending = "带锁" in message
            if message.strip() == "__advance__":
                record["advanceCalls"] = record.get("advanceCalls", 0) + 1

            # 只读问答：走独立分支，不消耗业务脚本的推进位，也不改任何业务对象
            kind = classify_user_message(message)
            if kind in ("query", "clarify", "read_fail"):
                response = build_read_only_response(record, kind)
            else:
                response = build_response(record, message, save_pending)
            # 消息里带「不存锁」→ 后端不保存 pending_choice，
            # 用来验证前端「后端没存锁时靠回放恢复菜单、且不恢复已作答菜单」的兜底逻辑。
            if "不存锁" in message:
                record["pendingChoice"] = None
            # 和真后端一致：__advance__ 是内部控制消息，不作为 user 消息入库
            if message.strip() != "__advance__":
                record["messages"].append(
                    {"seq": len(record["messages"]) + 1, "role": "user", "content": message, "payload": None, "createdAt": ""}
                )
            record["messages"].append(
                {"seq": len(record["messages"]) + 1, "role": "assistant", "content": response.get("reply", ""),
                 "payload": response, "createdAt": ""}
            )
            if path == "/api/agent/chat/stream":
                body = ("event: final\ndata: " + json.dumps(response, ensure_ascii=False) + "\n\n").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_json(response)
            return

        self._send_json({"detail": "not found"}, 404)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8100
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"agent-b mock backend on http://127.0.0.1:{port} (root={ROOT})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
