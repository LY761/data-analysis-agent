from agent.agent_router import agent_router
from domain.metric_registry import metric_registry


def test_core_modules_import():
    assert metric_registry.get("average_order_value").name == "客单价"


def test_router_uses_deterministic_fast_paths():
    assert agent_router.route("你好")["mode"] == "chat"
    assert agent_router.route("什么是客单价")["mode"] == "chat"
    assert agent_router.route("本月销售额")["mode"] in {"quick_card", "sql_query"}