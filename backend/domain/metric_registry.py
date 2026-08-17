"""可版本化的电商指标语义目录。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


MetricCalculator = Callable[[dict[str, float]], float | None]


def _safe_divide(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


@dataclass(frozen=True)
class MetricDefinition:
    metric_key: str
    name: str
    description: str
    formula: str
    unit: str
    aggregation: str
    required_fields: tuple[str, ...]
    dimensions: tuple[str, ...]
    version: str = "1.0.0"
    time_field: str = ""
    calculator: MetricCalculator | None = field(default=None, repr=False, compare=False)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "key": self.metric_key,
            "name": self.name,
            "description": self.description,
            "formula": self.formula,
            "unit": self.unit,
            "aggregation": self.aggregation,
            "required_fields": list(self.required_fields),
            "dimensions": list(self.dimensions),
            "version": self.version,
            "time_field": self.time_field,
            "calculable": self.calculator is not None,
        }


class MetricRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, MetricDefinition] = {}

    def register(self, definition: MetricDefinition) -> None:
        if definition.metric_key in self._definitions:
            raise ValueError(f"指标已注册: {definition.metric_key}")
        self._definitions[definition.metric_key] = definition

    def get(self, metric_key: str) -> MetricDefinition:
        try:
            return self._definitions[metric_key]
        except KeyError as error:
            raise KeyError(f"指标不存在: {metric_key}") from error

    def list(self) -> list[MetricDefinition]:
        return sorted(self._definitions.values(), key=lambda item: item.metric_key)

    def calculate(self, metric_key: str, values: dict[str, float]) -> float | None:
        definition = self.get(metric_key)
        if definition.calculator is None:
            raise ValueError(f"指标不支持聚合值计算: {metric_key}")
        missing = [field for field in definition.required_fields if field not in values]
        if missing:
            raise ValueError(f"指标 {metric_key} 缺少字段: {', '.join(missing)}")
        result = definition.calculator(values)
        return round(result, 2) if result is not None else None

    def calculate_many(self, metric_keys: list[str], values: dict[str, float]) -> dict[str, float | None]:
        return {metric_key: self.calculate(metric_key, values) for metric_key in metric_keys}

    def catalog(self) -> dict[str, Any]:
        definitions = self.list()
        return {
            "schema_version": "1.0",
            "metric_count": len(definitions),
            "metrics": [definition.to_public_dict() for definition in definitions],
        }


def build_metric_registry() -> MetricRegistry:
    registry = MetricRegistry()
    common_dimensions = ("tenant_id", "shop_id", "platform", "channel", "category", "product_id", "sku_id")
    definitions = [
        MetricDefinition(
            metric_key="gmv",
            name="商品交易总额",
            description="统计周期内有效交易金额总和；订单粒度使用实付金额，商品粒度使用可归因明细成交额。",
            formula="SUM(valid_transaction_amount)",
            unit="currency",
            aggregation="sum",
            required_fields=("valid_transaction_amount",),
            dimensions=common_dimensions,
            time_field="paid_at",
        ),
        MetricDefinition(
            metric_key="paid_order_count",
            name="支付订单数",
            description="统计周期内完成支付且未取消的去重订单数。",
            formula="COUNT(DISTINCT order.order_id)",
            unit="count",
            aggregation="count_distinct",
            required_fields=("order_id", "order_status"),
            dimensions=common_dimensions,
            time_field="paid_at",
        ),
        MetricDefinition(
            metric_key="conversion_rate_pct",
            name="支付转化率",
            description="支付订单数占访客数的比例。",
            formula="orders / visitors * 100",
            unit="percent",
            aggregation="ratio",
            required_fields=("orders", "visitors"),
            dimensions=common_dimensions,
            calculator=lambda values: _safe_divide(values["orders"] * 100, values["visitors"]),
        ),
        MetricDefinition(
            metric_key="average_order_value",
            name="客单价",
            description="商品交易总额除以支付订单数。",
            formula="gmv / orders",
            unit="currency",
            aggregation="ratio",
            required_fields=("gmv", "orders"),
            dimensions=common_dimensions,
            calculator=lambda values: _safe_divide(values["gmv"], values["orders"]),
        ),
        MetricDefinition(
            metric_key="roas",
            name="广告投入产出比",
            description="广告归因成交额或约定口径 GMV 除以广告消耗。",
            formula="gmv / ad_spend",
            unit="ratio",
            aggregation="ratio",
            required_fields=("gmv", "ad_spend"),
            dimensions=common_dimensions + ("campaign_id",),
            calculator=lambda values: _safe_divide(values["gmv"], values["ad_spend"]),
        ),
        MetricDefinition(
            metric_key="refund_rate_pct",
            name="退款金额率",
            description="退款金额占商品交易总额的比例。",
            formula="refund_amount / gmv * 100",
            unit="percent",
            aggregation="ratio",
            required_fields=("refund_amount", "gmv"),
            dimensions=common_dimensions + ("refund_reason",),
            calculator=lambda values: _safe_divide(values["refund_amount"] * 100, values["gmv"]),
        ),
        MetricDefinition(
            metric_key="refund_order_rate_pct",
            name="退款订单率",
            description="退款或取消订单数占统计周期总订单数的比例。",
            formula="refund_orders / total_orders * 100",
            unit="percent",
            aggregation="ratio",
            required_fields=("refund_orders", "total_orders"),
            dimensions=common_dimensions + ("refund_reason",),
            calculator=lambda values: _safe_divide(values["refund_orders"] * 100, values["total_orders"]),
        ),
        MetricDefinition(
            metric_key="gross_profit",
            name="经营毛利",
            description="GMV 扣除商品成本、广告消耗和退款损失后的经营毛利。",
            formula="gmv - cost_of_goods - ad_spend - refund_amount",
            unit="currency",
            aggregation="derived",
            required_fields=("gmv", "cost_of_goods", "ad_spend", "refund_amount"),
            dimensions=common_dimensions,
            calculator=lambda values: values["gmv"] - values["cost_of_goods"] - values["ad_spend"] - values["refund_amount"],
        ),
        MetricDefinition(
            metric_key="gross_margin_pct",
            name="经营毛利率",
            description="经营毛利占 GMV 的比例。",
            formula="(gmv - cost_of_goods - ad_spend - refund_amount) / gmv * 100",
            unit="percent",
            aggregation="ratio",
            required_fields=("gmv", "cost_of_goods", "ad_spend", "refund_amount"),
            dimensions=common_dimensions,
            calculator=lambda values: _safe_divide(
                (values["gmv"] - values["cost_of_goods"] - values["ad_spend"] - values["refund_amount"]) * 100,
                values["gmv"],
            ),
        ),
        MetricDefinition(
            metric_key="sell_through_rate_pct",
            name="动销率",
            description="已售件数占已售件数与当前库存之和的比例。",
            formula="sold_units / (sold_units + stock_units) * 100",
            unit="percent",
            aggregation="ratio",
            required_fields=("sold_units", "stock_units"),
            dimensions=common_dimensions,
            calculator=lambda values: _safe_divide(
                values["sold_units"] * 100,
                values["sold_units"] + values["stock_units"],
            ),
        ),
        MetricDefinition(
            metric_key="gmv_growth_pct",
            name="GMV 增长率",
            description="当前周期 GMV 相对上一可比周期的变化率。",
            formula="(gmv - previous_gmv) / previous_gmv * 100",
            unit="percent",
            aggregation="period_comparison",
            required_fields=("gmv", "previous_gmv"),
            dimensions=common_dimensions,
            calculator=lambda values: _safe_divide(
                (values["gmv"] - values["previous_gmv"]) * 100,
                values["previous_gmv"],
            ),
        ),
        MetricDefinition(
            metric_key="inventory_days",
            name="库存可售天数",
            description="当前可售库存按近期平均日销量预计可销售的天数。",
            formula="available_stock / average_daily_sales",
            unit="days",
            aggregation="ratio",
            required_fields=("available_stock", "average_daily_sales"),
            dimensions=common_dimensions,
            calculator=lambda values: _safe_divide(values["available_stock"], values["average_daily_sales"]),
        ),
        MetricDefinition(
            metric_key="sold_units",
            name="成交件数",
            description="统计周期内有效订单明细的成交数量总和。",
            formula="SUM(order_item.quantity)",
            unit="count",
            aggregation="sum",
            required_fields=("quantity",),
            dimensions=common_dimensions,
        ),
        MetricDefinition(
            metric_key="available_stock",
            name="可售库存",
            description="当前库存快照中的可售库存数量。",
            formula="SUM(inventory_snapshot.available_stock)",
            unit="count",
            aggregation="sum",
            required_fields=("available_stock",),
            dimensions=common_dimensions,
            time_field="snapshot_at",
        ),
        MetricDefinition(
            metric_key="average_rating",
            name="平均评分",
            description="统计周期内商品评价评分的算术平均值。",
            formula="AVG(review.rating)",
            unit="score",
            aggregation="average",
            required_fields=("rating",),
            dimensions=common_dimensions,
            time_field="reviewed_at",
        ),
        MetricDefinition(
            metric_key="review_count",
            name="评价数",
            description="统计周期内商品评价记录数量。",
            formula="COUNT(review.review_id)",
            unit="count",
            aggregation="count",
            required_fields=("review_id",),
            dimensions=common_dimensions,
            time_field="reviewed_at",
        ),
    ]
    for definition in definitions:
        registry.register(definition)
    return registry


metric_registry = build_metric_registry()
