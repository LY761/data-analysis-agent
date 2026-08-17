"""从标准数据快照聚合商品与 SKU 诊断输入。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from domain.product_diagnosis import analyze_product_diagnosis
from services.snapshot_metrics import EXCLUDED_ORDER_STATUSES
from services.standard_data_store import StandardDataStore, standard_data_store


def _number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


def _period_days(order_rows: list[dict[str, Any]]) -> int:
    timestamps = []
    for row in order_rows:
        value = str(row.get("ordered_at") or row.get("paid_at") or "").strip()
        if not value:
            continue
        try:
            timestamps.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            continue
    if len(timestamps) < 2:
        return 1
    return max(1, (max(timestamps).date() - min(timestamps).date()).days + 1)


def _raw_item(
    product_id: str,
    metadata: dict[str, Any] | None,
    period_days: int,
    *,
    sku_id: str = "",
) -> dict[str, Any]:
    source = metadata or {}
    return {
        "product_id": product_id,
        "product_name": source.get("product_name") or product_id,
        "category": source.get("category") or "",
        "brand": source.get("brand") or "",
        "sku_id": sku_id,
        "sku_name": source.get("sku_name") or sku_id,
        "gmv": 0.0,
        "order_ids": set(),
        "sold_units": 0.0,
        "cost_of_goods": 0.0,
        "ad_spend": 0.0,
        "refund_amount": 0.0,
        "refund_count": 0,
        "visitors": 0.0,
        "payers": 0.0,
        "available_stock": 0.0,
        "stock_age_days": 0.0,
        "rating_total": 0.0,
        "review_count": 0,
        "period_days": period_days,
        "skus": {},
    }


def _load_snapshot_bundle(
    tenant_id: str,
    snapshot_ids: list[str],
    store: StandardDataStore,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    rows_by_entity: dict[str, list[dict[str, Any]]] = {}
    snapshots: dict[str, dict[str, Any]] = {}
    for snapshot_id in snapshot_ids:
        snapshot = store.get_snapshot(snapshot_id)
        if snapshot["tenant_id"] != tenant_id:
            raise ValueError(f"快照不属于当前租户: {snapshot_id}")
        entity_type = snapshot["entity_type"]
        if entity_type in snapshots:
            raise ValueError(f"同一周期不能选择多个 {entity_type} 快照")
        snapshots[entity_type] = snapshot
        rows_by_entity[entity_type] = store.get_snapshot_rows(
            snapshot_id,
            max(1, snapshot["row_count"]),
        )
    return rows_by_entity, snapshots


def _filter_rows(
    rows_by_entity: dict[str, list[dict[str, Any]]],
    filters: dict[str, str] | None,
) -> dict[str, list[dict[str, Any]]]:
    active = {key: str(value).strip() for key, value in (filters or {}).items() if str(value).strip()}
    if not active:
        return rows_by_entity
    result = {key: list(rows) for key, rows in rows_by_entity.items()}
    shops = {str(row.get("shop_id") or ""): row for row in result.get("shop", [])}

    def shop_matches(shop_id: str) -> bool:
        if active.get("shop_id") and shop_id != active["shop_id"]:
            return False
        if active.get("platform"):
            return str(shops.get(shop_id, {}).get("platform") or "") == active["platform"]
        return True

    product_rows = [
        row for row in result.get("product", [])
        if shop_matches(str(row.get("shop_id") or ""))
        and (not active.get("category") or str(row.get("category") or "") == active["category"])
    ]
    allowed_product_ids = {
        str(row.get("product_id")) for row in product_rows
        if row.get("product_id") not in {None, ""}
    }
    if active.get("category") or active.get("shop_id") or active.get("platform"):
        result["product"] = product_rows
        result["sku"] = [
            row for row in result.get("sku", [])
            if str(row.get("product_id") or "") in allowed_product_ids
        ]

    order_rows = [
        row for row in result.get("order", [])
        if shop_matches(str(row.get("shop_id") or ""))
        and (not active.get("channel") or str(row.get("channel") or "") == active["channel"])
    ]
    allowed_order_ids = {
        str(row.get("order_id")) for row in order_rows
        if row.get("order_id") not in {None, ""}
    }
    if active.get("shop_id") or active.get("platform") or active.get("channel"):
        result["order"] = order_rows

    scoped_product_filter = bool(active.get("category") or active.get("shop_id") or active.get("platform"))
    scoped_order_filter = bool(active.get("shop_id") or active.get("platform") or active.get("channel"))
    result["order_item"] = [
        row for row in result.get("order_item", [])
        if (not scoped_product_filter or str(row.get("product_id") or "") in allowed_product_ids)
        and (not scoped_order_filter or str(row.get("order_id") or "") in allowed_order_ids)
    ]
    for entity_type in ("traffic_daily", "ad_daily"):
        result[entity_type] = [
            row for row in result.get(entity_type, [])
            if shop_matches(str(row.get("shop_id") or ""))
            and (not active.get("channel") or str(row.get("channel") or "") == active["channel"])
            and (not scoped_product_filter or not row.get("product_id") or str(row.get("product_id")) in allowed_product_ids)
        ]

    allowed_sku_ids = {
        str(row.get("sku_id")) for row in result.get("sku", [])
        if row.get("sku_id") not in {None, ""}
    }
    if scoped_product_filter:
        result["inventory_snapshot"] = [
            row for row in result.get("inventory_snapshot", [])
            if str(row.get("sku_id") or "") in allowed_sku_ids
        ]
        result["review"] = [
            row for row in result.get("review", [])
            if str(row.get("product_id") or "") in allowed_product_ids
        ]
    if scoped_product_filter or scoped_order_filter:
        result["refund"] = [
            row for row in result.get("refund", [])
            if (not scoped_product_filter or not row.get("sku_id") or str(row.get("sku_id")) in allowed_sku_ids)
            and (not scoped_order_filter or str(row.get("order_id") or "") in allowed_order_ids)
        ]
    return result


def aggregate_product_period(
    tenant_id: str,
    snapshot_ids: list[str],
    store: StandardDataStore | None = None,
    filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    repository = store or standard_data_store
    rows_by_entity, snapshots = _load_snapshot_bundle(tenant_id, snapshot_ids, repository)
    rows_by_entity = _filter_rows(rows_by_entity, filters)
    product_rows = rows_by_entity.get("product", [])
    sku_rows = rows_by_entity.get("sku", [])
    order_rows = rows_by_entity.get("order", [])
    days = _period_days(order_rows)

    product_metadata = {
        str(row["product_id"]): row
        for row in product_rows
        if row.get("product_id") not in {None, ""}
    }
    sku_metadata = {
        str(row["sku_id"]): row
        for row in sku_rows
        if row.get("sku_id") not in {None, ""}
    }
    sku_to_product = {
        sku_id: str(row.get("product_id") or "")
        for sku_id, row in sku_metadata.items()
        if row.get("product_id") not in {None, ""}
    }
    products: dict[str, dict[str, Any]] = {}

    def ensure_product(product_id: str) -> dict[str, Any]:
        if product_id not in products:
            products[product_id] = _raw_item(product_id, product_metadata.get(product_id), days)
        return products[product_id]

    def ensure_sku(product_id: str, sku_id: str) -> dict[str, Any]:
        product = ensure_product(product_id)
        if sku_id not in product["skus"]:
            product["skus"][sku_id] = _raw_item(
                product_id,
                sku_metadata.get(sku_id),
                days,
                sku_id=sku_id,
            )
        return product["skus"][sku_id]

    for product_id in product_metadata:
        ensure_product(product_id)
    for sku_id, product_id in sku_to_product.items():
        ensure_sku(product_id, sku_id)

    valid_order_ids = {
        str(row.get("order_id"))
        for row in order_rows
        if row.get("order_id") not in {None, ""}
        and str(row.get("order_status", "")).strip().lower() not in EXCLUDED_ORDER_STATUSES
    }
    has_order_snapshot = "order" in rows_by_entity
    order_products: dict[str, set[str]] = {}
    order_item_rows = rows_by_entity.get("order_item", [])
    for row in order_item_rows:
        order_id = str(row.get("order_id") or "")
        if has_order_snapshot and order_id not in valid_order_ids:
            continue
        product_id = str(row.get("product_id") or sku_to_product.get(str(row.get("sku_id") or ""), ""))
        if not product_id:
            continue
        sku_id = str(row.get("sku_id") or "")
        quantity = _number(row.get("quantity"))
        gmv = max(0.0, quantity * _number(row.get("unit_price")) - _number(row.get("discount_amount")))
        cost = quantity * _number(sku_metadata.get(sku_id, {}).get("cost_price"))
        product = ensure_product(product_id)
        product["gmv"] += gmv
        product["sold_units"] += quantity
        product["cost_of_goods"] += cost
        if order_id:
            product["order_ids"].add(order_id)
            order_products.setdefault(order_id, set()).add(product_id)
        if sku_id:
            sku = ensure_sku(product_id, sku_id)
            sku["gmv"] += gmv
            sku["sold_units"] += quantity
            sku["cost_of_goods"] += cost
            if order_id:
                sku["order_ids"].add(order_id)

    for row in rows_by_entity.get("traffic_daily", []):
        product_id = str(row.get("product_id") or "")
        if not product_id:
            continue
        product = ensure_product(product_id)
        product["visitors"] += _number(row.get("visitors"))
        product["payers"] += _number(row.get("payers")) or _number(row.get("orders"))

    for row in rows_by_entity.get("ad_daily", []):
        product_id = str(row.get("product_id") or "")
        if product_id:
            ensure_product(product_id)["ad_spend"] += _number(row.get("ad_spend"))

    for row in rows_by_entity.get("inventory_snapshot", []):
        sku_id = str(row.get("sku_id") or "")
        product_id = sku_to_product.get(sku_id, "")
        if not product_id:
            continue
        stock = _number(row.get("available_stock"))
        stock_age = _number(row.get("stock_age_days"))
        product = ensure_product(product_id)
        product["available_stock"] += stock
        product["stock_age_days"] = max(product["stock_age_days"], stock_age)
        sku = ensure_sku(product_id, sku_id)
        sku["available_stock"] += stock
        sku["stock_age_days"] = max(sku["stock_age_days"], stock_age)

    unallocated_refund_amount = 0.0
    for row in rows_by_entity.get("refund", []):
        sku_id = str(row.get("sku_id") or "")
        order_id = str(row.get("order_id") or "")
        product_id = sku_to_product.get(sku_id, "")
        if not product_id and len(order_products.get(order_id, set())) == 1:
            product_id = next(iter(order_products[order_id]))
        amount = _number(row.get("refund_amount"))
        if not product_id:
            unallocated_refund_amount += amount
            continue
        product = ensure_product(product_id)
        product["refund_amount"] += amount
        product["refund_count"] += 1
        if sku_id:
            sku = ensure_sku(product_id, sku_id)
            sku["refund_amount"] += amount
            sku["refund_count"] += 1

    for row in rows_by_entity.get("review", []):
        sku_id = str(row.get("sku_id") or "")
        product_id = str(row.get("product_id") or sku_to_product.get(sku_id, ""))
        if not product_id:
            continue
        rating = _number(row.get("rating"))
        product = ensure_product(product_id)
        product["rating_total"] += rating
        product["review_count"] += 1
        if sku_id:
            sku = ensure_sku(product_id, sku_id)
            sku["rating_total"] += rating
            sku["review_count"] += 1

    return {
        "products": products,
        "snapshots": list(snapshots.values()),
        "row_counts": {entity: len(rows) for entity, rows in rows_by_entity.items()},
        "unallocated_refund_amount": round(unallocated_refund_amount, 2),
        "period_days": days,
    }


def analyze_product_snapshots(
    tenant_id: str,
    current_snapshot_ids: list[str],
    previous_snapshot_ids: list[str] | None = None,
    *,
    product_ids: list[str] | None = None,
    ranking_weights: dict[str, float] | None = None,
    thresholds: dict[str, float] | None = None,
    filters: dict[str, str] | None = None,
    store: StandardDataStore | None = None,
) -> dict[str, Any]:
    if not current_snapshot_ids:
        raise ValueError("current_snapshot_ids 不能为空")
    repository = store or standard_data_store
    current = aggregate_product_period(tenant_id, current_snapshot_ids, repository, filters)
    previous_ids = list(previous_snapshot_ids or [])
    previous = (
        aggregate_product_period(tenant_id, previous_ids, repository, filters)
        if previous_ids
        else {"products": {}, "snapshots": [], "row_counts": {}, "unallocated_refund_amount": 0, "period_days": 0}
    )
    if not current["products"] and not previous["products"]:
        raise ValueError("所选快照中没有可归因到商品的数据")
    all_snapshot_ids = list(dict.fromkeys([*current_snapshot_ids, *previous_ids]))
    result = analyze_product_diagnosis(
        current["products"],
        previous["products"],
        snapshot_ids=all_snapshot_ids,
        product_ids=product_ids,
        ranking_weights=ranking_weights,
        thresholds=thresholds,
    )
    result["snapshot_aggregation"] = {
        "current": {
            "snapshots": current["snapshots"],
            "row_counts": current["row_counts"],
            "period_days": current["period_days"],
            "unallocated_refund_amount": current["unallocated_refund_amount"],
        },
        "previous": {
            "snapshots": previous["snapshots"],
            "row_counts": previous["row_counts"],
            "period_days": previous["period_days"],
            "unallocated_refund_amount": previous["unallocated_refund_amount"],
        },
    }
    return result
