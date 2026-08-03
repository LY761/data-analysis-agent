"""
调试链路追踪 — 每个节点执行完自动记录状态，查询结束时打印完整链路

启用方式: .env 中设置 DEBUG_TRACE=true
输出示例:
  ═══ 查询链路追踪 ═══
  ✅ retrieve_schema    120ms  → 3张表(orders,products,order_items)
  ✅ clarify_question     0ms  → 规则快筛通过
  ✅ classify_intent      0ms  → ranking(规则)
  ✅ generate_sql      2100ms  → 86字符SQL
  ✅ validate_sql        2ms  → all_passed
  ✅ execute_sql        45ms  → 5行结果
  ✅ build_response      1ms
  ═══ 总耗时: 2269ms ═══

如果某节点失败，会显示 ❌ 和原因。
"""
import time
import logging
from typing import Optional
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# 当前请求的追踪记录
_current_trace: ContextVar[Optional["TraceLog"]] = ContextVar("trace", default=None)


class TraceLog:
    """单次查询的链路追踪记录"""

    def __init__(self, question: str):
        self.question = question
        self.nodes: list[dict] = []
        self.start_time = time.time()
        _current_trace.set(self)

    def record(self, node: str, status: str, detail: str = "", ms: float = 0):
        self.nodes.append({
            "node": node,
            "status": status,  # "ok" | "error" | "skipped"
            "detail": detail,
            "ms": round(ms, 1),
        })

    def summary(self) -> str:
        total = (time.time() - self.start_time) * 1000
        lines = ["\n" + "=" * 55]
        lines.append(f"  查询链路追踪: {self.question[:50]}")
        lines.append("=" * 55)

        for n in self.nodes:
            icon = {"ok": "✅", "error": "❌", "skipped": "⏭️"}.get(n["status"], "?")
            lines.append(f"  {icon} {n['node']:20s} {n['ms']:>6.0f}ms  {n['detail']}")

        lines.append("-" * 55)
        lines.append(f"  总耗时: {total:.0f}ms")
        lines.append("=" * 55)
        return "\n".join(lines)

    def print(self):
        """打印完整链路到控制台"""
        print(self.summary())

    @staticmethod
    def current() -> Optional["TraceLog"]:
        return _current_trace.get()

    @staticmethod
    def clear():
        _current_trace.set(None)
