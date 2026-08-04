"""
API路由 — 接收前端请求，调用Agent流水线，返回结果
"""
import json
import uuid
import hashlib
import asyncio
import time
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from agent.workflow import app_workflow
from agent.workflow import WorkflowState
from agent.conversation_memory import get_session, set_current_session, get_history_summary, search_all_history
from db.executor import executor
from services.data_masking import mask_result
from services.retrieval_metrics import RetrievalSpan
# 快捷查询服务（别名导入，避免与端点函数名 run_quick_query 冲突）
from services.quick_queries import run_quick_query as run_quick_query_service, get_quick_query_list
# 竞品分析（真实实现见 agent/competitor_analysis/analyzer.py）
from agent.competitor_analysis import analyze_competitor
from agent.competitor_analysis.analyzer import analyzer
# 查询缓存（SQLite，TTL 5分钟）— 顶部导入，修复端点作用域内 NameError 导致的缓存静默失效
from cache.query_cache import get_cached_result, set_cached_result

router = APIRouter()


class QueryRequest(BaseModel):
    question: str
    session_id: str = ""
    force_refresh: bool = False
    # 澄清对话支持
    is_clarification: bool = False          # 这条消息是对反问的回答
    original_question: str = ""             # 原始模糊问题
    clarification_history: list = []        # 之前的澄清对话


class QueryResponse(BaseModel):
    question: str
    # 聊天模式
    chat_reply: str = ""                   # 聊天/知识问答的直接回复
    nl_answer: str = ""                    # NL2SQL 聊天式回答（新）
    # 澄清相关
    clarification_needed: bool = False
    clarified_question: str = ""
    follow_up_questions: list = []
    alternative_questions: list = []
    empty_suggestions: list = []          # 空结果时的相似产品建议
    # 查询结果
    sql: str = ""
    sql_explanation: str = ""
    data: list = []
    columns: list = []
    row_count: int = 0
    chart: dict = {}
    execution_time_ms: float = 0
    warnings: list = []
    error: str | None = None
    schema_tables: list = []
    retry_count: int = 0
    cache_hit: bool = False
    trace_id: str = ""


async def _knowledge_with_search(question: str, fallback: str) -> str:
    """知识类问题：先联网搜索再 LLM 总结；任一环节失败降级 fallback（纯 LLM 回答）。"""
    try:
        from config import WEB_SEARCH_ENABLED
        if not WEB_SEARCH_ENABLED:
            return fallback
        from agent.web_search import web_search, summarize_with_llm
        results = await asyncio.to_thread(web_search, question, 5)
        if not results:
            return fallback
        answer = await asyncio.to_thread(summarize_with_llm, question, results)
        return answer or fallback
    except Exception:
        return fallback


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
      1. AgentRouter判断意图（chat/sql_query/quick_card/knowledge/clarify）
      2. chat/knowledge → LLM直接回复
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

    # 纯聊天/知识问答 → LLM直接回复，不走SQL流水线
    if route["mode"] in ("chat", "knowledge"):
        reply = route.get("reply", "")
        # 知识类问题：联网搜索增强（搜索/总结任一失败自动降级纯LLM回答）
        if route["mode"] == "knowledge":
            reply = await _knowledge_with_search(request.question, reply)
        return QueryResponse(
            question=request.question, chat_reply=reply,
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

    # 竞品分析 → 调竞品分析器
    if route["mode"] == "competitor":
        from agent.competitor_analysis import analyze_competitor
        competitor_name = route.get("rewritten") or request.question
        result = analyze_competitor(competitor_name)
        return QueryResponse(
            question=request.question, chat_reply=result.get("analysis", "竞品分析失败"),
            sql="", sql_explanation="", data=[], columns=[], row_count=0,
            chart={}, execution_time_ms=0, warnings=[],
            error=None, schema_tables=[], retry_count=0, trace_id=trace_id,
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

    # 第一步：缓存查询
    if not request.force_refresh:
        try:
            from cache.query_cache import get_cached_result
            schema_hash = _build_schema_hash()
            cached = get_cached_result(request.question, schema_hash)
            if cached:
                cached["cache_hit"] = True
                cached["trace_id"] = trace_id
                return QueryResponse(**cached)
        except (ImportError, Exception):
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
            "retry_count": final.get("retry_count", 0),
            "cache_hit": False,
            "trace_id": trace.trace_id,
        }

        # 第四步：写入Redis缓存
        if not final.get("error") and not request.force_refresh:
            try:
                set_cached_result(request.question, _build_schema_hash(), response_data)
            except (ImportError, Exception):
                pass

        # 数据脱敏：手机号/身份证/邮箱/银行卡自动打码
        response_data = mask_result(response_data)

        # 打印链路追踪到控制台
        trace_log.print()

        return QueryResponse(**response_data)


@router.post("/query/stream")
async def query_stream(request: QueryRequest):
    """
    SSE流式查询接口 — 实时推送进度 + 逐token输出回答。

    事件：
      event: status   data: {"message": "正在生成SQL..."}   阶段进度
      event: answer   data: {"delta": "显示器..."}          回答逐token（打字效果）
      event: result   data: <完整响应，与 /api/query 一致>
      event: error    data: {"message": "..."}
      event: done     data: {}
    """
    import json as _json
    from fastapi.responses import StreamingResponse
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
                    yield sse("answer", {"delta": payload})

        # ── 智能路由 ──
        route = await asyncio.to_thread(agent_router.route, request.question)
        mode = route.get("mode")

        # 聊天 / 知识 → 直接流式输出回复（打字效果）
        if mode in ("chat", "knowledge"):
            reply = route.get("reply", "") or ""
            for i in range(0, len(reply), 5):
                yield sse("answer", {"delta": reply[i:i + 5]})
                await asyncio.sleep(0.02)
            yield sse("done", {})
            return

        # 竞品分析 → LLM报告逐token流式输出
        if mode == "competitor":
            from agent.competitor_analysis import analyze_competitor
            yield sse("status", {"message": "正在分析竞品..."})
            name = route.get("rewritten") or request.question
            result = await asyncio.to_thread(analyze_competitor, name, stream_cb)
            yield sse("result", {
                "question": request.question,
                "chat_reply": result.get("analysis", "竞品分析失败"),
                "sql": "", "data": [], "columns": [], "row_count": 0,
                "chart": {}, "execution_time_ms": 0, "warnings": [],
                "error": None, "schema_tables": [], "retry_count": 0, "trace_id": trace_id,
            })
            yield sse("done", {})
            return

        # 需要澄清 → 一次性返回反问
        if mode == "clarify":
            yield sse("result", {
                "question": request.question,
                "clarification_needed": True,
                "follow_up_questions": route.get("follow_up_questions", ["您想分析什么指标？"]),
                "alternative_questions": route.get("alternative_questions", ["本月销售额", "库存预警"]),
                "chat_reply": route.get("reply", ""),
                "sql": "", "data": [], "columns": [], "row_count": 0,
                "chart": {}, "execution_time_ms": 0, "warnings": [],
                "error": None, "schema_tables": [], "retry_count": 0, "trace_id": trace_id,
            })
            yield sse("done", {})
            return

        # sql_query → 走 LangGraph 流水线 + 流式
        yield sse("status", {"message": "正在理解问题，检索相关数据..."})

        effective_question = route.get("rewritten") or request.question
        session_id = request.session_id or f"stream-{trace_id}"
        set_current_session(session_id)

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
            yield sse("error", {"message": f"查询失败: {e}"})
            yield sse("done", {})
            return

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
            "retry_count": final.get("retry_count", 0),
            "cache_hit": False,
            "trace_id": trace_id,
        }
        response_data = mask_result(response_data)

        yield sse("result", response_data)
        yield sse("done", {})

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.websocket("/ws/query")
async def websocket_query(websocket: WebSocket):
    """
    WebSocket流式查询接口 — 逐步推送每个节点的执行状态

    前端可以实时看到：
      · 正在分析问题，检索相关数据表...
      · 正在生成SQL查询...
      · 正在校验SQL安全性...
      · SQL校验通过，正在执行查询...
    """
    await websocket.accept()

    try:
        while True:
            data = await websocket.receive_text()
            request = json.loads(data)
            question = request.get("question", "")
            session_id = request.get("session_id", "")
            force_refresh = request.get("force_refresh", False)

            if not question:
                await websocket.send_json({"type": "error", "message": "问题不能为空"})
                continue

            # Redis缓存查询
            if not force_refresh:
                try:
                    schema_hash = _build_schema_hash()
                    cached = get_cached_result(question, schema_hash)
                    if cached:
                        cached["type"] = "result"
                        cached["cache_hit"] = True
                        await websocket.send_json({"type": "result", "data": cached, "cache_hit": True})
                        await websocket.send_json({"type": "done"})
                        continue
                except (ImportError, Exception):
                    pass

            # 对话记忆
            if session_id:
                set_current_session(session_id)
                mem = get_session(session_id)

            # 第一个状态推送
            await websocket.send_json({
                "type": "status", "stage": "retrieve_schema",
                "message": "正在分析问题，检索相关数据表..."
            })

            initial_state: WorkflowState = {
                "question": question,
                "schema_context": {},
                "sql": "",
                "validation_result": {},
                "retry_count": 0,
                "query_result": {},
                "chart_recommendation": {},
                "sql_explanation": "",
                "final_response": {},
                "error": None,
            }

            # 用astream_events实现流式推送
            final = {}
            async for event in app_workflow.astream_events(initial_state, version="v2"):
                kind = event.get("event", "")
                node_name = event.get("metadata", {}).get("langgraph_node", "")

                # 每个节点开始时推送状态
                if kind == "on_chain_start" and node_name:
                    stage_messages = {
                        "generate_sql": "正在生成SQL查询...",
                        "validate_sql": "正在校验SQL安全性...",
                        "fix_sql": "SQL有误，正在自动修正...",
                        "execute_sql": "SQL校验通过，正在执行查询...",
                        "build_response": "正在生成结果和可视化建议...",
                    }
                    if node_name in stage_messages:
                        await websocket.send_json({
                            "type": "status", "stage": node_name,
                            "message": stage_messages[node_name],
                        })

                # 最后一个节点完成时推送最终结果
                if kind == "on_chain_end" and node_name == "build_response":
                    output = event.get("data", {}).get("output", {})
                    final = output.get("final_response", {})

                    await websocket.send_json({
                        "type": "result",
                        "data": {
                            "question": final.get("question", ""),
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
                            "retry_count": final.get("retry_count", 0),
                        },
                    })

            await websocket.send_json({"type": "done"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        await websocket.send_json({"type": "error", "message": str(e)})


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

@router.post("/reports/daily")
async def generate_daily_report(date: str = None):
    """手动触发生成日报"""
    report = scheduler.generate_daily_report(date)
    return report

@router.post("/reports/monthly")
async def generate_monthly_report(year: int = None, month: int = None):
    """手动触发生成月报"""
    report = scheduler.generate_monthly_report(year, month)
    return report

@router.get("/reports")
async def list_reports():
    """列出所有已生成的报告"""
    return {"reports": scheduler.list_reports()}

@router.get("/reports/{report_key}")
async def get_report(report_key: str):
    """获取指定报告"""
    report = scheduler.get_report(report_key)
    if not report:
        return JSONResponse(status_code=404, content={"error": "报告不存在"})
    return report

# ================================================================
# 系统状态接口（限流+熔断状态）
# ================================================================

@router.get("/health/full")
async def health_full():
    """完整健康检查（含限流和熔断状态）"""
    from middleware.auth_middleware import rate_limiter, circuit_breaker
    return {
        "status": "ok",
        "service": "data-analysis-agent",
        "rate_limiter": {"active": True},
        "circuit_breaker": circuit_breaker.get_status(),
    }


# ================================================================
# 智能分析接口 — 一键分析昨日+上月销售+差评+改进建议
# ================================================================

@router.post("/analysis/quick")
async def quick_analysis():
    """
    一键智能分析:
      1. 昨日销售概况
      2. 上月销售概况
      3. 差评产品根因分析
      4. LLM生成改进建议
    """
    from services.auto_analysis import auto_analyzer
    result = auto_analyzer.analyze()
    return result

@router.get("/analysis/yesterday")
async def yesterday_summary():
    """仅获取昨日销售概况"""
    from datetime import datetime, timedelta
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    report = scheduler.generate_daily_report(yesterday)
    return report

@router.get("/analysis/monthly")
async def monthly_summary(year: int = None, month: int = None):
    """仅获取月报（含产品评论分析）"""
    report = scheduler.generate_monthly_report(year, month)
    return report


# ================================================================
# 快捷查询接口 — 常规问题不走LLM，直接执行预写SQL
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
    "refund_count": "退款数", "refund_rate": "退款率", "rating": "评分",
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

from fastapi.responses import StreamingResponse

class ExportRequest(BaseModel):
    data: list = []
    columns: list = []
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

@router.get("/bi/anomalies")
async def get_anomalies():
    """异常检测：销售额骤降/退款率过高/库存告急"""
    from services.business_intelligence import bi
    return bi.detect_anomalies()

@router.get("/bi/trends")
async def get_trends():
    """趋势预警：检测销量连续下滑的产品"""
    return bi.detect_trends()

@router.post("/bi/daily_report")
async def get_daily_report():
    """生成运营日报（LLM总结+异常+趋势）"""
    return bi.generate_daily_report()


# ================================================================
# 数据库管理接口
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
# 竞品分析接口
# ================================================================

class CompetitorAnalyzeRequest(BaseModel):
    company_name: str = ""
    competitor_url: str = ""
    include_internal: bool = True
    category: str = ""
    session_id: str = ""


@router.post("/competitor/analyze")
async def competitor_analyze(request: CompetitorAnalyzeRequest):
    """
    竞品分析 — 完整5阶段流水线:
      Stage 1: 抓取竞品官网（Scrapling引擎）
      Stage 2: 7维度解析（产品/定价/口碑/市场/技术/内容/客户）
      Stage 3: 自动打分 + SWOT + 排名
      Stage 4: 内部SQL数据对比
      Stage 5: LLM战略报告生成

    返回: {found, name, analysis, data_sources, internal_summary, error, trace_id}
    """
    trace_id = uuid.uuid4().hex[:12]

    if not request.company_name:
        return {
            "error": "请提供竞品公司名称",
            "company_name": "",
            "trace_id": trace_id,
        }

    # 缓存检查（5分钟 TTL）
    if not getattr(request, 'force_refresh', False):
        try:
            cache_key = f"competitor:{request.company_name}:{request.include_internal}"
            cached = get_cached_result(cache_key, "competitor_v1")
            if cached:
                cached["cache_hit"] = True
                cached["trace_id"] = trace_id
                return cached
        except Exception:
            pass

    # 真实流水线（agent/competitor_analysis/analyzer.py）：
    # 匹配已知竞品 → 加载本地竞品JSON → 内部SQL对比 → 差评 → LLM洞察
    t0 = time.time()
    result = await asyncio.to_thread(analyze_competitor, request.company_name, None)

    response = {
        "company_name": request.company_name,
        "found": result.get("found", False),
        "name": result.get("name", request.company_name),
        "analysis": result.get("analysis", ""),
        "data_sources": result.get("data_sources", []),
        "internal_summary": result.get("internal_summary", {}),
        "execution_time_ms": int((time.time() - t0) * 1000),
        "cache_hit": False,
        "error": result.get("error"),
        "trace_id": trace_id,
    }

    # 写入缓存（仅找到竞品数据时）
    if result.get("found"):
        try:
            cache_key = f"competitor:{request.company_name}:{request.include_internal}"
            set_cached_result(cache_key, "competitor_v1", response)
        except Exception:
            pass

    return response


@router.websocket("/competitor/analyze/ws")
async def competitor_analyze_ws(websocket: WebSocket):
    """
    竞品分析 WebSocket 流式接口 — 状态推送 + LLM 报告逐token流式输出
    """
    await websocket.accept()

    try:
        data = await websocket.receive_text()
        request = json.loads(data)
        company_name = request.get("company_name", "")

        if not company_name:
            await websocket.send_json({"type": "error", "message": "请提供竞品公司名称"})
            return

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def stream_cb(delta: str):
            # 工作线程（to_thread）→ 事件循环 → 队列，与 /api/query/stream 同模式
            loop.call_soon_threadsafe(lambda: queue.put_nowait(("delta", delta)))

        await websocket.send_json({
            "type": "status",
            "message": "正在加载竞品数据并对比内部销售...",
        })

        task = asyncio.create_task(
            asyncio.to_thread(analyze_competitor, company_name, stream_cb)
        )
        while True:
            while not queue.empty():
                _, delta = queue.get_nowait()
                await websocket.send_json({"type": "delta", "delta": delta})
            if task.done():
                break
            await asyncio.sleep(0.03)
        while not queue.empty():
            _, delta = queue.get_nowait()
            await websocket.send_json({"type": "delta", "delta": delta})

        result = task.result()
        await websocket.send_json({
            "type": "result",
            "data": {
                "company_name": company_name,
                "name": result.get("name", company_name),
                "found": result.get("found", False),
                "analysis": result.get("analysis", ""),
                "internal_summary": result.get("internal_summary", {}),
                "error": result.get("error"),
            },
        })
        await websocket.send_json({"type": "done"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass


# ================================================================
# 检索质量监控
# ================================================================

@router.get("/metrics/retrieval")
async def retrieval_metrics(limit: int = 100):
    """
    检索质量看板：关键词命中率 / 向量降级率 / Precision@k / Recall漏损 / Token消耗 / 成功率和平均延迟。
    """
    from services.retrieval_metrics import get_metrics
    return get_metrics(limit)


# ================================================================
# 竞品分析接口
# ================================================================

@router.get("/competitor/list")
async def list_competitors():
    """列出所有可分析的竞品"""
    # analyzer is the singleton instance
    return {"competitors": analyzer.list_competitors()}




# ================================================================
# 市场情报
# ================================================================

class MarketSelectionRequest(BaseModel):
    category: str


class MarketProductRequest(BaseModel):
    query: str


class MarketPasteRequest(BaseModel):
    text: str
    mode: str = "product"   # product | selection | competitor


@router.post("/market/selection")
def market_selection(request: MarketSelectionRequest):
    # 普通 def：FastAPI 自动丢线程池，避免网络爬取+LLM 的阻塞调用卡住事件循环
    from agent.market_intelligence.selection import analyze_selection
    result = analyze_selection(request.category)
    return result


@router.post("/market/product")
def market_product(request: MarketProductRequest):
    # 普通 def：FastAPI 自动丢线程池，避免网络爬取+LLM 的阻塞调用卡住事件循环
    from agent.market_intelligence.product_analyzer import analyze_product
    result = analyze_product(request.query)
    return result


@router.post("/market/paste")
def market_paste(request: MarketPasteRequest):
    """粘贴数据分析 — 用户提供真实文本，LLM 直接分析（不触发爬虫）。
    反爬导致自动抓取失败时的可靠替代：把看到的商品/市场/竞品信息贴进来。
    mode: product(商品研究) / selection(选品) / competitor(竞品洞察)
    """
    from agent.market_intelligence.paste_analysis import analyze_pasted
    result = analyze_pasted(request.mode, request.text)
    return result


@router.post("/market/stream")
async def market_stream(request: MarketProductRequest):
    """SSE 流式市场情报分析。按 query 自动判断 selection / product。"""
    import json as _json
    from fastapi.responses import StreamingResponse
    from agent.agent_router import agent_router

    async def event_gen():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def sse(event, data):
            return f"event: {event}\ndata: {_json.dumps(data, ensure_ascii=False)}\n\n"

        def stream_cb(text):
            loop.call_soon_threadsafe(lambda: queue.put_nowait(text))

        async def forward():
            while not queue.empty():
                yield sse("status", {"message": queue.get_nowait()})

        route = await asyncio.to_thread(agent_router.route, request.query)
        mode = route.get("mode")
        sub = route.get("sub", "selection")
        query = route.get("query", request.query)

        if mode != "market_intelligence":
            # 尽力而为兜底：前端既然投到 market 端点，意图是市场分析 →
            # 直接跑选品，不再"未识别"后死端无结果。保留下方 error 事件处理。
            sub = "selection"
            query = request.query
            yield sse("status", {"message": "按选品分析处理..."})

        task = asyncio.create_task(_run_market(sub, query, stream_cb))
        try:
            while True:
                async for ev in forward():
                    yield ev
                if task.done():
                    break
                await asyncio.sleep(0.03)

            async for ev in forward():
                yield ev

            try:
                result = task.result()
            except Exception as e:
                yield sse("error", {"message": str(e)})
                yield sse("done", {})
                return

            yield sse("result", result)
            yield sse("done", {})
        finally:
            # 客户端断开 → generator 被 abort，取消后台分析任务，避免白跑浪费 LLM/爬取
            if not task.done():
                task.cancel()

    return StreamingResponse(event_gen(), media_type="text/event-stream")


async def _run_market(sub: str, query: str, stream_cb):
    """后台任务：跑选品或商品研究（阻塞调用丢线程池）"""
    import asyncio
    if sub == "selection":
        from agent.market_intelligence.selection import analyze_selection
        return await asyncio.to_thread(analyze_selection, query, None, stream_cb)
    from agent.market_intelligence.product_analyzer import analyze_product
    return await asyncio.to_thread(analyze_product, query, None, stream_cb)
