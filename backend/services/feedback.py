"""
用户反馈闭环 — 收集Bad Case → 归因 → 驱动Prompt迭代

反馈类型:
  - helpful: 查询结果有帮助
  - not_helpful: 结果不对/不相关
  - partial: 部分正确但不够好

存储: 内存 + 可扩展Redis/PostgreSQL
"""
import json
import time
import hashlib
import logging
from datetime import datetime
from typing import Optional
from collections import defaultdict

logger = logging.getLogger(__name__)

# 内存存储（Demo用，生产接数据库）
_feedback_store: list[dict] = []
_stats: dict = {"helpful": 0, "not_helpful": 0, "partial": 0}


def record_feedback(
    query_id: str,
    question: str,
    sql: str,
    rating: str,      # "helpful" | "not_helpful" | "partial"
    comment: str = "",
    user_id: str = "",
    expected_result: str = "",
) -> dict:
    """
    记录一条用户反馈。

    返回: 反馈记录
    """
    feedback = {
        "id": hashlib.md5(f"{query_id}{time.time()}".encode()).hexdigest()[:8],
        "query_id": query_id,
        "question": question[:500],
        "sql": sql,
        "rating": rating,
        "comment": comment,
        "expected_result": expected_result,
        "user_id": user_id,
        "timestamp": datetime.now().isoformat(),
    }

    _feedback_store.append(feedback)
    _stats[rating] = _stats.get(rating, 0) + 1
    logger.info(f"[Feedback] {rating} — question='{question[:60]}...' comment='{comment[:60]}'")

    # 如果是负面反馈，自动触发Bad Case分析
    if rating in ("not_helpful", "partial"):
        _auto_analyze_bad_case(feedback)

    return feedback


def _auto_analyze_bad_case(feedback: dict):
    """
    Bad Case自动归因。

    根据g反馈内容尝试分类：
      - wrong_table: LLM选错了表
      - wrong_filter: 过滤条件不对
      - wrong_aggregation: 聚合逻辑错误（缺GROUP BY/算错）
      - schema_missing: Schema检索漏了关键表
      - unclear_question: 用户问题太模糊
      - unknown: 不确定原因
    """
    comment = (feedback.get("comment", "") + " " + feedback.get("expected_result", "")).lower()
    sql = feedback.get("sql", "").lower()

    category = "unknown"

    if any(w in comment for w in ("表不对", "表选错", "不该查", "wrong table")):
        category = "wrong_table"
    elif any(w in comment for w in ("条件", "筛选", "过滤", "日期", "filter", "where")):
        category = "wrong_filter"
    elif any(w in comment for w in ("加总", "求和", "平均", "数量不对", "聚合", "group", "sum")):
        category = "wrong_aggregation"
    elif any(w in comment for w in ("找不到", "没有数据", "字段", "列")):
        category = "schema_missing"
    elif len(feedback.get("question", "")) < 10:
        category = "unclear_question"
    elif "group by" not in sql and ("每个" in feedback.get("question", "") or "各" in feedback.get("question", "")):
        category = "wrong_aggregation"

    feedback["auto_category"] = category
    logger.info(f"[Feedback] Bad Case auto-categorized: {category}")


def get_feedback_stats() -> dict:
    """获取反馈统计"""
    total = sum(_stats.values())
    if total == 0:
        return {"total": 0, "helpful_rate": 0}

    return {
        "total": total,
        "helpful": _stats.get("helpful", 0),
        "not_helpful": _stats.get("not_helpful", 0),
        "partial": _stats.get("partial", 0),
        "helpful_rate": round(_stats.get("helpful", 0) / total * 100, 1),
    }


def get_bad_cases(limit: int = 20) -> list:
    """获取最近的Bad Case列表（用于Prompt迭代）"""
    bad = [f for f in _feedback_store if f.get("rating") in ("not_helpful", "partial")]
    return sorted(bad, key=lambda x: x.get("timestamp", ""), reverse=True)[:limit]


def get_category_breakdown() -> dict:
    """按归因分类统计Bad Case"""
    categories = defaultdict(int)
    for f in _feedback_store:
        cat = f.get("auto_category", "unknown")
        if f.get("rating") in ("not_helpful", "partial"):
            categories[cat] += 1
    return dict(categories)


def export_feedback_for_optimization() -> str:
    """
    导出反馈数据为Prompt优化建议。

    返回: 一段可读的优化建议文本
    """
    stats = get_feedback_stats()
    bad = get_bad_cases(10)
    breakdown = get_category_breakdown()

    lines = [
        "## 用户反馈优化建议",
        f"- 好评率: {stats['helpful_rate']}% ({stats['helpful']}/{stats['total']})",
        f"- Bad Case分类: {json.dumps(breakdown, ensure_ascii=False)}",
        "",
        "### 最近的Bad Case:",
    ]

    for i, case in enumerate(bad[:5], 1):
        lines.append(f"{i}. [{case.get('auto_category', '?')}] Q: {case['question'][:80]}")
        lines.append(f"   SQL: {case.get('sql', '')[:120]}")
        lines.append(f"   Feedback: {case.get('comment', '')[:80]}")

    return "\n".join(lines)
