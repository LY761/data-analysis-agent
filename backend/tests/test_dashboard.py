from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import dashboard_routes
from services.dashboard_service import build_dashboard
from services.standard_data_store import StandardDataStore
from services.workflow_engine import WorkflowEngine
from services.workflow_store import WorkflowStore


def _snapshot(store, tenant_id, entity_type, period, rows):
    return store.create_snapshot(
        tenant_id=tenant_id,
        entity_type=entity_type,
        source_name=f"dashboard:{period}:{entity_type}",
        mapping={key: key for key in rows[0]},
        canonical_rows=rows,
        quality_score=100,
        quality_status="pass",
    )["snapshot_id"]


def _dashboard_fixture(tmp_path):
    db_path = str(tmp_path / "dashboard.db")
    data_store = StandardDataStore(db_path)
    automation_store = WorkflowStore(db_path)
    engine = WorkflowEngine(automation_store, data_store)
    tenant_id = "dashboard-tenant"
    products = [
        {"product_id": "P-1", "product_name": "耳机", "category": "数码"},
        {"product_id": "P-2", "product_name": "水杯", "category": "家居"},
    ]
    skus = [
        {"sku_id": "S-1", "product_id": "P-1", "sku_name": "黑色", "sale_price": 100, "cost_price": 50},
        {"sku_id": "S-2", "product_id": "P-2", "sku_name": "白色", "sale_price": 100, "cost_price": 40},
    ]
    previous = {
        "product": products,
        "sku": skus,
        "order": [
            {"order_id": "P-O1", "ordered_at": "2026-07-01", "paid_amount": 200, "order_status": "paid"},
            {"order_id": "P-O2", "ordered_at": "2026-07-02", "paid_amount": 100, "order_status": "paid"},
        ],
        "order_item": [
            {"order_item_id": "P-I1", "order_id": "P-O1", "product_id": "P-1", "sku_id": "S-1", "quantity": 2, "unit_price": 100},
            {"order_item_id": "P-I2", "order_id": "P-O2", "product_id": "P-2", "sku_id": "S-2", "quantity": 1, "unit_price": 100},
        ],
        "traffic_daily": [
            {"stat_date": "2026-07-01", "shop_id": "SHOP", "product_id": "P-1", "visitors": 100, "payers": 1},
            {"stat_date": "2026-07-01", "shop_id": "SHOP", "product_id": "P-2", "visitors": 100, "payers": 1},
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
            {"order_id": "C-O1", "ordered_at": "2026-08-01", "paid_amount": 100, "order_status": "paid"},
            {"order_id": "C-O2", "ordered_at": "2026-08-02", "paid_amount": 300, "order_status": "paid"},
        ],
        "order_item": [
            {"order_item_id": "C-I1", "order_id": "C-O1", "product_id": "P-1", "sku_id": "S-1", "quantity": 1, "unit_price": 100},
            {"order_item_id": "C-I2", "order_id": "C-O2", "product_id": "P-2", "sku_id": "S-2", "quantity": 3, "unit_price": 100},
        ],
        "traffic_daily": [
            {"stat_date": "2026-08-01", "shop_id": "SHOP", "product_id": "P-1", "visitors": 100, "payers": 1},
            {"stat_date": "2026-08-01", "shop_id": "SHOP", "product_id": "P-2", "visitors": 200, "payers": 1},
        ],
        "inventory_snapshot": [
            {"snapshot_at": "2026-08-31", "sku_id": "S-1", "available_stock": 0},
            {"snapshot_at": "2026-08-31", "sku_id": "S-2", "available_stock": 30},
        ],
    }
    previous_ids = [_snapshot(data_store, tenant_id, entity, "previous", rows) for entity, rows in previous.items()]
    current_ids = [_snapshot(data_store, tenant_id, entity, "current", rows) for entity, rows in current.items()]
    inputs = {
        "business_date": "2026-08-31",
        "current_snapshot_ids": current_ids,
        "previous_snapshot_ids": previous_ids,
    }
    return data_store, automation_store, engine, tenant_id, current_ids, previous_ids, inputs


def test_dashboard_unifies_cards_trends_anomalies_and_tasks(tmp_path):
    data_store, automation_store, engine, tenant_id, current_ids, previous_ids, inputs = _dashboard_fixture(tmp_path)
    engine.run("daily_operating_report", tenant_id, inputs, idempotency_key="daily")
    engine.run("low_stock_task", tenant_id, inputs, idempotency_key="stock")

    dashboard = build_dashboard(
        tenant_id,
        current_ids,
        previous_ids,
        targets={"gmv": 1000},
        current_label="2026-08",
        previous_label="2026-07",
        data_store=data_store,
        automation_store=automation_store,
    )

    cards = {card["metric_key"]: card for card in dashboard["overview"]["cards"]}
    assert cards["gmv"]["value"] == 400
    assert cards["gmv"]["previous_value"] == 300
    assert cards["gmv"]["change_pct"] == 33.33
    assert cards["gmv"]["target_completion_pct"] == 40
    assert cards["gmv"]["metric_version"] == "1.0.0"
    assert dashboard["trends"]["series"]["gmv"] == [300, 400]
    assert dashboard["overview"]["pending_task_count"] == 1
    assert dashboard["overview"]["open_alert_count"] == 1
    assert any(item["product_id"] == "P-1" for item in dashboard["anomalies"] if item["type"] == "product_risk")


def test_dashboard_category_filter_and_no_data_reason(tmp_path):
    data_store, automation_store, _, tenant_id, current_ids, previous_ids, _ = _dashboard_fixture(tmp_path)

    digital = build_dashboard(
        tenant_id,
        current_ids,
        previous_ids,
        filters={"category": "数码"},
        data_store=data_store,
        automation_store=automation_store,
    )
    missing = build_dashboard(
        tenant_id,
        current_ids,
        previous_ids,
        filters={"category": "不存在"},
        data_store=data_store,
        automation_store=automation_store,
    )

    digital_cards = {card["metric_key"]: card for card in digital["overview"]["cards"]}
    missing_cards = {card["metric_key"]: card for card in missing["overview"]["cards"]}
    assert digital["overview"]["product_count"] == 1
    assert digital_cards["gmv"]["value"] == 100
    assert missing["overview"]["product_count"] == 0
    assert missing_cards["gmv"]["status"] == "unavailable"
    assert "缺少" in missing_cards["gmv"]["unavailable_reason"]


def test_dashboard_http_contracts(tmp_path, monkeypatch):
    data_store, automation_store, _, tenant_id, current_ids, previous_ids, _ = _dashboard_fixture(tmp_path)

    def local_dashboard(*args, **kwargs):
        return build_dashboard(*args, **kwargs, data_store=data_store, automation_store=automation_store)

    monkeypatch.setattr(dashboard_routes, "build_dashboard", local_dashboard)
    monkeypatch.setattr(dashboard_routes, "workflow_store", automation_store)
    app = FastAPI()
    app.include_router(dashboard_routes.router, prefix="/api")
    client = TestClient(app)
    params = [("tenant_id", tenant_id), ("gmv_target", "1000")]
    params.extend(("current_snapshot_ids", snapshot_id) for snapshot_id in current_ids)
    params.extend(("previous_snapshot_ids", snapshot_id) for snapshot_id in previous_ids)

    overview = client.get("/api/dashboard/overview", params=params)
    workbench = client.get("/api/dashboard/workbench", params=[*params, ("anomaly_limit", "1")])
    trends = client.get("/api/dashboard/trends", params=params)
    anomalies = client.get("/api/dashboard/anomalies", params=params)
    tasks = client.get("/api/dashboard/tasks", params={"tenant_id": tenant_id})

    assert overview.status_code == 200, overview.text
    assert len(overview.json()["overview"]["cards"]) == 8
    assert workbench.status_code == 200, workbench.text
    assert len(workbench.json()["anomalies"]) <= 1
    assert workbench.json()["anomaly_count"] >= len(workbench.json()["anomalies"])
    assert trends.json()["series"]["gmv"] == [300, 400]
    assert "anomalies" in anomalies.json()
    assert tasks.json()["tasks"] == []
