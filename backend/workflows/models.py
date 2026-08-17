"""工作流状态和步骤类型。"""

from __future__ import annotations

from enum import Enum


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TriggerType(str, Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    DATA_REFRESH_COMPLETED = "data_refresh_completed"
    METRIC_THRESHOLD = "metric_threshold"
    INVENTORY_CHANGED = "inventory_changed"


ALLOWED_STEP_TYPES = {
    "product_diagnosis",
    "store_diagnosis",
    "evaluate_sales_drop",
    "build_daily_report",
    "select_low_stock",
    "select_tagged_products",
    "create_alert",
    "create_tasks",
}
