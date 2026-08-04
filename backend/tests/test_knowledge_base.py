# -*- coding: utf-8 -*-
"""K3: 企业知识库回归测试（mock 嵌入模型提速：真实 Chroma 384 维，不加载 BGE）"""
from unittest.mock import patch
import numpy as np
from fastapi.testclient import TestClient
import main

from agent.knowledge_base import _chunk_text, _extract_text, kb

client = TestClient(main.app)

TXT = ("供应商准入标准：\n一、注册资本不低于100万。\n\n"
       "二、需提供 ISO9001 质量认证。\n\n"
       "三、交货周期不超过 30 天。\n\n" * 5)


from unittest.mock import MagicMock


class FakeModel:
    """替代 SentenceTransformer（不加载 BGE 模型）"""

    def encode(self, texts, **kw):
        n = len(texts) if isinstance(texts, list) else 1
        return np.full((n, 8), 0.05, dtype=float)


def _use_fake_store():
    """替换 kb._col 为 MagicMock：完全绕开真实 Chroma（维度/写入/耗时）"""
    fake = MagicMock()
    fake.query.return_value = {
        "documents": [["供应商准入标准：\n一、注册资本不低于100万。\n\n二、需提供 ISO9001 质量认证。"]],
        "metadatas": [[{"doc": "g_test_policy.txt", "chunk": 0}]],
        "distances": [[0.1]],
    }
    fake.get.return_value = {
        "metadatas": [{"doc": "g_test_policy.txt", "chunk": 0}],
    }
    kb.model = FakeModel()
    kb._col = fake


def test_chunk_text_splits_and_overlaps():
    chunks = _chunk_text(TXT, size=100, overlap=20)
    assert len(chunks) >= 2
    assert all(len(c) <= 100 for c in chunks)
    big = "字" * 5000
    cs = _chunk_text(big, size=500, overlap=100)
    assert len(cs) >= 10
    assert all(len(c) <= 500 for c in cs)


def test_extract_text_txt():
    assert "供应商准入标准" in _extract_text("policy.txt", TXT.encode("utf-8"))


def test_kb_add_list_search_delete():
    """add → list → search → delete 逻辑（collection 为 mock）"""
    _use_fake_store()
    doc = "g_test_policy.txt"
    r = kb.add_document(doc, TXT.encode("utf-8"))
    assert r["doc"] == doc
    assert r["chunks"] >= 1
    kb._col.add.assert_called_once()
    kb._col.delete.assert_called_once()

    docs = kb.list_documents()
    assert any(d["doc"] == doc for d in docs)

    hits = kb.search("供应商注册资本要求", top_k=2)
    assert hits and hits[0]["doc"] == doc
    assert "注册资本" in hits[0]["text"]

    kb.delete_document(doc)
    kb._col.delete.assert_called_with(where={"doc": doc})


def test_kb_answer_with_llm():
    """answer：检索结果 + LLM 带文档名总结"""
    _use_fake_store()
    doc = "g_test_policy.txt"
    kb.add_document(doc, TXT.encode("utf-8"))
    with patch("openai.OpenAI") as mock_openai:
        inst = mock_openai.return_value
        inst.chat.completions.create.return_value.choices[0].message.content = "注册资本不低于100万（g_test_policy.txt）。"
        answer, refs = kb.answer("注册资本要求是什么")
    assert "100万" in answer
    assert doc in refs


def test_kb_upload_endpoint():
    """端点测试 mock kb 单例（不触发真实模型/Chroma 写）"""
    with patch.object(kb, "add_document",
                      return_value={"doc": "g_test_endpoint.txt", "chunks": 3}) as add_m:
        r = client.post("/api/kb/upload",
                        files={"file": ("g_test_endpoint.txt", TXT.encode("utf-8"), "text/plain")})
    assert r.status_code == 200, r.text
    assert r.json()["doc"] == "g_test_endpoint.txt"
    add_m.assert_called_once()

    with patch.object(kb, "list_documents", return_value=[{"doc": "g_test_endpoint.txt", "chunks": 3}]):
        lst = client.get("/api/kb/list").json()
    assert any(d["doc"] == "g_test_endpoint.txt" for d in lst["documents"])

    with patch.object(kb, "delete_document") as del_m:
        d = client.delete("/api/kb/g_test_endpoint.txt")
    assert d.status_code == 200
    del_m.assert_called_once_with("g_test_endpoint.txt")


def test_knowledge_mode_uses_kb_first():
    """knowledge 意图：知识库命中优先于联网搜索"""
    fake_reply = "纯LLM回答"
    with patch("agent.agent_router.agent_router.route",
               return_value={"mode": "knowledge", "reply": fake_reply}):
        with patch("agent.knowledge_base.kb.answer",
                   return_value=("知识库回答：注册资本100万（policy.txt）。", ["policy.txt"])):
            with patch("agent.web_search.web_search") as m:
                r = client.post("/api/query", json={"question": "供应商注册资本要求"})
    assert r.status_code == 200
    assert "知识库回答" in r.json()["chat_reply"]
    m.assert_not_called()  # 知识库命中，不联网


def test_knowledge_mode_falls_back_when_kb_empty():
    """知识库无命中 → 降级联网搜索"""
    fake_reply = "纯LLM回答"
    with patch("agent.agent_router.agent_router.route",
               return_value={"mode": "knowledge", "reply": fake_reply}):
        with patch("agent.knowledge_base.kb.answer", return_value=(None, [])):
            with patch("agent.web_search.web_search",
                       return_value=[{"title": "T", "snippet": "S", "url": "https://e.com"}]):
                with patch("agent.web_search.summarize_with_llm",
                           return_value="联网回答") as m:
                    r = client.post("/api/query", json={"question": "什么是RFM"})
    assert "联网回答" in r.json()["chat_reply"]
    m.assert_called_once()
