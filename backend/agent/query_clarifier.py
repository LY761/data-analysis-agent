"""
查询理解器 — 一次LLM调用同时完成：澄清判断 + 意图分类 + 问题改写

替换了原来的"正则快筛→LLM澄清→独立意图分类"三步走，改为一步到位。
成本: ~500 Token/次，换来真正的语义理解而非关键词匹配。
"""
import asyncio
import json
import logging
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger(__name__)

UNDERSTAND_PROMPT = """你是数据库查询理解助手。分析用户问题，输出JSON。

## 核心原则：能查就查，不要没事反问用户！

绝大多数问题都能直接生成SQL，只有极少数真正模糊的才需要反问。
以下情况不需要反问，直接改写：
- "销量不好" → 改写为"按销量升序排列" → intent=ranking
- "上个月" → 保留原样，SQL生成时会自动转为日期函数
- "哪些" → 保留原样 → intent=ranking
- 所有含具体指标的问法（销售额/订单/库存/退款/客户/评分等）→ 直接改写

## 需要反问的极少数情况：
- 用户问的概念数据库里绝对不存在且无法推断（如问"用户满意度"但没有相关表和字段）
- 问题完全无法理解（如乱码、完全不相关的领域）

## 意图类型
- sales_aggregation: 求总额/平均/计数
- ranking: 排名/取前N/最高最低/最好/最差/哪些
- filtering: 条件筛选（库存不足/退款）
- comparison: 对比分析（环比/同比/涨跌）
- grouping: 分组统计（各/每个/按...分/占比）
- detail_lookup: 查具体记录
- analysis: 含"为什么/原因/如何改进"的综合分析

## 话题标签 topic_id（用于对话记忆分组，保证同话题检索同表）
给当前问题打一个简短话题标签（如 "蓝牙耳机销售分析" / "退货分析" / "库存盘点"）。
规则：
  - 当前问题**延续**上一轮话题（上一轮话题标签见下方上下文）→ **必须复用**那个标签
  - 当前问题**开启新话题**（如"按地区看销售额"是自足的新问法）→ 输出一个新的简短标签
  - 首轮对话或没有上一轮标签 → 输出一个新标签

## 输出JSON格式
{
  "needs_clarification": true/false,
  "reason": "一句话",
  "intent": "意图类型",
  "topic_id": "简短话题标签",
  "clarified_question": "改写后的清晰问题（不需要反问时必填）",
  "sql_hint": "给SQL生成器的指引（如：用ORDER BY ASC表示'不好'、用LEFT JOIN查所有产品包括无订单的）",
  "follow_up_questions": [],
  "alternative_questions": []
}

只输出JSON。"""


class QueryClarifier:
    """查询理解器：一次LLM调用 = 澄清判断 + 意图分类 + 问题改写"""

    def __init__(self):
        self.client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

    async def understand(self, question: str, schema_context: dict,
                         conversation_history: list = None,
                         prev_topic_id: str = "") -> dict:
        """
        综合分析用户问题。

        返回: {needs_clarification, intent, topic_id, clarified_question, sql_hint,
               follow_up_questions, alternative_questions, reason}

        prev_topic_id: 上一轮话题标签，让 LLM 判断当前是延续还是新话题。

        异步化：OpenAI调用丢到线程池，避免阻塞事件循环。
        """
        # Schema摘要
        tables = schema_context.get("tables", [])
        schema_summary = "数据库表: " + ", ".join(
            t.get("table", "") for t in tables
        ) if tables else "（Schema检索中）"

        # 对话上下文
        ctx = ""
        if conversation_history:
            ctx = "\n对话历史:\n"
            for m in conversation_history[-4:]:
                ctx += f"  {m['role']}: {m['content'][:80]}\n"

        prev_line = f"上一轮话题标签: {prev_topic_id}" if prev_topic_id else "上一轮话题标签: （无）"
        user_prompt = f"""{schema_summary}
{ctx}
{prev_line}
用户问题: {question}"""

        def _llm_call():
            return self.client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": UNDERSTAND_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=400,
                response_format={"type": "json_object"},
            )

        try:
            response = await asyncio.to_thread(_llm_call)
            raw = response.choices[0].message.content
            if not raw:
                logger.warning(f"[Understand] LLM返回空内容! finish={response.choices[0].finish_reason}")
                return self._fallback(question)
            result = json.loads(raw)
        except json.JSONDecodeError:
            raw = response.choices[0].message.content or ""
            logger.warning(f"[Understand] JSON解析失败，原始返回({len(raw)}字符): {raw[:200]}")
            return self._fallback(question)
        except Exception as e:
            logger.warning(f"[Understand] LLM调用异常: {e}")
            return self._fallback(question)

        # 附带 token 用量（供检索质量日志）
        result["_usage"] = response.usage.total_tokens if response.usage else 0

        logger.info(f"[Understand] intent={result.get('intent','?')} "
                    f"clarify={result.get('needs_clarification',False)} "
                    f"reason={result.get('reason','')[:50]}")
        return result

    def _fallback(self, question: str) -> dict:
        """LLM调用失败时的兜底"""
        return {
            "needs_clarification": False,
            "reason": "LLM异常，默认放行",
            "intent": "sales_aggregation",
            "topic_id": "",
            "clarified_question": question,
            "sql_hint": "",
            "follow_up_questions": [],
            "alternative_questions": [],
        }

    async def rewrite_with_context(self, original_question: str, user_answer: str,
                                   schema_context: dict) -> str:
        """多轮澄清：合并原问题+用户回答为一个清晰问题"""
        schema_summary = ", ".join(
            t.get("table", "") for t in schema_context.get("tables", [])
        )

        prompt = f"""用户原本问: {original_question}
用户澄清回答: {user_answer}
数据库有这些表: {schema_summary}

请将用户问题改写为一个清晰、可直接生成SQL的自然语言问题。
只输出改写后的问题，不要解释。"""

        def _llm_call():
            return self.client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1, max_tokens=200,
            )

        try:
            response = await asyncio.to_thread(_llm_call)
            rewritten = response.choices[0].message.content.strip()
            logger.info(f"[Understand] Rewrite: '{original_question[:30]}...' → '{rewritten[:60]}...'")
            return rewritten
        except Exception as e:
            logger.warning(f"[Understand] Rewrite failed: {e}")
            return f"{original_question}（补充说明：{user_answer}）"


query_clarifier = QueryClarifier()
