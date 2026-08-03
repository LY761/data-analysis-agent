"""
长记忆对话 — SQLite持久化，重启不丢失，支持历史搜索

功能:
  1. 增量SQL改写 — "按地区分组" → "再按月份排序"
  2. 历史查询模板复用 — 相同结构不同参数零Token
  3. 跨会话记忆 — 重启后仍能回顾之前问过的问题
  4. 相似问题推荐 — "你之前问过类似的问题，要重新查吗？"
"""
import re
import hashlib
import time
import json
import sqlite3
import logging
from typing import Optional
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# 内存缓存：session_id → ConversationMemory
_sessions: dict[str, "ConversationMemory"] = {}
_current_session: ContextVar[str] = ContextVar("session_id", default="")

# SQLite持久化
from config import DEMO_DB_PATH

_table_created = False


def _ensure_table():
    global _table_created
    if _table_created:
        return
    conn = sqlite3.connect(DEMO_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            question TEXT NOT NULL,
            sql TEXT,
            result_summary TEXT,
            template_hash TEXT,
            topic_id TEXT,
            created_at REAL NOT NULL,
            UNIQUE(session_id, turn_index)
        )
    """)
    # 迁移：老库缺 topic_id 列 → 补上，不丢历史
    cols = {r[1] for r in conn.execute("PRAGMA table_info(conversation_history)")}
    if "topic_id" not in cols:
        conn.execute("ALTER TABLE conversation_history ADD COLUMN topic_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_session ON conversation_history(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_hash ON conversation_history(template_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_time ON conversation_history(created_at)")
    conn.commit()
    conn.close()
    _table_created = True


class ConversationMemory:
    """会话记忆：内存操作 + SQLite持久化"""

    def __init__(self, session_id: str, max_turns: int = 20):
        self.session_id = session_id
        self.max_turns = max_turns
        self.turns: list[dict] = []
        self.templates: dict[str, dict] = {}
        self.created_at = time.time()
        self._load_history()

    @property
    def last_sql(self) -> Optional[str]:
        return self.turns[-1].get("sql") if self.turns else None

    @property
    def last_question(self) -> Optional[str]:
        return self.turns[-1].get("question") if self.turns else None

    # ═══════════════════════════════════════════
    # 持久化加载
    # ═══════════════════════════════════════════

    def _load_history(self):
        """从SQLite加载历史对话"""
        _ensure_table()
        try:
            conn = sqlite3.connect(DEMO_DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM conversation_history WHERE session_id=? ORDER BY turn_index DESC LIMIT ?",
                (self.session_id, self.max_turns)
            ).fetchall()
            conn.close()

            for row in reversed(rows):
                turn = {
                    "question": row["question"],
                    "sql": row["sql"] or "",
                    "result_summary": json.loads(row["result_summary"]) if row["result_summary"] else {},
                    "topic_id": row["topic_id"] or "",
                    "timestamp": row["created_at"],
                }
                self.turns.append(turn)

                # 重建模板索引
                if row["template_hash"]:
                    th = row["template_hash"]
                    if th in self.templates:
                        self.templates[th]["count"] += 1
                    else:
                        self.templates[th] = {"sql": row["sql"] or "", "count": 1, "last_used": row["created_at"]}

            if self.turns:
                logger.info(f"[Memory] 加载历史: {len(self.turns)}轮对话 (session={self.session_id[:8]}...)")
        except Exception as e:
            logger.warning(f"[Memory] 加载历史失败: {e}")

    def _save_turn(self, turn_index: int, question: str, sql: str, summary: dict,
                   template_hash: str, topic_id: str = ""):
        """持久化一轮对话到SQLite"""
        _ensure_table()
        try:
            conn = sqlite3.connect(DEMO_DB_PATH)
            conn.execute(
                """INSERT OR REPLACE INTO conversation_history
                   (session_id, turn_index, question, sql, result_summary, template_hash, topic_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.session_id, turn_index, question[:500], sql,
                 json.dumps(summary, ensure_ascii=False) if summary else None,
                 template_hash, topic_id, time.time())
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"[Memory] 保存失败: {e}")

    # ═══════════════════════════════════════════
    # 对话操作
    # ═══════════════════════════════════════════

    def add_turn(self, question: str, sql: str, result_summary: dict = None, topic_id: str = ""):
        turn = {
            "question": question,
            "sql": sql,
            "result_summary": result_summary or {},
            "topic_id": topic_id or "",
            "timestamp": time.time(),
        }
        self.turns.append(turn)
        if len(self.turns) > self.max_turns:
            self.turns = self.turns[-self.max_turns:]

        # 模板哈希
        th = self._hash_template(sql) if sql else ""
        if th and th in self.templates:
            self.templates[th]["count"] += 1
            self.templates[th]["last_used"] = time.time()
        elif th:
            self.templates[th] = {"sql": sql, "count": 1, "last_used": time.time()}

        # 持久化
        turn_index = len(self.turns) - 1 if self.turns else 0
        self._save_turn(turn_index, question, sql, result_summary, th, topic_id)

    def build_context(self, max_turns: int = 3) -> str:
        """构建对话上下文 Prompt"""
        if not self.turns:
            return ""
        recent = self.turns[-max_turns:]
        parts = ["## 对话上下文（你之前问过）"]
        for i, turn in enumerate(recent):
            idx = len(self.turns) - len(recent) + i + 1
            parts.append(f"Q{idx}: {turn['question']}")
            if turn.get("sql"):
                parts.append(f"SQL{idx}: {turn['sql'][:100]}")
        return "\n".join(parts)

    # ═══════════════════════════════════════════
    # 话题自适应上下文（同话题稳定、换话题发散）
    # ═══════════════════════════════════════════

    _TOPIC_OVERLAP_THRESHOLD = 0.3

    @staticmethod
    def _bigrams(text: str) -> set:
        """中文 bigram 切词（去单字，避免'按/的'等噪声匹配）"""
        return {text[i:i + 2] for i in range(max(0, len(text) - 1))}

    @staticmethod
    def _keyword_overlap(a: str, b: str) -> float:
        """两段文本的 bigram 重叠比例（交集 / 较短者长度）"""
        ta, tb = ConversationMemory._bigrams(a), ConversationMemory._bigrams(b)
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / min(len(ta), len(tb))

    @staticmethod
    def _sql_table_names(sql: str) -> set:
        """粗略提取 SQL 引用的表名（FROM/JOIN 后首个标识符）"""
        if not sql:
            return set()
        return set(re.findall(r'\b(?:from|join)\s+([A-Za-z_]\w*)', sql, re.IGNORECASE))

    def get_last_topic_id(self) -> str:
        """返回最近一轮的 LLM 语义话题标签；无则空串"""
        return (self.turns[-1].get("topic_id") or "") if self.turns else ""

    def get_topic_context(self, current_question: str, topic_id: str = "", max_turns: int = 10) -> tuple:
        """从最近一轮往回回溯同一话题的轮次。

        返回 (turns, anchor)：
          turns —— 话题内轮次（按时间正序）
          anchor —— 该话题起始（最早）一轮的完整问题，用于稳定检索

        优先用 LLM 语义 topic_id 精确分组（同话题同标签，正解）；
        无 topic_id 时退回启发式（规则快路径/旧轮次）：
          · 当前轮→最近一轮：detect_incremental 命中（"再按月份"），或关键词重叠 ≥ 阈值
          · 相邻已存轮之间：关键词重叠 ≥ 阈值，或两者 SQL 引用了同一批表
        不满足 → 话题已切换，返回空段（anchor=当前问题），让检索自然发散。
        """
        # ── 正解：LLM 语义 topic_id 精确分组 ──
        if topic_id:
            matches = [t for t in self.turns if (t.get("topic_id") or "") == topic_id]
            if matches:
                anchor = (matches[0].get("question") or "").strip() or current_question
                return matches[-max_turns:], anchor
            return [], current_question
        # ── 兜底：启发式 ──
        if not self.turns:
            return [], current_question
        segment = []
        prev_q = current_question
        prev_sql = ""
        for turn in reversed(self.turns):
            q = (turn.get("question") or "").strip()
            sql = turn.get("sql") or ""
            if not q:
                continue
            if not segment:
                if not (self.detect_incremental(current_question)
                        or self._keyword_overlap(current_question, q) >= self._TOPIC_OVERLAP_THRESHOLD):
                    return [], current_question
            else:
                if not (self._keyword_overlap(prev_q, q) >= self._TOPIC_OVERLAP_THRESHOLD
                        or bool(self._sql_table_names(prev_sql) & self._sql_table_names(sql))):
                    break
            segment.append(turn)
            prev_q = q
            prev_sql = sql
            if len(segment) >= max_turns:
                break
        if not segment:
            return [], current_question
        anchor = (segment[-1].get("question") or "").strip() or current_question
        segment.reverse()
        return segment, anchor

    def build_topic_context(self, current_question: str, topic_id: str = "", max_turns: int = 10) -> tuple:
        """构建话题上下文 Prompt 文本 + 话题锚，供工作流使用。

        返回 (text, anchor)；text 为空表示全新话题、无历史上下文可复用。"""
        turns, anchor = self.get_topic_context(current_question, topic_id, max_turns)
        if not turns:
            return "", anchor
        parts = ["## 对话上下文（同一话题，你之前问过）"]
        for i, turn in enumerate(turns):
            parts.append(f"Q{i + 1}: {turn['question']}")
            if turn.get("sql"):
                parts.append(f"SQL{i + 1}: {turn['sql'][:100]}")
        return "\n".join(parts), anchor

    def detect_incremental(self, question: str) -> bool:
        """检测是否追问"""
        if not self.turns:
            return False
        return bool(re.search(r'再|接着|然后|按|改成|换成|加上|去掉|只要|在上面', question))

    def find_similar_question(self, question: str) -> Optional[dict]:
        """搜索历史中相似的问题"""
        th = self._hash_question_structure(question)
        if th in self.templates:
            return self.templates[th]
        # 模糊匹配
        for turn in reversed(self.turns):
            if turn["question"] and (question[:10] in turn["question"] or turn["question"][:10] in question):
                return {"sql": turn["sql"], "count": 1, "fuzzy": True}
        return None

    def search_history(self, keyword: str) -> list:
        """按关键词搜索历史对话"""
        results = []
        for i, turn in enumerate(self.turns):
            if keyword in turn.get("question", ""):
                results.append({"index": i + 1, "question": turn["question"], "sql": turn.get("sql", "")[:80], "timestamp": turn.get("timestamp", 0)})
        return results[-10:]  # 最近10条

    def get_stats(self) -> dict:
        return {
            "session_id": self.session_id,
            "turn_count": len(self.turns),
            "template_count": len(self.templates),
            "age_seconds": time.time() - self.created_at,
        }

    # ═══════════════════════════════════════════
    # 哈希
    # ═══════════════════════════════════════════

    @staticmethod
    def _hash_template(sql: str) -> str:
        if not sql:
            return ""
        n = re.sub(r"'[^']*'", "'?'", sql)
        n = re.sub(r"\b\d+\b", "N", n)
        return hashlib.md5(n.encode()).hexdigest()[:12]

    @staticmethod
    def _hash_question_structure(question: str) -> str:
        c = re.sub(r"\b\d+\b", "N", question)
        c = re.sub(r"\d{4}[-/]\d{2}[-/]\d{2}", "DATE", c)
        return hashlib.md5(c.encode()).hexdigest()[:12]


# ═══════════════════════════════════════════
# 全局会话管理
# ═══════════════════════════════════════════

def get_session(session_id: str = "default", max_turns: int = 20) -> ConversationMemory:
    if session_id not in _sessions:
        _sessions[session_id] = ConversationMemory(session_id, max_turns)
    return _sessions[session_id]


def set_current_session(session_id: str):
    _current_session.set(session_id)


def get_current_session() -> Optional[ConversationMemory]:
    sid = _current_session.get()
    return _sessions.get(sid) if sid else None


def search_all_history(keyword: str) -> list:
    """全局搜索所有会话的历史记录"""
    _ensure_table()
    try:
        conn = sqlite3.connect(DEMO_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT DISTINCT session_id, question, sql, created_at FROM conversation_history "
            "WHERE question LIKE ? ORDER BY created_at DESC LIMIT 20",
            (f"%{keyword}%",)
        ).fetchall()
        conn.close()
        return [{"session": r["session_id"][:8], "question": r["question"],
                 "sql": (r["sql"] or "")[:80], "time": r["created_at"]} for r in rows]
    except Exception:
        return []


def get_history_summary(session_id: str = "default") -> dict:
    """获取某个会话的历史摘要（供前端展示）"""
    mem = get_session(session_id)
    return {
        "turn_count": len(mem.turns),
        "recent_questions": [t["question"][:60] for t in mem.turns[-10:]],
        "templates": len(mem.templates),
        "stats": mem.get_stats(),
    }
