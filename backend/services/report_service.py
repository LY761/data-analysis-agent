"""基于驾驶舱事实生成版本化经营报告。"""

from __future__ import annotations

import json
from typing import Any, Callable

from services.dashboard_service import build_dashboard
from services.export_service import export_to_excel
from services.report_store import ReportStore, report_store
from services.standard_data_store import StandardDataStore, standard_data_store
from services.workflow_store import WorkflowStore, workflow_store


REPORT_TYPES = {"daily", "weekly", "monthly", "diagnostic"}
SummaryProvider = Callable[[dict[str, Any]], str]


def _deterministic_summary(dashboard: dict[str, Any]) -> str:
    cards = {card["metric_key"]: card for card in dashboard["overview"]["cards"]}
    gmv = cards["gmv"]
    profit = cards["gross_profit"]
    growth_text = (
        f"较上一周期变化 {gmv['change_pct']:.2f}%"
        if gmv["change_pct"] is not None
        else "上一周期基数不足，暂不计算变化率"
    )
    return (
        f"本期 GMV 为 {gmv['value'] or 0}，{growth_text}；"
        f"经营毛利为 {profit['value'] or 0}。"
        f"当前识别 {dashboard['overview']['high_risk_product_count']} 个高风险商品、"
        f"{dashboard['overview']['open_alert_count']} 条开放告警和"
        f"{dashboard['overview']['pending_task_count']} 个待办任务。"
    )


def _llm_summary(dashboard: dict[str, Any]) -> str:
    from openai import OpenAI

    from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

    if not LLM_API_KEY:
        raise RuntimeError("未配置 LLM_API_KEY")
    facts = {
        "cards": dashboard["overview"]["cards"],
        "top_products": dashboard["products"]["top"][:5],
        "anomalies": dashboard["anomalies"][:10],
    }
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    response = client.chat.completions.create(
        model=LLM_MODEL,
        temperature=0.1,
        messages=[
            {
                "role": "system",
                "content": "你是电商经营分析助手。只总结输入事实，不新增数字，不改变指标口径，输出一段简洁管理层摘要。",
            },
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
        ],
    )
    return response.choices[0].message.content or ""


def generate_report(
    tenant_id: str,
    report_type: str,
    period_key: str,
    current_snapshot_ids: list[str],
    previous_snapshot_ids: list[str] | None = None,
    *,
    title: str = "",
    filters: dict[str, str] | None = None,
    targets: dict[str, float] | None = None,
    use_llm_summary: bool = False,
    summary_provider: SummaryProvider | None = None,
    data_store: StandardDataStore | None = None,
    automation_store: WorkflowStore | None = None,
    repository: ReportStore | None = None,
) -> dict[str, Any]:
    if report_type not in REPORT_TYPES:
        raise ValueError(f"report_type 必须是: {', '.join(sorted(REPORT_TYPES))}")
    if not period_key.strip():
        raise ValueError("period_key 不能为空")
    dashboard = build_dashboard(
        tenant_id,
        current_snapshot_ids,
        previous_snapshot_ids,
        filters=filters,
        targets=targets,
        current_label=period_key,
        previous_label="previous",
        data_store=data_store or standard_data_store,
        automation_store=automation_store or workflow_store,
    )
    deterministic_summary = _deterministic_summary(dashboard)
    summary = deterministic_summary
    summary_mode = "deterministic"
    summary_fallback_reason = ""
    if use_llm_summary:
        try:
            summary = (summary_provider or _llm_summary)(dashboard).strip() or deterministic_summary
            summary_mode = "llm"
        except Exception as error:
            summary_fallback_reason = str(error)

    content = {
        "schema_version": "1.0",
        "report_type": report_type,
        "period_key": period_key,
        "title": title.strip() or f"{period_key} 电商经营{report_type}报告",
        "executive_summary": summary,
        "summary_mode": summary_mode,
        "summary_fallback_reason": summary_fallback_reason,
        "sections": {
            "kpis": dashboard["overview"]["cards"],
            "trends": dashboard["trends"],
            "products": dashboard["products"],
            "anomalies": dashboard["anomalies"],
            "tasks": dashboard["tasks"],
            "data_availability": dashboard["data_availability"],
        },
        "evidence": {
            "snapshot_ids": dashboard["data_availability"]["snapshot_ids"],
            "metric_references": [
                {
                    "metric_key": card["metric_key"],
                    "metric_version": card["metric_version"],
                    "formula": card["formula"],
                }
                for card in dashboard["overview"]["cards"]
            ],
        },
        "filters": dashboard["filters"],
    }
    return (repository or report_store).save_report_version(
        tenant_id=tenant_id,
        report_type=report_type,
        period_key=period_key,
        title=content["title"],
        content=content,
        snapshot_ids=dashboard["data_availability"]["snapshot_ids"],
        summary_mode=summary_mode,
    )


def export_report_excel(report: dict[str, Any]) -> bytes:
    version = report["current_version"]
    content = version["content"]
    rows = []
    for card in content["sections"]["kpis"]:
        rows.append({
            "类型": "核心指标",
            "名称": card["name"],
            "当前值": card["value"],
            "上一周期": card["previous_value"],
            "变化率%": card["change_pct"],
            "状态": card["status"],
            "指标版本": card["metric_version"],
            "公式": card["formula"],
        })
    for product in content["sections"]["products"]["top"]:
        rows.append({
            "类型": "商品排名",
            "名称": product["product_name"],
            "当前值": product["score"],
            "上一周期": "",
            "变化率%": "",
            "状态": f"第 {product['rank']} 名",
            "指标版本": "",
            "公式": "配置化加权评分",
        })
    columns = ["类型", "名称", "当前值", "上一周期", "变化率%", "状态", "指标版本", "公式"]
    return export_to_excel(rows, columns, content["title"])
