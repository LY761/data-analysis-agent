"""共享爬虫：抓取 Amazon 产品页 → 结构化字段。
供 market_intelligence 与 competitor_analysis 共用。
依赖: curl_cffi（Chrome TLS 指纹，对抗 Amazon 反爬）, bs4"""
import re
import time
from curl_cffi import requests as cr
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

    # 品牌（"Visit the Anker Store" → "Anker"）
    brand = ""
    b = soup.select_one("#bylineInfo")
    if b:
        brand = (b.get_text(strip=True)
                 .replace("Visit the", "").replace("Store", "").strip())[:40]

    # 库存状态（"In Stock" / "仅剩3件"）
    availability = ""
    a = soup.select_one("#availability")
    if a:
        availability = a.get_text(" ", strip=True)[:60]

    # 核心卖点 bullet points
    features = []
    for li in soup.select("#feature-bullets li")[:8]:
        t = li.get_text(" ", strip=True)
        if t and len(t) > 3:
            features.append(t)

    # 主图
    image_url = ""
    img = soup.select_one("#landingImage")
    if img:
        image_url = img.get("src") or img.get("data-old-hires") or ""

    # ASIN（从 URL /dp/XXXXXXXXXX 提取）
    asin = ""
    m = re.search(r"/dp/([A-Z0-9]{10})", url or "")
    if m:
        asin = m.group(1)

    return {"title": title, "price": price, "rating": rating,
            "review_count": reviews, "url": url, "brand": brand,
            "availability": availability, "features": features,
            "image_url": image_url, "asin": asin}


def scrape_product(url: str) -> dict:
    """抓取单个 Amazon 产品页。用 curl_cffi 的 Chrome TLS 指纹对抗反爬。
    网络失败/非200 自动重试 1 次（间隔1.5s）。失败返回空字段 dict，不抛异常。"""
    import time
    empty = {"title": "", "price": None, "rating": None, "review_count": None,
             "url": url, "brand": "", "availability": "", "features": [],
             "image_url": "", "asin": ""}
    for attempt in range(2):
        try:
            resp = cr.get(url, headers=HEADERS, impersonate="chrome",
                          timeout=20, allow_redirects=True)
            if resp.status_code == 200:
                return extract_amazon_product(url, resp.text)
        except Exception:
            pass
        if attempt == 0:
            time.sleep(1.5)
    return empty


# ═══════════════════════════════════════════════════════
# 京东抓取（需先运行 login_jd.py 登录存会话）
# ═══════════════════════════════════════════════════════

JD_PROFILE = "jd_session"


def _jd_price(text) -> float | None:
    """从京东价格文本提取数字（'¥1299.00' 或 '1299' → 1299.0）"""
    import re
    if not text:
        return None
    m = re.search(r"([\d,]+\.?\d*)", str(text))
    return float(m.group(1).replace(",", "")) if m else None


def _jd_session(headless=True):
    """创建带登录态的京东 StealthySession"""
    from scrapling.fetchers import StealthySession
    return StealthySession(headless=headless, user_data_dir=JD_PROFILE,
                           hide_canvas=True, block_webrtc=True)


def scrape_jd_product(url: str) -> dict:
    """抓取京东商品页（动态渲染 + 登录会话）。失败返回空字段 dict。

    京东商品页 2026 起改版，旧标记 .sku-name / #jd-price 已废弃。
    新版用 .sku-title-name（标题）与 .product-price--value（价格，纯数字）。
    未登录会跳 passport 登录页，此时返回空字段。"""
    try:
        with _jd_session() as session:
            p = session.fetch(url, timeout=30000, wait=3000, network_idle=True)
            title = (p.css(".sku-title-name::text").get() or "").strip()
            price_text = (p.css(".product-price--value::text").get()
                          or p.css(".product-price::text").get() or "")
            price = _jd_price(price_text)
            if not title:
                # 兜底：从 <title> 提取商品名，去掉京东后缀（如 【行情 报价 价格 评测】-京东）
                t = p.css("title::text").get()
                if t:
                    title = re.sub(r"【[^】]*】-京东$", "", t.strip())
            if not title and "passport" in str(getattr(p, "url", "")):
                return {"title": "", "price": None, "rating": None,
                        "review_count": None, "url": url}
            # 评分/评论数：京东 2026 改版后标记不稳定，尽力从常见标记提取，失败保持 None
            rating = None
            for sel in (".summaryScore", ".score", ".rating-num"):
                t = p.css(sel + "::text").get()
                if t:
                    m = re.search(r"([\d.]+)", t)
                    if m:
                        rating = float(m.group(1))
                        break
            reviews = None
            for sel in (".comment-count", "#comment-count", ".total-comments"):
                t = p.css(sel + "::text").get()
                if t:
                    m = re.search(r"([\d,]+)", t)
                    if m:
                        reviews = int(m.group(1).replace(",", ""))
                        break
            return {"title": title, "price": price, "rating": rating,
                    "review_count": reviews, "url": url}
    except Exception:
        pass
    return {"title": "", "price": None, "rating": None, "review_count": None, "url": url}


def search_jd_products(query: str, limit: int = 15) -> list[dict]:
    """渲染京东搜索页，返回 [{title, url, price}]。需已登录。失败返回 []。

    京东搜索页 2026 起改为 React 应用（search-pc-java），旧标记 li.gl-item/p-name
    已废弃，商品卡片改用 [data-sku] 属性，内部类名是 CSS-module 哈希（每次部署会变）。
    因此这里用稳定的 [data-sku] + bs4 解析，标题取卡片内首个带 title 属性的元素。
    """
    from urllib.parse import urlencode
    from scrapling.fetchers import StealthySession
    # StealthySession.fetch 只接受 url（内部 page.goto），query 参数必须拼进 URL
    url = "https://search.jd.com/Search?" + urlencode({"keyword": query, "enc": "utf-8"})
    try:
        with StealthySession(headless=True, user_data_dir=JD_PROFILE,
                             hide_canvas=True, block_webrtc=True) as session:
            p = session.fetch(url, timeout=30000, wait=3000, network_idle=True)
            soup = BeautifulSoup(p.body, "html.parser")
            results = []
            seen = set()
            for card in soup.select("[data-sku]")[:limit]:
                sku = card.get("data-sku", "") or ""
                title_el = card.find(attrs={"title": True})
                name = (title_el.get("title", "") if title_el else "").strip()
                if not sku or sku in seen or not name:
                    continue
                seen.add(sku)
                m = re.search(r"¥\s*([\d,]+\.?\d*)", card.get_text())
                price = _jd_price(m.group(0)) if m else None
                results.append({
                    "title": name,
                    "url": f"https://item.jd.com/{sku}.html",
                    "price": price,
                })
            return results
    except Exception:
        return []
