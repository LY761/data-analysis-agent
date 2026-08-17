"""版本化经营报告与分析历史 API。"""

from __future__ import annotations

import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from services.report_service import export_report_excel, generate_report
from services.report_store import report_store
from services.standard_data_store import standard_data_store


router = APIRouter(tags=["reports"])


class ReportGenerateRequest(BaseModel):
    tenant_id: str = Field("default", min_length=1, max_length=64)
    report_type: str = Field(..., min_length=1, max_length=32)
    period_key: str = Field(..., min_length=1, max_length=64)
    title: str = Field("", max_length=256)
    current_snapshot_ids: list[str] = Field(..., min_length=1, max_length=20)
    previous_snapshot_ids: list[str] = Field(default_factory=list, max_length=20)
    filters: dict[str, str] = Field(default_factory=dict)
    targets: dict[str, float] = Field(default_factory=dict)
    use_llm_summary: bool = False


@router.post("/reports/generate")
async def generate_versioned_report(request: ReportGenerateRequest):
    try:
        return generate_report(
            tenant_id=request.tenant_id,
            report_type=request.report_type,
            period_key=request.period_key,
            current_snapshot_ids=request.current_snapshot_ids,
            previous_snapshot_ids=request.previous_snapshot_ids,
            title=request.title,
            filters=request.filters,
            targets=request.targets,
            use_llm_summary=request.use_llm_summary,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/reports")
async def list_versioned_reports(
    tenant_id: str = Query("default", min_length=1, max_length=64),
    report_type: str = Query("", max_length=32),
    limit: int = Query(100, ge=1, le=500),
):
    return {"reports": report_store.list_reports(tenant_id, report_type, limit)}


@router.get("/reports/{report_id}/export")
async def export_versioned_report(
    report_id: str,
    tenant_id: str = Query("default", min_length=1, max_length=64),
    version: int | None = Query(None, ge=1),
):
    try:
        report = report_store.get_report(report_id, tenant_id, version)
        content = export_report_excel(report)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    filename = f"report_{report_id}_v{report['current_version']['version']}.xlsx"
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/reports/{report_id}")
async def versioned_report_detail(
    report_id: str,
    tenant_id: str = Query("default", min_length=1, max_length=64),
    version: int | None = Query(None, ge=1),
):
    try:
        return report_store.get_report(report_id, tenant_id, version)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/analysis-runs")
async def list_analysis_runs(
    tenant_id: str = Query("default", min_length=1, max_length=64),
    scenario: str = Query("", max_length=64),
    limit: int = Query(100, ge=1, le=500),
):
    return {
        "runs": standard_data_store.list_analysis_runs(tenant_id, scenario, limit),
    }


@router.get("/analysis-runs/{run_id}")
async def analysis_run_detail(
    run_id: str,
    tenant_id: str = Query("default", min_length=1, max_length=64),
):
    try:
        return standard_data_store.get_analysis_run(run_id, tenant_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
