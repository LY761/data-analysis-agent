"""智能问数 Agent 路由器。

只负责确定当前请求应走规则回复、预写查询、NL2SQL 或澄清流程。
知识库、联网搜索、爬虫、竞品和选品能力不属于本项目主链路。
"""

import json
import logging
import re

import httpx
from openai import OpenAI

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger(__name__)

_ALLOWED_MODES = {"chat", "sql_query", "quick_card", "clarify"}

ROUTER_PROMPT = """你是电商数据分析 Agent 的意图路由器，只处理以下四类请求：

1. chat：问候、帮助、能力说明，或电商指标概念解释。
2. sql_query：必须查询企业数据库才能回答的问题。
3. clarify：缺少指标、对象或时间范围，无法安全查询。
4. quick_card：仅当问题能明确映射到给定快捷查询键时使用。

项目不负责天气、新闻、百科、法律、知识库、爬虫、选品或竞品调研。遇到越界请求，使用 chat 并简短说明能力边界。

输出严格 JSON：
{
  "mode": "chat|sql_query|quick_card|clarify",
  "reply": "chat 或 clarify 的简短回复",
  "rewritten": "sql_query 的清晰查询问题",
  "card_key": "quick_card 的快捷查询键",
  "follow_up_questions": ["最多两个澄清问题"],
  "reason": "一句话判断依据"
}
"""


class AgentRouter:
    """用确定性规则覆盖高频请求，仅在必要时调用小模型路由。"""

    GREETINGS = {
        "你好": "你好！我是电商数据分析助手。可以查询销售、订单、库存、退款、客户和商品数据，也能生成经营诊断、报告与自动化任务。",
        "您好": "您好！可以直接告诉我指标、对象和时间范围，例如“上月各品类销售额”。",
        "hi": "你好！请直接告诉我想查询的电商指标和时间范围。",
        "hello": "你好！请直接告诉我想查询的电商指标和时间范围。",
        "嗨": "你好！想先看销售、订单、库存还是退款数据？",
        "在吗": "在的。请直接输入要查询的指标、对象和时间范围。",
        "在不在": "在的。请直接输入要查询的指标、对象和时间范围。",
        "你是谁": "我是电商数据分析助手，核心能力是智能问数、经营诊断、数据看板、版本化报告和自动化工作流。",
        "能做什么": "我支持智能问数、快捷指标、商品与店铺诊断、经营看板、报告生成和自动化工作流。",
        "帮助": "示例：上月销售额、库存不足的商品、退款率最高的商品、最近 7 天销售趋势。",
        "help": "示例：上月销售额、库存不足的商品、退款率最高的商品、最近 7 天销售趋势。",
        "谢谢": "不客气，还需要分析哪个指标？",
        "感谢": "不客气，还需要分析哪个指标？",
        "再见": "再见，有需要随时回来查看经营数据。",
        "拜拜": "再见，有需要随时回来查看经营数据。",
    }

    QUICK_CARD_KEYWORDS = {
        "monthly_sales": ["本月销售", "这个月卖了多少", "本月营业额"],
        "stock_alert": ["库存不足", "缺货", "库存预警", "快没了"],
        "last_7days_trend": ["最近7天", "最近 7 天", "过去一周"],
        "refund_analysis": ["退款情况", "退货情况", "退单情况"],
        "top5_products": ["卖得最好", "热销top5", "销量最高的5个"],
        "category_sales": ["类别销售", "品类占比", "各类别销售"],
        "region_sales": ["地区销售", "哪里卖得好", "区域销售"],
        "payment_methods": ["支付方式", "微信还是支付宝"],
    }

    SQL_QUERY_KEYWORDS = [
        "销售", "订单", "库存", "退款", "退货", "客户", "会员", "产品", "商品",
        "销量", "金额", "多少", "排名", "排行", "最高", "最低", "最好", "最差",
        "趋势", "对比", "环比", "同比", "占比", "利润", "毛利", "收入", "成本",
        "地区", "区域", "类别", "品类", "分类", "支付", "评价", "评分", "投诉",
        "上个月", "本月", "这个月", "上月", "最近", "昨天", "今天", "今年", "去年",
        "查询", "查一下", "统计", "报表", "数据", "卖得", "热销", "增长", "转化率",
        "客单价", "复购率", "动销率", "roas", "gmv",
    ]

    COMPLEX_INTENT_KEYWORDS = [
        "最高的", "最贵的", "最便宜的", "最差的", "最多的", "最少的",
        "排名", "排行", "对比", "比较", "且", "并", "同时", "每个", "各",
        "哪个", "哪些", "类别的", "类目", "没有", "无",
    ]

    VAGUE_MESSAGES = {"帮我分析一下", "分析一下", "帮我看看", "看一下", "分析数据"}
    CONCEPT_PREFIXES = ("什么是", "解释一下", "怎么算", "如何计算", "定义")

    def __init__(self):
        self.client = OpenAI(
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL,
            timeout=httpx.Timeout(8.0, connect=3.0),
        )
        self.cache: dict[str, dict] = {}

    def route(self, user_message: str, conversation_history: list | None = None) -> dict:
        """返回统一路由结果；有对话上下文时不复用无上下文缓存。"""
        message = (user_message or "").strip()
        if not message:
            return self._clarify("请输入想分析的指标、对象和时间范围。")

        if not conversation_history and message in self.cache:
            return dict(self.cache[message])

        stripped = message.rstrip("？?！!。.~～ ")
        lowered = stripped.lower()
        for greeting, reply in self.GREETINGS.items():
            if lowered == greeting.lower() or (
                greeting.lower() in lowered and len(stripped) <= len(greeting) + 3
            ):
                return self._cache(message, {"mode": "chat", "reply": reply, "reason": "固定问候快路径"})

        if stripped in self.VAGUE_MESSAGES:
            return self._clarify("请补充要分析的指标、对象和时间范围。")

        if any(prefix in message for prefix in self.CONCEPT_PREFIXES):
            metric_reply = self._metric_definition_reply(message)
            if metric_reply:
                return self._cache(message, {
                    "mode": "chat",
                    "reply": metric_reply,
                    "reason": "指标语义目录快路径",
                })
            return self._llm_route(message, conversation_history)

        for card_key, patterns in self.QUICK_CARD_KEYWORDS.items():
            if any(pattern in lowered for pattern in patterns):
                if any(keyword in message for keyword in self.COMPLEX_INTENT_KEYWORDS):
                    break
                return self._cache(message, {
                    "mode": "quick_card",
                    "card_key": card_key,
                    "reason": "预写查询快路径",
                })

        if len(message) >= 3 and any(keyword in lowered for keyword in self.SQL_QUERY_KEYWORDS):
            return self._cache(message, {
                "mode": "sql_query",
                "rewritten": message,
                "reason": "数据指标规则命中",
            })

        return self._llm_route(message, conversation_history)

    def _llm_route(self, user_message: str, conversation_history: list | None = None) -> dict:
        history_text = ""
        if conversation_history:
            recent = conversation_history[-4:]
            history_text = "\n".join(
                f"{item.get('role', '?')}: {str(item.get('content', ''))[:100]}"
                for item in recent
            )

        try:
            response = self.client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": ROUTER_PROMPT},
                    {"role": "user", "content": f"对话历史：\n{history_text}\n用户请求：{user_message}"},
                ],
                temperature=0,
                max_tokens=220,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content
            if not raw:
                raise ValueError("路由模型返回空内容")
            result = json.loads(raw)
            if result.get("mode") not in _ALLOWED_MODES:
                raise ValueError("路由模型返回未知模式")
        except Exception as error:
            logger.warning("路由模型不可用，启用本地兜底: %s", error)
            return self._offline_fallback(user_message)

        if result["mode"] == "clarify" and not result.get("follow_up_questions"):
            result["follow_up_questions"] = [
                "您想分析哪个指标？",
                "需要查看什么时间范围？",
            ]
        if result["mode"] == "sql_query":
            result["rewritten"] = result.get("rewritten") or user_message
        if not conversation_history:
            self._cache(user_message, result)
        return result

    @staticmethod
    def _metric_definition_reply(question: str) -> str:
        from domain.metric_registry import metric_registry

        normalized = question.lower()
        for definition in metric_registry.list():
            if definition.name.lower() in normalized or definition.metric_key.lower() in normalized:
                return (
                    f"{definition.name}：{definition.description} "
                    f"计算口径：{definition.formula}；单位：{definition.unit}；"
                    f"指标版本：{definition.version}。"
                )
        return ""

    def _offline_fallback(self, user_message: str) -> dict:
        if any(keyword in user_message.lower() for keyword in self.SQL_QUERY_KEYWORDS):
            return {
                "mode": "sql_query",
                "rewritten": user_message,
                "reason": "路由模型不可用，数据规则兜底",
            }
        if not re.search(r"[A-Za-z\u4e00-\u9fff]", user_message):
            return self._clarify("请输入想分析的电商数据问题。")
        return {
            "mode": "chat",
            "reply": "当前仅支持电商数据查询、经营诊断、报告和自动化工作流。请提供指标、对象与时间范围。",
            "reason": "路由模型不可用，能力边界兜底",
        }

    def _clarify(self, reply: str) -> dict:
        return {
            "mode": "clarify",
            "reply": reply,
            "follow_up_questions": ["您想分析哪个指标？", "需要查看什么时间范围？"],
            "reason": "缺少必要查询条件",
        }

    def _cache(self, key: str, value: dict) -> dict:
        if len(self.cache) >= 100:
            self.cache.clear()
        self.cache[key] = dict(value)
        return dict(value)


agent_router = AgentRouter()