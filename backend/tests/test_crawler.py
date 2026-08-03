# backend/tests/test_crawler.py
from agent import crawler
from agent.crawler import extract_amazon_product

FAKE_PAGE = """
<html><body>
<span id="productTitle">Anker Soundcore P40i True Wireless</span>
<span class="a-price-whole">49<sup>99</sup></span>
<span class="a-icon-alt">4.5 out of 5 stars</span>
<span id="acrCustomerReviewText">2,341 ratings</span>
</body></html>
"""


def test_extract_amazon_product():
    p = extract_amazon_product("https://www.amazon.com/dp/B0XYZ", FAKE_PAGE)
    assert p["title"] == "Anker Soundcore P40i True Wireless"
    assert p["price"] == 49.99
    assert p["rating"] == 4.5
    assert p["review_count"] == 2341


class _FakeResponse:
    status_code = 200
    text = FAKE_PAGE


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url):
        return _FakeResponse()


class _FailingClient(_FakeClient):
    def get(self, url):
        raise RuntimeError("network error")


def test_scrape_product_success(monkeypatch):
    monkeypatch.setattr(crawler.httpx, "Client", _FakeClient)
    p = crawler.scrape_product("https://www.amazon.com/dp/B0XYZ")
    assert p["title"] == "Anker Soundcore P40i True Wireless"
    assert p["price"] == 49.99
    assert p["rating"] == 4.5
    assert p["review_count"] == 2341


def test_scrape_product_failure_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(crawler.httpx, "Client", _FailingClient)
    p = crawler.scrape_product("https://www.amazon.com/dp/B0XYZ")
    assert p == {"title": "", "price": None, "rating": None,
                 "review_count": None, "url": "https://www.amazon.com/dp/B0XYZ"}
