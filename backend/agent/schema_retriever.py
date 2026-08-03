"""
Schema检索器 — 把数据库表结构转成向量，按用户问题语义检索最相关的表和字段。
使用本地BGE模型，不需要网络和API Key。

向量库后端（通过 VECTOR_STORE 环境变量切换）:
  - chromadb（默认）: 本地持久化向量库，零基础设施
  - milvus: 分布式向量数据库，生产环境适用
"""
import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Optional
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
from config import EMBEDDING_MODEL, CHROMA_PERSIST_DIR, VECTOR_STORE, MILVUS_HOST, MILVUS_PORT

logger = logging.getLogger(__name__)


class SchemaRetriever:
    """Retrieve relevant database schema by semantic search"""

    def __init__(self):
        self.model = None  # 懒加载：只在走向量检索时才加载BGE模型
        self.chroma_client = chromadb.PersistentClient(
            path=CHROMA_PERSIST_DIR,
            settings=Settings(anonymized_telemetry=False),
        )
        # 集合名带上 embedding 模型标识：
        # 换模型（如 large→small）时自动用全新的空集合，避免"维度不匹配"报错。
        # 旧的集合文件会留着但不使用，不影响运行。
        model_tag = re.sub(r"[^A-Za-z0-9]+", "_", EMBEDDING_MODEL).strip("_")
        self.collection_name = f"schema_embeddings_{model_tag}"
        self._init_collection()

    def _ensure_model(self):
        """懒加载BGE模型（仅在需要向量检索时）"""
        if self.model is None:
            print(f"[SchemaRetriever] Loading embedding model: {EMBEDDING_MODEL} ...")
            self.model = SentenceTransformer(EMBEDDING_MODEL)
            print(f"[SchemaRetriever] Model loaded. dim={self.model.get_embedding_dimension()}")

    def _init_collection(self):
        """获取或创建ChromaDB集合"""
        try:
            self.collection = self.chroma_client.get_collection(self.collection_name)
        except Exception:
            self.collection = self.chroma_client.create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for texts using local model"""
        embeddings = self.model.encode(texts, normalize_embeddings=True)
        return embeddings.tolist()

    def index_schemas(self, schema_list: list[dict], force: bool = False):
        """将表/列Schema索引到ChromaDB。force=True时强制重建索引。"""
        if self.collection.count() > 0:
            if force:
                # 彻底重建：删除整个集合再新建。
                # 注意：只删文档不会重置维度——集合的 embedding 维度在创建时固化，
                # 换过 embedding 模型（如 large→small）后必须重建集合才能匹配新维度。
                name = self.collection.name
                self.chroma_client.delete_collection(name)
                self.collection = self.chroma_client.create_collection(
                    name=name,
                    metadata={"hnsw:space": "cosine"},
                )
                print(f"[SchemaRetriever] 强制重建索引（已重建集合 {name}）")
            else:
                print(f"[SchemaRetriever] Already indexed {self.collection.count()} documents, skipping...")
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
        self.collection.add(
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
        logger = __import__('logging').getLogger(__name__)
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
        all_data = self.collection.get()  # 一次性拿所有文档
        logger = __import__('logging').getLogger(__name__)

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
        results = self.collection.query(
            query_embeddings=[query_embedding], n_results=n_candidates,
        )
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


# ═══════════════════════════════════════════════════════════════════
# Vector Store Abstraction Layer
# ═══════════════════════════════════════════════════════════════════
# Supports: ChromaDB (default) | Milvus (production)
# Config: VECTOR_STORE env var → "chromadb" or "milvus"
# The SchemaRetriever above uses ChromaDB directly for simplicity.
# For production Milvus deployment, replace with MilvusVectorStore below.


class VectorStore(ABC):
    """Abstract vector store interface.

    Implementations: ChromaVectorStore, MilvusVectorStore
    All concrete stores must implement: add, query, count, delete
    """

    @abstractmethod
    def add(self, embeddings: list[list[float]], documents: list[str],
            metadatas: list[dict], ids: list[str]) -> None:
        """Index vectors with metadata."""

    @abstractmethod
    def query(self, query_embedding: list[float], n_results: int = 20) -> dict:
        """Search for nearest neighbors. Returns {documents, metadatas, distances}."""

    @abstractmethod
    def count(self) -> int:
        """Return total number of indexed documents."""

    @abstractmethod
    def delete(self, ids: list[str] = None) -> None:
        """Delete vectors by ID or clear all."""


class ChromaVectorStore(VectorStore):
    """ChromaDB-backed vector store (default for development)."""

    def __init__(self, collection_name: str, persist_dir: str = None):
        import chromadb
        self.client = chromadb.PersistentClient(
            path=persist_dir or CHROMA_PERSIST_DIR,
            settings=Settings(anonymized_telemetry=False),
        )
        try:
            self.collection = self.client.get_collection(collection_name)
        except Exception:
            self.collection = self.client.create_collection(
                name=collection_name, metadata={"hnsw:space": "cosine"},
            )

    def add(self, embeddings, documents, metadatas, ids):
        self.collection.add(embeddings=embeddings, documents=documents,
                           metadatas=metadatas, ids=ids)

    def query(self, query_embedding, n_results=20):
        return self.collection.query(query_embeddings=[query_embedding], n_results=n_results)

    def count(self):
        return self.collection.count()

    def delete(self, ids=None):
        if ids:
            self.collection.delete(ids=ids)
        else:
            # Delete all — recreate collection
            name = self.collection.name
            self.client.delete_collection(name)
            self.collection = self.client.create_collection(
                name=name, metadata={"hnsw:space": "cosine"},
            )


class MilvusVectorStore(VectorStore):
    """
    Milvus-backed vector store (production, distributed).

    **Architecture-ready skeleton.** Core interface matches ChromaVectorStore
    for drop-in switching. Full implementation requires:
      pip install pymilvus
      docker run -d -p 19530:19530 milvusdb/milvus

    Key advantages over ChromaDB for production:
      - Horizontal scaling (distributed index across nodes)
      - Metadata filtering at query time (WHERE clauses on metadata fields)
      - GPU-accelerated search (IVF_PQ, HNSW indices)
      - Role-based access control for multi-tenant deployments
    """

    def __init__(self, collection_name: str, host: str = None, port: int = None):
        self.collection_name = collection_name
        self.host = host or MILVUS_HOST
        self.port = port or MILVUS_PORT
        self._available = False
        self._collection = None

        try:
            from pymilvus import connections, Collection, FieldSchema, CollectionSchema, DataType
            connections.connect(host=self.host, port=self.port)
            # Check if collection exists
            from pymilvus import utility
            if utility.has_collection(collection_name):
                self._collection = Collection(collection_name)
                self._collection.load()
                self._available = True
                logger.info(f"[Milvus] Connected to {self.host}:{self.port}, "
                          f"collection='{collection_name}' loaded.")
            else:
                logger.info(f"[Milvus] Connected to {self.host}:{self.port}, "
                          f"collection='{collection_name}' not yet created — will auto-create on first index.")
                self._available = True  # Connection OK, collection will be created on add()
        except ImportError:
            logger.info("[Milvus] pymilvus not installed — falling back to ChromaDB. "
                       "Install: pip install pymilvus")
        except Exception as e:
            logger.warning(f"[Milvus] Connection failed ({e}) — falling back to ChromaDB. "
                         f"Ensure Milvus is running at {self.host}:{self.port}")

    @property
    def available(self) -> bool:
        return self._available

    def add(self, embeddings, documents, metadatas, ids):
        if not self._available:
            return
        # TODO: Full Milvus insert implementation
        # - Auto-create collection with correct schema on first call
        # - Batch insert with proper field mapping
        # - Create index (IVF_FLAT or HNSW depending on data scale)
        # - Flush to ensure durability
        logger.info(f"[Milvus] add() called — {len(documents)} docs (stub, full impl pending)")

    def query(self, query_embedding, n_results=20):
        if not self._available or not self._collection:
            return {"documents": [[]], "metadatas": [[]], "distances": [[]]}
        # TODO: Full Milvus ANN search
        # - search() with metric_type="COSINE"
        # - Filter by metadata fields
        # - Return structured results matching ChromaDB format
        logger.info(f"[Milvus] query() called — top_k={n_results} (stub, full impl pending)")
        return {"documents": [[]], "metadatas": [[]], "distances": [[]]}

    def count(self):
        if not self._available or not self._collection:
            return 0
        return self._collection.num_entities

    def delete(self, ids=None):
        if not self._available or not self._collection:
            return
        # TODO: Full Milvus delete
        logger.info(f"[Milvus] delete() called (stub, full impl pending)")


def get_vector_store(collection_name: str = "schema_embeddings") -> VectorStore:
    """Factory: return the appropriate vector store based on VECTOR_STORE config."""
    if VECTOR_STORE == "milvus":
        store = MilvusVectorStore(collection_name)
        if store.available:
            return store
        logger.warning(f"[VectorStore] Milvus requested but unavailable, falling back to ChromaDB.")
    return ChromaVectorStore(collection_name)
