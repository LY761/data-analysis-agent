"""
API路由 — 接收前端请求，调用Agent流水线，返回结果
"""
import json
import uuid
import hashlib
import asyncio
import time
from fastapi import APIRouter, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from agent.workflow import app_workflow
from agent.workflow import WorkflowState
from agent.conversation_memory import (
    _ensure_table,
    get_session,
    set_current_session,
    get_history_summary,
    search_all_history,
)
from db.executor import executor
from services.data_masking import mask_result
from services.retrieval_metrics import RetrievalSpan
# 快捷查询服务（别名导入，避免与端点函数名 run_quick_query 冲突）
from services.quick_queries import run_quick_query as run_quick_query_service, get_quick_query_list
# 查询缓存（SQLite，TTL 5分钟）— 顶部导入，修复端点作用域内 NameError 导致的缓存静默失效
from cache.query_cache import get_cached_result, set_cached_result

router = APIRouter()


def _build_cache_scope() -> dict:
    from config import LLM_MODEL
    from db.connection_manager import get_current_db
    from middleware.auth_middleware import current_user_ctx

    user = current_user_ctx.get() or {}
    database = get_current_db() or {}
    permissions = user.get("permissions", {})
    permission_hash = hashlib.sha256(
        json.dumps(permissions, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:12]
    return {
        "tenant_id": user.get("tenant_id", "default"),
        "user_id": user.get("user_id", "anonymous"),
        "permission_hash": permission_hash,
        "database": database.get("key", "demo"),
        "model": LLM_MODEL,
        "prompt_version": "nl2sql-v1",
    }


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    session_id: str = Field("", max_length=128)
    force_refresh: bool = False
    # 澄清对话支持
    is_clarification: bool = False          # 这条消息是对反问的回答
    original_question: str = ""             # 原始模糊问题
    clarification_history: list = Field(default_factory=list)        # 之前的澄清对话


class QueryResponse(BaseModel):
    question: str
    # 直接回复模式
    chat_reply: str = ""                   # 问候、帮助和范围提示
    nl_answer: str = ""                    # NL2SQL 聊天式回答（新）
    # 澄清相关
    clarification_needed: bool = False
    clarified_question: str = ""
    follow_up_questions: list = Field(default_factory=list)
    alternative_questions: list = Field(default_factory=list)
    empty_suggestions: list = Field(default_factory=list)          # 空结果时的相似产品建议
    # 查询结果
    sql: str = ""
    sql_explanation: str = ""
    data: list = Field(default_factory=list)
    columns: list = Field(default_factory=list)
    row_count: int = 0
    chart: dict = Field(default_factory=dict)
    execution_time_ms: float = 0
    warnings: list = Field(default_factory=list)
    error: str | None = None
    schema_tables: list = Field(default_factory=list)
    schema_columns: list = Field(default_factory=list)
    schema_relationships: list = Field(default_factory=list)
    metric_definitions: list = Field(default_factory=list)
    join_paths: list = Field(default_factory=list)
    retrieval: dict = Field(default_factory=dict)
    retry_count: int = 0
    cache_hit: bool = False
    trace_id: str = ""


def _build_schema_hash() -> str:
    """生成当前数据库Schema的指纹，用于缓存键的Schema感知失效"""
    try:
        tables = executor.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        raw = json.dumps(tables.get("data", []), ensure_ascii=False).encode()
        return hashlib.md5(raw).hexdigest()[:12]
    except Exception:
        return "unknown"


@router.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest):
    """
    聊天式Agent接口 — LLM先判断意图，再决定聊天/查数据/快捷卡片

    流程：
      1. AgentRouter判断意图（chat/sql_query/quick_card/clarify）
      2. chat → 规则直接回复
      3. sql_query → 走LangGraph流水线
      4. quick_card → 预写SQL，毫秒级
    """
    trace_id = uuid.uuid4().hex[:12]

    # ═══════════════════════════════════════════════════════
    # 第零步：智能路由（聊天? 查数据?）
    # ═══════════════════════════════════════════════════════
    # route() 内部可能调 LLM（规则未命中时），丢线程池避免阻塞事件循环
    from agent.agent_router import agent_router
    route = await asyncio.to_thread(agent_router.route, request.question)

    # 问候和能力说明走确定性回复，不进入 SQL 流水线。
    if route["mode"] == "chat":
        return QueryResponse(
            question=request.question, chat_reply=route.get("reply", ""),
            sql="", sql_explanation="", data=[], columns=[], row_count=0,
            chart={}, execution_time_ms=0, warnings=[],
            error=None, schema_tables=[], retry_count=0, trace_id=trace_id,
        )
    # 快捷卡片 → 预写SQL，毫秒级
    if route["mode"] == "quick_card":
        from services.quick_queries import run_quick_query
        card_key = route.get("card_key", "monthly_sales")
        result = run_quick_query(card_key)
        result = mask_result(result)
        return QueryResponse(
            question=result.get("question",""), sql=result.get("sql",""),
            sql_explanation="", data=result.get("data",[]),
            columns=result.get("columns",[]), row_count=result.get("row_count",0),
            chart=result.get("chart",{}), execution_time_ms=result.get("execution_time_ms",0),
            warnings=result.get("warnings",[]), error=result.get("error"),
            schema_tables=[], retry_count=0, trace_id=trace_id,
        )

    # 需要澄清 → 返回LLM生成的反问
    if route["mode"] == "clarify":
        return QueryResponse(
            question=request.question,
            clarification_needed=True,
            follow_up_questions=route.get("follow_up_questions", ["您想分析什么指标？"]),
            alternative_questions=route.get("alternative_questions", ["本月销售额", "库存预警"]),
            chat_reply=route.get("reply", ""),
            sql="", sql_explanation="", data=[], columns=[], row_count=0,
            chart={}, execution_time_ms=0, warnings=[],
            error=None, schema_tables=[], retry_count=0, trace_id=trace_id,
        )

    # sql_query模式：走原有LangGraph流水线
    # ═══════════════════════════════════════════════════════
    # 用LLM改写后的问题，而非原始问题
    effective_question = route.get("rewritten") or request.question

    # 第一步：只缓存无会话、无澄清的独立查询。
    cache_allowed = not (
        request.force_refresh
        or request.session_id
        or request.is_clarification
        or request.clarification_history
    )
    cache_scope = _build_cache_scope()
    if cache_allowed:
        try:
            schema_hash = _build_schema_hash()
            cached = get_cached_result(effective_question, schema_hash, scope=cache_scope)
            if cached:
                cached["cache_hit"] = True
                cached["trace_id"] = trace_id
                return QueryResponse(**cached)
        except Exception:
            pass

    # 第二步：对话记忆（维护多轮对话上下文）
    session_id = request.session_id or f"http-{trace_id}"
    set_current_session(session_id)
    mem = get_session(session_id)
    is_incremental = mem.detect_incremental(request.question)

    # 第三步：跑LangGraph流水线（带本地链路追踪）
    from agent.debug_trace import TraceLog
    trace_log = TraceLog(request.question)

    from tracing.tracer import TraceContext
    with TraceContext(request.question) as trace:
        # 检索质量监控 span
        from config import DEMO_DB_PATH
        from services.retrieval_metrics import RetrievalSpan
        retrieval_span = RetrievalSpan(request.question, DEMO_DB_PATH)

        initial_state: WorkflowState = {
            "question": effective_question,
            "original_question": request.original_question or request.question,
            "schema_context": {},
            "clarification_needed": False,
            "clarified_question": "",
            "follow_up_questions": [],
            "sql_hint": "",
            "alternative_questions": [],
            "conversation_history": request.clarification_history or [],
            "intent": "",
            "intent_name": "",
            "intent_method": "",
            "sql": "",
            "validation_result": {},
            "retry_count": 0,
            "query_result": {},
            "chart_recommendation": {},
            "sql_explanation": "",
            "analysis_text": "",
            "nl_answer": "",
            "final_response": {},
            "error": None,
            "progress_cb": None,
            "stream_answer_cb": None,
            "retrieval_span": retrieval_span,
        }

        result = await app_workflow.ainvoke(initial_state)
        final = result.get("final_response", {})
        # 兜底落库检索指标（execute_sql 节点已 flush 则此处幂等跳过；
        # 上游失败未 flush 时补记一条含错误信息，保证指标看板有数据）
        try:
            retrieval_span.flush()
        except Exception:
            pass

        # 记录本轮对话到记忆
        if not final.get("error"):
            mem.add_turn(
                request.question,
                final.get("sql", ""),
                {"row_count": final.get("result", {}).get("row_count", 0)},
                topic_id=final.get("topic_id", ""),
            )

        # 空结果有产品建议 → 触发反问模式
        empty_sugs = final.get("empty_suggestions", [])
        response_data = {
            "question": final.get("original_question") or final.get("question", ""),
            "clarification_needed": final.get("clarification_needed", False) or bool(empty_sugs),
            "clarified_question": final.get("clarified_question", ""),
            "follow_up_questions": final.get("follow_up_questions", []) or final.get("empty_suggestions", []),
            "alternative_questions": final.get("alternative_questions", []),
            "sql": final.get("sql", ""),
            "sql_explanation": final.get("sql_explanation", ""),
            "nl_answer": final.get("nl_answer", ""),
            "data": final.get("result", {}).get("data", []),
            "columns": final.get("result", {}).get("columns", []),
            "row_count": final.get("result", {}).get("row_count", 0),
            "chart": final.get("chart", {}),
            "execution_time_ms": final.get("result", {}).get("execution_time_ms", 0),
            "warnings": final.get("result", {}).get("warnings", []),
            "error": final.get("error"),
            "schema_tables": final.get("schema_retrieved", {}).get("tables", []),
            "schema_columns": final.get("schema_retrieved", {}).get("columns", []),
            "schema_relationships": final.get("schema_retrieved", {}).get("relationships", []),
            "metric_definitions": final.get("schema_retrieved", {}).get("metrics", []),
            "join_paths": final.get("schema_retrieved", {}).get("join_paths", []),
            "retrieval": final.get("schema_retrieved", {}).get("retrieval", {}),
            "retry_count": final.get("retry_count", 0),
            "cache_hit": False,
            "trace_id": trace.trace_id,
        }

        # 数据脱敏必须先于缓存，避免缓存未脱敏结果。
        response_data = mask_result(response_data)

        # 第四步：写入查询缓存。
        if not final.get("error") and cache_allowed:
            try:
                set_cached_result(effective_question, _build_schema_hash(), response_data, scope=cache_scope)
            except Exception:
                pass

        # 打印链路追踪到控制台
        trace_log.print()

        return QueryResponse(**response_data)


@router.post("/query/stream")
async def query_stream(request: QueryRequest):
    """
    SSE流式查询接口 — 实时推送进度 + 逐token输出回答。

    标准事件：message.start / route.selected / retrieval.start|complete /
    answer.delta / message.completed|error。旧 status/answer/result/error/done
    暂时保留，兼容现有 Web 客户端。
    """
    import json as _json
    from agent.agent_router import agent_router

    trace_id = uuid.uuid4().hex[:12]

    async def event_gen():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def sse(event: str, data) -> str:
            return f"event: {event}\ndata: {_json.dumps(data, ensure_ascii=False)}\n\n"

        def progress_cb(text: str):
            loop.call_soon_threadsafe(lambda: queue.put_nowait(("status", text)))

        def stream_answer_cb(delta: str):
            loop.call_soon_threadsafe(lambda: queue.put_nowait(("answer", delta)))

        async def forward_queue():
            while not queue.empty():
                kind, payload = queue.get_nowait()
                if kind == "status":
                    yield sse("status", {"message": payload})
                elif kind == "answer":
                    yield sse("answer.delta", {"delta": payload})
                    yield sse("answer", {"delta": payload})

        yield sse("message.start", {"request_id": trace_id, "session_id": request.session_id})

        # ── 智能路由 ──
        route = await asyncio.to_thread(agent_router.route, request.question)
        mode = route.get("mode")
        yield sse("route.selected", {"intent": mode})

        # 问候和能力说明直接流式返回。
        if mode == "chat":
            reply = route.get("reply", "") or ""
            for index in range(0, len(reply), 5):
                delta = reply[index:index + 5]
                yield sse("answer.delta", {"delta": delta})
                yield sse("answer", {"delta": delta})
                await asyncio.sleep(0.02)
            yield sse("message.completed", {
                "request_id": trace_id,
                "intent": mode,
                "answer": reply,
                "citations": [],
            })
            yield sse("done", {})
            return
        # 需要澄清 → 一次性返回反问
        if mode == "clarify":
            response_data = {
                "question": request.question,
                "clarification_needed": True,
                "follow_up_questions": route.get("follow_up_questions", ["您想分析什么指标？"]),
                "alternative_questions": route.get("alternative_questions", ["本月销售额", "库存预警"]),
                "chat_reply": route.get("reply", ""),
                "sql": "", "data": [], "columns": [], "row_count": 0,
                "chart": {}, "execution_time_ms": 0, "warnings": [],
                "error": None, "schema_tables": [], "retry_count": 0, "trace_id": trace_id,
            }
            yield sse("result", response_data)
            yield sse("message.completed", response_data)
            yield sse("done", {})
            return

        # sql_query → 走 LangGraph 流水线 + 流式
        yield sse("retrieval.start", {"source": "schema", "top_k": 3})
        yield sse("status", {"message": "正在理解问题，检索相关数据..."})

        effective_question = route.get("rewritten") or request.question
        session_id = request.session_id or f"stream-{trace_id}"
        set_current_session(session_id)

        # 检索质量监控：流式路径同样记录指标（此前缺失 → 看板永远无数据）
        from config import DEMO_DB_PATH as _demo_db_path
        retrieval_span = RetrievalSpan(effective_question, _demo_db_path)

        initial_state: WorkflowState = {
            "question": effective_question,
            "original_question": request.original_question or request.question,
            "schema_context": {},
            "clarification_needed": False,
            "clarified_question": "",
            "follow_up_questions": [],
            "sql_hint": "",
            "alternative_questions": [],
            "conversation_history": request.clarification_history or [],
            "intent": "",
            "intent_name": "",
            "intent_method": "",
            "sql": "",
            "validation_result": {},
            "retry_count": 0,
            "query_result": {},
            "chart_recommendation": {},
            "sql_explanation": "",
            "analysis_text": "",
            "nl_answer": "",
            "final_response": {},
            "error": None,
            "progress_cb": progress_cb,
            "stream_answer_cb": stream_answer_cb,
            "retrieval_span": retrieval_span,
        }

        task = asyncio.create_task(app_workflow.ainvoke(initial_state))

        while True:
            async for ev in forward_queue():
                yield ev
            if task.done():
                break
            await asyncio.sleep(0.03)

        async for ev in forward_queue():
            yield ev

        try:
            final_state = task.result()
            final = final_state.get("final_response", {})
        except Exception as e:
            # 失败也落库一条指标（execute_sql 未执行时兜底）
            try:
                retrieval_span.flush()
            except Exception:
                pass
            error_data = {
                "request_id": trace_id,
                "code": "DATA_AGENT_STREAM_FAILED",
                "message": f"查询失败: {e}",
                "retryable": False,
            }
            yield sse("message.error", error_data)
            yield sse("error", {"message": error_data["message"]})
            yield sse("done", {})
            return

        # 成功路径兜底 flush（execute_sql 已 flush 则幂等跳过）
        try:
            retrieval_span.flush()
        except Exception:
            pass

        response_data = {
            "question": final.get("original_question") or final.get("question", ""),
            "clarification_needed": final.get("clarification_needed", False),
            "clarified_question": final.get("clarified_question", ""),
            "follow_up_questions": final.get("follow_up_questions", []),
            "alternative_questions": final.get("alternative_questions", []),
            "sql": final.get("sql", ""),
            "sql_explanation": final.get("sql_explanation", ""),
            "nl_answer": final.get("nl_answer", ""),
            "data": final.get("result", {}).get("data", []),
            "columns": final.get("result", {}).get("columns", []),
            "row_count": final.get("result", {}).get("row_count", 0),
            "chart": final.get("chart", {}),
            "execution_time_ms": final.get("result", {}).get("execution_time_ms", 0),
            "warnings": final.get("result", {}).get("warnings", []),
            "error": final.get("error"),
            "schema_tables": final.get("schema_retrieved", {}).get("tables", []),
            "schema_columns": final.get("schema_retrieved", {}).get("columns", []),
            "schema_relationships": final.get("schema_retrieved", {}).get("relationships", []),
            "metric_definitions": final.get("schema_retrieved", {}).get("metrics", []),
            "join_paths": final.get("schema_retrieved", {}).get("join_paths", []),
            "retrieval": final.get("schema_retrieved", {}).get("retrieval", {}),
            "retry_count": final.get("retry_count", 0),
            "cache_hit": False,
            "trace_id": trace_id,
        }
        response_data = mask_result(response_data)

        yield sse("retrieval.complete", {
            "tables": len(response_data.get("schema_tables", [])),
            "columns": len(response_data.get("schema_columns", [])),
        })
        yield sse("result", response_data)
        yield sse("message.completed", response_data)
        yield sse("done", {})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/health")
async def health():
    """健康检查接口"""
    return {"status": "ok", "service": "data-analysis-agent"}


@router.get("/schema")
async def get_schema():
    """返回完整数据库Schema供前端参考"""
    tables_result = executor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row["name"] for row in tables_result.get("data", [])]

    schema_info = {}
    for table in tables:
        cols = executor.execute(f"PRAGMA table_info('{table}')")
        schema_info[table] = cols.get("data", [])

    return {"tables": tables, "schemas": schema_info}


# ================================================================
# 认证接口
# ================================================================

class LoginRequest(BaseModel):
    username: str
    password: str

@router.post("/login")
async def login(request: LoginRequest):
    """用户登录，返回Bearer Token"""
    from auth.jwt_handler import authenticate, create_session_token
    user = authenticate(request.username, request.password)
    if not user:
        return JSONResponse(status_code=401, content={"error": "用户名或密码错误"})
    token = create_session_token(user)
    return {"token": token, "user": {"user_id": user.user_id, "username": user.username, "role": user.role}}

@router.post("/register")
async def register(request: LoginRequest):
    """用户注册（Demo用，生产应接LDAP/OAuth）"""
    from auth.jwt_handler import create_user, create_session_token, authenticate
    result = create_user(request.username, request.password)
    if "error" in result:
        return JSONResponse(status_code=400, content=result)
    user = authenticate(request.username, request.password)
    token = create_session_token(user)
    return {"token": token, "user": {"user_id": user.user_id, "username": user.username, "role": user.role}}

# ================================================================
# 用户反馈接口
# ================================================================

class FeedbackRequest(BaseModel):
    query_id: str = ""
    question: str
    sql: str = ""
    rating: str  # helpful | not_helpful | partial
    comment: str = ""
    expected_result: str = ""

@router.post("/feedback")
async def submit_feedback(request: FeedbackRequest):
    """提交用户反馈，驱动Prompt迭代优化"""
    from services.feedback import record_feedback, get_feedback_stats
    result = record_feedback(
        query_id=request.query_id,
        question=request.question,
        sql=request.sql,
        rating=request.rating,
        comment=request.comment,
        expected_result=request.expected_result,
    )
    stats = get_feedback_stats()
    return {"recorded": True, "id": result["id"], "stats": stats}

@router.get("/feedback/stats")
async def feedback_stats():
    """获取反馈统计"""
    from services.feedback import get_feedback_stats, get_bad_cases, export_feedback_for_optimization
    stats = get_feedback_stats()
    bad_cases = get_bad_cases(10)
    optimization = export_feedback_for_optimization()
    return {"stats": stats, "bad_cases_count": len(bad_cases), "optimization_suggestions": optimization}

# ================================================================

@router.get("/quick/list")
async def list_quick_queries():
    """返回所有快捷查询列表（前端渲染卡片用）"""
    return {"queries": get_quick_query_list()}

# 快捷卡片列名 → 中文（生成语言化摘要用）
_QUICK_COL_ZH = {
    "total_sales": "总销售额", "order_count": "订单数", "avg_order": "客单价",
    "sales_amount": "销售额", "quantity": "销量", "total_qty": "销量",
    "total_amount": "金额", "stock_quantity": "库存", "product_name": "产品",
    "category": "品类", "month": "月份", "supplier": "供应商",
    "refund_count": "退款数", "refund_rate": "退款率",
    "refund_order_rate_pct": "退款订单率", "rating": "评分",
    "avg_rating": "平均评分", "customer_name": "客户", "region": "地区",
    "member_level": "会员等级", "payment_method": "支付方式",
    "count": "数量", "sum": "合计", "revenue": "营收", "growth": "增长",
}


def _quick_card_summary(key: str, result: dict) -> str:
    """把快捷查询结果转成一句自然语言（用户期望“语言”而非只有表格）"""
    from services.quick_queries import QUICK_QUERIES
    q = QUICK_QUERIES.get(key)
    label = (q or {}).get("label", key)
    data = result.get("data") or []
    if not data:
        return f"{label}：暂无数据"
    parts = []
    for col, val in data[0].items():
        cname = _QUICK_COL_ZH.get(col, col)
        if isinstance(val, float):
            v = f"{val:,.2f}" if abs(val) < 100 else f"{val:,.0f}"
        else:
            v = str(val)
        parts.append(f"{cname} {v}")
    return f"{label}：{'；'.join(parts)}"


@router.post("/quick/{query_key}")
async def run_quick_card(query_key: str):
    """执行一个快捷查询（0 Token，毫秒级）。附带语言化摘要 nl_answer。"""
    result = run_quick_query_service(query_key)
    result = mask_result(result)
    result["nl_answer"] = _quick_card_summary(query_key, result)
    return result


# ================================================================
# 数据导出接口
# ================================================================

from fastapi.responses import JSONResponse, StreamingResponse

@router.post("/data/upload")
async def data_upload(file: UploadFile = File(...)):
    """上传 CSV/Excel → 自动建表入库 → 立即可被 NL2SQL 查询。
    支持 .csv（utf-8/gbk）与 .xlsx；表名由文件名生成（data_ 前缀）。"""
    content = await file.read()
    from services.data_ingest import ingest_file
    result = ingest_file(file.filename or "upload.csv", content)
    return result


class ExportRequest(BaseModel):
    data: list = Field(default_factory=list)
    columns: list = Field(default_factory=list)
    title: str = "查询结果"

@router.post("/export/excel")
async def export_excel(request: ExportRequest):
    """导出查询结果为 Excel 文件"""
    from services.export_service import export_to_excel, is_available
    if not is_available():
        return JSONResponse(status_code=500, content={"error": "openpyxl未安装，请执行: pip install openpyxl"})
    excel_bytes = export_to_excel(request.data, request.columns, request.title)
    filename = f"data_{__import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        __import__('io').BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ================================================================
# 商业智能接口
# ================================================================

class DBConnectionRequest(BaseModel):
    key: str
    label: str = ""
    db_type: str = "sqlite"   # sqlite / mysql / postgresql
    path_or_url: str = ""

@router.get("/db/list")
async def list_databases():
    """列出所有已连接的数据库"""
    from db.connection_manager import list_connections, get_current_db
    return {"databases": list_connections(), "current": get_current_db()}

@router.post("/db/add")
async def add_database(request: DBConnectionRequest):
    """添加新的数据库连接"""
    from db.connection_manager import add_connection
    return add_connection(request.key, request.label or request.key, request.db_type, request.path_or_url)

@router.post("/db/switch/{key}")
async def switch_database(key: str):
    """切换到指定数据库"""
    from db.connection_manager import switch_database
    return switch_database(key)

@router.delete("/db/{key}")
async def remove_database(key: str):
    """移除数据库连接"""
    from db.connection_manager import remove_connection
    return remove_connection(key)


# ================================================================
# 对话历史接口
# ================================================================

@router.get("/history/{session_id}")
async def get_history(session_id: str = "default"):
    """获取会话历史摘要"""
    return get_history_summary(session_id)

@router.get("/history/search")
async def search_history(keyword: str, session_id: str = "default"):
    """搜索历史对话"""
    if session_id and session_id != "all":
        mem = get_session(session_id)
        results = mem.search_history(keyword)
    else:
        results = search_all_history(keyword)
    return {"keyword": keyword, "results": results}

@router.delete("/history/{session_id}")
async def clear_history(session_id: str = "default"):
    """清除会话历史"""
    import sqlite3
    from config import DEMO_DB_PATH
    _ensure_table()
    conn = sqlite3.connect(DEMO_DB_PATH)
    conn.execute("DELETE FROM conversation_history WHERE session_id=?", (session_id,))
    conn.commit()
    conn.close()
    return {"ok": True, "session_id": session_id}


# ================================================================
# 检索质量指标接口
# ================================================================

@router.get("/metrics/retrieval")
async def retrieval_metrics(limit: int = 100):
    """
    检索质量看板：关键词命中率 / 向量降级率 / Precision@k / Recall漏损 / Token消耗 / 成功率和平均延迟。
    """
    from services.retrieval_metrics import get_metrics
    return get_metrics(limit)
