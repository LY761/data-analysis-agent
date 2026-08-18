# 电商数据分析 Agent 优化日志

## 基线

- 日期：2026-08-18
- 自动化测试：优化前首次运行 106 通过、1 失败；单测重跑存在偶发通过
- 内部评测：100 条路由用例准确率 100%，SQL 安全用例 100%
- 路由延迟：P95 约 1858ms，最慢约 24s
- 已知问题：CSV 摄取、连接管理器和测试使用的数据源状态不一致

## EDA-001 数据摄取数据库一致性

- 日期：2026-08-18
- 状态：completed
- 问题与根因：运行时切换数据库后，部分模块通过 `from config import DEMO_DB_PATH` 保留陈旧路径引用。
- 优化前基线：全量测试 106/107。
- 采用方案：以 `ExecutorProxy` 作为运行时业务数据源唯一事实来源；数据摄取、动态 Schema 与 SQL 校验均读取当前执行器状态；Schema 缓存增加数据库路径隔离。
- 未采用方案及原因：未继续运行时修改 `config.DEMO_DB_PATH`，因为 `from config import ...` 会形成不可控快照，且会把业务数据源与会话、缓存、监控等系统存储耦合。
- 修改文件：`backend/db/executor.py`、`backend/services/data_ingest.py`、`backend/db/init_db.py`、`backend/agent/sql_validator.py`、`backend/db/connection_manager.py`、`backend/tests/test_data_ingest.py`。
- 新增或更新测试：新增“切换 SQLite 后上传、查询、Schema 同库”回归用例。
- 测试结果：专项测试 25/25 通过；全量后端测试 108/108 通过。
- 指标变化：全量回归从首次 106/107 提升为稳定 108/108，并新增 1 条跨库一致性测试。
- 安全与兼容性影响：MySQL 模式下明确拒绝 SQLite 专用的文件自动建表，避免误写默认库；SQLite 原有接口保持兼容。
- 回滚方式：回退上述 6 个代码/测试文件；无需数据迁移。
- Git commit：待记录。
- 简历可用指标：108 项后端自动化测试全通过；动态数据源切换后上传、查询和 Schema 检索链路一致。
- 指标复现命令：`.venv\\Scripts\\python.exe -m pytest backend/tests -q`。
- 证据文件或报告链接：`docs/RESUME_EVIDENCE.md`、`backend/tests/test_data_ingest.py`。
- 面试可讲的技术取舍：配置快照与运行时数据源状态的边界。
- 简历 Bullet 候选：重构多数据源运行时状态管理，以执行器作为业务库唯一事实来源，解决 CSV 摄取、Schema 索引与 SQL 校验跨库不一致问题，并通过 108 项回归测试。
- 后续事项：EDA-002 CI 与质量门。

## EDA-002 CI 与质量门

- 日期：2026-08-18
- 状态：completed
- 问题与根因：仓库缺少自动化质量门，依赖完整性、语法错误、密钥泄漏和回归测试依赖人工发现。
- 采用方案：GitHub Actions 使用 Python 3.11，执行 Gitleaks、依赖安装、`pip check`、`compileall` 和全量 Pytest。
- 未采用方案及原因：暂未加入前端构建，当前前端为静态资源且无独立包管理配置。
- 修改文件：`.github/workflows/ci.yml`、`backend/requirements-dev.txt`。
- 测试结果：本地 `compileall`、`pip check` 和 108 项测试通过；远程 CI 以 GitHub Actions 运行结果为准。
- 安全与兼容性影响：CI 只读取仓库内容；密钥通过环境变量注入，不写入版本库。
- 简历 Bullet 候选：为 Agent 项目建立包含密钥扫描、依赖校验、静态编译与 108 项回归测试的 GitHub Actions 质量门。
- 后续事项：在 M1 增加端到端评测阈值和报告制品。
