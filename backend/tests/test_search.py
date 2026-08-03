from unittest.mock import patch, MagicMock
from agent.market_intelligence.search import search_products

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
