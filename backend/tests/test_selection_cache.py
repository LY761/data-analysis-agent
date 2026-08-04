# -*- coding: utf-8 -*-
"""D1/D2: 选品缓存 + 硬编码路径配置化回归测试"""
import sqlite3
from unittest.mock import patch
from agent.market_intelligence import selection


def _clear_cache():
    from cache.query_cache import _ensure_table
    from config import DEMO_DB_PATH
    _ensure_table()
    conn = sqlite3.connect(DEMO_DB_PATH)
    conn.execute("DELETE FROM query_cache")
    conn.commit()
    conn.close()


def test_selection_cache_hit_skips_scraping():
    """第二次同类目调用命中缓存：不再搜索/抓取/调 LLM"""
    _clear_cache()
    with patch("agent.market_intelligence.selection.search_products",
               return_value=[{"title": "A", "url": "u1"}]) as sp:
        with patch("agent.market_intelligence.selection.scrape_product",
                   return_value={"title": "A", "price": 10}):
            with patch("agent.market_intelligence.selection._call_llm",
                       side_effect=["画像", '{"score": 70, "verdict": "推荐"}']):
                first = selection.analyze_selection("蓝牙耳机")
                assert first["recommendation"]["score"] == 70
                assert sp.call_count == 1
                # 第二次：命中缓存，search 不再被调用
                second = selection.analyze_selection("蓝牙耳机")
                assert second["recommendation"]["score"] == 70
                assert sp.call_count == 1


def test_selection_stream_path_skips_cache():
    """流式路径（stream_cb）不读写缓存：每次都重跑（保证进度实时）"""
    _clear_cache()
    with patch("agent.market_intelligence.selection.search_products",
               return_value=[]) as sp:
        with patch("agent.market_intelligence.selection.scrape_product", return_value={}):
            with patch("agent.market_intelligence.selection._call_llm",
                       side_effect=["画像", '{"score": 70}']):
                selection.analyze_selection("蓝牙耳机", stream_cb=lambda m: None)
                selection.analyze_selection("蓝牙耳机", stream_cb=lambda m: None)
    assert sp.call_count == 2


def test_analyzer_scraper_path_from_config():
    import agent.competitor_analysis.analyzer as an
    from config import COMPETITOR_SCRAPER_PATH
    assert an.SCRAPER_PATH == COMPETITOR_SCRAPER_PATH
    assert "competitor-scraper" in an.SCRAPER_PATH


def test_reviews_glob_default_from_config():
    import agent.market_intelligence.reviews as rv
    from config import COMPETITOR_SCRAPER_PATH
    assert COMPETITOR_SCRAPER_PATH.replace("\\", "/") in rv.REVIEWS_GLOB
    assert rv.REVIEWS_GLOB.endswith("real_negative_reviews_*.json")
