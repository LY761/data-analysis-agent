"""
结果检查器 — 查询执行后的四项异常检查
"""
class ResultChecker:
    """对SQL查询结果做四项检查：空结果、超行数、超耗时、数值异常"""

    @staticmethod
    def check(result: dict) -> dict:
        """
        检查查询结果是否存在异常，附带告警信息。
        返回在原result基础上追加warnings字段的dict。
        """
        warnings = []

        if not result.get("success"):
            return result

        data = result.get("data", [])
        row_count = result.get("row_count", 0)
        execution_time = result.get("execution_time_ms", 0)

        # 检查1：结果为空 → 提示可能是WHERE条件太严格
        if row_count == 0:
            warnings.append(
                "查询结果为空。可能原因：① WHERE条件过于严格 ② 数据表中确实没有匹配记录。"
                "建议：去掉部分过滤条件重试，或确认查询条件是否正确。"
            )

        # 检查2：结果行数过多 → 提示加筛选条件
        if row_count >= 500:
            warnings.append(
                f"返回{row_count}行数据，数量较多。建议添加过滤条件缩小范围，"
                "或使用 LIMIT 控制返回行数。"
            )

        # 检查3：查询过慢 → 提示缺少索引或JOIN条件不合理
        if execution_time > 3000:
            warnings.append(
                f"查询耗时 {execution_time}ms，响应较慢。建议检查是否缺少索引或JOIN条件是否合理。"
            )

        # 检查4：数值异常检测 → 最大值超过均值100倍，可能数据有问题
        if data and len(data) > 0:
            numeric_cols = []
            for col_name, value in data[0].items():
                if isinstance(value, (int, float)) and col_name.lower() not in (
                    "product_id", "customer_id", "order_id", "item_id",
                ):
                    numeric_cols.append(col_name)

            for col in numeric_cols:
                values = [row[col] for row in data if row[col] is not None]
                if values:
                    avg_val = sum(values) / len(values)
                    max_val = max(values)
                    # 最大值超过平均值100倍 → 极可能存在异常值
                    if avg_val > 0 and max_val / avg_val > 100:
                        warnings.append(
                            f"注意：列「{col}」中存在极端值（最大{max_val}，均值{avg_val:.2f}），"
                            "请确认数据或查询条件是否正确。"
                        )
                        break  # 一处异常就够，不用重复告警

        result["warnings"] = warnings
        return result


# 全局单例
result_checker = ResultChecker()
