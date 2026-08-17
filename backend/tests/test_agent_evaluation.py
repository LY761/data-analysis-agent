from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import evaluation_routes
from eval.runner import run_agent_evaluation, save_evaluation_report


class FakeRouter:
    def route(self, question, conversation_history=None):
        mapping = {
            "你好": "chat",
            "查销售": "sql_query",
            "分析一下": "sql_query",
        }
        return {"mode": mapping[question], "reason": "fake"}


def test_agent_evaluation_reports_accuracy_latency_safety_and_limitations(tmp_path):
    cases = [
        {"id": 1, "question": "你好", "category": "intent", "expect_mode": "chat"},
        {"id": 2, "question": "查销售", "category": "basic", "expect_mode": "sql_query|quick_card"},
        {"id": 3, "question": "分析一下", "category": "intent", "expect_mode": "clarify"},
    ]

    report = run_agent_evaluation(router=FakeRouter(), cases=cases)
    output = save_evaluation_report(report, tmp_path / "agent_eval.json")

    assert report["routing"]["accuracy_pct"] == 66.67
    assert report["routing"]["passed_count"] == 2
    assert report["routing"]["latency_ms"]["p95"] is not None
    assert report["sql_safety"]["block_or_allow_accuracy_pct"] == 100
    assert report["token_usage"]["measured"] is False
    assert len(report["golden_scenarios"]) == 3
    assert output.exists()
    assert "内部评测" in output.read_text(encoding="utf-8")


def test_evaluation_http_exposes_report_and_golden_scenarios(monkeypatch):
    monkeypatch.setattr(
        evaluation_routes,
        "run_agent_evaluation",
        lambda cases: {"routing": {"case_count": len(cases)}, "sql_safety": {}},
    )
    app = FastAPI()
    app.include_router(evaluation_routes.router, prefix="/api")
    client = TestClient(app)

    report = client.post("/api/evaluations/agent/run", params={"limit": 2})
    scenarios = client.get("/api/evaluations/golden-scenarios")

    assert report.status_code == 200
    assert report.json()["routing"]["case_count"] == 2
    assert len(scenarios.json()["scenarios"]) == 3
