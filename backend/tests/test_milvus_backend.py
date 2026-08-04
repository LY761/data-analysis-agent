# -*- coding: utf-8 -*-
"""A4: Milvus 向量库后端回归测试 — mock pymilvus 验证 add/query/count/get_all/delete/工厂"""
import sys
import types
from unittest.mock import MagicMock, patch

from agent.schema_retriever import get_vector_store, MilvusVectorStore, ChromaVectorStore


class FakeHit:
    def __init__(self, doc="doc", meta="{}", score=0.8):
        self.entity = {"document": doc, "metadata": meta}
        self.score = score


class FakeCollection:
    """模拟 pymilvus Collection：类级 query_pages 控制 get_all 分页"""
    query_pages = []

    def __init__(self, *args, **kwargs):
        self.num_entities = 0
        self.insert_calls = []
        self.delete_calls = []

    def load(self):
        pass

    def insert(self, data):
        self.insert_calls.append(data)
        self.num_entities += len(data[0])

    def flush(self):
        pass

    def create_index(self, *a, **kw):
        pass

    def delete(self, expr=None):
        self.delete_calls.append(expr)

    def search(self, **kw):
        return [[
            FakeHit(doc="表名: products",
                    meta='{"type": "table", "table_name": "products"}', score=0.9),
            FakeHit(doc="表名: orders",
                    meta='{"type": "table", "table_name": "orders"}', score=0.6),
        ]]

    def query(self, **kw):
        if FakeCollection.query_pages:
            return FakeCollection.query_pages.pop(0)
        return []


def _fake_pymilvus_module(has_collection: bool):
    pm = types.ModuleType("pymilvus")
    pm.connections = MagicMock()
    pm.utility = MagicMock()
    pm.utility.has_collection.return_value = has_collection
    pm.utility.drop_collection = MagicMock()
    pm.Collection = FakeCollection  # 类 → 每次 Collection(name, schema) 创建新实例
    pm.FieldSchema = MagicMock()
    pm.CollectionSchema = MagicMock()
    pm.DataType = MagicMock()
    return pm


def _patch_pymilvus(pm):
    return patch.dict(sys.modules, {
        "pymilvus": pm,
        "pymilvus.connections": pm.connections,
        "pymilvus.utility": pm.utility,
    })


def test_milvus_store_add_and_query():
    """首次 add 自动建集合；query 返回 Chroma 兼容结构（score→distance）"""
    FakeCollection.query_pages = []
    pm = _fake_pymilvus_module(has_collection=False)
    with _patch_pymilvus(pm):
        store = MilvusVectorStore("schema_test")
        assert store.available is True
        store.add([[0.1] * 8], ["表名: products"],
                  [{"type": "table", "table_name": "products"}], ["table:products"])
        assert store.count() == 1
        assert len(store._collection.insert_calls) == 1

        res = store.query([0.1] * 8, n_results=5)
        assert set(res.keys()) == {"documents", "metadatas", "distances"}
        assert res["documents"][0][0] == "表名: products"
        assert res["metadatas"][0][0]["table_name"] == "products"
        # Milvus score 0.9 → 距离 0.1（与 Chroma 距离语义对齐）
        assert res["distances"][0][0] == 0.1


def test_milvus_store_get_all_and_delete():
    """get_all 分页拉全量；delete(ids) 与 delete()（drop collection）"""
    FakeCollection.query_pages = [
        [{"id": "table:products", "document": "表名: products",
          "metadata": '{"type": "table", "table_name": "products"}'}],
        [],  # 第二页空 → 停止分页
    ]
    pm = _fake_pymilvus_module(has_collection=True)
    with _patch_pymilvus(pm):
        store = MilvusVectorStore("schema_test")
        assert store._collection is not None  # 已存在集合 → 直接加载
        all_data = store.get_all()
        assert all_data["ids"] == ["table:products"]
        assert all_data["documents"] == ["表名: products"]
        assert all_data["metadatas"][0]["table_name"] == "products"

        store.delete(ids=["table:products"])  # 按 id 删除，不崩溃
        assert store._collection.delete_calls == ["id in ['table:products']"]
        store.delete()  # 清空 → drop_collection
        pm.utility.drop_collection.assert_called_once_with("schema_test")


def test_milvus_store_unavailable_without_pymilvus():
    """模拟 pymilvus 不可用：available=False，query/get_all 返回空结构，不崩溃"""
    with patch.dict(sys.modules, {"pymilvus": None, "pymilvus.connections": None,
                                  "pymilvus.utility": None}):
        store = MilvusVectorStore("schema_test")
    assert store.available is False
    assert store.count() == 0
    assert store.query([0.1] * 8)["documents"] == [[]]
    assert store.get_all()["ids"] == []


def test_vector_store_factory_default_chroma():
    store = get_vector_store("test_col")
    assert isinstance(store, ChromaVectorStore)


def test_vector_store_factory_milvus_available():
    """VECTOR_STORE=milvus 且 Milvus 可用 → 返回 MilvusVectorStore"""
    FakeCollection.query_pages = []
    pm = _fake_pymilvus_module(has_collection=False)
    with patch("agent.schema_retriever.VECTOR_STORE", "milvus"):
        with _patch_pymilvus(pm):
            store = get_vector_store("schema_test")
    assert isinstance(store, MilvusVectorStore)


def test_vector_store_factory_milvus_unavailable_fallback_chroma():
    """VECTOR_STORE=milvus 但 Milvus 不可用 → 明确回退 ChromaDB"""
    with patch("agent.schema_retriever.VECTOR_STORE", "milvus"):
        with patch.dict(sys.modules, {"pymilvus": None, "pymilvus.connections": None,
                                      "pymilvus.utility": None}):
            store = get_vector_store("test_col")
    assert isinstance(store, ChromaVectorStore)
