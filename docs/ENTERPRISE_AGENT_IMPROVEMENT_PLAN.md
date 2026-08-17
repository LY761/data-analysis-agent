# 电商数据分析 Agent 企业级规范审计与整改方案

## 1. 结论

项目已经具备 Agent 应用的主要骨架：LangGraph NL2SQL、Schema 检索、四层 SQL 校验、确定性指标服务、版本化报告、工作流幂等与任务去重、能力注册表、REST/MCP 和内部评测。

当前竞争力不足的主要原因不是“功能少”，而是企业级边界和工程规范没有统一：

- API、SSE、MCP、工作流各自返回不同结构。
- LLM 调用散落，缺少统一模型网关和成本策略。
- 权限、租户、缓存和数据源上下文没有形成闭环。
- 兜底逻辑存在，但分散在各模块，无法统一观测和评测。
- Schema 检索只返回表和字段，表关系和指标语义没有标准契约。
- 旧知识库、爬虫、竞品和旧 BI 接口干扰主项目定位。
- 部分“企业级能力”目前只是代码存在，实际没有生效。

整改原则：先修安全和数据边界，再统一协议与模型调用，之后优化检索、延迟和 Token，最后做渠道接入。

## 2. 当前值得保留的能力

### 2.1 Agent

- AgentRouter 负责意图路由。
- LangGraph 负责问题理解、Schema 检索、SQL 生成、校验、修复、执行和结果解释。
- 对模糊问题支持澄清。
- SQL 生成失败支持有限次数修复。
- SSE 支持流式进度和回答。

### 2.2 确定性服务

- 店铺经营指标。
- 商品和 SKU 诊断。
- 店铺经营诊断。
- 数据质量检查。
- 经营驾驶舱。
- 版本化报告和不可变快照。
- 指标语义层和来源证据。

### 2.3 工作流

- 运营日报。
- 销售下降预警。
- 低库存任务。
- 商品风险复核。
- 滞销复核。
- 幂等键、步骤日志、失败重试、任务及告警去重。

这些模块应该成为主项目，不应为了“全都用 Agent”而改成 Agent。确定性指标、权限、状态流转、风险评分和审批必须继续保持确定性。

## 3. P0：必须先修的安全与正确性问题

### 3.1 认证中间件实际上可被全部绕过

位置：backend/middleware/auth_middleware.py:185、backend/middleware/auth_middleware.py:201。

PUBLIC_PATHS 包含根路径 /，判断使用 path.startswith。所有 API 路径都以 / 开头，因此开启 AUTH_ENABLED 后仍会被当成公开路径。

整改：

- 公开路径使用精确匹配。
- 仅对静态资源目录使用前缀匹配。
- 增加认证开启后的受保护接口集成测试。
- 生产环境 AUTH_ENABLED 默认必须为 true，缺少密钥时启动失败。

### 3.2 数据脱敏没有真正作用于问数结果

位置：backend/services/data_masking.py:69、backend/api/routes.py:286、backend/api/routes.py:291。

mask_result 要求输入包含 success=true，但 QueryResponse 的 response_data 没有 success 字段，因此直接返回原始数据。并且数据先写缓存，再尝试脱敏，缓存内保存的是未脱敏结果。

整改：

1. 脱敏放到数据库结果进入任何缓存、日志、Trace 和 LLM 之前。
2. 脱敏服务接收明确的数据模型，不能依赖可选 success 字段。
3. 缓存只保存权限过滤和脱敏后的结果。
4. 增加手机号、邮箱、地址、银行卡及自定义敏感列的端到端测试。

### 3.3 租户 ID 由调用者直接指定

多个接口允许用户传 tenant_id，但没有把它绑定到 current_user_ctx。认证用户可以尝试传入其他租户 ID。

整改：

- TenantContext 由认证中间件创建。
- 普通用户不能从请求体覆盖 tenant_id。
- 平台管理员跨租户操作必须显式授权并记录审计。
- 所有 Repository 方法强制接收 TenantContext，不接收裸字符串。
- 测试必须覆盖同一资源 ID 在不同租户间不可见。

### 3.4 查询缓存存在跨租户和跨权限复用风险

位置：backend/cache/query_cache.py:43。

当前缓存键只有 question 和 schema_hash，没有 tenant、用户权限、数据源、指标版本、Prompt 版本和模型版本。

整改后的缓存键：

tenant_id + datasource_id + permission_scope_hash + normalized_question +
schema_version + metric_version + prompt_version + model_policy_version。

缓存分层：

- L1：进程内短 TTL，仅存无敏感元数据。
- L2：Redis，按租户命名空间。
- 数据发生更新时按 snapshot_id 或 cache_tag 失效。
- 不能缓存高风险错误、权限拒绝或未经脱敏的数据。

### 3.5 数据库切换是全局状态

位置：backend/db/connection_manager.py:21、backend/db/connection_manager.py:75。

_current_db 和全局 ExecutorProxy 会让一个用户切换数据库后影响其他并发用户。

整改：

- 数据源由 RequestContext 指定。
- ExecutorFactory 按 tenant_id 和 datasource_id 返回连接池。
- 禁止修改全局当前数据库。
- Schema 索引按 tenant_id、datasource_id、schema_version 隔离。

### 3.6 SQLite 查询超时没有真正生效

位置：backend/db/executor.py:54。

PRAGMA query_timeout 不是可靠的 SQLite 查询执行超时机制。

整改：

- SQLite 使用 progress_handler 检查截止时间并中断。
- MySQL/PostgreSQL 使用数据库原生 statement timeout。
- 查询运行在受控线程池，设置并发上限。
- 超时错误使用 QUERY_TIMEOUT 标准错误码。
- 连接使用只读账户；SQLite 使用只读 URI 和 authorizer 双重限制。

### 3.7 表权限只定义、未接入执行链

check_table_permission 只存在于 auth/jwt_handler.py，没有进入 Schema 检索、SQL 校验和执行器。

整改：

- Schema 检索前过滤无权限表。
- SQL AST 解析后再次检查真实表权限。
- 执行器只接收验证后的 QueryPlan。
- Trace 记录 permission_scope_hash，不记录完整权限内容。

### 3.8 Langfuse 初始化逻辑不可达

位置：backend/tracing/tracer.py:19、backend/tracing/tracer.py:31。

_langfuse_available 初始为 false，_get_langfuse 在首次调用时直接返回，实际不会进入初始化。

整改：

- 使用三态 initialized/uninitialized/unavailable。
- 启动时执行一次连接检查。
- Trace 失败不得影响业务，但必须暴露 degraded 状态。
- Trace 输入输出先脱敏，再按字段白名单记录。

## 4. 目标架构

~~~mermaid
flowchart LR
    Client["Web / REST / MCP"] --> Gateway["API Gateway"]
    Gateway --> Context["RequestContext
用户/租户/数据源/权限/Trace"]
    Context --> Router["Decision Router"]
    Router --> Fast["规则与语义指标快路径"]
    Router --> Agent["LangGraph NL2SQL Agent"]
    Router --> Workflow["确定性工作流"]
    Agent --> Schema["Schema Hybrid Retrieval"]
    Agent --> Tools["Tool Registry"]
    Tools --> Policy["Policy Enforcement"]
    Policy --> Executor["Read-only Query Executor"]
    Executor --> Result["Result Normalizer + Masking"]
    Result --> Answer["Deterministic / LLM Presenter"]
    Workflow --> Outbox["Task / Alert / Webhook Outbox"]
    Answer --> Envelope["Unified Response Envelope"]
    Outbox --> Envelope
    Envelope --> Client
    Agent --> Trace["Tracing / Cost / Eval"]
    Workflow --> Trace
~~~

核心上下文统一为 RequestContext：

- request_id。
- trace_id。
- tenant_id。
- user_id。
- role 和 permission_scope_hash。
- datasource_id。
- channel。
- locale。
- deadline。
- model_policy。
- dry_run。
- idempotency_key。

## 5. 输入与输出规范

### 5.1 统一成功响应

所有 REST、SSE 最终 result 事件和 MCP structured output 使用同一业务结构。

~~~json
{
  "schema_version": "1.0",
  "request_id": "req_xxx",
  "trace_id": "trace_xxx",
  "status": "success",
  "scenario": "nl2sql",
  "data": {},
  "answer": {
    "text": "本月销售额为……",
    "mode": "deterministic",
    "confidence": 0.94
  },
  "evidence": [
    {
      "type": "database_query",
      "source_id": "sales_db",
      "snapshot_id": "snap_xxx",
      "query_hash": "sha256:...",
      "tables": ["orders"]
    }
  ],
  "tool_calls": [
    {
      "tool": "execute_readonly_sql",
      "status": "success",
      "source": "internal",
      "latency_ms": 82
    }
  ],
  "fallback": {
    "used": false,
    "level": "none",
    "reason": ""
  },
  "warnings": [],
  "meta": {
    "latency_ms": 1240,
    "token_usage": {
      "input": 620,
      "output": 138
    },
    "cache": "miss",
    "model": "policy:nl2sql-primary"
  }
}
~~~

### 5.2 统一错误响应

~~~json
{
  "schema_version": "1.0",
  "request_id": "req_xxx",
  "trace_id": "trace_xxx",
  "status": "error",
  "error": {
    "code": "SCHEMA_NOT_FOUND",
    "message": "没有找到相关数据表，请补充业务对象。",
    "retryable": false,
    "stage": "schema_retrieval",
    "details": {}
  },
  "fallback": {
    "used": true,
    "level": "keyword_only",
    "reason": "vector_store_unavailable"
  }
}
~~~

### 5.3 错误码分类

- AUTH_REQUIRED、FORBIDDEN、TENANT_MISMATCH。
- INVALID_INPUT、UNSUPPORTED_SCENARIO。
- MODEL_TIMEOUT、MODEL_RATE_LIMITED、MODEL_UNAVAILABLE。
- SCHEMA_NOT_FOUND、SCHEMA_VERSION_MISMATCH。
- SQL_GENERATION_FAILED、SQL_VALIDATION_FAILED、QUERY_TIMEOUT。
- TOOL_NOT_FOUND、TOOL_PERMISSION_DENIED、TOOL_EXECUTION_FAILED。
- DATA_SOURCE_UNAVAILABLE、NO_DATA。
- WORKFLOW_CONFLICT、IDEMPOTENCY_CONFLICT、APPROVAL_REQUIRED。
- INTERNAL_ERROR。

不得直接把 Python 异常文本和数据库错误返回给前端。

### 5.4 SSE 事件规范

- accepted：返回 request_id 和 trace_id。
- stage：仅返回可公开的阶段、进度和耗时。
- tool_start。
- tool_result：只返回摘要和 evidence_id。
- delta：用户可见文本。
- result：完整统一响应。
- error：统一错误响应。
- done。

不展示模型隐式思维链，只展示可审计的决策、工具、来源和阶段。

## 6. Agent 决策与 Token/延迟策略

### 6.1 决策矩阵

| 场景 | 处理方式 | LLM 调用 | 目标延迟 |
| --- | --- | --- | --- |
| 问候、帮助、能力说明 | 规则 | 0 | P95 < 100ms |
| 固定 KPI、快捷指标 | 语义指标服务/预写 SQL | 0 | P95 < 300ms |
| 驾驶舱、异常列表 | 确定性聚合服务 | 0 | P95 < 800ms |
| 日报、低库存、销售下降 | 工作流 | 默认 0；总结可选 1 | P95 < 3s |
| 明确单表问数 | 规则路由 + Schema 快路径 + NL2SQL | 1 | P95 < 4s |
| 明确多表问数 | 混合 Schema 检索 + NL2SQL | 1 | P95 < 5s |
| 模糊问题 | 澄清 Agent | 1，暂不生成 SQL | P95 < 2s |
| “为什么下降”类分析 | 查询后分析 Agent | 2 | P95 < 8s |
| SQL 校验失败 | 修复 Agent | 每次 1，最多 1 次 | 受控 |
| 图表推荐 | 规则 | 0 | P95 < 50ms |
| 普通结果口语化 | 模板 | 0 | P95 < 20ms |
| 跨指标复杂总结 | LLM Presenter | 1 | P95 < 3s |

### 6.2 当前重复调用问题

当前一次问数可能经过：

1. AgentRouter LLM。
2. QueryClarifier LLM。
3. SQLGenerator LLM。
4. SQL 修复 LLM，最多两次。
5. 结果口语化 LLM。
6. “为什么”分析 LLM。

建议改为：

- Router 对明显数据问题只走规则，不调用 LLM。
- Clarifier 合并意图、槽位、改写和澄清判断，一次完成。
- 明确问题跳过 Clarifier，直接进入 Schema 检索。
- SQL 生成同时返回 query_plan 和 explanation。
- 普通结果使用确定性模板，不再调用口语化模型。
- 只有复杂归因和多指标综合才调用 Presenter。
- SQL 修复最多一次；第二次失败转澄清或人工 SQL。

### 6.3 Token 预算

| 调用 | 输入上限 | 输出上限 |
| --- | --- | --- |
| 路由/澄清 | 1200 | 180 |
| SQL 生成 | 3500 | 450 |
| SQL 修复 | 2500 | 350 |
| 复杂归因 | 5000 | 600 |
| 报告摘要 | 6000 | 800 |

约束：

- 不把完整数据库 Schema 发送给模型。
- 最多发送 4 张表、8—12 个重点字段和必要连接关系。
- 原始查询结果最多发送 20 行；优先传聚合事实。
- 对话历史按结构化摘要保存，不直接拼接完整历史。
- Prompt、模型和 Schema 版本参与缓存键。
- 每个请求设置 cost_budget 和 deadline。

### 6.4 模型策略

建立统一 LLMGateway，所有模块禁止直接实例化 OpenAI Client。

LLMGateway 负责：

- 按任务选择模型。
- 超时、重试、指数退避和 Provider 级熔断。
- Token 预算。
- JSON Schema 输出校验。
- 统一日志和成本统计。
- 敏感字段过滤。
- 主模型和备用模型切换。
- 幂等请求和取消传播。

建议策略：

- router/slot：便宜快速模型。
- nl2sql：主推理模型。
- presenter：便宜模型或确定性模板。
- evaluation_judge：与生产模型隔离。
- provider 不可用：回退规则/模板，不盲目换模型继续生成。

## 7. 兜底规范

### 7.1 分层兜底

| 失败点 | 第一兜底 | 第二兜底 | 最终行为 |
| --- | --- | --- | --- |
| Router 模型失败 | 规则路由 | 能力范围提示 | 不把无关问题送入 SQL |
| Schema 向量失败 | 关键词/BM25 | 精确表字段匹配 | 无证据则澄清 |
| Schema 召回不足 | 扩展同义词和 Join 图 | 增加 top_k | 仍不足则澄清 |
| SQL 生成失败 | 模板 SQL | 一次修复 | 返回标准错误 |
| SQL 校验失败 | 带阶段原因修复一次 | 降级预写指标 | 不执行未验证 SQL |
| 查询超时 | 取消查询 | 提示缩小范围 | 不自动无限重试 |
| 图表失败 | 表格 | 文本摘要 | 保留原始数据证据 |
| 结果解释失败 | 确定性模板 | 原始指标卡 | 不影响查询结果 |
| 报告摘要失败 | 结构化报告 | 无 LLM 摘要 | 报告仍可生成 |
| 工作流动作失败 | 幂等重试 | Dead Letter/人工任务 | 保留步骤审计 |
| 渠道推送失败 | Outbox 重试 | 告警和人工重发 | 不回滚已完成分析 |

### 7.2 兜底必须可见

响应中必须返回：

- fallback.used。
- fallback.level。
- fallback.reason。
- degraded_capabilities。
- retry_count。

Trace 和评测中统计每种兜底发生率，不能只在日志中写一句 warning。

## 8. Schema 检索整改

### 8.1 当前状态

当前 schema_retriever.py 的策略是：

1. 关键词检索。
2. 只要命中任意表就直接返回。
3. 完全没有命中才运行向量检索。

问题：

- 单字和 bigram 容易产生弱关键词误命中。
- 弱命中会阻断向量检索。
- 跨表问题可能缺少中间连接表。
- 输出只有 tables 和 columns。
- SQLGenerator 的表关系是硬编码的。

### 8.2 推荐策略

不是“向量优先、关键词兜底”，而是置信度分层：

1. 精确命中表名、字段名或标准指标，且问题明显为单表：关键词快路径。
2. 其余问题：BM25/关键词与向量并行召回。
3. 使用 RRF 或加权重排合并结果。
4. 通过 Join Graph 补齐中间表。
5. 向量服务失败时才降级为关键词。
6. 低于阈值不继续生成 SQL，转澄清。

这样兼顾速度、精确关键词和语义同义词。

### 8.3 结构化检索契约

SchemaSearchResult 应包含：

- tables：表名、用途、DDL 摘要、分数、召回来源。
- columns：表、字段、类型、业务含义、分数。
- relationships：源表字段、目标表字段、Join 类型。
- metrics：指标名、表达式、过滤规则、版本。
- dimensions：可用维度和枚举值。
- join_paths：当前问题推荐的连接路径。
- sample_queries：相似已验证查询。
- evidence：schema_version、datasource_id 和快照。
- retrieval：策略、候选数、耗时和降级信息。

Schema 索引文档类型：

- table。
- column。
- relationship。
- metric。
- dimension_value。
- verified_query。

MySQL/PostgreSQL 的外键、主键和索引从 information_schema 读取，不能只靠 Demo DDL 和硬编码 Join。

## 9. 工具调用规范

### 9.1 ToolDefinition

每个工具统一声明：

- tool_id 和 version。
- runtime：domain_service/workflow/agent。
- input_schema 和 output_schema。
- permissions。
- risk_level。
- timeout_ms。
- retry_policy。
- idempotent。
- data_sources。
- side_effect。
- requires_approval。
- owner。
- maturity。

### 9.2 ToolCallRecord

每次调用记录：

- tool_call_id。
- request_id、trace_id。
- tool_id、version。
- caller。
- tenant_id。
- input_hash，不记录完整敏感输入。
- source 和 snapshot_id。
- status。
- latency_ms。
- error_code。
- fallback。
- cost。

### 9.3 风险边界

- 只读查询可由 Agent 调用。
- 创建内部任务进入工作流。
- 对外消息必须审批。
- 修改价格、库存、订单等外部动作必须 dry-run、审批、幂等和回滚方案。
- MCP 暴露工具时沿用同一权限与风险策略，不能绕过 API Gateway。

## 10. API 和代码结构整改

### 10.1 API

当前 backend/api/routes.py 包含 42 个端点，职责过多。目标拆分：

- query_routes.py：同步问数和 SSE。
- auth_routes.py。
- quick_query_routes.py。
- data_source_routes.py。
- export_routes.py。
- history_routes.py。
- health_routes.py。
- report_routes.py。
- workflow_routes.py。
- evaluation_routes.py。
- integration_routes.py。

删除 WebSocket 查询，只保留 REST + SSE，避免维护两套流式协议。

所有接口迁移到 /api/v1；旧接口在一个兼容期返回标准弃用信息，之后删除。

### 10.2 核心包

建议目录：

- core/context.py。
- core/errors.py。
- core/envelope.py。
- core/policies.py。
- llm/gateway.py。
- llm/model_policy.py。
- retrieval/schema_models.py。
- retrieval/schema_index.py。
- retrieval/schema_search.py。
- tools/registry.py。
- tools/executor.py。
- agents/nl2sql/graph.py。
- workflows/。
- observability/。
- evals/。

### 10.3 删除或移出主项目

- 企业知识库 RAG。
- Amazon/京东爬虫及浏览器会话。
- 竞品、选品和市场情报。
- 通用联网搜索。
- business_intelligence.py 和 auto_analysis.py 旧实现。
- /api/bi/*。
- /api/reports/daily、/monthly 等旧兼容接口。
- 重复 WebSocket 查询。
- 与删除模块绑定的依赖、配置、测试和前端入口。

Schema 使用 Chroma/Milvus，因此不能因为删除知识库就删除向量库依赖。

## 11. 权限和企业治理

### 11.1 认证

Demo 和生产配置分离：

- Demo 可显式关闭认证。
- Production 缺少 JWT_SECRET、INTEGRATION_TOKEN 或数据库只读账户时拒绝启动。
- 使用标准 JWT/OIDC，不使用内存 Session 模拟 JWT。
- 密码使用 Argon2/bcrypt，不使用单次 SHA256。
- 删除生产环境默认 admin/admin123。

### 11.2 授权

- RBAC：管理员、数据管理员、分析师、运营、只读。
- ABAC：租户、部门、数据源、表、字段、区域、店铺。
- SQL AST 权限校验。
- 行级和列级策略。
- MCP 和集成 API 使用相同策略。

### 11.3 审计

必须记录：

- 登录和 Token。
- 数据源增删改。
- Schema 版本变化。
- Agent 路由和工具调用。
- SQL 哈希、表集合和行数。
- 工作流审批和动作。
- 权限拒绝。
- 配置和 Prompt 版本变更。

日志不得记录 API Key、密码、完整手机号、完整 SQL 结果或模型隐式思维链。

## 12. 可观测与评测

### 12.1 指标

- 请求量、成功率、P50/P95/P99。
- 各阶段延迟。
- 每种模型 Token 和费用。
- 缓存命中率。
- Schema keyword/vector/hybrid 使用率。
- SQL 一次成功率和修复率。
- 澄清率、拒答率、兜底率。
- 权限拦截率。
- 工作流成功率、重试率和积压。
- 工具错误率。

### 12.2 Trace

每个请求统一 trace_id，覆盖：

gateway → router → retrieval → tool → policy → executor → presenter → channel。

先脱敏再 Trace；支持采样。错误请求和高风险动作全量采样，普通成功请求按比例采样。

### 12.3 评测

现有路由和 SQL 安全评测应扩展为：

- 路由准确率。
- 澄清触发准确率。
- Schema Recall@k、Join Path Recall。
- SQL 执行正确率。
- 指标口径一致率。
- 权限越权拦截率。
- 脱敏准确率。
- 缓存隔离测试。
- Prompt Injection 和工具越权。
- Provider 故障和降级测试。
- 延迟、Token 和成本回归。

发布门禁不能只看回答正确率，安全指标必须单独达标。

## 13. 分阶段整改路线

### Phase 0：停止扩功能，建立基线

- 冻结新增业务模块。
- 备份当前工作树。
- 记录现有测试、延迟和 Token 基线。
- 明确要删除的旧模块。
- 创建 ADR：Agent、服务和工作流边界。

验收：有可重复运行的基线报告。

### Phase 1：P0 安全修复

- 修复 PUBLIC_PATHS。
- 修复脱敏和缓存顺序。
- TenantContext 绑定身份。
- 缓存加入租户、权限和数据源。
- 删除全局数据库切换。
- 实现真实 SQL 超时。
- 接入表/字段权限。
- 修复 Tracer 初始化。

验收：越权、脱敏、缓存隔离和超时测试全部通过。

### Phase 2：统一契约

- 建立 RequestContext。
- 建立成功/错误 Envelope。
- 建立 ErrorCode。
- 统一 SSE。
- 所有路由增加 response_model。
- ToolDefinition 和 ToolCallRecord 标准化。
- API 版本化。

验收：REST、SSE、MCP 对同一能力返回一致语义。

### Phase 3：模型网关与决策优化

- 实现 LLMGateway。
- 任务级模型策略。
- 统一超时、重试、熔断和 Token 预算。
- 普通回答改确定性模板。
- 合并重复意图理解。
- 复杂问题才调用第二次 LLM。

验收：典型问数模型调用由 2 次降为 1 次；简单指标 0 次。

### Phase 4：Schema 混合检索

- 结构化 Schema 契约。
- BM25/关键词和向量并行。
- RRF 重排。
- Join Graph。
- 指标和维度检索。
- 按租户和数据源隔离索引。

验收：Schema Recall@4 和 Join Path Recall 达到门槛，向量故障可降级。

### Phase 5：代码收敛

- 删除 RAG、爬虫、竞品、市场情报、旧 BI。
- 拆分 routes.py。
- 删除 WebSocket。
- 清理配置、依赖、测试和前端。
- README 只展示真实可用能力。

验收：全仓无旧模块运行引用，无失效导航和接口。

### Phase 6：可观测、评测和企业接口

- OpenTelemetry/Langfuse。
- 成本和延迟看板。
- 故障注入和安全评测。
- MCP 工具权限。
- Outbox/Webhook。
- 再研究飞书、钉钉、企微渠道适配。

验收：渠道故障不影响主分析链路，所有调用可追踪。

## 14. 建议的实施顺序

严格按以下顺序执行：

1. 先修认证、脱敏、租户、缓存和查询超时。
2. 再统一输出、错误、SSE 和工具协议。
3. 再实现 LLMGateway 与 Token 策略。
4. 再升级 Schema 混合检索。
5. 再删除旧模块、拆路由和清前端。
6. 最后做 MCP 和飞书/钉钉等渠道接入。

不要先优化 UI，也不要先接企业渠道。否则会把当前不统一的权限、错误和输出协议复制到更多入口。

## 15. 简历可体现的能力

完成整改后，项目可以真实体现：

- 能判断 Agent、确定性服务和工作流边界。
- 能设计意图路由、澄清、工具调用和可追溯来源。
- 能实现 Schema 混合检索、Join Graph 和指标语义层。
- 能设计统一输出、错误码和分层兜底。
- 能实现租户隔离、RBAC/ABAC、SQL AST 权限和脱敏。
- 能实现模型网关、Token 预算、缓存和延迟策略。
- 能实现工作流幂等、审批、重试、任务和告警去重。
- 能通过 REST/MCP 接入企业业务，同时保持同一权限与审计策略。
## 16. 当前实施状态

截至 2026-08-17，已完成非核心模块裁剪、认证绕过修复、表权限接入、脱敏后缓存、缓存作用域隔离、SQLite 截止时间、Trace 初始化与脱敏、确定性摘要默认策略，以及 Schema 混合检索和结构化输出。下一阶段按“统一 API 契约 → LLM Gateway → 请求级租户/数据源隔离 → 工具审计 → 可靠性评测 → 前端优化”推进。