import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import data_product_routes
from domain.store_diagnosis import analyze_store_diagnosis
from services.standard_data_store import StandardDataStore
from services.standardization import confirm_mapping, import_standard_file, import_standard_rows, validate_mapping


ORDER_MAPPING = {
    "订单号": "order_id",
    "下单时间": "ordered_at",
    "实付金额": "paid_amount",
    "订单状态": "order_status",
}

ORDER_ROWS = [
    {"订单号": "A-1", "下单时间": "2026-08-01 10:00:00", "实付金额": 199, "订单状态": "已支付"},
    {"订单号": "A-2", "下单时间": "2026-08-01 11:00:00", "实付金额": 299, "订单状态": "已完成"},
]


def test_mapping_confirmation_and_standard_import_create_snapshot(tmp_path):
    store = StandardDataStore(str(tmp_path / "standard.db"))

    profile = confirm_mapping("tenant-a", "order", "平台订单模板", ORDER_MAPPING, store)
    result = import_standard_rows(
        tenant_id="tenant-a",
        entity_type="order",
        source_name="orders.csv",
        rows=ORDER_ROWS,
        mapping=ORDER_MAPPING,
        store=store,
    )

    snapshot = result["snapshot"]
    assert profile["mapping"] == ORDER_MAPPING
    assert snapshot["row_count"] == 2
    assert snapshot["quality_status"] == "pass"
    assert len(snapshot["content_hash"]) == 64
    assert store.get_snapshot_rows(snapshot["snapshot_id"])[0]["order_id"] == "A-1"

    connection = sqlite3.connect(store.db_path)
    row_count = connection.execute(
        "SELECT COUNT(*) FROM std_order WHERE _snapshot_id=?",
        (snapshot["snapshot_id"],),
    ).fetchone()[0]
    connection.close()
    assert row_count == 2


def test_failed_quality_gate_does_not_create_snapshot(tmp_path):
    store = StandardDataStore(str(tmp_path / "standard.db"))
    invalid_rows = [ORDER_ROWS[0], dict(ORDER_ROWS[0], 实付金额=-1)]

    with pytest.raises(ValueError, match="质量检查失败"):
        import_standard_rows(
            tenant_id="tenant-a",
            entity_type="order",
            source_name="invalid.csv",
            rows=invalid_rows,
            mapping=ORDER_MAPPING,
            store=store,
        )

    assert store.list_snapshots("tenant-a") == []


def test_mapping_confirmation_requires_all_standard_fields():
    with pytest.raises(ValueError, match="缺少必填标准字段"):
        validate_mapping("order", {"订单号": "order_id"})


def test_standard_file_import_uses_confirmed_mapping(tmp_path):
    store = StandardDataStore(str(tmp_path / "standard.db"))
    content = "订单号,下单时间,实付金额,订单状态\nA-1,2026-08-01 10:00:00,199,已支付\n".encode("utf-8")

    result = import_standard_file(
        tenant_id="tenant-a",
        entity_type="order",
        filename="orders.csv",
        content=content,
        mapping=ORDER_MAPPING,
        store=store,
    )

    assert result["snapshot"]["source_name"] == "orders.csv"
    assert result["snapshot"]["row_count"] == 1


def test_store_diagnosis_decomposition_reconciles_gmv_change():
    previous = {"visitors": 1000, "orders": 50, "gmv": 5000}
    current = {"visitors": 1100, "orders": 66, "gmv": 7260}

    result = analyze_store_diagnosis(current, previous, ["snapshot-current", "snapshot-previous"])

    assert result["severity"] == "info"
    assert result["decomposition_available"] is True
    assert round(sum(driver["contribution"] for driver in result["drivers"]), 2) == 2260
    assert {driver["driver"] for driver in result["drivers"]} == {
        "traffic",
        "conversion",
        "average_order_value",
    }
    assert all(evidence["metric_version"] == "1.0.0" for evidence in result["evidence"])


def test_analysis_run_persists_evidence(tmp_path):
    store = StandardDataStore(str(tmp_path / "standard.db"))
    previous = {"visitors": 1000, "orders": 50, "gmv": 5000}
    current = {"visitors": 900, "orders": 36, "gmv": 3240}
    diagnosis = analyze_store_diagnosis(current, previous)

    run = store.save_analysis_run(
        tenant_id="tenant-a",
        scenario="store_diagnosis",
        request={"current": current, "previous": previous},
        result=diagnosis,
        snapshot_ids=[],
    )

    connection = sqlite3.connect(store.db_path)
    evidence_count = connection.execute(
        "SELECT COUNT(*) FROM analysis_evidence WHERE run_id=?",
        (run["run_id"],),
    ).fetchone()[0]
    connection.close()
    assert run["status"] == "completed"
    assert evidence_count == len(diagnosis["evidence"])


def test_standard_import_and_diagnosis_http_flow(tmp_path, monkeypatch):
    store = StandardDataStore(str(tmp_path / "standard.db"))
    monkeypatch.setattr(data_product_routes, "standard_data_store", store)
    app = FastAPI()
    app.include_router(data_product_routes.router, prefix="/api")
    client = TestClient(app)

    import_response = client.post("/api/standardization/import", json={
        "tenant_id": "tenant-a",
        "entity_type": "order",
        "source_name": "orders.json",
        "rows": ORDER_ROWS,
        "mapping": ORDER_MAPPING,
    })
    assert import_response.status_code == 200, import_response.text
    snapshot_id = import_response.json()["snapshot"]["snapshot_id"]

    diagnosis_response = client.post("/api/diagnostics/store", json={
        "tenant_id": "tenant-a",
        "current": {"visitors": 1100, "orders": 66, "gmv": 7260},
        "previous": {"visitors": 1000, "orders": 50, "gmv": 5000},
        "snapshot_ids": [snapshot_id],
    })
    assert diagnosis_response.status_code == 200, diagnosis_response.text
    body = diagnosis_response.json()
    assert body["run"]["status"] == "completed"
    assert len(body["diagnosis"]["drivers"]) == 3
    assert body["diagnosis"]["snapshot_context"][0]["snapshot_id"] == snapshot_id


def test_store_diagnosis_can_aggregate_standard_snapshots(tmp_path, monkeypatch):
    store = StandardDataStore(str(tmp_path / "standard.db"))
    monkeypatch.setattr(data_product_routes, "standard_data_store", store)
    app = FastAPI()
    app.include_router(data_product_routes.router, prefix="/api")
    client = TestClient(app)

    def import_snapshot(entity_type, source_name, rows, mapping):
        response = client.post("/api/standardization/import", json={
            "tenant_id": "tenant-a",
            "entity_type": entity_type,
            "source_name": source_name,
            "rows": rows,
            "mapping": mapping,
        })
        assert response.status_code == 200, response.text
        return response.json()["snapshot"]["snapshot_id"]

    previous_orders = [
        {"订单号": f"P-{index}", "下单时间": "2026-07-01", "实付金额": 100, "订单状态": "已支付"}
        for index in range(50)
    ]
    current_orders = [
        {"订单号": f"C-{index}", "下单时间": "2026-08-01", "实付金额": 110, "订单状态": "已支付"}
        for index in range(66)
    ]
    traffic_mapping = {
        "日期": "stat_date",
        "店铺id": "shop_id",
        "访客": "visitors",
        "支付人数": "payers",
    }
    previous_order_snapshot = import_snapshot("order", "previous-orders", previous_orders, ORDER_MAPPING)
    current_order_snapshot = import_snapshot("order", "current-orders", current_orders, ORDER_MAPPING)
    previous_traffic_snapshot = import_snapshot(
        "traffic_daily",
        "previous-traffic",
        [{"日期": "2026-07-01", "店铺id": "S-1", "访客": 1000, "支付人数": 50}],
        traffic_mapping,
    )
    current_traffic_snapshot = import_snapshot(
        "traffic_daily",
        "current-traffic",
        [{"日期": "2026-08-01", "店铺id": "S-1", "访客": 1100, "支付人数": 66}],
        traffic_mapping,
    )

    response = client.post("/api/diagnostics/store/from-snapshots", json={
        "tenant_id": "tenant-a",
        "current_snapshot_ids": [current_order_snapshot, current_traffic_snapshot],
        "previous_snapshot_ids": [previous_order_snapshot, previous_traffic_snapshot],
    })

    assert response.status_code == 200, response.text
    diagnosis = response.json()["diagnosis"]
    assert diagnosis["snapshot_aggregation"]["current"]["values"]["gmv"] == 7260
    assert diagnosis["snapshot_aggregation"]["previous"]["values"]["visitors"] == 1000
    assert round(sum(driver["contribution"] for driver in diagnosis["drivers"]), 2) == 2260
