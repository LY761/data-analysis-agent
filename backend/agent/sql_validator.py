"""
SQL校验器 — 五道闸门确保LLM生成的SQL安全可执行

v4.0: 新增第4道闸门"Schema存在性校验"——用sqlglot解析SQL，
     拦截引用了数据库里不存在表名/字段名的幻觉SQL（最常见的幻觉来源）。
"""
import re
import time
import sqlite3
import sqlglot
from sqlglot.errors import ErrorLevel
from config import DEMO_DB_PATH


class SQLValidator:
    """SQL五道闸门校验：语法 → 注入 → 只读 → 表权限 → Schema存在性"""

    # 真实Schema缓存（表→字段集合），短TTL，DB切换后最多30s内更新
    _schema_cache: dict = {}
    _schema_cache_ts: float = 0

    # 危险操作和注入特征正则库
    DANGEROUS_PATTERNS = [
        # 写操作拦截
        (r'\bDROP\b', 'DROP操作'),
        (r'\bDELETE\b', 'DELETE操作'),
        (r'\bUPDATE\b', 'UPDATE操作'),
        (r'\bINSERT\b', 'INSERT操作'),
        (r'\bALTER\b', 'ALTER操作'),
        (r'\bCREATE\b', 'CREATE操作'),
        (r'\bTRUNCATE\b', 'TRUNCATE操作'),
        (r'\bGRANT\b', 'GRANT操作'),
        (r'\bREVOKE\b', 'REVOKE操作'),
        (r'\bEXEC\b', 'EXEC操作'),
        (r'\bEXECUTE\b', 'EXECUTE操作'),
        # 注入特征拦截
        (r"'\\s*OR\\s+'1'\\s*=\\s*'1", 'SQL注入特征: OR 1=1'),
        (r"'\\s*OR\\s+1\\s*=\\s*1", 'SQL注入特征: OR 1=1'),
        (r'\bUNION\s+SELECT\b', 'UNION SELECT注入'),
        (r'--\s*$', 'SQL注释注入'),
        (r'/\*.*\*/', 'SQL注释注入'),
        (r"';", '语句终止注入'),
    ]

    def validate(self, sql: str) -> dict:
        """
        三道闸门顺序校验SQL。
        返回: {"valid": True/False, "error": str或None, "stage": str}
        三道闸门的设计思路：先做最便宜的检查（语法），再做较贵的检查（正则），
        任何一道不通过立即返回，不浪费后续算力。
        """

        # 第一道闸门：语法校验（用SQLGlot解析AST）
        syntax_result = self._check_syntax(sql)
        if not syntax_result["valid"]:
            return {"valid": False, "error": syntax_result["error"], "stage": "syntax"}

        # 第二道闸门：注入和危险操作检测（14条正则）
        injection_result = self._check_injection(sql)
        if not injection_result["valid"]:
            return {"valid": False, "error": injection_result["error"], "stage": "injection"}

        # 第三道闸门：权限校验（只允许SELECT/WITH）
        perm_result = self._check_permissions(sql)
        if not perm_result["valid"]:
            return {"valid": False, "error": perm_result["error"], "stage": "permission"}

        # 第四道闸门：按当前认证上下文校验表级访问权限。
        access_result = self._check_table_access(sql)
        if not access_result["valid"]:
            return {"valid": False, "error": access_result["error"], "stage": "table_permission"}

        # 第五道闸门：Schema存在性校验（拦截幻觉表名/字段名）
        schema_result = self._check_schema_identifiers(sql)
        if not schema_result["valid"]:
            return {"valid": False, "error": schema_result["error"], "stage": "schema"}

        return {"valid": True, "error": None, "stage": "all_passed"}

    def _check_syntax(self, sql: str) -> dict:
        """第一道闸门：SQLGlot语法解析，不合法的SQL直接拒绝"""
        try:
            parsed = sqlglot.parse(sql, error_level=ErrorLevel.RAISE)
            if not parsed:
                return {"valid": False, "error": "无法解析SQL语句，请检查语法。"}
            return {"valid": True, "error": None}
        except Exception as e:
            return {"valid": False, "error": f"SQL语法错误：{str(e)}"}

    def _check_injection(self, sql: str) -> dict:
        """第二道闸门：正则匹配检测危险操作和SQL注入特征"""
        sql_upper = sql.upper()
        for pattern, description in self.DANGEROUS_PATTERNS:
            if re.search(pattern, sql_upper, re.IGNORECASE):
                return {"valid": False, "error": f"检测到危险操作或注入特征：{description}"}
        return {"valid": True, "error": None}

    def _check_permissions(self, sql: str) -> dict:
        """第三道闸门：只允许只读查询（SELECT或WITH开头）"""
        sql_stripped = sql.strip().upper()
        # 去掉开头的注释，防止用注释绕过权限检查
        sql_stripped = re.sub(r'^/\*.*?\*/', '', sql_stripped).strip()
        sql_stripped = re.sub(r'^--.*?\n', '', sql_stripped).strip()

        if sql_stripped.startswith("SELECT") or sql_stripped.startswith("WITH"):
            return {"valid": True, "error": None}
        return {"valid": False, "error": "权限不足：仅允许SELECT查询操作。"}

    def _check_table_access(self, sql: str) -> dict:
        """校验 SQL 引用表是否包含在当前用户权限范围内。"""
        from middleware.auth_middleware import current_user_ctx

        user = current_user_ctx.get()
        if not user or user.get("role") == "admin":
            return {"valid": True, "error": None}

        allowed_tables = {
            str(table).lower()
            for table in user.get("permissions", {}).get("tables", [])
        }
        if "*" in allowed_tables:
            return {"valid": True, "error": None}

        try:
            ast = sqlglot.parse_one(sql)
            referenced_tables = {
                table.name.lower()
                for table in ast.find_all(sqlglot.exp.Table)
                if table.name
            }
            cte_names = {
                cte.alias_or_name.lower()
                for cte in ast.find_all(sqlglot.exp.CTE)
                if cte.alias_or_name
            }
        except Exception:
            return {"valid": False, "error": "无法确认查询表权限，已拒绝执行。"}

        denied_tables = referenced_tables - cte_names - allowed_tables
        if denied_tables:
            return {
                "valid": False,
                "error": f"无权访问数据表：{', '.join(sorted(denied_tables))}",
            }
        return {"valid": True, "error": None}


    def _get_db_schema(self, max_age: int = 30) -> dict:
        """读取真实数据库Schema（表→字段名集合），短TTL缓存。异常时返回空dict（本闸门自动放行）。"""
        now = time.time()
        if self._schema_cache and now - self._schema_cache_ts < max_age:
            return self._schema_cache
        try:
            conn = sqlite3.connect(DEMO_DB_PATH)
            schema = {}
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'"
            ):
                cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')}
                schema[name] = cols
            conn.close()
            self._schema_cache = schema
            self._schema_cache_ts = now
            return schema
        except Exception:
            return {}

    def _check_schema_identifiers(self, sql: str) -> dict:
        """
        第四道闸门：校验SQL引用的表/字段是否真实存在，拦截幻觉标识符。

        保守策略（避免误伤合法SQL）：
          · 解析失败 → 放行（交给前面的语法闸门处理）
          · 表名必须存在于真实表集合（排除CTE别名）
          · 字段名必须存在于「真实字段集合 ∪ 查询中定义的别名」
            别名放行是因为 ORDER BY total_sales / GROUP BY alias 等用法，
            别名虽不在Schema里但完全合法。
        无法读取Schema（如异常）→ 放行，保证功能不因校验器降级。
        """
        try:
            ast = sqlglot.parse_one(sql)
        except Exception:
            return {"valid": True, "error": None}
        if ast is None:
            return {"valid": True, "error": None}

        schema = self._get_db_schema()
        if not schema:
            return {"valid": True, "error": None}

        # ── 表名校验 ──
        try:
            tables = {t.name for t in ast.find_all(sqlglot.exp.Table)}
            ctes = {c.alias_or_name for c in ast.find_all(sqlglot.exp.CTE)}
        except Exception:
            tables, ctes = set(), set()

        real_tables = set(schema.keys())
        unknown_tables = tables - real_tables - ctes
        if unknown_tables:
            return {
                "valid": False,
                "error": f"引用了数据库不存在的表：{', '.join(sorted(unknown_tables))}。请只用全部表/字段清单里的名字。",
            }

        # ── 字段名校验 ──
        try:
            referenced_cols = {
                c.name for c in ast.find_all(sqlglot.exp.Column)
                if c.name and c.name != "*"
            }
            aliases = {
                a.alias_or_name for a in ast.find_all(sqlglot.exp.Alias)
                if a.alias_or_name
            }
        except Exception:
            referenced_cols, aliases = set(), set()

        if referenced_cols:
            valid_cols = set().union(*schema.values()) if schema else set()
            valid_cols |= aliases
            unknown_cols = referenced_cols - valid_cols
            if unknown_cols:
                shown = ", ".join(sorted(unknown_cols))
                return {
                    "valid": False,
                    "error": f"引用了数据库不存在的字段：{shown[:200]}。请用字段清单里真实存在的字段名。",
                }

        return {"valid": True, "error": None}


# 全局单例
sql_validator = SQLValidator()
