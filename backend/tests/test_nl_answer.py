# -*- coding: utf-8 -*-
"""C1: NL 回答 LLM 增强回归测试 — 普通路径 LLM 回答 / 开关关闭降级 / LLM 空降级"""
import asyncio
from unittest.mock import patch, AsyncMock
from agent import workflow


def _mk_state():
    return {
        "question": "本月销售额是多少",
        "original_question": "本月销售额是多少",
        "sql": "SELECT SUM(total_amount) AS total_sales FROM orders",
        "schema_context": {"tables": [{"table": "orders"}], "columns": []},
        "intent": "sales_aggregation",
        "retrieval_span": None,
        "stream_answer_cb": None,
    }


FAKE_RESULT = {
    "success": True,
    "data": [{"product_name": "显示器", "total_sales": 100}],
    "columns": ["product_name", "total_sales"],
    "row_count": 1,
    "execution_time_ms": 1.0,
    "warnings": [],
}


def test_execute_sql_node_uses_llm_answer_when_enabled():
    """普通路径（无 stream_cb）且 NL_ANSWER_LLM 开启 → 用 LLM 口语化回答"""
    state = _mk_state()
    with patch("agent.workflow.executor.execute", return_value=FAKE_RESULT):
        with patch("agent.workflow.result_checker.check", side_effect=lambda r: r):
            with patch("agent.workflow.sql_generator.answer_summary",
                       new=AsyncMock(return_value="本月销售额是100元")) as m:
                out = asyncio.run(workflow.execute_sql_node(state))
    assert out["nl_answer"] == "本月销售额是100元"
    m.assert_awaited_once_with("本月销售额是多少", state["sql"], FAKE_RESULT)


def test_execute_sql_node_falls_back_to_rule_when_disabled():
    """NL_ANSWER_LLM=False → 规则版回答，不调 LLM"""
    state = _mk_state()
    with patch("config.NL_ANSWER_LLM", False):
        with patch("agent.workflow.executor.execute", return_value=FAKE_RESULT):
            with patch("agent.workflow.result_checker.check", side_effect=lambda r: r):
                with patch("agent.workflow.sql_generator.answer_summary",
                           new=AsyncMock(return_value="不应被使用")) as m:
                    out = asyncio.run(workflow.execute_sql_node(state))
    assert "100" in out["nl_answer"]  # 规则版包含数值
    m.assert_not_awaited()


def test_execute_sql_node_falls_back_when_llm_empty():
    """LLM 回答为空（失败/降级）→ 规则版回答兜底"""
    state = _mk_state()
    with patch("agent.workflow.executor.execute", return_value=FAKE_RESULT):
        with patch("agent.workflow.result_checker.check", side_effect=lambda r: r):
            with patch("agent.workflow.sql_generator.answer_summary",
                       new=AsyncMock(return_value="")) as m:
                out = asyncio.run(workflow.execute_sql_node(state))
    assert out["nl_answer"]  # 降级规则版非空
    m.assert_awaited_once()
