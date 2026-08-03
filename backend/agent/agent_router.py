"""
Agent路由器 — 聊天式智能调度

用户输入 → LLM判断: 聊天? 查数据? 知识问答?
         → 聊天: 直接LLM回复
         → 查数据: 走SQL流水线
         → 快捷卡片: 匹配预写SQL
         → 知识问答: LLM知识库回复
"""
import json
import logging
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger(__name__)

ROUTER_PROMPT = """你是一个数据分析Agent的路由器。分析用户消息，判断应该怎么处理。

## 处理方式

1. **chat** — 纯聊天/问候/闲聊，不需要查数据库
   例: "你好" "今天天气不错" "谢谢" "你是谁" "能做什么"

2. **sql_query** — 需要查数据库才能回答
   例: "上个月销售额" "库存不足的产品" "哪个客户消费最多"

3. **quick_card** — 可以用快捷卡片回答（已有预写SQL）
   例: "本月销售额" "库存预警" "最近7天趋势" "退款情况"
   可用卡片: monthly_sales, last_month_sales, top5_products, worst5_products,
            stock_alert, category_sales, region_sales, member_analysis,
            payment_methods, month_vs_last, refund_analysis, supplier_analysis,
            last_7days_trend, product_review_score

4. **knowledge** — 问的是通用知识/概念，不涉及当前数据库
   例: "什么是RFM分析" "怎么做用户分群" "电商常用指标有哪些"

5. **clarify** — 问题不够清楚，需要反问
   例: "帮我分析一下"（没说要分析什么）

6. **competitor** — 分析竞品/对比竞品
   例: "分析一下安克创新" "绿联跟我们比怎么样" "竞品分析" "对比一下绿联和我们"

## 输出JSON
{
  "mode": "chat|sql_query|quick_card|knowledge|clarify|competitor_analysis",
  "reply": "如果是chat/knowledge/clarify模式，直接回复用户的内容",
  "rewritten": "如果是sql_query模式，改写后的清晰问题",
  "card_key": "如果是quick_card模式，对应的卡片key",
  "competitor_name": "如果是competitor_analysis模式，提取的竞品公司名",
  "competitor_url": "如果是competitor_analysis且有URL，提取的URL",
  "include_internal": true/false,
  "reason": "判断依据(一句话)"
}"""


class AgentRouter:
    """智能路由器：判断用户意图，分发到不同处理路径"""

    def __init__(self):
        import httpx
        self.client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL,
                            timeout=httpx.Timeout(10.0, connect=5.0))
        self.cache: dict[str, dict] = {}

    # 规则快筛（不调LLM，0延迟）
    COMPETITOR_KEYWORDS = [
        "安克", "anker", "绿联", "ugreen", "正浩", "ecoflow",
        "华宝", "jackery", "倍思", "baseus", "竞品", "竞争对手",
        "跟我们比", "友商",
    ]
    # 问候语 → 固定回复（0延迟）。匹配规则：完全等于，或问候词在短消息里。
    GREETINGS = {
        "你好": "你好！我是数据分析助手 👋\n我能帮你：\n· 📊 查数据：销售额、订单、库存、退款…\n· 🆚 竞品分析：比如「分析一下绿联」\n· 💡 解释概念：比如「什么是RFM分析」\n\n想从哪个开始？",
        "您好": "您好！我是数据分析助手 👋 有什么想查的或想分析的？",
        "hi": "你好！我是数据分析助手 👋 有什么想查的？",
        "hello": "你好！我是数据分析助手 👋 有什么想查的？",
        "hallo": "你好！我是数据分析助手 👋 有什么想查的？",
        "嗨": "嗨！👋 有什么想查的数据或想分析的竞品？",
        "在吗": "在的！我是数据分析助手，想查点什么？",
        "在不在": "在的！我是数据分析助手，想查点什么？",
        "你是谁": "我是数据分析助手，可以帮你：\n1. 查数据库（销售额/订单/库存/退款/客户…）\n2. 做竞品分析（安克/绿联/正浩/华宝/倍思…）\n3. 解释分析概念（RFM、用户分群、指标含义…）",
        "能做什么": "我能做这些：\n1. 📊 查数据 — 「上个月销售额」「库存不足的产品」\n2. 🆚 竞品分析 — 「分析一下绿联」\n3. 💡 知识问答 — 「什么是RFM分析」\n\n直接问我即可！",
        "帮助": "你可以这样问我：\n· 查数据：「上个月卖得最好的5个产品」\n· 竞品：「分析一下安克创新」\n· 概念：「什么是客单价」\n· 趋势：「最近7天销售趋势」",
        "help": "Try asking me things like:\n· 上个月销售额\n· 库存不足的产品\n· 分析一下绿联\n· 什么是RFM分析",
        "谢谢": "不客气！还有什么想分析的吗？😊",
        "感谢": "不客气！还有什么想分析的吗？😊",
        "再见": "再见！👋 有需要随时找我",
        "拜拜": "拜拜！👋 有需要随时找我",
    }
    # 知识/概念类问题：交LLM回答，避免被下面的SQL关键词规则误判成查数据
    KNOWLEDGE_KEYWORDS = [
        "什么是", "啥是", "是什么意思", "解释一下", "介绍一下", "怎么做",
        "如何", "怎么算", "区别", "概念", "含义", "定义", "原理",
    ]
    QUICK_CARD_KEYWORDS = {
        "monthly_sales": ["本月销售", "这个月卖了多少", "本月营业额"],
        "stock_alert": ["库存不足", "缺货", "库存预警", "快没了"],
        "last_7days_trend": ["最近7天", "过去一周", "趋势"],
        "refund_analysis": ["退款", "退货", "退单"],
        "top5_products": ["卖得最好", "热销", "销量最高", "top"],
        "category_sales": ["类别销售", "品类占比", "各类别"],
        "region_sales": ["地区销售", "哪里卖得好", "区域"],
        "payment_methods": ["支付方式", "微信还是支付宝"],
    }
    # 数据分析类关键词快筛：命中直接走SQL流水线，跳过Router的LLM调用（省一次往返）
    SQL_QUERY_KEYWORDS = [
        "销售", "订单", "库存", "退款", "退货", "客户", "会员", "产品", "商品",
        "销量", "金额", "多少", "排名", "排行", "最高", "最低", "最好", "最差",
        "趋势", "对比", "环比", "同比", "占比", "利润", "毛利", "收入", "成本",
        "地区", "区域", "类别", "品类", "分类", "支付", "评价", "评分", "投诉",
        "上个月", "本月", "这个月", "上月", "最近", "昨天", "今天", "今年", "去年",
        "查询", "查一下", "统计", "报表", "数据", "卖得", "热销", "增长",
    ]
    # 市场情报关键词：选品/商品研究
    MARKET_INTEL_KEYWORDS = ["选品", "市场机会", "能不能做", "值得卖吗",
                             "研究一下", "分析一下这个产品", "差评", "痛点"]
    SELECTION_KEYWORDS = ["选品", "市场机会", "能不能做", "值得卖吗", "竞争怎么样"]
    # 明确的内部数据指标词：命中则视为数据查询，不走进市场情报
    DATA_OVERRIDE_KEYWORDS = ["上个月", "本月", "这个月", "上月", "最近", "昨天", "今天",
                              "今年", "去年", "环比", "同比", "销售额", "销量", "订单数",
                              "订单量", "库存"]

    def route(self, user_message: str, conversation_history: list = None) -> dict:
        """
        分析用户消息，返回处理方案。

        设计：规则只管「确定性快路径」（竞品/快捷卡片/问候/数据关键词），
        其余一律交给LLM理解意图（聊天/知识/澄清/查数据）。这样Agent才有对话能力。
        """
        # 缓存命中
        if user_message in self.cache:
            return dict(self.cache[user_message])

        msg = user_message.strip()
        msg_lower = msg.lower()

        # 1. 竞品快筛
        if any(kw in msg_lower for kw in self.COMPETITOR_KEYWORDS):
            r = {"mode": "competitor", "rewritten": msg, "reason": "竞品关键词"}
            self.cache[msg] = r
            return r

        # 2. 快捷卡片匹配（预写SQL，0 Token）
        for card_key, patterns in self.QUICK_CARD_KEYWORDS.items():
            if any(p in msg for p in patterns):
                r = {"mode": "quick_card", "card_key": card_key, "reason": "快捷卡片匹配"}
                self.cache[msg] = r
                return r

        # 3. 问候语 → 固定回复（0延迟，恢复对话能力）
        stripped = msg.strip().rstrip("？?！!。.~～ ")
        for greet, reply in self.GREETINGS.items():
            if stripped == greet or (greet in stripped and len(stripped) <= len(greet) + 3):
                r = {"mode": "chat", "reply": reply, "reason": "问候语"}
                self.cache[msg] = r
                return r

        # 4. 知识/概念类问题 → 交给LLM回答（先于SQL规则，避免误判成查数据）
        if any(kw in msg for kw in self.KNOWLEDGE_KEYWORDS):
            return self._llm_route(msg, conversation_history)

        # 3.5 市场情报（选品/商品研究）—— 先于SQL，但数据查询优先
        if any(kw in msg for kw in self.MARKET_INTEL_KEYWORDS) \
                and not any(k in msg for k in self.DATA_OVERRIDE_KEYWORDS):
            sub = "selection" if any(kw in msg for kw in self.SELECTION_KEYWORDS) else "product"
            r = {"mode": "market_intelligence", "sub": sub, "query": msg, "reason": "市场情报关键词"}
            self.cache[msg] = r
            return r

        # 5. 数据分析类问题快筛（命中直接走SQL流水线，不调LLM）
        if len(msg) >= 4 and any(kw in msg for kw in self.SQL_QUERY_KEYWORDS):
            r = {"mode": "sql_query", "rewritten": msg, "reason": "数据分析关键词"}
            self.cache[msg] = r
            return r

        # 6. 兜底：调LLM（聊天/知识/澄清/查数据，由LLM理解意图）
        return self._llm_route(msg, conversation_history)

    def _llm_route(self, user_message: str, conversation_history: list = None) -> dict:
        """LLM兜底路由（仅规则快筛失败时调用，静默降级）"""
        history_text = ""
        if conversation_history:
            recent = conversation_history[-6:]
            history_text = "对话历史:\n" + "\n".join(
                f"  {m.get('role','?')}: {str(m.get('content',''))[:80]}"
                for m in recent
            )

        try:
            response = self.client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": ROUTER_PROMPT},
                    {"role": "user", "content": f"{history_text}\n用户: {user_message}"},
                ],
                temperature=0.1, max_tokens=300,
            )
            raw = response.choices[0].message.content
            if not raw:
                raise ValueError("LLM返回空内容")
            result = json.loads(raw)
        except Exception:
            # 静默降级：LLM不可用时，问候语给固定回复，否则默认查数据
            stripped = user_message.strip().rstrip("？?！!。.~～ ")
            for greet, reply in self.GREETINGS.items():
                if stripped == greet or (greet in stripped and len(stripped) <= len(greet) + 3):
                    return {"mode": "chat", "reply": reply, "reason": "LLM不可用，问候语兜底"}
            result = {"mode": "sql_query", "rewritten": user_message,
                      "reason": "LLM不可用，默认查数据"}
            self.cache[user_message] = dict(result)
            return result

        # clarify模式补上反问
        if result.get("mode") == "clarify" and not result.get("follow_up_questions"):
            result["follow_up_questions"] = [
                "您想分析哪个指标？比如销售额、订单数、库存等",
                "需要什么时间范围？比如本月、上个月、今年",
            ]

        # 缓存
        if len(self.cache) < 50:
            self.cache[user_message] = dict(result)

        logger.info(f"[Router] mode={result.get('mode')} reason={result.get('reason','')[:50]}")
        return result


agent_router = AgentRouter()
