from unittest.mock import patch, MagicMock
from agent.market_intelligence.search import search_products, _normalize_url

FAKE_HTML = """
<html><body>
<a class="result__a" href="https://www.amazon.com/dp/B0ABC123">Bluetooth Earbuds Pro</a>
<a class="result__a" href="https://www.amazon.com/dp/B0XYZ999">Wireless Earbuds Mini</a>
<div class="result__snippet">Best seller bluetooth earbuds 2026</div>
</body></html>
"""

def test_search_parses_results():
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = FAKE_HTML
    with patch("httpx.Client.get", return_value=fake_resp) as mock_get:
        results = search_products("bluetooth earbuds")
    assert len(results) == 2
    assert results[0]["title"] == "Bluetooth Earbuds Pro"
    assert "B0ABC123" in results[0]["url"]
    assert results[0]["snippet"] == "Best seller bluetooth earbuds 2026"
    assert mock_get.call_args[0][0].startswith("https://html.duckduckgo.com")

def test_search_returns_empty_on_non_200():
    fake_resp = MagicMock()
    fake_resp.status_code = 500
    fake_resp.text = FAKE_HTML
    with patch("httpx.Client.get", return_value=fake_resp) as mock_get:
        results = search_products("bluetooth earbuds")
    assert results == []
    assert mock_get.call_args[0][0].startswith("https://html.duckduckgo.com")


def test_normalize_protocol_relative_url():
    # // 开头的 protocol-relative 链接（非重定向）→ 补 https: 前缀
    assert _normalize_url("//www.amazon.com/dp/B0ABC123") == \
        "https://www.amazon.com/dp/B0ABC123"


def test_normalize_ddg_redirect_decodes_uddg():
    # DDG 重定向链接 → 解码 uddg 参数取真实目标 URL
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.amazon.com%2Fdp%2FB0ABC123%3Fth%3D1&rut=abc"
    assert _normalize_url(href) == "https://www.amazon.com/dp/B0ABC123?th=1"


def test_normalize_keeps_plain_http():
    assert _normalize_url("https://www.amazon.com/dp/B0XYZ999") == \
        "https://www.amazon.com/dp/B0XYZ999"


def test_normalize_rejects_non_http():
    assert _normalize_url("javascript:void(0)") is None
    assert _normalize_url("") is None
    assert _normalize_url(None) is None


DDG_REDIRECT_HTML = """
<html><body>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.amazon.com%2Fdp%2FB0ABC123%3Fth%3D1&amp;rut=abc">Earbuds Pro</a>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.amazon.com%2Fdp%2FB0XYZ999">Earbuds Mini</a>
<a class="result__a" href="javascript:void(0)">Bad Link</a>
<div class="result__snippet">Top bluetooth earbuds</div>
</body></html>
"""

def test_search_normalizes_ddg_redirects_and_filters_bad():
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = DDG_REDIRECT_HTML
    with patch("httpx.Client.get", return_value=fake_resp):
        results = search_products("bluetooth earbuds")
    # 3 个 a.result__a：2 个重定向解码为 http(s)，1 个 javascript: 被过滤
    assert len(results) == 2
    assert results[0]["url"] == "https://www.amazon.com/dp/B0ABC123?th=1"
    assert results[1]["url"] == "https://www.amazon.com/dp/B0XYZ999"
