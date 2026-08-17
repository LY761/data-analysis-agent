"""将电商分析核心确定性能力暴露为 MCP 工具。"""

from __future__ import annotations

import os
from typing import Any

from mcp.server import MCPServer

from domain.ecommerce_metrics import analyze_store_metrics, compare_products

server = MCPServer(
    name="ecommerce-data-analysis",
    instructions="提供可审计的电商经营指标和商品对比工具。",
    version="1.0.0",
)


@server.tool(name="analyze_store_metrics", structured_output=True)
async def mcp_analyze_store_metrics(
    visitors: float,
    orders: float,
    gmv: float,
    ad_spend: float = 0,
    cost_of_goods: float = 0,
    refund_amount: float = 0,
    sold_units: float = 0,
    stock_units: float = 0,
    previous_gmv: float = 0,
) -> dict[str, Any]:
    """计算转化率、客单价、ROAS、退款率、毛利率、动销率和增长率。"""
    return analyze_store_metrics(
        visitors=visitors,
        orders=orders,
        gmv=gmv,
        ad_spend=ad_spend,
        cost_of_goods=cost_of_goods,
        refund_amount=refund_amount,
        sold_units=sold_units,
        stock_units=stock_units,
        previous_gmv=previous_gmv,
    )


@server.tool(name="compare_products", structured_output=True)
async def mcp_compare_products(products: list[dict[str, Any]]) -> dict[str, Any]:
    """按照销量、评分、评价数、毛利率和增长率进行可解释排序。"""
    return compare_products(products)


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    if transport == "streamable-http":
        server.run(
            transport=transport,
            host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("MCP_PORT", "8001")),
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
        )
    else:
        server.run(transport=transport)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass