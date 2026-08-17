"""电商数据字段映射与确定性质量检查。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from domain.ecommerce_schema import EntityDefinition, FieldDefinition, get_entity, suggest_field_mapping


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _is_valid_datetime(value: Any) -> bool:
    if isinstance(value, datetime):
        return True
    text = str(value).strip()
    if not text:
        return False
    normalized = text.replace("Z", "+00:00").replace("/", "-")
    try:
        datetime.fromisoformat(normalized)
        return True
    except ValueError:
        return False


def _validate_value(value: Any, definition: FieldDefinition) -> str | None:
    if _is_empty(value):
        return None
    if definition.data_type in {"number", "integer"}:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "not_numeric"
        if definition.data_type == "integer" and not number.is_integer():
            return "not_integer"
        if definition.minimum is not None and number < definition.minimum:
            return "below_minimum"
        if definition.maximum is not None and number > definition.maximum:
            return "above_maximum"
    elif definition.data_type in {"date", "datetime"} and not _is_valid_datetime(value):
        return "invalid_datetime"
    return None


def _source_fields(rows: list[dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows[:100]:
        for field in row:
            if field not in seen:
                seen.add(field)
                fields.append(field)
    return fields


def _validate_mapping(entity: EntityDefinition, mapping: dict[str, str]) -> None:
    valid_targets = {field.field_key for field in entity.fields}
    invalid_targets = sorted(set(mapping.values()) - valid_targets)
    if invalid_targets:
        raise ValueError(f"映射包含未知标准字段: {', '.join(invalid_targets)}")
    if len(set(mapping.values())) != len(mapping.values()):
        raise ValueError("多个源字段不能映射到同一个标准字段")


def check_data_quality(
    entity_key: str,
    rows: list[dict[str, Any]],
    mapping: dict[str, str] | None = None,
) -> dict[str, Any]:
    entity = get_entity(entity_key)
    if not isinstance(rows, list):
        raise ValueError("rows 必须是数组")
    if len(rows) > 10000:
        raise ValueError("单次质量检查最多支持 10000 行")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("rows 中的每一行必须是对象")

    source_fields = _source_fields(rows)
    mapping_result = suggest_field_mapping(entity_key, source_fields)
    effective_mapping = dict(mapping) if mapping is not None else mapping_result["mapping"]
    _validate_mapping(entity, effective_mapping)

    target_to_source = {target: source for source, target in effective_mapping.items()}
    required_fields = [field for field in entity.fields if field.required]
    missing_required_columns = [
        field.field_key for field in required_fields if field.field_key not in target_to_source
    ]

    issues: list[dict[str, Any]] = []
    for field_key in missing_required_columns:
        issues.append({
            "code": "missing_required_column",
            "severity": "error",
            "field": field_key,
            "message": f"缺少必填字段映射: {field_key}",
        })

    field_stats: dict[str, dict[str, Any]] = {}
    total_invalid = 0
    total_required_missing = 0
    checked_cells = 0
    for definition in entity.fields:
        source = target_to_source.get(definition.field_key)
        if not source:
            continue
        values = [row.get(source) for row in rows]
        missing_count = sum(1 for value in values if _is_empty(value))
        invalid_reasons: dict[str, int] = {}
        for value in values:
            reason = _validate_value(value, definition)
            if reason:
                invalid_reasons[reason] = invalid_reasons.get(reason, 0) + 1
        invalid_count = sum(invalid_reasons.values())
        total_invalid += invalid_count
        checked_cells += len(values)
        if definition.required:
            total_required_missing += missing_count

        field_stats[definition.field_key] = {
            "source_field": source,
            "row_count": len(values),
            "missing_count": missing_count,
            "missing_rate_pct": round(missing_count * 100 / len(values), 2) if values else 0,
            "invalid_count": invalid_count,
            "invalid_reasons": invalid_reasons,
            "distinct_count": len({str(value) for value in values if not _is_empty(value)}),
        }
        if definition.required and missing_count:
            issues.append({
                "code": "missing_required_values",
                "severity": "error" if missing_count == len(values) else "warning",
                "field": definition.field_key,
                "message": f"必填字段 {definition.field_key} 有 {missing_count} 行为空",
            })
        if invalid_count:
            issues.append({
                "code": "invalid_values",
                "severity": "error" if invalid_count == len(values) else "warning",
                "field": definition.field_key,
                "message": f"字段 {definition.field_key} 有 {invalid_count} 个格式或范围错误",
                "reasons": invalid_reasons,
            })

    duplicate_count = 0
    primary_fields = [field for field in entity.fields if field.primary_key and field.field_key in target_to_source]
    if primary_fields:
        seen_keys: set[tuple[str, ...]] = set()
        for row in rows:
            key = tuple(str(row.get(target_to_source[field.field_key], "")).strip() for field in primary_fields)
            if any(key) and key in seen_keys:
                duplicate_count += 1
            elif any(key):
                seen_keys.add(key)
        if duplicate_count:
            issues.append({
                "code": "duplicate_primary_key",
                "severity": "error",
                "field": ",".join(field.field_key for field in primary_fields),
                "message": f"发现 {duplicate_count} 行重复主键",
            })

    row_count = len(rows)
    score = 100.0
    score -= min(50, len(missing_required_columns) * 20)
    if row_count and required_fields:
        score -= min(25, total_required_missing * 25 / (row_count * len(required_fields)))
    if checked_cells:
        score -= min(20, total_invalid * 20 / checked_cells)
    if row_count:
        score -= min(25, duplicate_count * 25 / row_count)
    if not rows:
        score = 0
        issues.append({
            "code": "empty_dataset",
            "severity": "error",
            "field": "",
            "message": "数据集没有可检查的记录",
        })
    score = round(max(0, score), 2)
    has_error = any(issue["severity"] == "error" for issue in issues)
    if has_error or score < 70:
        status = "fail"
    elif score < 90 or issues:
        status = "warning"
    else:
        status = "pass"

    canonical_preview: list[dict[str, Any]] = []
    sensitive_fields = {field.field_key for field in entity.fields if field.sensitive}
    for row in rows[:5]:
        canonical_preview.append({
            target: "***" if target in sensitive_fields and not _is_empty(row.get(source)) else row.get(source)
            for source, target in effective_mapping.items()
        })

    return {
        "schema_version": "1.0",
        "entity": entity_key,
        "row_count": row_count,
        "source_fields": source_fields,
        "mapping": effective_mapping,
        "mapping_suggestion": mapping_result,
        "missing_required_fields": missing_required_columns,
        "score": score,
        "status": status,
        "issue_count": len(issues),
        "issues": issues,
        "field_stats": field_stats,
        "duplicate_primary_key_count": duplicate_count,
        "canonical_preview": canonical_preview,
    }
