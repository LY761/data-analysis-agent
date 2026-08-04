"""
竞品分析器 — 内部数据 vs 外部竞品，LLM生成竞争洞察

数据来源:
  - 内部: demo_sales.db (products, reviews, orders)
  - 外部: competitor-scraper (竞品JSON + 差评分析 + 报告)
"""
import json
import os
import logging
from typing import Optional
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, COMPETITOR_SCRAPER_PATH

logger = logging.getLogger(__name__)

# competitor-scraper 项目路径（可经 COMPETITOR_SCRAPER_PATH 环境变量覆盖）
SCRAPER_PATH = COMPETITOR_SCRAPER_PATH

# 已知竞品列表
KNOWN_COMPETITORS = {
    "安克创新": "安克创新_Anker.json",
    "anker": "安克创新_Anker.json",
    "绿联": "绿联_UGREEN.json",
    "ugreen": "绿联_UGREEN.json",
    "正浩创新": "正浩创新_EcoFlow.json",
    "ecoflow": "正浩创新_EcoFlow.json",
    "华宝新能": "华宝新能_Jackery.json",
    "jackery": "华宝新能_Jackery.json",
    "倍思": "倍思_Baseus.json",
    "baseus": "倍思_Baseus.json",
}

ANALYSIS_PROMPT = """你是电商竞争分析师。根据内部数据库的销售数据和外部竞品信息，生成竞争洞察。

## 分析维度
1. 产品对比: 内部 vs 竞品的价格/评分/销量
2. 优势劣势: 我们从数据和评论中能看出什么
3. 机会威胁: 竞品动态对我们有什么影响
4. 建议: 具体可落地的竞争策略

## 输出格式
用中文，分段落，每段一个要点。控制在200字以内。要有数据支撑。"""


class CompetitorAnalyzer:
    """竞品分析器"""

    def __init__(self):
        self.client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

    def analyze(self, competitor_name: str, stream_cb=None) -> dict:
        """
        分析指定竞品。

        stream_cb(delta)：可选，传入后 LLM 生成的报告会逐token回调（流式输出）。
        返回: {found, name, analysis, data_sources, error?}
        """
        # 0. 从问句中解析出规范竞品名（如"分析一下绿联公司" → "绿联"）
        key = self._match_key(competitor_name)
        if not key:
            return {
                "found": False,
                "name": competitor_name,
                "analysis": f"未找到「{competitor_name}」的竞品数据。可用竞品: {', '.join(KNOWN_COMPETITORS.keys())}",
                "data_sources": [],
            }
        competitor_name = key  # 用规范名展示和生成

        # 1. 查找竞品数据文件
        data_file = self._find_competitor_file(competitor_name)
        if not data_file:
            return {
                "found": False,
                "name": competitor_name,
                "analysis": f"未找到「{competitor_name}」的竞品数据。可用竞品: {', '.join(KNOWN_COMPETITORS.keys())}",
                "data_sources": [],
            }

        # 2. 加载竞品数据
        competitor_data = self._load_json(data_file)
        if not competitor_data:
            return {"found": False, "name": competitor_name, "analysis": "竞品数据加载失败", "data_sources": []}

        # 3. 加载内部对比数据
        internal_data = self._get_internal_comparison(competitor_name)

        # 4. 加载差评分析（如果有）
        negative_reviews = self._load_latest_negative_reviews()

        # 5. LLM生成洞察（流式路径逐token回调）
        analysis = self._generate_insights(
            competitor_name, competitor_data, internal_data, negative_reviews, stream_cb
        )

        return {
            "found": True,
            "name": competitor_name,
            "analysis": analysis,
            "data_sources": [os.path.basename(data_file)],
            "internal_summary": internal_data,
        }

    def list_competitors(self) -> list:
        """列出所有可分析的竞品"""
        result = []
        seen = set()
        for name, filename in KNOWN_COMPETITORS.items():
            # 去重（中英文名指向同文件，只保留中文名）
            base = os.path.splitext(filename)[0].split("_")[0]
            if base in seen:
                continue
            seen.add(base)
            path = os.path.join(SCRAPER_PATH, "data", filename)
            result.append({
                "name": base,
                "filename": filename,
                "has_data": os.path.exists(path),
            })
        return result

    # ═══════════════════════════════
    # 内部方法
    # ═══════════════════════════════

    def _match_key(self, name: str) -> Optional[str]:
        """
        从用户输入中解析出已知竞品名（精确或包含式模糊匹配）。
        例: "分析一下绿联公司" → "绿联"；"anker怎么样" → "anker"。
        """
        name = (name or "").strip().lower()
        if not name:
            return None
        for known in KNOWN_COMPETITORS:
            if known.lower() == name or known.lower() in name:
                return known
        return None

    def _find_competitor_file(self, name: str) -> Optional[str]:
        """查找竞品数据文件"""
        key = self._match_key(name)
        if not key:
            return None
        filename = KNOWN_COMPETITORS[key]
        path = os.path.join(SCRAPER_PATH, "data", filename)
        return path if os.path.exists(path) else None

    def _load_json(self, path: str) -> Optional[dict]:
        """安全加载JSON文件"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[Competitor] 加载失败 {path}: {e}")
            return None

    def _get_internal_comparison(self, competitor_name: str) -> dict:
        """从内部数据库获取对比数据"""
        from db.executor import executor

        # 内部产品数据：Top5产品 + 平均评分 + 价格区间
        products = executor.execute("""
            SELECT p.product_name, p.category, p.unit_price, p.stock_quantity,
                   ROUND(AVG(pr.rating), 1) AS avg_rating, COUNT(pr.review_id) AS review_count
            FROM products p
            LEFT JOIN product_reviews pr ON p.product_id = pr.product_id
            WHERE p.is_active = 1
            GROUP BY p.product_name
            ORDER BY p.unit_price DESC
        """)

        sales = executor.execute("""
            SELECT SUM(total_amount) AS total_sales, COUNT(*) AS order_count
            FROM orders
            WHERE strftime('%Y-%m', order_date) = strftime('%Y-%m', 'now')
              AND status NOT IN ('已退款', '已取消')
        """)

        return {
            "products": products.get("data", [])[:10],
            "monthly_sales": (sales.get("data", [{}])[0] or {}).get("total_sales", 0),
            "monthly_orders": (sales.get("data", [{}])[0] or {}).get("order_count", 0),
        }

    def _load_latest_negative_reviews(self) -> Optional[dict]:
        """加载最新的差评分析"""
        data_dir = os.path.join(SCRAPER_PATH, "data")
        if not os.path.exists(data_dir):
            return None
        # 找最新的 real_negative_reviews 文件
        files = sorted(
            [f for f in os.listdir(data_dir) if f.startswith("real_negative_reviews")],
            reverse=True
        )
        if files:
            return self._load_json(os.path.join(data_dir, files[0]))
        return None

    def _generate_insights(self, name: str, competitor: dict,
                           internal: dict, reviews: Optional[dict],
                           stream_cb=None) -> str:
        """用LLM生成竞争洞察（支持流式）"""
        # 提取竞品关键信息
        comp_summary = json.dumps(competitor, ensure_ascii=False)[:800]
        internal_summary = json.dumps(internal, ensure_ascii=False)[:600]

        prompt = f"""## 竞品: {name}
竞品数据摘要: {comp_summary}

## 内部数据对比
{internal_summary}

## 差评数据
{json.dumps(reviews, ensure_ascii=False)[:400] if reviews else '暂无'}

请分析竞品情况并给出建议。"""

        def _build():
            return self.client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": ANALYSIS_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3, max_tokens=500,
                stream=bool(stream_cb),
            )

        try:
            if stream_cb:
                resp = _build()
                parts = []
                for chunk in resp:
                    if not chunk or not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta.content or ""
                    if delta:
                        parts.append(delta)
                        stream_cb(delta)
                return "".join(parts).strip()
            response = _build()
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"[Competitor] LLM分析失败: {e}")
            return f"竞品「{name}」数据已加载，但LLM分析临时不可用。请稍后重试。"


# 全局单例
analyzer = CompetitorAnalyzer()


def analyze_competitor(name: str, stream_cb=None) -> dict:
    """快捷调用"""
    return analyzer.analyze(name, stream_cb)
