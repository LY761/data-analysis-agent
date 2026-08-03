# backend/tests/test_reviews.py
from unittest.mock import patch
from agent.market_intelligence.reviews import extract_asin, get_reviews_for_asin
from agent.market_intelligence import product_analyzer


def test_extract_asin():
    assert extract_asin("https://www.amazon.com/dp/B0H5VN7MDL") == "B0H5VN7MDL"
    assert extract_asin("https://www.amazon.com/product-reviews/B0H5VN7MDL") == "B0H5VN7MDL"
    assert extract_asin("https://example.com/x") == ""
    assert extract_asin("") == ""


def test_get_reviews_for_asin():
    with patch("agent.market_intelligence.reviews.load_reviews_index",
               return_value={"B0ABC": [{"body": "battery dies fast", "rating": 2}]}):
        revs = get_reviews_for_asin("B0ABC")
    assert len(revs) == 1
    assert revs[0]["body"] == "battery dies fast"
    assert get_reviews_for_asin("NOPE") == []


def _analyze_with_reviews(monkeypatch, reviews_return, url):
    """跑 analyze_product，返回 LLM 的调用记录"""
    calls = []

    def fake_llm(client, messages):
        calls.append(messages)
        n = len(calls)
        if n == 1:
            return "卖点：降噪"
        if n == 2:
            return "痛点：续航短"
        return "建议：做长续航"

    monkeypatch.setattr("agent.market_intelligence.product_analyzer._call_llm", fake_llm)
    with patch("agent.market_intelligence.product_analyzer.search_products",
               return_value=[{"title": "P", "url": url, "snippet": ""}]):
        with patch("agent.market_intelligence.product_analyzer.scrape_product",
                   return_value={"title": "P", "price": 10, "rating": 4,
                                 "review_count": 5, "url": url}):
            with patch("agent.market_intelligence.reviews.get_reviews_for_asin",
                       return_value=reviews_return):
                with patch("db.executor.executor.execute",
                           return_value={"data": [], "columns": [], "row_count": 0}):
                    product_analyzer.analyze_product("P")
    return calls


def test_product_analyzer_uses_real_reviews(monkeypatch):
    calls = _analyze_with_reviews(
        monkeypatch,
        reviews_return=[{"body": "battery dies fast after 2 months", "rating": 2, "asin": "B0ABC"}],
        url="https://amzn/dp/B0ABC",
    )
    # pain 环节的 user content 应含真实评论 body，且不含降级 note
    pain_msg = calls[1][1]["content"]
    assert "battery dies fast after 2 months" in pain_msg
    assert "评论正文未抓取" not in pain_msg


def test_product_analyzer_falls_back_when_no_reviews(monkeypatch):
    calls = _analyze_with_reviews(monkeypatch, reviews_return=[], url="https://amzn/dp/B0XYZ")
    pain_msg = calls[1][1]["content"]
    # 无本地评论 → 降级元数据，含 note 标注
    assert "评论正文未抓取" in pain_msg
