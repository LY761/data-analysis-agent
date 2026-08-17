"""确定性的电商经营指标与商品对比计算。"""

from __future__ import annotations

from typing import Any

from domain.metric_registry import metric_registry


def _as_number(value: Any, field_name: str, allow_negative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} 必须是数字")
    number = float(value)
    if number < 0 and not allow_negative:
        raise ValueError(f"{field_name} 不能为负数")
    return number


def analyze_store_metrics(
    visitors: float,
    orders: float,
    gmv: float,
    ad_spend: float = 0,
    cost_of_goods: float = 0,
    refund_amount: float = 0,
    sold_units: float = 0,
    stock_units: float = 0,
    previous_gmv: float = 0,
) -> dict:
    values = {
        "visitors": _as_number(visitors, "visitors"),
        "orders": _as_number(orders, "orders"),
        "gmv": _as_number(gmv, "gmv"),
        "ad_spend": _as_number(ad_spend, "ad_spend"),
        "cost_of_goods": _as_number(cost_of_goods, "cost_of_goods"),
        "refund_amount": _as_number(refund_amount, "refund_amount"),
        "sold_units": _as_number(sold_units, "sold_units"),
        "stock_units": _as_number(stock_units, "stock_units"),
        "previous_gmv": _as_number(previous_gmv, "previous_gmv"),
    }

    metric_keys = [
        "conversion_rate_pct",
        "average_order_value",
        "roas",
        "refund_rate_pct",
        "gross_profit",
        "gross_margin_pct",
        "sell_through_rate_pct",
        "gmv_growth_pct",
    ]
    metrics = metric_registry.calculate_many(metric_keys, values)
    conversion_rate = metrics["conversion_rate_pct"]
    roas = metrics["roas"]
    refund_rate = metrics["refund_rate_pct"]
    gross_margin = metrics["gross_margin_pct"]
    sell_through_rate = metrics["sell_through_rate_pct"]
    gmv_growth = metrics["gmv_growth_pct"]

    diagnostics: list[dict] = []
    recommendations: list[str] = []

    if conversion_rate is not None:
        status = "healthy" if conversion_rate >= 2 else "warning" if conversion_rate >= 1 else "critical"
        diagnostics.append({"metric": "conversion_rate_pct", "status": status, "value": metrics["conversion_rate_pct"]})
        if conversion_rate < 1:
            recommendations.append("优先检查流量精准度、商品详情页、价格力和下单路径。")

    if roas is not None:
        status = "healthy" if roas >= 2 else "warning" if roas >= 1 else "critical"
        diagnostics.append({"metric": "roas", "status": status, "value": metrics["roas"]})
        if roas < 1:
            recommendations.append("暂停或降价低回报广告计划，按素材、人群和关键词拆分复盘。")

    if refund_rate is not None:
        status = "critical" if refund_rate >= 10 else "warning" if refund_rate >= 5 else "healthy"
        diagnostics.append({"metric": "refund_rate_pct", "status": status, "value": metrics["refund_rate_pct"]})
        if refund_rate >= 5:
            recommendations.append("按退款原因和 SKU 排查质量、描述偏差及履约问题。")

    if gross_margin is not None:
        status = "healthy" if gross_margin >= 15 else "warning" if gross_margin >= 0 else "critical"
        diagnostics.append({"metric": "gross_margin_pct", "status": status, "value": metrics["gross_margin_pct"]})
        if gross_margin < 15:
            recommendations.append("重新核算商品成本、投放费用和退款损耗，避免只看 GMV。")

    if sell_through_rate is not None and sell_through_rate < 20:
        diagnostics.append({"metric": "sell_through_rate_pct", "status": "warning", "value": metrics["sell_through_rate_pct"]})
        recommendations.append("动销偏低，建议控制补货并针对滞销 SKU 做促销或组合销售。")

    if gmv_growth is not None and gmv_growth < 0:
        status = "critical" if gmv_growth <= -10 else "warning"
        diagnostics.append({"metric": "gmv_growth_pct", "status": status, "value": metrics["gmv_growth_pct"]})
        recommendations.append("GMV 下滑时按流量、转化率和客单价三层定位贡献因素。")

    if not recommendations:
        recommendations.append("核心指标未触发默认风险阈值，建议继续按渠道和 SKU 分层观察。")

    return {
        "input": values,
        "metrics": metrics,
        "metric_definitions": {
            key: {
                "name": metric_registry.get(key).name,
                "unit": metric_registry.get(key).unit,
                "version": metric_registry.get(key).version,
                "formula": metric_registry.get(key).formula,
            }
            for key in metric_keys
        },
        "diagnostics": diagnostics,
        "recommendations": recommendations,
        "assumptions": [
            "cost_of_goods 表示本周期已售商品总成本。",
            "stock_units 表示当前库存，动销率按已售/(已售+库存)计算。",
            "默认诊断阈值仅用于初筛，生产环境应按租户、平台和品类配置。",
        ],
    }


def _normalize(value: float, minimum: float, maximum: float) -> float:
    if maximum == minimum:
        return 0.5
    return (value - minimum) / (maximum - minimum)


def compare_products(products: list[dict]) -> dict:
    if not isinstance(products, list) or len(products) < 2:
        raise ValueError("products 至少需要两个商品")

    metric_weights = {
        "sales": 0.35,
        "rating": 0.20,
        "review_count": 0.15,
        "gross_margin_pct": 0.20,
        "growth_rate_pct": 0.10,
    }
    prepared: list[dict] = []
    warnings: list[str] = []

    for index, product in enumerate(products, 1):
        if not isinstance(product, dict):
            raise ValueError(f"第 {index} 个商品必须是对象")
        name = str(product.get("name", "")).strip()
        if not name:
            raise ValueError(f"第 {index} 个商品缺少 name")

        item = {"name": name, "price": _as_number(product.get("price", 0), f"{name}.price")}
        for metric in metric_weights:
            if metric not in product:
                warnings.append(f"{name} 缺少 {metric}，本次按 0 计算。")
            item[metric] = _as_number(
                product.get(metric, 0),
                f"{name}.{metric}",
                allow_negative=metric in {"gross_margin_pct", "growth_rate_pct"},
            )
        if item["rating"] > 5:
            raise ValueError(f"{name}.rating 不能大于 5")
        prepared.append(item)

    ranges = {
        metric: (
            min(item[metric] for item in prepared),
            max(item[metric] for item in prepared),
        )
        for metric in metric_weights
    }

    rankings: list[dict] = []
    for item in prepared:
        contributions = {
            metric: _normalize(item[metric], *ranges[metric]) * weight * 100
            for metric, weight in metric_weights.items()
        }
        rankings.append({
            **item,
            "score": round(sum(contributions.values()), 2),
            "score_breakdown": {key: round(value, 2) for key, value in contributions.items()},
        })

    rankings.sort(key=lambda item: item["score"], reverse=True)
    for rank, item in enumerate(rankings, 1):
        item["rank"] = rank

    leaders = {
        metric: max(prepared, key=lambda item: item[metric])["name"]
        for metric in metric_weights
    }

    return {
        "rankings": rankings,
        "leaders": {
            **leaders,
            "lowest_price": min(prepared, key=lambda item: item["price"])["name"],
        },
        "weights": metric_weights,
        "warnings": warnings,
        "assumption": "评分用于横向初筛，不替代平台真实流量、投放和利润数据。",
    }
