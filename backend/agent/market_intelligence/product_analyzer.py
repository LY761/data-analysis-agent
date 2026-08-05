"""商品研究流水线：定位 → 抓取 → 卖点 → 评论洞察 → 内部对比 → 建议"""
import json
import logging
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from agent.market_intelligence.search import search_products
from agent.crawler import scrape_product
from agent.market_intelligence import prompts
from agent.market_intelligence.selection import _call_llm, _compare_internal

logger = logging.getLogger(__name__)


def _default_client():
    return OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


def _looks_like_url(text: str) -> bool:
    return text.startswith("http")


def analyze_product(query: str, llm_client=None, stream_cb=None) -> dict:
    """商品深度研究主入口"""
    def progress(msg):
        if stream_cb:
            try: stream_cb(msg)
            except Exception: pass

    client = llm_client or _default_client()
    try:
        if _looks_like_url(query):
            product = scrape_product(query)
        else:
            progress(f"正在搜索「{query}」...")
            found = search_products(query, limit=3)
            if not found:
                return {"query": query, "product": {}, "sellpoints": "", "pains": "",
                        "internal": [], "suggestions": "",
                        "error": "未找到该产品（搜索被拦截或无结果）。"
                                 "请改用【📋 粘贴数据分析】粘贴真实商品链接或信息。"}
            product = scrape_product(found[0]["url"])

        if not product.get("title"):
            return {"query": query, "product": product, "sellpoints": "", "pains": "",
                    "internal": [], "suggestions": "",
                    "error": "商品页抓取失败（可能被反爬拦截）。"
                             "请改用【📋 粘贴数据分析】粘贴真实商品链接或信息。"}

        progress(f"正在提取「{product['title'][:30]}」的卖点...")
        sellpoints = _call_llm(client, [
            {"role": "system", "content": "你是商品文案专家。"},
            {"role": "user", "content": prompts.SELLPOINTS_PROMPT.format(
                title=product["title"], description=json.dumps(product, ensure_ascii=False))},
        ])

        # 找本地真实评论（competitor-scraper 爬好的）；有则用真实评论，无则降级元数据推断
        from agent.market_intelligence.reviews import get_reviews_for_asin, extract_asin
        asin = extract_asin(product.get("url", ""))
        local_reviews = get_reviews_for_asin(asin)

        progress(f"正在分析评论痛点{'（真实评论 ' + str(len(local_reviews)) + ' 条）' if local_reviews else '（无本地评论，基于元数据推断）'}...")
        review_data = (
            [{"asin": asin, **r} for r in local_reviews]
            if local_reviews
            else [{"note": "评论正文未抓取，以下为产品元数据",
                   "title": product["title"], **product}]
        )
        pains = _call_llm(client, [
            {"role": "system", "content": "你是用户反馈分析师。"},
            {"role": "user", "content": prompts.REVIEW_PAIN_PROMPT.format(
                reviews=json.dumps(review_data, ensure_ascii=False))},
        ])

        progress("正在对比内部产品并给建议...")
        internal = _compare_internal(product["title"])
        suggestions = _call_llm(client, [
            {"role": "system", "content": "你是产品经理。"},
            {"role": "user", "content": prompts.SUGGEST_PROMPT.format(
                sellpoints=sellpoints, pains=pains,
                internal=json.dumps(internal, ensure_ascii=False))},
        ])

        return {"query": query, "product": product, "sellpoints": sellpoints,
                "pains": pains, "internal": internal, "suggestions": suggestions,
                "error": None}
    except Exception as e:
        logger.warning(f"[ProductAnalyzer] 分析失败: {e}")
        return {"query": query, "product": {}, "sellpoints": "", "pains": "",
                "internal": [], "suggestions": "", "error": str(e)}
