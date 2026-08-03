# 市场情报模块 — 设计文档

> 2026-08-03 · 选品分析 + 商品研究 MVP

## 1. 概述

在现有数据分析 Agent 基础上，新增**市场情报模块**，覆盖跨境电商选品和商品研究两个核心场景。复用 `competitor-scraper` 的爬虫引擎和分析能力，新增搜索发现和 LLM 选品推理。

## 2. 架构

```
frontend/index.html（新增 🛒 市场情报 Tab）
        │
        ▼  POST /api/market/selection | /product | /stream
┌───────────────────────────────────────┐
│  api/routes.py — market 路由组        │
└───────────────┬───────────────────────┘
                │
    ┌───────────▼───────────────────────┐
    │  agent/market_intelligence/       │  新建
    │  ├── __init__.py                  │
    │  ├── search.py                    │  DDG 搜索 → 产品 URL 列表
    │  ├── scraper.py                   │  封装 competitor-scraper 调用
    │  ├── selection.py                 │  选品流水线
    │  ├── product_analyzer.py          │  商品研究流水线
    │  └── prompts.py                   │  各环节 prompt 模板
    └───────────┬───────────────────────┘
                │
    ┌───────────▼───────────────────────┐
    │  复用现有                          │
    │  · competitor-scraper（爬取引擎）   │
    │  · sql_generator / executor       │
    │  · chart_recommender              │
    │  · cache / metrics / circuit      │
    │  · AgentRouter（新增路由模式）       │
    └───────────────────────────────────┘
```

**新增代码量**：~3 个后端文件 + 前端 ~100 行

## 3. 路由分发

`agent_router.py` 新增关键词规则：

```python
MARKET_INTEL_KEYWORDS = [
    "选品", "市场机会", "能不能做", "值得卖吗", "竞争怎么样",
    "研究一下", "分析一下这个产品", "竞品分析", "差评", "痛点",
]
```

命中 → `mode: "market_intelligence"`，LLM 二次判断是 `selection` 还是 `product`。

## 4. 选品流水线（品类扫描）

输入：自然语言品类名（"蓝牙耳机"）

| 步骤 | 操作 | 实现 |
|------|------|------|
| 1. 搜索发现 | DDG 搜索 `"{品类} bestseller amazon"` → 提取 10-20 个产品 URL | `search.py`（httpx + BS4） |
| 2. 批量抓取 | 调 `competitor-scraper` 抓取产品页（价格/评分/BSR/评论数） | `scraper.py` 封装 |
| 3. 品类画像 | LLM 分析：价格分布带、品牌集中度(CR5)、评论质量、新品率 | `prompts.py` |
| 4. 内部对比 | SQL 查询内部数据库是否有相似产品 | 复用 `executor` |
| 5. 选品报告 | LLM 综合评分：机会评分/建议价格带/竞争强度/风险/差异化方向 | `selection.py` |

## 5. 商品研究流水线（单品拆解）

输入：产品名或 Amazon URL

| 步骤 | 操作 | 实现 |
|------|------|------|
| 1. 搜索定位 | 有 URL 直接抓，无 URL 则 DDG 搜索 | `search.py` |
| 2. 深度抓取 | 产品页（标题/Bullet/描述/参数）+ 评论页（最新 50 条）| `scraper.py` |
| 3. 卖点提取 | LLM 提取：核心卖点、目标用户、差异化角度 | `prompts.py` |
| 4. 评论洞察 | LLM 聚类差评 → Top3 痛点 + 关键词 | 复用 `review_analyzer.py` |
| 5. 内部对比 | SQL 查内部最接近产品 → 功能/价格/评分差异 | 复用 `executor` |
| 6. 改善建议 | LLM："我们能做得更好的点" | `product_analyzer.py` |

## 6. 前端

- 新增侧边栏 Tab：🛒 市场情报
- 卡片入口：选品分析 / 商品研究
- 结果渲染：复用 `formatResponse`（分析文本 + 图表 + 可折叠数据表）
- 流式交互：SSE 进度 + 打字效果（复用 `/api/market/stream`）

## 7. 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/market/selection` | HTTP 同步选品分析 |
| `POST` | `/api/market/product` | HTTP 同步商品研究 |
| `POST` | `/api/market/stream` | SSE 流式（自动判断 selection/product） |

## 8. MVP 边界

**包含**：选品评分、价格分析、竞争强度、痛点提取、内部对比、改善建议

**不包含**：实时价格追踪、多平台（1688/Temu）、达人数据、广告分析、自动化定时报告

## 9. 指标

- 选品分析：搜索 15s + 抓取 30-60s + LLM 分析 10s → 总计 **45-90s**
- 商品研究：抓取 10s + 分析 10s → 总计 **20-30s**
- 流式输出：逐段推送，首段分析文本在抓取完成后 **5-10s** 可见
