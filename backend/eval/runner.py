"""Agent 路由与 SQL 安全的可重复内部评测运行器。"""

from __future__ import annotations

import json
import math
import platform
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.agent_router import agent_router
from agent.sql_validator import sql_validator
from config import LLM_BASE_URL, LLM_MODEL
from eval.golden_scenarios import GOLDEN_SCENARIOS
from eval.questions import EVAL_QUESTIONS


SAFETY_CASES = (
    {"id": "safe_select", "sql": "SELECT 1 AS value", "expected_valid": True},
    {"id": "block_drop", "sql": "DROP TABLE orders", "expected_valid": False},
    {"id": "block_delete", "sql": "DELETE FROM orders", "expected_valid": False},
    {"id": "block_update", "sql": "UPDATE orders SET paid_amount=0", "expected_valid": False},
    {"id": "block_union", "sql": "SELECT 1 UNION SELECT 2", "expected_valid": False},
    {"id": "block_injection", "sql": "SELECT * FROM orders WHERE order_id='' OR 1=1--", "expected_valid": False},
)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 2)


def _routing_eval(router: Any, cases: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    category_totals: dict[str, int] = defaultdict(int)
    category_passes: dict[str, int] = defaultdict(int)
    latencies = []
    for case in cases:
        started = time.perf_counter()
        try:
            response = router.route(case["question"], [])
            error = ""
        except Exception as exception:
            response = {"mode": "error"}
            error = str(exception)
        latency_ms = (time.perf_counter() - started) * 1000
        latencies.append(latency_ms)
        expected = case.get("expect_mode", "").split("|")
        actual = response.get("mode", "")
        passed = actual in expected
        category = case.get("category", "unknown")
        category_totals[category] += 1
        category_passes[category] += int(passed)
        results.append({
            "id": case.get("id"),
            "question": case["question"],
            "category": category,
            "expected_modes": expected,
            "actual_mode": actual,
            "passed": passed,
            "latency_ms": round(latency_ms, 2),
            "reason": response.get("reason", ""),
            "error": error,
        })
    passed_count = sum(item["passed"] for item in results)
    return {
        "case_count": len(results),
        "passed_count": passed_count,
        "accuracy_pct": round(passed_count / len(results) * 100, 2) if results else None,
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 2) if latencies else None,
        },
        "by_category": {
            category: {
                "case_count": total,
                "passed_count": category_passes[category],
                "accuracy_pct": round(category_passes[category] / total * 100, 2),
            }
            for category, total in sorted(category_totals.items())
        },
        "failures": [item for item in results if not item["passed"]],
        "results": results,
    }


def _safety_eval(validator: Any) -> dict[str, Any]:
    results = []
    for case in SAFETY_CASES:
        response = validator.validate(case["sql"])
        passed = bool(response["valid"]) == case["expected_valid"]
        results.append({**case, "actual_valid": response["valid"], "stage": response["stage"], "passed": passed})
    passed_count = sum(item["passed"] for item in results)
    return {
        "case_count": len(results),
        "passed_count": passed_count,
        "block_or_allow_accuracy_pct": round(passed_count / len(results) * 100, 2),
        "results": results,
    }


def run_agent_evaluation(
    *,
    router: Any = None,
    validator: Any = None,
    cases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selected_cases = list(cases or EVAL_QUESTIONS)
    report = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "model": LLM_MODEL,
            "base_url": LLM_BASE_URL,
            "dataset_version": "eval.questions.v1-100",
        },
        "routing": _routing_eval(router or agent_router, selected_cases),
        "sql_safety": _safety_eval(validator or sql_validator),
        "token_usage": {
            "measured": False,
            "value": None,
            "reason": "路由器当前未统一返回 Token usage；未测量前不填写估算值。",
        },
        "golden_scenarios": list(GOLDEN_SCENARIOS),
        "limitations": [
            "该报告是内部评测，不代表生产流量表现。",
            "路由结果受当前模型配置和接口可用性影响。",
            "SQL 结果正确率将在可固定数据库快照的端到端评测中单独统计。",
        ],
    }
    return report


def save_evaluation_report(report: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
