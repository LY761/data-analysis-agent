"""
商业智能服务 — 异常检测 + 趋势预警 + 自动化运营日报
"""
import json
import logging
from datetime import datetime, timedelta
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger(__name__)


class BusinessIntelligence:
    """商业智能分析：异常检测 + 趋势预警 + 运营日报"""

    def __init__(self):
        self.client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

    # ═══════════════════════════════════════════════════
    # 1. 异常检测
    # ═══════════════════════════════════════════════════

    def detect_anomalies(self) -> dict:
        """检测关键指标的异常"""
        from db.executor import executor

        anomalies = []

        # 检测1: 今日销售额 vs 过去7天日均
        today = datetime.now().strftime("%Y-%m-%d")
        today_sales = executor.execute(f"""
            SELECT COALESCE(SUM(total_amount),0) AS sales, COUNT(*) AS orders
            FROM orders
            WHERE DATE(order_date) = DATE('now')
              AND status NOT IN ('已退款','已取消')
        """)

        avg_7d = executor.execute(f"""
            SELECT COALESCE(SUM(total_amount)/7,0) AS avg_sales
            FROM orders
            WHERE order_date >= DATETIME('now','-7 days')
              AND DATE(order_date) < DATE('now')
              AND status NOT IN ('已退款','已取消')
        """)

        today_val = (today_sales.get("data", [{}])[0] or {}).get("sales", 0) or 0
        avg_val = (avg_7d.get("data", [{}])[0] or {}).get("avg_sales", 0) or 0

        if avg_val > 0:
            ratio = today_val / avg_val
            if ratio < 0.5:
                anomalies.append({
                    "type": "sales_drop",
                    "severity": "high",
                    "title": f"今日销售额异常下降",
                    "detail": f"今日 {today_val:.0f}元 vs 近7天日均 {avg_val:.0f}元，下降 {((1-ratio)*100):.0f}%",
                    "suggestion": "建议检查是否有大规模退款、系统故障或竞品活动",
                })
            elif ratio > 2.0:
                anomalies.append({
                    "type": "sales_spike",
                    "severity": "info",
                    "title": f"今日销售额异常增长",
                    "detail": f"今日 {today_val:.0f}元 vs 近7天日均 {avg_val:.0f}元，增长 {((ratio-1)*100):.0f}%",
                    "suggestion": "建议确认是否有促销活动或大客户订单",
                })

        # 检测2: 退款率是否超标
        refund = executor.execute(f"""
            SELECT COUNT(*) AS cnt FROM orders
            WHERE strftime('%Y-%m',order_date)=strftime('%Y-%m','now')
              AND status IN ('已退款','已取消')
        """)
        total = executor.execute(f"""
            SELECT COUNT(*) AS cnt FROM orders
            WHERE strftime('%Y-%m',order_date)=strftime('%Y-%m','now')
        """)
        refund_cnt = (refund.get("data", [{}])[0] or {}).get("cnt", 0) or 0
        total_cnt = (total.get("data", [{}])[0] or {}).get("cnt", 0) or 1
        refund_rate = refund_cnt / total_cnt * 100

        if refund_rate > 15:
            anomalies.append({
                "type": "high_refund",
                "severity": "high",
                "title": f"退款率偏高",
                "detail": f"本月退款率 {refund_rate:.1f}%（{refund_cnt}/{total_cnt}单），超过15%警戒线",
                "suggestion": "建议排查高频退款产品，检查产品质量或描述准确性",
            })

        # 检测3: 库存告急
        stock = executor.execute("""
            SELECT product_name, stock_quantity FROM products
            WHERE is_active=1 AND stock_quantity < 50 AND stock_quantity > 0
            ORDER BY stock_quantity ASC LIMIT 5
        """)
        for item in (stock.get("data") or []):
            anomalies.append({
                "type": "low_stock",
                "severity": "medium" if item["stock_quantity"] < 20 else "low",
                "title": f"库存告急: {item['product_name']}",
                "detail": f"仅剩 {item['stock_quantity']} 件",
                "suggestion": "建议立即补货",
            })

        logger.info(f"[BI] 异常检测完成: {len(anomalies)}项异常")
        return {"total": len(anomalies), "items": anomalies}

    # ═══════════════════════════════════════════════════
    # 2. 趋势预警
    # ═══════════════════════════════════════════════════

    def detect_trends(self) -> dict:
        """检测产品销量的下降趋势"""
        from db.executor import executor

        # 对比本月和上月每个产品的销量，找出连续下降的
        trends = executor.execute("""
            WITH this_month AS (
                SELECT p.product_name, p.category,
                       COALESCE(SUM(oi.quantity*oi.unit_price),0) AS sales
                FROM products p
                LEFT JOIN order_items oi ON p.product_id=oi.product_id
                LEFT JOIN orders o ON oi.order_id=o.order_id
                    AND strftime('%Y-%m',o.order_date)=strftime('%Y-%m','now')
                    AND o.status NOT IN ('已退款','已取消')
                WHERE p.is_active=1
                GROUP BY p.product_name
            ),
            last_month AS (
                SELECT p.product_name,
                       COALESCE(SUM(oi.quantity*oi.unit_price),0) AS sales
                FROM products p
                LEFT JOIN order_items oi ON p.product_id=oi.product_id
                LEFT JOIN orders o ON oi.order_id=o.order_id
                    AND strftime('%Y-%m',o.order_date)=strftime('%Y-%m','now','-1 month')
                    AND o.status NOT IN ('已退款','已取消')
                WHERE p.is_active=1
                GROUP BY p.product_name
            )
            SELECT t.product_name, t.category, t.sales AS this_month, l.sales AS last_month,
                   ROUND((t.sales - l.sales) / NULLIF(l.sales,0) * 100, 1) AS change_pct
            FROM this_month t JOIN last_month l ON t.product_name=l.product_name
            WHERE l.sales > 500 AND t.sales < l.sales * 0.7
            ORDER BY change_pct ASC LIMIT 8
        """)

        warnings = []
        for item in (trends.get("data") or []):
            warnings.append({
                "product": item["product_name"],
                "category": item.get("category", ""),
                "this_month": item["this_month"],
                "last_month": item["last_month"],
                "change_pct": item["change_pct"],
                "alert": f"环比下降 {abs(item['change_pct']):.0f}%，连续下滑趋势",
            })

        logger.info(f"[BI] 趋势预警: {len(warnings)}个产品下降")
        return {
            "total": len(warnings),
            "items": warnings,
            "summary": f"共{len(warnings)}个产品出现明显下滑趋势" if warnings else "所有产品销售趋势稳定"
        }

    # ═══════════════════════════════════════════════════
    # 3. 自动化运营日报
    # ═══════════════════════════════════════════════════

    def generate_daily_report(self) -> dict:
        """生成自然语言运营日报"""
        from db.executor import executor

        today = datetime.now().strftime("%Y-%m-%d")

        # 收集数据
        sales = executor.execute(f"""
            SELECT COALESCE(SUM(total_amount),0) AS sales, COUNT(*) AS orders
            FROM orders WHERE DATE(order_date)=DATE('now')
              AND status NOT IN ('已退款','已取消')
        """)
        sales_data = (sales.get("data", [{}])[0] or {})

        top = executor.execute(f"""
            SELECT p.product_name, SUM(oi.quantity*oi.unit_price) AS sales
            FROM order_items oi JOIN products p ON oi.product_id=p.product_id
            JOIN orders o ON oi.order_id=o.order_id
            WHERE DATE(o.order_date)=DATE('now')
              AND o.status NOT IN ('已退款','已取消')
            GROUP BY p.product_name ORDER BY sales DESC LIMIT 3
        """)

        stock_alert = executor.execute("""
            SELECT COUNT(*) AS cnt FROM products
            WHERE is_active=1 AND stock_quantity < 50
        """)

        # 异常检测
        anomalies = self.detect_anomalies()
        trends = self.detect_trends()

        # LLM生成报告
        prompt = f"""请根据以下数据生成一份简洁的电商运营日报（200字以内）。

今日销售额: {sales_data.get('sales',0)}元, 订单数: {sales_data.get('orders',0)}单
热销产品: {json.dumps([{'name':p['product_name'],'sales':p['sales']} for p in (top.get('data') or [])], ensure_ascii=False)}
库存预警产品数: {(stock_alert.get('data',[{}])[0] or {}).get('cnt',0)}个
异常数: {anomalies['total']}项
下滑趋势产品: {trends['total']}个

格式: 用一段话总结今日运营情况,包含:
1. 总体概况(1句)
2. 亮点(1句)
3. 需关注(1句,如果可以引用异常或趋势数据)
4. 建议(1句)"""

        try:
            response = self.client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": "你是电商运营总监，擅长用简洁的语言做数据总结。只输出报告内容，不要markdown格式。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3, max_tokens=300,
            )
            narrative = response.choices[0].message.content.strip()
        except Exception as e:
            narrative = f"今日销售额 {sales_data.get('sales',0)}元，共 {sales_data.get('orders',0)} 单。系统繁忙，详细报告生成失败。"

        return {
            "date": today,
            "narrative": narrative,
            "metrics": {
                "sales": sales_data.get("sales", 0),
                "orders": sales_data.get("orders", 0),
            },
            "top_products": top.get("data", []),
            "anomalies": anomalies,
            "trends": trends,
        }


# 全局单例
bi = BusinessIntelligence()
