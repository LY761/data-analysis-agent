import asyncio

from api.semantic_routes import (
    DataQualityRequest,
    MappingSuggestRequest,
    MetricCalculateRequest,
    calculate_semantic_metrics,
    data_quality_check,
    semantic_mapping_suggest,
)
from domain.ecommerce_metrics import analyze_store_metrics
from domain.ecommerce_schema import entity_catalog, suggest_field_mapping
from domain.metric_registry import metric_registry
from services.data_ingest import inspect_file, parse_file


ORDER_ROWS = [
    {"订单号": "A-1", "下单时间": "2026-08-01 10:00:00", "实付金额": 199.0, "订单状态": "已支付"},
    {"订单号": "A-2", "下单时间": "2026-08-01 11:00:00", "实付金额": 299.0, "订单状态": "已完成"},
]


def test_standard_entity_catalog_covers_core_ecommerce_facts():
    catalog = entity_catalog()
    entity_keys = {entity["key"] for entity in catalog["entities"]}

    assert catalog["entity_count"] == 10
    assert {"order", "order_item", "traffic_daily", "ad_daily", "inventory_snapshot", "refund", "review"} <= entity_keys


def test_field_mapping_supports_chinese_platform_headers():
    result = suggest_field_mapping("order", list(ORDER_ROWS[0]))

    assert result["mapping"]["订单号"] == "order_id"
    assert result["mapping"]["下单时间"] == "ordered_at"
    assert result["mapping"]["实付金额"] == "paid_amount"
    assert result["missing_required_fields"] == []


def test_quality_check_passes_valid_orders_and_masks_customer_id():
    rows = [dict(ORDER_ROWS[0], 买家id="buyer-1"), dict(ORDER_ROWS[1], 买家id="buyer-2")]
    request = DataQualityRequest(entity_type="order", rows=rows)

    result = asyncio.run(data_quality_check(request))

    assert result["status"] == "pass"
    assert result["score"] == 100
    assert result["canonical_preview"][0]["customer_id"] == "***"


def test_quality_check_detects_duplicate_and_invalid_values():
    rows = [ORDER_ROWS[0], dict(ORDER_ROWS[0], 实付金额=-1)]
    result = asyncio.run(data_quality_check(DataQualityRequest(entity_type="order", rows=rows)))

    assert result["status"] == "fail"
    assert result["duplicate_primary_key_count"] == 1
    assert any(issue["code"] == "invalid_values" for issue in result["issues"])


def test_metric_registry_is_versioned_and_used_by_store_analysis():
    definition = metric_registry.get("gross_margin_pct")
    result = analyze_store_metrics(
        visitors=1000,
        orders=50,
        gmv=5000,
        ad_spend=1000,
        cost_of_goods=2000,
        refund_amount=500,
    )

    assert definition.version == "1.0.0"
    assert result["metrics"]["gross_margin_pct"] == 30
    assert result["metric_definitions"]["gross_margin_pct"]["version"] == "1.0.0"


def test_metric_calculation_api_uses_same_registry():
    request = MetricCalculateRequest(
        metric_keys=["conversion_rate_pct", "average_order_value"],
        values={"visitors": 1000, "orders": 50, "gmv": 5000},
    )

    result = asyncio.run(calculate_semantic_metrics(request))

    assert result["metrics"] == {"conversion_rate_pct": 5.0, "average_order_value": 100.0}


def test_refund_amount_rate_and_order_rate_are_distinct_metrics():
    amount_rate = metric_registry.calculate("refund_rate_pct", {"refund_amount": 100, "gmv": 1000})
    order_rate = metric_registry.calculate("refund_order_rate_pct", {"refund_orders": 2, "total_orders": 10})

    assert amount_rate == 10
    assert order_rate == 20
    assert metric_registry.get("refund_rate_pct").name == "退款金额率"
    assert metric_registry.get("refund_order_rate_pct").name == "退款订单率"


def test_mapping_api_and_file_inspection_share_contract():
    mapping = asyncio.run(semantic_mapping_suggest(MappingSuggestRequest(
        entity_type="order",
        source_fields=list(ORDER_ROWS[0]),
    )))
    content = "订单号,下单时间,实付金额,订单状态\nA-1,2026-08-01 10:00:00,199,已支付\n".encode("utf-8")
    parsed = parse_file("orders.csv", content)
    inspected = inspect_file("orders.csv", content, "order")

    assert mapping["missing_required_fields"] == []
    assert parsed["error"] is None
    assert inspected["quality"]["status"] == "pass"
