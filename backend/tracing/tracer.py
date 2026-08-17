"""
Langfuse追踪器 — NL2SQL流水线的全链路可观测。
追踪每个LangGraph节点的输入/输出/耗时/Token。
Langfuse未配置时自动降级为本地日志，不抛异常。

追踪字段: question, retrieved_schema, generated_sql, validation_result, execution_time_ms, token_usage, error
"""
import time
import json
import logging
import re
import uuid
from typing import Optional, Any
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# Lazy init
_langfuse_client: Optional[object] = None
_langfuse_initialized: bool = False

# Context variable for current trace_id (thread-safe)
_current_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def _get_langfuse():
    """首次使用时初始化 Langfuse；未配置时稳定降级为本地日志。"""
    global _langfuse_client, _langfuse_initialized

    if _langfuse_initialized:
        return _langfuse_client
    _langfuse_initialized = True

    try:
        from config import LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY
        if not LANGFUSE_PUBLIC_KEY or not LANGFUSE_SECRET_KEY:
            logger.info("[Tracer] Langfuse keys not configured; using local logs.")
            return None

        from langfuse import Langfuse
        _langfuse_client = Langfuse(
            public_key=LANGFUSE_PUBLIC_KEY,
            secret_key=LANGFUSE_SECRET_KEY,
            host=LANGFUSE_HOST or "https://cloud.langfuse.com",
        )
        logger.info("[Tracer] Langfuse connected: %s", LANGFUSE_HOST)
    except ImportError:
        logger.info("[Tracer] langfuse not installed; using local logs.")
    except Exception as error:
        logger.warning("[Tracer] Langfuse init failed: %s", error)
    return _langfuse_client

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

        logger.info(f"[Trace:{self.trace_id}] START — question='{_redact_text(self.question)[:80]}'")

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


_SENSITIVE_KEYS = {"password", "token", "api_key", "authorization", "email", "phone", "mobile", "id_card", "bank_card"}


def _redact_text(value: str) -> str:
    value = re.sub(r"\b1\d{10}\b", "***PHONE***", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "***EMAIL***", value)
    value = re.sub(r"\b(?:sk|ak)-[A-Za-z0-9_-]{8,}\b", "***TOKEN***", value)
    return value


def _safe_serialize(obj: Any, max_len: int = 500) -> Any:
    """序列化 Trace 数据并对凭证、手机号和邮箱做最小化脱敏。"""
    if obj is None:
        return None
    if isinstance(obj, str):
        value = _redact_text(obj)
        suffix = f"...[truncated, total {len(value)} chars]" if len(value) > max_len else ""
        return value[:max_len] + suffix
    if isinstance(obj, (int, float, bool)):
        return obj
    if isinstance(obj, dict):
        serialized = {}
        for key, value in obj.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                serialized[key] = "***REDACTED***"
            else:
                serialized[key] = _safe_serialize(value, max_len)
        return serialized
    if isinstance(obj, list):
        items = [_safe_serialize(item, max_len) for item in obj[:10]]
        if len(obj) > 10:
            items.append(f"...[{len(obj) - 10} more items]")
        return items
    try:
        return _redact_text(json.dumps(obj, ensure_ascii=False, default=str))[:max_len]
    except Exception:
        return type(obj).__name__

def get_current_trace_id() -> str:
    """Get the current trace ID for log correlation."""
    return _current_trace_id.get()
