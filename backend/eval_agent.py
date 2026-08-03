# -*- coding: utf-8 -*-
"""
Agent 评测脚本 — 用 eval/questions.py 的 100 条问题测准确率/召回率

用法（在 backend 目录）:
  ../.venv/Scripts/python.exe eval_agent.py              # 跑全部
  ../.venv/Scripts/python.exe eval_agent.py --limit 20    # 只跑前20条
  ../.venv/Scripts/python.exe eval_agent.py --category extreme  # 只跑极值类
  ../.venv/Scripts/python.exe eval_agent.py --save eval_report.json

指标:
  route_accuracy   路由准确率（期望模式 == 实际模式）
  validation_rate  SQL校验通过率（sql类问题中 all_passed 占比）
  exec_success     SQL执行成功率（无 error）
  data_hit         数据命中率（期望有数据→有数据 / 期望空→空）
  sql_logic_hit    SQL逻辑命中率（期望片段出现在生成的SQL中）
  recall           检索召回率（期望表被SQL实际引用的比例）
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eval.questions import EVAL_QUESTIONS
from agent.agent_router import agent_router
from agent.workflow import app_workflow, WorkflowState


def _extract_tables(sql: str) -> set:
    return {m.group(1).lower() for m in re.finditer(r"(?:FROM|JOIN)\s+(\w+)", sql or "", re.I)}


def _make_state(question: str) -> WorkflowState:
    return {
        "question": question, "original_question": question, "schema_context": {},
        "clarification_needed": False, "clarified_question": "",
        "follow_up_questions": [], "sql_hint": "", "alternative_questions": [],
        "conversation_history": [], "intent": "", "intent_name": "", "intent_method": "",
        "sql": "", "validation_result": {}, "retry_count": 0,
        "query_result": {}, "chart_recommendation": {},
        "sql_explanation": "", "analysis_text": "", "nl_answer": "",
        "final_response": {}, "error": None,
        "progress_cb": None, "stream_answer_cb": None, "retrieval_span": None,
    }


async def _run_sql(question: str) -> dict:
    """跑完整 LangGraph 流水线，返回执行结果"""
    final = await app_workflow.ainvoke(_make_state(question))
    fr = final.get("final_response", {})
    return {
        "validation": (final.get("validation_result") or {}).get("stage", ""),
        "error": fr.get("error"),
        "row_count": (fr.get("result") or {}).get("row_count", 0),
        "sql": fr.get("sql", ""),
    }


async def evaluate(questions) -> list:
    results = []
    for i, q in enumerate(questions, 1):
        t0 = time.time()
        route = agent_router.route(q["question"])
        item = {
            "id": q["id"], "question": q["question"], "category": q["category"],
            "expect_mode": q["expect_mode"], "actual_mode": route.get("mode"),
            "expect_data": q.get("expect_data"),
        }
        # expect_mode 支持 "|" 分隔的合法模式集合（如 sql_query|quick_card）
        expect_set = set(q["expect_mode"].split("|"))
        item["route_ok"] = route.get("mode") in expect_set

        if route.get("mode") == "quick_card":
            # 快捷卡片：直接拿预写SQL结果判数据命中（不走workflow）
            from services.quick_queries import run_quick_query
            r = run_quick_query(route.get("card_key", "monthly_sales"))
            item.update({"validation": "quick_card", "error": r.get("error"),
                         "row_count": r.get("row_count", 0), "sql": r.get("sql", "")})
            item["has_data"] = r.get("row_count", 0) > 0
            item["recall"] = None
        elif "sql_query" in expect_set:
            r = await _run_sql(q["question"])
            item.update(r)
            item["has_data"] = r["row_count"] > 0
            # 检索召回：期望表被SQL引用
            sql_tables = _extract_tables(r["sql"])
            exp_tables = set(t.lower() for t in q.get("expect_tables", []))
            item["recall"] = (len(exp_tables & sql_tables) / len(exp_tables)) if exp_tables else 1.0
        else:
            item["validation"] = "route_only"
            item["recall"] = None

        item["ms"] = round((time.time() - t0) * 1000)
        print(f"[{i:3d}/{len(questions)}] {q['question'][:28]:<30} "
              f"期望={q['expect_mode'][:12]:<12} 实际={item['actual_mode'][:14]:<14} "
              f"{'✓' if item['route_ok'] else '✗'} "
              f"rows={item.get('row_count', '-')} {item.get('validation','')}")
        results.append(item)
    return results


def _compute(results) -> dict:
    n = len(results)
    route_acc = sum(r["route_ok"] for r in results) / n if n else 0

    sql_items = [r for r in results if "sql_query" in r["expect_mode"]]
    n_sql = len(sql_items)
    # 真正走了 workflow 的（排除 quick_card 预写SQL，它没有 validation stage）
    wf_items = [r for r in sql_items if r.get("validation") not in ("quick_card", "route_only")]
    n_wf = len(wf_items)
    if n_sql:
        validation_rate = sum(r["validation"] == "all_passed" for r in wf_items) / n_wf if n_wf else None
        exec_success = sum(not r.get("error") for r in sql_items) / n_sql
        # 数据命中（有期望数据的）
        data_q = [r for r in sql_items if r.get("expect_data") in ("yes", "no")]
        data_hit = (sum(
            (r["expect_data"] == "yes" and r["has_data"]) or
            (r["expect_data"] == "no" and not r["has_data"])
            for r in data_q) / len(data_q)) if data_q else None
        # SQL逻辑命中
        frag_q = [r for r in sql_items if r.get("expect_sql_frag")]
        sql_logic = (sum(r["expect_sql_frag"] in (r.get("sql") or "")
                         for r in frag_q) / len(frag_q)) if frag_q else None
        # 检索召回
        recall_vals = [r["recall"] for r in sql_items if r.get("recall") is not None]
        recall = (sum(recall_vals) / len(recall_vals)) if recall_vals else None
    else:
        validation_rate = exec_success = data_hit = sql_logic = recall = None

    # 分类别路由准确率
    by_cat = defaultdict(lambda: [0, 0])
    for r in results:
        by_cat[r["category"]][0] += 1
        by_cat[r["category"]][1] += r["route_ok"]
    cat_report = {k: {"n": v[0], "route_acc": round(v[1] / v[0], 3) if v[0] else 0}
                  for k, v in sorted(by_cat.items())}

    return {
        "total": n,
        "route_accuracy": round(route_acc, 3),
        "validation_rate": round(validation_rate, 3) if validation_rate is not None else None,
        "exec_success": round(exec_success, 3) if exec_success is not None else None,
        "data_hit": round(data_hit, 3) if data_hit is not None else None,
        "sql_logic_hit": round(sql_logic, 3) if sql_logic is not None else None,
        "recall": round(recall, 3) if recall is not None else None,
        "by_category": cat_report,
    }


def _failures(results) -> dict:
    return {
        "route_mismatch": [
            {"id": r["id"], "question": r["question"], "expect": r["expect_mode"],
             "actual": r["actual_mode"]}
            for r in results if not r["route_ok"]
        ],
        "sql_validation_fail": [
            {"id": r["id"], "question": r["question"], "stage": r.get("validation"),
             "sql": (r.get("sql") or "")[:80]}
            for r in results if "sql_query" in r["expect_mode"]
            and r.get("validation") not in ("all_passed", "quick_card", "route_only")
        ],
        "sql_exec_error": [
            {"id": r["id"], "question": r["question"], "error": r.get("error")}
            for r in results if "sql_query" in r["expect_mode"] and r.get("error")
        ],
        "data_mismatch": [
            {"id": r["id"], "question": r["question"], "expect": r.get("expect_data"),
             "rows": r.get("row_count")}
            for r in results if "sql_query" in r["expect_mode"]
            and r.get("expect_data") in ("yes", "no")
            and ((r["expect_data"] == "yes" and not r["has_data"]) or
                 (r["expect_data"] == "no" and r["has_data"]))
        ],
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前N条")
    ap.add_argument("--category", default="", help="只跑指定类别")
    ap.add_argument("--save", default="eval_report.json", help="结果输出文件")
    args = ap.parse_args()

    questions = EVAL_QUESTIONS
    if args.category:
        questions = [q for q in questions if q["category"] == args.category]
    if args.limit:
        questions = questions[:args.limit]

    # 清空路由缓存，避免旧路由结果影响本次评测
    agent_router.cache.clear()
    print(f"开始评测 {len(questions)} 条问题...\n")
    results = await evaluate(questions)

    metrics = _compute(results)
    failures = _failures(results)

    print("\n" + "=" * 60)
    print("  评测报告")
    print("=" * 60)
    print(f"  总问题数:      {metrics['total']}")
    print(f"  路由准确率:    {metrics['route_accuracy']*100:.1f}%")
    print(f"  SQL校验通过率: {metrics['validation_rate']*100:.1f}%  " if metrics["validation_rate"] is not None else "")
    print(f"  SQL执行成功率: {metrics['exec_success']*100:.1f}%" if metrics["exec_success"] is not None else "")
    print(f"  数据命中率:    {metrics['data_hit']*100:.1f}%" if metrics["data_hit"] is not None else "")
    print(f"  SQL逻辑命中率: {metrics['sql_logic_hit']*100:.1f}%" if metrics["sql_logic_hit"] is not None else "")
    print(f"  检索召回率:    {metrics['recall']*100:.1f}%" if metrics["recall"] is not None else "")
    print("\n  分类别路由准确率:")
    for k, v in metrics["by_category"].items():
        print(f"    {k:<10} n={v['n']:<3} route_acc={v['route_acc']*100:.0f}%")
    print(f"\n  失败明细: 路由不匹配 {len(failures['route_mismatch'])} | "
          f"校验失败 {len(failures['sql_validation_fail'])} | "
          f"执行错误 {len(failures['sql_exec_error'])} | "
          f"数据不符 {len(failures['data_mismatch'])}")
    for name, lst in failures.items():
        if lst:
            print(f"    [{name}]")
            for f in lst[:8]:
                print(f"      #{f['id']} {f['question'][:36]}")

    report = {"metrics": metrics, "failures": failures, "results": results}
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n完整结果已保存: {args.save}")


if __name__ == "__main__":
    asyncio.run(main())
