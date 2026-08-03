"""DDG HTML 搜索 → 产品 URL 列表。零 API key，纯免费。"""
import httpx
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

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
        href = link.get("href", "")
        title = link.get_text(strip=True)
        results.append({"title": title, "url": href, "snippet": ""})
    for i, res in enumerate(soup.select("div.result__snippet")[:limit]):
        if i < len(results):
            results[i]["snippet"] = res.get_text(strip=True)
    return results
