"""简单工作流、运行记录和任务 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from services.workflow_engine import WorkflowExecutionError, workflow_engine


router = APIRouter(tags=["workflows"])


class WorkflowStepRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=64)
    type: str = Field(..., min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


class WorkflowCreateRequest(BaseModel):
    tenant_id: str = Field("default", min_length=1, max_length=64)
    workflow_id: str = Field("", max_length=128)
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field("", max_length=500)
    enabled: bool = True
    triggers: list[str] = Field(..., min_length=1, max_length=10)
    steps: list[WorkflowStepRequest] = Field(..., min_length=1, max_length=20)


class WorkflowRunRequest(BaseModel):
    tenant_id: str = Field("default", min_length=1, max_length=64)
    trigger_type: str = Field("manual", min_length=1, max_length=64)
    idempotency_key: str = Field("", max_length=256)
    inputs: dict[str, Any] = Field(default_factory=dict)


class WorkflowRetryRequest(BaseModel):
    tenant_id: str = Field("default", min_length=1, max_length=64)


@router.post("/workflows")
async def create_workflow(request: WorkflowCreateRequest):
    try:
        payload = request.model_dump(exclude={"tenant_id"})
        return workflow_engine.create_definition(request.tenant_id, payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/workflows")
async def list_workflows(
    tenant_id: str = Query("default", min_length=1, max_length=64),
):
    return {"workflows": workflow_engine.list_definitions(tenant_id)}


@router.post("/workflows/{workflow_id}/run")
async def run_workflow(workflow_id: str, request: WorkflowRunRequest):
    try:
        return workflow_engine.run(
            workflow_id,
            request.tenant_id,
            request.inputs,
            trigger_type=request.trigger_type,
            idempotency_key=request.idempotency_key,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WorkflowExecutionError as error:
        raise HTTPException(
            status_code=500,
            detail={"message": str(error), "run_id": error.run_id},
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/workflow-runs/{run_id}")
async def workflow_run_detail(
    run_id: str,
    tenant_id: str = Query("default", min_length=1, max_length=64),
):
    try:
        return workflow_engine.get_run(run_id, tenant_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/workflow-runs/{run_id}/retry")
async def retry_workflow(run_id: str, request: WorkflowRetryRequest):
    try:
        return workflow_engine.retry(run_id, request.tenant_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WorkflowExecutionError as error:
        raise HTTPException(
            status_code=500,
            detail={"message": str(error), "run_id": error.run_id},
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/tasks")
async def list_tasks(
    tenant_id: str = Query("default", min_length=1, max_length=64),
    status: str = Query("", max_length=32),
    limit: int = Query(100, ge=1, le=500),
):
    return {
        "tasks": workflow_engine.store.list_tasks(tenant_id, status, limit),
    }
