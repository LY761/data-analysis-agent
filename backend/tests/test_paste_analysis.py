# -*- coding: utf-8 -*-
"""A方案: 粘贴数据分析回归测试 — 三种模式 / JSON容错 / 端点"""
from unittest.mock import patch
from fastapi.testclient import TestClient
import main  # 触发 app 构建

from agent.market_intelligence import paste_analysis as pa

client = TestClient(main.app)

PRODUCT_JSON = """{"product": {"title": "Anker 蓝牙耳机", "price": "$29.99", "rating": "4.5"},
"sellpoints": "续航30小时；低延迟游戏模式",
"pains": "佩戴舒适度一般；充电盒偏大",
"suggestions": "改进耳塞尺寸；缩小充电盒"}"""

SELECTION_JSON = """{"category": "蓝牙耳机", "profile": "中端价位竞争激烈",
"recommendation": {"score": 72, "verdict": "可考虑切入", "price_band": "$25-45",
"competition": "中", "risks": ["同质化"], "differentiation": "主打降噪+续航",
"reasoning": "需求稳定"}}"""


def test_paste_product_mode():
    with patch("agent.market_intelligence.paste_analysis._call_llm",
               return_value=PRODUCT_JSON):
        out = pa.analyze_pasted("product", "Anker 蓝牙耳机 $29.99 评分4.5")
    assert out["error"] is None
    assert out["product"]["title"] == "Anker 蓝牙耳机"
    assert out["sellpoints"]
    assert out["pains"]
    assert out["suggestions"]
    assert out["internal"] == []


def test_paste_selection_mode():
    with patch("agent.market_intelligence.paste_analysis._call_llm",
               return_value=SELECTION_JSON):
        out = pa.analyze_pasted("selection", "蓝牙耳机市场：主流 $25-45，安克/绿联占大头")
    assert out["error"] is None
    assert out["recommendation"]["score"] == 72
    assert out["recommendation"]["verdict"]
    assert out["profile"]


def test_paste_competitor_mode():
    with patch("agent.market_intelligence.paste_analysis._call_llm",
               return_value="1. 绿联定价中端...\n2. 优势是性价比\n3. 机会在高端\n4. 建议差异化"):
        out = pa.analyze_pasted("competitor", "绿联：$19.99，评分4.3，主打快充")
    assert out["error"] is None
    assert "绿联" in out["analysis"]
    assert out["data_sources"] == ["用户提供"]


def test_paste_extract_json_fenced():
    raw = '```json\n{"score": 70}\n```'
    assert pa._extract_json(raw) == {"score": 70}


def test_paste_extract_json_with_prose():
    raw = '分析如下：{"score": 70, "verdict": "ok"} 以上就是结论'
    assert pa._extract_json(raw)["verdict"] == "ok"


def test_paste_empty_text_returns_error():
    out = pa.analyze_pasted("product", "   ")
    assert out.get("error")


def test_paste_invalid_mode():
    out = pa.analyze_pasted("unknown", "text")
    assert out.get("error")


def test_paste_endpoint_product():
    fake = {"query": "Anker 蓝牙耳机", "product": {"title": "Anker 蓝牙耳机"},
            "sellpoints": "卖点", "pains": "痛点", "suggestions": "建议",
            "internal": [], "error": None}
    with patch("agent.market_intelligence.paste_analysis.analyze_pasted",
               return_value=fake) as m:
        r = client.post("/api/market/paste",
                        json={"text": "Anker 蓝牙耳机 $29.99", "mode": "product"})
    assert r.status_code == 200
    assert r.json()["sellpoints"] == "卖点"
    m.assert_called_once_with("product", "Anker 蓝牙耳机 $29.99")


def test_paste_endpoint_competitor_default_mode():
    fake = {"name": "粘贴竞品数据", "analysis": "洞察", "data_sources": ["用户提供"], "error": None}
    with patch("agent.market_intelligence.paste_analysis.analyze_pasted",
               return_value=fake):
        r = client.post("/api/market/paste", json={"text": "绿联 价格$19.99"})
    assert r.status_code == 200
    assert r.json()["analysis"] == "洞察"
