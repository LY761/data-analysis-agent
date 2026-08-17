"""
LangGraph 工作流 — 编排完整的 NL2SQL 流水线

v3.0: 新增查询澄清节点。模糊问题先反问用户，清楚后再生成SQL。
"""
import asyncio
import json
import re
import time
import logging
from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, END
from agent.schema_retriever import schema_retriever
from agent.sql_generator import sql_generator
from agent.sql_validator import sql_validator
from agent.result_checker import result_checker
from agent.chart_recommender import chart_recommender
from agent.query_clarifier import query_clarifier
from agent.intent_classifier import classify_by_rules
from agent.debug_trace import TraceLog
from db.executor import executor
from config import MAX_RETRY_COUNT

logger = logging.getLogger(__name__)

try:
    from tracing.tracer import TraceContext, get_current_trace_id
except ImportError:
    TraceContext = None
    get_current_trace_id = lambda: ""


def _node_span(node_name: str, input_data: dict = None, output_data: dict = None, metadata: dict = None):
    """记录工作流节点的追踪 span（尽最大努力，绝不抛异常）"""
    try:
        trace_id = get_current_trace_id()
        if trace_id:
            logger.info(f"[Trace:{trace_id}] NODE '{node_name}' " +
                       (json.dumps(metadata, ensure_ascii=False) if metadata else ""))
    except Exception:
        pass


class WorkflowState(TypedDict):
    """LangGraph 工作流中流转的状态数据"""
    question: str              # 用户原始问题
    original_question: str     # 首轮问题（澄清对话中不变）
    schema_context: dict       # Schema检索结果
    clarification_needed: bool # 是否需要澄清
    clarified_question: str    # 澄清后的改写问题
    follow_up_questions: list  # 反问用户的问题列表
    sql_hint: str              # 给SQL生成器的提示
    alternative_questions: list # 建议的替代问题
    conversation_history: list # 澄清对话历史
    intent: str                # 意图分类结果
    intent_name: str           # 意图中文名
    intent_method: str         # 分类方法(rule/llm)
    topic_id: str              # LLM 语义话题标签（同话题同标签，检索锚的依据）
    sql: str                   # LLM生成的SQL
    validation_result: dict    # SQL校验结果
    retry_count: int           # SQL修正已重试次数
    query_result: dict         # SQL执行结果
    chart_recommendation: dict # 图表推荐
    sql_explanation: str       # NL回译
    nl_answer: str             # NL聊天式回答
    analysis_text: str         # 分析类问题的LLM解释
    final_response: dict       # 最终响应
    error: str                 # 全局错误
    progress_cb: object        # 可选：流式进度回调 cb(text)，节点边界调用（事件循环内）
    stream_answer_cb: object   # 可选：流式回答回调 cb(delta)，逐token转发（工作线程调用）
    retrieval_span: object     # 可选：检索质量采集 span（RetrievalSpan 实例）


def _emit_progress(state: WorkflowState, text: str):
    """推送流式进度（尽力而为，绝不抛异常）"""
    cb = state.get("progress_cb")
    if cb:
        try:
            cb(text)
        except Exception:
            pass


def retrieve_schema_node(state: WorkflowState) -> WorkflowState:
    """节点2: Schema检索 — 用改写后的清晰问题搜表（已过LLM理解）"""
    t0 = time.time()
    # 用LLM改写后的问题检索，比原始问题更精准
    question = state.get("clarified_question") or state["question"]
    # 同话题锚：追问沿用话题起始的完整问题，稳定命中同一批表；换话题锚=当前问题，自然发散。
    from agent.conversation_memory import get_current_session
    mem = get_current_session()
    if mem:
        _ctx, anchor = mem.build_topic_context(
            state.get("original_question") or state["question"],
            topic_id=state.get("topic_id", ""))
        if anchor and anchor != question and anchor not in question:
            question = f"{anchor} {question}"
    schema_context = schema_retriever.retrieve(question)

    retrieval_meta = schema_context.get("retrieval", {})
    if retrieval_meta.get("low_confidence"):
        return {
            **state,
            "schema_context": schema_context,
            "clarification_needed": True,
            "follow_up_questions": [
                "请补充要分析的业务对象或指标。",
                "需要查看哪个时间范围或店铺范围？",
            ],
            "alternative_questions": ["本月销售额", "库存不足的商品", "最近7天退款趋势"],
            "retry_count": 0,
        }

    if not schema_context.get("tables"):
        tr = TraceLog.current()
        if tr: tr.record("retrieve_schema", "error", "未找到相关表", (time.time()-t0)*1000)
        return {**state, "error": "未找到与问题相关的数据表，请尝试换个方式描述您的问题。", "retry_count": 0}

    table_names = [t["table"] for t in schema_context.get("tables", [])]
    tr = TraceLog.current()
    if tr: tr.record("retrieve_schema", "ok", f"{len(table_names)}张表({','.join(table_names)})", (time.time()-t0)*1000)

    # 检索质量日志：记录召回来源、表、字段和耗时。
    span = state.get("retrieval_span")
    if span:
        try:
            strategy = retrieval_meta.get("strategy", "")
            keyword_contributed = strategy in {"exact_keyword", "keyword_fallback"} or any(
                "keyword" in item.get("sources", [])
                for item in schema_context.get("evidence", [])
            )
            span.set_latency(retrieve=(time.time() - t0) * 1000)
            span.set_keyword_hit(keyword_contributed)
            span.set_retrieved(
                schema_context.get("tables", []),
                schema_context.get("columns", []),
            )
        except Exception:
            pass

    return {**state, "schema_context": schema_context, "retry_count": 0}


# 意图理解缓存（同问题同Schema → 不重复调LLM）
_understand_cache: dict[str, dict] = {}

async def clarify_question_node(state: WorkflowState) -> WorkflowState:
    """节点1: 查询理解 — LLM先理解意图改写问题，再检索Schema"""
    _emit_progress(state, "正在理解你的问题...")
    question = state["question"]
    schema_context = {}  # 此时还没检索Schema，传空
    history = state.get("conversation_history", [])

    if state.get("error"):
        return state

    from agent.conversation_memory import get_current_session
    mem = get_current_session()
    prev_tid = mem.get_last_topic_id() if mem else ""

    # 缓存命中：同问题（同上一话题标签）直接复用
    cache_key = f"{question}|{len(schema_context.get('tables',[]))}|{prev_tid}"
    if cache_key in _understand_cache and not history:
        cached = _understand_cache[cache_key]
        tr = TraceLog.current()
        if tr: tr.record("understand", "ok",
            f"缓存命中 intent={cached.get('intent')}", 0)
        return {**state, **cached}

    # 多轮澄清：用户已回答了反问 → 合并改写
    if history and state.get("original_question"):
        rewritten = await query_clarifier.rewrite_with_context(
            state["original_question"], question, schema_context
        )
        if rewritten:
            tr = TraceLog.current()
            if tr: tr.record("understand", "ok", f"改写: {rewritten[:40]}...", 0)
            return {
                **state,
                "clarification_needed": False,
                "clarified_question": rewritten,
                "follow_up_questions": [],
                "sql_hint": "",
                "alternative_questions": [],
                "intent": "sales_aggregation",
                "intent_name": "销售统计",
                "intent_method": "rewrite",
                "topic_id": "",
                "question": rewritten,
            }

    # 注入对话历史上下文（同一话题自适应窗口，替代固定2轮）
    if mem and mem.turns and not history:
        ctx_text, _anchor = mem.build_topic_context(question, topic_id=prev_tid, max_turns=8)
        if ctx_text:
            question = f"{ctx_text}\n当前问题: {question}"
            logger.info(f"[Understand] 注入对话上下文 ({len(mem.turns)}轮历史)")

    # ═══ 规则快路径：意图明确的数据查询，跳过LLM理解（省一次LLM往返）═══
    # 仅适用于：无多轮上下文、规则分类命中、且非最需要澄清的 analysis/comparison 意图。
    if not history and not (mem and mem.turns):
        rule = classify_by_rules(question)
        if rule and rule.get("intent") not in ("analysis", "comparison") \
                and rule.get("confidence", 0) >= 0.3:
            new_state = {
                **state,
                "clarification_needed": False,
                "clarified_question": question,
                "follow_up_questions": [],
                "sql_hint": "",
                "alternative_questions": [],
                "intent": rule["intent"],
                "intent_name": rule["intent_name"],
                "intent_method": "rule",
                "topic_id": "",
                "question": question,
            }
            _understand_cache[cache_key] = dict(new_state)
            tr = TraceLog.current()
            if tr: tr.record("understand", "ok",
                f"规则快路径 intent={rule['intent']}", 0)
            logger.info(f"[Understand] 规则快路径: intent={rule['intent']} (跳过LLM)")
            return new_state

    # 一次LLM调用：同时完成澄清判断+意图分类+问题改写+话题标签
    result = await query_clarifier.understand(question, schema_context, history,
                                              prev_topic_id=prev_tid)
    # Token 消耗采集
    span = state.get("retrieval_span")
    if span:
        span.add_tokens(understand=result.pop("_usage", 0))
    tr = TraceLog.current()
    intent = result.get("intent", "sales_aggregation")
    needs_clarify = result.get("needs_clarification", False)

    # 详细记录：如果触发澄清，说明原因
    if needs_clarify:
        reason = result.get("reason", "未知")
        follow_ups = result.get("follow_up_questions", [])
        detail = f"需要澄清: {reason} (反问{len(follow_ups)}条)"
        logger.warning(f"[Understand] CLARIFY: {reason} | 反问: {follow_ups}")
        if tr: tr.record("understand", "clarify", detail, 0)
    else:
        rewritten = result.get("clarified_question", question)
        if tr: tr.record("understand", "ok",
            f"intent={intent} → '{rewritten[:30]}...'", 0)

    new_state = {
        **state,
        "clarification_needed": needs_clarify,
        "clarified_question": result.get("clarified_question", question),
        "follow_up_questions": result.get("follow_up_questions", []),
        "sql_hint": result.get("sql_hint", ""),
        "alternative_questions": result.get("alternative_questions", []),
        "intent": intent,
        "intent_name": intent,
        "intent_method": "llm",
        "topic_id": result.get("topic_id", ""),
        "question": result.get("clarified_question", question),
    }

    # 写入缓存（仅非澄清对话，下次同问题秒返）
    if not needs_clarify and not history:
        _understand_cache[cache_key] = dict(new_state)
        if len(_understand_cache) > 100:
            _understand_cache.pop(next(iter(_understand_cache)))

    return new_state


async def generate_sql_node(state: WorkflowState) -> WorkflowState:
    """节点4: SQL生成 — 带意图模板的LLM生成"""
    _emit_progress(state, "正在生成SQL...")
    question = state.get("clarified_question") or state["question"]
    schema_context = state["schema_context"]
    intent = state.get("intent", "")

    if state.get("error") and "未找到" in state.get("error", ""):
        return state

    # 如果澄清判断需要反问，跳过SQL生成
    if state.get("clarification_needed"):
        tr = TraceLog.current()
        if tr: tr.record("generate_sql", "skipped", "因澄清反问，跳过", 0)
        return state

    t0 = time.time()
    try:
        result = await sql_generator.generate(question, schema_context, intent=intent)
        if isinstance(result, dict):
            sql = result.get("sql", "")
            explanation = result.get("explanation", "")
            tokens = result.get("tokens", 0)
        else:
            sql, explanation, tokens = result, "", 0
        tr = TraceLog.current()
        if tr: tr.record("generate_sql", "ok",
            f"{len(sql)}字符 intent={intent}", (time.time()-t0)*1000)
        # 检索质量日志：SQL + 意图 + Token 消耗
        span = state.get("retrieval_span")
        if span:
            try:
                span.set_sql(sql, intent=intent, intent_method=state.get("intent_method", ""))
                span.add_tokens(generate=tokens)
                span.set_latency(generate=(time.time()-t0)*1000)
            except Exception:
                pass
    except Exception as e:
        tr = TraceLog.current()
        if tr: tr.record("generate_sql", "error", str(e)[:60], (time.time()-t0)*1000)
        return {**state, "error": f"SQL生成失败: {e}"}

    return {**state, "sql": sql, "sql_explanation": explanation}


def validate_sql_node(state: WorkflowState) -> WorkflowState:
    """节点3: SQL校验 — 三道闸门（语法→注入→权限）确保SQL安全"""
    sql = state.get("sql", "")
    retry_count = state.get("retry_count", 0)

    if state.get("error"):
        return state

    validation_result = sql_validator.validate(sql)
    # 检索质量日志：校验结果
    span = state.get("retrieval_span")
    if span:
        span.set_validation(validation_result.get("stage", ""))
    return {**state, "validation_result": validation_result}


async def fix_sql_node(state: WorkflowState) -> WorkflowState:
    """节点4: SQL修正 — 校验失败时把错误信息反馈给LLM重新生成SQL"""
    question = state["question"]
    schema_context = state["schema_context"]
    current_sql = state.get("sql", "")
    error_msg = state["validation_result"].get("error", "")
    retry_count = state.get("retry_count", 0)

    if retry_count >= MAX_RETRY_COUNT:
        return {
            **state,
            "error": f"SQL修正失败（已重试{MAX_RETRY_COUNT}次）。建议联系数据分析师手动编写SQL。\n最后一次错误：{error_msg}",
        }

    result = await sql_generator.fix_sql(question, schema_context, current_sql, error_msg)
    if isinstance(result, dict):
        fixed_sql = result.get("sql", "")
        fix_tokens = result.get("tokens", 0)
    else:
        fixed_sql, fix_tokens = result, 0
    # 检索质量日志：SQL修正 Token 消耗
    span = state.get("retrieval_span")
    if span:
        span.add_tokens(fix=fix_tokens)
    return {**state, "sql": fixed_sql, "retry_count": retry_count + 1}


async def execute_sql_node(state: WorkflowState) -> WorkflowState:
    """节点5: SQL执行 + 结果校验 + NL回译 + 图表推荐 + NL回答生成"""
    _emit_progress(state, "正在执行查询...")
    sql = state.get("sql", "")

    if state.get("error"):
        tr = TraceLog.current()
        if tr: tr.record("execute_sql", "skipped", f"因上游错误跳过: {(state.get('error','') or '')[:40]}", 0)
        return state

    t0 = time.time()
    query_result = executor.execute(sql)

    tr = TraceLog.current()
    if not query_result.get("success"):
        if tr: tr.record("execute_sql", "error", query_result.get("error","")[:60], (time.time()-t0)*1000)
        return {
            **state,
            "error": query_result.get("error", "SQL执行失败"),
            "query_result": query_result,
        }

    # 执行结果检查（空结果/超行数/数值异常/慢查询）
    query_result = result_checker.check(query_result)

    if tr: tr.record("execute_sql", "ok",
        f"{query_result.get('row_count',0)}行 {query_result.get('execution_time_ms',0):.0f}ms",
        query_result.get('execution_time_ms',0))

    # 空结果：先判断是不是"数据没覆盖到查询时间"，给用户明确原因
    if query_result.get("row_count", 0) == 0 and query_result.get("success"):
        original_q = state.get("original_question") or state.get("question", "")
        reason = _explain_empty_reason(sql)
        if reason:
            query_result["_empty_reason"] = reason
            if tr: tr.record("execute_sql", "clarify", f"空结果原因: {reason[:40]}", 0)
        # 空结果智能反问：搜索相似产品名帮助用户澄清
        similar = _find_similar_products(original_q, sql)
        if similar:
            query_result["_empty_suggestions"] = similar
            if tr: tr.record("execute_sql", "clarify",
                f"空结果，找到{len(similar)}个相似产品", 0)

    # NL回译：SQL生成时已一并返回（v3.0合并了explain_sql，省一次LLM往返）
    explanation = state.get("sql_explanation", "") or sql
    if not explanation:
        explanation = f"执行了SQL查询：{sql}"

    # 按需生成图表：只有用户明确要求可视化时才生成
    chart_rec = {"chart_type": None, "reason": "非可视化查询"}
    viz_keywords = ["画图", "画个图", "生成图", "可视化", "柱状图", "饼图", "折线图",
                    "趋势图", "做图", "出图", "绘", "图表展示", "图表",
                    "对比", "比较", "占比", "排名", "排行", "分布", "走势",
                    "趋势", "每月", "每日", "每周", "各月", "同比", "环比",
                    "增长", "下降", "变化", "走势图"]
    original_q = state.get("original_question") or state.get("question", "")
    if any(kw in original_q for kw in viz_keywords):
        chart_rec = chart_recommender.recommend(query_result)

    # 分析类意图：SQL查完数据后，自动调LLM分析"为什么"
    analysis_text = ""
    intent = state.get("intent", "")
    original_q = state.get("original_question") or state.get("question", "")
    if intent == "analysis" and query_result.get("data"):
        analysis_text = await asyncio.to_thread(
            _analyze_why, original_q, sql, query_result, state.get("schema_context", {})
        )
        if tr: tr.record("analysis_summary", "ok", f"{len(analysis_text)}字符分析", 0)

    # 默认使用确定性摘要（0 Token）；仅显式开启时调用 LLM 润色。
    nl_answer = ""
    answer_tokens = 0
    if query_result.get("data"):
        from config import NL_ANSWER_LLM

        stream_cb = state.get("stream_answer_cb")
        nl_answer = _generate_nl_answer(original_q, sql, query_result)
        if NL_ANSWER_LLM and intent != "analysis":
            _emit_progress(state, "正在生成回答...")
            if stream_cb:
                llm_answer = await sql_generator.answer_stream(
                    original_q, sql, query_result, stream_cb
                )
            else:
                llm_answer = await sql_generator.answer_summary(
                    original_q, sql, query_result
                )
            if llm_answer:
                nl_answer = llm_answer
                answer_tokens = len(llm_answer)
        elif stream_cb and nl_answer:
            stream_cb(nl_answer)
    elif query_result.get("row_count", 0) == 0:
        # 空结果也要给用户一句"原因/建议"，而不是什么都不说
        nl_answer = _generate_nl_answer(original_q, sql, query_result)
    if tr: tr.record("nl_answer", "ok", f"{len(nl_answer)}字符", 0)

    # 检索质量日志：结果 + 回答Token + 写入DB
    span = state.get("retrieval_span")
    if span:
        try:
            span.set_result(query_result.get("row_count", 0),
                           error=state.get("error", ""))
            span.set_latency(execute=(time.time()-t0)*1000)
            span.add_tokens(answer=answer_tokens)
            span.flush()
        except Exception:
            pass

    return {
        **state,
        "query_result": query_result,
        "sql_explanation": explanation,
        "chart_recommendation": chart_rec,
        "analysis_text": analysis_text,
        "nl_answer": nl_answer,
    }


def build_final_response(state: WorkflowState) -> WorkflowState:
    """节点6: 构建最终响应 — 打包所有结果返回给前端"""
    final_response = {
        "question": state.get("original_question") or state["question"],
        "clarification_needed": state.get("clarification_needed", False),
        "clarified_question": state.get("clarified_question", ""),
        "follow_up_questions": state.get("follow_up_questions", []),
        "alternative_questions": state.get("alternative_questions", []),
        "sql": state.get("sql", ""),
        "sql_explanation": state.get("sql_explanation", ""),
        "nl_answer": state.get("nl_answer", ""),
        "validation": state.get("validation_result", {}),
        "result": state.get("query_result", {}),
        "chart": state.get("chart_recommendation", {}),
        "analysis_text": state.get("analysis_text", ""),
        "schema_retrieved": {
            "tables": [item["table"] for item in state.get("schema_context", {}).get("tables", [])],
            "columns": state.get("schema_context", {}).get("columns", []),
            "relationships": state.get("schema_context", {}).get("relationships", []),
            "metrics": state.get("schema_context", {}).get("metrics", []),
            "join_paths": state.get("schema_context", {}).get("join_paths", []),
            "retrieval": state.get("schema_context", {}).get("retrieval", {}),
        },
        "retry_count": state.get("retry_count", 0),
        "topic_id": state.get("topic_id", ""),
        "empty_suggestions": state.get("query_result", {}).get("_empty_suggestions", []),
        "error": state.get("error"),
    }
    return {**state, "final_response": final_response}


def _analyze_why(question: str, sql: str, query_result: dict, schema_context: dict) -> str:
    """
    分析类问题（含"为什么/原因"）的LLM深度分析。
    SQL查了数据，现在用LLM结合评论解释原因。
    """
    from openai import OpenAI
    from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

    if not query_result.get("data"):
        return ""

    # 获取结果中的产品名
    product_names = []
    for row in query_result["data"][:5]:
        for key, val in row.items():
            if "product" in key.lower() or "name" in key.lower():
                product_names.append(str(val))

    if not product_names:
        return ""

    # 查这些产品的差评
    from db.executor import executor as db_exec
    reviews_text = ""
    try:
        for name in product_names[:3]:
            r = db_exec.execute("""
                SELECT pr.rating, pr.review_text, pr.sentiment
                FROM product_reviews pr
                JOIN products p ON pr.product_id = p.product_id
                WHERE p.product_name = ?
                  AND pr.sentiment IN ('差评','中评')
                ORDER BY pr.rating ASC LIMIT 3
            """, (name,))
            for rev in (r.get("data") or [])[:2]:
                reviews_text += f"\n[{name}] {rev.get('rating','')}星: {rev.get('review_text','')[:100]}"
    except Exception:
        pass

    try:
        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
        data_summary = json.dumps(query_result["data"][:5], ensure_ascii=False)
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{
                "role": "system",
                "content": "你是电商运营分析师。根据销量数据和客户评价，用1-2句话总结产品表现差的原因。只输出分析，不要SQL、不要markdown。"
            }, {
                "role": "user",
                "content": f"用户问题: {question}\nSQL: {sql}\n查询结果: {data_summary}\n客户评价: {reviews_text or '暂无评价数据'}\n请总结原因。"
            }],
            temperature=0.3, max_tokens=300,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"[Analyze] LLM分析失败: {e}")
        return ""


def _explain_empty_reason(sql: str) -> str:
    """
    空结果时尝试找出原因（目前针对"相对时间查询但数据没覆盖到"的场景）。
    返回说明文字；无需说明返回空串。
    例：今天是8月，但库里数据截止到7月 → "数据库数据截止到7月，本月暂无订单数据…"
    """
    if not sql or not re.search(r"'now'|date\('now'\)|datetime\('now'", sql, re.IGNORECASE):
        return ""
    try:
        from db.executor import executor as db_exec
        r = db_exec.execute("SELECT MAX(order_date) AS max_d FROM orders")
        max_d = (r.get("data") or [{}])[0].get("max_d")
        if not max_d:
            return ""
        from datetime import datetime
        max_dt = datetime.strptime(str(max_d)[:10], "%Y-%m-%d")
        now = datetime.now()
        # 数据已覆盖当前月 → 空结果另有原因（条件过严等），不归因于数据缺失
        if now.year == max_dt.year and now.month == max_dt.month:
            return ""
        return (f"数据库数据截止到 {max_dt.year}年{max_dt.month}月，当前是 {now.year}年{now.month}月，"
                f"暂无当月订单数据，所以本月/相对时间的查询查不到结果。")
    except Exception:
        return ""


def _find_similar_products(question: str, sql: str) -> list:
    """从SQL中提取产品名关键词，搜索数据库中相似的替代产品"""
    import re
    from db.executor import executor

    # 从SQL或问题中提取可能的产品名
    keywords = []
    for pattern in [r"LIKE\s+'%(.+?)%'", r"=\s*'(.+?)'"]:
        m = re.search(pattern, sql, re.IGNORECASE)
        if m:
            keywords.append(m.group(1))

    # 也从未命中SQL的问题中提取关键词
    for word in re.findall(r'[一-鿿]{2,4}', question):
        keywords.append(word)

    suggestions = []
    for kw in keywords[:3]:
        try:
            r = executor.execute(
                "SELECT product_name, category FROM products WHERE product_name LIKE ? LIMIT 5",
                (f"%{kw}%",),
            )
            for item in (r.get("data") or []):
                if item["product_name"] not in suggestions:
                    suggestions.append(item["product_name"])
        except Exception:
            pass

    # 如果没找到相似产品，返回所有产品名供参考
    if not suggestions:
        try:
            r = executor.execute("SELECT product_name FROM products WHERE is_active=1 LIMIT 8")
            suggestions = [item["product_name"] for item in (r.get("data") or [])]
        except Exception:
            pass

    return suggestions[:5]


# 列名 → (中文标签, 单位)，用于自然语言表述
_COLUMN_SEMANTICS = {
    "stock_quantity": ("库存", "件"),
    "total_amount": ("销售额", "元"),
    "total_sales": ("销售额", "元"),
    "monthly_total": ("月销售额", "元"),
    "unit_price": ("单价", "元"),
    "order_count": ("订单数", "单"),
    "total_qty": ("总销量", "件"),
    "quantity": ("数量", "件"),
    "avg_rating": ("平均评分", "分"),
    "rating": ("评分", "分"),
    "product_count": ("产品数", "个"),
    "customer_count": ("客户数", "人"),
    "refund_count": ("退款数", "单"),
    "refund_rate": ("退款率", "%"),
    "total_spent": ("消费总额", "元"),
    "avg_order": ("客单价", "元"),
    "growth": ("增长率", "%"),
    "total_sales_yesterday": ("昨日销售额", "元"),
}


def _product_keyword_from_sql(sql: str) -> str:
    """从SQL里提取 LIKE/等值 匹配的产品名（如 '%显示器%' → '显示器'）"""
    if not sql:
        return ""
    m = (re.search(r"product_name\s+LIKE\s+'%(.+?)%'", sql, re.IGNORECASE)
         or re.search(r"product_name\s*=\s*'(.+?)'", sql, re.IGNORECASE))
    return m.group(1).strip() if m else ""


def _pick_name_col(columns: list, row: dict):
    """优先选名称类列（product_name/name），否则第一个文本列"""
    for c in columns:
        lc = c.lower()
        if "name" in lc or "product" in lc:
            return c
    for c in columns:
        if isinstance(row.get(c), str):
            return c
    return None


def _pick_metric_col(columns: list, row: dict):
    """选第一个数值列作为指标"""
    for c in columns:
        if isinstance(row.get(c), (int, float)):
            return c
    return None


def _fmt_value(val, unit: str = "") -> str:
    if isinstance(val, float):
        s = f"{val:,.0f}" if val == int(val) else f"{val:,.1f}"
    else:
        s = str(val)
    return f"{s}{unit}"


def _generate_nl_answer(question: str, sql: str, query_result: dict) -> str:
    """基于查询结果生成口语化摘要（纯规则，0延迟）"""
    data = query_result.get("data", [])
    columns = query_result.get("columns", [])
    row_count = query_result.get("row_count", len(data))

    if row_count == 0:
        # 数据覆盖原因优先（如"本月暂无订单数据"）
        reason = query_result.get("_empty_reason", "")
        if reason:
            return reason
        suggestions = query_result.get("_empty_suggestions", [])
        if suggestions:
            names = "、".join(suggestions)
            return f"没找到你要的产品 😅 数据库里有这些：{names}。你是想查哪一个？"
        return "没查到匹配的数据，要不要换个关键词试试？"

    # 单行结果：口语化表述，如「显示器的库存是80件」
    if row_count == 1 and data:
        row = data[0]
        name_col = _pick_name_col(columns, row)
        metric_col = _pick_metric_col(columns, row)
        name = str(row.get(name_col, "")).strip() if name_col else ""
        if not name:
            name = _product_keyword_from_sql(sql)
        if metric_col:
            label, unit = _COLUMN_SEMANTICS.get(metric_col, (metric_col, ""))
            val_str = _fmt_value(row.get(metric_col), unit)
            if name:
                return f"{name}的{label}是{val_str}。"
            return f"{label}是{val_str}。"
        if name:
            return f"{name}：{'，'.join(f'{c}={row.get(c)}' for c in columns[:3])}。"

    # 多行：前3个名字+第一个数值
    items = []
    for row in data[:3]:
        name_col = _pick_name_col(columns, row)
        metric_col = _pick_metric_col(columns, row)
        name = str(row.get(name_col, "")).strip() if name_col else ""
        if metric_col:
            label, unit = _COLUMN_SEMANTICS.get(metric_col, (metric_col, ""))
            val_str = _fmt_value(row.get(metric_col), unit)
            items.append(f"{name}：{val_str}" if name else f"{label} {val_str}")
        else:
            items.append(name or str(row.get(columns[0], "")))
    tail = "…" if row_count > 3 else ""
    return f"共{row_count}条。{'，'.join(items)}{tail}"


# ═══════════════════════════════════════════════════════════════
# 路由函数 — 控制节点间的跳转逻辑
# ═══════════════════════════════════════════════════════════════

def should_clarify_or_sql(state: WorkflowState) -> str:
    """澄清判断后：返回反问 或 继续SQL生成"""
    if state.get("error"):
        return "build_response"
    if state.get("clarification_needed"):
        return "build_response"    # 需要反问 → 直接返回
    return "generate_sql"          # 问题清晰 → 生成SQL


def should_generate_after_retrieval(state: WorkflowState) -> str:
    if state.get("error") or state.get("clarification_needed"):
        return "build_response"
    return "generate_sql"


def should_retry_sql(state: WorkflowState) -> str:
    """决定SQL校验失败后的走向：修正SQL / 跳过执行 / 继续执行"""
    if state.get("error"):
        return "build_response"
    validation = state.get("validation_result", {})
    retry_count = state.get("retry_count", 0)

    if not validation.get("valid") and retry_count < MAX_RETRY_COUNT:
        return "fix_sql"
    elif not validation.get("valid"):
        return "build_response"
    return "execute_sql"


def should_execute(state: WorkflowState) -> str:
    """决定是否执行SQL：有错误直接跳到最后，否则继续"""
    if state.get("error"):
        return "build_response"
    return "execute_sql"


# ═══════════════════════════════════════════════════════════════
# 构建 LangGraph 工作流
# ═══════════════════════════════════════════════════════════════

def build_workflow() -> StateGraph:
    """构建并编译 LangGraph 状态图"""
    workflow = StateGraph(WorkflowState)

    # 注册7个节点
    workflow.add_node("retrieve_schema", retrieve_schema_node)
    workflow.add_node("clarify_question", clarify_question_node)
    workflow.add_node("generate_sql", generate_sql_node)
    workflow.add_node("validate_sql", validate_sql_node)
    workflow.add_node("fix_sql", fix_sql_node)
    workflow.add_node("execute_sql", execute_sql_node)
    workflow.add_node("build_response", build_final_response)

    workflow.set_entry_point("clarify_question")

    # 条件边：理解通过→检索Schema，需要反问→直接返回用户
    workflow.add_conditional_edges(
        "clarify_question",
        should_clarify_or_sql,
        {"generate_sql": "retrieve_schema", "build_response": "build_response"},
    )

    # Schema 低置信时先澄清，不冒险生成 SQL。
    workflow.add_conditional_edges(
        "retrieve_schema",
        should_generate_after_retrieval,
        {"generate_sql": "generate_sql", "build_response": "build_response"},
    )
    workflow.add_edge("generate_sql", "validate_sql")

    # 条件边：校验不通过→修正SQL，通过→执行
    workflow.add_conditional_edges(
        "validate_sql",
        should_retry_sql,
        {
            "fix_sql": "fix_sql",
            "execute_sql": "execute_sql",
            "build_response": "build_response",
        },
    )

    # 修正后回到校验节点（形成重试循环）
    workflow.add_edge("fix_sql", "validate_sql")

    # 执行完→构建响应→结束
    workflow.add_edge("execute_sql", "build_response")
    workflow.add_edge("build_response", END)

    return workflow.compile()


# 全局单例，启动时编译一次，所有请求复用
app_workflow = build_workflow()
