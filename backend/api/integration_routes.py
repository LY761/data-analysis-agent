"""面向企业系统、渠道网关和外部 Agent 的稳定集成 API。"""

from __future__ import annotations

import hmac
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from capabilities import capability_registry
from config import APP_ENV, INTEGRATION_TOKEN

router = APIRouter(prefix="/integrations/v1", tags=["integrations"])


class IntegrationRequest(BaseModel):
    tenant_id: str = Field("default", min_length=1, max_length=64)
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=8, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StoreMetricsRequest(IntegrationRequest):
    visitors: float
    orders: float
    gmv: float
    ad_spend: float = 0
    cost_of_goods: float = 0
    refund_amount: float = 0
    sold_units: float = 0
    stock_units: float = 0
    previous_gmv: float = 0


class ProductCompareRequest(IntegrationRequest):
    products: list[dict]


def _verify_token(x_integration_token: str = Header("")) -> None:
    if not INTEGRATION_TOKEN:
        if APP_ENV != "demo":
            raise HTTPException(status_code=503, detail="integration token is not configured")
        return
    if not hmac.compare_digest(x_integration_token, INTEGRATION_TOKEN):
        raise HTTPException(status_code=401, detail="invalid integration token")


def _envelope(tool: str, request: IntegrationRequest, data: dict) -> dict:
    return {
        "schema_version": "1.0",
        "request_id": request.request_id,
        "tenant_id": request.tenant_id,
        "tool": tool,
        "data": data,
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "data-analysis-agent",
        },
    }


@router.get("/capabilities")
async def integration_capabilities(x_integration_token: str = Header("")):
    _verify_token(x_integration_token)
    return {
        "schema_version": "1.0",
        "service": "data-analysis-agent",
        "transports": ["rest", "mcp-streamable-http", "mcp-stdio"],
        "tools": ["analytics.store_metrics", "analytics.compare_products"],
    }


@router.post("/analytics/store")
async def integration_store_metrics(
    request: StoreMetricsRequest,
    x_integration_token: str = Header(""),
):
    _verify_token(x_integration_token)
    data = await capability_registry.execute(
        "analytics.store_metrics",
        request.model_dump(exclude={"tenant_id", "request_id", "metadata"}),
    )
    return _envelope("analytics.store_metrics", request, data)


@router.post("/analytics/products/compare")
async def integration_compare_products(
    request: ProductCompareRequest,
    x_integration_token: str = Header(""),
):
    _verify_token(x_integration_token)
    data = await capability_registry.execute("analytics.compare_products", {"products": request.products})
    return _envelope("analytics.compare_products", request, data)