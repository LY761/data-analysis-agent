"""Amazon 搜索页 → 产品 URL 列表。零 API key，纯免费。

说明：DDG HTML 端点对自动化请求返回 202 反爬；Amazon 搜索页 s?k= 用
curl_cffi 的 Chrome TLS 指纹请求（实测能拿到大量真实产品 ASIN）。
"""
from curl_cffi import requests as cr
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _is_valid_asin(asin: str) -> bool:
    return bool(asin) and len(asin) == 10 and asin.isalnum()


def search_products(query: str, limit: int = 15) -> list[dict]:
    """搜索电商产品，返回 [{title, url, snippet}]。

    抓 Amazon 搜索页 s?k=<query>，从产品卡片提取 ASIN 和标题。
    反爬失败/无结果时返回 []。
    """
    url = "https://www.amazon.com/s"
    params = {"k": query}
    try:
        resp = cr.get(url, headers=HEADERS, params=params,
                      impersonate="chrome", timeout=20, allow_redirects=True)
    except Exception:
        return []
    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    seen = set()
    for card in soup.select("div[data-asin]"):
        asin = (card.get("data-asin") or "").strip()
        if not _is_valid_asin(asin) or asin in seen:
            continue
        seen.add(asin)
        title_el = card.select_one("h2 span") or card.select_one("h2")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue
        results.append({"title": title, "url": f"https://www.amazon.com/dp/{asin}", "snippet": ""})
        if len(results) >= limit:
            break
    return results
