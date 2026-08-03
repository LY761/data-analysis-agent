"""共享爬虫：抓取 Amazon 产品页 → 结构化字段。
httpx+BS4 自实现，供 market_intelligence 与 competitor_analysis 共用。
依赖: httpx, bs4（Agent venv 已有）"""
import re
import httpx
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def extract_amazon_product(url: str, html: str) -> dict:
    """从 Amazon 产品页 HTML 提取结构化字段（纯解析，可单测）"""
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    t = soup.select_one("#productTitle")
    if t:
        title = t.get_text(strip=True)

    price = None
    p = soup.select_one("span.a-price-whole")
    if p:
        # 仅取 a-price-whole 的直接文本，避免把嵌套 <sup>99</sup> 并入整数部分
        raw = "".join(p.find_all(string=True, recursive=False)).replace(",", "")
        m = re.search(r"\d+\.?\d*", raw)
        if m:
            price = float(m.group(0))
        # 简化 Amazon 标记：<sup> 内的小数部分
        sup = p.find("sup")
        if price is not None and sup:
            frac_text = sup.get_text(strip=True)
            if frac_text.isdigit():
                price += float("0." + frac_text)
    frac = soup.select_one("span.a-price-fraction")
    if price and frac:
        price += float("0." + frac.get_text(strip=True))

    rating = None
    r = soup.select_one("span.a-icon-alt")
    if r:
        m = re.search(r"([\d.]+)", r.get_text())
        if m:
            rating = float(m.group(1))

    reviews = None
    rc = soup.select_one("#acrCustomerReviewText")
    if rc:
        m = re.search(r"([\d,]+)", rc.get_text())
        if m:
            reviews = int(m.group(1).replace(",", ""))

    return {"title": title, "price": price, "rating": rating,
            "review_count": reviews, "url": url}


def scrape_product(url: str) -> dict:
    """抓取单个产品页。失败返回空字段 dict，不抛异常。"""
    try:
        with httpx.Client(headers=HEADERS, timeout=20.0, follow_redirects=True) as client:
            resp = client.get(url)
        if resp.status_code == 200:
            return extract_amazon_product(url, resp.text)
    except Exception:
        pass
    return {"title": "", "price": None, "rating": None, "review_count": None, "url": url}
