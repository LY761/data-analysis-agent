import asyncio

from api.integration_routes import StoreMetricsRequest, integration_store_metrics
from domain.ecommerce_metrics import compare_products
from mcp_server import server


def test_store_metrics_integration_envelope():
    request = StoreMetricsRequest(
        tenant_id="tenant-a",
        request_id="request-123",
        visitors=1000,
        orders=50,
        gmv=5000,
        ad_spend=1000,
    )

    response = asyncio.run(integration_store_metrics(request))

    assert response["schema_version"] == "1.0"
    assert response["tenant_id"] == "tenant-a"
    assert response["tool"] == "analytics.store_metrics"
    assert response["data"]["metrics"]["conversion_rate_pct"] == 5
    assert response["data"]["metrics"]["roas"] == 5


def test_product_comparison_is_deterministic():
    products = [
        {"name": "A", "price": 99, "sales": 100, "rating": 4.5, "review_count": 50, "gross_margin_pct": 20, "growth_rate_pct": 5},
        {"name": "B", "price": 89, "sales": 80, "rating": 4.8, "review_count": 90, "gross_margin_pct": 30, "growth_rate_pct": 20},
    ]

    first = compare_products(products)
    second = compare_products(products)

    assert first == second
    assert first["rankings"][0]["rank"] == 1


def test_mcp_exposes_core_tools():
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}

    assert names == {"analyze_store_metrics", "compare_products"}
