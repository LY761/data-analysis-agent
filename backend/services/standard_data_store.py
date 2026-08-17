"""标准电商数据、映射、快照和分析证据的 SQLite 仓储。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from config import STANDARD_DATA_DB_PATH
from domain.ecommerce_schema import EntityDefinition, FieldDefinition, get_entity, list_entities


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sqlite_type(definition: FieldDefinition) -> str:
    if definition.data_type == "integer":
        return "INTEGER"
    if definition.data_type == "number":
        return "REAL"
    return "TEXT"


def _coerce_value(value: Any, definition: FieldDefinition) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if definition.data_type == "integer":
        return int(float(value))
    if definition.data_type == "number":
        return float(value)
    return str(value).strip()


class StandardDataStore:
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
            CREATE TABLE IF NOT EXISTS mapping_profiles (
                mapping_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                profile_name TEXT NOT NULL,
                mapping_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, entity_type, profile_name)
            );
            CREATE TABLE IF NOT EXISTS data_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                source_name TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                quality_score REAL NOT NULL,
                quality_status TEXT NOT NULL,
                mapping_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_tenant_entity
                ON data_snapshots(tenant_id, entity_type, created_at DESC);
            CREATE TABLE IF NOT EXISTS analysis_runs (
                run_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                scenario TEXT NOT NULL,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                snapshot_ids_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS analysis_evidence (
                evidence_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                metric_key TEXT NOT NULL,
                metric_version TEXT NOT NULL,
                formula TEXT NOT NULL,
                current_value REAL,
                previous_value REAL,
                contribution REAL,
                source_json TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES analysis_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_runs_tenant
                ON analysis_runs(tenant_id, scenario, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_analysis_evidence_run
                ON analysis_evidence(run_id);
        """)
        for entity in list_entities():
            self._create_standard_table(connection, entity)
        connection.commit()
        connection.close()
        self._initialized = True

    def _create_standard_table(self, connection: sqlite3.Connection, entity: EntityDefinition) -> None:
        field_columns = ",\n".join(
            f'"{definition.field_key}" {_sqlite_type(definition)}'
            for definition in entity.fields
        )
        connection.execute(f"""
            CREATE TABLE IF NOT EXISTS "std_{entity.entity_key}" (
                _record_id INTEGER PRIMARY KEY AUTOINCREMENT,
                _snapshot_id TEXT NOT NULL,
                _tenant_id TEXT NOT NULL,
                _source_row_number INTEGER NOT NULL,
                _record_key TEXT,
                _imported_at TEXT NOT NULL,
                {field_columns},
                FOREIGN KEY(_snapshot_id) REFERENCES data_snapshots(snapshot_id)
            )
        """)
        connection.execute(
            f'CREATE INDEX IF NOT EXISTS "idx_std_{entity.entity_key}_snapshot" '
            f'ON "std_{entity.entity_key}"(_snapshot_id)'
        )

    def save_mapping_profile(
        self,
        tenant_id: str,
        entity_type: str,
        profile_name: str,
        mapping: dict[str, str],
    ) -> dict[str, Any]:
        self.initialize()
        get_entity(entity_type)
        now = _utc_now()
        mapping_id = uuid.uuid4().hex
        connection = self._connect()
        connection.execute("""
            INSERT INTO mapping_profiles (
                mapping_id, tenant_id, entity_type, profile_name, mapping_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, entity_type, profile_name) DO UPDATE SET
                mapping_json = excluded.mapping_json,
                updated_at = excluded.updated_at
        """, (
            mapping_id,
            tenant_id,
            entity_type,
            profile_name,
            json.dumps(mapping, ensure_ascii=False, sort_keys=True),
            now,
            now,
        ))
        connection.commit()
        row = connection.execute(
            "SELECT * FROM mapping_profiles WHERE tenant_id=? AND entity_type=? AND profile_name=?",
            (tenant_id, entity_type, profile_name),
        ).fetchone()
        connection.close()
        return self._mapping_row(row)

    def list_mapping_profiles(self, tenant_id: str, entity_type: str = "") -> list[dict[str, Any]]:
        self.initialize()
        connection = self._connect()
        if entity_type:
            rows = connection.execute(
                "SELECT * FROM mapping_profiles WHERE tenant_id=? AND entity_type=? ORDER BY updated_at DESC",
                (tenant_id, entity_type),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM mapping_profiles WHERE tenant_id=? ORDER BY updated_at DESC",
                (tenant_id,),
            ).fetchall()
        connection.close()
        return [self._mapping_row(row) for row in rows]

    def create_snapshot(
        self,
        tenant_id: str,
        entity_type: str,
        source_name: str,
        mapping: dict[str, str],
        canonical_rows: list[dict[str, Any]],
        quality_score: float,
        quality_status: str,
    ) -> dict[str, Any]:
        self.initialize()
        entity = get_entity(entity_type)
        snapshot_id = uuid.uuid4().hex
        created_at = _utc_now()
        serialized_rows = json.dumps(canonical_rows, ensure_ascii=False, sort_keys=True, default=str)
        content_hash = hashlib.sha256(serialized_rows.encode("utf-8")).hexdigest()
        connection = self._connect()
        existing = connection.execute(
            """
            SELECT snapshot_id FROM data_snapshots
            WHERE tenant_id=? AND entity_type=? AND source_name=? AND content_hash=? AND status='ready'
            ORDER BY created_at DESC LIMIT 1
            """,
            (tenant_id, entity_type, source_name, content_hash),
        ).fetchone()
        if existing is not None:
            connection.close()
            return self.get_snapshot(existing["snapshot_id"])
        try:
            connection.execute("BEGIN")
            connection.execute("""
                INSERT INTO data_snapshots (
                    snapshot_id, tenant_id, entity_type, source_name, content_hash,
                    row_count, quality_score, quality_status, mapping_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?)
            """, (
                snapshot_id,
                tenant_id,
                entity_type,
                source_name,
                content_hash,
                len(canonical_rows),
                quality_score,
                quality_status,
                json.dumps(mapping, ensure_ascii=False, sort_keys=True),
                created_at,
            ))
            self._insert_standard_rows(connection, entity, tenant_id, snapshot_id, canonical_rows, created_at)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_snapshot(snapshot_id)

    def _insert_standard_rows(
        self,
        connection: sqlite3.Connection,
        entity: EntityDefinition,
        tenant_id: str,
        snapshot_id: str,
        rows: list[dict[str, Any]],
        imported_at: str,
    ) -> None:
        fields = [definition.field_key for definition in entity.fields]
        definitions = {definition.field_key: definition for definition in entity.fields}
        primary_fields = [definition.field_key for definition in entity.fields if definition.primary_key]
        columns = ["_snapshot_id", "_tenant_id", "_source_row_number", "_record_key", "_imported_at", *fields]
        placeholders = ", ".join("?" for _ in columns)
        quoted_columns = ", ".join(f'"{column}"' for column in columns)
        sql = f'INSERT INTO "std_{entity.entity_key}" ({quoted_columns}) VALUES ({placeholders})'
        params = []
        for row_number, row in enumerate(rows, 1):
            record_key = "|".join(str(row.get(field, "")) for field in primary_fields) if primary_fields else ""
            params.append((
                snapshot_id,
                tenant_id,
                row_number,
                record_key,
                imported_at,
                *[_coerce_value(row.get(field), definitions[field]) for field in fields],
            ))
        connection.executemany(sql, params)

    def list_snapshots(
        self,
        tenant_id: str,
        entity_type: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.initialize()
        connection = self._connect()
        if entity_type:
            rows = connection.execute(
                "SELECT * FROM data_snapshots WHERE tenant_id=? AND entity_type=? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, entity_type, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM data_snapshots WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        connection.close()
        return [self._snapshot_row(row) for row in rows]

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        self.initialize()
        connection = self._connect()
        row = connection.execute(
            "SELECT * FROM data_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        connection.close()
        if row is None:
            raise KeyError(f"数据快照不存在: {snapshot_id}")
        return self._snapshot_row(row)

    def get_snapshot_rows(self, snapshot_id: str, limit: int = 100) -> list[dict[str, Any]]:
        snapshot = self.get_snapshot(snapshot_id)
        entity = get_entity(snapshot["entity_type"])
        fields = [definition.field_key for definition in entity.fields]
        quoted_fields = ", ".join(f'"{field}"' for field in fields)
        connection = self._connect()
        rows = connection.execute(
            f'SELECT {quoted_fields} FROM "std_{entity.entity_key}" '
            "WHERE _snapshot_id=? ORDER BY _source_row_number LIMIT ?",
            (snapshot_id, limit),
        ).fetchall()
        connection.close()
        return [dict(row) for row in rows]

    def save_analysis_run(
        self,
        tenant_id: str,
        scenario: str,
        request: dict[str, Any],
        result: dict[str, Any],
        snapshot_ids: list[str],
    ) -> dict[str, Any]:
        self.initialize()
        run_id = uuid.uuid4().hex
        created_at = _utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            connection.execute("""
                INSERT INTO analysis_runs (
                    run_id, tenant_id, scenario, status, request_json,
                    result_json, snapshot_ids_json, created_at
                ) VALUES (?, ?, ?, 'completed', ?, ?, ?, ?)
            """, (
                run_id,
                tenant_id,
                scenario,
                json.dumps(request, ensure_ascii=False, default=str),
                json.dumps(result, ensure_ascii=False, default=str),
                json.dumps(snapshot_ids, ensure_ascii=False),
                created_at,
            ))
            for evidence in result.get("evidence", []):
                connection.execute("""
                    INSERT INTO analysis_evidence (
                        evidence_id, run_id, metric_key, metric_version, formula,
                        current_value, previous_value, contribution, source_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    uuid.uuid4().hex,
                    run_id,
                    evidence.get("metric_key", ""),
                    evidence.get("metric_version", ""),
                    evidence.get("formula", ""),
                    evidence.get("current_value"),
                    evidence.get("previous_value"),
                    evidence.get("contribution"),
                    json.dumps(evidence.get("source", {}), ensure_ascii=False, default=str),
                ))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {"run_id": run_id, "status": "completed", "created_at": created_at}

    def list_analysis_runs(
        self,
        tenant_id: str,
        scenario: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.initialize()
        connection = self._connect()
        if scenario:
            rows = connection.execute(
                "SELECT * FROM analysis_runs WHERE tenant_id=? AND scenario=? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, scenario, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM analysis_runs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        connection.close()
        return [self._analysis_row(row, include_result=False) for row in rows]

    def get_analysis_run(self, run_id: str, tenant_id: str) -> dict[str, Any]:
        self.initialize()
        connection = self._connect()
        row = connection.execute(
            "SELECT * FROM analysis_runs WHERE run_id=? AND tenant_id=?",
            (run_id, tenant_id),
        ).fetchone()
        if row is None:
            connection.close()
            raise KeyError(f"分析运行不存在: {run_id}")
        evidence_rows = connection.execute(
            "SELECT * FROM analysis_evidence WHERE run_id=? ORDER BY evidence_id",
            (run_id,),
        ).fetchall()
        connection.close()
        result = self._analysis_row(row, include_result=True)
        result["evidence"] = [
            {
                **{key: value for key, value in dict(evidence).items() if key != "source_json"},
                "source": json.loads(evidence["source_json"]),
            }
            for evidence in evidence_rows
        ]
        return result

    @staticmethod
    def _mapping_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["mapping"] = json.loads(data.pop("mapping_json"))
        return data

    @staticmethod
    def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["mapping"] = json.loads(data.pop("mapping_json"))
        return data

    @staticmethod
    def _analysis_row(row: sqlite3.Row, include_result: bool) -> dict[str, Any]:
        data = dict(row)
        data["request"] = json.loads(data.pop("request_json"))
        data["snapshot_ids"] = json.loads(data.pop("snapshot_ids_json"))
        result = json.loads(data.pop("result_json"))
        if include_result:
            data["result"] = result
        else:
            data["summary"] = {
                "conclusion": result.get("conclusion", ""),
                "severity": result.get("severity", ""),
                "product_count": result.get("summary", {}).get("product_count"),
            }
        return data


standard_data_store = StandardDataStore()
