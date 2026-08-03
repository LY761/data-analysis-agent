"""
SQL执行器 — 带安全控制的只读SQL执行引擎
"""
import sqlite3
import time
from config import DEMO_DB_PATH, MAX_RESULT_ROWS, QUERY_TIMEOUT_SEC


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

    def execute(self, sql: str) -> dict:
        """执行只读SQL查询，返回结构化结果"""
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

            # 设置查询超时
            conn.execute(f"PRAGMA query_timeout = {QUERY_TIMEOUT_SEC * 1000}")

            cursor.execute(sql)
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

        except sqlite3.OperationalError as e:
            return {
                "success": False,
                "error": f"SQL语法错误：{str(e)}。请检查表名、字段名是否正确。",
                "data": None,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"执行异常：{str(e)}",
                "data": None,
            }
        finally:
            if conn:
                conn.close()


# 全局单例
executor = SQLExecutor()
