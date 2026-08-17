"""三条可重复演示链路定义。"""

from __future__ import annotations


GOLDEN_SCENARIOS = (
    {
        "id": "deterministic_product_diagnosis",
        "name": "确定性商品诊断",
        "runtime": "domain_service",
        "steps": [
            "导入 Olist 当前与上一周期快照",
            "调用 /api/diagnostics/products",
            "查看商品标签、SKU 下钻和下滑贡献",
            "核对公式、版本和快照证据",
        ],
        "assertions": [
            "所有数值来自标准快照",
            "标签由版本化规则计算",
            "LLM 不参与指标和风险等级计算",
        ],
    },
    {
        "id": "agent_nl2sql",
        "name": "Agent 自然语言问数",
        "runtime": "agent",
        "steps": [
            "提交自然语言经营问题",
            "观察意图路由和必要澄清",
            "检索 Schema 并生成只读 SQL",
            "执行 SQL 后返回图表和自然语言解释",
        ],
        "assertions": [
            "危险 SQL 被四道安全闸门拒绝",
            "答案数字来自 SQL 执行结果",
            "模型失败时返回明确降级结果",
        ],
    },
    {
        "id": "workflow_low_stock",
        "name": "低库存任务工作流",
        "runtime": "workflow",
        "steps": [
            "调用 /api/workflows/low_stock_task/run",
            "执行商品诊断和低库存条件",
            "创建内部补货任务",
            "查看步骤记录、幂等去重和重试信息",
        ],
        "assertions": [
            "Agent 不控制阈值和状态流转",
            "相同幂等键不会重复运行",
            "相同业务快照不会重复创建任务",
        ],
    },
)
