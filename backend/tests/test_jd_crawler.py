# backend/tests/test_jd_crawler.py
"""京东抓取函数 mock 单测：成功解析 / 登录墙检测 / 异常兜底。
约束：禁止真实网络请求，全部 mock。

2026 起京东搜索页/商品页改版：
- 搜索页商品卡片用 [data-sku] 属性（类名是 CSS-module 哈希，不可依赖）
- 商品页标题 .sku-title-name、价格 .product-price--value
"""
from agent import crawler


class _Get:
    """模拟 Selectors.get()：有值返回，否则返回 default。"""

    def __init__(self, val):
        self.val = val

    def get(self, default=None):
        return self.val if self.val is not None else default


class _FakePage:
    """模拟 scrape_jd_product 里 session.fetch 返回的页面（.css + .url）"""

    def __init__(self, title=None, price_text=None, url="https://item.jd.com/100012043978.html"):
        self._title = title
        self._price = price_text
        self.url = url

    def css(self, selector):
        if "sku-title-name" in selector:
            return _Get(self._title)
        if "product-price" in selector:
            return _Get(self._price)
        if selector.startswith("title::text"):
            return _Get(None)
        return _Get(None)


class _FakeSession:
    """模拟 StealthySession（上下文管理器 + fetch）"""

    def __init__(self, page):
        self._page = page

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def fetch(self, url, **kwargs):
        return self._page


def test_scrape_jd_product_success(monkeypatch):
    page = _FakePage(title="蓝牙耳机 降噪 无线", price_text="1299.00")
    monkeypatch.setattr(crawler, "_jd_session",
                        lambda headless=True: _FakeSession(page))
    p = crawler.scrape_jd_product("https://item.jd.com/100012043978.html")
    assert p["title"] == "蓝牙耳机 降噪 无线"
    assert p["price"] == 1299.0


def test_scrape_jd_product_title_fallback_from_pagetitle(monkeypatch):
    """.sku-title-name 缺失时从 <title> 兜底并去掉京东后缀"""
    class _TitlePage(_FakePage):
        def css(self, selector):
            if selector.startswith("title::text"):
                return _Get("蓝牙耳机 降噪 无线【行情 报价 价格 评测】-京东")
            return _Get(None)

    monkeypatch.setattr(crawler, "_jd_session",
                        lambda headless=True: _FakeSession(_TitlePage()))
    p = crawler.scrape_jd_product("https://item.jd.com/100012043978.html")
    assert p["title"] == "蓝牙耳机 降噪 无线"


def test_scrape_jd_product_login_wall(monkeypatch):
    # 未登录：被重定向到 passport.jd.com，标题为空
    page = _FakePage(title=None, url="https://passport.jd.com/new/login.aspx?ReturnUrl=...")
    monkeypatch.setattr(crawler, "_jd_session",
                        lambda headless=True: _FakeSession(page))
    p = crawler.scrape_jd_product("https://item.jd.com/100012043978.html")
    assert p["title"] == ""
    assert p["price"] is None


def test_scrape_jd_product_exception_returns_empty(monkeypatch):
    class _ErrSession(_FakeSession):
        def fetch(self, url, **kwargs):
            raise RuntimeError("browser crashed")

    monkeypatch.setattr(crawler, "_jd_session",
                        lambda headless=True: _ErrSession(_FakePage()))
    p = crawler.scrape_jd_product("https://item.jd.com/100012043978.html")
    assert p == {"title": "", "price": None, "rating": None,
                 "review_count": None, "url": "https://item.jd.com/100012043978.html"}


SEARCH_HTML = """
<html><body>
<div data-sku="10001"><div title="蓝牙耳机 无线降噪"></div><span class="x">¥1299.00</span></div>
<div data-sku="10002"><div title="无线耳机 运动款"></div><span class="x">¥79.00</span></div>
<div data-sku=""><div title="无SKU应被跳过"></div><span class="x">¥10.00</span></div>
</body></html>
"""


class _FakeSearchPage:
    """模拟 search_jd_products 里 session.fetch 返回的页面（含 .body 原始 HTML）"""

    def __init__(self, body):
        self.body = body


def test_search_jd_products_success(monkeypatch):
    page = _FakeSearchPage(SEARCH_HTML)
    monkeypatch.setattr("scrapling.fetchers.StealthySession",
                        lambda **kw: _FakeSession(page))
    results = crawler.search_jd_products("蓝牙耳机")
    assert len(results) == 2
    assert results[0]["title"] == "蓝牙耳机 无线降噪"
    assert results[0]["url"] == "https://item.jd.com/10001.html"
    assert results[0]["price"] == 1299.0
    assert results[1]["price"] == 79.0


def test_search_jd_products_exception_returns_empty(monkeypatch):
    class _ErrSession(_FakeSession):
        def fetch(self, url, **kwargs):
            raise RuntimeError("browser crashed")

    monkeypatch.setattr("scrapling.fetchers.StealthySession",
                        lambda **kw: _ErrSession(_FakeSearchPage(SEARCH_HTML)))
    assert crawler.search_jd_products("蓝牙耳机") == []
