"""经营驾驶舱聚合 API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from services.dashboard_service import build_dashboard
from services.workflow_store import workflow_store


router = APIRouter(tags=["dashboard"])


def _dashboard(
    tenant_id: str,
    current_snapshot_ids: list[str],
    previous_snapshot_ids: list[str],
    shop_id: str,
    platform: str,
    channel: str,
    category: str,
    current_label: str,
    previous_label: str,
    gmv_target: float | None,
    gross_profit_target: float | None,
):
    filters = {
        key: value
        for key, value in {
            "shop_id": shop_id,
            "platform": platform,
            "channel": channel,
            "category": category,
        }.items()
        if value
    }
    targets = {
        key: value
        for key, value in {
            "gmv": gmv_target,
            "gross_profit": gross_profit_target,
        }.items()
        if value is not None
    }
    try:
        return build_dashboard(
            tenant_id,
            current_snapshot_ids,
            previous_snapshot_ids,
            filters=filters,
            targets=targets,
            current_label=current_label,
            previous_label=previous_label,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/dashboard/workbench")
async def dashboard_workbench(
    tenant_id: str = Query("default", min_length=1, max_length=64),
    current_snapshot_ids: list[str] = Query(...),
    previous_snapshot_ids: list[str] = Query(default=[]),
    shop_id: str = Query("", max_length=128),
    platform: str = Query("", max_length=64),
    channel: str = Query("", max_length=128),
    category: str = Query("", max_length=128),
    current_label: str = Query("current", max_length=64),
    previous_label: str = Query("previous", max_length=64),
    gmv_target: float | None = Query(None, gt=0),
    gross_profit_target: float | None = Query(None, gt=0),
    anomaly_limit: int = Query(50, ge=1, le=500),
):
    dashboard = _dashboard(
        tenant_id,
        current_snapshot_ids,
        previous_snapshot_ids,
        shop_id,
        platform,
        channel,
        category,
        current_label,
        previous_label,
        gmv_target,
        gross_profit_target,
    )
    anomalies = dashboard["anomalies"]
    return {
        **dashboard,
        "anomalies": anomalies[:anomaly_limit],
        "anomaly_count": len(anomalies),
    }


@router.get("/dashboard/overview")
async def dashboard_overview(
    tenant_id: str = Query("default", min_length=1, max_length=64),
    current_snapshot_ids: list[str] = Query(...),
    previous_snapshot_ids: list[str] = Query(default=[]),
    shop_id: str = Query("", max_length=128),
    platform: str = Query("", max_length=64),
    channel: str = Query("", max_length=128),
    category: str = Query("", max_length=128),
    current_label: str = Query("current", max_length=64),
    previous_label: str = Query("previous", max_length=64),
    gmv_target: float | None = Query(None, gt=0),
    gross_profit_target: float | None = Query(None, gt=0),
):
    dashboard = _dashboard(
        tenant_id,
        current_snapshot_ids,
        previous_snapshot_ids,
        shop_id,
        platform,
        channel,
        category,
        current_label,
        previous_label,
        gmv_target,
        gross_profit_target,
    )
    return {
        "overview": dashboard["overview"],
        "products": dashboard["products"],
        "data_availability": dashboard["data_availability"],
        "filters": dashboard["filters"],
    }


@router.get("/dashboard/trends")
async def dashboard_trends(
    tenant_id: str = Query("default", min_length=1, max_length=64),
    current_snapshot_ids: list[str] = Query(...),
    previous_snapshot_ids: list[str] = Query(default=[]),
    shop_id: str = Query("", max_length=128),
    platform: str = Query("", max_length=64),
    channel: str = Query("", max_length=128),
    category: str = Query("", max_length=128),
    current_label: str = Query("current", max_length=64),
    previous_label: str = Query("previous", max_length=64),
):
    return _dashboard(
        tenant_id,
        current_snapshot_ids,
        previous_snapshot_ids,
        shop_id,
        platform,
        channel,
        category,
        current_label,
        previous_label,
        None,
        None,
    )["trends"]


@router.get("/dashboard/anomalies")
async def dashboard_anomalies(
    tenant_id: str = Query("default", min_length=1, max_length=64),
    current_snapshot_ids: list[str] = Query(...),
    previous_snapshot_ids: list[str] = Query(default=[]),
    category: str = Query("", max_length=128),
):
    return {
        "anomalies": _dashboard(
            tenant_id,
            current_snapshot_ids,
            previous_snapshot_ids,
            "",
            "",
            "",
            category,
            "current",
            "previous",
            None,
            None,
        )["anomalies"],
    }


@router.get("/dashboard/tasks")
async def dashboard_tasks(
    tenant_id: str = Query("default", min_length=1, max_length=64),
    status: str = Query("pending", max_length=32),
    limit: int = Query(100, ge=1, le=500),
):
    return {
        "tasks": workflow_store.list_tasks(tenant_id, status, limit),
    }
