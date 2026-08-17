"""字段映射确认、标准化转换和快照导入服务。"""

from __future__ import annotations

from typing import Any

from domain.ecommerce_schema import get_entity
from services.data_ingest import parse_file
from services.data_quality import check_data_quality
from services.standard_data_store import StandardDataStore, standard_data_store


def validate_mapping(entity_type: str, mapping: dict[str, str]) -> None:
    entity = get_entity(entity_type)
    if not mapping:
        raise ValueError("mapping 不能为空")
    if any(not str(source).strip() or not str(target).strip() for source, target in mapping.items()):
        raise ValueError("源字段和标准字段不能为空")
    target_fields = {definition.field_key for definition in entity.fields}
    invalid_targets = sorted(set(mapping.values()) - target_fields)
    if invalid_targets:
        raise ValueError(f"映射包含未知标准字段: {', '.join(invalid_targets)}")
    if len(set(mapping.values())) != len(mapping.values()):
        raise ValueError("多个源字段不能映射到同一个标准字段")
    required_fields = {definition.field_key for definition in entity.fields if definition.required}
    missing_required = sorted(required_fields - set(mapping.values()))
    if missing_required:
        raise ValueError(f"映射缺少必填标准字段: {', '.join(missing_required)}")


def canonicalize_rows(
    entity_type: str,
    rows: list[dict[str, Any]],
    mapping: dict[str, str],
) -> list[dict[str, Any]]:
    validate_mapping(entity_type, mapping)
    return [
        {target: row.get(source) for source, target in mapping.items()}
        for row in rows
    ]


def confirm_mapping(
    tenant_id: str,
    entity_type: str,
    profile_name: str,
    mapping: dict[str, str],
    store: StandardDataStore | None = None,
) -> dict[str, Any]:
    if not tenant_id.strip():
        raise ValueError("tenant_id 不能为空")
    if not profile_name.strip():
        raise ValueError("profile_name 不能为空")
    validate_mapping(entity_type, mapping)
    return (store or standard_data_store).save_mapping_profile(
        tenant_id.strip(),
        entity_type,
        profile_name.strip(),
        mapping,
    )


def import_standard_rows(
    tenant_id: str,
    entity_type: str,
    source_name: str,
    rows: list[dict[str, Any]],
    mapping: dict[str, str],
    allow_warning: bool = True,
    store: StandardDataStore | None = None,
) -> dict[str, Any]:
    if not tenant_id.strip():
        raise ValueError("tenant_id 不能为空")
    if not source_name.strip():
        raise ValueError("source_name 不能为空")
    quality = check_data_quality(entity_type, rows, mapping)
    if quality["status"] == "fail":
        raise ValueError("数据质量检查失败，不能写入标准数据层")
    if quality["status"] == "warning" and not allow_warning:
        raise ValueError("数据质量存在警告，请确认后再导入")
    canonical_rows = canonicalize_rows(entity_type, rows, quality["mapping"])
    snapshot = (store or standard_data_store).create_snapshot(
        tenant_id=tenant_id.strip(),
        entity_type=entity_type,
        source_name=source_name.strip(),
        mapping=quality["mapping"],
        canonical_rows=canonical_rows,
        quality_score=quality["score"],
        quality_status=quality["status"],
    )
    return {"snapshot": snapshot, "quality": quality}


def import_standard_file(
    tenant_id: str,
    entity_type: str,
    filename: str,
    content: bytes,
    mapping: dict[str, str],
    allow_warning: bool = True,
    store: StandardDataStore | None = None,
) -> dict[str, Any]:
    parsed = parse_file(filename, content)
    if parsed.get("error"):
        raise ValueError(parsed["error"])
    headers = parsed["headers"]
    rows = [
        {headers[index]: row[index] if index < len(row) else None for index in range(len(headers))}
        for row in parsed["rows"]
    ]
    return import_standard_rows(
        tenant_id=tenant_id,
        entity_type=entity_type,
        source_name=filename,
        rows=rows,
        mapping=mapping,
        allow_warning=allow_warning,
        store=store,
    )
