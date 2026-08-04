# backend/tests/test_crawler_enhance.py
"""C3: 爬虫增强回归测试 — Amazon 字段扩展 / 重试 / 搜索页价格评分摘要"""
import pytest
from agent import crawler
from agent.market_intelligence import search

AMAZON_HTML = """
<html><body>
<span id="productTitle">Anker 10000mAh Power Bank</span>
<span id="bylineInfo">Visit the Anker Store</span>
<span class="a-price-whole">2,999<sup>99</sup></span>
<span class="a-icon-alt">4.6 out of 5 stars</span>
<span id="acrCustomerReviewText">1,234 ratings</span>
<span id="availability"><span>In Stock</span></span>
<ul id="feature-bullets">
  <li><span>Fast charging 20W</span></li>
  <li><span>Compact design</span></li>
</ul>
<img id="landingImage" src="https://img.example.com/p.jpg">
</body></html>
"""


def test_extract_amazon_product_extended_fields():
    """Amazon 商品页解析：新增 brand/availability/features/image_url/asin"""
    out = crawler.extract_amazon_product("https://www.amazon.com/dp/B0ABCDEFGH", AMAZON_HTML)
    assert out["title"] == "Anker 10000mAh Power Bank"
    assert out["brand"] == "Anker"
    assert out["availability"] == "In Stock"
    assert out["features"] == ["Fast charging 20W", "Compact design"]
    assert out["image_url"] == "https://img.example.com/p.jpg"
    assert out["asin"] == "B0ABCDEFGH"
    assert out["price"] == 2999.99
    assert out["rating"] == 4.6
    assert out["review_count"] == 1234


def test_scrape_product_retries_then_succeeds(monkeypatch):
    """第一次网络失败 → 重试成功"""
    calls = []

    def _flaky(url, **kw):
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("network error")
        return type("R", (), {"status_code": 200, "text": AMAZON_HTML})()

    monkeypatch.setattr(crawler.cr, "get", _flaky)
    monkeypatch.setattr(crawler.time, "sleep", lambda s: None)
    out = crawler.scrape_product("https://www.amazon.com/dp/B0ABCDEFGH")
    assert len(calls) == 2
    assert out["title"] == "Anker 10000mAh Power Bank"


def test_scrape_product_retries_then_empty(monkeypatch):
    """连续失败 → 返回空字段 dict（不抛异常）"""
    def _always_fail(url, **kw):
        raise RuntimeError("network error")

    monkeypatch.setattr(crawler.cr, "get", _always_fail)
    monkeypatch.setattr(crawler.time, "sleep", lambda s: None)
    out = crawler.scrape_product("https://www.amazon.com/dp/B0ABCDEFGH")
    assert out["title"] == ""
    assert out["brand"] == ""
    assert out["features"] == []


SEARCH_HTML = """
<html><body>
<div data-asin="B0ABCDEFGH">
  <h2><span>Anker Power Bank 20000mAh</span></h2>
  <span class="a-price-whole">4,999</span>
  <span class="a-icon-alt">4.7 out of 5 stars</span>
  <span class="a-size-base-plus a-color-base">Fast charging portable charger</span>
</div>
<div data-asin="B0ZZZZZZZZ">
  <h2><span>Other Brand Cable</span></h2>
</div>
</body></html>
"""


def test_search_products_extracts_price_rating_snippet():
    """搜索页解析：价格/评分/摘要字段"""
    class FakeResp:
        status_code = 200
        text = SEARCH_HTML

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(search.cr, "get", lambda *a, **kw: FakeResp())
    try:
        results = search.search_products("power bank", limit=5)
    finally:
        monkeypatch.undo()
    assert len(results) == 2
    first = results[0]
    assert first["title"] == "Anker Power Bank 20000mAh"
    assert first["price"] == 4999.0
    assert first["rating"] == 4.7
    assert first["snippet"] == "Fast charging portable charger"
    assert first["url"] == "https://www.amazon.com/dp/B0ABCDEFGH"
    assert results[1]["price"] is None  # 无价格卡片不崩溃
