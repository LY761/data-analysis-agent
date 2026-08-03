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


def test_knowledge_not_hijacked_by_market():
    # 知识类词优先于市场情报（"什么是"命中知识检查 step 4，先于市场情报返回）
    r = agent_router.route("什么是选品机会")
    assert r["mode"] != "market_intelligence"


def test_sql_not_hijacked_by_market():
    # 内部数据指标词命中 DATA_OVERRIDE，跳过市场情报，落入 sql_query
    r = agent_router.route("研究一下上个月的销售额")
    assert r["mode"] != "market_intelligence"


def test_competition_keyword_independently_triggers_market():
    # "竞争怎么样" 既在 SELECTION 也在 MARKET_INTEL，能独立触发市场 → 选品
    r = agent_router.route("蓝牙耳机竞争怎么样")
    assert r["mode"] == "market_intelligence"
    assert r["sub"] == "selection"


def test_cost_data_word_overrides_market():
    # DATA_OVERRIDE 新增的成本词，让 "研究一下我们的成本" 落入 sql_query
    r = agent_router.route("研究一下我们的成本")
    assert r["mode"] != "market_intelligence"
    assert r["mode"] == "sql_query"
