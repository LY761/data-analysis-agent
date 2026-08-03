"""
意图分类器 — 两级路由：规则模板匹配（0 Token）→ LLM分类（兜底）

设计思路:
  常用查询（~80%场景）用正则/关键词匹配，直接分配到对应SQL模板
  陌生查询（~20%场景）用LLM判断意图类型，分配到最接近的模板
  这样大部分查询不需要额外的LLM意图调用
"""
import re
import json
import logging
from typing import Optional
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# 意图类型定义
# ═══════════════════════════════════════════════════════════

INTENT_TYPES = {
    "sales_aggregation": {
        "name": "销售统计",
        "description": "求总额/平均/计数，需要SUM/AVG/COUNT + GROUP BY + 时间过滤",
        "keywords": ["销售额", "卖了多少", "总金额", "营收", "收入", "订单数", "订单量", "客单价", "平均",
                      "总额", "消费", "花费", "买了", "卖了"],
        "patterns": [r"(多少|总计|总额|一共|统计|汇总)", r"(销售额|营收|收入|金额|订单数|订单量)"],
    },
    "ranking": {
        "name": "排名",
        "description": "取前N名/最高/最低，需要ORDER BY + LIMIT",
        "keywords": ["排名", "前", "最高", "最低", "最好", "最差", "哪个", "哪些", "TOP"],
        "patterns": [r"(前\d|排名|最高|最低|最好|最差|TOP\s*\d)", r"哪个.*(好|多|高|强|大)"],
    },
    "filtering": {
        "name": "条件筛选",
        "description": "满足特定条件，需要WHERE过滤",
        "keywords": ["库存", "不足", "缺货", "超过", "大于", "小于", "在售", "下架", "退款", "取消", "手机号"],
        "patterns": [r"(不足|少于|低于|超过|大于|小于|等于|不是|没有)", r"(库存|在售|下架|退款|取消)"],
    },
    "comparison": {
        "name": "对比分析",
        "description": "环比/同比/对比，需要子查询或多时间段",
        "keywords": ["环比", "同比", "对比", "比较", "增长", "下降", "变化", "趋势", "去年", "上月同期"],
        "patterns": [r"(环比|同比|对比|和.*比|跟.*比|增长|下降了|变化|趋势)"],
    },
    "grouping": {
        "name": "分组统计",
        "description": "按某个维度分组看数据，需要GROUP BY + 聚合",
        "keywords": ["各", "每个", "按", "分类", "地区", "类别", "渠道", "支付方式", "会员", "占比"],
        "patterns": [r"(各|每个|按.*分|每一)", r"(地区|类别|渠道|方式|等级|分类|类型)"],
    },
    "detail_lookup": {
        "name": "明细查询",
        "description": "查具体某条记录，不需要聚合",
        "keywords": ["明细", "详情", "具体", "某", "哪个客户", "电话"],
        "patterns": [r"(明细|详情|具体|哪一个|某.*信息)", r"(客户.*电话|订单.*详情|产品.*信息)"],
    },
    "analysis": {
        "name": "综合分析",
        "description": "需要分析原因/为什么/怎么样，需要结合评论等多维度数据",
        "keywords": ["原因", "为什么", "怎么样", "好不好", "好不好卖", "改进", "建议", "分析"],
        "patterns": [r"(为什么|原因|什么原因|怎么.*不好|怎么.*差|如何改进|分析.*原因)"],
    },
}

# ═══════════════════════════════════════════════════════════
# Tier 1: 规则匹配（0 Token）
# ═══════════════════════════════════════════════════════════

def classify_by_rules(question: str) -> Optional[dict]:
    """
    用关键词+正则快速匹配意图。
    返回匹配到的意图信息，或None（需要LLM兜底）。

    匹配策略: 每个意图有两个维度
      1. keywords: 快速关键词扫描
      2. patterns: 正则模式（需两个维度都命中才算匹配）
    """
    scores = {}

    for intent_id, config in INTENT_TYPES.items():
        kw_score = sum(1 for kw in config["keywords"] if kw in question)
        pattern_score = sum(1 for p in config["patterns"] if re.search(p, question))

        # 两个维度都命中才算有效匹配
        if kw_score >= 1 and pattern_score >= 1:
            scores[intent_id] = kw_score + pattern_score * 2  # 正则权重更高

    # 强制规则：含"为什么/原因/如何改进"→一定是analysis
    if re.search(r'(为什么|原因|如何改进|怎么.*不好|怎么.*差)', question):
        return {
            "intent": "analysis",
            "intent_name": "综合分析",
            "confidence": 0.95,
            "method": "rule",
        }

    if not scores:
        return None

    # 返回得分最高的意图
    best = max(scores, key=scores.get)
    confidence = min(scores[best] / 6.0, 1.0)  # 归一化到0~1
    return {
        "intent": best,
        "intent_name": INTENT_TYPES[best]["name"],
        "confidence": round(confidence, 2),
        "method": "rule",
    }


# ═══════════════════════════════════════════════════════════
# Tier 2: LLM分类（兜底，仅规则匹配失败时调用）
# ═══════════════════════════════════════════════════════════

LLM_CLASSIFY_PROMPT = """你是一个查询意图分类器。分析用户问题属于哪种数据分析意图。

## 意图类型
1. sales_aggregation — 求总额/平均/计数（需要SUM/AVG/COUNT + GROUP BY）
2. ranking — 排名/取前N/最高最低（需要ORDER BY + LIMIT）
3. filtering — 条件筛选（需要WHERE过滤特定条件）
4. comparison — 对比分析（需要环比/同比/多时间段）
5. grouping — 分组统计（按维度分组，需要GROUP BY）
6. detail_lookup — 明细查询（查具体记录，不需要聚合）

## 输出JSON
{"intent": "类型ID", "reason": "判断依据(一句话)"}"""


def classify_by_llm(question: str) -> dict:
    """用LLM判断意图（仅在规则匹配失败时调用）"""
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": LLM_CLASSIFY_PROMPT},
                {"role": "user", "content": f"用户问题：{question}"},
            ],
            temperature=0.1,
            max_tokens=100,
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)
        intent = result.get("intent", "sales_aggregation")
        if intent not in INTENT_TYPES:
            intent = "sales_aggregation"  # 兜底默认

        return {
            "intent": intent,
            "intent_name": INTENT_TYPES[intent]["name"],
            "confidence": 0.6,
            "method": "llm",
            "reason": result.get("reason", ""),
        }
    except Exception as e:
        logger.warning(f"[IntentClassifier] LLM分类失败: {e}")
        # 真正的兜底：默认当销售统计处理
        return {
            "intent": "sales_aggregation",
            "intent_name": "销售统计",
            "confidence": 0.3,
            "method": "fallback",
        }


# ═══════════════════════════════════════════════════════════
# 统一入口
# ═══════════════════════════════════════════════════════════

def classify(question: str) -> dict:
    """
    两级意图分类。
    返回: {"intent": str, "intent_name": str, "confidence": float, "method": str}
    """
    # Tier 1: 规则匹配
    result = classify_by_rules(question)
    if result:
        logger.info(f"[Intent] 规则命中: {result['intent_name']} (confidence={result['confidence']})")
        return result

    # Tier 2: LLM兜底
    logger.info(f"[Intent] 规则未命中，调LLM分类...")
    result = classify_by_llm(question)
    logger.info(f"[Intent] LLM分类: {result['intent_name']} (method={result['method']})")
    return result
