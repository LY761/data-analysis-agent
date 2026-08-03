"""选品流水线：搜索 → 抓取 → 品类画像 → 内部对比 → 选品报告"""
import json
import logging
import re
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from agent.market_intelligence.search import search_products
from agent.crawler import scrape_product
from agent.market_intelligence import prompts
from db.executor import executor

logger = logging.getLogger(__name__)


def _call_llm(client, messages) -> str:
    """调用 LLM，返回文本。client 兼容 openai 的 chat.completions.create。"""
    resp = client.chat.completions.create(
        model=LLM_MODEL, messages=messages, temperature=0.3, max_tokens=600
    )
    return resp.choices[0].message.content.strip()


def _default_client():
    return OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


def _compare_internal(category: str) -> list:
    """查内部数据库是否有相似产品（按品类关键词模糊匹配）"""
    try:
        # 纵深防御：参数化 SQL 改造前，先把关键词净化成「中文字母数字+空格」，
        # 剔除撇号/引号/分号等，避免截断词（如 women'）或恶意输入破坏 LIKE 拼接的语法。
        # 净化后撇号已无，原有的 '' 双写转义不再需要。
        kw = re.sub(r"[^\w一-鿿 ]", "", category)[:6]
        r = executor.execute(
            f"SELECT product_name, category, unit_price FROM products "
            f"WHERE is_active=1 AND (category LIKE '%{kw}%' "
            f"OR product_name LIKE '%{kw}%') LIMIT 5"
        )
        # executor 出错时 data 可能为 None，降级为 []，遵守 -> list 契约
        return r.get("data") or []
    except Exception:
        return []


def analyze_selection(category: str, llm_client=None, stream_cb=None) -> dict:
    """选品分析主入口"""
    def progress(msg):
        if stream_cb:
            try: stream_cb(msg)
            except Exception: pass

    client = llm_client or _default_client()
    try:
        progress(f"正在搜索「{category}」相关产品...")
        products = search_products(category, limit=12)

        progress(f"找到 {len(products)} 个产品，正在抓取详情...")
        scraped = [scrape_product(p["url"]) for p in products]
        scraped = [s for s in scraped if s.get("title")]

        progress("正在分析价格分布和竞争格局...")
        profile = _call_llm(client, [
            {"role": "system", "content": "你是电商选品分析师。"},
            {"role": "user", "content": prompts.PROFILE_PROMPT.format(
                products=json.dumps(scraped[:15], ensure_ascii=False))},
        ])

        progress("正在对比内部数据...")
        internal = _compare_internal(category)

        progress("正在生成选品建议...")
        rec_raw = _call_llm(client, [
            {"role": "system", "content": "你是跨境电商选品顾问。只输出JSON。"},
            {"role": "user", "content": prompts.SELECTION_PROMPT.format(
                category=category, profile=profile,
                internal=json.dumps(internal, ensure_ascii=False))},
        ])
        # 容错：LLM 可能返回 ```json 围栏或前后散文，剥离后取首个 {...} 再解析
        rec_raw = rec_raw.strip()
        if rec_raw.startswith("```"):
            rec_raw = re.sub(r"^```[a-zA-Z]*\s*", "", rec_raw).removesuffix("```").strip()
        m = re.search(r"\{.*\}", rec_raw, re.DOTALL)
        if m:
            rec_raw = m.group(0)
        recommendation = json.loads(rec_raw)

        return {"category": category, "products": scraped, "profile": profile,
                "internal": internal, "recommendation": recommendation, "error": None}
    except Exception as e:
        logger.warning(f"[Selection] 分析失败: {e}")
        return {"category": category, "products": [], "profile": "",
                "internal": [], "recommendation": {}, "error": str(e)}
