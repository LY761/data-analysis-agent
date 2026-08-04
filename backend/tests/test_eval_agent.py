# backend/tests/test_eval_agent.py
"""评测脚本回归测试 — 修复 sql_logic_hit 恒为 null 的 bug"""
import asyncio
from unittest.mock import patch
from eval_agent import _compute
import eval_agent as ev


def test_compute_sql_logic_hit_value():
    """含 expect_sql_frag 的结果应计算出 sql_logic_hit 数值，不再是 null"""
    results = [
        {"expect_mode": "sql_query", "category": "test", "validation": "all_passed", "error": None,
         "row_count": 3, "expect_data": "yes", "has_data": True,
         "expect_sql_frag": "SUM", "sql": "SELECT SUM(total_amount) FROM orders",
         "recall": 1.0, "route_ok": True},
        {"expect_mode": "sql_query", "category": "test", "validation": "all_passed", "error": None,
         "row_count": 0, "expect_data": "yes", "has_data": False,
         "expect_sql_frag": "COUNT", "sql": "SELECT COUNT(*) FROM orders",
         "recall": 1.0, "route_ok": True},
    ]
    metrics = _compute(results)
    assert metrics["sql_logic_hit"] == 1.0
    assert metrics["exec_success"] == 1.0
    assert metrics["recall"] == 1.0


def test_compute_sql_logic_miss_detected():
    """期望片段未出现在 SQL 中 → sql_logic_hit = 0（能区分命中与未命中）"""
    results = [
        {"expect_mode": "sql_query", "category": "test", "validation": "all_passed", "error": None,
         "row_count": 3, "expect_data": "yes", "has_data": True,
         "expect_sql_frag": "AVG", "sql": "SELECT SUM(total_amount) FROM orders",
         "recall": 1.0, "route_ok": True},
    ]
    metrics = _compute(results)
    assert metrics["sql_logic_hit"] == 0.0


def test_evaluate_item_contains_expect_sql_frag():
    """evaluate() 构建的 item 必须携带 expect_sql_frag（此前缺失导致指标恒 null）"""
    q = {"id": 1, "question": "上个月销售额是多少", "category": "time",
         "expect_mode": "sql_query", "expect_tables": ["orders"],
         "expect_data": "yes", "expect_sql_frag": "SUM"}

    async def fake_run_sql(question):
        return {"validation": "all_passed", "error": None, "row_count": 1,
                "sql": "SELECT SUM(total_amount) FROM orders"}

    async def run():
        with patch.object(ev.agent_router, "route",
                          return_value={"mode": "sql_query", "rewritten": q["question"]}):
            with patch.object(ev, "_run_sql", side_effect=fake_run_sql):
                results = await ev.evaluate([q])
        return results[0]

    item = asyncio.run(run())
    assert item["expect_sql_frag"] == "SUM"
    assert item["sql"] == "SELECT SUM(total_amount) FROM orders"
    assert item["route_ok"] is True
