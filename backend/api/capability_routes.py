"""供 Web 工作台使用的能力目录与统一执行 API。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from capabilities import RiskLevel, capability_registry
from capabilities.registry import CapabilityNotFoundError, CapabilityUnavailableError

router = APIRouter(prefix="/capabilities", tags=["capabilities"])


class CapabilityExecuteRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = True


@router.get("")
async def capability_catalog():
    return capability_registry.catalog()


@router.get("/modules/{module_id}")
async def capability_module(module_id: str):
    try:
        module = capability_registry.get_module(module_id)
    except CapabilityNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    capabilities = [
        capability_registry.get_capability(capability_id).to_public_dict()
        for capability_id in module.capability_ids
    ]
    return {"module": module.to_public_dict(), "capabilities": capabilities}


@router.get("/{capability_id}")
async def capability_detail(capability_id: str):
    try:
        return capability_registry.get_capability(capability_id).to_public_dict()
    except CapabilityNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/{capability_id}/execute")
async def execute_capability(capability_id: str, request: CapabilityExecuteRequest):
    try:
        definition = capability_registry.get_capability(capability_id)
        if definition.risk_level in {RiskLevel.BUSINESS_CHANGE, RiskLevel.FINANCIAL_ACTION} and not request.dry_run:
            raise HTTPException(status_code=403, detail="高风险能力必须通过审批工作流执行")
        result = await capability_registry.execute(capability_id, request.inputs)
    except CapabilityNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except CapabilityUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except TypeError as error:
        raise HTTPException(status_code=400, detail=f"输入参数不完整或格式错误: {error}") from error

    return {
        "schema_version": "1.0",
        "run_id": uuid.uuid4().hex,
        "capability_id": capability_id,
        "runtime": definition.runtime.value,
        "dry_run": request.dry_run,
        "result": result,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
