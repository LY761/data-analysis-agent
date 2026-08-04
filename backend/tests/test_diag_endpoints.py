# -*- coding: utf-8 -*-
"""F3/F4 诊断: db/metrics/history 端点实测（TestClient）"""
from fastapi.testclient import TestClient
import main

client = TestClient(main.app)


def test_db_list_endpoint():
    r = client.get("/api/db/list")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "databases" in body
    assert any(d.get("is_current") for d in body["databases"])


def test_history_endpoint_noworks():
    """修复后 /api/history/default 应返回 200（此前 get_history_summary 未 import 会 500）"""
    r = client.get("/api/history/default")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "recent_questions" in body
    assert "turn_count" in body


def test_metrics_endpoint():
    r = client.get("/api/metrics/retrieval")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "summary" in body
    assert "recent" in body
