"""DDG HTML 搜索 → 产品 URL 列表。零 API key，纯免费。"""
import httpx
import urllib.parse
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _normalize_url(href: str):
    """归一化 DDG 搜索结果 URL → 可直接抓取的 http(s) 产品页 URL。

    DDG HTML 结果 href 可能是：
      - protocol-relative 重定向：//duckduckgo.com/l/?uddg=<urlencoded 目标>
      - 普通绝对 URL：https://www.amazon.com/dp/B0XXX

    处理：// 前缀补 https:；含 uddg= 时解码出真实目标 URL。
    只保留 http(s) 协议，无法归一化的返回 None（调用方跳过，避免 httpx 抛 UnsupportedProtocol）。
    """
    if not href:
        return None
    href = href.strip()
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        target = qs.get("uddg", [None])[0]
        if target:
            href = target
    if not href.startswith(("http://", "https://")):
        return None
    return href


def search_products(query: str, limit: int = 15) -> list[dict]:
    """搜索电商产品，返回 [{title, url, snippet}]"""
    url = "https://html.duckduckgo.com/html/"
    params = {"q": f"{query} bestseller amazon", "kl": "us-en"}
    with httpx.Client(headers=HEADERS, timeout=15.0, follow_redirects=True) as client:
        resp = client.get(url, params=params)
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for link in soup.select("a.result__a")[:limit]:
        normalized = _normalize_url(link.get("href", ""))
        if not normalized:
            continue
        title = link.get_text(strip=True)
        results.append({"title": title, "url": normalized, "snippet": ""})
    for i, res in enumerate(soup.select("div.result__snippet")[:limit]):
        if i < len(results):
            results[i]["snippet"] = res.get_text(strip=True)
    return results
