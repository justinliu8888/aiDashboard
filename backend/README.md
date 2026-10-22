# 智能报表 AI 后端

第一版只做：**生成机房耗电量报表草稿**。

## 启动

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

后端请求与阶段日志默认写入 `~/Library/Logs/haewu-ai-report/backend.log`，
单文件最多 10 MB，保留 5 份轮转文件。可用 `AI_BACKEND_LOG_PATH`
指定其他路径；`conversationId` 和 `resumeOf` 可用于关联停止前后的两次请求。

## 接口

| 接口 | 作用 |
| --- | --- |
| `GET /health` | 健康检查。 |
| `POST /api/agent/chat` | AI 对话入口，第一版返回机房耗电量报表草稿。 |
| `GET /api/reports/machine-room-energy/dataset` | 查看机房耗电量假数据集。 |
| `GET /api/reports/machine-room-energy/definition` | 查看基于 ReportDefinition 1.0 简化生成的报表定义。 |

## 示例请求

```bash
curl -X POST http://127.0.0.1:8000/api/agent/chat \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":"conv_001","user_id":"user_001","scene":"report_draft","message":"生成机房耗电量报表","context":{"project_id":"project_001","room_id":"room_a"}}'
```

## 当前边界

- 使用 `FakeReportAgent`，暂不接真实 AgentScope。
- 使用本地假数据集，暂不连接 Java 后端和真实数据库。
- `ReportDefinition` 只覆盖第一版需要的标题、指标卡、趋势图、排名表、分析说明和数据质量区域。
- 结构参考 `原始文件/report-definition-v1.md`，但没有实现完整 1.0 协议校验。
