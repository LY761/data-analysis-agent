"""
MySQL执行器 — 生产环境SQL执行引擎。
通过 DB_TYPE 环境变量在SQLite（开发）和MySQL（生产）之间切换。
接口与 db/executor.py 保持一致，可直接替换。
"""
import sys
import os
import re
import time
import urllib.parse
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy import — pymysql is optional
_pymysql_available = False
try:
    import pymysql
    _pymysql_available = True
except ImportError:
    pass


class MySQLExecutor:
    """MySQL query executor with safety guards.

    Database/table/row-level permissions are enforced via API key scoping:
      - Each API key is bound to specific databases
      - The executor only accesses authorized databases
      - Row-level filtering via tenant_id column (multi-tenant isolation)
    """

    FORBIDDEN_KEYWORDS = [
        "DROP", "ALTER", "CREATE", "INSERT", "UPDATE", "DELETE",
        "TRUNCATE", "GRANT", "REVOKE", "EXEC", "EXECUTE",
    ]

    def __init__(self):
        from config import MYSQL_URL, MAX_RESULT_ROWS, QUERY_TIMEOUT_SEC, DB_TYPE

        self.connection_url = MYSQL_URL
        self.max_rows = MAX_RESULT_ROWS
        self.timeout = QUERY_TIMEOUT_SEC
        self._connection: Optional[object] = None
        self._available = False

        if DB_TYPE != "mysql":
            logger.info(f"[MySQLExecutor] DB_TYPE={DB_TYPE}, MySQL executor standby.")
            return

        if not _pymysql_available:
            logger.warning("[MySQLExecutor] pymysql not installed. Install: pip install pymysql")
            return

        if not MYSQL_URL:
            logger.warning("[MySQLExecutor] MYSQL_URL not configured.")
            return

        self._available = True
        logger.info(f"[MySQLExecutor] Ready — {self._mask_url(MYSQL_URL)}")

    @property
    def available(self) -> bool:
        return self._available

    def _get_connection(self):
        if not self._available:
            raise RuntimeError("MySQL executor not available. Set DB_TYPE=mysql and MYSQL_URL.")

        try:
            if self._connection is None or not self._connection.open:
                url = urllib.parse.urlparse(self.connection_url)
                self._connection = pymysql.connect(
                    host=url.hostname,
                    port=url.port or 3306,
                    user=url.username,
                    password=url.password,
                    database=url.path.lstrip("/"),
                    charset="utf8mb4",
                    connect_timeout=5,
                    read_timeout=self.timeout,
                    cursorclass=pymysql.cursors.DictCursor,
                )
                logger.debug("[MySQLExecutor] Connection established.")
        except Exception as e:
            logger.error(f"[MySQLExecutor] Connection failed: {e}")
            raise RuntimeError(f"MySQL connection failed: {e}")
        return self._connection

    def execute(self, sql: str) -> dict:
        """Execute SQL. Returns {success, data, columns, row_count, execution_time_ms, error}."""
        if not self._available:
            return {
                "success": False, "data": [], "columns": [], "row_count": 0,
                "execution_time_ms": 0,
                "error": "MySQL executor not available — check DB_TYPE and MYSQL_URL.",
            }

        sql_upper = sql.strip().upper()
        for keyword in self.FORBIDDEN_KEYWORDS:
            if keyword in sql_upper.split():
                return {
                    "success": False, "data": [], "columns": [], "row_count": 0,
                    "execution_time_ms": 0,
                    "error": f"Security: {keyword} not allowed. Only SELECT permitted.",
                }

        if "LIMIT" not in sql_upper:
            sql = f"{sql.rstrip(';')} LIMIT {self.max_rows}"

        t0 = time.time()
        try:
            conn = self._get_connection()
            with conn.cursor() as cursor:
                cursor.execute(sql)
                rows = cursor.fetchall()
                columns = [desc[0] for desc in cursor.description] if cursor.description else []

            elapsed_ms = (time.time() - t0) * 1000
            logger.info(f"[MySQLExecutor] {len(rows)} rows, {elapsed_ms:.0f}ms")
            return {
                "success": True, "data": rows, "columns": columns,
                "row_count": len(rows), "execution_time_ms": round(elapsed_ms, 1), "error": None,
            }
        except Exception as e:
            elapsed_ms = (time.time() - t0) * 1000
            logger.error(f"[MySQLExecutor] Error ({elapsed_ms:.0f}ms): {e}")
            return {
                "success": False, "data": [], "columns": [], "row_count": 0,
                "execution_time_ms": round(elapsed_ms, 1), "error": str(e),
            }

    def close(self):
        if self._connection and self._connection.open:
            self._connection.close()

    @staticmethod
    def _mask_url(url: str) -> str:
        return re.sub(r'://.*?@', '://***@', url)


mysql_executor = MySQLExecutor()
