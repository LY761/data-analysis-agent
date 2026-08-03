"""
Langfuse追踪器 — NL2SQL流水线的全链路可观测。
追踪每个LangGraph节点的输入/输出/耗时/Token。
Langfuse未配置时自动降级为本地日志，不抛异常。

追踪字段: question, retrieved_schema, generated_sql, validation_result, execution_time_ms, token_usage, error
"""
import time
import json
import logging
import uuid
from typing import Optional, Any
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# Lazy init
_langfuse_client: Optional[object] = None
_langfuse_available: bool = False

# Context variable for current trace_id (thread-safe)
_current_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def _get_langfuse():
    """Lazy-init Langfuse client. Returns None if not configured."""
    global _langfuse_client, _langfuse_available

    if _langfuse_client is not None:
        return _langfuse_client
    if not _langfuse_available and _langfuse_client is None:
        return None

    try:
        from config import LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST
        if not LANGFUSE_PUBLIC_KEY or not LANGFUSE_SECRET_KEY:
            logger.info("[Tracer] Langfuse keys not configured — tracing via local logs only.")
            _langfuse_available = False
            return None

        from langfuse import Langfuse
        _langfuse_client = Langfuse(
            public_key=LANGFUSE_PUBLIC_KEY,
            secret_key=LANGFUSE_SECRET_KEY,
            host=LANGFUSE_HOST or "https://cloud.langfuse.com",
        )
        _langfuse_available = True
        logger.info(f"[Tracer] Langfuse connected: {LANGFUSE_HOST or 'cloud.langfuse.com'}")
        return _langfuse_client
    except ImportError:
        logger.info("[Tracer] langfuse not installed — tracing via local logs only.")
        _langfuse_available = False
        return None
    except Exception as e:
        logger.warning(f"[Tracer] Langfuse init failed ({e}) — tracing via local logs only.")
        _langfuse_available = False
        return None


class TraceContext:
    """A single NL2SQL query trace. Context-managed, auto-flushes."""

    def __init__(self, question: str = ""):
        self.trace_id = uuid.uuid4().hex[:12]
        self.question = question
        self.start_time: float = 0.0
        self.spans: list[dict] = []
        self._active = False

    def start(self):
        """Begin the trace."""
        self.start_time = time.time()
        self._active = True
        _current_trace_id.set(self.trace_id)

        lf = _get_langfuse()
        if lf:
            try:
                lf.trace(id=self.trace_id, name="nl2sql-query",
                         input={"question": self.question})
            except Exception as e:
                logger.warning(f"[Tracer] Langfuse trace start failed: {e}")

        logger.info(f"[Trace:{self.trace_id}] START — question='{self.question[:80]}'")

    def span(self, name: str, input_data: Any = None, output_data: Any = None,
             metadata: dict = None) -> dict:
        """Record a span (one node in the workflow)."""
        span = {
            "name": name,
            "timestamp": time.time(),
            "input": _safe_serialize(input_data),
            "output": _safe_serialize(output_data),
            "metadata": metadata or {},
        }
        self.spans.append(span)

        lf = _get_langfuse()
        if lf:
            try:
                lf.span(
                    trace_id=self.trace_id, name=name,
                    input=input_data, output=output_data, metadata=metadata,
                )
            except Exception as e:
                logger.warning(f"[Tracer] Langfuse span '{name}' failed: {e}")

        meta_str = json.dumps(metadata, ensure_ascii=False) if metadata else ""
        logger.info(f"[Trace:{self.trace_id}] SPAN '{name}' {meta_str}")
        return span

    def end(self, final_response: dict = None, error: str = None):
        """End the trace with final output."""
        total_ms = (time.time() - self.start_time) * 1000

        lf = _get_langfuse()
        if lf:
            try:
                lf.trace(id=self.trace_id, name="nl2sql-query",
                         output=final_response,
                         metadata={"total_ms": round(total_ms, 1),
                                   "span_count": len(self.spans), "error": error})
                lf.flush()
            except Exception as e:
                logger.warning(f"[Tracer] Langfuse trace end failed: {e}")

        status = "ERROR" if error else "OK"
        logger.info(f"[Trace:{self.trace_id}] END — {status} total={total_ms:.0f}ms "
                    f"spans={len(self.spans)}")
        self._active = False
        _current_trace_id.set("")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        error = str(exc_val) if exc_val else None
        self.end(error=error)
        return False


def _safe_serialize(obj: Any, max_len: int = 500) -> Any:
    """Safely serialize for tracing, truncating long strings."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        if isinstance(obj, str) and len(obj) > max_len:
            return obj[:max_len] + f"...[truncated, total {len(obj)} chars]"
        return obj
    if isinstance(obj, dict):
        return {k: _safe_serialize(v, max_len) for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) > 10:
            return [_safe_serialize(item, max_len) for item in obj[:10]] + \
                   [f"...[{len(obj) - 10} more items]"]
        return [_safe_serialize(item, max_len) for item in obj]
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)[:max_len]
    except Exception:
        return str(type(obj).__name__)


def get_current_trace_id() -> str:
    """Get the current trace ID for log correlation."""
    return _current_trace_id.get()
