"""
MySQL执行器 — 生产环境SQL执行引擎。
通过 DB_TYPE 环境变量在SQLite（开发）和MySQL（生产）之间切换。
接口与 db/executor.py 保持一致，可直接替换。

v2:
  - SQLite→MySQL 方言自动翻译（strftime→DATE_FORMAT / date('now')→CURDATE）
  - 线程安全（连接池化于单连接 + 全局锁）
  - set_connection_url() 支持运行时切换目标库（connection_manager 用）
  - 返回结构与 db/executor.SQLExecutor 统一（含 warning 字段）
"""
import re
import time
import threading
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


def translate_sqlite_to_mysql(sql: str) -> str:
    """把 SQLite 专属语法翻译为 MySQL 语法。

    覆盖项目内最常见的模式：
      strftime('%Y-%m', col)               → DATE_FORMAT(col, '%Y-%m')
      strftime('%Y-%m', 'now')             → DATE_FORMAT(NOW(), '%Y-%m')
      strftime('%Y-%m', 'now', '-1 month') → DATE_FORMAT(DATE_SUB(NOW(), INTERVAL 1 MONTH), '%Y-%m')
      date('now')                          → CURDATE()
      datetime('now')                      → NOW()
    """
    if "strftime" not in sql and "date('now')" not in sql and "datetime('now')" not in sql:
        return sql

    # 1) 带相对偏移：strftime(fmt, 'now', '±N unit')
    def _offset_repl(m: re.Match) -> str:
        fmt = m.group(1)
        sign = m.group(2)
        unit = m.group(3)
        fn = "DATE_SUB" if sign.startswith("-") else "DATE_ADD"
        return f"DATE_FORMAT({fn}(NOW(), INTERVAL {abs(int(sign))} {unit.upper()}), '{fmt}')"

    s = re.sub(
        r"strftime\(\s*'([^']+)'\s*,\s*'now'\s*,\s*'([+-]\d+)\s+(month|day|year)s?'\s*\)",
        _offset_repl, sql, flags=re.IGNORECASE,
    )

    # 2) 列引用：strftime(fmt, table.col) / strftime(fmt, col)
    def _col_repl(m: re.Match) -> str:
        return f"DATE_FORMAT({m.group(2)}, '{m.group(1)}')"

    s = re.sub(
        r"strftime\(\s*'([^']+)'\s*,\s*(\w+(?:\.\w+)?)\s*\)",
        _col_repl, s, flags=re.IGNORECASE,
    )

    # 3) 'now' 无偏移
    def _now_repl(m: re.Match) -> str:
        return f"DATE_FORMAT(NOW(), '{m.group(1)}')"

    s = re.sub(
        r"strftime\(\s*'([^']+)'\s*,\s*'now'\s*\)",
        _now_repl, s, flags=re.IGNORECASE,
    )

    # 4) date('now') / datetime('now')
    s = re.sub(r"date\(\s*'now'\s*\)", "CURDATE()", s, flags=re.IGNORECASE)
    s = re.sub(r"datetime\(\s*'now'\s*\)", "NOW()", s, flags=re.IGNORECASE)
    return s


class MySQLExecutor:
    """MySQL query executor with safety guards (thread-safe, dialect-adaptive)."""

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
        self._lock = threading.Lock()

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

    def set_connection_url(self, url: str):
        """运行时切换目标库（connection_manager 切换数据库时调用）。

        切换后保留已建立的连接处理：若连接目标库不同则断开，懒重建。
        """
        if not url:
            return
        with self._lock:
            if self._connection is not None and self._connection.open:
                try:
                    self._connection.close()
                except Exception:
                    pass
            self._connection = None
        self.connection_url = url
        # 有 URL 即视为可用（连接在 execute 时懒建立，失败会明确报错）
        self._available = True
        logger.info(f"[MySQLExecutor] 切换连接 → {self._mask_url(url)}")

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

    def execute(self, sql: str, params: tuple = None) -> dict:
        """Execute SQL（支持参数化，? 占位符自动转 %s）。返回统一结构。"""
        if not self._available:
            return {
                "success": False, "data": [], "columns": [], "row_count": 0,
                "execution_time_ms": 0, "warning": None,
                "error": "MySQL executor not available — check DB_TYPE and MYSQL_URL.",
            }

        # SQLite → MySQL 方言翻译（快捷卡/LLM生成的SQL都含 strftime）
        sql = translate_sqlite_to_mysql(sql)
        if params:
            sql = sql.replace("?", "%s")  # 参数占位符 SQLite ? → MySQL %s

        sql_upper = sql.strip().upper()
        for keyword in self.FORBIDDEN_KEYWORDS:
            if re.search(r"" + keyword + r"", sql_upper):
                return {
                    "success": False, "data": [], "columns": [], "row_count": 0,
                    "execution_time_ms": 0, "warning": None,
                    "error": f"Security: {keyword} not allowed. Only SELECT permitted.",
                }

        if "LIMIT" not in sql_upper:
            sql = sql.rstrip(";").strip() + f" LIMIT {self.max_rows}"

        t0 = time.time()
        try:
            with self._lock:
                conn = self._get_connection()
                with conn.cursor() as cursor:
                    cursor.execute(sql, params or ())
                    rows = cursor.fetchall()
                    columns = [desc[0] for desc in cursor.description] if cursor.description else []

            elapsed_ms = (time.time() - t0) * 1000
            logger.info(f"[MySQLExecutor] {len(rows)} rows, {elapsed_ms:.0f}ms")

            if not rows:
                return {
                    "success": True, "data": [], "columns": [], "row_count": 0,
                    "execution_time_ms": round(elapsed_ms, 1),
                    "warning": "查询结果为空，可能条件过于严格。",
                    "error": None,
                }

            result = {
                "success": True, "data": rows, "columns": columns,
                "row_count": len(rows), "execution_time_ms": round(elapsed_ms, 1),
                "warning": None, "error": None,
            }
            if len(rows) >= self.max_rows:
                result["warning"] = f"结果已截断至{self.max_rows}行。"
            if result["execution_time_ms"] > 5000:
                result["warning"] = (result["warning"] or "") + f" 查询耗时{result['execution_time_ms']}ms。"
            return result
        except Exception as e:
            elapsed_ms = (time.time() - t0) * 1000
            logger.error(f"[MySQLExecutor] Error ({elapsed_ms:.0f}ms): {e}")
            return {
                "success": False, "data": [], "columns": [], "row_count": 0,
                "execution_time_ms": round(elapsed_ms, 1), "warning": None, "error": str(e),
            }

    def close(self):
        with self._lock:
            if self._connection and self._connection.open:
                self._connection.close()

    @staticmethod
    def _mask_url(url: str) -> str:
        return re.sub(r'://.*?@', '://***@', url)


mysql_executor = MySQLExecutor()
