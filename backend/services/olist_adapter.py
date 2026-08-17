"""Olist 真实公开数据到标准电商快照的适配器。"""

from __future__ import annotations

import csv
import hashlib
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from services.standard_data_store import StandardDataStore, standard_data_store
from services.standardization import import_standard_rows


DATASET_URL = "https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce"
DATASET_LICENSE = "CC BY-NC-SA 4.0"
REQUIRED_FILES = (
    "olist_orders_dataset.csv",
    "olist_order_items_dataset.csv",
    "olist_products_dataset.csv",
    "olist_order_reviews_dataset.csv",
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def _identity_mapping(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {key: key for key in rows[0]} if rows else {}


def _month(value: str) -> str:
    return value[:7] if len(value) >= 7 else ""


def _scenario_cover_days(product_id: str) -> int:
    digest = hashlib.sha256(product_id.encode("utf-8")).hexdigest()
    return 3 + int(digest[:4], 16) % 88


def _select_periods(orders: list[dict[str, str]]) -> tuple[str, str]:
    counts = Counter(
        _month(row.get("order_purchase_timestamp", ""))
        for row in orders
        if _month(row.get("order_purchase_timestamp", ""))
    )
    eligible = sorted(month for month, count in counts.items() if count >= 100)
    months = eligible if len(eligible) >= 2 else sorted(counts)
    if len(months) < 2:
        raise ValueError("Olist 数据至少需要覆盖两个自然月")
    return months[-2], months[-1]


def _build_period_rows(
    period: str,
    orders: list[dict[str, str]],
    items: list[dict[str, str]],
    products: dict[str, dict[str, str]],
    translations: dict[str, str],
    reviews: list[dict[str, str]],
    payments: list[dict[str, str]],
    max_orders: int | None,
) -> dict[str, list[dict[str, Any]]]:
    period_orders = [
        row for row in orders
        if _month(row.get("order_purchase_timestamp", "")) == period
    ]
    period_orders.sort(key=lambda row: (row.get("order_purchase_timestamp", ""), row.get("order_id", "")))
    if max_orders is not None:
        period_orders = period_orders[-max_orders:]
    order_ids = {row["order_id"] for row in period_orders}
    period_items = [row for row in items if row.get("order_id") in order_ids]
    items_by_order: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in period_items:
        items_by_order[row["order_id"]].append(row)

    payment_by_order: dict[str, float] = defaultdict(float)
    for row in payments:
        if row.get("order_id") in order_ids:
            payment_by_order[row["order_id"]] += float(row.get("payment_value") or 0)

    order_rows = []
    order_item_rows = []
    product_sales: dict[str, list[float]] = defaultdict(list)
    order_products: dict[str, set[str]] = defaultdict(set)
    for order in period_orders:
        order_id = order["order_id"]
        item_total = sum(
            float(item.get("price") or 0) + float(item.get("freight_value") or 0)
            for item in items_by_order.get(order_id, [])
        )
        order_rows.append({
            "order_id": order_id,
            "shop_id": "olist_marketplace",
            "customer_id": order.get("customer_id", ""),
            "ordered_at": order.get("order_purchase_timestamp", ""),
            "paid_at": order.get("order_approved_at", ""),
            "paid_amount": round(payment_by_order.get(order_id, item_total), 2),
            "discount_amount": 0,
            "order_status": order.get("order_status", ""),
            "channel": "olist_marketplace",
        })
        for item in items_by_order.get(order_id, []):
            product_id = item.get("product_id", "")
            price = float(item.get("price") or 0)
            order_products[order_id].add(product_id)
            product_sales[product_id].append(price)
            order_item_rows.append({
                "order_item_id": f"{order_id}:{item.get('order_item_id', '')}",
                "order_id": order_id,
                "product_id": product_id,
                "sku_id": product_id,
                "quantity": 1,
                "unit_price": price,
                "discount_amount": 0,
            })

    product_rows = []
    sku_rows = []
    inventory_rows = []
    for product_id, prices in sorted(product_sales.items()):
        source = products.get(product_id, {})
        category_key = source.get("product_category_name", "")
        category = translations.get(category_key, category_key or "unknown")
        average_price = sum(prices) / len(prices)
        product_rows.append({
            "product_id": product_id,
            "shop_id": "olist_marketplace",
            "product_name": f"{category}-{product_id[:8]}",
            "category": category,
            "brand": "",
            "list_price": round(average_price, 2),
            "product_status": "active",
            "created_at": "",
        })
        sku_rows.append({
            "sku_id": product_id,
            "product_id": product_id,
            "sku_name": f"default-{product_id[:8]}",
            "sale_price": round(average_price, 2),
            "cost_price": round(average_price * 0.65, 2),
            "sku_status": "active",
        })
        cover_days = _scenario_cover_days(product_id)
        estimated_daily_sales = max(1 / 30, len(prices) / 30)
        inventory_rows.append({
            "snapshot_at": f"{period}-28 23:59:59",
            "sku_id": product_id,
            "warehouse_id": "olist_demo_warehouse",
            "available_stock": math.ceil(estimated_daily_sales * cover_days),
            "locked_stock": 0,
            "inbound_stock": 0,
            "stock_age_days": cover_days,
        })

    review_rows_by_id: dict[str, dict[str, Any]] = {}
    for review in reviews:
        order_id = review.get("order_id", "")
        if order_id not in order_ids:
            continue
        for product_id in sorted(order_products.get(order_id, set())):
            review_id = f"{review.get('review_id', order_id)}:{product_id}"
            candidate = {
                "review_id": review_id,
                "product_id": product_id,
                "sku_id": product_id,
                "rating": float(review.get("review_score") or 0),
                "review_text": review.get("review_comment_message", ""),
                "sentiment": "",
                "reviewed_at": review.get("review_creation_date") or review.get("review_answer_timestamp", ""),
            }
            existing = review_rows_by_id.get(review_id)
            if existing is None or (
                bool(candidate["review_text"]), candidate["reviewed_at"]
            ) > (
                bool(existing["review_text"]), existing["reviewed_at"]
            ):
                review_rows_by_id[review_id] = candidate
    review_rows = list(review_rows_by_id.values())

    refund_rows = []
    cancelled_statuses = {"canceled", "unavailable"}
    orders_by_id = {row["order_id"]: row for row in period_orders}
    for item in period_items:
        order = orders_by_id[item["order_id"]]
        if order.get("order_status", "").lower() not in cancelled_statuses:
            continue
        refund_rows.append({
            "refund_id": f"derived:{item['order_id']}:{item.get('order_item_id', '')}",
            "order_id": item["order_id"],
            "sku_id": item.get("product_id", ""),
            "refund_amount": float(item.get("price") or 0),
            "refund_reason": f"derived_from_order_status:{order.get('order_status', '')}",
            "refund_status": "derived_refund",
            "created_at": order.get("order_approved_at") or order.get("order_purchase_timestamp", ""),
        })

    rows = {
        "product": product_rows,
        "sku": sku_rows,
        "order": order_rows,
        "order_item": order_item_rows,
        "inventory_snapshot": inventory_rows,
        "review": review_rows,
    }
    if refund_rows:
        rows["refund"] = refund_rows
    return rows


def import_olist_demo(
    dataset_dir: str | Path,
    tenant_id: str = "olist-demo",
    *,
    max_orders_per_period: int | None = 5000,
    store: StandardDataStore | None = None,
) -> dict[str, Any]:
    root = Path(dataset_dir)
    missing = [filename for filename in REQUIRED_FILES if not (root / filename).exists()]
    if missing:
        raise FileNotFoundError(f"Olist 数据文件缺失: {', '.join(missing)}")
    repository = store or standard_data_store
    orders = _read_csv(root / "olist_orders_dataset.csv")
    items = _read_csv(root / "olist_order_items_dataset.csv")
    product_list = _read_csv(root / "olist_products_dataset.csv")
    reviews = _read_csv(root / "olist_order_reviews_dataset.csv")
    payments_path = root / "olist_order_payments_dataset.csv"
    payments = _read_csv(payments_path) if payments_path.exists() else []
    translation_path = root / "product_category_name_translation.csv"
    translations = {
        row.get("product_category_name", ""): row.get("product_category_name_english", "")
        for row in (_read_csv(translation_path) if translation_path.exists() else [])
    }
    products = {row["product_id"]: row for row in product_list}
    previous_period, current_period = _select_periods(orders)

    snapshots_by_period: dict[str, list[str]] = {}
    entities_by_period: dict[str, dict[str, int]] = {}
    for period in (previous_period, current_period):
        entity_rows = _build_period_rows(
            period,
            orders,
            items,
            products,
            translations,
            reviews,
            payments,
            max_orders_per_period,
        )
        snapshot_ids = []
        entities_by_period[period] = {}
        for entity_type, rows in entity_rows.items():
            if not rows:
                continue
            result = import_standard_rows(
                tenant_id=tenant_id,
                entity_type=entity_type,
                source_name=f"olist:{period}:{entity_type}",
                rows=rows,
                mapping=_identity_mapping(rows),
                store=repository,
            )
            snapshot_ids.append(result["snapshot"]["snapshot_id"])
            entities_by_period[period][entity_type] = len(rows)
        snapshots_by_period[period] = snapshot_ids

    return {
        "tenant_id": tenant_id,
        "dataset": {
            "name": "Brazilian E-Commerce Public Dataset by Olist",
            "source_url": DATASET_URL,
            "license": DATASET_LICENSE,
            "real_fields": ["orders", "order_items", "products", "payments", "reviews"],
            "derived_fields": ["product_name", "review_product_attribution", "canceled_order_refund"],
            "simulated_fields": ["sku_id", "cost_price", "inventory_snapshot"],
        },
        "previous_period": previous_period,
        "current_period": current_period,
        "previous_snapshot_ids": snapshots_by_period[previous_period],
        "current_snapshot_ids": snapshots_by_period[current_period],
        "entity_row_counts": entities_by_period,
        "notes": [
            "真实、派生和模拟字段在结果中分开声明。",
            "模拟字段仅用于展示企业内部成本和库存场景，不作为 Olist 原始事实。",
            "默认每周期最多导入 5000 个订单，可通过参数调整。",
        ],
    }
