# backend/tests/test_market_api.py
from unittest.mock import patch, ANY
from fastapi.testclient import TestClient
import main  # 触发 app 构建

client = TestClient(main.app)


def test_market_selection_endpoint():
    fake_result = {"category": "蓝牙耳机", "products": [], "profile": "分析…",
                   "internal": [], "recommendation": {"score": 70, "verdict": "推荐"},
                   "error": None}
    # 端点在函数内部 `from agent.market_intelligence.selection import analyze_selection`，
    # 所以 patch 目标必须是模块的真实来源，不是 api.routes.analyze_selection。
    with patch("agent.market_intelligence.selection.analyze_selection",
               return_value=fake_result) as m:
        r = client.post("/api/market/selection", json={"category": "蓝牙耳机"})
    assert r.status_code == 200
    assert r.json()["recommendation"]["score"] == 70
    m.assert_called_once_with("蓝牙耳机")


def test_market_product_endpoint():
    fake_result = {"query": "P40i", "product": {}, "sellpoints": "卖点", "pains": "痛点",
                   "internal": [], "suggestions": "建议", "error": None}
    with patch("agent.market_intelligence.product_analyzer.analyze_product",
               return_value=fake_result) as m:
        r = client.post("/api/market/product", json={"query": "Anker P40i"})
    assert r.status_code == 200
    assert r.json()["sellpoints"] == "卖点"
    m.assert_called_once_with("Anker P40i")


def test_market_stream_endpoint():
    """SSE 流式：mock 路由 + 分析函数，验证事件序列 status/result/done"""
    fake_result = {"category": "蓝牙耳机", "products": [], "profile": "分析…",
                   "internal": [], "recommendation": {"score": 70, "verdict": "推荐"},
                   "error": None}
    with patch("agent.agent_router.agent_router.route",
               return_value={"mode": "market_intelligence", "sub": "selection",
                             "query": "蓝牙耳机"}):
        with patch("agent.market_intelligence.selection.analyze_selection",
                   return_value=fake_result) as m:
            r = client.post("/api/market/stream", json={"query": "蓝牙耳机"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    assert "event: result" in r.text
    assert "event: done" in r.text
    # _run_market 走 asyncio.to_thread(analyze_selection, query, None, stream_cb)
    m.assert_called_once_with("蓝牙耳机", None, ANY)


def test_market_stream_non_market_mode_falls_back_to_selection():
    """非市场模式不再死端：直接跑选品兜底，仍有 result/done 事件"""
    fake_result = {"category": "蓝牙耳机", "products": [], "profile": "分析…",
                   "internal": [], "recommendation": {"score": 70, "verdict": "推荐"},
                   "error": None}
    with patch("agent.agent_router.agent_router.route",
               return_value={"mode": "sql_query", "rewritten": "蓝牙耳机"}):
        with patch("agent.market_intelligence.selection.analyze_selection",
                   return_value=fake_result) as m:
            r = client.post("/api/market/stream", json={"query": "蓝牙耳机"})
    assert r.status_code == 200
    assert "event: result" in r.text
    assert "event: done" in r.text
    # 兜底：sub=selection，query=原始 request.query
    m.assert_called_once_with("蓝牙耳机", None, ANY)
