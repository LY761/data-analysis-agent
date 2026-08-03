"""
查询缓存 — 基于 SQLite 的热点查询结果缓存（零额外依赖）
对相同问题+相同Schema → 直接返回缓存结果，跳过LLM调用。

存储位置: 缓存表建在 demo_sales.db 中，不额外创建文件
缓存键: MD5(用户问题 + Schema指纹) → Schema变了自动失效
TTL: 5分钟（可配），过期自动清理
"""
import hashlib
import json
import time
import sqlite3
import logging
from config import DEMO_DB_PATH, CACHE_TTL

logger = logging.getLogger(__name__)

_stats = {"hits": 0, "misses": 0}
_table_created = False


def _ensure_table():
    """确保缓存表存在（首次调用时自动创建）"""
    global _table_created
    if _table_created:
        return
    conn = sqlite3.connect(DEMO_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS query_cache (
            cache_key TEXT PRIMARY KEY,
            question TEXT,
            result_json TEXT,
            created_at REAL,
            expires_at REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_expires ON query_cache(expires_at)")
    conn.commit()
    conn.close()
    _table_created = True


def _build_key(question: str, schema_hash: str = "") -> str:
    """构建缓存键: MD5(问题 + Schema指纹)"""
    raw = f"{question}|{schema_hash}"
    return hashlib.md5(raw.encode()).hexdigest()


def get_cached_result(question: str, schema_hash: str = "") -> dict | None:
    """
    查缓存。命中返回结果dict，未命中返回None。
    """
    _ensure_table()
    _cleanup_expired()

    try:
        key = _build_key(question, schema_hash)
        conn = sqlite3.connect(DEMO_DB_PATH)
        row = conn.execute(
            "SELECT result_json FROM query_cache WHERE cache_key = ? AND expires_at > ?",
            (key, time.time())
        ).fetchone()
        conn.close()

        if row:
            _stats["hits"] += 1
            logger.info(f"[Cache] HIT — key={key[:12]}... (hits={_stats['hits']}, misses={_stats['misses']})")
            return json.loads(row[0])
        else:
            _stats["misses"] += 1
            return None
    except Exception as e:
        logger.warning(f"[Cache] Get failed: {e}")
        _stats["misses"] += 1
        return None


def set_cached_result(question: str, schema_hash: str, result: dict, ttl: int = None) -> None:
    """
    写缓存。
    ttl: 过期时间（秒），默认用配置的 CACHE_TTL
    """
    _ensure_table()
    if ttl is None:
        ttl = CACHE_TTL

    try:
        key = _build_key(question, schema_hash)
        now = time.time()
        conn = sqlite3.connect(DEMO_DB_PATH)
        conn.execute(
            """INSERT OR REPLACE INTO query_cache (cache_key, question, result_json, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (key, question[:200], json.dumps(result, ensure_ascii=False, default=str), now, now + ttl)
        )
        conn.commit()
        conn.close()
        logger.debug(f"[Cache] SET — key={key[:12]}... ttl={ttl}s")
    except Exception as e:
        logger.warning(f"[Cache] Set failed: {e}")


def _cleanup_expired():
    """清理过期缓存（每10次查询清一次）"""
    if _stats["hits"] + _stats["misses"] == 0 or (_stats["hits"] + _stats["misses"]) % 10 != 0:
        return
    try:
        conn = sqlite3.connect(DEMO_DB_PATH)
        deleted = conn.execute("DELETE FROM query_cache WHERE expires_at < ?", (time.time(),)).rowcount
        conn.commit()
        conn.close()
        if deleted:
            logger.debug(f"[Cache] Cleaned {deleted} expired entries")
    except Exception:
        pass


def get_cache_stats() -> dict:
    """返回缓存统计"""
    total = _stats["hits"] + _stats["misses"]
    return {
        "hits": _stats["hits"],
        "misses": _stats["misses"],
        "hit_rate": round(_stats["hits"] / total * 100, 1) if total > 0 else 0,
        "total": total,
    }
