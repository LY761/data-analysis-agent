import json
from unittest.mock import patch
from agent.market_intelligence.selection import analyze_selection

class _Msg:
    def __init__(self, content): self.content = content
class _Choice:
    def __init__(self, content): self.message = _Msg(content)
class _Resp:
    def __init__(self, content): self.choices = [_Choice(content)]
class _Completions:
    def __init__(self, owner): self._owner = owner
    def create(self, **kw):
        self._owner.calls.append(kw.get("messages"))
        return _Resp(self._owner._r.pop(0))
class _Chat:
    def __init__(self, owner): self.completions = _Completions(owner)
class FakeLLM:
    """模拟 OpenAI client：fake.chat.completions.create(...)"""
    def __init__(self, responses):
        self._r = list(responses)
        self.calls = []
        self.chat = _Chat(self)

def test_analyze_selection_full():
    fake = FakeLLM([
        "价格分布：中端20-40美元空白较大…",
        json.dumps({"score": 72, "verdict": "推荐", "price_band": "$25-45",
                    "competition": "中", "risks": ["红海"], "differentiation": "做长续航",
                    "reasoning": "中端有空白"}),
    ])
    with patch("agent.market_intelligence.selection.search_products",
               return_value=[{"title": "Earbuds A", "url": "https://amzn/dp/A", "snippet": ""}]):
        with patch("agent.market_intelligence.selection.scrape_product",
                   return_value={"title": "Earbuds A", "price": 29.99, "rating": 4.3, "review_count": 100, "url": "https://amzn/dp/A"}):
            with patch("db.executor.executor.execute",
                       return_value={"data": [], "columns": [], "row_count": 0}):
                result = analyze_selection("蓝牙耳机", llm_client=fake)
    assert result["recommendation"]["score"] == 72
    assert result["profile"]
    assert len(result["products"]) == 1
    assert len(fake.calls) == 2  # profile + recommendation 两次 LLM


def test_analyze_selection_with_fenced_json():
    """LLM 返回带 ```json 围栏的 JSON 时也能正确解析 recommendation"""
    fake = FakeLLM([
        "价格分布：中端20-40美元空白较大…",
        "```json\n" + json.dumps({"score": 88, "verdict": "推荐", "price_band": "$20-40",
                    "competition": "中", "risks": [], "differentiation": "做低噪音",
                    "reasoning": "围栏JSON测试"}) + "\n```",
    ])
    with patch("agent.market_intelligence.selection.search_products",
               return_value=[{"title": "Earbuds A", "url": "https://amzn/dp/A", "snippet": ""}]):
        with patch("agent.market_intelligence.selection.scrape_product",
                   return_value={"title": "Earbuds A", "price": 29.99, "rating": 4.3, "review_count": 100, "url": "https://amzn/dp/A"}):
            with patch("db.executor.executor.execute",
                       return_value={"data": [], "columns": [], "row_count": 0}):
                result = analyze_selection("蓝牙耳机", llm_client=fake)
    assert result["recommendation"]["score"] == 88
    assert result["error"] is None


def test_compare_internal_data_none_returns_empty_list():
    """executor.execute 出错（data=None）时 _compare_internal 返回 []，不违反 list 契约"""
    from agent.market_intelligence.selection import _compare_internal
    with patch("db.executor.executor.execute",
               return_value={"data": None, "columns": [], "row_count": 0}):
        assert _compare_internal("蓝牙耳机") == []
