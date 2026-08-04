"""
检索质量监控 — 轻量级采集 -> 准确率/召回率/命中率看板

埋点织入 workflow 各节点后自动采集。指标定义：
  · keyword_hit:  关键词检索是否命中（true=没走向量兜底）
  · precision@k:  检索到的 top-k 表里，有多少被 SQL 实际引用
  · recall_gap:   SQL 引用了但检索没返回的表（召回漏损）
  · success:      查询是否产出有效结果（有数据 + 无错误）
  · latency_ms:   各阶段耗时

存储: demo_sales.db 内的 retrieval_log 表，零额外依赖。
"""
import re
import time
import json
import sqlite3
import hashlib
import logging
from typing import Optional
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# ── 持久化 ──
_table_created = False
_DB_PATH: str = ""

# 当前请求的 trace_id（被 workflow 各节点共用，写入同一条日志）
_current_log_id: ContextVar[str] = ContextVar("retrieval_log_id", default="")


def _ensure_table(db_path: str = "demo_sales.db"):
    global _table_created
    if _table_created:
        return
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS retrieval_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_id TEXT NOT NULL,
            question TEXT,
            keyword_hit INTEGER DEFAULT 0,           -- 0=向量兜底 1=关键词命中
            retrieved_tables TEXT,                    -- JSON: [{table, score}]
            retrieved_columns TEXT,                   -- JSON: [{table, column, score}]
            generated_sql TEXT,
            sql_chars INTEGER DEFAULT 0,
            intent TEXT,
            intent_method TEXT,
            validation_stage TEXT,
            row_count INTEGER DEFAULT 0,
            success INTEGER DEFAULT 0,                -- 0=错误或空 1=有数据
            error TEXT,
            token_understand INTEGER DEFAULT 0,       -- 意图理解LLM的Token
            token_generate INTEGER DEFAULT 0,         -- SQL生成LLM的Token
            token_fix INTEGER DEFAULT 0,              -- SQL修正LLM的Token（重试）
            token_answer INTEGER DEFAULT 0,           -- NL回答LLM的Token（流式回答）
            token_total INTEGER DEFAULT 0,            -- 总Token消耗
            latency_understand_ms REAL DEFAULT 0,
            latency_retrieve_ms REAL DEFAULT 0,
            latency_generate_ms REAL DEFAULT 0,
            latency_execute_ms REAL DEFAULT 0,
            total_latency_ms REAL DEFAULT 0,
            created_at REAL NOT NULL
        )
    """)
    # 迁移兼容：旧表可能没有 token 列（首次部署不会有旧表，安全执行）
    for col in ["token_understand", "token_generate", "token_fix", "token_answer", "token_total"]:
        try:
            conn.execute(f"ALTER TABLE retrieval_log ADD COLUMN {col} INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.execute("CREATE INDEX IF NOT EXISTS idx_retrieval_hit ON retrieval_log(keyword_hit)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_retrieval_time ON retrieval_log(created_at)")
    conn.commit()
    conn.close()
    _table_created = True


# ── 采集器 ──

class RetrievalSpan:
    """
    一次查询的检索质量 span。链路各节点逐步填充字段，
    在 execute_sql 节点完成后写入 DB。
    """

    def __init__(self, question: str, db_path: str = "demo_sales.db"):
        global _DB_PATH
        _DB_PATH = db_path
        _ensure_table(db_path)
        self.log_id = hashlib.md5(f"{question}{time.time()}".encode()).hexdigest()[:12]
        _current_log_id.set(self.log_id)
        self.question = question[:500]
        self.t_start = time.time()

        # 各阶段填充
        self.keyword_hit: Optional[bool] = None
        self.retrieved_tables: list = []
        self.retrieved_columns: list = []
        self.generated_sql: str = ""
        self.intent: str = ""
        self.intent_method: str = ""
        self.validation_stage: str = ""
        self.row_count: int = 0
        self.error: str = ""
        # Token 消耗（输入+输出+总计，按节点拆分）
        self.token_understand: int = 0
        self.token_generate: int = 0
        self.token_fix: int = 0
        self.token_answer: int = 0
        self.latency_understand: float = 0
        self.latency_retrieve: float = 0
        self.latency_generate: float = 0
        self.latency_execute: float = 0

    def set_keyword_hit(self, hit: bool):
        self.keyword_hit = hit

    def set_retrieved(self, tables: list, columns: list):
        self.retrieved_tables = tables
        self.retrieved_columns = columns

    def set_sql(self, sql: str, intent: str = "", intent_method: str = ""):
        self.generated_sql = sql
        self.intent = intent
        self.intent_method = intent_method

    def set_validation(self, stage: str):
        self.validation_stage = stage

    def set_result(self, row_count: int, error: str = ""):
        self.row_count = row_count
        self.error = error

    def add_tokens(self, understand: int = 0, generate: int = 0, fix: int = 0, answer: int = 0):
        self.token_understand += understand
        self.token_generate += generate
        self.token_fix += fix
        self.token_answer += answer

    @property
    def total_tokens(self) -> int:
        return self.token_understand + self.token_generate + self.token_fix + self.token_answer

    def set_latency(self, understand: float = 0, retrieve: float = 0,
                    generate: float = 0, execute: float = 0):
        self.latency_understand = understand
        self.latency_retrieve = retrieve
        self.latency_generate = generate
        self.latency_execute = execute

    @staticmethod
    def _extract_table_refs(sql: str) -> set[str]:
        """从 SQL 里提取被引用的表名（表别名映射回原名尽量）"""
        if not sql:
            return set()
        refs = set()
        # 匹配 FROM/JOIN 后面的表名
        for m in re.finditer(r'(?:FROM|JOIN)\s+(\w+)', sql, re.IGNORECASE):
            refs.add(m.group(1).lower())
        return refs

    def compute_precision(self) -> dict:
        """计算 Precision@k：检索表里有多少被 SQL 用到"""
        tables = [t.get("table", "") for t in self.retrieved_tables]
        sql_refs = self._extract_table_refs(self.generated_sql)
        if not tables:
            return {"precision": None, "hits": [], "misses": []}
        hits = [t for t in tables if t.lower() in sql_refs]
        misses = [t for t in tables if t.lower() not in sql_refs]
        p = len(hits) / len(tables) if tables else None
        return {"precision": round(p, 3) if p is not None else None,
                "k": len(tables), "hits": hits, "misses": misses}

    def compute_recall_gap(self) -> dict:
        """计算 Recall 漏损：SQL 用了但检索没返回的表"""
        tables = {t.get("table", "").lower() for t in self.retrieved_tables}
        sql_refs = self._extract_table_refs(self.generated_sql)
        missed = [t for t in sql_refs if t and t not in tables]
        return {"recall_gap": len(missed), "missed_tables": missed}

    def flush(self, _db_path: str = None):
        """写入 SQLite（幂等：execute_sql 节点与外层兜底各调一次只落库一条）。"""
        if getattr(self, "_flushed", False):
            return
        self._flushed = True
        db_path = _db_path or _DB_PATH or "demo_sales.db"
        total = (time.time() - self.t_start) * 1000
        success = 1 if (self.row_count > 0 and not self.error) else 0

        # Precision / Recall
        prec = self.compute_precision()
        recall = self.compute_recall_gap()

        conn = sqlite3.connect(db_path)
        conn.execute("""
            INSERT INTO retrieval_log
            (log_id, question, keyword_hit, retrieved_tables, retrieved_columns,
             generated_sql, sql_chars, intent, intent_method, validation_stage,
             row_count, success, error,
             token_understand, token_generate, token_fix, token_answer, token_total,
             latency_understand_ms, latency_retrieve_ms,
             latency_generate_ms, latency_execute_ms, total_latency_ms,
             created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            self.log_id, self.question,
            1 if self.keyword_hit else 0,
            json.dumps(self.retrieved_tables, ensure_ascii=False),
            json.dumps(self.retrieved_columns, ensure_ascii=False),
            self.generated_sql, len(self.generated_sql or ""),
            self.intent, self.intent_method, self.validation_stage,
            self.row_count, success, self.error,
            self.token_understand, self.token_generate,
            self.token_fix, self.token_answer, self.total_tokens,
            self.latency_understand, self.latency_retrieve,
            self.latency_generate, self.latency_execute, total,
            time.time(),
        ))
        conn.commit()
        conn.close()
        logger.info(
            f"[RetrievalMetrics] {self.log_id} "
            f"keyword_hit={self.keyword_hit} "
            f"precision@{prec.get('k',0)}={prec.get('precision')} "
            f"recall_gap={recall['recall_gap']} "
            f"success={success} total={total:.0f}ms"
        )


def get_current_span() -> Optional[RetrievalSpan]:
    return None  # Span实例由调用方持有，这里只留接口


# ── 统计看板 ──

def get_metrics(limit: int = 100) -> dict:
    """计算累积检索质量指标"""
    db_path = _DB_PATH or "demo_sales.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 基础计数
    total = conn.execute("SELECT COUNT(*) c FROM retrieval_log").fetchone()["c"]
    kw_hit = conn.execute("SELECT COUNT(*) c FROM retrieval_log WHERE keyword_hit=1").fetchone()["c"]
    successes = conn.execute("SELECT COUNT(*) c FROM retrieval_log WHERE success=1").fetchone()["c"]

    # 平均延迟
    avg_lat = conn.execute(
        "SELECT AVG(total_latency_ms) a, AVG(latency_retrieve_ms) b, AVG(latency_generate_ms) c FROM retrieval_log"
    ).fetchone()

    # Token 消耗统计
    tk = conn.execute(
        "SELECT AVG(token_understand) u, AVG(token_generate) g, AVG(token_fix) f, "
        "AVG(token_answer) a, AVG(token_total) t, SUM(token_total) s "
        "FROM retrieval_log"
    ).fetchone()

    # Precision@k 估算（在 Python 里算：检索表出现在 SQL 里的占比）
    rows = conn.execute(
        "SELECT retrieved_tables, generated_sql FROM retrieval_log ORDER BY created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    precisions = []
    recall_gaps = 0
    for r in rows:
        try:
            tables = [t.get("table", "") for t in (json.loads(r["retrieved_tables"]) or [])]
        except Exception:
            tables = []
        sql = r["generated_sql"] or ""
        sql_lower = sql.lower()
        if tables and sql:
            hits = [t for t in tables if t.lower() in sql_lower]
            precisions.append(len(hits) / len(tables))
            # recall gap: SQL 里的表不在检索结果里
            for m in re.finditer(r'(?:FROM|JOIN)\s+(\w+)', sql, re.IGNORECASE):
                if m.group(1).lower() not in {t.lower() for t in tables}:
                    recall_gaps += 1

    # 近期记录
    recent = conn.execute(
        "SELECT log_id, question, keyword_hit, row_count, success, "
        "token_understand, token_generate, token_total, total_latency_ms, created_at "
        "FROM retrieval_log ORDER BY created_at DESC LIMIT 10"
    ).fetchall()

    conn.close()

    return {
        "summary": {
            "total_queries": total,
            "keyword_hit_rate": round(kw_hit / total * 100, 1) if total else 0,
            "vector_fallback_rate": round((total - kw_hit) / total * 100, 1) if total else 0,
            "success_rate": round(successes / total * 100, 1) if total else 0,
            "avg_total_ms": round(avg_lat["a"], 0) if avg_lat["a"] else 0,
            "avg_retrieve_ms": round(avg_lat["b"], 0) if avg_lat["b"] else 0,
            "avg_generate_ms": round(avg_lat["c"], 0) if avg_lat["c"] else 0,
            "avg_token_understand": round(tk["u"], 0) if tk["u"] else 0,
            "avg_token_generate": round(tk["g"], 0) if tk["g"] else 0,
            "avg_token_answer": round(tk["a"], 0) if tk["a"] else 0,
            "avg_token_total": round(tk["t"], 0) if tk["t"] else 0,
            "sum_token_total": int(tk["s"] or 0),
            "avg_precision_at_k": round(sum(precisions) / len(precisions), 3) if precisions else None,
            "recall_gap_events": recall_gaps,
            "sample_size": limit,
        },
        "recent": [
            {
                "log_id": r["log_id"],
                "question": (r["question"] or "")[:60],
                "keyword_hit": bool(r["keyword_hit"]),
                "success": bool(r["success"]),
                "total_ms": round(r["total_latency_ms"], 0),
                "tokens": {
                    "understand": r["token_understand"] or 0,
                    "generate": r["token_generate"] or 0,
                    "total": r["token_total"] or 0,
                },
                "at": r["created_at"],
            }
            for r in recent
        ],
    }
