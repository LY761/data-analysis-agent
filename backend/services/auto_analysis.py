"""
自动分析服务 — 一键分析昨日/上月销售 + 差评归因 + LLM改进建议

触发方式:
  - 前端点击"智能分析"按钮 → POST /api/analysis/quick
  - 或定时报告自动调用
"""
import json
import logging
from datetime import datetime, timedelta
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger(__name__)

IMPROVEMENT_PROMPT = """你是一个电商运营分析专家。根据产品销量数据和客户差评，分析问题原因并给出改进建议。

## 分析维度
1. **问题根因**: 基于差评内容，这个产品销量下滑/表现差的核心原因是什么？
2. **改进建议**: 给出3条具体可落地的改进措施（按优先级排序）
3. **预期效果**: 做完改进后预计能提升多少（用百分比估算）

## 输出格式（JSON）
{
  "product_name": "产品名",
  "problem_summary": "一句话总结核心问题",
  "root_causes": ["根因1", "根因2"],
  "suggestions": [
    {"priority": 1, "action": "具体措施", "expected_impact": "预期效果"},
    {"priority": 2, "action": "具体措施", "expected_impact": "预期效果"},
    {"priority": 3, "action": "具体措施", "expected_impact": "预期效果"}
  ],
  "urgency": "紧急/一般/关注"
}

## 输出要求
只输出JSON，不要解释。"""


class AutoAnalyzer:
    """自动分析器：销量+差评→根因+改进建议"""

    def __init__(self):
        self.client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

    def analyze(self) -> dict:
        """
        一键分析：昨日概况 + 上月概况 + 差评产品 + LLM改进建议

        返回:
          {
            "yesterday": {昨日数据汇总},
            "last_month": {上月数据汇总},
            "underperforming": [{差评产品+分析+建议}, ...],
            "summary": "整体分析总结"
          }
        """
        from db.executor import executor

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        now = datetime.now()
        if now.month == 1:
            last_month_year = now.year - 1
            last_month = 12
        else:
            last_month_year = now.year
            last_month = now.month - 1
        month_filter = f"strftime('%Y-%m', order_date) = '{last_month_year}-{last_month:02d}'"

        logger.info(f"[AutoAnalysis] 分析: 昨日={yesterday}, 上月={last_month_year}-{last_month:02d}")

        # ═════════════════════════════════════════════════════
        # 1. 昨日概况
        # ═════════════════════════════════════════════════════
        yesterday_sales = executor.execute(f"""
            SELECT COUNT(*) as order_count, SUM(total_amount) as total_sales,
                   ROUND(AVG(total_amount),0) as avg_order
            FROM orders
            WHERE DATE(order_date) = '{yesterday}'
              AND status NOT IN ('已退款', '已取消')
        """)

        yesterday_top = executor.execute(f"""
            SELECT p.product_name, p.category,
                   SUM(oi.quantity) as qty, SUM(oi.quantity*oi.unit_price) as sales
            FROM order_items oi
            JOIN products p ON oi.product_id=p.product_id
            JOIN orders o ON oi.order_id=o.order_id
            WHERE DATE(o.order_date) = '{yesterday}'
              AND o.status NOT IN ('已退款','已取消')
            GROUP BY p.product_name ORDER BY sales DESC LIMIT 5
        """)

        # ═════════════════════════════════════════════════════
        # 2. 上月概况
        # ═════════════════════════════════════════════════════
        month_sales = executor.execute(f"""
            SELECT COUNT(*) as order_count, SUM(total_amount) as total_sales
            FROM orders WHERE {month_filter}
              AND status NOT IN ('已退款', '已取消')
        """)

        month_top = executor.execute(f"""
            SELECT p.product_name, SUM(oi.quantity*oi.unit_price) as sales
            FROM order_items oi
            JOIN products p ON oi.product_id=p.product_id
            JOIN orders o ON oi.order_id=o.order_id
            WHERE {month_filter} AND o.status NOT IN ('已退款','已取消')
            GROUP BY p.product_name ORDER BY sales DESC LIMIT 10
        """)

        # ═════════════════════════════════════════════════════
        # 3. 上月表现差的产品（销量低+差评多）
        # ═════════════════════════════════════════════════════
        underperforming = executor.execute(f"""
            SELECT p.product_name, p.category,
                   COALESCE(SUM(oi.quantity*oi.unit_price),0) as monthly_sales,
                   ROUND(AVG(pr.rating),1) as avg_rating,
                   COUNT(pr.review_id) as review_count,
                   SUM(CASE WHEN pr.sentiment='差评' THEN 1 ELSE 0 END) as bad_reviews,
                   SUM(CASE WHEN pr.sentiment='好评' THEN 1 ELSE 0 END) as good_reviews
            FROM products p
            LEFT JOIN order_items oi ON p.product_id=oi.product_id
            LEFT JOIN orders o ON oi.order_id=o.order_id AND {month_filter}
            LEFT JOIN product_reviews pr ON p.product_id=pr.product_id
                AND strftime('%Y-%m', pr.review_date)='{last_month_year}-{last_month:02d}'
            WHERE p.is_active=1
            GROUP BY p.product_name
            HAVING avg_rating IS NOT NULL AND avg_rating < 3.5
            ORDER BY monthly_sales ASC, avg_rating ASC
            LIMIT 8
        """)

        # ═════════════════════════════════════════════════════
        # 4. 为每个差评产品获取差评详情 + LLM生成建议
        # ═════════════════════════════════════════════════════
        analyzed_products = []
        for prod in (underperforming.get("data") or [])[:5]:
            # 获取该产品的差评详情
            bad_reviews = executor.execute(f"""
                SELECT pr.rating, pr.review_text, pr.review_date
                FROM product_reviews pr
                JOIN products p ON pr.product_id=p.product_id
                WHERE p.product_name = '{prod['product_name'].replace("'","''")}'
                  AND pr.sentiment = '差评'
                ORDER BY pr.rating ASC LIMIT 5
            """)

            # 调LLM分析
            analysis = self._analyze_product(
                prod["product_name"],
                prod.get("category", ""),
                prod.get("monthly_sales", 0),
                prod.get("avg_rating", 0),
                bad_reviews.get("data", [])
            )

            analyzed_products.append({
                "product_name": prod["product_name"],
                "category": prod.get("category", ""),
                "monthly_sales": prod.get("monthly_sales", 0),
                "avg_rating": prod.get("avg_rating", 0),
                "bad_review_count": prod.get("bad_reviews", 0),
                "bad_review_samples": [
                    {"rating": r["rating"], "text": r["review_text"][:100]}
                    for r in (bad_reviews.get("data") or [])[:3]
                ],
                **analysis,
            })

        # ═════════════════════════════════════════════════════
        # 5. 汇总
        # ═════════════════════════════════════════════════════
        yesterday_data = yesterday_sales.get("data", [{}])[0] if yesterday_sales.get("data") else {}
        month_data = month_sales.get("data", [{}])[0] if month_sales.get("data") else {}

        return {
            "analysis_time": datetime.now().isoformat(),
            "yesterday": {
                "date": yesterday,
                "total_sales": yesterday_data.get("total_sales", 0),
                "order_count": yesterday_data.get("order_count", 0),
                "avg_order": yesterday_data.get("avg_order", 0),
                "top5": yesterday_top.get("data", []),
            },
            "last_month": {
                "period": f"{last_month_year}年{last_month}月",
                "total_sales": month_data.get("total_sales", 0),
                "order_count": month_data.get("order_count", 0),
                "top10": month_top.get("data", []),
            },
            "underperforming": analyzed_products,
            "improvement_summary": self._summarize(analyzed_products),
        }

    def _analyze_product(self, name: str, category: str,
                         monthly_sales: float, avg_rating: float,
                         bad_reviews: list) -> dict:
        """用LLM分析单个产品的差评并给出改进建议"""
        if not bad_reviews:
            return {
                "problem_summary": "暂无差评数据，可能销量过低",
                "root_causes": ["曝光不足或非目标客户"],
                "suggestions": [
                    {"priority": 1, "action": "增加产品曝光和促销活动", "expected_impact": "提升销量30-50%"},
                    {"priority": 2, "action": "检查产品定价和竞品对比", "expected_impact": "提升转化率"},
                ],
                "urgency": "关注",
            }

        reviews_text = "\n".join([
            f"- [{r.get('rating',0)}星] {r.get('review_text','')[:150]}"
            for r in bad_reviews[:5]
        ])

        prompt = f"""产品: {name}（{category}）
上月销售额: {monthly_sales}元
平均评分: {avg_rating}/5
差评内容:
{reviews_text}

请分析问题并给出改进建议。"""

        try:
            response = self.client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": IMPROVEMENT_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=600,
                response_format={"type": "json_object"},
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.error(f"[AutoAnalysis] LLM分析失败 ({name}): {e}")
            return {
                "problem_summary": f"分析失败: {str(e)[:50]}",
                "root_causes": [],
                "suggestions": [],
                "urgency": "关注",
            }

    def _summarize(self, analyzed: list) -> str:
        """生成整体改进总结"""
        if not analyzed:
            return "上月产品销售表现正常，无需要特别关注的差评产品。"
        return f"共发现{len(analyzed)}个需要关注的产品，主要问题集中在" + \
               "、".join(set(p.get("problem_summary", "")[:30] for p in analyzed[:3]))


# 全局单例
auto_analyzer = AutoAnalyzer()
