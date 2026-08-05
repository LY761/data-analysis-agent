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
        # 参数化：用户输入经 ? 绑定，不再拼进 SQL 字符串
        r = executor.execute(
            "SELECT product_name, category, unit_price FROM products "
            "WHERE is_active=1 AND (category LIKE ? OR product_name LIKE ?) LIMIT 5",
            (f"%{kw}%", f"%{kw}%"),
        )
        # executor 出错时 data 可能为 None，降级为 []，遵守 -> list 契约
        return r.get("data") or []
    except Exception:
        return []


def analyze_selection(category: str, llm_client=None, stream_cb=None) -> dict:
    """选品分析主入口（带缓存：同类目在 TTL 内复用，避免重复爬12页+2次LLM）
    流式路径（stream_cb）不读写缓存，保证进度实时推送。"""
    def progress(msg):
        if stream_cb:
            try: stream_cb(msg)
            except Exception: pass

    # 缓存命中：同类目直接复用（TTL=COMPETITOR_CACHE_TTL，默认1小时）
    if not stream_cb and not llm_client:
        try:
            from cache.query_cache import get_cached_result
            cached = get_cached_result(f"selection:{category}", "selection_v1")
            if cached:
                logger.info(f"[Selection] 缓存命中: {category}")
                return cached
        except Exception:
            pass

    client = llm_client or _default_client()
    try:
        progress(f"正在搜索「{category}」相关产品...")
        products = search_products(category, limit=12)

        progress(f"找到 {len(products)} 个产品，正在抓取详情...")
        scraped = [scrape_product(p["url"]) for p in products]
        scraped = [s for s in scraped if s.get("title")]

        # 数据门槛：无真实数据时绝不输出推断结论（宁可明确报错+引导）
        if not scraped:
            progress("未获取到任何商品数据")
            return {
                "category": category, "products": [], "profile": "",
                "internal": [], "recommendation": {},
                "error": "未获取到任何真实商品数据（爬虫被拦截或网络异常）。"
                         "请改用【📋 粘贴数据分析】粘贴你看到的真实商品信息，"
                         "或检查网络后重试。无真实数据时不再生成推断结论。",
            }

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

        result = {"category": category, "products": scraped, "profile": profile,
                  "internal": internal, "recommendation": recommendation, "error": None}
        # 写缓存（仅非流式路径）
        if not stream_cb and not llm_client:
            try:
                from cache.query_cache import set_cached_result
                from config import COMPETITOR_CACHE_TTL
                set_cached_result(f"selection:{category}", "selection_v1", result,
                                  ttl=COMPETITOR_CACHE_TTL)
            except Exception:
                pass
        return result
    except Exception as e:
        logger.warning(f"[Selection] 分析失败: {e}")
        return {"category": category, "products": [], "profile": "",
                "internal": [], "recommendation": {}, "error": str(e)}
