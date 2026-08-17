"""
SQL执行器 — 带安全控制的只读SQL执行引擎
"""
import sqlite3
import time
from config import DEMO_DB_PATH, MAX_RESULT_ROWS, QUERY_TIMEOUT_SEC, DB_TYPE


class SQLExecutor:
    """安全的SQL执行器：只能跑SELECT，带超时和行数限制"""

    # 禁止的关键词（防止LLM幻觉生成写操作）
    FORBIDDEN_KEYWORDS = [
        "DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "CREATE",
        "TRUNCATE", "REPLACE", "GRANT", "REVOKE", "ATTACH", "DETACH",
        "PRAGMA", "VACUUM", "REINDEX",
    ]

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DEMO_DB_PATH

    def _is_read_only(self, sql: str) -> tuple[bool, str]:
        """安全检查：只允许SELECT查询，拦截所有写操作"""
        sql_upper = sql.strip().upper()
        for keyword in self.FORBIDDEN_KEYWORDS:
            import re
            # \b边界匹配，防止误杀（比如字段名含'update_time'不会被拦）
            if re.search(r'\b' + keyword + r'\b', sql_upper):
                return False, f"禁止的操作：{keyword}。仅允许 SELECT 查询。"
        if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
            return False, "仅允许 SELECT 查询语句。"
        return True, "OK"

    def execute(self, sql: str, params: tuple = None) -> dict:
        """执行只读SQL查询，返回结构化结果。
        params: 参数化占位符（?）的绑定值 — 调用方拼接用户输入时应优先传参。"""
        # 第一步：安全检查
        is_safe, reason = self._is_read_only(sql)
        if not is_safe:
            return {"success": False, "error": reason, "data": None}

        # 第二步：如果SQL没有LIMIT，自动追加行数上限
        if "LIMIT" not in sql.upper():
            sql = sql.rstrip(";").strip() + f" LIMIT {MAX_RESULT_ROWS}"

        conn = None
        start_time = time.time()
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # SQLite 没有可靠的 statement_timeout；用 VM progress handler 按截止时间中断。
            deadline = time.monotonic() + QUERY_TIMEOUT_SEC
            conn.set_progress_handler(
                lambda: 1 if time.monotonic() >= deadline else 0,
                1000,
            )
            conn.execute("PRAGMA query_only = ON")

            cursor.execute(sql, params or ())
            rows = cursor.fetchall()

            # 空结果处理
            if not rows:
                return {
                    "success": True,
                    "error": None,
                    "data": [],
                    "columns": [],
                    "row_count": 0,
                    "execution_time_ms": round((time.time() - start_time) * 1000, 2),
                    "warning": "查询结果为空，可能条件过于严格。建议检查WHERE条件或去掉部分过滤。",
                }

            columns = [desc[0] for desc in cursor.description]
            data = [dict(row) for row in rows]

            result = {
                "success": True,
                "error": None,
                "data": data,
                "columns": columns,
                "row_count": len(data),
                "execution_time_ms": round((time.time() - start_time) * 1000, 2),
                "warning": None,
            }

            # 行数告警
            if len(data) >= MAX_RESULT_ROWS:
                result["warning"] = f"结果已截断至{MAX_RESULT_ROWS}行。建议添加更精确的过滤条件或使用分页。"
            # 慢查询告警
            if result["execution_time_ms"] > 5000:
                result["warning"] = (result["warning"] or "") + f" 查询耗时{result['execution_time_ms']}ms，建议优化查询或添加索引。"

            return result

        except sqlite3.OperationalError as error:
            if "interrupted" in str(error).lower():
                message = f"查询超过 {QUERY_TIMEOUT_SEC} 秒，已安全中断。请缩小时间范围或增加过滤条件。"
            else:
                message = f"SQL执行失败：{error}。请检查表名、字段名和查询条件。"
            return {"success": False, "error": message, "data": None}
        except Exception as e:
            return {
                "success": False,
                "error": f"执行异常：{str(e)}",
                "data": None,
            }
        finally:
            if conn:
                conn.set_progress_handler(None, 0)
                conn.close()


class ExecutorProxy:
    """后端代理：按当前激活数据库类型分发执行。

    - 默认按 config.DB_TYPE 决定（sqlite / mysql）
    - connection_manager.switch_database() 切换时调用 set_backend()
    - 所有 `from db.executor import executor` 的调用方无需改动
    """

    def __init__(self):
        self._backend = "sqlite"
        self._sqlite_executor = SQLExecutor(DEMO_DB_PATH)
        self._mysql_executor = None
        if DB_TYPE == "mysql":
            self.set_backend("mysql")

    @property
    def backend(self) -> str:
        return self._backend

    def set_backend(self, db_type: str, path_or_url: str = None):
        """切换执行后端：sqlite（可带新文件路径）或 mysql（可带连接URL）"""
        db_type = (db_type or "").lower()
        if db_type == "sqlite":
            self._backend = "sqlite"
            self._sqlite_executor = SQLExecutor(path_or_url or DEMO_DB_PATH)
        elif db_type == "mysql":
            from db.mysql_executor import MySQLExecutor
            if self._mysql_executor is None:
                self._mysql_executor = MySQLExecutor()
            if path_or_url:
                self._mysql_executor.set_connection_url(path_or_url)
            self._backend = "mysql"
        else:
            raise ValueError(f"不支持的数据库类型: {db_type}")

    def execute(self, sql: str, params: tuple = None) -> dict:
        if self._backend == "mysql":
            return self._mysql_executor.execute(sql, params)
        return self._sqlite_executor.execute(sql, params)


# 全局单例（代理，默认 SQLite；DB_TYPE=mysql 或运行时切换后走 MySQL）
executor = ExecutorProxy()
