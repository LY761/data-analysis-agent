# 电商问数记忆与流式输出评测记录

## 当前实现

- 标准 SSE：`message.start`、`route.selected`、`retrieval.start`、`retrieval.complete`、`answer.delta`、`message.completed`、`message.error`。
- 兼容事件：暂保留 `status`、`answer`、`result`、`error`、`done`，避免现有 Web 前端升级期间中断。
- 短期记忆：SQLite 持久化会话轮次，窗口默认 20 轮，支持主题识别、增量追问和历史搜索。
- 当前限制：尚未实现与法律项目同级的受控长期偏好记忆、TTL/重要性衰减和用户主动删除接口。

## 已验证用例

| 用例 | 结果 | 证据 |
| --- | --- | --- |
| 流式查询写入检索指标 | 通过 | `backend/tests/test_metrics_flow.py::test_query_stream_writes_metrics` |
| 标准开始/路由/检索/结束事件 | 通过 | 同上 |
| 旧 SSE 事件兼容 | 通过 | 同上及现有 Web 前端 |
| 主题切换与追问继承 | 通过 | `backend/tests/test_conversation_topic.py` |

## 当前基线

- 全量后端测试：108/108 通过。
- CI 使用确定性 Hashing Embedding，不依赖运行时下载 HuggingFace 模型。
- 尚未形成首 Token、P95、Token 降幅和长期记忆准确率数字，不写入正式简历。

## 复现命令

```powershell
$env:EMBEDDING_BACKEND='hashing'
$env:CHROMA_PERSIST_DIR='.pytest_cache/chroma-ci'
.\.venv\Scripts\python.exe -m pytest backend/tests/test_metrics_flow.py backend/tests/test_conversation_topic.py -q
```
