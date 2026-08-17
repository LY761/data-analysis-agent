# -*- coding: utf-8 -*-
"""D3: SQL 参数化回归测试 — executor 参数绑定 + 高危拼接点注入字符安全"""
from agent import workflow


def test_executor_parameterized_query():
    """executor.execute 支持 ? 占位符参数绑定"""
    from db.executor import executor
    r = executor.execute("SELECT COUNT(*) AS n FROM products WHERE category = ?", ("电子产品",))
    assert r["success"] is True
    assert r["data"][0]["n"] >= 1


def test_executor_parameterized_quote_safe():
    """含撇号的输入经参数绑定不会破坏 SQL 语法"""
    from db.executor import executor
    # 库里不存在这个产品，但查询本身不能因引号报错（此前拼接会碎语法）
    r = executor.execute(
        "SELECT product_name FROM products WHERE product_name = ? LIMIT 1",
        ("It's a trap' OR 1=1 --",),
    )
    assert r["success"] is True
    assert r["data"] == []  # 注入字符被当作字面量，查不到就是空


def test_workflow_find_similar_products_parameterized():
    """_find_similar_products 参数化：恶意输入不拼进 SQL，走 ? 绑定"""
    from db.executor import executor
    calls = []

    def fake_execute(sql, params=None):
        calls.append((sql, params))
        return {"success": True, "data": [], "row_count": 0}

    original = executor.execute
    executor.execute = fake_execute
    try:
        workflow._find_similar_products("查询一下' OR 1=1--", "SELECT 1")
    finally:
        executor.execute = original
    like_calls = [c for c in calls if "LIKE" in c[0]]
    assert like_calls, calls  # 存在参数化 LIKE 查询
    for sql, params in like_calls:
        assert "' OR 1=1" not in sql      # 恶意输入没有拼进 SQL
        assert params is not None          # 走参数绑定
    assert any(p and p[0] == "%查询一下%" for _, p in like_calls)


def test_workflow_analyze_why_parameterized():
    """_analyze_why 差评查询参数化：用户产品名含撇号安全"""
    from db.executor import executor
    calls = []

    def fake_execute(sql, params=None):
        calls.append((sql, params))
        return {"success": True, "data": [], "row_count": 0}

    original = executor.execute
    executor.execute = fake_execute
    try:
        workflow._analyze_why("为什么O'Brien卖得差", "SELECT 1",
                              {"data": [{"product_name": "O'Brien"}]}, {})
    finally:
        executor.execute = original
    review_calls = [call for call in calls if "WHERE p.product_name = ?" in call[0]]
    assert review_calls
    assert review_calls[0][1] == ("O'Brien",)
