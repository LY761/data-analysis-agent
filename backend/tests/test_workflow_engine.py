import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import workflow_routes
from services.standard_data_store import StandardDataStore
from services.workflow_engine import WorkflowEngine, WorkflowExecutionError
from services.workflow_store import WorkflowStore


def _snapshot(store, tenant_id, entity_type, period, rows):
    return store.create_snapshot(
        tenant_id=tenant_id,
        entity_type=entity_type,
        source_name=f"workflow:{period}:{entity_type}",
        mapping={key: key for key in rows[0]},
        canonical_rows=rows,
        quality_score=100,
        quality_status="pass",
    )["snapshot_id"]


def _workflow_fixture(tmp_path):
    db_path = str(tmp_path / "workflow.db")
    data_store = StandardDataStore(db_path)
    workflow_store = WorkflowStore(db_path)
    engine = WorkflowEngine(workflow_store, data_store)
    tenant_id = "workflow-tenant"
    products = [{"product_id": "P-1", "product_name": "测试商品", "category": "测试"}]
    skus = [{"sku_id": "S-1", "product_id": "P-1", "sku_name": "默认", "sale_price": 100, "cost_price": 50}]
    previous = {
        "product": products,
        "sku": skus,
        "order": [{"order_id": "O-P", "ordered_at": "2026-07-01", "paid_amount": 200, "order_status": "paid"}],
        "order_item": [{"order_item_id": "I-P", "order_id": "O-P", "product_id": "P-1", "sku_id": "S-1", "quantity": 2, "unit_price": 100}],
        "inventory_snapshot": [{"snapshot_at": "2026-07-31", "sku_id": "S-1", "available_stock": 20}],
    }
    current = {
        "product": products,
        "sku": skus,
        "order": [{"order_id": "O-C", "ordered_at": "2026-08-01", "paid_amount": 100, "order_status": "paid"}],
        "order_item": [{"order_item_id": "I-C", "order_id": "O-C", "product_id": "P-1", "sku_id": "S-1", "quantity": 1, "unit_price": 100}],
        "inventory_snapshot": [{"snapshot_at": "2026-08-31", "sku_id": "S-1", "available_stock": 0}],
    }
    previous_ids = [
        _snapshot(data_store, tenant_id, entity_type, "previous", rows)
        for entity_type, rows in previous.items()
    ]
    current_ids = [
        _snapshot(data_store, tenant_id, entity_type, "current", rows)
        for entity_type, rows in current.items()
    ]
    inputs = {
        "business_date": "2026-08-31",
        "current_snapshot_ids": current_ids,
        "previous_snapshot_ids": previous_ids,
    }
    return engine, tenant_id, inputs


def test_builtin_workflows_create_alert_and_task_with_step_history(tmp_path):
    engine, tenant_id, inputs = _workflow_fixture(tmp_path)

    sales_run = engine.run(
        "sales_drop_alert",
        tenant_id,
        inputs,
        trigger_type="scheduled",
        idempotency_key="sales:2026-08-31",
    )
    stock_run = engine.run(
        "low_stock_task",
        tenant_id,
        inputs,
        idempotency_key="stock:2026-08-31",
    )

    assert sales_run["status"] == "completed"
    assert sales_run["output"]["results"]["sales_drop_condition"]["matched"] is True
    assert sales_run["output"]["results"]["sales_drop_alert"]["created"] is True
    assert [step["status"] for step in sales_run["steps"]] == ["completed"] * 3
    assert stock_run["output"]["results"]["replenishment_tasks"]["created_count"] == 1
    tasks = engine.store.list_tasks(tenant_id)
    assert len(tasks) == 1
    assert tasks[0]["payload"]["product_id"] == "P-1"


def test_idempotency_returns_original_run_and_prevents_duplicate_tasks(tmp_path):
    engine, tenant_id, inputs = _workflow_fixture(tmp_path)

    first = engine.run("low_stock_task", tenant_id, inputs, idempotency_key="same-trigger")
    duplicate = engine.run("low_stock_task", tenant_id, inputs, idempotency_key="same-trigger")
    rerun = engine.run("low_stock_task", tenant_id, inputs, idempotency_key="different-trigger")

    assert duplicate["run_id"] == first["run_id"]
    assert duplicate["deduplicated"] is True
    assert rerun["run_id"] != first["run_id"]
    assert rerun["output"]["results"]["replenishment_tasks"]["created_count"] == 0
    assert len(engine.store.list_tasks(tenant_id)) == 1


def test_failed_run_keeps_error_and_retry_creates_new_run(tmp_path):
    engine, tenant_id, _ = _workflow_fixture(tmp_path)
    attempts = {"count": 0}

    def flaky_step(context, step, inputs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary failure")
        return {"recovered": True}

    engine._handlers["build_daily_report"] = flaky_step
    definition = engine.create_definition(tenant_id, {
        "workflow_id": "retry-demo",
        "name": "重试演示",
        "description": "",
        "enabled": True,
        "triggers": ["manual"],
        "steps": [{"key": "flaky", "type": "build_daily_report", "params": {}}],
    })
    assert definition["built_in"] is False

    with pytest.raises(WorkflowExecutionError) as captured:
        engine.run("retry-demo", tenant_id, {}, idempotency_key="first-attempt")
    failed_run_id = captured.value.run_id
    failed = engine.get_run(failed_run_id, tenant_id)
    retried = engine.retry(failed_run_id, tenant_id)

    assert failed["status"] == "failed"
    assert failed["steps"][0]["error"] == "temporary failure"
    assert retried["status"] == "completed"
    assert retried["retry_of_run_id"] == failed_run_id
    assert retried["run_id"] != failed_run_id


def test_workflow_http_api_lists_runs_and_tasks(tmp_path, monkeypatch):
    engine, tenant_id, inputs = _workflow_fixture(tmp_path)
    monkeypatch.setattr(workflow_routes, "workflow_engine", engine)
    app = FastAPI()
    app.include_router(workflow_routes.router, prefix="/api")
    client = TestClient(app)

    catalog = client.get("/api/workflows", params={"tenant_id": tenant_id})
    assert catalog.status_code == 200
    assert {item["workflow_id"] for item in catalog.json()["workflows"]} >= {
        "daily_operating_report",
        "sales_drop_alert",
        "low_stock_task",
        "product_risk_review",
        "slow_moving_review",
    }

    response = client.post("/api/workflows/low_stock_task/run", json={
        "tenant_id": tenant_id,
        "idempotency_key": "api-low-stock",
        "inputs": inputs,
    })
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    detail = client.get(f"/api/workflow-runs/{run_id}", params={"tenant_id": tenant_id})
    tasks = client.get("/api/tasks", params={"tenant_id": tenant_id})
    assert detail.json()["status"] == "completed"
    assert len(tasks.json()["tasks"]) == 1


def test_product_risk_and_slow_moving_workflows_are_deterministic(tmp_path):
    engine, tenant_id, inputs = _workflow_fixture(tmp_path)

    risk_run = engine.run("product_risk_review", tenant_id, inputs)
    slow_run = engine.run("slow_moving_review", tenant_id, inputs)

    assert risk_run["output"]["results"]["risk_products"]["count"] == 1
    assert risk_run["output"]["results"]["risk_review_tasks"]["created_count"] == 1
    assert slow_run["output"]["results"]["slow_moving_products"]["count"] == 0
    assert slow_run["output"]["results"]["clearance_review_tasks"]["created_count"] == 0


def test_workflow_tables_are_created_in_standard_database(tmp_path):
    engine, _, _ = _workflow_fixture(tmp_path)
    engine.initialize()
    connection = sqlite3.connect(engine.store.db_path)
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    connection.close()

    assert {
        "workflow_definitions",
        "workflow_runs",
        "workflow_step_runs",
        "alert_events",
        "tasks",
    } <= tables
