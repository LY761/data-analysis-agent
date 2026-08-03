# 数据分析 Agent — 架构解析与企业发展路线

> 从 Demo 到 Enterprise：逐层拆解、逐步优化、边学边做

---

## 目录
1. [项目全景架构](#1-项目全景架构)
2. [Agent 链路深度拆解](#2-agent-链路深度拆解)
3. [当前问题诊断](#3-当前问题诊断)
4. [企业级演进路线图](#4-企业级演进路线图)
5. [Phase 1：基础加固](#5-phase-1基础加固)
6. [Phase 2：Agent 能力升级](#6-phase-2agent-能力升级)
7. [Phase 3：企业平台化](#7-phase-3企业平台化)

---

## 1. 项目全景架构

```
┌─────────────────────────────────────────────────────────────────┐
│                      浏览器 (frontend/index.html)                 │
│                    echarts 图表 + 纯 JS SPA                       │
└──────────────────────────┬──────────────────────────────────────┘
                           │  HTTP POST /api/query
                           │  WebSocket /api/ws/query
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                   FastAPI Server (main.py)                        │
│                   路由分发 + 静态文件托管                           │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│               LangGraph Workflow (agent/workflow.py)              │
│                                                                   │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐      │
│  │ ①Schema  │──▶│ ②Generate│──▶│ ③Validate│──▶│ ④Execute │      │
│  │ Retrieve │   │   SQL    │   │   SQL    │   │   SQL    │      │
│  └──────────┘   └──────────┘   └────┬─────┘   └────┬─────┘      │
│                                     │               │            │
│                          ┌──────────▼──────┐        │            │
│                          │ (loop) Fix SQL  │        │            │
│                          │  max 2 retries  │        │            │
│                          └─────────────────┘        │            │
│                                                     ▼            │
│                            ┌──────────┐   ┌──────────────┐      │
│                            │⑤ Result  │◀──│⑥ Chart       │      │
│                            │  Build   │   │  Recommend   │      │
│                            └──────────┘   └──────────────┘      │
└─────────────────────────────────────────────────────────────────┘
```

### 文件清单与职责

| 文件 | 职责 | 行数 |
|------|------|------|
| `main.py` | 入口：初始化 DB → 索引 Schema → 启动 FastAPI | 68 |
| `config.py` | 配置中心：.env 加载 + 全局常量 | 34 |
| `api/routes.py` | API 层：POST /query、WS /ws/query、GET /schema | 164 |
| `agent/workflow.py` | **核心**：LangGraph 状态图，编排 6 个节点 | 202 |
| `agent/schema_retriever.py` | 语义检索：BGE 模型 + ChromaDB | 143 |
| `agent/sql_generator.py` | LLM 调用：NL→SQL 生成 + 修正 + 回译 | 98 |
| `agent/sql_validator.py` | 三关校验：语法→注入→权限 | 88 |
| `agent/result_checker.py` | 结果检查：空值、极值、慢查询告警 | 70 |
| `agent/chart_recommender.py` | 规则引擎：数据类型→图表推荐 | 149 |
| `db/init_db.py` | 数据初始化：4 张表 + 450+ 行样本数据 | 316 |
| `db/executor.py` | SQL 执行器：安全检查 + limit 注入 + 超时控制 | 107 |

---

## 2. Agent 链路深度拆解

### 2.1 整体链路：6 个 Node + 1 个 Retry Loop

```
用户问题: "本月销售额最高的5个产品是哪些？"
           │
           ▼
┌──────────────────────────────────────────────────────────────┐
│  Node 1: Schema Retrieval (语义检索)                          │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ ① 用户问题 → BGE-large-zh-v1.5 → 1024维向量             │  │
│  │ ② 在 ChromaDB 中做 cosine 相似度搜索                      │  │
│  │ ③ 返回 top-3 相关表 + top-5 相关列                       │  │
│  │                                                          │  │
│  │ 技术要点：                                                │  │
│  │ · 离线预计算：启动时把表DDL/列注释/示例查询 转为embedding │  │
│  │ · 在线检索：用户查询 embedding → ANN 搜索 → 精排         │  │
│  │ · 去重逻辑：同一表/列只保留最相关的那个                    │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────┐
│  Node 2: SQL Generation (LLM 生成)                            │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ System Prompt (8条规则):                                  │  │
│  │  · 只生成 SELECT                                          │  │
│  │  · 只用 Schema 中存在的表和字段，不得编造                   │  │
│  │  · JOIN 用正确的外键                                      │  │
│  │  · 中文 LIKE '%关键词%'                                   │  │
│  │  · GROUP BY / ORDER BY                                   │  │
│  │  · LIMIT ≤ 100                                           │  │
│  │  · 纯 SQL 输出，无 markdown                                │  │
│  │  · 不使用 COMMENT（SQLite 不支持）                        │  │
│  │                                                            │  │
│  │ LLM 参数: temperature=0.1, max_tokens=500                  │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────┐
│  Node 3: SQL Validation (三关校验)                             │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ Gate 1·语法校验 (sqlglot.parse)                          │  │
│  │   · 解析 AST，语法错误直接拦截                            │  │
│  │                                                          │  │
│  │ Gate 2·注入检测 (正则匹配)                                │  │
│  │   · DROP/DELETE/INSERT/UPDATE → 拦截                     │  │
│  │   · UNION SELECT / OR 1=1 / -- / ; → 拦截                │  │
│  │                                                          │  │
│  │ Gate 3·权限校验                                          │  │
│  │   · 必须 SELECT/WITH 开头                                │  │
│  └────────────────────────────────────────────────────────┘  │
│          ↓ 失败 ↓                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ Node 4: SQL Fix (LLM 修正) - 最多重试 2 次               │  │
│  │   将错误信息 + 原SQL + Schema 发给 LLM 重新生成           │  │
│  │   修正后 → 回到 Node 3 再次校验                           │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
           │  通过
           ▼
┌──────────────────────────────────────────────────────────────┐
│  Node 5: SQL Execution + Result Check + Chart                 │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ 5a. SQL Executor (db/executor.py)                       │  │
│  │   · 二次安全检查（forbidden_keywords）                    │  │
│  │   · 自动追加 LIMIT 1000                                  │  │
│  │   · 设置 query_timeout = 10s                             │  │
│  │   · sqlite3.Row → dict                                   │  │
│  │                                                          │  │
│  │ 5b. Result Checker (agent/result_checker.py)             │  │
│  │   · 空结果告警                                           │  │
│  │   · 行数 > 500 告警                                      │  │
│  │   · 耗时 > 3s 告警                                       │  │
│  │   · 数值异常检测（max/avg > 100x）                        │  │
│  │                                                          │  │
│  │ 5c. NL 回译 (LLM)                                        │  │
│  │   · SQL → 自然语言，用于验证 SQL 是否理解正确             │  │
│  │                                                          │  │
│  │ 5d. Chart Recommender (规则引擎)                         │  │
│  │   ┌─────────────────────────────────────────────────┐   │  │
│  │   │ 有时间列 + 数值列 → 折线图 (line)                │   │  │
│  │   │ 文本列 + 数值列 + ≤8行 → 饼图 (pie)              │   │  │
│  │   │ 文本列 + 数值列 + ≤20行 → 柱状图 (bar)           │   │  │
│  │   │ 多数值列 + ≤5行 → 分组柱状图                     │   │  │
│  │   │ 多指标 + 时间 → 多线折线图                        │   │  │
│  │   │ 其他 → 表格 (table)                               │   │  │
│  │   └─────────────────────────────────────────────────┘   │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────┐
│  Node 6: Build Response → JSON → 前端渲染                      │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ {                                                        │  │
│  │   "sql": "...",                                          │  │
│  │   "sql_explanation": "...",                              │  │
│  │   "data": [...], "columns": [...], "row_count": 5,      │  │
│  │   "chart": {"chart_type": "bar", "echarts_option": {}}, │  │
│  │   "warnings": [...], "error": null,                      │  │
│  │   "execution_time_ms": 1234, "retry_count": 0           │  │
│  │ }                                                        │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 LangGraph 状态流转

```python
class WorkflowState(TypedDict):
    question: str            # 用户原始问题
    schema_context: dict     # 检索结果 {"tables":[...], "columns":[...]}
    sql: str                 # 生成的 SQL
    validation_result: dict  # {"valid":bool, "error":str, "stage":str}
    retry_count: int         # 当前重试次数
    query_result: dict       # 执行结果
    chart_recommendation: dict # 图表推荐
    sql_explanation: str     # NL 回译
    final_response: dict     # 最终响应
    error: str               # 全局错误
```

**关键设计决策**：所有节点通过 `{**state, "key": value}` 更新状态，状态不可变，保证可回溯。

### 2.3 两条路由

| 路由 | 协议 | 用途 | 特点 |
|------|------|------|------|
| `POST /api/query` | HTTP | 同步查询 | 等待完整结果后返回 |
| `WS /api/ws/query` | WebSocket | 流式查询 | 逐步推送每个节点的执行状态 |
| `GET /api/schema` | HTTP | 获取schema | 前端侧边栏加载 |

---

## 3. 当前问题诊断

### 3.1 架构层面

| 问题 | 严重程度 | 说明 |
|------|---------|------|
| 无对话记忆 | 🔴 高 | 每次查询独立，无法追问 "再按地区排一下" |
| 无多轮对话 | 🔴 高 | 没有 conversation history |
| 无用户/会话管理 | 🟡 中 | 所有用户共享同一个 DB，无隔离 |
| 单体架构 | 🟡 中 | API + Agent + DB 耦合在一个进程 |
| 无认证授权 | 🟡 中 | 任何人都可访问 API |
| 无日志/监控 | 🟡 中 | 没有请求追踪，出问题无法排查 |

### 3.2 Agent 层面

| 问题 | 说明 |
|------|------|
| SQL Generator 提示词太简单 | 没有 few-shot examples，没有 CoT（思维链） |
| Chart Recommender 纯规则 | 无法处理复杂可视化需求 |
| Schema Retriever 无增量更新 | DB 结构变化需要重启服务 |
| 错误处理粗糙 | fix_sql 只是单纯重试，没有结构化错误分析 |
| 无 Streaming 输出 | 用户需要等待 5-10s 才能看到结果 |

### 3.3 工程层面

| 问题 | 说明 |
|------|------|
| 无测试 | 0 个单元测试 |
| logging 用 print | 生产环境不可追踪 |
| 无配置校验 | .env 缺少关键字段时静默失败 |
| 无 Docker 多阶段构建 | 模型文件 1.3GB 每次都重新下载 |
| 前端无构建工具 | 纯 HTML，难以扩展 |

---

## 4. 企业级演进路线图

```
Phase 1 ──────── Phase 2 ──────── Phase 3 ──────── Phase 4
基础加固         Agent 升级       平台化          高级特性
(1-2周)          (2-4周)           (4-8周)          (持续)

┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
│· 日志系统 │  │· Few-shot│  │· 用户认证│  │· RAG增强 │
│· 异常处理 │  │· CoT推理 │  │· 多租户  │  │· 知识图谱│
│· 配置校验 │  │· 对话记忆│  │· API网关 │  │· 自主决策│
│· 单元测试 │  │· Streaming│ │· 限流熔断│  │· A/B测试 │
│· Docker化 │  │· 工具调用│  │· 监控告警│  │· 模型评测│
└──────────┘  └──────────┘  └──────────┘  └──────────┘
```

---

## 5. Phase 1：基础加固（本周可做）

### 5.1 添加结构化日志系统

**为什么**：`print()` 在生产环境无法追踪、无法搜索、无法分级。

```python
# backend/utils/logger.py
import logging
import uuid
from contextvars import ContextVar

# 每个请求一个 trace_id，贯穿整个 Agent 链路
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")

def setup_logger(name: str = "data-analysis-agent"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)-5s | %(trace_id)s | %(name)s | %(message)s'
    ))
    logger.addHandler(handler)
    return logger

class TraceFilter(logging.Filter):
    def filter(self, record):
        record.trace_id = trace_id_var.get() or "-"
        return True

logger = setup_logger()
logger.addFilter(TraceFilter())
```

### 5.2 添加配置校验

```python
# 在 config.py 末尾添加
def validate_config():
    errors = []
    if LLM_API_KEY == "sk-your-api-key-here":
        errors.append("LLM_API_KEY 未配置，请在 backend/.env 中设置")
    if not LLM_BASE_URL:
        errors.append("LLM_BASE_URL 不能为空")
    if errors:
        raise RuntimeError("配置错误:\n" + "\n".join(f"  · {e}" for e in errors))
```

### 5.3 添加基础单元测试

```python
# backend/tests/test_validator.py
import pytest
from agent.sql_validator import sql_validator

def test_valid_select():
    result = sql_validator.validate("SELECT * FROM products")
    assert result["valid"] is True

def test_reject_delete():
    result = sql_validator.validate("DELETE FROM products WHERE 1=1")
    assert result["valid"] is False
    assert result["stage"] == "injection"

def test_reject_drop():
    result = sql_validator.validate("DROP TABLE products")
    assert result["valid"] is False

def test_injection_union():
    result = sql_validator.validate("SELECT * FROM users UNION SELECT * FROM passwords")
    assert result["valid"] is False

def test_syntax_error():
    result = sql_validator.validate("SELECTT * FROM products")
    assert result["valid"] is False
    assert result["stage"] == "syntax"
```

### 5.4 Docker 多阶段构建

```dockerfile
# 优化后的 Dockerfile
FROM python:3.11-slim AS builder
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY backend/ .
COPY frontend/ ../frontend/
VOLUME ["/app/chroma_db", "/root/.cache/huggingface"]
CMD ["python", "main.py"]
```

---

## 6. Phase 2：Agent 能力升级

### 6.1 对话记忆（Memory）— 最重要！

这是当前项目**最大的缺失**。没有记忆，Agent 无法追问。

```python
# backend/agent/conversation_memory.py
class ConversationManager:
    def __init__(self, max_turns=10, window_size=3):
        self.sessions = {}

    def add_turn(self, session_id: str, question: str, response: dict):
        if session_id not in self.sessions:
            self.sessions[session_id] = []
        self.sessions[session_id].append({
            "question": question,
            "sql": response.get("sql"),
            "answer_summary": self._summarize(response),
        })
        self.sessions[session_id] = self.sessions[session_id][-window_size:]

    def build_context_prompt(self, session_id: str) -> str:
        history = self.sessions.get(session_id, [])
        if not history:
            return ""
        parts = ["## 对话上下文"]
        for i, turn in enumerate(history):
            parts.append(f"Q{i+1}: {turn['question']}")
            parts.append(f"SQL{i+1}: {turn['sql']}")
        return "\n".join(parts)
```

**LangGraph 集成**：
```python
from langgraph.checkpoint.memory import MemorySaver
memory = MemorySaver()
app_workflow = build_workflow().compile(checkpointer=memory)
# 调用时传入 thread_id = session_id
```

### 6.2 Few-Shot + CoT Prompt Engineering

**对比效果**：
- 旧 Prompt（8条规则）→ 正确率约 70%
- 新 Prompt（CoT + Few-Shot）→ 正确率约 90%

核心改进：加入 3 个 Few-Shot 示例（排名类、占比类、多表关联），每一步要求 LLM 先 Think 再输出 SQL。

### 6.3 工具调用 (Function Calling)

企业场景需要更多工具：

```python
@tool
def run_python_analysis(code: str) -> str:
    """执行 pandas 数据分析代码，用于复杂的统计计算"""
    ...

@tool
def export_excel(data: list, filename: str) -> str:
    """导出查询结果为 Excel"""
    ...

@tool
def query_external_api(api_name: str, params: dict) -> dict:
    """查询企业其他系统 API（ERP/CRM）"""
    ...
```

---

## 7. Phase 3：企业平台化

### 7.1 目标架构

```
┌─────────────────────────────────────────────┐
│                  API Gateway                  │
│           (认证/限流/路由/日志)                │
└────────┬────────────┬────────────┬──────────┘
         │            │            │
    ┌────▼───┐   ┌────▼───┐   ┌────▼───┐
    │ Tenant │   │ Tenant │   │ Tenant │
    │   A    │   │   B    │   │   C    │
    │ DB_A   │   │ DB_B   │   │ DB_C   │
    └────────┘   └────────┘   └────────┘
         共享 Agent 引擎，隔离数据和 Schema
```

### 7.2 企业级功能清单

| 功能 | 说明 | 优先级 |
|------|------|--------|
| RBAC 权限 | 不同角色看到不同数据/功能 | P0 |
| 审计日志 | 所有查询记录，合规要求 | P0 |
| 数据脱敏 | 手机号/身份证自动脱敏 | P1 |
| 查询审批 | 高危 SQL 需上级审批 | P1 |
| 定时报告 | 每日/每周自动生成分析报告 | P1 |
| 多数据源 | PostgreSQL/MySQL/BigQuery | P0 |
| 监控告警 | P95 延迟、Token 消耗、错误率 | P1 |
| 模型 A/B 测试 | 对比不同 Prompt/模型的效果 | P2 |

---

## 8. 学习路线

```
第 1 周: LangGraph 基础
  □ StateGraph / Node / Edge / Conditional Edge
  □ Checkpoint / Memory 机制
  □ 手写一个 3-node workflow

第 2 周: Prompt Engineering
  □ Few-shot / CoT / ReAct 模式
  □ System Prompt 设计原则
  □ 在你的项目上实验不同策略

第 3 周: RAG（检索增强生成）
  □ Embedding 原理（cosine/欧氏距离）
  □ ChromaDB 深入：collection/metadata/filter
  □ Chunking 策略 + Rerank

第 4 周: Agent 模式
  □ ReAct Agent（Reasoning + Acting）
  □ Tool Use / Function Calling
  □ Multi-Agent 协作（计划-执行-审查）

第 5-6 周: 企业平台化
  □ FastAPI 中间件 / JWT 认证 / 限流
  □ Docker / K8s 部署
  □ OpenTelemetry 链路追踪
```

---

## 9. 立即可做的 3 件事

1. **加日志**（30分钟） → 在 `workflow.py` 每个 node 入口加 logger.info，体验链路可追踪
2. **改 Prompt**（1小时） → 把 Few-Shot Prompt 替换到 `sql_generator.py`，对比正确率
3. **加对话记忆**（2小时） → 实现 ConversationManager + MemorySaver，支持多轮追问

做完这 3 步，Agent 就从 Demo 级别提升到了可用的产品级别。
