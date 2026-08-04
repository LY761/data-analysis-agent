# -*- coding: utf-8 -*-
"""A3: MySQL 后端切换回归测试 — 方言翻译 / 代理分发 / schema 发现 / switch 接线"""
from unittest.mock import patch
from db.mysql_executor import translate_sqlite_to_mysql


# ═══ 1. SQLite→MySQL 方言翻译 ═══

def test_translate_column_ref():
    assert (translate_sqlite_to_mysql("strftime('%Y-%m', order_date) = '2026-01'")
            == "DATE_FORMAT(order_date, '%Y-%m') = '2026-01'")


def test_translate_qualified_column():
    assert (translate_sqlite_to_mysql("strftime('%Y', o.order_date)")
            == "DATE_FORMAT(o.order_date, '%Y')")


def test_translate_now():
    assert translate_sqlite_to_mysql("strftime('%Y-%m', 'now')") == "DATE_FORMAT(NOW(), '%Y-%m')"


def test_translate_offset():
    assert (translate_sqlite_to_mysql("strftime('%Y-%m', 'now', '-1 month')")
            == "DATE_FORMAT(DATE_SUB(NOW(), INTERVAL 1 MONTH), '%Y-%m')")
    assert (translate_sqlite_to_mysql("strftime('%Y-%m-%d', 'now', '+7 day')")
            == "DATE_FORMAT(DATE_ADD(NOW(), INTERVAL 7 DAY), '%Y-%m-%d')")


def test_translate_date_now():
    assert translate_sqlite_to_mysql("date('now')") == "CURDATE()"
    assert translate_sqlite_to_mysql("datetime('now')") == "NOW()"


def test_plain_sql_unchanged():
    assert translate_sqlite_to_mysql("SELECT * FROM orders") == "SELECT * FROM orders"


def test_quick_query_style_full():
    """快捷卡模板里的典型语句应整体翻译"""
    src = ("SELECT SUM(total_amount) AS total_sales FROM orders "
           "WHERE strftime('%Y-%m', order_date) = strftime('%Y-%m', 'now')")
    dst = ("SELECT SUM(total_amount) AS total_sales FROM orders "
           "WHERE DATE_FORMAT(order_date, '%Y-%m') = DATE_FORMAT(NOW(), '%Y-%m')")
    assert translate_sqlite_to_mysql(src) == dst


# ═══ 2. ExecutorProxy 分发 ═══

def test_proxy_default_sqlite():
    from db.executor import executor
    assert executor.backend == "sqlite"
    r = executor.execute("SELECT 1 AS x")
    assert r["success"] is True
    assert r["data"] == [{"x": 1}]


def test_proxy_switch_mysql_then_back():
    """切换到 MySQL 后端：backend 变 mysql；无 pymysql/无服务器时应返回明确错误而非崩溃"""
    from db.executor import executor
    executor.set_backend("mysql", "mysql://user:pass@localhost:3306/testdb")
    try:
        assert executor.backend == "mysql"
        r = executor.execute("SELECT 1")
        assert r.get("success") is False
        assert r.get("error"), r
    finally:
        executor.set_backend("sqlite")
    assert executor.backend == "sqlite"


# ═══ 3. Schema 描述支持 MySQL ═══

def test_get_schema_descriptions_mysql_uses_discovery():
    from db import init_db
    fake = [{"table": "orders", "ddl": "CREATE TABLE orders (...);", "description": "x",
             "columns": [{"name": "id", "type": "INT", "comment": ""}],
             "sample_queries": []}]
    with patch("db.init_db._mysql_schema_descriptions", return_value=fake) as m:
        out = init_db.get_schema_descriptions("mysql", "mysql://u:p@h:3306/db")
    assert out == fake
    m.assert_called_once_with("mysql://u:p@h:3306/db")


def test_get_schema_descriptions_sqlite_default_unchanged():
    from db.init_db import get_schema_descriptions
    out = get_schema_descriptions()
    assert len(out) == 5
    assert {t["table"] for t in out} == {"products", "customers", "orders",
                                         "order_items", "product_reviews"}


def test_get_schema_descriptions_mysql_fallback_on_error():
    from db import init_db
    with patch("db.init_db._mysql_schema_descriptions", side_effect=RuntimeError("conn refused")):
        out = init_db.get_schema_descriptions("mysql", "mysql://u:p@h:3306/db")
    assert len(out) == 5  # 回退演示 Schema，不崩溃


# ═══ 4. switch_database 接线 ═══

def test_switch_database_mysql_calls_set_backend():
    import db.connection_manager as cm
    url = "mysql://user:pass@localhost:3306/proddb"
    cm._connections["t_mysql"] = {
        "key": "t_mysql", "label": "测试MySQL", "db_type": "mysql",
        "path_or_url": url, "tables": [{"name": "t1", "columns": 2}],
        "status": "connected",
    }
    saved_current = cm._current_db
    try:
        with patch("db.connection_manager._reindex_current_schema") as rp:
            with patch("db.executor.ExecutorProxy.set_backend") as sb:
                ret = cm.switch_database("t_mysql")
        assert ret["ok"] is True
        # 切 MySQL 后 set_backend 收到 ("mysql", url)
        assert sb.call_args_list[0][0] == ("mysql", url)
        # 重新索引也拿到 db_type + url
        assert rp.call_args_list[0][0] == ("mysql", url)
    finally:
        del cm._connections["t_mysql"]
        cm._current_db = saved_current


def test_switch_database_postgresql_explicit_error():
    """PostgreSQL 未实现查询执行：明确报错而不是假装切换成功"""
    import db.connection_manager as cm
    cm._connections["t_pg"] = {
        "key": "t_pg", "label": "测试PG", "db_type": "postgresql",
        "path_or_url": "postgresql://u:p@localhost:5432/db",
        "tables": [], "status": "connected",
    }
    saved_current = cm._current_db
    try:
        with patch("db.connection_manager._reindex_current_schema"):
            ret = cm.switch_database("t_pg")
        assert ret.get("error")
        assert "PostgreSQL" in ret["error"]
    finally:
        del cm._connections["t_pg"]
        cm._current_db = saved_current
