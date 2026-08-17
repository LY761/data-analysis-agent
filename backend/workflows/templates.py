"""首批固定电商工作流模板。"""

from __future__ import annotations

from typing import Any


BUILTIN_WORKFLOWS: tuple[dict[str, Any], ...] = (
    {
        "workflow_id": "daily_operating_report",
        "name": "定时经营日报",
        "description": "从标准快照生成确定性经营摘要并创建站内报告提醒。",
        "version": 1,
        "enabled": True,
        "triggers": ["manual", "scheduled", "data_refresh_completed"],
        "steps": [
            {"key": "product_diagnosis", "type": "product_diagnosis"},
            {"key": "daily_report", "type": "build_daily_report"},
            {
                "key": "report_alert",
                "type": "create_alert",
                "params": {
                    "source_step": "daily_report",
                    "alert_type": "daily_operating_report",
                    "title": "经营日报已生成",
                    "severity": "info",
                },
            },
        ],
    },
    {
        "workflow_id": "sales_drop_alert",
        "name": "销售下降告警",
        "description": "比较当前与上一周期经营快照，命中阈值后创建站内告警。",
        "version": 1,
        "enabled": True,
        "triggers": ["manual", "scheduled", "metric_threshold"],
        "steps": [
            {"key": "store_diagnosis", "type": "store_diagnosis"},
            {
                "key": "sales_drop_condition",
                "type": "evaluate_sales_drop",
                "params": {"threshold_pct": -10},
            },
            {
                "key": "sales_drop_alert",
                "type": "create_alert",
                "params": {
                    "source_step": "store_diagnosis",
                    "condition_step": "sales_drop_condition",
                    "alert_type": "sales_drop",
                    "title": "销售额下降告警",
                    "severity": "high",
                },
            },
        ],
    },
    {
        "workflow_id": "low_stock_task",
        "name": "低库存任务",
        "description": "识别缺货风险商品并创建内部补货任务。",
        "version": 1,
        "enabled": True,
        "triggers": ["manual", "scheduled", "inventory_changed", "data_refresh_completed"],
        "steps": [
            {"key": "product_diagnosis", "type": "product_diagnosis"},
            {"key": "low_stock_products", "type": "select_low_stock"},
            {
                "key": "replenishment_tasks",
                "type": "create_tasks",
                "params": {
                    "source_step": "low_stock_products",
                    "task_type": "replenishment_review",
                    "title_prefix": "补货复核",
                },
            },
        ],
    },
    {
        "workflow_id": "product_risk_review",
        "name": "商品风险复核",
        "description": "识别高退款、低评分或明显下滑商品并创建人工复核任务。",
        "version": 1,
        "enabled": True,
        "triggers": ["manual", "scheduled", "data_refresh_completed"],
        "steps": [
            {"key": "product_diagnosis", "type": "product_diagnosis"},
            {
                "key": "risk_products",
                "type": "select_tagged_products",
                "params": {"tag_keys": ["risk"]},
            },
            {
                "key": "risk_review_tasks",
                "type": "create_tasks",
                "params": {
                    "source_step": "risk_products",
                    "task_type": "product_risk_review",
                    "title_prefix": "商品风险复核",
                },
            },
        ],
    },
    {
        "workflow_id": "slow_moving_review",
        "name": "滞销清理复核",
        "description": "识别滞销商品并生成清仓、组合销售或暂停补货的复核任务。",
        "version": 1,
        "enabled": True,
        "triggers": ["manual", "scheduled", "inventory_changed", "data_refresh_completed"],
        "steps": [
            {"key": "product_diagnosis", "type": "product_diagnosis"},
            {
                "key": "slow_moving_products",
                "type": "select_tagged_products",
                "params": {"tag_keys": ["slow_moving"]},
            },
            {
                "key": "clearance_review_tasks",
                "type": "create_tasks",
                "params": {
                    "source_step": "slow_moving_products",
                    "task_type": "clearance_review",
                    "title_prefix": "滞销清理复核",
                },
            },
        ],
    },
)
