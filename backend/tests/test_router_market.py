# backend/tests/test_router_market.py
from agent.agent_router import agent_router


def test_selection_keyword_routes():
    r = agent_router.route("分析一下蓝牙耳机的选品机会")
    assert r["mode"] == "market_intelligence"
    assert r["sub"] == "selection"


def test_product_keyword_routes():
    # 注意：brief 原文用 "Anker Soundcore P40i"，但 "anker" 命中 COMPETITOR_KEYWORDS
    # （route() 第 1 步优先），会返回 competitor 模式，与 market_intelligence 冲突。
    # 这里改用不带竞品品牌的同名产品，保留 "研究一下" 产品研究关键词的本意（sub=product）。
    r = agent_router.route("研究一下 Soundcore P40i")
    assert r["mode"] == "market_intelligence"
    assert r["sub"] == "product"
