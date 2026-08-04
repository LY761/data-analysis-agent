# -*- coding: utf-8 -*-
"""K1: 数据接入回归测试 — CSV/XLSX 解析、类型推断、自动建表、上传端点、schema 合并"""
import sqlite3
import io
from fastapi.testclient import TestClient
import main

from config import DEMO_DB_PATH
from services.data_ingest import (safe_table_name, ingest_file, _parse_csv,
                                  _parse_xlsx, _infer_sql_type)
from db.init_db import build_full_sqlite_schemas

client = TestClient(main.app)

CSV_UTF8 = "产品,单价,销量\n显示器,1299,50\n键盘,199,120\n".encode("utf-8")
CSV_GBK = "产品,单价\n鼠标,59\n".encode("gbk")


def _drop_tables(*names):
    conn = sqlite3.connect(DEMO_DB_PATH)
    for n in names:
        conn.execute(f'DROP TABLE IF EXISTS "{n}"')
    conn.commit()
    conn.close()


def test_safe_table_name():
    assert safe_table_name("销售数据.csv") == "data_销售数据"
    assert safe_table_name("2026 sales.xlsx") == "data_t_2026_sales"
    assert safe_table_name("a b/c!.txt") == "data_a_b_c"


def test_parse_csv_utf8_and_gbk():
    h, rows = _parse_csv(CSV_UTF8)
    assert h == ["产品", "单价", "销量"]
    assert rows[0] == ["显示器", "1299", "50"]
    h2, rows2 = _parse_csv(CSV_GBK)
    assert h2 == ["产品", "单价"]
    assert rows2[0][0] == "鼠标"


def test_infer_sql_type():
    assert _infer_sql_type(["1", "2", "3"]) == "INTEGER"
    assert _infer_sql_type(["1.5", "2.0"]) == "REAL"
    assert _infer_sql_type(["a", "b"]) == "TEXT"
    assert _infer_sql_type(["1", "", "x"]) == "TEXT"


def test_parse_xlsx():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["产品", "价格"])
    ws.append(["鼠标", 59])
    buf = io.BytesIO()
    wb.save(buf)
    h, rows = _parse_xlsx(buf.getvalue())
    assert h == ["产品", "价格"]
    assert rows[0][0] == "鼠标"


def test_ingest_file_end_to_end():
    """上传 CSV → 建表 → executor 可查新表"""
    table = "data_test_products"
    _drop_tables(table)
    try:
        r = ingest_file("test_products.csv", CSV_UTF8)
        assert r["error"] is None
        assert r["table"] == "data_test_products"
        assert r["row_count"] == 2
        conn = sqlite3.connect(DEMO_DB_PATH)
        n = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        conn.close()
        assert n == 2
        # executor 能查（NL2SQL 数据链路打通）
        from db.executor import executor
        q = executor.execute(f'SELECT * FROM "{table}" LIMIT 1')
        assert q["success"] is True
        assert q["data"][0]["产品"] == "显示器"
        # schema 合并包含新表
        schemas = build_full_sqlite_schemas()
        assert any(s["table"] == table for s in schemas)
    finally:
        _drop_tables(table)


def test_ingest_file_invalid():
    assert ingest_file("x.txt", b"abc")["error"]
    assert ingest_file("x.csv", b"")["error"] or ingest_file("x.csv", b"")["row_count"] == 0


def test_upload_endpoint():
    """POST /api/data/upload 上传 CSV"""
    table = "data_endpoint_test"
    _drop_tables(table)
    try:
        r = client.post("/api/data/upload",
                        files={"file": ("endpoint_test.csv", CSV_UTF8,
                                        "text/csv")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["error"] is None
        assert body["table"] == "data_endpoint_test"
        assert body["row_count"] == 2
    finally:
        _drop_tables(table)
