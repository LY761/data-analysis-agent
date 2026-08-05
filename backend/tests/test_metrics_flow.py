# -*- coding: utf-8 -*-
"""G1: 指标看板数据链路回归测试 — flush 幂等 + 失败路径兜底落库"""
import sqlite3
from fastapi.testclient import TestClient
import main

from config import DEMO_DB_PATH
from services.retrieval_metrics import RetrievalSpan

client = TestClient(main.app)


def _clear_metric_logs():
    conn = sqlite3.connect(DEMO_DB_PATH)
    conn.execute("DELETE FROM retrieval_log")
    # 缓存命中路径不创建 span、不落库 —— 测试必须清缓存避免污染
    conn.execute("DELETE FROM query_cache")
    conn.commit()
    conn.close()


def test_flush_idempotent():
    """flush 幂等：调用两次只落库一条（execute_sql 节点 + 外层兜底各一次）"""
    _clear_metric_logs()
    span = RetrievalSpan("g1_flush_idempotent", DEMO_DB_PATH)
    span.flush()
    span.flush()  # 第二次应为 no-op
    conn = sqlite3.connect(DEMO_DB_PATH)
    n = conn.execute(
        "SELECT COUNT(*) FROM retrieval_log WHERE question='g1_flush_idempotent'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


def test_query_failure_still_logs_metrics():
    """查询失败（LLM 不可用等上游错误）也应落库一条指标记录（此前失败路径不 flush）"""
    _clear_metric_logs()
    # 无 LLM key 环境，/api/query 走 workflow 会失败——正应触发 routes 兜底 flush
    # 问题避开 quick_card/chat 关键词，确保走 sql_query 流水线（span 才会创建）
    r = client.post("/api/query", json={"question": "g1_指标测试 哪个供应商的订单最多"})
    assert r.status_code == 200, r.text
    conn = sqlite3.connect(DEMO_DB_PATH)
    n = conn.execute(
        "SELECT COUNT(*) FROM retrieval_log WHERE question LIKE 'g1_指标测试%'"
    ).fetchone()[0]
    conn.close()
    assert n == 1, f"retrieval_log 未落库（响应: {r.json().get('error')}）"


def test_query_stream_writes_metrics():
    """前端聊天实际走 /api/query/stream（SSE）——该路径也必须落库检索指标"""
    _clear_metric_logs()
    # 无 LLM 环境会失败，但兜底 flush 应保证落库一条
    r = client.post("/api/query/stream",
                    json={"question": "g_stream_指标测试-哪个供应商的订单最多"})
    assert r.status_code == 200
    _ = r.text  # 消费 SSE 流（触发完整执行）
    conn = sqlite3.connect(DEMO_DB_PATH)
    n = conn.execute(
        "SELECT COUNT(*) FROM retrieval_log WHERE question LIKE 'g_stream_指标测试%'"
    ).fetchone()[0]
    conn.close()
    assert n == 1, "流式查询未落库指标"
