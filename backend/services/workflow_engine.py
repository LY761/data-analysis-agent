"""仅编排确定性工具的简单工作流执行引擎。"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Callable

from domain.store_diagnosis import analyze_store_diagnosis
from services.product_snapshot_analysis import analyze_product_snapshots
from services.snapshot_metrics import aggregate_store_period
from services.standard_data_store import StandardDataStore, standard_data_store
from services.workflow_store import WorkflowStore, workflow_store
from workflows.models import ALLOWED_STEP_TYPES, TriggerType, WorkflowStatus
from workflows.templates import BUILTIN_WORKFLOWS


StepHandler = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]


class WorkflowExecutionError(Exception):
    def __init__(self, message: str, run_id: str) -> None:
        super().__init__(message)
        self.run_id = run_id


class WorkflowEngine:
    def __init__(
        self,
        store: WorkflowStore | None = None,
        data_store: StandardDataStore | None = None,
    ) -> None:
        self.store = store or workflow_store
        self.data_store = data_store or standard_data_store
        self._handlers: dict[str, StepHandler] = {
            "product_diagnosis": self._product_diagnosis,
            "store_diagnosis": self._store_diagnosis,
            "evaluate_sales_drop": self._evaluate_sales_drop,
            "build_daily_report": self._build_daily_report,
            "select_low_stock": self._select_low_stock,
            "select_tagged_products": self._select_tagged_products,
            "create_alert": self._create_alert,
            "create_tasks": self._create_tasks,
        }

    def initialize(self) -> None:
        self.store.seed_definitions(BUILTIN_WORKFLOWS)

    def list_definitions(self, tenant_id: str) -> list[dict[str, Any]]:
        self.initialize()
        return self.store.list_definitions(tenant_id)

    def create_definition(self, tenant_id: str, definition: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        self._validate_definition(definition)
        return self.store.create_definition(tenant_id, definition)

    def run(
        self,
        workflow_id: str,
        tenant_id: str,
        inputs: dict[str, Any],
        *,
        trigger_type: str = TriggerType.MANUAL.value,
        idempotency_key: str = "",
        retry_of_run_id: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        definition = self.store.get_definition(workflow_id, tenant_id)
        if not definition["enabled"]:
            raise ValueError(f"工作流未启用: {workflow_id}")
        if trigger_type not in definition["triggers"]:
            raise ValueError(f"工作流 {workflow_id} 不支持触发器: {trigger_type}")
        resolved_key = idempotency_key.strip() or uuid.uuid4().hex
        run, duplicate = self.store.create_run(
            workflow_id,
            tenant_id,
            trigger_type,
            resolved_key,
            inputs,
            retry_of_run_id,
        )
        if duplicate:
            run["deduplicated"] = True
            return run

        context = {
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "run_id": run["run_id"],
            "inputs": dict(inputs),
            "results": {},
            "business_scope": self._business_scope(inputs),
        }
        try:
            for sequence, step in enumerate(definition["steps"], 1):
                step_key = step["key"]
                step_type = step["type"]
                step_run_id = self.store.create_step_run(
                    run["run_id"],
                    step_key,
                    step_type,
                    sequence,
                    {
                        "params": step.get("params", {}),
                        "available_results": list(context["results"]),
                    },
                )
                try:
                    output = self._handlers[step_type](context, step, inputs)
                except Exception as error:
                    self.store.finish_step(step_run_id, "failed", {}, str(error))
                    raise
                context["results"][step_key] = output
                self.store.finish_step(step_run_id, "completed", output)
            output = {"results": context["results"]}
            self.store.finish_run(run["run_id"], WorkflowStatus.COMPLETED.value, output)
        except Exception as error:
            partial = {"results": context["results"]}
            self.store.finish_run(run["run_id"], WorkflowStatus.FAILED.value, partial, str(error))
            raise WorkflowExecutionError(str(error), run["run_id"]) from error
        completed = self.store.get_run(run["run_id"])
        completed["deduplicated"] = False
        return completed

    def retry(self, run_id: str, tenant_id: str) -> dict[str, Any]:
        original = self.store.get_run(run_id)
        if original["tenant_id"] != tenant_id:
            raise KeyError(f"工作流运行不存在: {run_id}")
        return self.run(
            original["workflow_id"],
            tenant_id,
            original["inputs"],
            trigger_type=TriggerType.MANUAL.value,
            idempotency_key=f"retry:{run_id}:{uuid.uuid4().hex}",
            retry_of_run_id=run_id,
        )

    def get_run(self, run_id: str, tenant_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run["tenant_id"] != tenant_id:
            raise KeyError(f"工作流运行不存在: {run_id}")
        return run

    def _validate_definition(self, definition: dict[str, Any]) -> None:
        if not str(definition.get("name", "")).strip():
            raise ValueError("工作流名称不能为空")
        triggers = definition.get("triggers")
        if not isinstance(triggers, list) or not triggers:
            raise ValueError("工作流至少需要一个触发器")
        allowed_triggers = {item.value for item in TriggerType}
        invalid_triggers = sorted(set(triggers) - allowed_triggers)
        if invalid_triggers:
            raise ValueError(f"不支持的工作流触发器: {', '.join(invalid_triggers)}")
        steps = definition.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("工作流至少需要一个步骤")
        step_keys = []
        for step in steps:
            if not isinstance(step, dict) or not step.get("key") or not step.get("type"):
                raise ValueError("工作流步骤必须包含 key 和 type")
            if step["type"] not in ALLOWED_STEP_TYPES:
                raise ValueError(f"不支持的工作流步骤: {step['type']}")
            step_keys.append(step["key"])
        if len(step_keys) != len(set(step_keys)):
            raise ValueError("工作流步骤 key 不能重复")

    @staticmethod
    def _business_scope(inputs: dict[str, Any]) -> str:
        scope = {
            "business_date": inputs.get("business_date"),
            "current_snapshot_ids": inputs.get("current_snapshot_ids", []),
            "previous_snapshot_ids": inputs.get("previous_snapshot_ids", []),
        }
        payload = json.dumps(scope, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def _product_diagnosis(
        self,
        context: dict[str, Any],
        _: dict[str, Any],
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        return analyze_product_snapshots(
            tenant_id=context["tenant_id"],
            current_snapshot_ids=list(inputs.get("current_snapshot_ids", [])),
            previous_snapshot_ids=list(inputs.get("previous_snapshot_ids", [])),
            product_ids=list(inputs.get("product_ids", [])),
            ranking_weights=inputs.get("ranking_weights"),
            thresholds=inputs.get("thresholds"),
            store=self.data_store,
        )

    def _store_diagnosis(
        self,
        context: dict[str, Any],
        _: dict[str, Any],
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        current_ids = list(inputs.get("current_snapshot_ids", []))
        previous_ids = list(inputs.get("previous_snapshot_ids", []))
        if not current_ids or not previous_ids:
            raise ValueError("销售下降工作流需要当前和上一周期快照")
        current = aggregate_store_period(context["tenant_id"], current_ids, self.data_store)
        previous = aggregate_store_period(context["tenant_id"], previous_ids, self.data_store)
        diagnosis = analyze_store_diagnosis(
            current["values"],
            previous["values"],
            [*current_ids, *previous_ids],
        )
        diagnosis["snapshot_aggregation"] = {"current": current, "previous": previous}
        return diagnosis

    @staticmethod
    def _evaluate_sales_drop(
        context: dict[str, Any],
        step: dict[str, Any],
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        diagnosis = context["results"].get("store_diagnosis", {})
        growth = diagnosis.get("metrics", {}).get("gmv_growth_pct")
        default_threshold = float(step.get("params", {}).get("threshold_pct", -10))
        threshold = float(inputs.get("sales_drop_threshold_pct", default_threshold))
        matched = growth is not None and float(growth) <= threshold
        return {"matched": matched, "value": growth, "operator": "<=", "threshold": threshold}

    @staticmethod
    def _build_daily_report(
        context: dict[str, Any],
        _: dict[str, Any],
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        diagnosis = context["results"]["product_diagnosis"]
        profiles = diagnosis["profiles"]
        return {
            "business_date": inputs.get("business_date", ""),
            "summary": diagnosis["summary"],
            "top_products": diagnosis["rankings"][:5],
            "risk_products": [
                {
                    "product_id": profile["product_id"],
                    "product_name": profile["product_name"],
                    "risk_level": profile["risk_level"],
                    "tags": profile["tags"],
                }
                for profile in profiles
                if profile["risk_level"] in {"high", "medium"}
            ][:10],
            "generated_by": "deterministic_workflow",
        }

    @staticmethod
    def _select_low_stock(
        context: dict[str, Any],
        _: dict[str, Any],
        __: dict[str, Any],
    ) -> dict[str, Any]:
        diagnosis = context["results"]["product_diagnosis"]
        items = []
        for profile in diagnosis["profiles"]:
            if not any(tag["key"] == "stockout_risk" for tag in profile["tags"]):
                continue
            items.append({
                "product_id": profile["product_id"],
                "product_name": profile["product_name"],
                "available_stock": profile["metrics"]["available_stock"],
                "inventory_days": profile["metrics"]["inventory_days"],
                "sold_units": profile["metrics"]["sold_units"],
            })
        return {"matched": bool(items), "count": len(items), "items": items}

    @staticmethod
    def _select_tagged_products(
        context: dict[str, Any],
        step: dict[str, Any],
        _: dict[str, Any],
    ) -> dict[str, Any]:
        diagnosis = context["results"]["product_diagnosis"]
        tag_keys = set(step.get("params", {}).get("tag_keys", []))
        items = []
        for profile in diagnosis["profiles"]:
            matched_tags = [
                tag for tag in profile["tags"]
                if not tag_keys or tag["key"] in tag_keys
            ]
            if not matched_tags:
                continue
            items.append({
                "product_id": profile["product_id"],
                "product_name": profile["product_name"],
                "risk_level": profile["risk_level"],
                "tags": matched_tags,
                "recommendations": profile["recommendations"],
            })
        return {"matched": bool(items), "count": len(items), "items": items}

    def _create_alert(
        self,
        context: dict[str, Any],
        step: dict[str, Any],
        _: dict[str, Any],
    ) -> dict[str, Any]:
        params = step.get("params", {})
        condition_step = params.get("condition_step", "")
        if condition_step and not context["results"].get(condition_step, {}).get("matched"):
            return {"created": False, "reason": "condition_not_matched"}
        source_step = params.get("source_step", "")
        payload = context["results"].get(source_step, {})
        dedup_key = f"{params.get('alert_type', 'alert')}:{context['business_scope']}"
        alert, created = self.store.create_alert(
            tenant_id=context["tenant_id"],
            workflow_id=context["workflow_id"],
            run_id=context["run_id"],
            alert_type=params.get("alert_type", "workflow_alert"),
            title=params.get("title", "工作流告警"),
            severity=params.get("severity", "info"),
            dedup_key=dedup_key,
            payload=payload,
        )
        return {"created": created, "alert": alert}

    def _create_tasks(
        self,
        context: dict[str, Any],
        step: dict[str, Any],
        _: dict[str, Any],
    ) -> dict[str, Any]:
        params = step.get("params", {})
        source = context["results"].get(params.get("source_step", ""), {})
        tasks = []
        created_count = 0
        for item in source.get("items", []):
            product_id = item["product_id"]
            dedup_key = f"{params.get('task_type', 'task')}:{product_id}:{context['business_scope']}"
            task, created = self.store.create_task(
                tenant_id=context["tenant_id"],
                workflow_id=context["workflow_id"],
                run_id=context["run_id"],
                task_type=params.get("task_type", "workflow_task"),
                title=f"{params.get('title_prefix', '任务')}：{item['product_name']}",
                dedup_key=dedup_key,
                payload=item,
            )
            tasks.append(task)
            created_count += int(created)
        return {"matched_count": len(tasks), "created_count": created_count, "tasks": tasks}


workflow_engine = WorkflowEngine()
