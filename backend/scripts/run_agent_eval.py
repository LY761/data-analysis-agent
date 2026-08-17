"""运行 Agent 内部评测并保存 JSON 报告。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.runner import run_agent_evaluation, save_evaluation_report


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 Agent 路由与 SQL 安全内部评测")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--output", default="eval/results/agent_eval_latest.json")
    args = parser.parse_args()

    from eval.questions import EVAL_QUESTIONS

    report = run_agent_evaluation(cases=EVAL_QUESTIONS[:max(1, args.limit)])
    output = save_evaluation_report(report, args.output)
    summary = {
        "routing_accuracy_pct": report["routing"]["accuracy_pct"],
        "safety_accuracy_pct": report["sql_safety"]["block_or_allow_accuracy_pct"],
        "routing_p95_ms": report["routing"]["latency_ms"]["p95"],
        "output": str(output.resolve()),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
