# -*- coding: utf-8 -*-
"""P1: 数据门槛回归测试 — 无真实数据时返回明确错误+引导，绝不输出推断结论"""
from unittest.mock import patch
from agent.market_intelligence import selection, product_analyzer


def test_selection_empty_data_returns_error_no_llm():
    """选品：搜索/抓取全空 → 明确错误+粘贴引导，LLM 绝不参与推断"""
    import uuid
    category = f"g_gate_{uuid.uuid4().hex[:8]}"  # 唯一类目名避开缓存命中
    with patch("agent.market_intelligence.selection.search_products", return_value=[]):
        with patch("agent.market_intelligence.selection.scrape_product", return_value={}):
            with patch("agent.market_intelligence.selection._call_llm") as llm:
                out = selection.analyze_selection(category)
    assert out["error"], out
    assert "粘贴数据分析" in out["error"]  # 引导用户走可靠路径
    assert out["recommendation"] == {}     # 无推断结论
    assert out["profile"] == ""
    llm.assert_not_called()                # 关键：不调 LLM 编造


def test_selection_partial_data_still_analyzes():
    """有真实数据 → 正常分析（门槛放行）"""
    import uuid
    category = f"g_gate2_{uuid.uuid4().hex[:8]}"
    with patch("agent.market_intelligence.selection.search_products",
               return_value=[{"title": "A", "url": "https://a.com/dp/B0ABC"}]):
        with patch("agent.market_intelligence.selection.scrape_product",
                   return_value={"title": "A", "price": 10}):
            with patch("agent.market_intelligence.selection._compare_internal", return_value=[]):
                with patch("agent.market_intelligence.selection._call_llm",
                           side_effect=["画像", '{"score": 70}']):
                    out = selection.analyze_selection(category)
    assert out["error"] is None
    assert out["products"]


def test_product_not_found_guides_to_paste():
    """商品研究：搜索无结果 → 明确错误 + 粘贴引导"""
    with patch("agent.market_intelligence.product_analyzer.search_products", return_value=[]):
        out = product_analyzer.analyze_product("某个不存在的产品xyz")
    assert out["error"]
    assert "粘贴数据分析" in out["error"]
    assert out["sellpoints"] == ""  # 无推断


def test_product_fetch_failed_guides_to_paste():
    """商品研究：抓取失败（无标题）→ 明确错误 + 粘贴引导"""
    with patch("agent.market_intelligence.product_analyzer.search_products",
               return_value=[{"title": "A", "url": "https://a.com/dp/B0ABC"}]):
        with patch("agent.market_intelligence.product_analyzer.scrape_product",
                   return_value={"title": "", "price": None}):
            out = product_analyzer.analyze_product("测试产品")
    assert out["error"]
    assert "粘贴数据分析" in out["error"]
