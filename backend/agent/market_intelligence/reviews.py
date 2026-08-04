"""本地评论数据源 — 读 competitor-scraper 已爬好的 Amazon 评论 JSON

competitor-scraper 的 scrape_reviews_now.py 会爬真实差评存到：
    C:/Users/LY/competitor-scraper/data/real_negative_reviews_*.json
格式: {brand: [{brand, asin, rating, title, body, date}, ...]}

本模块建立 {asin: [评论...]} 索引，供商品研究的痛点分析用真实评论；
没有本地评论数据时返回空，调用方降级为"基于元数据推断"。
"""
import glob
import json
import os
import logging
from functools import lru_cache
from config import COMPETITOR_SCRAPER_PATH

logger = logging.getLogger(__name__)

REVIEWS_GLOB = os.getenv(
    "REVIEWS_DATA_GLOB",
    os.path.join(COMPETITOR_SCRAPER_PATH, "data", "real_negative_reviews_*.json")
        .replace("\\", "/"),
)


@lru_cache(maxsize=1)
def load_reviews_index() -> dict[str, list[dict]]:
    """合并所有评论文件 → {asin: [review...]}。按文件时间新优先，去重。"""
    index: dict[str, dict[str, dict]] = {}
    files = sorted(glob.glob(REVIEWS_GLOB))
    for f in files:
        try:
            data = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for brand, reviews in (data or {}).items():
            for r in reviews or []:
                asin = r.get("asin")
                if not asin:
                    continue
                # 用 body 去重（同一条评论可能在多次爬取中重复）
                body = r.get("body", "")
                if body:
                    index.setdefault(asin, {})[body[:80]] = r
    result = {asin: list(revs.values()) for asin, revs in index.items()}
    logger.info(f"[Reviews] 本地评论索引: {len(result)} 个ASIN，"
                f"{sum(len(v) for v in result.values())} 条评论")
    return result


def get_reviews_for_asin(asin: str, limit: int = 20) -> list[dict]:
    """返回指定 ASIN 的本地评论（最多 limit 条）。无数据返回空列表。"""
    if not asin:
        return []
    return load_reviews_index().get(asin, [])[:limit]


def extract_asin(url: str) -> str:
    """从 Amazon URL 提取 ASIN（/dp/B0XXXX 或 /product-reviews/B0XXXX）。"""
    import re
    m = re.search(r"/(?:dp|product-reviews)/([A-Z0-9]{10})", url or "")
    return m.group(1) if m else ""
