"""经营驾驶舱统一聚合服务。"""

from __future__ import annotations

from typing import Any

from domain.metric_registry import metric_registry
from services.product_snapshot_analysis import analyze_product_snapshots
from services.standard_data_store import StandardDataStore, standard_data_store
from services.workflow_store import WorkflowStore, workflow_store


CARD_METRICS = (
    "gmv",
    "gross_profit",
    "gross_margin_pct",
    "sold_units",
    "refund_rate_pct",
    "conversion_rate_pct",
    "available_stock",
    "average_rating",
)

DRILLDOWN_TARGETS = {
    "gmv": "/api/diagnostics/store/from-snapshots",
    "gross_profit": "/api/diagnostics/products",
    "gross_margin_pct": "/api/diagnostics/products",
    "sold_units": "/api/products/rankings",
    "refund_rate_pct": "/api/diagnostics/products",
    "conversion_rate_pct": "/api/diagnostics/store/from-snapshots",
    "available_stock": "/api/tasks",
    "average_rating": "/api/diagnostics/products",
}


def _round(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _divide(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _aggregate_profiles(profiles: list[dict[str, Any]], previous: bool = False) -> dict[str, float | None]:
    if not profiles:
        return {metric_key: None for metric_key in CARD_METRICS}
    field = "previous_metrics" if previous else "metrics"
    metrics = [profile[field] for profile in profiles]
    gmv = sum(float(item.get("gmv") or 0) for item in metrics)
    gross_profit = sum(float(item.get("gross_profit") or 0) for item in metrics)
    sold_units = sum(float(item.get("sold_units") or 0) for item in metrics)
    refund_amount = sum(float(item.get("refund_amount") or 0) for item in metrics)
    visitors = sum(float(item.get("visitors") or 0) for item in metrics)
    order_count = sum(float(item.get("order_count") or 0) for item in metrics)
    available_stock = sum(float(item.get("available_stock") or 0) for item in metrics)
    review_count = sum(float(item.get("review_count") or 0) for item in metrics)
    rating_total = sum(
        float(item.get("average_rating") or 0) * float(item.get("review_count") or 0)
        for item in metrics
    )
    return {
        "gmv": _round(gmv),
        "gross_profit": _round(gross_profit),
        "gross_margin_pct": _round(_divide(gross_profit * 100, gmv)),
        "sold_units": _round(sold_units),
        "refund_rate_pct": _round(_divide(refund_amount * 100, gmv)),
        "conversion_rate_pct": _round(_divide(order_count * 100, visitors)),
        "available_stock": _round(available_stock),
        "average_rating": _round(_divide(rating_total, review_count)),
    }


def _change_pct(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return _round((current - previous) / previous * 100)


def _card_status(metric_key: str, value: float | None, change_pct: float | None) -> str:
    if value is None:
        return "unavailable"
    if metric_key == "refund_rate_pct":
        return "critical" if value >= 10 else "warning" if value >= 5 else "healthy"
    if metric_key == "gross_margin_pct":
        return "critical" if value < 0 else "warning" if value < 15 else "healthy"
    if metric_key == "average_rating":
        return "critical" if value < 3.5 else "warning" if value < 4 else "healthy"
    if change_pct is not None and change_pct <= -10:
        return "critical"
    if change_pct is not None and change_pct < 0:
        return "warning"
    return "healthy"


def _build_cards(
    current: dict[str, float | None],
    previous: dict[str, float | None],
    targets: dict[str, float] | None,
    snapshot_ids: list[str],
) -> list[dict[str, Any]]:
    cards = []
    for metric_key in CARD_METRICS:
        definition = metric_registry.get(metric_key)
        current_value = current.get(metric_key)
        previous_value = previous.get(metric_key)
        change = _change_pct(current_value, previous_value)
        target = (targets or {}).get(metric_key)
        target_completion = _round(_divide(float(current_value or 0) * 100, float(target))) if target else None
        unavailable_reason = ""
        if current_value is None:
            unavailable_reason = f"缺少计算 {definition.name} 所需的有效分母或数据。"
        elif previous_value in {None, 0}:
            unavailable_reason = "上一周期无有效基数，暂不计算变化率。"
        cards.append({
            "metric_key": metric_key,
            "name": definition.name,
            "value": current_value,
            "previous_value": previous_value,
            "change_pct": change,
            "unit": definition.unit,
            "status": _card_status(metric_key, current_value, change),
            "target": target,
            "target_completion_pct": target_completion,
            "unavailable_reason": unavailable_reason,
            "metric_version": definition.version,
            "formula": definition.formula,
            "drilldown": DRILLDOWN_TARGETS[metric_key],
            "evidence": {
                "snapshot_ids": list(snapshot_ids),
                "metric_key": metric_key,
                "metric_version": definition.version,
            },
        })
    return cards


def _empty_diagnosis(snapshot_ids: list[str]) -> dict[str, Any]:
    return {
        "profiles": [],
        "rankings": [],
        "decline_contributors": [],
        "evidence": [],
        "snapshot_ids": snapshot_ids,
        "summary": {"product_count": 0, "total_gmv": 0, "total_gmv_change": 0, "high_risk_product_count": 0},
    }


def build_dashboard(
    tenant_id: str,
    current_snapshot_ids: list[str],
    previous_snapshot_ids: list[str] | None = None,
    *,
    filters: dict[str, str] | None = None,
    targets: dict[str, float] | None = None,
    current_label: str = "current",
    previous_label: str = "previous",
    data_store: StandardDataStore | None = None,
    automation_store: WorkflowStore | None = None,
) -> dict[str, Any]:
    if not current_snapshot_ids:
        raise ValueError("current_snapshot_ids 不能为空")
    repository = data_store or standard_data_store
    task_store = automation_store or workflow_store
    previous_ids = list(previous_snapshot_ids or [])
    all_snapshot_ids = list(dict.fromkeys([*current_snapshot_ids, *previous_ids]))
    try:
        diagnosis = analyze_product_snapshots(
            tenant_id,
            current_snapshot_ids,
            previous_ids,
            filters=filters,
            store=repository,
        )
    except ValueError as error:
        if "没有可归因到商品的数据" not in str(error):
            raise
        diagnosis = _empty_diagnosis(all_snapshot_ids)

    profiles = diagnosis["profiles"]
    current_metrics = _aggregate_profiles(profiles)
    previous_metrics = _aggregate_profiles(profiles, previous=True)
    cards = _build_cards(current_metrics, previous_metrics, targets, all_snapshot_ids)
    alerts = task_store.list_alerts(tenant_id, "open", 50)
    tasks = task_store.list_tasks(tenant_id, "pending", 100)
    product_anomalies = [
        {
            "type": "product_risk",
            "product_id": profile["product_id"],
            "product_name": profile["product_name"],
            "severity": profile["risk_level"],
            "tags": [tag for tag in profile["tags"] if tag["level"] in {"warning", "critical"}],
            "drilldown": f"/api/products/{profile['product_id']}/profile",
        }
        for profile in profiles
        if profile["risk_level"] in {"high", "medium"}
    ]
    workflow_anomalies = [
        {
            "type": alert["alert_type"],
            "severity": alert["severity"],
            "title": alert["title"],
            "alert_id": alert["alert_id"],
            "run_id": alert["run_id"],
            "created_at": alert["created_at"],
            "drilldown": f"/api/workflow-runs/{alert['run_id']}",
        }
        for alert in alerts
    ]
    snapshots = [repository.get_snapshot(snapshot_id) for snapshot_id in all_snapshot_ids]
    entity_types = sorted({snapshot["entity_type"] for snapshot in snapshots})
    recommended_entities = {"product", "sku", "order", "order_item", "inventory_snapshot", "refund", "review"}

    return {
        "schema_version": "1.0",
        "tenant_id": tenant_id,
        "filters": dict(filters or {}),
        "overview": {
            "cards": cards,
            "product_count": len(profiles),
            "high_risk_product_count": sum(profile["risk_level"] == "high" for profile in profiles),
            "pending_task_count": len(tasks),
            "open_alert_count": len(alerts),
        },
        "trends": {
            "periods": [
                {"label": previous_label, "metrics": previous_metrics},
                {"label": current_label, "metrics": current_metrics},
            ],
            "series": {
                metric_key: [previous_metrics.get(metric_key), current_metrics.get(metric_key)]
                for metric_key in CARD_METRICS
            },
        },
        "products": {
            "top": diagnosis["rankings"][:10],
            "decline_contributors": diagnosis["decline_contributors"][:10],
        },
        "anomalies": [*workflow_anomalies, *product_anomalies],
        "tasks": tasks,
        "data_availability": {
            "available_entities": entity_types,
            "missing_recommended_entities": sorted(recommended_entities - set(entity_types)),
            "snapshot_ids": all_snapshot_ids,
        },
    }
