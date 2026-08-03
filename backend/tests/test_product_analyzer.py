from unittest.mock import patch
from agent.market_intelligence.product_analyzer import analyze_product


def test_analyze_product(monkeypatch):
    calls = []
    def fake_llm(client, messages):
        calls.append(messages)
        n = len(calls)
        if n == 1:
            return "核心卖点：主动降噪…目标用户：通勤族"
        if n == 2:
            return "痛点1：续航短…高频词：续航、降噪"
        return "建议做长续航版本…定价$30-40"

    monkeypatch.setattr("agent.market_intelligence.product_analyzer._call_llm", fake_llm)
    with patch("agent.market_intelligence.product_analyzer.search_products",
               return_value=[{"title": "P40i", "url": "https://amzn/dp/B0", "snippet": ""}]):
        with patch("agent.market_intelligence.product_analyzer.scrape_product",
                   return_value={"title": "P40i", "price": 49.99, "rating": 4.5,
                                 "review_count": 100, "url": "https://amzn/dp/B0"}):
            with patch("db.executor.executor.execute",
                       return_value={"data": [], "columns": [], "row_count": 0}):
                r = analyze_product("Anker P40i")
    assert "卖点" in r["sellpoints"]
    assert "痛点" in r["pains"]
    assert "建议" in r["suggestions"]
    assert len(calls) == 3


def test_analyze_product_review_data_has_note(monkeypatch):
    """评论痛点环节传给 LLM 的数据必须带 note 标注，提示没有真实评论正文"""
    calls = []
    def fake_llm(client, messages):
        calls.append(messages)
        n = len(calls)
        if n == 1:
            return "核心卖点：主动降噪"
        if n == 2:
            return "痛点1：续航短"
        return "建议做长续航版本"

    monkeypatch.setattr("agent.market_intelligence.product_analyzer._call_llm", fake_llm)
    with patch("agent.market_intelligence.product_analyzer.search_products",
               return_value=[{"title": "P40i", "url": "https://amzn/dp/B0", "snippet": ""}]):
        with patch("agent.market_intelligence.product_analyzer.scrape_product",
                   return_value={"title": "P40i", "price": 49.99, "rating": 4.5,
                                 "review_count": 100, "url": "https://amzn/dp/B0"}):
            with patch("db.executor.executor.execute",
                       return_value={"data": [], "columns": [], "row_count": 0}):
                analyze_product("Anker P40i")
    # 第 2 次 LLM 调用是评论痛点环节，user 消息里应含 note 标注
    user_msg = calls[1][-1]["content"]
    assert "note" in user_msg
    assert "评论正文未抓取" in user_msg
