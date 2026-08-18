# 电商数据分析 Agent 简历证据

> 只记录已完成且可通过代码、测试或评测报告复现的能力。计划中的功能不得写成已实现。

## 已验证基线

- 100 条内部路由评测准确率 100%。
- SQL 安全允许/阻止评测准确率 100%。
- 支持 Web、REST 与 MCP 接入。
- 后端自动化测试 108/108 通过。
- 动态切换 SQLite 数据源后，文件摄取、SQL 查询、Schema 构建与 SQL 标识符校验共享同一运行时数据源。
- Schema 检索支持 SentenceTransformer 与确定性 Hashing 双后端，CI 无需下载外部模型也能复现 108 项测试。
- 电商问数 SSE 已与法律 RAG 统一为路由、检索、增量答案、完成和结构化错误事件，并保留旧事件兼容。

## 可复现命令

- 全量测试：`.venv\\Scripts\\python.exe -m pytest backend/tests -q`
- 数据接入专项：`.venv\\Scripts\\python.exe -m pytest backend/tests/test_data_ingest.py -q`

## 待优化后补充

- 全量自动化测试结果。
- 端到端 NL2SQL Result Accuracy。
- 多轮记忆成功率与 Token 降幅。
- SSE 首 Token/P95 延迟。
- MCP 工具和 HITL 演示证据。
