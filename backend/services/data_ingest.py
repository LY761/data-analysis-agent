"""
数据接入 — 上传 CSV/Excel 自动建表入库，立即可被 NL2SQL 查询。

支持: .csv（utf-8/gbk 自动探测）、.xlsx（openpyxl）
流程: 解析 → 推断列类型 → 建表（SQLite 当前库）→ 参数化写入 →
      Schema 动态合并并重建向量索引（让 Agent 立刻能问新表）
"""
import csv
import io
import logging
import re
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

# 系统内部表（上传建表时跳过，不污染用户数据）
_INTERNAL_TABLES = {"query_cache", "conversation_history", "retrieval_log"}

ALLOWED_EXT = {".csv", ".xlsx"}
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20MB


def safe_table_name(filename: str) -> str:
    """文件名 → 安全表名：去扩展名、非字母数字下划线转下划线、加 data_ 前缀、截断 40。"""
    base = re.sub(r"\.[^.]+$", "", filename or "upload")
    name = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]+", "_", base).strip("_")[:40]
    if not name:
        name = "upload"
    if name[0].isdigit():
        name = "t_" + name
    return f"data_{name.lower()}"


def _parse_csv(content: bytes) -> tuple:
    """解析 CSV（自动探测 utf-8/gbk），返回 (headers, rows)。"""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = content.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = content.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return [], []
    headers = [h.strip() or f"col{i}" for i, h in enumerate(rows[0])]
    data = rows[1:]
    return headers, data


def _parse_xlsx(content: bytes) -> tuple:
    """解析 xlsx（openpyxl），返回 (headers, rows)。"""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    rows = []
    for row in ws.iter_rows(values_only=True):
        if row is None or all(c is None or str(c).strip() == "" for c in row):
            continue
        rows.append(["" if c is None else c for c in row])
    wb.close()
    if not rows:
        return [], []
    headers = [str(h).strip() or f"col{i}" for i, h in enumerate(rows[0])]
    return headers, rows[1:]


def _infer_sql_type(values: list) -> str:
    """按样本值推断 SQLite 列类型：INTEGER / REAL / TEXT。"""
    ints = floats = 0
    n = 0
    for v in values[:100]:
        s = str(v).strip()
        if s == "":
            continue
        n += 1
        try:
            int(s)
            ints += 1
            continue
        except ValueError:
            pass
        try:
            float(s)
            floats += 1
        except ValueError:
            pass
    if n and ints == n:
        return "INTEGER"
    if n and ints + floats == n:
        return "REAL"
    return "TEXT"


def _to_insert_value(v, col_type: str):
    """按列类型转换值（None/空 → None）。"""
    if v is None or str(v).strip() == "":
        return None
    if col_type == "INTEGER":
        try:
            return int(str(v).strip())
        except ValueError:
            return None
    if col_type == "REAL":
        try:
            return float(str(v).strip())
        except ValueError:
            return None
    return str(v)


def parse_file(filename: str, content: bytes) -> dict:
    """解析并校验表格文件，不写入数据库。"""
    if len(content) > MAX_FILE_BYTES:
        return {"error": f"文件超过 {MAX_FILE_BYTES // 1024 // 1024}MB 限制"}
    ext = "." + (filename.rsplit(".", 1)[-1].lower() if "." in filename else "")
    if ext not in ALLOWED_EXT:
        return {"error": f"仅支持 {'/'.join(sorted(ALLOWED_EXT))} 格式"}

    try:
        if ext == ".csv":
            headers, rows = _parse_csv(content)
        else:
            headers, rows = _parse_xlsx(content)
    except Exception as error:
        logger.warning(f"[DataIngest] 解析失败: {error}")
        return {"error": f"文件解析失败: {error}"}

    if not headers:
        return {"error": "文件为空或没有表头"}
    if len(headers) > 50:
        return {"error": f"列数过多（{len(headers)} 列，上限 50）"}
    if len(rows) > 200_000:
        return {"error": "行数超过 20 万，请分批导入"}
    return {"error": None, "extension": ext, "headers": headers, "rows": rows}


def inspect_file(filename: str, content: bytes, entity_type: str) -> dict:
    """上传前预检：解析文件、建议字段映射并执行质量检查。"""
    parsed = parse_file(filename, content)
    if parsed.get("error"):
        return parsed
    from services.data_quality import check_data_quality

    headers = parsed["headers"]
    dict_rows = [
        {headers[index]: row[index] if index < len(row) else None for index in range(len(headers))}
        for row in parsed["rows"]
    ]
    quality = check_data_quality(entity_type, dict_rows)
    return {
        "error": None,
        "filename": filename,
        "extension": parsed["extension"],
        "quality": quality,
    }


def ingest_file(filename: str, content: bytes) -> dict:
    """解析并入库文件。返回 {table, columns, row_count, error}。"""
    from db.executor import executor

    if executor.backend != "sqlite":
        return {"error": "文件自动建表当前仅支持 SQLite 数据源，请切换到 SQLite 后重试。"}

    active_db_path = executor.sqlite_path
    parsed = parse_file(filename, content)
    if parsed.get("error"):
        return parsed
    headers = parsed["headers"]
    rows = parsed["rows"]

    table = safe_table_name(filename)
    col_types = [_infer_sql_type([r[i] for r in rows[:100] if i < len(r)])
                 for i in range(len(headers))]
    # 列名安全化（重复列名去重）
    safe_headers = []
    seen = set()
    for h in headers:
        ch = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]+", "_", str(h)).strip("_") or "col"
        if ch[0].isdigit():
            ch = "c_" + ch
        if ch in seen:
            ch = f"{ch}_{len(safe_headers)}"
        seen.add(ch)
        safe_headers.append(ch)

    try:
        conn = sqlite3.connect(active_db_path)
        col_defs = ", ".join(f'"{ch}" {ct}' for ch, ct in zip(safe_headers, col_types))
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({col_defs})')
        placeholders = ", ".join(["?"] * len(safe_headers))
        sql = f'INSERT INTO "{table}" ({", ".join(f"\"{ch}\"" for ch in safe_headers)}) VALUES ({placeholders})'
        params = [
            tuple(_to_insert_value(r[i] if i < len(r) else None, col_types[i])
                  for i in range(len(safe_headers)))
            for r in rows
        ]
        conn.executemany(sql, params)
        conn.commit()
        conn.close()
        logger.info(f"[DataIngest] 表 {table} 入库 {len(rows)} 行")
    except Exception as e:
        logger.warning(f"[DataIngest] 入库失败: {e}")
        return {"error": f"入库失败: {e}"}

    # 重建 Schema 索引，让 NL2SQL 立刻能问新表（尽力而为，失败不影响数据入库）
    try:
        from db.init_db import build_full_sqlite_schemas
        from agent.schema_retriever import schema_retriever
        schemas = build_full_sqlite_schemas(active_db_path)
        schema_retriever.index_schemas(schemas, force=True)
    except Exception as e:
        logger.warning(f"[DataIngest] Schema 索引重建失败: {e}")

    return {"table": table, "columns": safe_headers,
            "row_count": len(rows), "error": None}
