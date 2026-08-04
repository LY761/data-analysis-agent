# -*- coding: utf-8 -*-
"""I1/I2: 联网搜索回归测试 — Bing/维基解析、LLM 总结、knowledge 分支降级、开关"""
from unittest.mock import patch
from fastapi.testclient import TestClient
import main

from agent import web_search as ws
from agent.web_search import search_bing, search_baidu, search_wikipedia, web_search, summarize_with_llm

client = TestClient(main.app)

BING_HTML = """
<html><body>
<li class="b_algo"><h2><a href="https://example.com/rfm">RFM分析是什么</a></h2>
  <div class="b_caption"><p>RFM是客户价值分析模型，按最近购买/频率/金额给客户分层。</p></div></li>
<li class="b_algo"><h2><a href="https://example.com/rfm2">RFM模型详解</a></h2>
  <div class="b_caption"><p>RFM: Recency, Frequency, Monetary。</p></div></li>
</body></html>
"""


class FakeResp:
    status_code = 200
    text = ""
    _json = {}

    def json(self):
        return self._json


def _fake_get(factory):
    """返回一个 mock cr.get，使用 factory(url) 构造响应"""

    def _get(url, **kw):
        return factory(url)

    return _get


def test_search_bing_parses_results():
    resp = FakeResp()
    resp.text = BING_HTML
    with patch.object(ws.cr, "get", return_value=resp):
        out = search_bing("RFM分析", limit=2)
    assert len(out) == 2
    assert out[0]["title"] == "RFM分析是什么"
    assert out[0]["url"] == "https://example.com/rfm"
    assert "客户价值分析" in out[0]["snippet"]


def test_search_bing_failure_returns_empty():
    def _fail(url, **kw):
        raise RuntimeError("network")

    with patch.object(ws.cr, "get", _fail):
        assert search_bing("x") == []


def test_search_wikipedia_parses_json():
    resp = FakeResp()
    resp._json = {"query": {"search": [
        {"title": "RFM模型", "snippet": "RFM是<b>客户价值</b>分析模型"},
    ]}}
    with patch.object(ws.cr, "get", return_value=resp):
        out = search_wikipedia("RFM", limit=1)
    assert out[0]["title"] == "RFM模型"
    assert out[0]["url"].startswith("https://zh.wikipedia.org/wiki/")
    assert "客户价值" in out[0]["snippet"]


BAIDU_HTML = """
<html><body>
<div class="result c-container" id="1"><h3 class="t"><a href="https://baike.baidu.com/item/RFM">RFM分析</a></h3>
  <span class="c-abstract">RFM是客户价值分析模型。</span></div>
</body></html>
"""


def test_search_baidu_parses_results():
    """百度搜索结果解析：标题/URL/摘要"""
    resp = FakeResp()
    resp.text = BAIDU_HTML
    with patch.object(ws.cr, "get", return_value=resp):
        out = search_baidu("RFM", limit=1)
    assert out[0]["title"] == "RFM分析"
    assert out[0]["url"].startswith("https://")
    assert "客户价值" in out[0]["snippet"]


def test_web_search_falls_back_to_baidu():
    """必应中国失败 → 百度兜底"""
    calls = []

    def _factory(url, **kw):
        calls.append(url)
        if "cn.bing.com" in url:
            raise RuntimeError("bing blocked")
        resp = FakeResp()
        resp.text = BAIDU_HTML
        return resp

    with patch.object(ws.cr, "get", _factory):
        out = web_search("RFM", limit=1)
    assert out and out[0]["title"] == "RFM分析"
    assert any("baidu.com" in u for u in calls)


def test_web_search_full_fallback_chain():
    """必应+百度都失败 → 维基最后兜底"""
    calls = []

    def _factory(url, **kw):
        calls.append(url)
        if "cn.bing.com" in url:
            raise RuntimeError("bing blocked")
        if "baidu.com" in url:
            raise RuntimeError("baidu blocked")
        resp = FakeResp()
        resp._json = {"query": {"search": [{"title": "RFM模型", "snippet": "RFM是客户价值分析模型"}]}}
        return resp

    with patch.object(ws.cr, "get", _factory):
        out = web_search("RFM", limit=1)
    assert out and out[0]["title"] == "RFM模型"
    assert any("wikipedia.org" in u for u in calls)


def test_summarize_with_llm():
    results = [{"title": "RFM模型", "snippet": "RFM是客户价值分析", "url": "https://e.com/rfm"}]
    with patch("agent.web_search.OpenAI") as mock_openai:
        inst = mock_openai.return_value
        inst.chat.completions.create.return_value.choices[0].message.content = "RFM是客户价值分析模型（来源1）。"
        out = summarize_with_llm("什么是RFM", results)
    assert "RFM" in out
    assert "来源1" in out
    mock_openai.assert_called_once()


def test_summarize_empty_results():
    assert summarize_with_llm("q", []) == ""


def test_knowledge_mode_uses_web_search():
    """knowledge 模式：联网搜索 + LLM 总结返回增强回答"""
    fake_reply = "纯LLM回答"
    with patch("agent.agent_router.agent_router.route",
               return_value={"mode": "knowledge", "reply": fake_reply}):
        with patch("agent.web_search.web_search",
                   return_value=[{"title": "T", "snippet": "S", "url": "https://e.com"}]):
            with patch("agent.web_search.summarize_with_llm",
                       return_value="联网搜索后的完整回答（来源1）") as m:
                r = client.post("/api/query", json={"question": "什么是RFM分析"})
    assert r.status_code == 200
    assert r.json()["chat_reply"] == "联网搜索后的完整回答（来源1）"
    m.assert_called_once()


def test_knowledge_mode_falls_back_when_search_empty():
    """搜索无结果 → 降级纯 LLM 回答"""
    fake_reply = "纯LLM回答"
    with patch("agent.agent_router.agent_router.route",
               return_value={"mode": "knowledge", "reply": fake_reply}):
        with patch("agent.web_search.web_search", return_value=[]):
            with patch("agent.web_search.summarize_with_llm",
                       return_value="不应被调用") as m:
                r = client.post("/api/query", json={"question": "什么是RFM分析"})
    assert r.json()["chat_reply"] == fake_reply
    m.assert_not_called()


def test_knowledge_mode_disabled_by_config():
    """WEB_SEARCH_ENABLED=False → 不搜索直接纯 LLM 回答"""
    fake_reply = "纯LLM回答"
    with patch("config.WEB_SEARCH_ENABLED", False):
        with patch("agent.agent_router.agent_router.route",
                   return_value={"mode": "knowledge", "reply": fake_reply}):
            with patch("agent.web_search.web_search",
                       return_value=[{"title": "T", "snippet": "S", "url": "https://e.com"}]) as m:
                r = client.post("/api/query", json={"question": "什么是RFM分析"})
    assert r.json()["chat_reply"] == fake_reply
    m.assert_not_called()


def test_chat_mode_does_not_search():
    """chat 模式不联网（闲聊）"""
    fake_reply = "你好！"
    with patch("agent.agent_router.agent_router.route",
               return_value={"mode": "chat", "reply": fake_reply}):
        with patch("agent.web_search.web_search") as m:
            r = client.post("/api/query", json={"question": "你好"})
    assert r.json()["chat_reply"] == fake_reply
    m.assert_not_called()
