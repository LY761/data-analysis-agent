# 市场情报模块 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有数据分析 Agent 上新增选品分析和商品研究两条流水线，复用内部 SQL 对比 + LLM 推理，前端加市场情报 Tab。

**Architecture:** 新建 `agent/market_intelligence/` 包（search→scrape→LLM分析→内部对比），`agent_router` 加 `market_intelligence` 模式分发，`api/routes.py` 加 3 个端点（2 个 HTTP + 1 个 SSE 流式）。

**Tech Stack:** Python 3.12 · FastAPI · httpx · BeautifulSoup4 · OpenAI 兼容客户端(DeepSeek) · pytest

## Global Constraints

- 项目根：`E:/projects/data-analysis-agent`，后端在 `backend/`，venv 为 `.venv/Scripts/python.exe`
- 所有新模块代码放在 `backend/agent/market_intelligence/`，路由加在 `backend/api/routes.py`
- LLM 调用统一用现有 `config.py` 的 `LLM_API_KEY / LLM_BASE_URL / LLM_MODEL`（DeepSeek）
- 内部数据对比一律走 `backend/db/executor.py` 的 `executor.execute(sql)`
- **共享爬虫**：新建 `backend/agent/crawler.py`（httpx + BS4 自实现，不依赖 competitor-scraper 的 Scrapling，因其在独立 venv）。**竞品分析与选品模块共用**这个爬虫——market_intelligence 直接用，competitor_analysis 可在后续接入（现仍读预抓取文件，不破坏现有功能）
- 所有网络/LLM 测试用 mock，禁止真实网络请求进单元测试
- 测试在 `backend/tests/`，pytest 从 `backend/` 目录运行（cwd=backend，sys.path 含 backend）

---

### Task 1: 测试基础设施 + 模块骨架

**Files:**
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_smoke.py`
- Create: `backend/agent/market_intelligence/__init__.py`
- Modify: `backend/requirements.txt`（若存在）

**Interfaces:**
- Produces: pytest 可运行；`market_intelligence` 包可 `import`

- [ ] **Step 1: 安装 pytest**

```bash
cd E:/projects/data-analysis-agent/backend
E:/projects/data-analysis-agent/.venv/Scripts/python.exe -m pip install pytest
```

- [ ] **Step 2: 写 conftest（保证 tests 能 import 到 backend 包）**

```python
# backend/tests/conftest.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

- [ ] **Step 3: 写冒烟测试（先失败）**

```python
# backend/tests/test_smoke.py
from agent.market_intelligence import __version__

def test_module_imports():
    assert isinstance(__version__, str)
```

- [ ] **Step 4: 运行确认失败**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_smoke.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.market_intelligence'`

- [ ] **Step 5: 创建包 + `__init__.py`**

```python
# backend/agent/market_intelligence/__init__.py
"""市场情报模块 — 选品分析 + 商品研究"""
__version__ = "0.1.0"
```

- [ ] **Step 6: 运行确认通过**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_smoke.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/tests/ backend/agent/market_intelligence/
git commit -m "chore: 建立 pytest 基础设施和市场情报模块骨架"
```

---

### Task 2: 搜索发现 search.py（DDG → 产品URL列表）

**Files:**
- Create: `backend/agent/market_intelligence/search.py`
- Test: `backend/tests/test_search.py`

**Interfaces:**
- Consumes: 无（纯 httpx + bs4）
- Produces: `search_products(query: str, limit: int = 15) -> list[dict]`，每项 `{"title": str, "url": str, "snippet": str}`

- [ ] **Step 1: 写失败测试（mock 网络）**

```python
# backend/tests/test_search.py
from unittest.mock import patch, MagicMock
from agent.market_intelligence.search import search_products

FAKE_HTML = """
<html><body>
<a class="result__a" href="https://www.amazon.com/dp/B0ABC123">Bluetooth Earbuds Pro</a>
<a class="result__a" href="https://www.amazon.com/dp/B0XYZ999">Wireless Earbuds Mini</a>
<div class="result__snippet">Best seller bluetooth earbuds 2026</div>
</body></html>
"""

def test_search_parses_results():
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = FAKE_HTML
    with patch("httpx.Client.get", return_value=fake_resp) as mock_get:
        results = search_products("bluetooth earbuds")
    assert len(results) == 2
    assert results[0]["title"] == "Bluetooth Earbuds Pro"
    assert "B0ABC123" in results[0]["url"]
    assert mock_get.call_args[0][0].startswith("https://html.duckduckgo.com")
```

- [ ] **Step 2: 运行确认失败**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_search.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现 search.py**

```python
# backend/agent/market_intelligence/search.py
"""DDG HTML 搜索 → 产品 URL 列表。零 API key，纯免费。"""
import httpx
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

def search_products(query: str, limit: int = 15) -> list[dict]:
    """搜索电商产品，返回 [{title, url, snippet}]"""
    url = "https://html.duckduckgo.com/html/"
    params = {"q": f"{query} bestseller amazon", "kl": "us-en"}
    with httpx.Client(headers=HEADERS, timeout=15.0, follow_redirects=True) as client:
        resp = client.get(url, params=params)
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for link in soup.select("a.result__a")[:limit]:
        href = link.get("href", "")
        title = link.get_text(strip=True)
        results.append({"title": title, "url": href, "snippet": ""})
    # 补 snippet
    for i, res in enumerate(soup.select("div.result__snippet")[:limit]):
        if i < len(results):
            results[i]["snippet"] = res.get_text(strip=True)
    return results
```

- [ ] **Step 4: 运行确认通过**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_search.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/market_intelligence/search.py backend/tests/test_search.py
git commit -m "feat: DDG 搜索发现产品 URL"
```

---

### Task 3: 共享爬虫 crawler.py（Amazon 产品页 → 结构化）

> 供 market_intelligence 和（后续）competitor_analysis 共用。放在 `agent/crawler.py`，任何子模块都能 `from agent.crawler import scrape_product`。

**Files:**
- Create: `backend/agent/crawler.py`
- Test: `backend/tests/test_crawler.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `scrape_product(url: str) -> dict`，字段：`{"title","price","rating","review_count","url"}`
  - `extract_amazon_product(url: str, html: str) -> dict`（纯解析，供测试/复用）

- [ ] **Step 1: 写失败测试（纯解析，不用网络）**

```python
# backend/tests/test_crawler.py
from agent.crawler import extract_amazon_product

FAKE_PAGE = """
<html><body>
<span id="productTitle">Anker Soundcore P40i True Wireless</span>
<span class="a-price-whole">49<sup>99</sup></span>
<span class="a-icon-alt">4.5 out of 5 stars</span>
<span id="acrCustomerReviewText">2,341 ratings</span>
</body></html>
"""

def test_extract_amazon_product():
    p = extract_amazon_product("https://www.amazon.com/dp/B0XYZ", FAKE_PAGE)
    assert p["title"] == "Anker Soundcore P40i True Wireless"
    assert p["price"] == 49.99
    assert p["rating"] == 4.5
    assert p["review_count"] == 2341
```

- [ ] **Step 2: 运行确认失败**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_crawler.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现 crawler.py**

```python
# backend/agent/crawler.py
"""共享爬虫：抓取 Amazon 产品页 → 结构化字段。
httpx+BS4 自实现，供 market_intelligence 与 competitor_analysis 共用。
依赖: httpx, bs4（Agent venv 已有）"""
import re
import httpx
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def extract_amazon_product(url: str, html: str) -> dict:
    """从 Amazon 产品页 HTML 提取结构化字段（纯解析，可单测）"""
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    t = soup.select_one("#productTitle")
    if t:
        title = t.get_text(strip=True)

    price = None
    p = soup.select_one("span.a-price-whole")
    if p:
        raw = p.get_text(strip=True).replace(",", "")
        m = re.search(r"\d+\.?\d*", raw)
        if m:
            price = float(m.group(0))
    # 处理价格小数
    frac = soup.select_one("span.a-price-fraction")
    if price and frac:
        price += float("0." + frac.get_text(strip=True))

    rating = None
    r = soup.select_one("span.a-icon-alt")
    if r:
        m = re.search(r"([\d.]+)", r.get_text())
        if m:
            rating = float(m.group(1))

    reviews = None
    rc = soup.select_one("#acrCustomerReviewText")
    if rc:
        m = re.search(r"([\d,]+)", rc.get_text())
        if m:
            reviews = int(m.group(1).replace(",", ""))

    return {
        "title": title,
        "price": price,
        "rating": rating,
        "review_count": reviews,
        "url": url,
    }


def scrape_product(url: str) -> dict:
    """抓取单个产品页。失败返回空字段 dict，不抛异常。"""
    try:
        with httpx.Client(headers=HEADERS, timeout=20.0, follow_redirects=True) as client:
            resp = client.get(url)
        if resp.status_code == 200:
            return extract_amazon_product(url, resp.text)
    except Exception:
        pass
    return {"title": "", "price": None, "rating": None, "review_count": None, "url": url}
```

- [ ] **Step 4: 运行确认通过**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_crawler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/crawler.py backend/tests/test_crawler.py
git commit -m "feat: 共享爬虫（Amazon 产品页抓取解析）"
```

---

### Task 4: LLM Prompt 模板 prompts.py

**Files:**
- Create: `backend/agent/market_intelligence/prompts.py`
- Test: `backend/tests/test_prompts.py`

**Interfaces:**
- Consumes: 无
- Produces: 常量字符串模板：`PROFILE_PROMPT`、`SELECTION_PROMPT`、`SELLPOINTS_PROMPT`、`REVIEW_PAIN_PROMPT`、`SUGGEST_PROMPT`，每个含 `{...}` 占位符

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_prompts.py
from agent.market_intelligence.prompts import PROFILE_PROMPT, SELECTION_PROMPT

def test_profile_prompt_renders():
    text = PROFILE_PROMPT.format(products="[{...}]")
    assert "价格分布" in text

def test_selection_prompt_has_output_shape():
    text = SELECTION_PROMPT.format(category="蓝牙耳机", profile="...", internal="...")
    assert "机会评分" in text
    assert "建议价格带" in text
```

- [ ] **Step 2: 运行确认失败**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_prompts.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现 prompts.py**

```python
# backend/agent/market_intelligence/prompts.py
"""市场情报各环节的 LLM prompt 模板"""

PROFILE_PROMPT = """你是电商选品分析师。根据下列抓取到的产品数据，分析品类画像。

产品数据（JSON）：
{products}

请输出中文分析，覆盖：
1. 价格分布带（低/中/高端价位区间、空白带在哪里）
2. 品牌集中度（头部品牌占多大份额，判断 CR5 高低）
3. 评论质量（平均评分、差评集中在哪些价位段）
4. 新品机会（有没有近期新品卖得不错）
只输出分析，不要 markdown 表格。"""

SELECTION_PROMPT = """你是跨境电商选品顾问。综合品类画像和内部数据，给出选品建议。

品类：{category}
品类画像：
{profile}

内部数据库已有产品（JSON，可能为空）：
{internal}

请输出 JSON（严格格式，不要其他内容）：
{{
  "score": 0-100的整数,
  "verdict": "推荐/谨慎/不推荐一句话",
  "price_band": "建议价格带，如$25-45",
  "competition": "竞争强度：低/中/高 + 一句依据",
  "risks": ["风险1", "风险2"],
  "differentiation": "差异化方向一句话",
  "reasoning": "2-3句综合理由"
}}"""

SELLPOINTS_PROMPT = """你是商品文案专家。根据产品信息提取核心卖点。

产品标题：{title}
产品描述：
{description}

请输出：
1. 核心卖点（最多5条，每条一句话）
2. 目标用户画像
3. 差异化角度（和常见同类品比，它主打什么）"""

REVIEW_PAIN_PROMPT = """你是用户反馈分析师。根据下面的评论，找出最集中的痛点。

评论列表：
{reviews}

请输出中文：
1. Top3 痛点（每个痛点 + 出现频次感觉 + 一句用户原话佐证）
2. 高频关键词（5-8个）"""

SUGGEST_PROMPT = """你是产品经理。基于竞品卖点和用户痛点，给出我们的改进建议。

竞品卖点：
{sellpoints}

用户痛点：
{pains}

内部相似产品（JSON，可能为空）：
{internal}

请输出：
1. 我们能做得更好的 2-3 个点
2. 建议的定价策略
3. 一句话总结机会"""
```

- [ ] **Step 4: 运行确认通过**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_prompts.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/market_intelligence/prompts.py backend/tests/test_prompts.py
git commit -m "feat: 选品/商品研究 LLM prompt 模板"
```

---

### Task 5: 选品流水线 selection.py

**Files:**
- Create: `backend/agent/market_intelligence/selection.py`
- Test: `backend/tests/test_selection.py`

**Interfaces:**
- Consumes: `search_products(query, limit)`、`scrape_product(url)`、`prompts.*`、`executor.execute(sql)`（来自 `db.executor`）
- Produces: `analyze_selection(category: str, llm_client=None, stream_cb=None) -> dict`，返回：
  ```python
  {"category", "products": [...], "profile": str, "internal": [...],
   "recommendation": {"score","verdict","price_band","competition","risks","differentiation","reasoning"},
   "error": str|None}
  ```
  内部 helper `_call_llm(messages, client) -> str`（被测试 mock）

- [ ] **Step 1: 写失败测试（mock 搜索/抓取/LLM/SQL）**

```python
# backend/tests/test_selection.py
import json
from unittest.mock import patch, MagicMock
from agent.market_intelligence.selection import analyze_selection

class _Msg:  # 模拟 message.content
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
```

- [ ] **Step 2: 运行确认失败**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_selection.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现 selection.py**

```python
# backend/agent/market_intelligence/selection.py
"""选品流水线：搜索 → 抓取 → 品类画像 → 内部对比 → 选品报告"""
import json
import logging
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from agent.market_intelligence.search import search_products
from agent.crawler import scrape_product
from agent.market_intelligence import prompts
from db.executor import executor

logger = logging.getLogger(__name__)


def _call_llm(client, messages) -> str:
    """调用 LLM，返回文本。client 兼容 openai 的 chat.completions.create。"""
    resp = client.chat.completions.create(
        model=LLM_MODEL, messages=messages, temperature=0.3, max_tokens=600
    )
    return resp.choices[0].message.content.strip()


def _default_client():
    return OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


def _compare_internal(category: str) -> list:
    """查内部数据库是否有相似产品（按品类关键词模糊匹配）"""
    try:
        r = executor.execute(
            f"SELECT product_name, category, unit_price FROM products "
            f"WHERE is_active=1 AND (category LIKE '%{category[:6]}%' "
            f"OR product_name LIKE '%{category[:6]}%') LIMIT 5"
        )
        return r.get("data", [])
    except Exception:
        return []


def analyze_selection(category: str, llm_client=None, stream_cb=None) -> dict:
    """选品分析主入口"""
    def progress(msg):
        if stream_cb:
            try: stream_cb(msg)
            except Exception: pass

    client = llm_client or _default_client()
    try:
        progress(f"正在搜索「{category}」相关产品...")
        products = search_products(category, limit=12)

        progress(f"找到 {len(products)} 个产品，正在抓取详情...")
        scraped = [scrape_product(p["url"]) for p in products]
        scraped = [s for s in scraped if s.get("title")]

        progress("正在分析价格分布和竞争格局...")
        profile = _call_llm(client, [
            {"role": "system", "content": "你是电商选品分析师。"},
            {"role": "user", "content": prompts.PROFILE_PROMPT.format(
                products=json.dumps(scraped[:15], ensure_ascii=False))},
        ])

        progress("正在对比内部数据...")
        internal = _compare_internal(category)

        progress("正在生成选品建议...")
        rec_raw = _call_llm(client, [
            {"role": "system", "content": "你是跨境电商选品顾问。只输出JSON。"},
            {"role": "user", "content": prompts.SELECTION_PROMPT.format(
                category=category, profile=profile,
                internal=json.dumps(internal, ensure_ascii=False))},
        ])
        recommendation = json.loads(rec_raw)

        return {
            "category": category,
            "products": scraped,
            "profile": profile,
            "internal": internal,
            "recommendation": recommendation,
            "error": None,
        }
    except Exception as e:
        logger.warning(f"[Selection] 分析失败: {e}")
        return {"category": category, "products": [], "profile": "",
                "internal": [], "recommendation": {}, "error": str(e)}
```

- [ ] **Step 4: 运行确认通过**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_selection.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/market_intelligence/selection.py backend/tests/test_selection.py
git commit -m "feat: 选品流水线"
```

---

### Task 6: 商品研究流水线 product_analyzer.py

**Files:**
- Create: `backend/agent/market_intelligence/product_analyzer.py`
- Test: `backend/tests/test_product_analyzer.py`

**Interfaces:**
- Consumes: `search_products`、`scrape_product`、`prompts.*`、`executor.execute`
- Produces: `analyze_product(query: str, llm_client=None, stream_cb=None) -> dict`，返回：
  ```python
  {"query", "product": dict, "sellpoints": str, "pains": str,
   "internal": [...], "suggestions": str, "error": str|None}
  ```
  内部复用 `selection._call_llm`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_product_analyzer.py
from unittest.mock import patch
from agent.market_intelligence.product_analyzer import analyze_product
from agent.market_intelligence.selection import _call_llm

def test_analyze_product(monkeypatch):
    calls = []
    def fake_llm(client, messages):
        calls.append(messages)
        n = len(calls)
        if n == 1:
            return "核心卖点：主动降噪…目标用户：通勤族"
        if n == 2:
            return "痛点1：续航短…高频词：续航、降噪"
        return "建议做长续航版本…定价$30-40"

    monkeypatch.setattr("agent.market_intelligence.product_analyzer._call_llm", fake_llm)
    with patch("agent.market_intelligence.product_analyzer.search_products",
               return_value=[{"title": "P40i", "url": "https://amzn/dp/B0", "snippet": ""}]):
        with patch("agent.market_intelligence.product_analyzer.scrape_product",
                   return_value={"title": "P40i", "price": 49.99, "rating": 4.5,
                                 "review_count": 100, "url": "https://amzn/dp/B0"}):
            with patch("db.executor.executor.execute",
                       return_value={"data": [], "columns": [], "row_count": 0}):
                r = analyze_product("Anker P40i")
    assert "卖点" in r["sellpoints"]
    assert "痛点" in r["pains"]
    assert "建议" in r["suggestions"]
    assert len(calls) == 3
```

- [ ] **Step 2: 运行确认失败**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_product_analyzer.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现 product_analyzer.py**

```python
# backend/agent/market_intelligence/product_analyzer.py
"""商品研究流水线：定位 → 抓取 → 卖点 → 评论洞察 → 内部对比 → 建议"""
import json
import logging
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from agent.market_intelligence.search import search_products
from agent.crawler import scrape_product
from agent.market_intelligence import prompts
from agent.market_intelligence.selection import _call_llm, _compare_internal

logger = logging.getLogger(__name__)


def _default_client():
    return OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


def _looks_like_url(text: str) -> bool:
    return text.startswith("http")


def analyze_product(query: str, llm_client=None, stream_cb=None) -> dict:
    """商品深度研究主入口"""
    def progress(msg):
        if stream_cb:
            try: stream_cb(msg)
            except Exception: pass

    client = llm_client or _default_client()
    try:
        # 定位：有 URL 直接用，否则搜索
        if _looks_like_url(query):
            product = scrape_product(query)
        else:
            progress(f"正在搜索「{query}」...")
            found = search_products(query, limit=3)
            if not found:
                return {"query": query, "product": {}, "sellpoints": "", "pains": "",
                        "internal": [], "suggestions": "", "error": "未找到该产品"}
            product = scrape_product(found[0]["url"])

        if not product.get("title"):
            return {"query": query, "product": product, "sellpoints": "", "pains": "",
                    "internal": [], "suggestions": "", "error": "抓取失败"}

        progress(f"正在提取「{product['title'][:30]}」的卖点...")
        sellpoints = _call_llm(client, [
            {"role": "system", "content": "你是商品文案专家。"},
            {"role": "user", "content": prompts.SELLPOINTS_PROMPT.format(
                title=product["title"], description=json.dumps(product, ensure_ascii=False))},
        ])

        progress("正在分析评论痛点...")
        pains = _call_llm(client, [
            {"role": "system", "content": "你是用户反馈分析师。"},
            {"role": "user", "content": prompts.REVIEW_PAIN_PROMPT.format(
                reviews=json.dumps([{"title": product["title"], **product}], ensure_ascii=False))},
        ])

        progress("正在对比内部产品并给建议...")
        internal = _compare_internal(product["title"][:6])
        suggestions = _call_llm(client, [
            {"role": "system", "content": "你是产品经理。"},
            {"role": "user", "content": prompts.SUGGEST_PROMPT.format(
                sellpoints=sellpoints, pains=pains,
                internal=json.dumps(internal, ensure_ascii=False))},
        ])

        return {"query": query, "product": product, "sellpoints": sellpoints,
                "pains": pains, "internal": internal, "suggestions": suggestions,
                "error": None}
    except Exception as e:
        logger.warning(f"[ProductAnalyzer] 分析失败: {e}")
        return {"query": query, "product": {}, "sellpoints": "", "pains": "",
                "internal": [], "suggestions": "", "error": str(e)}
```

- [ ] **Step 4: 运行确认通过**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_product_analyzer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/market_intelligence/product_analyzer.py backend/tests/test_product_analyzer.py
git commit -m "feat: 商品研究流水线"
```

---

### Task 7: AgentRouter 分发 market_intelligence

**Files:**
- Modify: `backend/agent/agent_router.py`
- Test: `backend/tests/test_router_market.py`

**Interfaces:**
- Consumes: 现有 `route()` 结构
- Produces: `route()` 返回 `{"mode": "market_intelligence", "sub": "selection"|"product", "query": str, "reason": str}`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_router_market.py
from agent.agent_router import agent_router

def test_selection_keyword_routes():
    r = agent_router.route("分析一下蓝牙耳机的选品机会")
    assert r["mode"] == "market_intelligence"
    assert r["sub"] == "selection"

def test_product_keyword_routes():
    r = agent_router.route("研究一下 Anker Soundcore P40i")
    assert r["mode"] == "market_intelligence"
    assert r["sub"] == "product"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_router_market.py -v`
Expected: FAIL — assertion error（mode 不是 market_intelligence）

- [ ] **Step 3: 实现路由规则**

在 `agent_router.py` 的类属性区新增：

```python
    # 市场情报关键词：选品/商品研究
    MARKET_INTEL_KEYWORDS = ["选品", "市场机会", "能不能做", "值得卖吗",
                             "研究一下", "分析一下这个产品", "竞品分析", "差评", "痛点"]
    SELECTION_KEYWORDS = ["选品", "市场机会", "能不能做", "值得卖吗", "竞争怎么样"]
```

在 `route()` 里、`SQL_QUERY_KEYWORDS` 判断之前插入：

```python
        # 3.5 市场情报（选品/商品研究）
        if any(kw in msg for kw in self.MARKET_INTEL_KEYWORDS):
            sub = "selection" if any(kw in msg for kw in self.SELECTION_KEYWORDS) else "product"
            r = {"mode": "market_intelligence", "sub": sub, "query": msg, "reason": "市场情报关键词"}
            self.cache[msg] = r
            return r
```

> ⚠️ 注意：`"竞品分析"` 已存在于 `COMPETITOR_KEYWORDS`，它在路由第 1 步优先命中，会先走到 competitor。若要让"竞品分析+选品"走到 market_intelligence，把 `"竞品分析"` 从 `MARKET_INTEL_KEYWORDS` 移除（第 1 步的 competitor 会拦截）。测试里用不含"竞品分析"的输入即可。

- [ ] **Step 4: 运行确认通过**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_router_market.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/agent_router.py backend/tests/test_router_market.py
git commit -m "feat: AgentRouter 分发市场情报模式"
```

---

### Task 8: API 路由 + SSE 流式端点

**Files:**
- Modify: `backend/api/routes.py`
- Test: `backend/tests/test_market_api.py`

**Interfaces:**
- Consumes: `analyze_selection(category, stream_cb)`、`analyze_product(query, stream_cb)`、`agent_router.route()`
- Produces:
  - `POST /api/market/selection` body `{"category": str}` → `{category, products, profile, internal, recommendation, error}`
  - `POST /api/market/product` body `{"query": str}` → `{query, product, sellpoints, pains, internal, suggestions, error}`
  - `POST /api/market/stream` body `{"query": str}` → SSE `status`/`answer`/`result`/`done`

- [ ] **Step 1: 写失败测试（TestClient）**

```python
# backend/tests/test_market_api.py
from unittest.mock import patch
from fastapi.testclient import TestClient
import main  # 触发 app 构建

client = TestClient(main.app)

def test_market_selection_endpoint():
    fake_result = {"category": "蓝牙耳机", "products": [], "profile": "分析…",
                   "internal": [], "recommendation": {"score": 70, "verdict": "推荐"},
                   "error": None}
    with patch("api.routes.analyze_selection", return_value=fake_result) as m:
        r = client.post("/api/market/selection", json={"category": "蓝牙耳机"})
    assert r.status_code == 200
    assert r.json()["recommendation"]["score"] == 70
    m.assert_called_once_with("蓝牙耳机", stream_cb=None)

def test_market_product_endpoint():
    fake_result = {"query": "P40i", "product": {}, "sellpoints": "卖点", "pains": "痛点",
                   "internal": [], "suggestions": "建议", "error": None}
    with patch("api.routes.analyze_product", return_value=fake_result) as m:
        r = client.post("/api/market/product", json={"query": "Anker P40i"})
    assert r.status_code == 200
    assert r.json()["sellpoints"] == "卖点"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_market_api.py -v`
Expected: FAIL — 404（路由不存在）

- [ ] **Step 3: 实现路由**

在 `routes.py` 末尾新增：

```python
# ================================================================
# 市场情报
# ================================================================

class MarketSelectionRequest(BaseModel):
    category: str

class MarketProductRequest(BaseModel):
    query: str


@router.post("/market/selection")
async def market_selection(request: MarketSelectionRequest):
    from agent.market_intelligence.selection import analyze_selection
    result = analyze_selection(request.category)
    return result


@router.post("/market/product")
async def market_product(request: MarketProductRequest):
    from agent.market_intelligence.product_analyzer import analyze_product
    result = analyze_product(request.query)
    return result


@router.post("/market/stream")
async def market_stream(request: MarketProductRequest):
    """SSE 流式市场情报分析。按 query 自动判断 selection / product。"""
    import json as _json
    from fastapi.responses import StreamingResponse
    from agent.agent_router import agent_router

    async def event_gen():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def sse(event, data):
            return f"event: {event}\ndata: {_json.dumps(data, ensure_ascii=False)}\n\n"

        def stream_cb(text):
            loop.call_soon_threadsafe(lambda: queue.put_nowait(text))

        async def forward():
            while not queue.empty():
                yield sse("status", {"message": queue.get_nowait()})

        route = await asyncio.to_thread(agent_router.route, request.query)
        mode = route.get("mode")
        sub = route.get("sub", "selection")
        query = route.get("query", request.query)

        if mode != "market_intelligence":
            yield sse("status", {"message": "未识别为市场情报请求"})
            yield sse("done", {})
            return

        task = asyncio.create_task(_run_market(sub, query, stream_cb))

        while True:
            async for ev in forward():
                yield ev
            if task.done():
                break
            await asyncio.sleep(0.03)

        async for ev in forward():
            yield ev

        try:
            result = task.result()
        except Exception as e:
            yield sse("error", {"message": str(e)})
            yield sse("done", {})
            return

        yield sse("result", result)
        yield sse("done", {})

    return StreamingResponse(event_gen(), media_type="text/event-stream")


async def _run_market(sub: str, query: str, stream_cb):
    """后台任务：跑选品或商品研究（阻塞调用丢线程池）"""
    import asyncio
    if sub == "selection":
        from agent.market_intelligence.selection import analyze_selection
        return await asyncio.to_thread(analyze_selection, query, None, stream_cb)
    from agent.market_intelligence.product_analyzer import analyze_product
    return await asyncio.to_thread(analyze_product, query, None, stream_cb)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd E:/projects/data-analysis-agent/backend && .venv/Scripts/python.exe -m pytest tests/test_market_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/api/routes.py backend/tests/test_market_api.py
git commit -m "feat: 市场情报 API + SSE 流式端点"
```

---

### Task 9: 前端市场情报 Tab + 流式渲染

**Files:**
- Modify: `frontend/index.html`

**Interfaces:**
- Consumes: `POST /api/market/stream`、`POST /api/market/selection`、`POST /api/market/product`
- Produces: 侧边栏 🛒 市场情报 Tab（选品/商品研究两个卡片入口）；主输入框自动走 `market_intelligence` 流式

- [ ] **Step 1: 侧边栏加 Tab**

在 `index.html` 侧边栏的快捷 Tab 列表附近，找到现有的 quickTab 结构，复制一份改为 `marketTab`：

```html
<!-- 示例：在现有 tab 按钮区加入 -->
<button class="tab-btn" id="marketTab" onclick="toggleMarketPanel()">🛒 市场情报</button>
```

- [ ] **Step 2: 加面板 + 卡片**

```html
<div id="marketPanel" style="display:none">
  <button onclick="runMarketSelection()">🔍 选品分析</button>
  <button onclick="runMarketProduct()">📦 商品研究</button>
</div>
```

- [ ] **Step 3: 加 JS 函数**

```javascript
function toggleMarketPanel() {
  const p = document.getElementById('marketPanel');
  p.style.display = p.style.display === 'none' ? 'block' : 'none';
}

function runMarketSelection() {
  const q = prompt('输入品类名，例如：蓝牙耳机');
  if (q) sendMessage('分析一下' + q + '的选品机会');
}

function runMarketProduct() {
  const q = prompt('输入产品名或 Amazon 链接');
  if (q) sendMessage('研究一下 ' + q);
}
```

> 复用现有 `sendMessage`（已支持 SSE `/api/query/stream`）。但市场情报走的是 `/api/market/stream`。修改 `sendMessage` 使其在 query 含市场情报关键词时请求 `/api/market/stream`：
> 在 `sendMessage` 的 `fetch` URL 处，用关键词判断：
> ```javascript
> const isMarket = /选品|市场机会|研究一下|能不能做|值得卖吗|差评|痛点/.test(query);
> const url = isMarket ? '/api/market/stream' : '/api/query/stream';
> ```

- [ ] **Step 4: 结果渲染**

`formatResponse` 已有 `chat_reply`/`nl_answer`/`data`/`chart` 分支。市场情报 `result` 是自定义结构，加一个兜底：当 `data.recommendation` 存在时，渲染为可读文本 + 数据表：

```javascript
  // 市场情报结果
  if (data.recommendation) {
    const rec = data.recommendation;
    html += '<div class="bubble">'
      + '<h3>📊 ' + (data.category || '') + ' 选品机会</h3>'
      + '<p>评分：' + (rec.score ?? '-') + '/100 · ' + (rec.verdict || '') + '</p>'
      + '<p>建议价格带：' + (rec.price_band || '-') + '</p>'
      + '<p>竞争强度：' + (rec.competition || '-') + '</p>'
      + '<p>' + (rec.reasoning || data.profile || '') + '</p>'
      + '</div>';
    if (data.products && data.products.length) {
      html += '<button class="toggle-btn" onclick="toggleSection(\'mk_data\')">📄 产品数据 (' + data.products.length + '个)</button>';
      html += '<div id="mk_data" class="toggle-section"><table>'
        + '<tr><th>产品</th><th>价格</th><th>评分</th><th>评论</th></tr>'
        + data.products.map(p => '<tr><td>' + (p.title||'').slice(0,30) + '</td><td>' + (p.price ?? '-') + '</td><td>' + (p.rating ?? '-') + '</td><td>' + (p.review_count ?? '-') + '</td></tr>').join('')
        + '</table></div>';
    }
    return html;
  }
```

- [ ] **Step 5: 手工验证**

启动服务后浏览器打开 `http://localhost:8001`，在侧边栏点 🛒 市场情报 → 🔍 选品分析 → 输入"蓝牙耳机"，确认出现流式进度和选品卡片。

- [ ] **Step 6: Commit**

```bash
git add frontend/index.html
git commit -m "feat: 前端市场情报 Tab + 流式渲染"
```

---

## 自检结果

**Spec 覆盖：**
- 选品流水线（Task 5）✅
- 商品研究流水线（Task 6）✅
- 路由分发（Task 7）✅
- 3 个接口 + SSE（Task 8）✅
- 前端 Tab + 卡片 + 流式（Task 9）✅
- 复用内部 SQL（Task 5/6 的 `_compare_internal`）✅

**已知偏离：** 按用户要求改为**共享爬虫** `agent/crawler.py`（httpx+BS4 自实现，不 import competitor-scraper），竞品分析与选品共用。competitor_analysis 现仍读预抓取文件，接入共享爬虫为后续任务。

**已知注意事项：** `"竞品分析"` 会被现有 `COMPETITOR_KEYWORDS` 先拦截，市场情报路由需避开该词；Task 7 已说明。
