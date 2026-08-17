import csv
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import data_product_routes
from capabilities import capability_registry
from services.olist_adapter import import_olist_demo
from services.product_snapshot_analysis import analyze_product_snapshots
from services.standard_data_store import StandardDataStore


def _snapshot(store, tenant_id, entity_type, period, rows):
    return store.create_snapshot(
        tenant_id=tenant_id,
        entity_type=entity_type,
        source_name=f"test:{period}:{entity_type}",
        mapping={key: key for key in rows[0]},
        canonical_rows=rows,
        quality_score=100,
        quality_status="pass",
    )["snapshot_id"]


def _product_snapshots(store):
    tenant_id = "tenant-products"
    products = [
        {"product_id": "P-1", "product_name": "耳机", "category": "数码", "brand": "A"},
        {"product_id": "P-2", "product_name": "键盘", "category": "数码", "brand": "B"},
    ]
    skus = [
        {"sku_id": "S-1", "product_id": "P-1", "sku_name": "黑色", "sale_price": 100, "cost_price": 50},
        {"sku_id": "S-2", "product_id": "P-2", "sku_name": "标准版", "sale_price": 50, "cost_price": 20},
    ]
    previous = {
        "product": products,
        "sku": skus,
        "order": [
            {"order_id": "O-P1", "ordered_at": "2026-07-01", "paid_amount": 200, "order_status": "paid"},
            {"order_id": "O-P2", "ordered_at": "2026-07-30", "paid_amount": 50, "order_status": "paid"},
        ],
        "order_item": [
            {"order_item_id": "I-P1", "order_id": "O-P1", "product_id": "P-1", "sku_id": "S-1", "quantity": 2, "unit_price": 100},
            {"order_item_id": "I-P2", "order_id": "O-P2", "product_id": "P-2", "sku_id": "S-2", "quantity": 1, "unit_price": 50},
        ],
        "inventory_snapshot": [
            {"snapshot_at": "2026-07-31", "sku_id": "S-1", "available_stock": 20},
            {"snapshot_at": "2026-07-31", "sku_id": "S-2", "available_stock": 20},
        ],
    }
    current = {
        "product": products,
        "sku": skus,
        "order": [
            {"order_id": "O-C1", "ordered_at": "2026-08-01", "paid_amount": 100, "order_status": "paid"},
            {"order_id": "O-C2", "ordered_at": "2026-08-30", "paid_amount": 150, "order_status": "paid"},
        ],
        "order_item": [
            {"order_item_id": "I-C1", "order_id": "O-C1", "product_id": "P-1", "sku_id": "S-1", "quantity": 1, "unit_price": 100},
            {"order_item_id": "I-C2", "order_id": "O-C2", "product_id": "P-2", "sku_id": "S-2", "quantity": 3, "unit_price": 50},
        ],
        "inventory_snapshot": [
            {"snapshot_at": "2026-08-31", "sku_id": "S-1", "available_stock": 100, "stock_age_days": 100},
            {"snapshot_at": "2026-08-31", "sku_id": "S-2", "available_stock": 0, "stock_age_days": 3},
        ],
        "refund": [
            {"refund_id": "R-1", "order_id": "O-C1", "sku_id": "S-1", "refund_amount": 20, "refund_status": "success", "created_at": "2026-08-02"},
        ],
        "review": [
            {"review_id": "V-1", "product_id": "P-1", "sku_id": "S-1", "rating": 3, "reviewed_at": "2026-08-03"},
            {"review_id": "V-2", "product_id": "P-2", "sku_id": "S-2", "rating": 5, "reviewed_at": "2026-08-04"},
        ],
    }
    previous_ids = [
        _snapshot(store, tenant_id, entity_type, "previous", rows)
        for entity_type, rows in previous.items()
    ]
    current_ids = [
        _snapshot(store, tenant_id, entity_type, "current", rows)
        for entity_type, rows in current.items()
    ]
    return tenant_id, current_ids, previous_ids


def test_product_snapshot_diagnosis_builds_profiles_tags_and_contributions(tmp_path):
    store = StandardDataStore(str(tmp_path / "standard.db"))
    tenant_id, current_ids, previous_ids = _product_snapshots(store)

    result = analyze_product_snapshots(
        tenant_id,
        current_ids,
        previous_ids,
        store=store,
    )

    profiles = {profile["product_id"]: profile for profile in result["profiles"]}
    first = profiles["P-1"]
    second = profiles["P-2"]
    assert first["metrics"]["gmv"] == 100
    assert first["metrics"]["gmv_growth_pct"] == -50
    assert {tag["key"] for tag in first["tags"]} >= {"slow_moving", "risk"}
    assert {tag["key"] for tag in second["tags"]} >= {"potential", "profit", "stockout_risk"}
    assert result["decline_contributors"][0]["product_id"] == "P-1"
    assert result["decline_contributors"][0]["contribution_to_decline_pct"] == 100
    assert first["sku_decline_contributors"][0]["sku_id"] == "S-1"
    assert all(item["metric_version"] == "1.0.0" for item in result["evidence"])


def test_product_diagnosis_api_persists_evidence_and_supports_profile(tmp_path, monkeypatch):
    store = StandardDataStore(str(tmp_path / "standard.db"))
    tenant_id, current_ids, previous_ids = _product_snapshots(store)
    monkeypatch.setattr(data_product_routes, "standard_data_store", store)
    app = FastAPI()
    app.include_router(data_product_routes.router, prefix="/api")
    client = TestClient(app)

    response = client.post("/api/diagnostics/products", json={
        "tenant_id": tenant_id,
        "current_snapshot_ids": current_ids,
        "previous_snapshot_ids": previous_ids,
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["status"] == "completed"
    connection = sqlite3.connect(store.db_path)
    evidence_count = connection.execute(
        "SELECT COUNT(*) FROM analysis_evidence WHERE run_id=?",
        (body["run"]["run_id"],),
    ).fetchone()[0]
    connection.close()
    assert evidence_count == len(body["diagnosis"]["evidence"])

    params = [("tenant_id", tenant_id)]
    params.extend(("current_snapshot_ids", snapshot_id) for snapshot_id in current_ids)
    params.extend(("previous_snapshot_ids", snapshot_id) for snapshot_id in previous_ids)
    profile_response = client.get("/api/products/P-1/profile", params=params)
    assert profile_response.status_code == 200, profile_response.text
    assert profile_response.json()["profile"]["product_name"] == "耳机"


def _write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_olist_adapter_marks_simulated_fields_and_creates_diagnostic_snapshots(tmp_path):
    _write_csv(tmp_path / "olist_orders_dataset.csv", [
        {"order_id": "O-1", "customer_id": "C-1", "order_status": "delivered", "order_purchase_timestamp": "2017-01-10 10:00:00", "order_approved_at": "2017-01-10 11:00:00"},
        {"order_id": "O-2", "customer_id": "C-2", "order_status": "delivered", "order_purchase_timestamp": "2017-02-10 10:00:00", "order_approved_at": "2017-02-10 11:00:00"},
    ])
    _write_csv(tmp_path / "olist_order_items_dataset.csv", [
        {"order_id": "O-1", "order_item_id": "1", "product_id": "P-1", "price": "100", "freight_value": "10"},
        {"order_id": "O-2", "order_item_id": "1", "product_id": "P-1", "price": "120", "freight_value": "10"},
    ])
    _write_csv(tmp_path / "olist_products_dataset.csv", [
        {"product_id": "P-1", "product_category_name": "audio"},
    ])
    _write_csv(tmp_path / "olist_order_reviews_dataset.csv", [
        {"review_id": "R-1", "order_id": "O-1", "review_score": "4", "review_comment_message": "good", "review_creation_date": "2017-01-15"},
        {"review_id": "R-2", "order_id": "O-2", "review_score": "5", "review_comment_message": "great", "review_creation_date": "2017-02-15"},
        {"review_id": "R-2", "order_id": "O-2", "review_score": "5", "review_comment_message": "great", "review_creation_date": "2017-02-15"},
    ])
    _write_csv(tmp_path / "olist_order_payments_dataset.csv", [
        {"order_id": "O-1", "payment_value": "110"},
        {"order_id": "O-2", "payment_value": "130"},
    ])
    _write_csv(tmp_path / "product_category_name_translation.csv", [
        {"product_category_name": "audio", "product_category_name_english": "audio"},
    ])
    store = StandardDataStore(str(tmp_path / "olist.db"))

    imported = import_olist_demo(tmp_path, max_orders_per_period=None, store=store)
    repeated = import_olist_demo(tmp_path, max_orders_per_period=None, store=store)
    diagnosis = analyze_product_snapshots(
        imported["tenant_id"],
        imported["current_snapshot_ids"],
        imported["previous_snapshot_ids"],
        store=store,
    )

    assert imported["dataset"]["source_url"].startswith("https://www.kaggle.com/")
    assert "cost_price" in imported["dataset"]["simulated_fields"]
    assert imported["previous_period"] == "2017-01"
    assert imported["current_period"] == "2017-02"
    assert imported["current_snapshot_ids"] == repeated["current_snapshot_ids"]
    assert imported["entity_row_counts"]["2017-02"]["review"] == 1
    assert diagnosis["profiles"][0]["metrics"]["gmv"] == 120
    assert diagnosis["profiles"][0]["metrics"]["average_rating"] == 5


def test_product_diagnosis_is_registered_as_domain_service():
    definition = capability_registry.get_capability("analytics.product_diagnosis")

    assert definition.runtime.value == "domain_service"
    assert definition.executable is True
    assert "evidence" in definition.to_public_dict()["execution"]["output_fields"]
