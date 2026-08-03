# backend/tests/test_conversation_topic.py
"""话题自适应上下文测试：同话题检索稳定（锚不变）、换话题检索发散（锚=当前）。

纯函数单测（不触 DB）：用 object.__new__ 绕过 __init__ 的 SQLite 加载，
只填充内存 turns。"""
import asyncio
import json
import time
from agent import conversation_memory as cm
from agent.conversation_memory import ConversationMemory


def _make_mem(turns):
    """构造不带 DB 副作用的内存会话"""
    mem = ConversationMemory.__new__(ConversationMemory)
    mem.session_id = "test-topic"
    mem.max_turns = 20
    mem.turns = turns
    mem.templates = {}
    mem.created_at = time.time()
    return mem


# 同话题：话题首轮完整问题 + 一轮追问
SAME_TOPIC_TURNS = [
    {"question": "蓝牙耳机各地区销售额", "sql": "SELECT region, SUM(total_amount) FROM orders GROUP BY region"},
    {"question": "按地区分组", "sql": "SELECT region, COUNT(*) FROM orders GROUP BY region"},
]


def test_get_topic_context_same_topic_segments_and_anchor():
    mem = _make_mem(SAME_TOPIC_TURNS)
    turns_seg, anchor = mem.get_topic_context("再按月份排序")
    assert anchor == "蓝牙耳机各地区销售额"      # 锚 = 话题首轮完整问题
    assert len(turns_seg) == 2                    # 两轮都被并入同话题
    assert turns_seg[0]["question"] == "蓝牙耳机各地区销售额"


def test_get_topic_context_new_topic_returns_empty_and_current_anchor():
    # 最近一轮是"退货率"，当前问"库存"——关键词无交集、无追问标记 → 全新话题
    mem = _make_mem([
        {"question": "蓝牙耳机销售额", "sql": "SELECT SUM(total_amount) FROM orders"},
        {"question": "退货率最高的产品", "sql": "SELECT product_name FROM products ORDER BY refund_rate DESC"},
    ])
    turns_seg, anchor = mem.get_topic_context("库存不足的产品有哪些")
    assert turns_seg == []                          # 不沿用旧话题
    assert anchor == "库存不足的产品有哪些"          # 锚 = 当前问题


def test_get_topic_context_bare_followup_links_to_newest_turn():
    # 追问"再按月份"无关键词，但 detect_incremental 命中 → 并入最近一轮，止于更早的不同话题
    mem = _make_mem([
        {"question": "退货率最高的产品", "sql": "SELECT product_name FROM products"},
        {"question": "蓝牙耳机各地区销售额", "sql": "SELECT region, SUM(total_amount) FROM orders"},
    ])
    turns_seg, anchor = mem.get_topic_context("再按月份排序")
    assert len(turns_seg) == 1                       # 只并入最近一轮"蓝牙耳机…"
    assert anchor == "蓝牙耳机各地区销售额"


def test_get_topic_context_max_turns_guard():
    turns = [{"question": f"第{i}轮销售额", "sql": "SELECT 1"} for i in range(5)]
    mem = _make_mem(turns)
    turns_seg, _ = mem.get_topic_context("再按月份", max_turns=2)
    assert len(turns_seg) == 2


def test_build_topic_context_formats_context_and_anchor():
    mem = _make_mem(SAME_TOPIC_TURNS)
    text, anchor = mem.build_topic_context("再按月份排序")
    assert "对话上下文" in text
    assert "Q1: 蓝牙耳机各地区销售额" in text
    assert "SQL1:" in text
    assert anchor == "蓝牙耳机各地区销售额"


def test_retrieve_schema_node_prepends_topic_anchor(monkeypatch):
    """同话题追问：检索 query 要带上话题锚（首轮完整问题），稳定命中同表"""
    from agent.workflow import retrieve_schema_node
    from agent.schema_retriever import SchemaRetriever

    mem = _make_mem(SAME_TOPIC_TURNS)
    monkeypatch.setattr(cm, "get_current_session", lambda: mem)
    captured = {}

    def fake_retrieve(self, q, **kw):
        captured["q"] = q
        return {"tables": [{"table": "orders", "doc": "orders表"}], "columns": []}

    monkeypatch.setattr(SchemaRetriever, "retrieve", fake_retrieve)
    state = {
        "question": "再按月份排序",
        "original_question": "再按月份排序",
        "clarified_question": "按月份分组排序",
        "retry_count": 0,
    }
    retrieve_schema_node(state)
    assert "蓝牙耳机各地区销售额" in captured["q"]  # 锚拼进检索 query


# ═══════════════════════════════════════════
# LLM topic_id：语义话题精确分组（正解）
# ═══════════════════════════════════════════

TOPIC_TURNS = [
    {"question": "蓝牙耳机各地区销售额", "sql": "SELECT region, SUM(total_amount) FROM orders GROUP BY region", "topic_id": "t-sales"},
    {"question": "按地区分组", "sql": "SELECT region, COUNT(*) FROM orders GROUP BY region", "topic_id": "t-sales"},
    {"question": "退货率最高的产品", "sql": "SELECT product_name FROM products ORDER BY refund_rate DESC", "topic_id": "t-refund"},
]


def test_get_topic_context_groups_by_llm_topic_id():
    mem = _make_mem(TOPIC_TURNS)
    # 当前延续销售话题 → 只取 t-sales 轮，锚=该话题最早问题
    seg, anchor = mem.get_topic_context("再按月份", topic_id="t-sales")
    assert anchor == "蓝牙耳机各地区销售额"
    assert len(seg) == 2
    assert all(t["topic_id"] == "t-sales" for t in seg)
    # 换话题 → 不吸收旧话题
    seg2, anchor2 = mem.get_topic_context("库存不足的产品有哪些", topic_id="t-stock")
    assert seg2 == []
    assert anchor2 == "库存不足的产品有哪些"


def test_get_topic_context_heuristic_fallback_without_topic_id():
    # 无 topic_id（规则快路径/旧轮次）→ 退回启发式
    mem = _make_mem([{"question": "蓝牙耳机各地区销售额", "sql": "SELECT region FROM orders", "topic_id": ""}])
    seg, anchor = mem.get_topic_context("再按月份")
    assert anchor == "蓝牙耳机各地区销售额"
    assert len(seg) == 1


def test_add_turn_persists_topic_id_roundtrip(monkeypatch, tmp_path):
    from agent import conversation_memory as cm_mod
    monkeypatch.setattr(cm_mod, "_table_created", False)
    monkeypatch.setattr(cm_mod, "DEMO_DB_PATH", str(tmp_path / "mem.db"))
    mem = cm_mod.ConversationMemory("s1")
    mem.add_turn("蓝牙耳机各地区销售额", "SELECT region FROM orders",
                 {"row_count": 3}, topic_id="t-sales")
    assert mem.get_last_topic_id() == "t-sales"
    # 重启后从 SQLite 读回 topic_id
    mem2 = cm_mod.ConversationMemory("s1")
    assert mem2.get_last_topic_id() == "t-sales"
    assert mem2.turns[0]["topic_id"] == "t-sales"


def test_retrieve_schema_node_uses_topic_id_anchor(monkeypatch):
    """state 带 LLM 的 topic_id → 按语义话题取锚，而非启发式"""
    from agent.workflow import retrieve_schema_node
    from agent.schema_retriever import SchemaRetriever
    mem = _make_mem(TOPIC_TURNS)
    monkeypatch.setattr(cm, "get_current_session", lambda: mem)
    captured = {}

    def fake_retrieve(self, q, **kw):
        captured["q"] = q
        return {"tables": [{"table": "orders", "doc": "orders表"}], "columns": []}

    monkeypatch.setattr(SchemaRetriever, "retrieve", fake_retrieve)
    state = {
        "question": "再按月份排序",
        "original_question": "再按月份排序",
        "clarified_question": "按月份分组排序",
        "topic_id": "t-sales",  # LLM 判定延续销售话题
        "retry_count": 0,
    }
    retrieve_schema_node(state)
    assert "蓝牙耳机各地区销售额" in captured["q"]   # 锚=销售话题最早问题
    assert "退货率最高的产品" not in captured["q"]   # 不吸收退货话题


def test_understand_returns_topic_id_and_passes_prev(monkeypatch):
    """understand() 输出 topic_id，并把上一轮 topic_id 传进 prompt"""
    from agent.query_clarifier import QueryClarifier
    calls = []

    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self):
            self.message = _Msg(json.dumps({
                "needs_clarification": False, "intent": "sales_aggregation",
                "clarified_question": "蓝牙耳机销售额按月份", "topic_id": "t-x",
            }))

    class _Resp:
        choices = [_Choice()]

        class _usage:
            total_tokens = 10
        usage = _usage

    class _Completions:
        def create(self, **kw):
            calls.append(kw.get("messages"))
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    qc = QueryClarifier.__new__(QueryClarifier)
    qc.client = _Client()
    result = asyncio.run(qc.understand("再按月份", {}, prev_topic_id="t-sales"))
    assert result["topic_id"] == "t-x"
    user_content = calls[0][1]["content"]
    assert "t-sales" in user_content  # 上一轮 topic_id 进了 prompt
