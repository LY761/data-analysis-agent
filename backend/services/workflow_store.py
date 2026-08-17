"""简单工作流、运行记录、告警和任务的 SQLite 仓储。"""

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


class WorkflowStore:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or STANDARD_DATA_DB_PATH
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        if self._initialized:
            return
        connection = self._connect()
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS workflow_definitions (
                workflow_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                version INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                built_in INTEGER NOT NULL,
                triggers_json TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workflow_runs (
                run_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                status TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                input_json TEXT NOT NULL,
                output_json TEXT NOT NULL,
                error TEXT NOT NULL,
                retry_of_run_id TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                UNIQUE(tenant_id, workflow_id, idempotency_key),
                FOREIGN KEY(workflow_id) REFERENCES workflow_definitions(workflow_id)
            );
            CREATE INDEX IF NOT EXISTS idx_workflow_runs_tenant
                ON workflow_runs(tenant_id, workflow_id, started_at DESC);
            CREATE TABLE IF NOT EXISTS workflow_step_runs (
                step_run_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                step_key TEXT NOT NULL,
                step_type TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                status TEXT NOT NULL,
                input_json TEXT NOT NULL,
                output_json TEXT NOT NULL,
                error TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                FOREIGN KEY(run_id) REFERENCES workflow_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_workflow_steps_run
                ON workflow_step_runs(run_id, sequence);
            CREATE TABLE IF NOT EXISTS alert_events (
                alert_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                title TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL,
                dedup_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(tenant_id, workflow_id, dedup_key),
                FOREIGN KEY(run_id) REFERENCES workflow_runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                dedup_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, workflow_id, dedup_key),
                FOREIGN KEY(run_id) REFERENCES workflow_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_tenant_status
                ON tasks(tenant_id, status, created_at DESC);
        """)
        connection.commit()
        connection.close()
        self._initialized = True

    def seed_definitions(self, definitions: tuple[dict[str, Any], ...]) -> None:
        self.initialize()
        now = _utc_now()
        connection = self._connect()
        for definition in definitions:
            connection.execute("""
                INSERT OR IGNORE INTO workflow_definitions (
                    workflow_id, tenant_id, name, description, version, enabled,
                    built_in, triggers_json, steps_json, created_at, updated_at
                ) VALUES (?, '*', ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """, (
                definition["workflow_id"],
                definition["name"],
                definition.get("description", ""),
                int(definition.get("version", 1)),
                int(bool(definition.get("enabled", True))),
                json.dumps(definition.get("triggers", []), ensure_ascii=False),
                json.dumps(definition.get("steps", []), ensure_ascii=False),
                now,
                now,
            ))
        connection.commit()
        connection.close()

    def create_definition(self, tenant_id: str, definition: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        workflow_id = definition.get("workflow_id") or uuid.uuid4().hex
        now = _utc_now()
        connection = self._connect()
        try:
            connection.execute("""
                INSERT INTO workflow_definitions (
                    workflow_id, tenant_id, name, description, version, enabled,
                    built_in, triggers_json, steps_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, 0, ?, ?, ?, ?)
            """, (
                workflow_id,
                tenant_id,
                definition["name"],
                definition.get("description", ""),
                int(bool(definition.get("enabled", True))),
                json.dumps(definition["triggers"], ensure_ascii=False),
                json.dumps(definition["steps"], ensure_ascii=False),
                now,
                now,
            ))
            connection.commit()
        except sqlite3.IntegrityError as error:
            raise ValueError(f"工作流 ID 已存在: {workflow_id}") from error
        finally:
            connection.close()
        return self.get_definition(workflow_id, tenant_id)

    def list_definitions(self, tenant_id: str) -> list[dict[str, Any]]:
        self.initialize()
        connection = self._connect()
        rows = connection.execute(
            "SELECT * FROM workflow_definitions WHERE tenant_id IN ('*', ?) ORDER BY built_in DESC, name",
            (tenant_id,),
        ).fetchall()
        connection.close()
        return [self._definition_row(row) for row in rows]

    def get_definition(self, workflow_id: str, tenant_id: str) -> dict[str, Any]:
        self.initialize()
        connection = self._connect()
        row = connection.execute(
            "SELECT * FROM workflow_definitions WHERE workflow_id=? AND tenant_id IN ('*', ?)",
            (workflow_id, tenant_id),
        ).fetchone()
        connection.close()
        if row is None:
            raise KeyError(f"工作流不存在: {workflow_id}")
        return self._definition_row(row)

    def create_run(
        self,
        workflow_id: str,
        tenant_id: str,
        trigger_type: str,
        idempotency_key: str,
        inputs: dict[str, Any],
        retry_of_run_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        self.initialize()
        run_id = uuid.uuid4().hex
        now = _utc_now()
        connection = self._connect()
        try:
            connection.execute("""
                INSERT INTO workflow_runs (
                    run_id, workflow_id, tenant_id, status, trigger_type,
                    idempotency_key, input_json, output_json, error,
                    retry_of_run_id, started_at, finished_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, '{}', '', ?, ?, NULL)
            """, (
                run_id,
                workflow_id,
                tenant_id,
                trigger_type,
                idempotency_key,
                json.dumps(inputs, ensure_ascii=False, default=str),
                retry_of_run_id,
                now,
            ))
            connection.commit()
            duplicate = False
        except sqlite3.IntegrityError:
            row = connection.execute(
                "SELECT run_id FROM workflow_runs WHERE tenant_id=? AND workflow_id=? AND idempotency_key=?",
                (tenant_id, workflow_id, idempotency_key),
            ).fetchone()
            if row is None:
                raise
            run_id = row["run_id"]
            duplicate = True
        finally:
            connection.close()
        return self.get_run(run_id), duplicate

    def finish_run(self, run_id: str, status: str, output: dict[str, Any], error: str = "") -> None:
        self.initialize()
        connection = self._connect()
        connection.execute(
            "UPDATE workflow_runs SET status=?, output_json=?, error=?, finished_at=? WHERE run_id=?",
            (status, json.dumps(output, ensure_ascii=False, default=str), error, _utc_now(), run_id),
        )
        connection.commit()
        connection.close()

    def create_step_run(
        self,
        run_id: str,
        step_key: str,
        step_type: str,
        sequence: int,
        inputs: dict[str, Any],
    ) -> str:
        self.initialize()
        step_run_id = uuid.uuid4().hex
        connection = self._connect()
        connection.execute("""
            INSERT INTO workflow_step_runs (
                step_run_id, run_id, step_key, step_type, sequence, status,
                input_json, output_json, error, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, 'running', ?, '{}', '', ?, NULL)
        """, (
            step_run_id,
            run_id,
            step_key,
            step_type,
            sequence,
            json.dumps(inputs, ensure_ascii=False, default=str),
            _utc_now(),
        ))
        connection.commit()
        connection.close()
        return step_run_id

    def finish_step(self, step_run_id: str, status: str, output: dict[str, Any], error: str = "") -> None:
        self.initialize()
        connection = self._connect()
        connection.execute(
            "UPDATE workflow_step_runs SET status=?, output_json=?, error=?, finished_at=? WHERE step_run_id=?",
            (status, json.dumps(output, ensure_ascii=False, default=str), error, _utc_now(), step_run_id),
        )
        connection.commit()
        connection.close()

    def get_run(self, run_id: str) -> dict[str, Any]:
        self.initialize()
        connection = self._connect()
        row = connection.execute("SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            connection.close()
            raise KeyError(f"工作流运行不存在: {run_id}")
        steps = connection.execute(
            "SELECT * FROM workflow_step_runs WHERE run_id=? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        connection.close()
        result = self._run_row(row)
        result["steps"] = [self._step_row(step) for step in steps]
        return result

    def create_alert(
        self,
        tenant_id: str,
        workflow_id: str,
        run_id: str,
        alert_type: str,
        title: str,
        severity: str,
        dedup_key: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        self.initialize()
        alert_id = uuid.uuid4().hex
        connection = self._connect()
        cursor = connection.execute("""
            INSERT OR IGNORE INTO alert_events (
                alert_id, tenant_id, workflow_id, run_id, alert_type, title,
                severity, status, dedup_key, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
        """, (
            alert_id,
            tenant_id,
            workflow_id,
            run_id,
            alert_type,
            title,
            severity,
            dedup_key,
            json.dumps(payload, ensure_ascii=False, default=str),
            _utc_now(),
        ))
        created = cursor.rowcount == 1
        connection.commit()
        row = connection.execute(
            "SELECT * FROM alert_events WHERE tenant_id=? AND workflow_id=? AND dedup_key=?",
            (tenant_id, workflow_id, dedup_key),
        ).fetchone()
        connection.close()
        return self._alert_row(row), created

    def create_task(
        self,
        tenant_id: str,
        workflow_id: str,
        run_id: str,
        task_type: str,
        title: str,
        dedup_key: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        self.initialize()
        task_id = uuid.uuid4().hex
        now = _utc_now()
        connection = self._connect()
        cursor = connection.execute("""
            INSERT OR IGNORE INTO tasks (
                task_id, tenant_id, workflow_id, run_id, task_type, title,
                status, dedup_key, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
        """, (
            task_id,
            tenant_id,
            workflow_id,
            run_id,
            task_type,
            title,
            dedup_key,
            json.dumps(payload, ensure_ascii=False, default=str),
            now,
            now,
        ))
        created = cursor.rowcount == 1
        connection.commit()
        row = connection.execute(
            "SELECT * FROM tasks WHERE tenant_id=? AND workflow_id=? AND dedup_key=?",
            (tenant_id, workflow_id, dedup_key),
        ).fetchone()
        connection.close()
        return self._task_row(row), created

    def list_tasks(self, tenant_id: str, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        connection = self._connect()
        if status:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE tenant_id=? AND status=? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, status, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        connection.close()
        return [self._task_row(row) for row in rows]

    def list_alerts(self, tenant_id: str, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        connection = self._connect()
        if status:
            rows = connection.execute(
                "SELECT * FROM alert_events WHERE tenant_id=? AND status=? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, status, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM alert_events WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        connection.close()
        return [self._alert_row(row) for row in rows]

    @staticmethod
    def _definition_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["built_in"] = bool(result["built_in"])
        result["triggers"] = json.loads(result.pop("triggers_json"))
        result["steps"] = json.loads(result.pop("steps_json"))
        return result

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["inputs"] = json.loads(result.pop("input_json"))
        result["output"] = json.loads(result.pop("output_json"))
        return result

    @staticmethod
    def _step_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["inputs"] = json.loads(result.pop("input_json"))
        result["output"] = json.loads(result.pop("output_json"))
        return result

    @staticmethod
    def _alert_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result


workflow_store = WorkflowStore()
