from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import report_routes
from services.report_service import export_report_excel, generate_report
from services.report_store import ReportStore
from services.standard_data_store import StandardDataStore
from services.workflow_store import WorkflowStore


def _snapshot(store, tenant_id, entity_type, period, rows):
    return store.create_snapshot(
        tenant_id=tenant_id,
        entity_type=entity_type,
        source_name=f"report:{period}:{entity_type}",
        mapping={key: key for key in rows[0]},
        canonical_rows=rows,
        quality_score=100,
        quality_status="pass",
    )["snapshot_id"]


def _report_fixture(tmp_path):
    db_path = str(tmp_path / "reports.db")
    data_store = StandardDataStore(db_path)
    automation_store = WorkflowStore(db_path)
    repository = ReportStore(db_path)
    tenant_id = "report-tenant"
    product = [{"product_id": "P-1", "product_name": "商品", "category": "类目"}]
    sku = [{"sku_id": "S-1", "product_id": "P-1", "sku_name": "默认", "sale_price": 100, "cost_price": 50}]
    previous = {
        "product": product,
        "sku": sku,
        "order": [{"order_id": "O-P", "ordered_at": "2026-07-01", "paid_amount": 100, "order_status": "paid"}],
        "order_item": [{"order_item_id": "I-P", "order_id": "O-P", "product_id": "P-1", "sku_id": "S-1", "quantity": 1, "unit_price": 100}],
        "inventory_snapshot": [{"snapshot_at": "2026-07-31", "sku_id": "S-1", "available_stock": 10}],
    }
    current = {
        "product": product,
        "sku": sku,
        "order": [{"order_id": "O-C", "ordered_at": "2026-08-01", "paid_amount": 200, "order_status": "paid"}],
        "order_item": [{"order_item_id": "I-C", "order_id": "O-C", "product_id": "P-1", "sku_id": "S-1", "quantity": 2, "unit_price": 100}],
        "inventory_snapshot": [{"snapshot_at": "2026-08-31", "sku_id": "S-1", "available_stock": 20}],
    }
    previous_ids = [_snapshot(data_store, tenant_id, entity, "previous", rows) for entity, rows in previous.items()]
    current_ids = [_snapshot(data_store, tenant_id, entity, "current", rows) for entity, rows in current.items()]
    return data_store, automation_store, repository, tenant_id, current_ids, previous_ids


def test_report_generation_creates_versions_and_preserves_metric_evidence(tmp_path):
    data_store, automation_store, repository, tenant_id, current_ids, previous_ids = _report_fixture(tmp_path)

    first = generate_report(
        tenant_id,
        "monthly",
        "2026-08",
        current_ids,
        previous_ids,
        data_store=data_store,
        automation_store=automation_store,
        repository=repository,
    )
    second = generate_report(
        tenant_id,
        "monthly",
        "2026-08",
        current_ids,
        previous_ids,
        title="八月经营复盘",
        data_store=data_store,
        automation_store=automation_store,
        repository=repository,
    )

    assert first["report_id"] == second["report_id"]
    assert first["current_version"]["version"] == 1
    assert second["current_version"]["version"] == 2
    assert [item["version"] for item in second["versions"]] == [2, 1]
    content = second["current_version"]["content"]
    gmv = next(card for card in content["sections"]["kpis"] if card["metric_key"] == "gmv")
    assert gmv["value"] == 200
    assert gmv["previous_value"] == 100
    assert gmv["formula"] == "SUM(valid_transaction_amount)"
    assert set(content["evidence"]["snapshot_ids"]) == set([*current_ids, *previous_ids])


def test_llm_summary_failure_falls_back_to_structured_report(tmp_path):
    data_store, automation_store, repository, tenant_id, current_ids, previous_ids = _report_fixture(tmp_path)

    def unavailable_summary(_):
        raise RuntimeError("model unavailable")

    report = generate_report(
        tenant_id,
        "daily",
        "2026-08-31",
        current_ids,
        previous_ids,
        use_llm_summary=True,
        summary_provider=unavailable_summary,
        data_store=data_store,
        automation_store=automation_store,
        repository=repository,
    )
    content = report["current_version"]["content"]

    assert report["current_version"]["summary_mode"] == "deterministic"
    assert content["summary_mode"] == "deterministic"
    assert content["summary_fallback_reason"] == "model unavailable"
    assert "GMV" in content["executive_summary"]
    assert len(content["sections"]["kpis"]) == 8


def test_report_excel_and_analysis_history_are_available(tmp_path):
    data_store, automation_store, repository, tenant_id, current_ids, previous_ids = _report_fixture(tmp_path)
    report = generate_report(
        tenant_id,
        "weekly",
        "2026-W35",
        current_ids,
        previous_ids,
        data_store=data_store,
        automation_store=automation_store,
        repository=repository,
    )
    excel = export_report_excel(report)
    run = data_store.save_analysis_run(
        tenant_id,
        "test_analysis",
        {"question": "test"},
        {
            "conclusion": "ok",
            "evidence": [{
                "metric_key": "gmv",
                "metric_version": "1.0.0",
                "formula": "SUM(valid_transaction_amount)",
                "current_value": 200,
                "previous_value": 100,
                "contribution": 100,
                "source": {"snapshot_ids": current_ids},
            }],
        },
        current_ids,
    )

    assert excel[:2] == b"PK"
    assert data_store.list_analysis_runs(tenant_id)[0]["run_id"] == run["run_id"]
    detail = data_store.get_analysis_run(run["run_id"], tenant_id)
    assert detail["result"]["conclusion"] == "ok"
    assert detail["evidence"][0]["metric_version"] == "1.0.0"


def test_report_http_contracts(tmp_path, monkeypatch):
    data_store, automation_store, repository, tenant_id, current_ids, previous_ids = _report_fixture(tmp_path)

    def local_generate(**kwargs):
        return generate_report(
            **kwargs,
            data_store=data_store,
            automation_store=automation_store,
            repository=repository,
        )

    monkeypatch.setattr(report_routes, "generate_report", local_generate)
    monkeypatch.setattr(report_routes, "report_store", repository)
    monkeypatch.setattr(report_routes, "standard_data_store", data_store)
    app = FastAPI()
    app.include_router(report_routes.router, prefix="/api")
    client = TestClient(app)
    response = client.post("/api/reports/generate", json={
        "tenant_id": tenant_id,
        "report_type": "monthly",
        "period_key": "2026-08",
        "current_snapshot_ids": current_ids,
        "previous_snapshot_ids": previous_ids,
    })

    assert response.status_code == 200, response.text
    report_id = response.json()["report_id"]
    listing = client.get("/api/reports", params={"tenant_id": tenant_id})
    detail = client.get(f"/api/reports/{report_id}", params={"tenant_id": tenant_id})
    exported = client.get(f"/api/reports/{report_id}/export", params={"tenant_id": tenant_id})
    history = client.get("/api/analysis-runs", params={"tenant_id": tenant_id})

    assert listing.json()["reports"][0]["latest_version"] == 1
    assert detail.json()["current_version"]["content"]["report_type"] == "monthly"
    assert exported.status_code == 200
    assert exported.content[:2] == b"PK"
    assert history.json()["runs"] == []
