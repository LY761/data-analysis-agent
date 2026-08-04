"""
图表推荐器 — 根据查询结果的数据特征自动推荐最佳图表类型
"""
import re


class ChartRecommender:
    """分析查询结果的列类型和行数，推荐图表类型并输出ECharts配置"""

    @staticmethod
    def recommend(result: dict) -> dict:
        """
        分析数据结构并推荐图表。
        返回: {chart_type, reason, columns, row_count, echarts_option}
        """
        if not result.get("success") or not result.get("data"):
            return {"chart_type": None, "reason": "无数据，无法推荐图表", "echarts_option": None}

        columns = result.get("columns", [])
        data = result.get("data", [])
        row_count = len(data)
        col_count = len(columns)

        if row_count == 0 or col_count == 0:
            return {"chart_type": None, "reason": "无数据"}

        # 第一步：对列按类型分类
        text_cols = []
        numeric_cols = []
        date_cols = []

        for col in columns:
            sample_values = [row[col] for row in data[:20] if row[col] is not None]
            if not sample_values:
                continue
            sample = sample_values[0]
            if isinstance(sample, str):
                # 判断是否像日期列：列名关键词 或 值符合日期格式（2026-01 / 2026年1月）
                if (any(kw in col.lower() for kw in ("date", "time", "日期", "时间", "月份"))
                        or re.match(r"^\d{4}[-/年]\d{1,2}", sample.strip())):
                    date_cols.append(col)
                else:
                    text_cols.append(col)
            elif isinstance(sample, (int, float)):
                numeric_cols.append(col)

        # 第二步：基于数据特征推荐图表类型
        chart_type = "table"
        reason = ""
        echarts_option = None

        # 有时间+数值 → 折线图展示趋势
        if date_cols and numeric_cols:
            chart_type = "line"
            reason = f"检测到时间字段「{date_cols[0]}」和数值字段，推荐折线图展示趋势变化。"
            echarts_option = _build_line_chart(data, date_cols[0], numeric_cols, columns)

        # 有分类+数值+行数较少 → 饼图或柱状图
        elif text_cols and numeric_cols and row_count <= 20:
            if row_count <= 8:
                # ≤8个分类 → 饼图展示占比
                chart_type = "pie"
                reason = f"共{row_count}个分类，占比较少，推荐饼图展示占比分布。"
                echarts_option = _build_pie_chart(data, text_cols[0], numeric_cols[0])
            else:
                # ≤20个分类 → 柱状图做对比
                chart_type = "bar"
                reason = f"共{row_count}个分类，推荐柱状图进行对比（自动排序）。"
                echarts_option = _build_bar_chart(data, text_cols[0], numeric_cols, columns)

        # 有分类+数值+行数多（>20） → Top15 柱状图（避免表格过宽）
        elif text_cols and numeric_cols:
            chart_type = "bar"
            top = data[:15]
            reason = f"共{row_count}个分类，取前15展示柱状图（数据量较大）。"
            echarts_option = _build_bar_chart(top, text_cols[0], numeric_cols, columns)

        # 多指标+少行数 → 分组柱状图
        elif len(numeric_cols) >= 2 and row_count <= 5:
            chart_type = "bar"
            reason = f"多指标对比，推荐分组柱状图。"
            echarts_option = _build_bar_chart(data, text_cols[0] if text_cols else columns[0], numeric_cols, columns)

        # 多指标+时间 → 多线折线图
        elif date_cols and len(numeric_cols) >= 2:
            chart_type = "line"
            reason = f"多指标时间趋势，推荐多线折线图。"
            echarts_option = _build_line_chart(data, date_cols[0], numeric_cols, columns)

        # 兜底：表格
        else:
            chart_type = "table"
            reason = "数据特征复杂，默认使用表格展示。可尝试追问指定图表类型。"

        return {
            "chart_type": chart_type,
            "reason": reason,
            "columns": columns,
            "row_count": row_count,
            "echarts_option": echarts_option,
        }


def _build_bar_chart(data, category_col, value_cols, all_columns):
    """构建ECharts柱状图配置"""
    categories = [str(row[category_col]) for row in data]
    series = []
    for vcol in value_cols:
        series.append({
            "name": vcol,
            "type": "bar",
            "data": [row[vcol] if row[vcol] is not None else 0 for row in data],
        })
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": value_cols} if len(value_cols) > 1 else None,
        "xAxis": {"type": "category", "data": categories, "axisLabel": {"rotate": 30 if len(categories) > 6 else 0}},
        "yAxis": {"type": "value"},
        "series": series,
    }


def _build_line_chart(data, date_col, value_cols, all_columns):
    """构建ECharts折线图配置"""
    dates = [str(row[date_col]) for row in data]
    series = []
    for vcol in value_cols:
        series.append({
            "name": vcol,
            "type": "line",
            "smooth": True,
            "data": [row[vcol] if row[vcol] is not None else 0 for row in data],
        })
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": value_cols} if len(value_cols) > 1 else None,
        "xAxis": {"type": "category", "data": dates, "axisLabel": {"rotate": 30 if len(dates) > 10 else 0}},
        "yAxis": {"type": "value"},
        "series": series,
    }


def _build_pie_chart(data, name_col, value_col):
    """构建ECharts饼图配置"""
    pie_data = [{"name": str(row[name_col]), "value": row[value_col]} for row in data]
    return {
        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
        "series": [{
            "type": "pie",
            "radius": ["30%", "70%"],
            "data": pie_data,
            "label": {"formatter": "{b}\n{d}%"},
        }],
    }


# 全局单例
chart_recommender = ChartRecommender()
