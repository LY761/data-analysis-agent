# -*- coding: utf-8 -*-
"""粘贴数据分析 — 用户提供真实数据（文本/链接内容），Agent 直接 LLM 分析，不触发任何爬虫。

三种模式：
  - product:    粘贴商品信息（标题/价格/评分/评论/描述）→ 卖点 / 痛点 / 建议
  - selection:  粘贴市场调研文本 → 选品建议（与爬虫版 selection 输出结构一致）
  - competitor: 粘贴竞品信息文本 → 竞争洞察（与竞品分析 chat_reply 兼容）

用途：Amazon/京东反爬导致自动抓取经常拿不到真实数据时，用户把看到的真实信息
贴进来分析——这是最可靠、零反爬的数据来源。
"""
import json
import logging
import re
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from agent.market_intelligence.selection import _call_llm

logger = logging.getLogger(__name__)

PASTE_PRODUCT_PROMPT = """你是电商产品分析师。根据用户提供的产品信息文本（可能是商品链接、标题、价格、评分、评论、卖点描述等），
提取并分析该产品。信息不足的字段填空字符串，**不要编造**。

只输出 JSON（严格格式，不要其他内容）：
{
  "product": {"title": "产品名", "price": "价格或空", "rating": "评分或空", "summary": "一句话产品概述"},
  "sellpoints": "核心卖点分析（3-5点，每点一句话）",
  "pains": "用户痛点/不足（基于文本中的评论或描述；无则写'文本中未见明确痛点'）",
  "suggestions": "改进建议（2-3条）"
}"""

PASTE_SELECTION_PROMPT = """你是跨境电商选品顾问。根据用户提供的市场调研文本（可能是产品、价格、销量、评论、竞争情况等），
评估该品类的选品机会。

只输出 JSON（严格格式，不要其他内容）：
{
  "category": "推断的品类名",
  "profile": "品类画像（一段话：价格带/竞争/机会）",
  "recommendation": {
    "score": 0-100整数,
    "verdict": "推荐/谨慎/不推荐一句话",
    "price_band": "建议价格带",
    "competition": "低/中/高 + 一句依据",
    "risks": ["风险1", "风险2"],
    "differentiation": "差异化方向一句话",
    "reasoning": "2-3句综合理由"
  }
}"""

PASTE_COMPETITOR_PROMPT = """你是电商竞争分析师。根据用户提供的竞品信息文本（公司/产品/价格/评价/市场动态等），
生成中文竞争洞察，分 4 段：
1. 产品与价格概况
2. 优势与劣势
3. 机会与威胁
4. 竞争建议
控制在 250 字以内，用文本中出现的信息支撑观点，**不要编造**。"""


def _default_client():
    return OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


def _extract_json(raw: str) -> dict:
    """剥离 ```json 围栏/前后散文，取首个 {...} 解析。失败抛异常由上层兜底。"""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw).removesuffix("```").strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"未找到JSON: {raw[:80]}")


def analyze_pasted(mode: str, text: str, llm_client=None) -> dict:
    """按模式分析用户粘贴的文本数据（不触发任何爬虫）。

    返回结构兼容前端现有渲染：
      - product:   {query, product, sellpoints, pains, suggestions, internal, error}
      - selection: {category, products, profile, internal, recommendation, error}
      - competitor:{name, analysis, data_sources, error}
    """
    client = llm_client or _default_client()
    text = (text or "").strip()
    if not text:
        return {"error": "粘贴内容为空，请粘贴商品/市场/竞品信息后再试", "mode": mode}
    if mode not in ("product", "selection", "competitor"):
        return {"error": f"不支持的模式: {mode}", "mode": mode}

    # 文本过长截断（防 token 溢出）
    text = text[:8000]

    try:
        if mode == "product":
            raw = _call_llm(client, [
                {"role": "system", "content": "你是电商产品分析师，只输出JSON。"},
                {"role": "user", "content": PASTE_PRODUCT_PROMPT + "\n\n用户提供的信息：\n" + text},
            ])
            data = _extract_json(raw)
            p = data.get("product") or {}
            return {
                "query": p.get("title") or text[:30],
                "product": {
                    "title": p.get("title", ""),
                    "price": p.get("price", ""),
                    "rating": p.get("rating", ""),
                    "summary": p.get("summary", ""),
                },
                "sellpoints": data.get("sellpoints", ""),
                "pains": data.get("pains", ""),
                "suggestions": data.get("suggestions", ""),
                "internal": [],
                "error": None,
            }

        if mode == "selection":
            raw = _call_llm(client, [
                {"role": "system", "content": "你是跨境电商选品顾问，只输出JSON。"},
                {"role": "user", "content": PASTE_SELECTION_PROMPT + "\n\n用户提供的调研文本：\n" + text},
            ])
            data = _extract_json(raw)
            return {
                "category": data.get("category", "未知名"),
                "products": [],
                "profile": data.get("profile", ""),
                "internal": [],
                "recommendation": data.get("recommendation", {}),
                "error": None,
            }

        # competitor：纯文本洞察
        analysis = _call_llm(client, [
            {"role": "system", "content": PASTE_COMPETITOR_PROMPT},
            {"role": "user", "content": "竞品信息：\n" + text},
        ])
        return {"name": "粘贴竞品数据", "analysis": analysis,
                "data_sources": ["用户提供"], "error": None}
    except Exception as e:
        logger.warning(f"[PasteAnalysis] 分析失败: {e}")
        return {"error": f"分析失败（请确认粘贴内容包含足够的产品/市场信息）: {e}", "mode": mode}
