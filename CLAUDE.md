# 数据分析Agent 项目约定

## 提交策略（自动提交）
- 完成一个功能/修复、且**相关测试通过**后，**自动提交**，无需每次询问用户。
- 一个逻辑单元一个 commit；message 用中文简要描述（`feat:` / `fix:` / `test:` / `docs:` 前缀）。
- 提交前先跑相关测试确认通过（后端：`cd backend && PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m pytest tests/ -q`）。
- 不把半成品提交成独立 commit；若中途要保存进度，用 `wip:` 前缀。

## 后端（backend/）
- 测试从 `backend/` 目录运行（cwd=backend，sys.path 含 backend）。
- 所有网络/LLM 测试用 mock，禁止真实请求进单元测试。
- LLM 配置走 `config.py` 的 `LLM_API_KEY / LLM_BASE_URL / LLM_MODEL`。
- 运行生成物不入库：`jd_session/`（京东登录会话）、`eval_report.json`（评测报告）已 gitignore。

## 项目结构速览
- `agent/`：NL2SQL 工作流（schema 检索 → LLM 理解 → SQL 生成 → 四道校验闸门 → 执行+结果校验）
- `agent/conversation_memory.py`：对话记忆（话题自适应窗口 + LLM topic_id 语义分组）
- `agent/crawler.py`：共享爬虫（Amazon curl_cffi + 京东登录会话）
- `agent/market_intelligence/`：选品/商品研究流水线
- `db/executor.py`：SQL 执行；`eval_agent.py`：100 条评测集
