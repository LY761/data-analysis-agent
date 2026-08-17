"""电商标准模型、指标语义和数据质量 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from domain.ecommerce_schema import entity_catalog, get_entity, suggest_field_mapping
from domain.metric_registry import metric_registry
from services.data_ingest import inspect_file
from services.data_quality import check_data_quality

router = APIRouter(tags=["semantic-layer"])


class MappingSuggestRequest(BaseModel):
    entity_type: str = Field(..., min_length=1, max_length=64)
    source_fields: list[str] = Field(..., min_length=1, max_length=100)


class DataQualityRequest(BaseModel):
    entity_type: str = Field(..., min_length=1, max_length=64)
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=10000)
    mapping: dict[str, str] | None = None


class MetricCalculateRequest(BaseModel):
    metric_keys: list[str] = Field(..., min_length=1, max_length=50)
    values: dict[str, float] = Field(default_factory=dict)


@router.get("/semantic/entities")
async def semantic_entities():
    return entity_catalog()


@router.get("/semantic/entities/{entity_type}")
async def semantic_entity_detail(entity_type: str):
    try:
        return get_entity(entity_type).to_public_dict()
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/semantic/metrics")
async def semantic_metrics():
    return metric_registry.catalog()


@router.get("/semantic/metrics/{metric_key}")
async def semantic_metric_detail(metric_key: str):
    try:
        return metric_registry.get(metric_key).to_public_dict()
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/semantic/metrics/calculate")
async def calculate_semantic_metrics(request: MetricCalculateRequest):
    try:
        metrics = metric_registry.calculate_many(request.metric_keys, request.values)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "schema_version": "1.0",
        "metric_versions": {
            key: metric_registry.get(key).version for key in request.metric_keys
        },
        "metrics": metrics,
    }


@router.post("/semantic/mappings/suggest")
async def semantic_mapping_suggest(request: MappingSuggestRequest):
    try:
        return suggest_field_mapping(request.entity_type, request.source_fields)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/data-quality/check")
async def data_quality_check(request: DataQualityRequest):
    try:
        return check_data_quality(request.entity_type, request.rows, request.mapping)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/data-quality/inspect-file")
async def data_quality_inspect_file(
    file: UploadFile = File(...),
    entity_type: str = Form(...),
):
    content = await file.read()
    try:
        result = inspect_file(file.filename or "upload.csv", content, entity_type)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result
