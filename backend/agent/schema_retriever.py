"""
Schema检索器 — 把数据库表结构转成向量，按用户问题语义检索最相关的表和字段。
使用本地BGE模型，不需要网络和API Key。

向量库后端（通过 VECTOR_STORE 环境变量切换）:
  - chromadb（默认）: 本地持久化向量库，零基础设施
  - milvus: 分布式向量数据库，生产环境适用（需 pip install pymilvus + 运行中的 Milvus）

v2: 数据层抽象为 VectorStore 接口（Chroma/Milvus 可切换），Milvus 后端完整实现。
"""
import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Optional
from sentence_transformers import SentenceTransformer
from config import EMBEDDING_MODEL, CHROMA_PERSIST_DIR, VECTOR_STORE, MILVUS_HOST, MILVUS_PORT
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
    """Retrieve relevant database schema by semantic search"""

    def __init__(self):
        self.model = None  # 懒加载：只在走向量检索时才加载BGE模型
        # 集合名带上 embedding 模型标识：
        # 换模型（如 large→small）时自动用全新的空集合，避免"维度不匹配"报错。
        model_tag = re.sub(r"[^A-Za-z0-9]+", "_", EMBEDDING_MODEL).strip("_")
        self.collection_name = f"schema_embeddings_{model_tag}"
        # 数据层：按 VECTOR_STORE 配置选择 ChromaDB 或 Milvus
        self.store = get_vector_store(self.collection_name)
        # 兼容旧引用（main.py / reindex_schema.py 用 schema_retriever.collection.count()）
        self.collection = self.store

    def _ensure_model(self):
        """懒加载BGE模型（仅在需要向量检索时）"""
        if self.model is None:
            print(f"[SchemaRetriever] Loading embedding model: {EMBEDDING_MODEL} ...")
            self.model = SentenceTransformer(EMBEDDING_MODEL)
            print(f"[SchemaRetriever] Model loaded. dim={self.model.get_embedding_dimension()}")

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for texts using local model"""
        embeddings = self.model.encode(texts, normalize_embeddings=True)
        return embeddings.tolist()

    def index_schemas(self, schema_list: list[dict], force: bool = False):
        """将表/列Schema索引到向量库。force=True时强制重建索引。"""
        if self.store.count() > 0:
            if force:
                # 彻底重建：删除整个集合再新建。
                # 注意：只删文档不会重置维度——集合的 embedding 维度在创建时固化，
                # 换过 embedding 模型（如 large→small）后必须重建集合才能匹配新维度。
                self.store.recreate()
                print(f"[SchemaRetriever] 强制重建索引（已重建集合 {self.collection_name}）")
            else:
                print(f"[SchemaRetriever] Already indexed {self.store.count()} documents, skipping...")
                return

        documents = []
        metadatas = []
        ids = []

        for table_info in schema_list:
            # Index full table description
            doc_id = f"table:{table_info['table']}"
            doc_text = f"表名: {table_info['table']}\n说明: {table_info['description']}\nDDL: {table_info['ddl']}"
            documents.append(doc_text)
            metadatas.append({"type": "table", "table_name": table_info["table"]})
            ids.append(doc_id)

            # Index each column
            for col in table_info.get("columns", []):
                col_id = f"col:{table_info['table']}.{col['name']}"
                col_text = f"表名: {table_info['table']}\n字段名: {col['name']}\n类型: {col['type']}\n说明: {col['comment']}"
                documents.append(col_text)
                metadatas.append({
                    "type": "column",
                    "table_name": table_info["table"],
                    "column_name": col["name"],
                })
                ids.append(col_id)

            # Index sample queries
            for i, query in enumerate(table_info.get("sample_queries", [])):
                q_id = f"query:{table_info['table']}.{i}"
                q_text = f"表名: {table_info['table']}\n示例查询: {query}"
                documents.append(q_text)
                metadatas.append({"type": "sample_query", "table_name": table_info["table"]})
                ids.append(q_id)

        # Batch embed and insert（模型是懒加载，这里必须确保已加载）
        self._ensure_model()
        embeddings = self._embed(documents)
        self.store.add(
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
            ids=ids,
        )
        print(f"[SchemaRetriever] Indexed {len(documents)} schema documents")

    def retrieve(self, question: str, top_k_tables: int = 3, top_k_columns: int = 5) -> dict:
        """
        Schema检索 — 先关键词（1ms）→ 失败再混合检索（向量兜底）

        大部分查询用关键词就能命中（"销售额"→orders表），毫秒级。
        关键词检索无结果时（如同义词"营收"未命中），走向量兜底。
        """
        # 第一步：关键词快速检索
        result = self._fast_keyword_retrieve(question, top_k_tables, top_k_columns)

        # 关键词命中 → 直接返回（99%的场景）
        if result["tables"]:
            return result

        # 关键词无结果 → 向量混合检索兜底（如同义词"营收"="销售额"）
        logger.info(f"[Schema] 关键词无结果，降级到向量检索: '{question[:30]}...'")
        try:
            return self._hybrid_retrieve(question, top_k_tables, top_k_columns)
        except Exception as e:
            # 兜底再兜底：向量检索异常（如embedding维度与集合不一致）不致命，
            # 降级返回空，由上层给出"未找到相关表"的友好提示，而不是500。
            logger.warning(f"[Schema] 向量检索失败，降级返回空: {e}")
            return {"tables": [], "columns": []}

    def _fast_keyword_retrieve(self, question: str, top_k_tables: int, top_k_columns: int) -> dict:
        """
        快速关键词检索 — 不走向量，直接拿所有文档做关键词匹配
        适用场景: 表少（≤50个文档），向量检索的启动开销比匹配本身还大
        """
        import time
        t0 = time.time()
        all_data = self.store.get_all()  # 一次性拿所有文档

        # 对每个文档做关键词打分
        tokens = self._tokenize(question)
        scored = []
        for i, (doc, meta) in enumerate(zip(all_data["documents"], all_data["metadatas"])):
            if not doc:
                continue
            doc_lower = doc.lower()
            kw_hits = sum(1 for t in tokens if t in doc_lower)
            if kw_hits > 0:
                score = kw_hits / max(len(tokens), 1)
                scored.append((score, meta, doc))

        # 按分数排序
        scored.sort(key=lambda x: x[0], reverse=True)

        seen_tables = set()
        seen_columns = set()
        relevant_tables = []
        relevant_columns = []

        for score, meta, doc in scored:
            if meta["type"] == "table":
                if meta["table_name"] not in seen_tables:
                    seen_tables.add(meta["table_name"])
                    relevant_tables.append({"table": meta["table_name"], "doc": doc, "score": round(score, 3)})
            elif meta["type"] == "column":
                col_key = f"{meta['table_name']}.{meta['column_name']}"
                if col_key not in seen_columns:
                    seen_columns.add(col_key)
                    relevant_columns.append({"table": meta["table_name"], "column": meta["column_name"], "doc": doc, "score": round(score, 3)})

        elapsed = (time.time() - t0) * 1000
        logger.debug(f"[Schema] 快速关键词检索: {len(scored)}个匹配, {elapsed:.0f}ms")

        return {
            "tables": relevant_tables[:top_k_tables],
            "columns": relevant_columns[:top_k_columns],
        }

    def _hybrid_retrieve(self, question: str, top_k_tables: int, top_k_columns: int) -> dict:
        """向量+关键词混合检索（仅关键词失败时调用）"""
        self._ensure_model()  # 懒加载BGE模型
        query_embedding = self._embed([question])[0]
        n_candidates = max(top_k_tables * 5 + top_k_columns * 3, 30)
        results = self.store.query(query_embedding, n_results=n_candidates)
        kw_scores = self._compute_keyword_scores(question, results)

        seen_tables = set()
        seen_columns = set()
        relevant_tables = []
        relevant_columns = []

        for i, (doc, meta, distance) in enumerate(zip(
            results["documents"][0], results["metadatas"][0], results["distances"][0],
        )):
            vector_sim = 1.0 / (1.0 + distance)
            kw_score = kw_scores.get(i, 0)
            hybrid_score = 0.7 * vector_sim + 0.3 * kw_score

            if meta["type"] == "table":
                if meta["table_name"] not in seen_tables:
                    seen_tables.add(meta["table_name"])
                    relevant_tables.append({"table": meta["table_name"], "doc": doc, "score": round(hybrid_score, 3)})
            elif meta["type"] == "column":
                col_key = f"{meta['table_name']}.{meta['column_name']}"
                if col_key not in seen_columns:
                    seen_columns.add(col_key)
                    relevant_columns.append({"table": meta["table_name"], "column": meta["column_name"], "doc": doc, "score": round(hybrid_score, 3)})

        relevant_tables.sort(key=lambda x: x["score"], reverse=True)
        relevant_columns.sort(key=lambda x: x["score"], reverse=True)
        return {
            "tables": relevant_tables[:top_k_tables],
            "columns": relevant_columns[:top_k_columns],
        }

    @staticmethod
    def _tokenize(text: str) -> set:
        """中文分词：bigram + 单字"""
        tokens = set()
        for i in range(len(text) - 1):
            tokens.add(text[i:i+2])
        for ch in text:
            if '一' <= ch <= '鿿':
                tokens.add(ch)
        return tokens

    @staticmethod
    def _compute_keyword_scores(question: str, results: dict) -> dict:
        """
        计算关键词匹配分数。

        使用中文bigram切词（每连续2字为一个词）+ 单字兜底，
        统计query中的词在文档中出现的比例。
        返回: {结果序号: 关键词得分(0~1)}
        """
        # 中文bigram + 单字
        tokens = set()
        for i in range(len(question) - 1):
            tokens.add(question[i:i+2])  # bigram: "本月" "月销" "销售"...
        for ch in question:
            if '一' <= ch <= '鿿':  # 只取中文字
                tokens.add(ch)

        if not tokens:
            return {}

        scores = {}
        for i, doc in enumerate(results["documents"][0]):
            doc_lower = doc.lower()
            hits = sum(1 for t in tokens if t in doc_lower)
            scores[i] = min(hits / max(len(tokens), 1), 1.0)

        return scores


# Singleton
schema_retriever = SchemaRetriever()
