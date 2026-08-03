from unittest.mock import patch, MagicMock
from agent.market_intelligence.search import search_products

FAKE_AMAZON = """
<html><body>
<div data-asin="B0ABC12345"><h2><span>Bluetooth Earbuds Pro</span></h2></div>
<div data-asin="B0XYZ99999"><h2><span>Wireless Earbuds Mini</span></h2></div>
<div data-asin="BADASIN"><h2><span>No</span></h2></div>
<div><h2><span>No ASIN attr</span></h2></div>
</body></html>
"""

def test_search_parses_amazon_results():
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = FAKE_AMAZON
    with patch("agent.market_intelligence.search.cr.get", return_value=fake_resp) as mock_get:
        results = search_products("bluetooth earbuds")
    # 只保留 10位合法ASIN + 有标题的卡片；无效ASIN(BADASIN)和无data-asin的被过滤
    assert len(results) == 2
    assert results[0]["title"] == "Bluetooth Earbuds Pro"
    assert "/dp/B0ABC12345" in results[0]["url"]
    assert mock_get.call_args[0][0] == "https://www.amazon.com/s"


def test_search_returns_empty_on_non_200():
    fake_resp = MagicMock()
    fake_resp.status_code = 500
    fake_resp.text = FAKE_AMAZON
    with patch("agent.market_intelligence.search.cr.get", return_value=fake_resp):
        assert search_products("x") == []


def test_search_filters_invalid_asin():
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = '<div data-asin="NOT_A_VALID_ASIN_XYZ"><h2><span>T</span></h2></div>'
    with patch("agent.market_intelligence.search.cr.get", return_value=fake_resp):
        assert search_products("x") == []


def test_search_handles_request_exception():
    with patch("agent.market_intelligence.search.cr.get", side_effect=Exception("network")):
        assert search_products("x") == []


def test_search_respects_limit():
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    cards = "".join(f'<div data-asin="B0{i:08d}"><h2><span>P{i}</span></h2></div>'
                    for i in range(10))
    fake_resp.text = f"<html><body>{cards}</body></html>"
    with patch("agent.market_intelligence.search.cr.get", return_value=fake_resp):
        assert len(search_products("x", limit=3)) == 3
