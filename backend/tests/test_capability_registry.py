import asyncio

from api.capability_routes import CapabilityExecuteRequest, capability_catalog, execute_capability
from capabilities import (
    AgentScenarioDefinition,
    DomainServiceDefinition,
    WorkflowDefinition,
    capability_registry,
)


def test_catalog_exposes_all_web_modules_and_runtime_boundaries():
    catalog = asyncio.run(capability_catalog())

    assert catalog["summary"]["module_count"] == 16
    assert catalog["principles"]["deterministic_first"] is True
    assert catalog["principles"]["agent_direct_side_effects"] is False
    assert {module["id"] for module in catalog["modules"]} >= {
        "operation_dashboard",
        "intelligent_query",
        "automation_center",
        "data_management",
    }


def test_definitions_use_distinct_runtime_types():
    assert isinstance(capability_registry.get_capability("analytics.store_metrics"), DomainServiceDefinition)
    assert isinstance(capability_registry.get_capability("workflow.low_stock_scan"), WorkflowDefinition)
    assert isinstance(capability_registry.get_capability("agent.nl2sql"), AgentScenarioDefinition)


def test_execute_domain_service_through_web_entrypoint():
    request = CapabilityExecuteRequest(inputs={
        "visitors": 1000,
        "orders": 50,
        "gmv": 5000,
        "ad_spend": 1000,
    })

    response = asyncio.run(execute_capability("analytics.store_metrics", request))

    assert response["runtime"] == "domain_service"
    assert response["result"]["metrics"]["conversion_rate_pct"] == 5
    assert response["result"]["metrics"]["roas"] == 5


def test_workflow_exposes_fixed_steps():
    definition = capability_registry.get_capability("workflow.daily_operating_report")
    public = definition.to_public_dict()

    assert public["runtime"] == "workflow"
    assert public["execution"]["steps"] == [
        "product_diagnosis",
        "build_daily_report",
        "create_alert",
    ]


def test_agent_scenario_only_declares_safe_tools():
    definition = capability_registry.get_capability("agent.nl2sql")
    public = definition.to_public_dict()

    assert public["runtime"] == "agent"
    assert "read_only_sql" in public["execution"]["guardrails"]
    assert public["execution_endpoint"] == "/api/query"
