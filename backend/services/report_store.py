"""版本化经营报告 SQLite 仓储。"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from config import STANDARD_DATA_DB_PATH


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReportStore:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or STANDARD_DATA_DB_PATH
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        if self._initialized:
            return
        connection = self._connect()
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS reports (
                report_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                report_type TEXT NOT NULL,
                period_key TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, report_type, period_key)
            );
            CREATE TABLE IF NOT EXISTS report_versions (
                version_id TEXT PRIMARY KEY,
                report_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                status TEXT NOT NULL,
                summary_mode TEXT NOT NULL,
                content_json TEXT NOT NULL,
                snapshot_ids_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(report_id, version),
                FOREIGN KEY(report_id) REFERENCES reports(report_id)
            );
            CREATE INDEX IF NOT EXISTS idx_reports_tenant
                ON reports(tenant_id, report_type, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_report_versions_report
                ON report_versions(report_id, version DESC);
        """)
        connection.commit()
        connection.close()
        self._initialized = True

    def save_report_version(
        self,
        tenant_id: str,
        report_type: str,
        period_key: str,
        title: str,
        content: dict[str, Any],
        snapshot_ids: list[str],
        summary_mode: str,
    ) -> dict[str, Any]:
        self.initialize()
        now = _utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT report_id FROM reports WHERE tenant_id=? AND report_type=? AND period_key=?",
                (tenant_id, report_type, period_key),
            ).fetchone()
            if row is None:
                report_id = uuid.uuid4().hex
                connection.execute("""
                    INSERT INTO reports (
                        report_id, tenant_id, report_type, period_key, title, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (report_id, tenant_id, report_type, period_key, title, now, now))
                version = 1
            else:
                report_id = row["report_id"]
                version = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM report_versions WHERE report_id=?",
                    (report_id,),
                ).fetchone()[0]
                connection.execute(
                    "UPDATE reports SET title=?, updated_at=? WHERE report_id=?",
                    (title, now, report_id),
                )
            version_id = uuid.uuid4().hex
            connection.execute("""
                INSERT INTO report_versions (
                    version_id, report_id, version, status, summary_mode,
                    content_json, snapshot_ids_json, created_at
                ) VALUES (?, ?, ?, 'completed', ?, ?, ?, ?)
            """, (
                version_id,
                report_id,
                version,
                summary_mode,
                json.dumps(content, ensure_ascii=False, default=str),
                json.dumps(snapshot_ids, ensure_ascii=False),
                now,
            ))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_report(report_id, tenant_id, version)

    def list_reports(
        self,
        tenant_id: str,
        report_type: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.initialize()
        connection = self._connect()
        if report_type:
            rows = connection.execute("""
                SELECT r.*, MAX(v.version) AS latest_version
                FROM reports r JOIN report_versions v ON v.report_id=r.report_id
                WHERE r.tenant_id=? AND r.report_type=?
                GROUP BY r.report_id ORDER BY r.updated_at DESC LIMIT ?
            """, (tenant_id, report_type, limit)).fetchall()
        else:
            rows = connection.execute("""
                SELECT r.*, MAX(v.version) AS latest_version
                FROM reports r JOIN report_versions v ON v.report_id=r.report_id
                WHERE r.tenant_id=?
                GROUP BY r.report_id ORDER BY r.updated_at DESC LIMIT ?
            """, (tenant_id, limit)).fetchall()
        connection.close()
        return [dict(row) for row in rows]

    def get_report(
        self,
        report_id: str,
        tenant_id: str,
        version: int | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        connection = self._connect()
        report = connection.execute(
            "SELECT * FROM reports WHERE report_id=? AND tenant_id=?",
            (report_id, tenant_id),
        ).fetchone()
        if report is None:
            connection.close()
            raise KeyError(f"报告不存在: {report_id}")
        if version is None:
            version_row = connection.execute(
                "SELECT * FROM report_versions WHERE report_id=? ORDER BY version DESC LIMIT 1",
                (report_id,),
            ).fetchone()
        else:
            version_row = connection.execute(
                "SELECT * FROM report_versions WHERE report_id=? AND version=?",
                (report_id, version),
            ).fetchone()
        versions = connection.execute(
            "SELECT version, version_id, status, summary_mode, created_at FROM report_versions WHERE report_id=? ORDER BY version DESC",
            (report_id,),
        ).fetchall()
        connection.close()
        if version_row is None:
            raise KeyError(f"报告版本不存在: {report_id} v{version}")
        result = dict(report)
        version_data = dict(version_row)
        version_data["content"] = json.loads(version_data.pop("content_json"))
        version_data["snapshot_ids"] = json.loads(version_data.pop("snapshot_ids_json"))
        result["current_version"] = version_data
        result["versions"] = [dict(item) for item in versions]
        return result


report_store = ReportStore()
