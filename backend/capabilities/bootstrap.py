"""注册现有能力与 Web 首期模块。"""

from __future__ import annotations

from typing import Any

from capabilities.models import (
    AgentScenarioDefinition,
    Availability,
    DomainServiceDefinition,
    MaturityLevel,
    ModuleDefinition,
    RiskLevel,
    RuntimeKind,
    WorkflowDefinition,
)
from capabilities.registry import CapabilityRegistry
from domain.ecommerce_metrics import analyze_store_metrics, compare_products


def _store_metrics(inputs: dict[str, Any]) -> dict:
    return analyze_store_metrics(**inputs)


def _compare_products(inputs: dict[str, Any]) -> dict:
    return compare_products(inputs.get("products", []))


def _product_diagnosis(inputs: dict[str, Any]) -> dict:
    from services.product_snapshot_analysis import analyze_product_snapshots

    return analyze_product_snapshots(
        tenant_id=str(inputs.get("tenant_id", "default")),
        current_snapshot_ids=list(inputs.get("current_snapshot_ids", [])),
        previous_snapshot_ids=list(inputs.get("previous_snapshot_ids", [])),
        product_ids=list(inputs.get("product_ids", [])),
        ranking_weights=inputs.get("ranking_weights"),
        thresholds=inputs.get("thresholds"),
    )


def _data_quality_profile(inputs: dict[str, Any]) -> dict:
    from services.data_quality import check_data_quality

    return check_data_quality(
        str(inputs.get("entity_type", "")),
        inputs.get("rows", []),
        inputs.get("mapping"),
    )


def _store_diagnosis(inputs: dict[str, Any]) -> dict:
    from domain.store_diagnosis import analyze_store_diagnosis

    return analyze_store_diagnosis(
        current=inputs.get("current", {}),
        previous=inputs.get("previous", {}),
        snapshot_ids=inputs.get("snapshot_ids", []),
    )


def _dashboard_overview(inputs: dict[str, Any]) -> dict:
    from services.dashboard_service import build_dashboard

    return build_dashboard(
        tenant_id=str(inputs.get("tenant_id", "default")),
        current_snapshot_ids=list(inputs.get("current_snapshot_ids", [])),
        previous_snapshot_ids=list(inputs.get("previous_snapshot_ids", [])),
        filters=inputs.get("filters"),
        targets=inputs.get("targets"),
        current_label=str(inputs.get("current_label", "current")),
        previous_label=str(inputs.get("previous_label", "previous")),
    )


def _generate_report(inputs: dict[str, Any]) -> dict:
    from services.report_service import generate_report

    return generate_report(
        tenant_id=str(inputs.get("tenant_id", "default")),
        report_type=str(inputs.get("report_type", "daily")),
        period_key=str(inputs.get("period_key", "")),
        current_snapshot_ids=list(inputs.get("current_snapshot_ids", [])),
        previous_snapshot_ids=list(inputs.get("previous_snapshot_ids", [])),
        title=str(inputs.get("title", "")),
        filters=inputs.get("filters"),
        targets=inputs.get("targets"),
        use_llm_summary=bool(inputs.get("use_llm_summary", False)),
    )


def _run_builtin_workflow(workflow_id: str, inputs: dict[str, Any]) -> dict:
    from services.workflow_engine import workflow_engine

    execution_inputs = dict(inputs)
    tenant_id = str(execution_inputs.pop("tenant_id", "default"))
    trigger_type = str(execution_inputs.pop("trigger_type", "manual"))
    idempotency_key = str(execution_inputs.pop("idempotency_key", ""))
    return workflow_engine.run(
        workflow_id,
        tenant_id,
        execution_inputs,
        trigger_type=trigger_type,
        idempotency_key=idempotency_key,
    )


def _sales_anomaly_scan(inputs: dict[str, Any]) -> dict:
    return _run_builtin_workflow("sales_drop_alert", inputs)


def _low_stock_scan(inputs: dict[str, Any]) -> dict:
    return _run_builtin_workflow("low_stock_task", inputs)


def _daily_operating_report(inputs: dict[str, Any]) -> dict:
    return _run_builtin_workflow("daily_operating_report", inputs)


def _product_risk_review(inputs: dict[str, Any]) -> dict:
    return _run_builtin_workflow("product_risk_review", inputs)


def _slow_moving_review(inputs: dict[str, Any]) -> dict:
    return _run_builtin_workflow("slow_moving_review", inputs)


def _register_capabilities(registry: CapabilityRegistry) -> None:
    definitions = [
        DomainServiceDefinition(
            capability_id="analytics.store_metrics",
            name="店铺经营指标",
            category="operation_dashboard",
            description="确定性计算店铺经营指标、风险和建议。",
            maturity=MaturityLevel.L1,
            availability=Availability.AVAILABLE,
            input_fields=("visitors", "orders", "gmv", "ad_spend", "cost_of_goods", "refund_amount"),
            output_fields=("metrics", "diagnostics", "recommendations"),
            handler=_store_metrics,
        ),
        DomainServiceDefinition(
            capability_id="analytics.compare_products",
            name="商品竞争力对比",
            category="product_operations",
            description="对多个商品做可解释的确定性横向比较。",
            maturity=MaturityLevel.L1,
            availability=Availability.AVAILABLE,
            input_fields=("products",),
            output_fields=("rankings", "leaders", "weights"),
            handler=_compare_products,
        ),
        DomainServiceDefinition(
            capability_id="analytics.product_diagnosis",
            name="商品与 SKU 经营诊断",
            category="product_operations",
            description="从标准快照聚合商品画像，生成确定性标签、排名、SKU 下钻和下滑贡献证据。",
            maturity=MaturityLevel.L1,
            availability=Availability.AVAILABLE,
            input_fields=(
                "tenant_id",
                "current_snapshot_ids",
                "previous_snapshot_ids",
                "product_ids",
                "ranking_weights",
                "thresholds",
            ),
            output_fields=("summary", "profiles", "rankings", "decline_contributors", "evidence"),
            handler=_product_diagnosis,
        ),
        DomainServiceDefinition(
            capability_id="analytics.store_diagnosis",
            name="店铺经营诊断",
            category="operation_dashboard",
            description="确定性拆解 GMV 的流量、转化率和客单价贡献并生成证据链。",
            maturity=MaturityLevel.L1,
            availability=Availability.AVAILABLE,
            input_fields=("current", "previous", "snapshot_ids"),
            output_fields=("conclusion", "drivers", "evidence", "recommendations"),
            handler=_store_diagnosis,
        ),
        DomainServiceDefinition(
            capability_id="analytics.dashboard_overview",
            name="经营驾驶舱聚合",
            category="operation_dashboard",
            description="统一聚合指标卡、周期趋势、商品风险、工作流告警和待办任务。",
            maturity=MaturityLevel.L2,
            availability=Availability.AVAILABLE,
            input_fields=("tenant_id", "current_snapshot_ids", "previous_snapshot_ids", "filters", "targets"),
            output_fields=("overview", "trends", "products", "anomalies", "tasks", "data_availability"),
            handler=_dashboard_overview,
        ),
        DomainServiceDefinition(
            capability_id="analytics.report_generate",
            name="版本化经营报告",
            category="report_alert",
            description="基于驾驶舱事实生成可降级、可追溯和可导出的经营报告版本。",
            maturity=MaturityLevel.L2,
            availability=Availability.AVAILABLE,
            input_fields=(
                "tenant_id",
                "report_type",
                "period_key",
                "current_snapshot_ids",
                "previous_snapshot_ids",
                "filters",
                "targets",
                "use_llm_summary",
            ),
            output_fields=("report_id", "current_version", "versions"),
            handler=_generate_report,
        ),
        DomainServiceDefinition(
            capability_id="analytics.conversion_funnel",
            name="流量转化漏斗",
            category="traffic_conversion",
            description="计算展现、点击、访问、加购、下单和支付漏斗。",
            maturity=MaturityLevel.L0,
            availability=Availability.PLANNED,
        ),
        DomainServiceDefinition(
            capability_id="analytics.ad_performance",
            name="广告投放诊断",
            category="advertising",
            description="按计划、素材、人群和关键词诊断投放效率。",
            maturity=MaturityLevel.L0,
            availability=Availability.PLANNED,
        ),
        DomainServiceDefinition(
            capability_id="analytics.customer_rfm",
            name="客户 RFM 分群",
            category="customer_membership",
            description="生成客户价值分群和生命周期标签。",
            maturity=MaturityLevel.L0,
            availability=Availability.PLANNED,
        ),
        DomainServiceDefinition(
            capability_id="analytics.merchandising_associations",
            name="关联购买推荐",
            category="merchandising",
            description="基于订单明细生成关联购和组合销售建议。",
            maturity=MaturityLevel.L0,
            availability=Availability.PLANNED,
        ),
        DomainServiceDefinition(
            capability_id="data.quality_profile",
            name="电商数据质量检查",
            category="data_management",
            description="检查字段映射、缺失、重复、时间和金额质量。",
            maturity=MaturityLevel.L1,
            availability=Availability.AVAILABLE,
            input_fields=("entity_type", "rows", "mapping"),
            output_fields=("mapping", "score", "status", "issues", "field_stats"),
            handler=_data_quality_profile,
        ),
        WorkflowDefinition(
            capability_id="workflow.sales_anomaly_scan",
            name="销售异常扫描",
            category="report_alert",
            description="扫描销售额异常并生成站内告警数据。",
            maturity=MaturityLevel.L2,
            availability=Availability.AVAILABLE,
            triggers=("scheduled", "manual", "metric_threshold"),
            steps=("store_diagnosis", "evaluate_sales_drop", "create_alert"),
            handler=_sales_anomaly_scan,
        ),
        WorkflowDefinition(
            capability_id="workflow.low_stock_scan",
            name="低库存任务",
            category="inventory_supply",
            description="扫描低库存商品并生成任务候选。",
            maturity=MaturityLevel.L2,
            availability=Availability.AVAILABLE,
            risk_level=RiskLevel.INTERNAL_ACTION,
            triggers=("scheduled", "manual", "inventory_changed"),
            steps=("product_diagnosis", "select_low_stock", "create_tasks"),
            handler=_low_stock_scan,
        ),
        WorkflowDefinition(
            capability_id="workflow.daily_operating_report",
            name="运营日报",
            category="report_alert",
            description="按固定步骤生成经营日报。",
            maturity=MaturityLevel.L2,
            availability=Availability.AVAILABLE,
            triggers=("scheduled", "manual"),
            steps=("product_diagnosis", "build_daily_report", "create_alert"),
            handler=_daily_operating_report,
        ),
        WorkflowDefinition(
            capability_id="workflow.product_risk_review",
            name="商品风险复核",
            category="aftersales_voc",
            description="扫描高退款、低评分或明显下滑商品并创建内部复核任务。",
            maturity=MaturityLevel.L2,
            availability=Availability.AVAILABLE,
            risk_level=RiskLevel.INTERNAL_ACTION,
            triggers=("scheduled", "manual", "data_refresh_completed"),
            steps=("product_diagnosis", "select_tagged_products", "create_tasks"),
            handler=_product_risk_review,
        ),
        WorkflowDefinition(
            capability_id="workflow.slow_moving_review",
            name="滞销清理复核",
            category="inventory_supply",
            description="扫描滞销商品并创建清仓或暂停补货的内部复核任务。",
            maturity=MaturityLevel.L2,
            availability=Availability.AVAILABLE,
            risk_level=RiskLevel.INTERNAL_ACTION,
            triggers=("scheduled", "manual", "inventory_changed", "data_refresh_completed"),
            steps=("product_diagnosis", "select_tagged_products", "create_tasks"),
            handler=_slow_moving_review,
        ),
        WorkflowDefinition(
            capability_id="workflow.lifecycle_marketing",
            name="客户生命周期流程",
            category="marketing_automation",
            description="欢迎、弃购、购后、复购和召回流程模板。",
            maturity=MaturityLevel.L0,
            availability=Availability.PLANNED,
            risk_level=RiskLevel.EXTERNAL_MESSAGE,
            triggers=("customer_event", "scheduled", "manual"),
            steps=("resolve_audience", "apply_frequency_cap", "draft_message", "request_approval"),
        ),
        WorkflowDefinition(
            capability_id="workflow.order_exception_scan",
            name="订单履约异常流程",
            category="order_fulfillment",
            description="识别未支付、未发货、物流停滞和高风险订单。",
            maturity=MaturityLevel.L0,
            availability=Availability.PLANNED,
            triggers=("order_event", "scheduled"),
            steps=("load_order", "evaluate_rules", "create_review_task"),
        ),
        AgentScenarioDefinition(
            capability_id="agent.nl2sql",
            name="智能问数 Agent",
            category="intelligent_query",
            description="理解自然语言、澄清问题并调用只读 SQL 工具。",
            maturity=MaturityLevel.L1,
            availability=Availability.AVAILABLE,
            tools=("schema_retrieval", "sql_generation", "sql_validation", "sql_execution", "chart_recommendation"),
            guardrails=("read_only_sql", "row_limit", "timeout", "data_masking"),
            execution_endpoint="/api/query",
        ),
    ]
    for definition in definitions:
        registry.register_capability(definition)


def _register_modules(registry: CapabilityRegistry) -> None:
    module_specs = [
        ("operation_dashboard", "经营驾驶舱", "经营", "核心指标、趋势、异常和待办。", (RuntimeKind.DOMAIN_SERVICE, RuntimeKind.WORKFLOW), MaturityLevel.L2, Availability.AVAILABLE, ("analytics.dashboard_overview", "analytics.store_metrics", "analytics.store_diagnosis", "workflow.sales_anomaly_scan")),
        ("intelligent_query", "智能问数", "经营", "自然语言问数、追问、图表和导出。", (RuntimeKind.AGENT, RuntimeKind.DOMAIN_SERVICE), MaturityLevel.L1, Availability.AVAILABLE, ("agent.nl2sql",)),
        ("product_operations", "商品运营", "商品", "商品与 SKU 健康、对比和策略建议。", (RuntimeKind.DOMAIN_SERVICE, RuntimeKind.WORKFLOW), MaturityLevel.L1, Availability.AVAILABLE, ("analytics.product_diagnosis", "analytics.compare_products")),
        ("traffic_conversion", "流量转化", "增长", "流量来源、漏斗和转化诊断。", (RuntimeKind.DOMAIN_SERVICE,), MaturityLevel.L0, Availability.PLANNED, ("analytics.conversion_funnel",)),
        ("advertising", "广告投放", "增长", "计划、素材、人群和关键词诊断。", (RuntimeKind.DOMAIN_SERVICE, RuntimeKind.WORKFLOW), MaturityLevel.L0, Availability.PLANNED, ("analytics.ad_performance",)),
        ("customer_membership", "用户会员", "客户", "RFM、分群、复购和生命周期。", (RuntimeKind.DOMAIN_SERVICE,), MaturityLevel.L0, Availability.PLANNED, ("analytics.customer_rfm",)),
        ("marketing_automation", "营销自动化", "增长", "欢迎、弃购、购后、复购和召回。", (RuntimeKind.WORKFLOW, RuntimeKind.AGENT), MaturityLevel.L0, Availability.PLANNED, ("workflow.lifecycle_marketing",)),
        ("order_fulfillment", "订单履约", "履约", "订单异常、履约时效、物流和风险。", (RuntimeKind.WORKFLOW, RuntimeKind.DOMAIN_SERVICE), MaturityLevel.L0, Availability.PLANNED, ("workflow.order_exception_scan",)),
        ("inventory_supply", "库存供应链", "履约", "缺货、滞销、周转和补货任务。", (RuntimeKind.DOMAIN_SERVICE, RuntimeKind.WORKFLOW), MaturityLevel.L2, Availability.AVAILABLE, ("workflow.low_stock_scan", "workflow.slow_moving_review")),
        ("aftersales_voc", "售后口碑", "客户", "退款、评价、投诉和问题归因。", (RuntimeKind.DOMAIN_SERVICE, RuntimeKind.WORKFLOW), MaturityLevel.L1, Availability.PARTIAL, ("workflow.product_risk_review",)),
        ("merchandising", "推荐陈列", "商品", "关联购、交叉销售、搜索和陈列规则。", (RuntimeKind.DOMAIN_SERVICE,), MaturityLevel.L0, Availability.PLANNED, ("analytics.merchandising_associations",)),
        ("promotion", "活动促销", "增长", "活动模拟、监控、风险和复盘。", (RuntimeKind.DOMAIN_SERVICE, RuntimeKind.WORKFLOW), MaturityLevel.L0, Availability.PLANNED, ()),
        ("report_alert", "报告预警", "自动化", "日周月报、异常订阅和任务派发。", (RuntimeKind.WORKFLOW, RuntimeKind.DOMAIN_SERVICE), MaturityLevel.L2, Availability.AVAILABLE, ("analytics.report_generate", "workflow.daily_operating_report", "workflow.sales_anomaly_scan")),
        ("automation_center", "自动化中心", "自动化", "触发器、条件、动作、审批和运行记录。", (RuntimeKind.WORKFLOW,), MaturityLevel.L2, Availability.AVAILABLE, ("workflow.daily_operating_report", "workflow.sales_anomaly_scan", "workflow.low_stock_scan", "workflow.product_risk_review", "workflow.slow_moving_review")),
        ("data_management", "数据管理", "系统", "数据源、字段映射、质量和刷新状态。", (RuntimeKind.DOMAIN_SERVICE, RuntimeKind.WORKFLOW), MaturityLevel.L0, Availability.PARTIAL, ("data.quality_profile",)),
        ("system_settings", "系统设置", "系统", "指标口径、阈值、模型和接口状态。", (RuntimeKind.DOMAIN_SERVICE,), MaturityLevel.L0, Availability.PLANNED, ()),
    ]
    for module_id, name, group, description, runtimes, maturity, availability, capability_ids in module_specs:
        registry.register_module(ModuleDefinition(
            module_id=module_id,
            name=name,
            group=group,
            description=description,
            runtimes=runtimes,
            maturity=maturity,
            availability=availability,
            web_path=f"/modules/{module_id}",
            capability_ids=capability_ids,
        ))


def build_capability_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    _register_capabilities(registry)
    _register_modules(registry)
    return registry


capability_registry = build_capability_registry()
