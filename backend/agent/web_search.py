"""
联网搜索工具 — 知识类问题（非数据库查询）先上网找资料，再 LLM 总结回答。

后端（零 API key）:
  - Bing 搜索页：curl_cffi Chrome 指纹，免费无反爬验证
  - 维基百科 API（zh.wikipedia.org）：结构化 JSON，稳定兜底

失败策略：任一环节失败返回空/降级，调用方回退纯 LLM 回答，绝不让用户看到报错。
"""
import re
import logging
from curl_cffi import requests as cr
from bs4 import BeautifulSoup
from openai import OpenAI

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def search_bing(query: str, limit: int = 5) -> list:
    """必应中国（cn.bing.com，国内可直连）→ [{title, url, snippet}]。失败/无结果返回 []。"""
    try:
        resp = cr.get("https://cn.bing.com/search", params={"q": query},
                      headers=HEADERS, impersonate="chrome", timeout=15,
                      allow_redirects=True)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for li in soup.select("li.b_algo")[:limit]:
            a = li.select_one("h2 a")
            if not a:
                continue
            title = a.get_text(strip=True)
            url = a.get("href", "")
            snippet = ""
            p = li.select_one(".b_caption p") or li.select_one("p")
            if p:
                snippet = p.get_text(" ", strip=True)[:200]
            if title and url:
                results.append({"title": title, "url": url, "snippet": snippet})
        return results
    except Exception as e:
        logger.warning(f"[WebSearch] Bing 搜索失败: {e}")
        return []


def search_wikipedia(query: str, limit: int = 3) -> list:
    """维基百科 API（中文）→ [{title, url, snippet}]。无反爬、稳定。"""
    try:
        resp = cr.get("https://zh.wikipedia.org/w/api.php", params={
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": limit, "format": "json", "utf8": 1,
        }, headers=HEADERS, impersonate="chrome", timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        hits = data.get("query", {}).get("search", [])
        return [
            {
                "title": h.get("title", ""),
                "url": "https://zh.wikipedia.org/wiki/" + (h.get("title", "").replace(" ", "_")),
                "snippet": re.sub(r"<[^>]+>", "", h.get("snippet", ""))[:200],
            }
            for h in hits
        ]
    except Exception as e:
        logger.warning(f"[WebSearch] 维基搜索失败: {e}")
        return []


def search_baidu(query: str, limit: int = 5) -> list:
    """百度搜索页 → [{title, url, snippet}]。国内兜底源。失败/无结果返回 []。"""
    try:
        resp = cr.get("https://www.baidu.com/s", params={"wd": query, "rn": limit},
                      headers=HEADERS, impersonate="chrome", timeout=15,
                      allow_redirects=True)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for div in soup.select("div.result, div.c-container")[:limit]:
            a = div.select_one("h3 a") or div.select_one("h3.t a")
            if not a:
                continue
            title = a.get_text(strip=True)
            url = a.get("href", "")
            snippet = ""
            p = (div.select_one(".c-abstract")
                 or div.select_one("[class*='content-right']")
                 or div.select_one("span"))
            if p:
                snippet = p.get_text(" ", strip=True)[:200]
            if title and url:
                results.append({"title": title, "url": url, "snippet": snippet})
        return results
    except Exception as e:
        logger.warning(f"[WebSearch] 百度搜索失败: {e}")
        return []


def web_search(query: str, limit: int = 5) -> list:
    """主入口：必应中国 → 百度 → 维基，逐级兜底（全部国内可访问优先）。"""
    results = search_bing(query, limit)
    if not results:
        results = search_baidu(query, limit)
    if not results:
        results = search_wikipedia(query, limit)
    return results


def summarize_with_llm(question: str, results: list, llm_client=None) -> str:
    """基于搜索结果用 LLM 生成带来源的中文回答。失败返回 ""（调用方降级）。"""
    if not results:
        return ""
    from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
    client = llm_client or OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    src = "\n".join(
        f"{i + 1}. {r.get('title', '')} — {r.get('snippet', '')} ({r.get('url', '')})"
        for i, r in enumerate(results[:5])
    )
    system = (
        "你是知识问答助手。根据用户提供的网络搜索结果，用中文回答用户问题。"
        "引用来源编号，如（来源1）。不要编造搜索结果里没有的信息；资料不足就如实说明。"
    )
    user = f"问题：{question}\n\n搜索结果：\n{src}\n\n请回答。"
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.3,
            max_tokens=600,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"[WebSearch] LLM 总结失败: {e}")
        return ""
