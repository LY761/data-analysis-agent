# -*- coding: utf-8 -*-
"""C2: 图表推荐升级回归测试 — 日期值识别 / top15 bar / workflow 触发词"""
import asyncio
from unittest.mock import patch, AsyncMock
from agent.chart_recommender import chart_recommender
from agent import workflow


def test_date_detected_by_value_format():
    """列名不含日期关键词，但值符合日期格式 → 识别为时间列 → 折线图"""
    result = {
        "success": True,
        "data": [{"period": "2026-01", "sales": 10}, {"period": "2026-02", "sales": 20}],
        "columns": ["period", "sales"],
        "row_count": 2,
    }
    rec = chart_recommender.recommend(result)
    assert rec["chart_type"] == "line"
    assert rec["echarts_option"] is not None


def test_date_detected_by_column_name_still_works():
    result = {
        "success": True,
        "data": [{"order_date": "2026-01-05", "sales": 10}],
        "columns": ["order_date", "sales"],
        "row_count": 1,
    }
    rec = chart_recommender.recommend(result)
    assert rec["chart_type"] == "line"


def test_large_result_uses_top15_bar():
    """>20 行分类数据 → Top15 柱状图（不再退化为表格）"""
    data = [{"cat": f"产品{i}", "sales": i} for i in range(25)]
    result = {"success": True, "data": data, "columns": ["cat", "sales"], "row_count": 25}
    rec = chart_recommender.recommend(result)
    assert rec["chart_type"] == "bar"
    assert len(rec["echarts_option"]["xAxis"]["data"]) == 15


def _mk_state(question):
    return {
        "question": question,
        "original_question": question,
        "sql": "SELECT month, sales FROM orders",
        "schema_context": {"tables": [{"table": "orders"}], "columns": []},
        "intent": "sales_aggregation",
        "retrieval_span": None,
        "stream_answer_cb": None,
    }


def test_workflow_triggers_chart_for_monthly_trend():
    """'每月销售趋势' 命中扩展触发词 → 生成图表"""
    fake_result = {
        "success": True,
        "data": [{"month": "2026-01", "sales": 10}, {"month": "2026-02", "sales": 20}],
        "columns": ["month", "sales"],
        "row_count": 2,
        "execution_time_ms": 1.0,
        "warnings": [],
    }
    state = _mk_state("每月销售趋势")
    with patch("agent.workflow.executor.execute", return_value=fake_result):
        with patch("agent.workflow.result_checker.check", side_effect=lambda r: r):
            with patch("agent.workflow.sql_generator.answer_summary",
                       new=AsyncMock(return_value="")):
                out = asyncio.run(workflow.execute_sql_node(state))
    assert out["chart_recommendation"]["chart_type"] == "line"


def test_workflow_no_chart_without_viz_keyword():
    """普通查询（不含可视化词）仍不生成图表（不打扰）"""
    fake_result = {
        "success": True,
        "data": [{"product_name": "显示器", "total_sales": 100}],
        "columns": ["product_name", "total_sales"],
        "row_count": 1,
        "execution_time_ms": 1.0,
        "warnings": [],
    }
    state = _mk_state("显示器的销售额是多少")
    with patch("agent.workflow.executor.execute", return_value=fake_result):
        with patch("agent.workflow.result_checker.check", side_effect=lambda r: r):
            with patch("agent.workflow.sql_generator.answer_summary",
                       new=AsyncMock(return_value="")):
                out = asyncio.run(workflow.execute_sql_node(state))
    assert out["chart_recommendation"]["chart_type"] is None
