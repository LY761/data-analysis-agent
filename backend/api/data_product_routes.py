"""标准化导入、数据快照和经营诊断 API。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from domain.ecommerce_metrics import compare_products
from domain.store_diagnosis import analyze_store_diagnosis
from services.product_snapshot_analysis import analyze_product_snapshots
from services.standard_data_store import standard_data_store
from services.standardization import confirm_mapping, import_standard_file, import_standard_rows
from services.snapshot_metrics import aggregate_store_period

router = APIRouter(tags=["data-products"])


class MappingProfileRequest(BaseModel):
    tenant_id: str = Field("default", min_length=1, max_length=64)
    entity_type: str = Field(..., min_length=1, max_length=64)
    profile_name: str = Field(..., min_length=1, max_length=128)
    mapping: dict[str, str]


class StandardImportRequest(BaseModel):
    tenant_id: str = Field("default", min_length=1, max_length=64)
    entity_type: str = Field(..., min_length=1, max_length=64)
    source_name: str = Field(..., min_length=1, max_length=256)
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=10000)
    mapping: dict[str, str]
    allow_warning: bool = True


class StorePeriodInput(BaseModel):
    visitors: float
    orders: float
    gmv: float
    ad_spend: float = 0
    cost_of_goods: float = 0
    refund_amount: float = 0
    sold_units: float = 0
    stock_units: float = 0


class StoreDiagnosisRequest(BaseModel):
    tenant_id: str = Field("default", min_length=1, max_length=64)
    current: StorePeriodInput
    previous: StorePeriodInput
    snapshot_ids: list[str] = Field(default_factory=list, max_length=50)


class SnapshotStoreDiagnosisRequest(BaseModel):
    tenant_id: str = Field("default", min_length=1, max_length=64)
    current_snapshot_ids: list[str] = Field(..., min_length=1, max_length=20)
    previous_snapshot_ids: list[str] = Field(..., min_length=1, max_length=20)


class ProductDiagnosisRequest(BaseModel):
    tenant_id: str = Field("default", min_length=1, max_length=64)
    current_snapshot_ids: list[str] = Field(..., min_length=1, max_length=20)
    previous_snapshot_ids: list[str] = Field(default_factory=list, max_length=20)
    product_ids: list[str] = Field(default_factory=list, max_length=100)
    ranking_weights: dict[str, float] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=dict)


class ProductCompareRequest(BaseModel):
    products: list[dict[str, Any]] = Field(..., min_length=2, max_length=100)


@router.post("/standardization/mappings")
async def save_standard_mapping(request: MappingProfileRequest):
    try:
        return confirm_mapping(
            request.tenant_id,
            request.entity_type,
            request.profile_name,
            request.mapping,
            store=standard_data_store,
        )
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/standardization/mappings")
async def list_standard_mappings(
    tenant_id: str = Query("default", min_length=1, max_length=64),
    entity_type: str = Query("", max_length=64),
):
    return {
        "mappings": standard_data_store.list_mapping_profiles(tenant_id, entity_type),
    }


@router.post("/standardization/import")
async def standard_import(request: StandardImportRequest):
    try:
        return import_standard_rows(
            tenant_id=request.tenant_id,
            entity_type=request.entity_type,
            source_name=request.source_name,
            rows=request.rows,
            mapping=request.mapping,
            allow_warning=request.allow_warning,
            store=standard_data_store,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/standardization/import-file")
async def standard_import_file(
    file: UploadFile = File(...),
    entity_type: str = Form(...),
    mapping_json: str = Form(...),
    tenant_id: str = Form("default"),
    allow_warning: bool = Form(True),
):
    try:
        mapping = json.loads(mapping_json)
        if not isinstance(mapping, dict):
            raise ValueError("mapping_json 必须是对象")
        content = await file.read()
        return import_standard_file(
            tenant_id=tenant_id,
            entity_type=entity_type,
            filename=file.filename or "upload.csv",
            content=content,
            mapping=mapping,
            allow_warning=allow_warning,
            store=standard_data_store,
        )
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail="mapping_json 不是有效 JSON") from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/standardization/snapshots")
async def standard_snapshots(
    tenant_id: str = Query("default", min_length=1, max_length=64),
    entity_type: str = Query("", max_length=64),
    limit: int = Query(100, ge=1, le=500),
):
    return {
        "snapshots": standard_data_store.list_snapshots(tenant_id, entity_type, limit),
    }


@router.get("/standardization/snapshots/{snapshot_id}")
async def standard_snapshot_detail(
    snapshot_id: str,
    include_rows: bool = Query(False),
    row_limit: int = Query(100, ge=1, le=1000),
):
    try:
        snapshot = standard_data_store.get_snapshot(snapshot_id)
        rows = standard_data_store.get_snapshot_rows(snapshot_id, row_limit) if include_rows else []
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"snapshot": snapshot, "rows": rows}


@router.post("/diagnostics/store")
async def store_diagnosis(request: StoreDiagnosisRequest):
    try:
        snapshot_context = []
        for snapshot_id in request.snapshot_ids:
            snapshot = standard_data_store.get_snapshot(snapshot_id)
            if snapshot["tenant_id"] != request.tenant_id:
                raise ValueError(f"快照不属于当前租户: {snapshot_id}")
            snapshot_context.append(snapshot)
        diagnosis = analyze_store_diagnosis(
            current=request.current.model_dump(),
            previous=request.previous.model_dump(),
            snapshot_ids=request.snapshot_ids,
        )
        diagnosis["snapshot_context"] = snapshot_context
        run = standard_data_store.save_analysis_run(
            tenant_id=request.tenant_id,
            scenario="store_diagnosis",
            request=request.model_dump(),
            result=diagnosis,
            snapshot_ids=request.snapshot_ids,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"run": run, "diagnosis": diagnosis}


@router.post("/diagnostics/store/from-snapshots")
async def store_diagnosis_from_snapshots(request: SnapshotStoreDiagnosisRequest):
    try:
        current = aggregate_store_period(
            request.tenant_id,
            request.current_snapshot_ids,
            standard_data_store,
        )
        previous = aggregate_store_period(
            request.tenant_id,
            request.previous_snapshot_ids,
            standard_data_store,
        )
        all_snapshot_ids = [*request.current_snapshot_ids, *request.previous_snapshot_ids]
        diagnosis = analyze_store_diagnosis(
            current=current["values"],
            previous=previous["values"],
            snapshot_ids=all_snapshot_ids,
        )
        diagnosis["snapshot_aggregation"] = {
            "current": current,
            "previous": previous,
        }
        run = standard_data_store.save_analysis_run(
            tenant_id=request.tenant_id,
            scenario="store_diagnosis_from_snapshots",
            request=request.model_dump(),
            result=diagnosis,
            snapshot_ids=all_snapshot_ids,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"run": run, "diagnosis": diagnosis}


@router.post("/diagnostics/products")
async def product_diagnosis(request: ProductDiagnosisRequest):
    try:
        diagnosis = analyze_product_snapshots(
            tenant_id=request.tenant_id,
            current_snapshot_ids=request.current_snapshot_ids,
            previous_snapshot_ids=request.previous_snapshot_ids,
            product_ids=request.product_ids,
            ranking_weights=request.ranking_weights,
            thresholds=request.thresholds,
            store=standard_data_store,
        )
        snapshot_ids = list(dict.fromkeys([
            *request.current_snapshot_ids,
            *request.previous_snapshot_ids,
        ]))
        run = standard_data_store.save_analysis_run(
            tenant_id=request.tenant_id,
            scenario="product_diagnosis_from_snapshots",
            request=request.model_dump(),
            result=diagnosis,
            snapshot_ids=snapshot_ids,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"run": run, "diagnosis": diagnosis}


@router.post("/diagnostics/products/compare")
async def product_compare(request: ProductCompareRequest):
    try:
        return compare_products(request.products)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/products/{product_id}/profile")
async def product_profile(
    product_id: str,
    tenant_id: str = Query("default", min_length=1, max_length=64),
    current_snapshot_ids: list[str] = Query(...),
    previous_snapshot_ids: list[str] = Query(default=[]),
):
    try:
        diagnosis = analyze_product_snapshots(
            tenant_id=tenant_id,
            current_snapshot_ids=current_snapshot_ids,
            previous_snapshot_ids=previous_snapshot_ids,
            product_ids=[product_id],
            store=standard_data_store,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not diagnosis["profiles"]:
        raise HTTPException(status_code=404, detail=f"商品不存在或没有分析数据: {product_id}")
    return {
        "profile": diagnosis["profiles"][0],
        "rule_version": diagnosis["rule_version"],
        "snapshot_ids": diagnosis["snapshot_ids"],
    }


@router.get("/products/rankings")
async def product_rankings(
    tenant_id: str = Query("default", min_length=1, max_length=64),
    current_snapshot_ids: list[str] = Query(...),
    previous_snapshot_ids: list[str] = Query(default=[]),
    limit: int = Query(50, ge=1, le=500),
):
    try:
        diagnosis = analyze_product_snapshots(
            tenant_id=tenant_id,
            current_snapshot_ids=current_snapshot_ids,
            previous_snapshot_ids=previous_snapshot_ids,
            store=standard_data_store,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "rankings": diagnosis["rankings"][:limit],
        "total": len(diagnosis["rankings"]),
        "ranking_weights": diagnosis["ranking_weights"],
    }
