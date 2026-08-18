"""
Schema检索器 — 把数据库表结构转成向量，按用户问题语义检索最相关的表和字段。
使用本地BGE模型，不需要网络和API Key。

向量库后端（通过 VECTOR_STORE 环境变量切换）:
  - chromadb（默认）: 本地持久化向量库，零基础设施
  - milvus: 分布式向量数据库，生产环境适用（需 pip install pymilvus + 运行中的 Milvus）

v2: 数据层抽象为 VectorStore 接口（Chroma/Milvus 可切换），Milvus 后端完整实现。
"""
import json
import hashlib
import logging
import math
import re
from abc import ABC, abstractmethod
from typing import Optional
from config import (
    CHROMA_PERSIST_DIR,
    EMBEDDING_BACKEND,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    MILVUS_HOST,
    MILVUS_PORT,
    VECTOR_STORE,
)
from chromadb.config import Settings

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Vector Store Abstraction Layer（数据面：Chroma / Milvus 可切换）
# ═══════════════════════════════════════════════════════════════════

class VectorStore(ABC):
    """抽象向量库接口 — SchemaRetriever 只依赖此接口，与具体后端解耦。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """集合名"""

    @abstractmethod
    def add(self, embeddings, documents, metadatas, ids) -> None:
        """批量写入向量。"""

    @abstractmethod
    def query(self, query_embedding, n_results: int = 20) -> dict:
        """最近邻检索。返回 {documents: [[]], metadatas: [[]], distances: [[]]}"""

    @abstractmethod
    def count(self) -> int:
        """已索引文档数。"""

    @abstractmethod
    def get_all(self) -> dict:
        """拉取全部文档。返回 {documents: [...], metadatas: [...], ids: [...]}"""

    @abstractmethod
    def recreate(self) -> None:
        """删除并重建集合（强制重建 / embedding 模型维度变更时）。"""

    @abstractmethod
    def delete(self, ids=None) -> None:
        """按 id 删除，或清空全部。"""


class ChromaVectorStore(VectorStore):
    """ChromaDB 后端（默认，零基础设施）。"""

    def __init__(self, collection_name: str, persist_dir: str = None):
        import chromadb
        self._name = collection_name
        self.client = chromadb.PersistentClient(
            path=persist_dir or CHROMA_PERSIST_DIR,
            settings=Settings(anonymized_telemetry=False),
        )
        self._collection = self._get_or_create()

    @property
    def name(self) -> str:
        return self._name

    def _get_or_create(self):
        try:
            return self.client.get_collection(self._name)
        except Exception:
            return self.client.create_collection(
                name=self._name, metadata={"hnsw:space": "cosine"},
            )

    def add(self, embeddings, documents, metadatas, ids):
        self._collection.add(embeddings=embeddings, documents=documents,
                             metadatas=metadatas, ids=ids)

    def query(self, query_embedding, n_results=20):
        return self._collection.query(query_embeddings=[query_embedding], n_results=n_results)

    def count(self):
        return self._collection.count()

    def get_all(self):
        return self._collection.get()

    def recreate(self):
        self.client.delete_collection(self._name)
        self._collection = self._get_or_create()

    def delete(self, ids=None):
        if ids:
            self._collection.delete(ids=ids)
        else:
            self.recreate()


class MilvusVectorStore(VectorStore):
    """Milvus 后端（生产/分布式）— pymilvus 2.x 完整实现。

    首次 add() 时按 embedding 维度自动建集合（HNSW + COSINE 索引），
    query() 返回与 Chroma 一致的 {documents, metadatas, distances} 结构。
    """

    def __init__(self, collection_name: str, host: str = None, port: int = None):
        self._name = collection_name
        self.host = host or MILVUS_HOST
        self.port = int(port or MILVUS_PORT)
        self._available = False
        self._collection = None

        try:
            from pymilvus import connections, Collection, utility
            connections.connect(host=self.host, port=self.port)
            if utility.has_collection(collection_name):
                self._collection = Collection(collection_name)
                self._collection.load()
                self._available = True
                logger.info(f"[Milvus] Connected {self.host}:{self.port}, "
                            f"collection='{collection_name}' loaded.")
            else:
                logger.info(f"[Milvus] Connected {self.host}:{self.port}, "
                            f"collection will be auto-created on first index.")
                self._available = True
        except ImportError:
            logger.warning("[Milvus] pymilvus not installed — fallback ChromaDB. "
                           "Install: pip install pymilvus")
        except Exception as e:
            logger.warning(f"[Milvus] Connection failed ({e}) — fallback ChromaDB. "
                           f"Ensure Milvus is running at {self.host}:{self.port}")

    @property
    def name(self) -> str:
        return self._name

    @property
    def available(self) -> bool:
        return self._available

    def _ensure_collection(self, dim: int):
        """首次 add 时按 embedding 维度自动建集合 + HNSW/COSINE 索引"""
        from pymilvus import Collection, CollectionSchema, DataType, FieldSchema
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=128),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(name="document", dtype=DataType.VARCHAR, max_length=8192),
            FieldSchema(name="metadata", dtype=DataType.VARCHAR, max_length=4096),
        ]
        schema = CollectionSchema(fields, description="data-analysis-agent schema embeddings")
        self._collection = Collection(self._name, schema)
        self._collection.create_index(
            "embedding",
            {"index_type": "HNSW", "metric_type": "COSINE",
             "params": {"M": 16, "efConstruction": 200}},
        )
        self._collection.load()
        logger.info(f"[Milvus] Collection '{self._name}' created, dim={dim}")

    def add(self, embeddings, documents, metadatas, ids):
        if not self._available:
            raise RuntimeError("Milvus not available")
        if not documents:
            return
        if self._collection is None:
            self._ensure_collection(dim=len(embeddings[0]) if embeddings else 384)
        data = [
            list(ids),
            [list(v) for v in embeddings],
            list(documents),
            [json.dumps(m, ensure_ascii=False) for m in metadatas],
        ]
        self._collection.insert(data)
        self._collection.flush()

    def query(self, query_embedding, n_results=20):
        empty = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
        if not self._available or self._collection is None:
            return empty
        try:
            res = self._collection.search(
                data=[list(query_embedding)],
                anns_field="embedding",
                param={"metric_type": "COSINE", "params": {"nprobe": 10}},
                limit=n_results,
                output_fields=["document", "metadata"],
            )
            documents, metadatas, distances = [], [], []
            for hits in res:
                ds, ms, dists = [], [], []
                for h in hits:
                    ds.append(h.entity.get("document"))
                    try:
                        ms.append(json.loads(h.entity.get("metadata") or "{}"))
                    except Exception:
                        ms.append({})
                    # Milvus COSINE score 越接近 1 越相似；转成"距离"语义与 Chroma 对齐
                    dists.append(round(1.0 - h.score, 6))
                documents.append(ds)
                metadatas.append(ms)
                distances.append(dists)
            return {"documents": documents, "metadatas": metadatas, "distances": distances}
        except Exception as e:
            logger.warning(f"[Milvus] query failed: {e}")
            return empty

    def count(self):
        if not self._available or self._collection is None:
            return 0
        return self._collection.num_entities

    def get_all(self):
        empty = {"documents": [], "metadatas": [], "ids": []}
        if not self._available or self._collection is None:
            return empty
        try:
            offset, batch = 0, 1024
            docs, metas, ids = [], [], []
            while True:
                res = self._collection.query(
                    expr="id != ''", offset=offset, limit=batch,
                    output_fields=["id", "document", "metadata"],
                )
                if not res:
                    break
                for r in res:
                    docs.append(r.get("document"))
                    try:
                        metas.append(json.loads(r.get("metadata") or "{}"))
                    except Exception:
                        metas.append({})
                    ids.append(r.get("id"))
                if len(res) < batch:
                    break
                offset += batch
            return {"documents": docs, "metadatas": metas, "ids": ids}
        except Exception as e:
            logger.warning(f"[Milvus] get_all failed: {e}")
            return empty

    def recreate(self):
        from pymilvus import utility
        try:
            if utility.has_collection(self._name):
                utility.drop_collection(self._name)
        except Exception as e:
            logger.warning(f"[Milvus] recreate failed: {e}")
        self._collection = None

    def delete(self, ids=None):
        if not self._available or self._collection is None:
            return
        if ids:
            expr = "id in " + str(list(ids))
            self._collection.delete(expr=expr)
        else:
            self.recreate()


def get_vector_store(collection_name: str = "schema_embeddings") -> VectorStore:
    """工厂：按 VECTOR_STORE 配置返回对应后端；Milvus 不可用时回退 ChromaDB。"""
    if VECTOR_STORE == "milvus":
        store = MilvusVectorStore(collection_name)
        if store.available:
            return store
        logger.warning(f"[VectorStore] Milvus requested but unavailable, falling back to ChromaDB.")
    return ChromaVectorStore(collection_name)


# ═══════════════════════════════════════════════════════════════════
# SchemaRetriever — 检索逻辑与后端解耦
# ═══════════════════════════════════════════════════════════════════

class SchemaRetriever:
    """按置信度执行精确关键词快路径和关键词/向量混合召回。"""

    RETRIEVAL_VERSION = "schema-hybrid-v1"
    LOW_CONFIDENCE_THRESHOLD = 0.30

    def __init__(self):
        self.model = None
        self.schema_catalog: dict[str, dict] = {}
        model_identity = EMBEDDING_MODEL if EMBEDDING_BACKEND == "sentence_transformer" else f"hashing-{EMBEDDING_DIMENSION}"
        model_tag = re.sub(r"[^A-Za-z0-9]+", "_", model_identity).strip("_")
        self.collection_name = f"schema_embeddings_{model_tag}"
        self.store = get_vector_store(self.collection_name)
        self.collection = self.store

    def set_schema_catalog(self, schema_list: list[dict]) -> None:
        self.schema_catalog = {item["table"]: item for item in schema_list}

    def _ensure_model(self):
        if EMBEDDING_BACKEND == "sentence_transformer" and self.model is None:
            from sentence_transformers import SentenceTransformer

            logger.info("[SchemaRetriever] loading embedding model: %s", EMBEDDING_MODEL)
            self.model = SentenceTransformer(EMBEDDING_MODEL)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if EMBEDDING_BACKEND == "hashing":
            return [self._hash_embed(text) for text in texts]
        self._ensure_model()
        embeddings = self.model.encode(texts, normalize_embeddings=True)
        return embeddings.tolist()

    @staticmethod
    def _hash_embed(text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIMENSION
        tokens = re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+", text.lower())
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSION
            vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def index_schemas(self, schema_list: list[dict], force: bool = False):
        self.set_schema_catalog(schema_list)
        if self.store.count() > 0:
            if force:
                self.store.recreate()
                logger.info("[SchemaRetriever] rebuilt collection %s", self.collection_name)
            else:
                logger.info("[SchemaRetriever] index exists; skip rebuild")
                return

        documents = []
        metadatas = []
        ids = []
        for table_info in schema_list:
            table_name = table_info["table"]
            documents.append(
                f"表名: {table_name}\n说明: {table_info.get('description', '')}\n"
                f"DDL: {table_info.get('ddl', '')}"
            )
            metadatas.append({"type": "table", "table_name": table_name})
            ids.append(f"table:{table_name}")

            for column in table_info.get("columns", []):
                documents.append(
                    f"表名: {table_name}\n字段名: {column['name']}\n"
                    f"类型: {column.get('type', '')}\n说明: {column.get('comment', '')}"
                )
                metadatas.append({
                    "type": "column",
                    "table_name": table_name,
                    "column_name": column["name"],
                })
                ids.append(f"col:{table_name}.{column['name']}")

            for index, query in enumerate(table_info.get("sample_queries", [])):
                documents.append(f"表名: {table_name}\n示例查询: {query}")
                metadatas.append({"type": "sample_query", "table_name": table_name})
                ids.append(f"query:{table_name}.{index}")

        if not documents:
            return
        self.store.add(
            embeddings=self._embed(documents),
            documents=documents,
            metadatas=metadatas,
            ids=ids,
        )
        logger.info("[SchemaRetriever] indexed %s schema documents", len(documents))

    def retrieve(self, question: str, top_k_tables: int = 3, top_k_columns: int = 8) -> dict:
        """精确命中直接返回；其余融合关键词和向量结果；向量失败时关键词兜底。"""
        keyword_result = self._fast_keyword_retrieve(question, top_k_tables * 2, top_k_columns * 2)
        if self._is_exact_fast_path(question, keyword_result):
            return self._enrich_result(
                question,
                keyword_result,
                strategy="exact_keyword",
                vector_available=False,
            )

        vector_available = True
        try:
            vector_result = self._vector_retrieve(question, top_k_tables * 2, top_k_columns * 2)
        except Exception as error:
            logger.warning("[Schema] vector retrieval unavailable, keyword fallback: %s", error)
            vector_available = False
            vector_result = {"tables": [], "columns": []}

        merged = self._fuse_results(keyword_result, vector_result, top_k_tables, top_k_columns)
        strategy = "hybrid" if vector_available else "keyword_fallback"
        return self._enrich_result(question, merged, strategy, vector_available)

    def _fast_keyword_retrieve(self, question: str, top_k_tables: int, top_k_columns: int) -> dict:
        all_data = self.store.get_all()
        tokens = self._tokenize(question)
        scored = []
        for document, metadata in zip(all_data.get("documents", []), all_data.get("metadatas", [])):
            if not document or not metadata:
                continue
            document_lower = document.lower()
            hits = sum(1 for token in tokens if token in document_lower)
            if hits:
                score = hits / max(len(tokens), 1)
                scored.append((score, metadata, document))
        return self._collect_ranked(scored, top_k_tables, top_k_columns)

    def _vector_retrieve(self, question: str, top_k_tables: int, top_k_columns: int) -> dict:
        query_embedding = self._embed([question])[0]
        candidate_count = max(top_k_tables * 5 + top_k_columns * 3, 30)
        results = self.store.query(query_embedding, n_results=candidate_count)
        scored = []
        documents = (results.get("documents") or [[]])[0]
        metadatas = (results.get("metadatas") or [[]])[0]
        distances = (results.get("distances") or [[]])[0]
        for document, metadata, distance in zip(documents, metadatas, distances):
            similarity = max(0.0, min(1.0, 1.0 - float(distance)))
            scored.append((similarity, metadata, document))
        return self._collect_ranked(scored, top_k_tables, top_k_columns)

    @staticmethod
    def _collect_ranked(scored, top_k_tables: int, top_k_columns: int) -> dict:
        scored.sort(key=lambda item: item[0], reverse=True)
        table_scores: dict[str, dict] = {}
        column_scores: dict[str, dict] = {}
        for score, metadata, document in scored:
            table_name = metadata.get("table_name", "")
            if not table_name:
                continue
            if metadata.get("type") in {"table", "sample_query"}:
                current = table_scores.get(table_name)
                if not current or score > current["score"]:
                    table_scores[table_name] = {
                        "table": table_name,
                        "doc": document,
                        "score": round(float(score), 4),
                    }
            elif metadata.get("type") == "column":
                column_name = metadata.get("column_name", "")
                key = f"{table_name}.{column_name}"
                current = column_scores.get(key)
                if not current or score > current["score"]:
                    column_scores[key] = {
                        "table": table_name,
                        "column": column_name,
                        "doc": document,
                        "score": round(float(score), 4),
                    }
        tables = sorted(table_scores.values(), key=lambda item: item["score"], reverse=True)
        columns = sorted(column_scores.values(), key=lambda item: item["score"], reverse=True)
        return {"tables": tables[:top_k_tables], "columns": columns[:top_k_columns]}

    @staticmethod
    def _fuse_results(keyword_result: dict, vector_result: dict, top_k_tables: int, top_k_columns: int) -> dict:
        def fuse(items_by_source: list[tuple[list[dict], float]], key_builder) -> list[dict]:
            merged: dict[str, dict] = {}
            for items, weight in items_by_source:
                for rank, item in enumerate(items, start=1):
                    key = key_builder(item)
                    entry = merged.setdefault(key, {**item, "score": 0.0, "sources": []})
                    entry["score"] += weight * float(item.get("score", 0))
                    entry["sources"].append("keyword" if weight == 0.45 else "vector")
            for entry in merged.values():
                if len(set(entry["sources"])) > 1:
                    entry["score"] += 0.05
                entry["score"] = round(min(entry["score"], 1.0), 4)
                entry["sources"] = sorted(set(entry["sources"]))
            return sorted(merged.values(), key=lambda item: item["score"], reverse=True)

        tables = fuse(
            [(keyword_result.get("tables", []), 0.45), (vector_result.get("tables", []), 0.55)],
            lambda item: item["table"],
        )
        columns = fuse(
            [(keyword_result.get("columns", []), 0.45), (vector_result.get("columns", []), 0.55)],
            lambda item: f"{item['table']}.{item['column']}",
        )
        return {"tables": tables[:top_k_tables], "columns": columns[:top_k_columns]}

    def _is_exact_fast_path(self, question: str, result: dict) -> bool:
        tables = result.get("tables", [])
        if not tables:
            return False
        normalized = question.lower()
        explicit_identifier = any(
            table_name.lower() in normalized
            for table_name in self.schema_catalog
        )
        top_score = tables[0].get("score", 0)
        second_score = tables[1].get("score", 0) if len(tables) > 1 else 0
        return explicit_identifier or (top_score >= 0.7 and top_score - second_score >= 0.2)

    def _enrich_result(self, question: str, result: dict, strategy: str, vector_available: bool) -> dict:
        selected_tables = [item["table"] for item in result.get("tables", [])]
        relationships = self._relationships()
        selected_tables, join_paths = self._expand_join_paths(selected_tables, relationships)

        existing = {item["table"] for item in result.get("tables", [])}
        tables = list(result.get("tables", []))
        for table_name in selected_tables:
            if table_name not in existing and table_name in self.schema_catalog:
                info = self.schema_catalog[table_name]
                tables.append({
                    "table": table_name,
                    "doc": info.get("description", ""),
                    "score": 0.15,
                    "sources": ["join_graph"],
                })

        selected_set = set(selected_tables)
        selected_relationships = [
            relation
            for relation in relationships
            if relation["from_table"] in selected_set and relation["to_table"] in selected_set
        ]
        columns = list(result.get("columns", []))
        column_keys = {f"{item['table']}.{item['column']}" for item in columns}
        for relation in selected_relationships:
            for table_name, column_name in (
                (relation["from_table"], relation["from_column"]),
                (relation["to_table"], relation["to_column"]),
            ):
                key = f"{table_name}.{column_name}"
                if key not in column_keys:
                    columns.append({
                        "table": table_name,
                        "column": column_name,
                        "doc": "关系字段",
                        "score": 0.15,
                        "sources": ["join_graph"],
                    })
                    column_keys.add(key)

        metrics = self._matched_metrics(question)
        dimensions = self._dimensions(selected_tables)
        samples = [
            {"table": table_name, "query": query}
            for table_name in selected_tables
            for query in self.schema_catalog.get(table_name, {}).get("sample_queries", [])[:2]
        ]
        confidence = round(max((item.get("score", 0) for item in tables), default=0), 4)
        return {
            "tables": tables,
            "columns": columns,
            "relationships": selected_relationships,
            "metrics": metrics,
            "dimensions": dimensions,
            "join_paths": join_paths,
            "sample_queries": samples,
            "evidence": [
                {"table": item["table"], "score": item.get("score", 0), "sources": item.get("sources", [strategy])}
                for item in tables
            ],
            "retrieval": {
                "strategy": strategy,
                "confidence": confidence,
                "low_confidence": confidence < self.LOW_CONFIDENCE_THRESHOLD,
                "vector_available": vector_available,
                "version": self.RETRIEVAL_VERSION,
            },
        }

    def _relationships(self) -> list[dict]:
        relationships = []
        pattern = re.compile(
            r"FOREIGN\s+KEY\s*\((\w+)\)\s+REFERENCES\s+(\w+)\s*\((\w+)\)",
            re.IGNORECASE,
        )
        for table_name, table_info in self.schema_catalog.items():
            for from_column, to_table, to_column in pattern.findall(table_info.get("ddl", "")):
                relationships.append({
                    "from_table": table_name,
                    "from_column": from_column,
                    "to_table": to_table,
                    "to_column": to_column,
                    "join_type": "many_to_one",
                })
        return relationships

    @staticmethod
    def _expand_join_paths(selected_tables: list[str], relationships: list[dict]) -> tuple[list[str], list[list[str]]]:
        selected = list(dict.fromkeys(selected_tables))
        selected_set = set(selected)
        adjacency: dict[str, set[str]] = {}
        for relation in relationships:
            left, right = relation["from_table"], relation["to_table"]
            adjacency.setdefault(left, set()).add(right)
            adjacency.setdefault(right, set()).add(left)

        join_paths = []
        original = list(selected)
        for index, source in enumerate(original):
            for target in original[index + 1:]:
                if target in adjacency.get(source, set()):
                    join_paths.append([source, target])
                    continue
                bridges = adjacency.get(source, set()) & adjacency.get(target, set())
                if bridges:
                    bridge = sorted(bridges)[0]
                    join_paths.append([source, bridge, target])
                    if bridge not in selected_set:
                        selected.append(bridge)
                        selected_set.add(bridge)
        return selected, join_paths

    def _matched_metrics(self, question: str) -> list[dict]:
        from domain.metric_registry import metric_registry

        normalized = question.lower()
        aliases = {
            "销售额": "gmv",
            "营收": "gmv",
            "成交额": "gmv",
            "客单价": "average_order_value",
            "转化率": "conversion_rate_pct",
            "退款率": "refund_rate_pct",
            "毛利率": "gross_margin_pct",
            "动销率": "sell_through_rate_pct",
        }
        matched_keys = {key for alias, key in aliases.items() if alias in normalized}
        for definition in metric_registry.list():
            if definition.name.lower() in normalized or definition.metric_key.lower() in normalized:
                matched_keys.add(definition.metric_key)
        return [metric_registry.get(key).to_public_dict() for key in sorted(matched_keys)]

    def _dimensions(self, selected_tables: list[str]) -> list[dict]:
        dimensions = []
        for table_name in selected_tables:
            for column in self.schema_catalog.get(table_name, {}).get("columns", []):
                column_type = column.get("type", "").upper()
                if any(marker in column_type for marker in ("TEXT", "DATE", "TIME")):
                    dimensions.append({
                        "table": table_name,
                        "column": column["name"],
                        "type": column.get("type", ""),
                        "description": column.get("comment", ""),
                    })
        return dimensions

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        normalized = text.lower().strip()
        tokens = set(re.findall(r"[a-z0-9_]+", normalized))
        chinese = "".join(character for character in normalized if "一" <= character <= "鿿")
        for size in (2, 3):
            for index in range(max(0, len(chinese) - size + 1)):
                tokens.add(chinese[index:index + size])
        if len(chinese) == 1:
            tokens.add(chinese)
        return {token for token in tokens if token}

    @staticmethod
    def _compute_keyword_scores(question: str, results: dict) -> dict:
        tokens = SchemaRetriever._tokenize(question)
        scores = {}
        for index, document in enumerate((results.get("documents") or [[]])[0]):
            document_lower = (document or "").lower()
            hits = sum(1 for token in tokens if token in document_lower)
            scores[index] = hits / max(len(tokens), 1)
        return scores


# Singleton
schema_retriever = SchemaRetriever()
