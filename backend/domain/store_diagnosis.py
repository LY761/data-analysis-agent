"""店铺经营诊断与 GMV 确定性贡献拆解。"""

from __future__ import annotations

from typing import Any

from domain.ecommerce_metrics import analyze_store_metrics
from domain.metric_registry import metric_registry


INPUT_FIELDS = (
    "visitors",
    "orders",
    "gmv",
    "ad_spend",
    "cost_of_goods",
    "refund_amount",
    "sold_units",
    "stock_units",
)


def _normalize_period(values: dict[str, Any], period_name: str) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for field in INPUT_FIELDS:
        value = values.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{period_name}.{field} 必须是数字")
        number = float(value)
        if number < 0:
            raise ValueError(f"{period_name}.{field} 不能为负数")
        normalized[field] = number
    return normalized


def _change_pct(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / previous * 100, 2)


def _evidence(
    metric_key: str,
    current_value: float | None,
    previous_value: float | None,
    snapshot_ids: list[str],
    contribution: float | None = None,
) -> dict[str, Any]:
    definition = metric_registry.get(metric_key)
    return {
        "metric_key": metric_key,
        "metric_name": definition.name,
        "metric_version": definition.version,
        "formula": definition.formula,
        "current_value": current_value,
        "previous_value": previous_value,
        "change_pct": _change_pct(current_value, previous_value),
        "contribution": round(contribution, 2) if contribution is not None else None,
        "source": {
            "type": "aggregated_input",
            "snapshot_ids": list(snapshot_ids),
        },
    }


def analyze_store_diagnosis(
    current: dict[str, Any],
    previous: dict[str, Any],
    snapshot_ids: list[str] | None = None,
) -> dict[str, Any]:
    current_values = _normalize_period(current, "current")
    previous_values = _normalize_period(previous, "previous")
    snapshots = list(snapshot_ids or [])

    current_analysis = analyze_store_metrics(
        **current_values,
        previous_gmv=previous_values["gmv"],
    )
    previous_analysis = analyze_store_metrics(**previous_values)
    current_metrics = current_analysis["metrics"]
    previous_metrics = previous_analysis["metrics"]

    drivers: list[dict[str, Any]] = []
    decomposition_available = (
        previous_values["visitors"] > 0
        and previous_values["orders"] > 0
        and current_values["visitors"] > 0
    )
    if decomposition_available:
        previous_conversion = previous_values["orders"] / previous_values["visitors"]
        current_conversion = current_values["orders"] / current_values["visitors"]
        previous_aov = previous_values["gmv"] / previous_values["orders"]
        current_aov = current_values["gmv"] / current_values["orders"] if current_values["orders"] > 0 else 0
        contributions = {
            "traffic": (current_values["visitors"] - previous_values["visitors"]) * previous_conversion * previous_aov,
            "conversion": current_values["visitors"] * (current_conversion - previous_conversion) * previous_aov,
            "average_order_value": current_values["visitors"] * current_conversion * (current_aov - previous_aov),
        }
        driver_names = {
            "traffic": "流量",
            "conversion": "支付转化率",
            "average_order_value": "客单价",
        }
        for driver_key, contribution in contributions.items():
            drivers.append({
                "driver": driver_key,
                "name": driver_names[driver_key],
                "contribution": round(contribution, 2),
                "direction": "positive" if contribution > 0 else "negative" if contribution < 0 else "neutral",
            })
        drivers.sort(key=lambda item: abs(item["contribution"]), reverse=True)

    gmv_growth = current_metrics["gmv_growth_pct"]
    if gmv_growth is None:
        severity = "unknown"
        conclusion = "缺少有效的上一周期 GMV，暂时无法判断增长趋势。"
    elif gmv_growth <= -10:
        severity = "high"
        conclusion = f"当前周期 GMV 较上一周期下降 {abs(gmv_growth):.2f}%，需要优先处理。"
    elif gmv_growth < 0:
        severity = "medium"
        conclusion = f"当前周期 GMV 较上一周期下降 {abs(gmv_growth):.2f}%。"
    else:
        severity = "info"
        conclusion = f"当前周期 GMV 较上一周期增长 {gmv_growth:.2f}%。"

    contribution_by_driver = {item["driver"]: item["contribution"] for item in drivers}
    evidence = [
        _evidence("gmv_growth_pct", current_values["gmv"], previous_values["gmv"], snapshots),
        _evidence(
            "conversion_rate_pct",
            current_metrics["conversion_rate_pct"],
            previous_metrics["conversion_rate_pct"],
            snapshots,
            contribution_by_driver.get("conversion"),
        ),
        _evidence(
            "average_order_value",
            current_metrics["average_order_value"],
            previous_metrics["average_order_value"],
            snapshots,
            contribution_by_driver.get("average_order_value"),
        ),
        _evidence("gross_margin_pct", current_metrics["gross_margin_pct"], previous_metrics["gross_margin_pct"], snapshots),
        _evidence("refund_rate_pct", current_metrics["refund_rate_pct"], previous_metrics["refund_rate_pct"], snapshots),
        _evidence("roas", current_metrics["roas"], previous_metrics["roas"], snapshots),
    ]

    return {
        "schema_version": "1.0",
        "scenario": "store_diagnosis",
        "conclusion": conclusion,
        "severity": severity,
        "metrics": current_metrics,
        "previous_metrics": previous_metrics,
        "drivers": drivers,
        "decomposition_available": decomposition_available,
        "evidence": evidence,
        "recommendations": current_analysis["recommendations"],
        "diagnostics": current_analysis["diagnostics"],
        "snapshot_ids": snapshots,
        "assumptions": [
            "GMV 贡献按流量、转化率、客单价顺序做完全分解。",
            "输入是已聚合经营数据，快照仅作为本次分析的数据上下文引用。",
            *current_analysis["assumptions"],
        ],
    }
