"""
Schema增量更新监听器 — 数据库结构变了自动重新索引，不需要重启

监听方式:
  - SQLite: 定时轮询 sqlite_master 表（轻量，30秒一次）
  - MySQL: 可扩展为监听 information_schema 变更

使用方式:
  在 main.py 启动后调用 schema_watcher.start()
"""
import time
import hashlib
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class SchemaWatcher:
    """
    Schema变更监听器。

    工作原理:
      1. 启动时记录当前Schema指纹
      2. 每隔30秒重新计算指纹
      3. 指纹变了 → 发现新增/删除的表 → 触发重新索引
      4. 重新索引只处理增量的新表，不重建已有的
    """

    def __init__(self, interval_seconds: int = 30):
        self.interval = interval_seconds
        self._fingerprint: str = ""
        self._timer: Optional[threading.Timer] = None
        self._running = False

    def start(self):
        """启动监听"""
        from db.executor import executor

        # 记录当前Schema指纹
        try:
            self._fingerprint = self._compute_fingerprint(executor)
            logger.info(f"[SchemaWatcher] 启动监听，当前Schema指纹: {self._fingerprint}")
        except Exception as e:
            logger.warning(f"[SchemaWatcher] 启动指纹计算失败: {e}")
            self._fingerprint = ""

        self._running = True
        self._schedule_next()

    def stop(self):
        """停止监听"""
        self._running = False
        if self._timer:
            self._timer.cancel()
        logger.info("[SchemaWatcher] 已停止")

    def check_now(self) -> Optional[dict]:
        """
        手动立即检查一次Schema变化。
        返回: None（无变化）或变化详情dict
        """
        from db.executor import executor

        try:
            new_fp = self._compute_fingerprint(executor)
            if new_fp != self._fingerprint:
                changes = self._detect_changes(executor)
                logger.info(f"[SchemaWatcher] 检测到Schema变更: {changes}")
                self._reindex(executor, changes)
                self._fingerprint = new_fp
                return changes
        except Exception as e:
            logger.error(f"[SchemaWatcher] 检查失败: {e}")

        return None

    # ── 内部实现 ──────────────────────────────────────────────

    @staticmethod
    def _compute_fingerprint(executor) -> str:
        """计算当前Schema的MD5指纹"""
        result = executor.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
        )
        if result.get("data"):
            raw = str(result["data"]).encode()
            return hashlib.md5(raw).hexdigest()
        return "empty"

    def _detect_changes(self, executor) -> dict:
        """检测具体变更：哪些表被新增/删除了"""
        result = executor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        current_tables = {row["name"] for row in result.get("data", [])}

        # 需要对比上次的表列表（简化：这次只检测是否有新表）
        return {
            "current_tables": sorted(current_tables),
            "table_count": len(current_tables),
        }

    def _reindex(self, executor, changes: dict):
        """触发Schema重新索引（增量更新）"""
        try:
            from db.init_db import get_schema_descriptions
            from agent.schema_retriever import schema_retriever

            # 重新索引
            schemas = get_schema_descriptions()

            # 对schema_retriever做增量：当前只支持全量重建
            # 生产环境应该只索引新增的表
            schema_retriever.index_schemas(schemas)

            logger.info(f"[SchemaWatcher] Schema重新索引完成 — "
                       f"{changes.get('table_count', 0)}张表")
        except Exception as e:
            logger.error(f"[SchemaWatcher] 重新索引失败: {e}")

    def _schedule_next(self):
        """定时调度下一次检查"""
        if not self._running:
            return

        self._timer = threading.Timer(self.interval, self._on_timer)
        self._timer.daemon = True
        self._timer.start()

    def _on_timer(self):
        """定时器触发"""
        if not self._running:
            return
        self.check_now()
        self._schedule_next()


# 全局单例
schema_watcher = SchemaWatcher(interval_seconds=30)
