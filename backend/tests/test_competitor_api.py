# backend/tests/test_competitor_api.py
"""竞品分析 API 修复回归测试 — 验证端点不再 500、结构正确、流式可用"""
import sqlite3
from unittest.mock import patch
from fastapi.testclient import TestClient
import main  # 触发 app 构建

client = TestClient(main.app)


def _clear_competitor_cache():
    """清掉竞品缓存，避免 mock 断言受 5 分钟 TTL 缓存影响"""
    from cache.query_cache import _ensure_table
    from config import DEMO_DB_PATH
    _ensure_table()
    conn = sqlite3.connect(DEMO_DB_PATH)
    conn.execute("DELETE FROM query_cache")
    conn.commit()
    conn.close()


def test_competitor_analyze_endpoint():
    """POST /competitor/analyze 应返回真实实现的结构，不再 500"""
    _clear_competitor_cache()
    fake = {
        "found": True, "name": "绿联",
        "analysis": "绿联在中端市场有价格优势……",
        "data_sources": ["绿联_UGREEN.json"],
        "internal_summary": {"products": [], "monthly_sales": 100},
    }
    # 端点顶部已 import analyze_competitor，patch 目标是 api.routes.analyze_competitor
    with patch("api.routes.analyze_competitor", return_value=fake) as m:
        r = client.post("/api/competitor/analyze", json={"company_name": "绿联"})
    assert r.status_code == 200
    data = r.json()
    assert data["found"] is True
    assert data["name"] == "绿联"
    assert data["analysis"] == fake["analysis"]
    assert data["data_sources"] == fake["data_sources"]
    assert data["internal_summary"] == fake["internal_summary"]
    assert data["trace_id"]
    assert data["cache_hit"] is False
    # 以线程方式调用（asyncio.to_thread），stream_cb 传 None
    m.assert_called_once_with("绿联", None)


def test_competitor_analyze_missing_name():
    """缺公司名 → 友好错误，不 500"""
    r = client.post("/api/competitor/analyze", json={"company_name": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["error"] == "请提供竞品公司名称"
    assert body["company_name"] == ""


def test_competitor_list_endpoint():
    """GET /competitor/list 应正常返回竞品列表"""
    fake_list = [{"name": "安克创新", "filename": "安克创新_Anker.json", "has_data": True}]
    with patch("api.routes.analyzer.list_competitors", return_value=fake_list) as m:
        r = client.get("/api/competitor/list")
    assert r.status_code == 200
    assert r.json()["competitors"] == fake_list
    m.assert_called_once_with()


def test_competitor_ws_stream():
    """WS 流式：status → delta（逐token）→ result → done"""
    _clear_competitor_cache()

    def fake_analyze(name, stream_cb):
        if stream_cb:
            stream_cb("报告")
            stream_cb("生成中")
        return {"found": True, "name": name, "analysis": "报告生成中",
                "internal_summary": {}, "error": None}

    with patch("api.routes.analyze_competitor", side_effect=fake_analyze):
        with client.websocket_connect("/api/competitor/analyze/ws") as ws:
            ws.send_json({"company_name": "绿联"})
            types, deltas = [], []
            while True:
                msg = ws.receive_json()
                types.append(msg["type"])
                if msg["type"] == "delta":
                    deltas.append(msg["delta"])
                if msg["type"] == "done":
                    break
    assert "status" in types, types
    assert "result" in types, types
    assert "done" in types, types
    assert deltas == ["报告", "生成中"], deltas
