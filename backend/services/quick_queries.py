"""
快捷查询模板 — 常规问题不走LLM，直接执行预写SQL（0 Token，毫秒级响应）

前端点击卡片 → 调用对应SQL → 返回数据+图表配置
"""
from datetime import datetime, timedelta
from db.executor import executor


QUICK_QUERIES = {
    "monthly_sales": {
        "label": "本月销售额",
        "icon": "💰",
        "description": "本月总销售额、订单数、客单价",
        "sql": """
            SELECT SUM(total_amount) AS total_sales,
                   COUNT(*) AS order_count,
                   ROUND(AVG(total_amount), 0) AS avg_order
            FROM orders
            WHERE strftime('%Y-%m', order_date) = strftime('%Y-%m', 'now')
              AND status NOT IN ('已退款', '已取消')
        """,
        "chart_type": "stat",  # 统计卡片
    },
    "last_month_sales": {
        "label": "上月销售额",
        "icon": "📊",
        "description": "上月销售总额、环比变化",
        "sql": """
            SELECT SUM(total_amount) AS total_sales,
                   COUNT(*) AS order_count,
                   ROUND(AVG(total_amount),0) AS avg_order
            FROM orders
            WHERE strftime('%Y-%m', order_date) = strftime('%Y-%m', 'now', '-1 month')
              AND status NOT IN ('已退款', '已取消')
        """,
        "chart_type": "stat",
    },
    "top5_products": {
        "label": "本月热销Top5",
        "icon": "🏆",
        "description": "本月销售额最高的5个产品",
        "sql": """
            SELECT p.product_name, p.category,
                   SUM(oi.quantity) AS total_qty,
                   SUM(oi.quantity * oi.unit_price) AS total_sales
            FROM order_items oi
            JOIN products p ON oi.product_id = p.product_id
            JOIN orders o ON oi.order_id = o.order_id
            WHERE strftime('%Y-%m', o.order_date) = strftime('%Y-%m', 'now')
              AND o.status NOT IN ('已退款', '已取消')
            GROUP BY p.product_name
            ORDER BY total_sales DESC LIMIT 5
        """,
        "chart_type": "bar",
    },
    "worst5_products": {
        "label": "本月滞销Top5",
        "icon": "📉",
        "description": "本月销售额最低的5个产品",
        "sql": """
            SELECT p.product_name, p.category,
                   COALESCE(SUM(oi.quantity),0) AS total_qty,
                   COALESCE(SUM(oi.quantity * oi.unit_price),0) AS total_sales
            FROM products p
            LEFT JOIN order_items oi ON p.product_id = oi.product_id
            LEFT JOIN orders o ON oi.order_id = o.order_id
                AND strftime('%Y-%m', o.order_date) = strftime('%Y-%m', 'now')
                AND o.status NOT IN ('已退款', '已取消')
            WHERE p.is_active = 1
            GROUP BY p.product_name
            ORDER BY total_sales ASC LIMIT 5
        """,
        "chart_type": "bar",
    },
    "stock_alert": {
        "label": "库存预警",
        "icon": "⚠️",
        "description": "库存不足100的产品",
        "sql": """
            SELECT product_name, category, stock_quantity, supplier
            FROM products
            WHERE is_active = 1 AND stock_quantity < 100
            ORDER BY stock_quantity ASC
        """,
        "chart_type": "table",
    },
    "category_sales": {
        "label": "各类别销售额",
        "icon": "🥧",
        "description": "本月各产品类别销售占比",
        "sql": """
            SELECT p.category,
                   SUM(oi.quantity * oi.unit_price) AS total_sales,
                   COUNT(DISTINCT oi.product_id) AS product_count
            FROM order_items oi
            JOIN products p ON oi.product_id = p.product_id
            JOIN orders o ON oi.order_id = o.order_id
            WHERE strftime('%Y-%m', o.order_date) = strftime('%Y-%m', 'now')
              AND o.status NOT IN ('已退款', '已取消')
            GROUP BY p.category
            ORDER BY total_sales DESC
        """,
        "chart_type": "pie",
    },
    "region_sales": {
        "label": "各地区销售额",
        "icon": "🗺️",
        "description": "本月各地区销售额排名",
        "sql": """
            SELECT c.region,
                   SUM(o.total_amount) AS total_sales,
                   COUNT(*) AS order_count
            FROM orders o
            JOIN customers c ON o.customer_id = c.customer_id
            WHERE strftime('%Y-%m', o.order_date) = strftime('%Y-%m', 'now')
              AND o.status NOT IN ('已退款', '已取消')
            GROUP BY c.region
            ORDER BY total_sales DESC
        """,
        "chart_type": "bar",
    },
    "member_analysis": {
        "label": "会员消费分析",
        "icon": "👑",
        "description": "各会员等级消费金额和人数",
        "sql": """
            SELECT c.member_level,
                   COUNT(DISTINCT c.customer_id) AS customer_count,
                   COALESCE(SUM(o.total_amount),0) AS total_spent,
                   ROUND(COALESCE(AVG(o.total_amount),0),0) AS avg_per_order
            FROM customers c
            LEFT JOIN orders o ON c.customer_id = o.customer_id
                AND strftime('%Y-%m', o.order_date) = strftime('%Y-%m', 'now')
                AND o.status NOT IN ('已退款', '已取消')
            GROUP BY c.member_level
            ORDER BY total_spent DESC
        """,
        "chart_type": "bar",
    },
    "payment_methods": {
        "label": "支付方式统计",
        "icon": "💳",
        "description": "本月各支付方式订单量和金额",
        "sql": """
            SELECT payment_method,
                   COUNT(*) AS order_count,
                   SUM(total_amount) AS total_amount
            FROM orders
            WHERE strftime('%Y-%m', order_date) = strftime('%Y-%m', 'now')
              AND status NOT IN ('已退款', '已取消')
            GROUP BY payment_method
            ORDER BY order_count DESC
        """,
        "chart_type": "pie",
    },
    "month_vs_last": {
        "label": "本月vs上月对比",
        "icon": "📈",
        "description": "本月和上月销售额/订单数对比",
        "sql": """
            SELECT '本月' AS period,
                   SUM(total_amount) AS total_sales,
                   COUNT(*) AS order_count
            FROM orders
            WHERE strftime('%Y-%m', order_date) = strftime('%Y-%m', 'now')
              AND status NOT IN ('已退款', '已取消')
            UNION ALL
            SELECT '上月' AS period,
                   SUM(total_amount) AS total_sales,
                   COUNT(*) AS order_count
            FROM orders
            WHERE strftime('%Y-%m', order_date) = strftime('%Y-%m', 'now', '-1 month')
              AND status NOT IN ('已退款', '已取消')
        """,
        "chart_type": "bar",
    },
    "refund_analysis": {
        "label": "退款分析",
        "icon": "↩️",
        "description": "退款订单数/金额/退款率",
        "sql": """
            SELECT
                COUNT(*) AS refund_count,
                SUM(total_amount) AS refund_amount,
                ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM orders), 1) AS refund_rate
            FROM orders
            WHERE status IN ('已退款', '已取消')
              AND strftime('%Y-%m', order_date) = strftime('%Y-%m', 'now')
        """,
        "chart_type": "stat",
    },
    "supplier_analysis": {
        "label": "供应商分析",
        "icon": "🏭",
        "description": "各供应商产品数+平均评分",
        "sql": """
            SELECT p.supplier,
                   COUNT(DISTINCT p.product_id) AS product_count,
                   ROUND(AVG(pr.rating),1) AS avg_rating,
                   COUNT(pr.review_id) AS review_count
            FROM products p
            LEFT JOIN product_reviews pr ON p.product_id = pr.product_id
            WHERE p.is_active = 1
            GROUP BY p.supplier
            ORDER BY avg_rating DESC
        """,
        "chart_type": "bar",
    },
    "last_7days_trend": {
        "label": "最近7天趋势",
        "icon": "📅",
        "description": "过去7天每日销售额折线图",
        "sql": """
            SELECT DATE(order_date) AS day,
                   SUM(total_amount) AS daily_sales,
                   COUNT(*) AS order_count
            FROM orders
            WHERE order_date >= DATETIME('now', '-7 days')
              AND status NOT IN ('已退款', '已取消')
            GROUP BY DATE(order_date)
            ORDER BY day
        """,
        "chart_type": "line",
    },
    "product_review_score": {
        "label": "产品评分排行",
        "icon": "⭐",
        "description": "产品平均评分+评价数量",
        "sql": """
            SELECT p.product_name, p.category,
                   ROUND(AVG(pr.rating),1) AS avg_rating,
                   COUNT(pr.review_id) AS review_count,
                   SUM(CASE WHEN pr.sentiment='好评' THEN 1 ELSE 0 END) AS good,
                   SUM(CASE WHEN pr.sentiment='差评' THEN 1 ELSE 0 END) AS bad
            FROM products p
            LEFT JOIN product_reviews pr ON p.product_id = pr.product_id
            WHERE p.is_active = 1
            GROUP BY p.product_name
            ORDER BY avg_rating DESC, review_count DESC
        """,
        "chart_type": "bar",
    },
}


def get_quick_query_list() -> list:
    """返回所有快捷查询的列表（给前端渲染卡片用）"""
    return [
        {
            "key": key,
            "label": config["label"],
            "icon": config["icon"],
            "description": config["description"],
        }
        for key, config in QUICK_QUERIES.items()
    ]


def run_quick_query(query_key: str) -> dict:
    """
    执行一个快捷查询，返回数据+图表配置。

    返回格式跟正常query接口一致，前端无需改动渲染逻辑。
    """
    if query_key not in QUICK_QUERIES:
        return {"error": f"未知的快捷查询: {query_key}", "available": list(QUICK_QUERIES.keys())}

    config = QUICK_QUERIES[query_key]
    result = executor.execute(config["sql"])

    # 根据图表类型构建ECharts配置
    chart = _build_chart(query_key, config["chart_type"], result)

    return {
        "question": config["label"],
        "sql": config["sql"].strip()[:200],
        "data": result.get("data", []),
        "columns": result.get("columns", []),
        "row_count": result.get("row_count", 0),
        "execution_time_ms": result.get("execution_time_ms", 0),
        "chart": chart,
        "warnings": result.get("warnings") or [],
        "error": result.get("error"),
        "quick_query": True,  # 标记来自快捷查询
    }


def _build_chart(query_key: str, chart_type: str, result: dict) -> dict:
    """根据数据结果为快捷查询生成ECharts图表配置。

    排序规则：
      - 折线图(line)：X轴升序排列（时间从左到右）
      - 柱状图(bar)：Y轴降序排列（数值从大到小）
      - 饼图(pie)：按数值降序排列（从大到小顺时针）
    """
    data = result.get("data", [])
    columns = result.get("columns", [])

    if not data or not columns:
        return {"chart_type": "table", "reason": "无数据"}

    if chart_type == "stat":
        return {"chart_type": "stat", "reason": "KPI统计卡片"}

    # 找第一个文本列和第一个数值列
    text_col = columns[0]
    num_cols = [c for c in columns[1:] if any(
        isinstance(row.get(c), (int, float)) for row in data[:1]
    )]
    num_col = num_cols[0] if num_cols else None

    if not num_col:
        return {"chart_type": "table", "reason": "无可视化数值列"}

    if chart_type == "line":
        # 折线图：X轴升序（时间从左到右）
        sorted_data = sorted(data, key=lambda r: str(r.get(text_col, "")))
        return {
            "chart_type": "line",
            "reason": "折线图趋势（X轴升序）",
            "echarts_option": {
                "tooltip": {"trigger": "axis"},
                "xAxis": {
                    "type": "category",
                    "data": [str(row[text_col]) for row in sorted_data],
                },
                "yAxis": {"type": "value"},
                "series": [{
                    "name": num_col, "type": "line", "smooth": True,
                    "data": [row.get(num_col, 0) or 0 for row in sorted_data],
                }]
            }
        }

    if chart_type == "bar":
        # 柱状图：Y轴降序（数值从大到小）
        sorted_data = sorted(data, key=lambda r: r.get(num_col, 0) or 0, reverse=True)
        return {
            "chart_type": "bar",
            "reason": "柱状图对比（数值降序）",
            "echarts_option": {
                "tooltip": {"trigger": "axis"},
                "xAxis": {
                    "type": "category",
                    "data": [str(row[text_col]) for row in sorted_data],
                    "axisLabel": {"rotate": 30 if len(sorted_data) > 5 else 0},
                },
                "yAxis": {"type": "value"},
                "series": [{
                    "name": num_col, "type": "bar",
                    "data": [row.get(num_col, 0) or 0 for row in sorted_data],
                }]
            }
        }

    if chart_type == "pie":
        # 饼图：按数值降序（从大到小顺时针）
        sorted_data = sorted(data, key=lambda r: r.get(num_col, 0) or 0, reverse=True)
        return {
            "chart_type": "pie",
            "reason": "饼图占比（数值降序）",
            "echarts_option": {
                "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                "series": [{
                    "type": "pie", "radius": ["30%", "70%"],
                    "data": [{"name": str(row[text_col]), "value": row.get(num_col, 0) or 0} for row in sorted_data],
                    "label": {"formatter": "{b}\n{d}%"},
                }]
            }
        }

    return {"chart_type": "table", "reason": "表格展示"}
