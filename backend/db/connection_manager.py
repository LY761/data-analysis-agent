"""
数据库连接管理器 — 支持多数据库切换，运行时动态添加/切换数据源

支持的数据库类型:
  - SQLite: 本地文件数据库
  - MySQL: 远程数据库（需 pymysql）
  - PostgreSQL: 远程数据库（需 psycopg2）

使用方式:
  前端选择数据库 → API触发切换 → 自动发现Schema → 重新索引 → 立即可查
"""
import os
import logging
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 已注册的数据库连接
_connections: dict[str, dict] = {}
_current_db: str = "demo"


@dataclass
class DatabaseInfo:
    """数据库连接信息"""
    key: str           # 唯一标识
    label: str         # 显示名称
    db_type: str       # sqlite / mysql / postgresql
    path_or_url: str   # 文件路径 或 连接URL
    tables: list = field(default_factory=list)
    status: str = "disconnected"


def register_demo_db():
    """注册默认演示数据库"""
    from config import DEMO_DB_PATH
    _connections["demo"] = {
        "key": "demo",
        "label": "📦 演示数据库 (SQLite)",
        "db_type": "sqlite",
        "path_or_url": os.path.abspath(DEMO_DB_PATH),
        "tables": [],
        "status": "connected",
    }
    _current_db = "demo"
    return _connections["demo"]


def add_connection(key: str, label: str, db_type: str, path_or_url: str) -> dict:
    """添加新的数据库连接"""
    if key in _connections:
        return {"error": f"数据库 '{key}' 已存在"}

    # 测试连接
    test_result = _test_connection(db_type, path_or_url)
    if not test_result["ok"]:
        return {"error": test_result["error"]}

    # 发现表结构
    tables = _discover_tables(db_type, path_or_url)

    _connections[key] = {
        "key": key,
        "label": label,
        "db_type": db_type,
        "path_or_url": path_or_url,
        "tables": tables,
        "status": "connected",
    }
    logger.info(f"[DB Manager] 已添加: {label} ({db_type}) — {len(tables)}张表")
    return {"ok": True, "key": key, "tables": tables}


def switch_database(key: str) -> dict:
    """切换到指定数据库"""
    global _current_db
    if key not in _connections:
        return {"error": f"数据库 '{key}' 不存在"}
    if _connections[key]["status"] != "connected":
        return {"error": f"数据库 '{key}' 连接状态异常"}

    _current_db = key
    conn = _connections[key]

    # 更新执行器后端 — 关键：让查询真正走到对应数据库
    from db.executor import executor
    if conn["db_type"] == "sqlite":
        executor.set_backend("sqlite", conn["path_or_url"])
    elif conn["db_type"] == "mysql":
        executor.set_backend("mysql", conn["path_or_url"])
    elif conn["db_type"] == "postgresql":
        # 连接可注册/可发现表，但查询执行暂未实现（不假装支持）
        return {"error": "PostgreSQL 查询执行暂未实现，仅支持连接注册。请使用 SQLite 或 MySQL。",
                "current": key, "label": conn["label"]}

    # 重新索引 Schema
    _reindex_current_schema(conn["db_type"], conn["path_or_url"])

    logger.info(f"[DB Manager] 已切换到: {conn['label']} (backend={executor.backend})")
    return {"ok": True, "current": key, "label": conn["label"], "tables": conn["tables"]}


def get_current_db() -> dict:
    """获取当前使用的数据库信息"""
    return _connections.get(_current_db, _connections.get("demo", {}))


def list_connections() -> list:
    """列出所有已注册的数据库"""
    return [
        {"key": k, "label": v["label"], "db_type": v["db_type"],
         "tables_count": len(v.get("tables", [])), "status": v["status"],
         "is_current": k == _current_db}
        for k, v in _connections.items()
    ]


def remove_connection(key: str) -> dict:
    """移除数据库连接"""
    if key == "demo":
        return {"error": "不能删除演示数据库"}
    if key == _current_db:
        switch_database("demo")
    _connections.pop(key, None)
    return {"ok": True}


# ═══════════════════════════════════════════════════════
# 内部
# ═══════════════════════════════════════════════════════

def _test_connection(db_type: str, path_or_url: str) -> dict:
    """测试数据库连接"""
    if db_type == "sqlite":
        if os.path.exists(path_or_url):
            return {"ok": True}
        return {"ok": False, "error": f"SQLite文件不存在: {path_or_url}"}

    elif db_type == "mysql":
        try:
            import pymysql, urllib.parse
            url = urllib.parse.urlparse(path_or_url)
            conn = pymysql.connect(host=url.hostname, port=url.port or 3306,
                                   user=url.username, password=url.password,
                                   database=url.path.lstrip("/"), connect_timeout=5)
            conn.close()
            return {"ok": True}
        except ImportError:
            return {"ok": False, "error": "pymysql 未安装: pip install pymysql"}
        except Exception as e:
            return {"ok": False, "error": f"MySQL连接失败: {str(e)[:80]}"}

    elif db_type == "postgresql":
        try:
            import psycopg2
            conn = psycopg2.connect(path_or_url, connect_timeout=5)
            conn.close()
            return {"ok": True}
        except ImportError:
            return {"ok": False, "error": "psycopg2 未安装: pip install psycopg2-binary"}
        except Exception as e:
            return {"ok": False, "error": f"PostgreSQL连接失败: {str(e)[:80]}"}

    return {"ok": False, "error": f"不支持的数据库类型: {db_type}"}


def _discover_tables(db_type: str, path_or_url: str) -> list:
    """自动发现数据库中的表"""
    tables = []
    if db_type == "sqlite":
        import sqlite3
        conn = sqlite3.connect(path_or_url)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'query_cache' ORDER BY name"
        ).fetchall()
        for row in rows:
            cols = conn.execute(f"PRAGMA table_info('{row[0]}')").fetchall()
            tables.append({"name": row[0], "columns": len(cols)})
        conn.close()

    elif db_type == "mysql":
        try:
            import pymysql, urllib.parse
            url = urllib.parse.urlparse(path_or_url)
            conn = pymysql.connect(host=url.hostname, port=url.port or 3306,
                                   user=url.username, password=url.password,
                                   database=url.path.lstrip("/"))
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES")
                for row in cur.fetchall():
                    cur.execute(f"SELECT COUNT(*) FROM information_schema.columns WHERE table_name='{row[0]}'")
                    col_count = cur.fetchone()[0]
                    tables.append({"name": row[0], "columns": col_count})
            conn.close()
        except Exception:
            pass

    elif db_type == "postgresql":
        try:
            import psycopg2
            conn = psycopg2.connect(path_or_url)
            with conn.cursor() as cur:
                cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
                for row in cur.fetchall():
                    tables.append({"name": row[0], "columns": "?"})
            conn.close()
        except Exception:
            pass

    return tables


def _reindex_current_schema(db_type: str = "sqlite", path_or_url: str = ""):
    """切换数据库后重新索引Schema（按后端类型动态发现）"""
    try:
        from agent.schema_retriever import schema_retriever
        from db.init_db import get_schema_descriptions
        schemas = get_schema_descriptions(db_type, path_or_url)
        schema_retriever.index_schemas(schemas, force=True)
        logger.info("[DB Manager] Schema已重新索引")
    except Exception as e:
        logger.warning(f"[DB Manager] Schema重新索引失败: {e}")
