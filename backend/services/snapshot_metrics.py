"""从标准数据快照聚合店铺诊断输入。"""

from __future__ import annotations

from typing import Any

from services.standard_data_store import StandardDataStore, standard_data_store


EXCLUDED_ORDER_STATUSES = {
    "已退款",
    "已取消",
    "退款成功",
    "cancelled",
    "canceled",
    "refunded",
    "closed",
}


def _number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


def aggregate_store_period(
    tenant_id: str,
    snapshot_ids: list[str],
    store: StandardDataStore | None = None,
) -> dict[str, Any]:
    repository = store or standard_data_store
    snapshots: dict[str, dict[str, Any]] = {}
    rows_by_entity: dict[str, list[dict[str, Any]]] = {}
    for snapshot_id in snapshot_ids:
        snapshot = repository.get_snapshot(snapshot_id)
        if snapshot["tenant_id"] != tenant_id:
            raise ValueError(f"快照不属于当前租户: {snapshot_id}")
        entity_type = snapshot["entity_type"]
        if entity_type in snapshots:
            raise ValueError(f"同一周期不能选择多个 {entity_type} 快照")
        snapshots[entity_type] = snapshot
        rows_by_entity[entity_type] = repository.get_snapshot_rows(
            snapshot_id,
            max(1, snapshot["row_count"]),
        )

    values = {
        "visitors": 0.0,
        "orders": 0.0,
        "gmv": 0.0,
        "ad_spend": 0.0,
        "cost_of_goods": 0.0,
        "refund_amount": 0.0,
        "sold_units": 0.0,
        "stock_units": 0.0,
    }
    source_metrics: dict[str, dict[str, Any]] = {}

    traffic_rows = rows_by_entity.get("traffic_daily", [])
    if traffic_rows:
        values["visitors"] = sum(_number(row.get("visitors")) for row in traffic_rows)
        traffic_orders = sum(
            _number(row.get("payers")) or _number(row.get("orders"))
            for row in traffic_rows
        )
        values["orders"] = traffic_orders
        source_metrics["visitors"] = {"entity_type": "traffic_daily", "snapshot_id": snapshots["traffic_daily"]["snapshot_id"]}
        source_metrics["orders"] = {"entity_type": "traffic_daily", "snapshot_id": snapshots["traffic_daily"]["snapshot_id"]}

    order_rows = rows_by_entity.get("order", [])
    if order_rows:
        valid_orders = [
            row for row in order_rows
            if str(row.get("order_status", "")).strip().lower() not in EXCLUDED_ORDER_STATUSES
        ]
        values["orders"] = float(len(valid_orders))
        values["gmv"] = sum(_number(row.get("paid_amount")) for row in valid_orders)
        source_metrics["orders"] = {"entity_type": "order", "snapshot_id": snapshots["order"]["snapshot_id"]}
        source_metrics["gmv"] = {"entity_type": "order", "snapshot_id": snapshots["order"]["snapshot_id"]}

    ad_rows = rows_by_entity.get("ad_daily", [])
    if ad_rows:
        values["ad_spend"] = sum(_number(row.get("ad_spend")) for row in ad_rows)
        source_metrics["ad_spend"] = {"entity_type": "ad_daily", "snapshot_id": snapshots["ad_daily"]["snapshot_id"]}

    refund_rows = rows_by_entity.get("refund", [])
    if refund_rows:
        values["refund_amount"] = sum(_number(row.get("refund_amount")) for row in refund_rows)
        source_metrics["refund_amount"] = {"entity_type": "refund", "snapshot_id": snapshots["refund"]["snapshot_id"]}

    order_item_rows = rows_by_entity.get("order_item", [])
    if order_item_rows:
        values["sold_units"] = sum(_number(row.get("quantity")) for row in order_item_rows)
        source_metrics["sold_units"] = {"entity_type": "order_item", "snapshot_id": snapshots["order_item"]["snapshot_id"]}

    inventory_rows = rows_by_entity.get("inventory_snapshot", [])
    if inventory_rows:
        values["stock_units"] = sum(_number(row.get("available_stock")) for row in inventory_rows)
        source_metrics["stock_units"] = {"entity_type": "inventory_snapshot", "snapshot_id": snapshots["inventory_snapshot"]["snapshot_id"]}

    sku_rows = rows_by_entity.get("sku", [])
    if order_item_rows and sku_rows:
        costs = {
            str(row.get("sku_id")): _number(row.get("cost_price"))
            for row in sku_rows
            if row.get("sku_id") is not None
        }
        values["cost_of_goods"] = sum(
            _number(row.get("quantity")) * costs.get(str(row.get("sku_id")), 0)
            for row in order_item_rows
        )
        source_metrics["cost_of_goods"] = {
            "entity_type": "order_item+sku",
            "snapshot_ids": [
                snapshots["order_item"]["snapshot_id"],
                snapshots["sku"]["snapshot_id"],
            ],
        }

    return {
        "values": values,
        "snapshots": list(snapshots.values()),
        "source_metrics": source_metrics,
        "missing_inputs": [key for key, value in values.items() if value == 0 and key not in source_metrics],
    }
