"""
企业知识库 — 文档问答（RAG）

流程: 上传文档（txt/md/pdf/csv）→ 切块(500字/重叠100) → BGE 嵌入
      → ChromaDB(kb_docs) → 问答时检索 top-k → LLM 基于块回答（带文档名）

意图接入: knowledge 意图先查知识库，命中即答；未命中降级联网搜索/纯 LLM。
"""
import hashlib
import io
import logging
import re
from sentence_transformers import SentenceTransformer
import chromadb
from config import EMBEDDING_MODEL, CHROMA_PERSIST_DIR, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from chromadb.config import Settings

logger = logging.getLogger(__name__)

CHUNK_SIZE = 500
CHUNK_OVERLAP = 100
COLLECTION = "kb_docs"
ALLOWED_EXT = {".txt", ".md", ".pdf", ".csv"}
MAX_FILE_BYTES = 30 * 1024 * 1024  # 30MB


def _chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list:
    """按 500 字/重叠 100 切块（先聚合段落，超长段再切）。"""
    text = re.sub(r"\n{3,}", "\n\n", text or "")
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks = []
    current = ""
    for p in paras:
        if not current:
            current = p
        elif len(current) + len(p) + 2 <= size:
            current = current + "\n\n" + p
        else:
            chunks.append(current)
            current = p
    if current:
        chunks.append(current)
    # 单块超长再切
    out = []
    for c in chunks:
        if len(c) <= size:
            out.append(c)
        else:
            for i in range(0, len(c), size - overlap):
                out.append(c[i:i + size])
    return [c for c in out if c.strip()]


def _extract_text(filename: str, content: bytes) -> str:
    ext = "." + (filename.rsplit(".", 1)[-1].lower() if "." in filename else "")
    if ext == ".pdf":
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(content))
        pages = [(pg.extract_text() or "") for pg in reader.pages[:100]]
        return "\n\n".join(pages)
    if ext in (".txt", ".md", ".csv"):
        for enc in ("utf-8-sig", "utf-8", "gbk"):
            try:
                return content.decode(enc)
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="replace")
    raise ValueError(f"不支持格式: {ext or '未知'}（支持 txt/md/pdf/csv）")


class KnowledgeBase:
    """文档向量库：add / list / delete / search / answer"""

    def __init__(self):
        self.model = None
        self._client = chromadb.PersistentClient(
            path=CHROMA_PERSIST_DIR, settings=Settings(anonymized_telemetry=False))
        try:
            self._col = self._client.get_collection(COLLECTION)
        except Exception:
            self._col = self._client.create_collection(
                COLLECTION, metadata={"hnsw:space": "cosine"})

    def _ensure_model(self):
        if self.model is None:
            self.model = SentenceTransformer(EMBEDDING_MODEL)

    def add_document(self, filename: str, content: bytes) -> dict:
        if len(content) > MAX_FILE_BYTES:
            raise ValueError(f"文件超过 {MAX_FILE_BYTES // 1024 // 1024}MB 限制")
        text = _extract_text(filename, content)
        if not text.strip():
            raise ValueError("文档没有可提取的文本（可能是扫描件，需要 OCR）")
        self._ensure_model()
        chunks = _chunk_text(text)
        doc_id = hashlib.md5(filename.encode("utf-8")).hexdigest()[:8]
        ids = [f"{doc_id}:{i}" for i in range(len(chunks))]
        embeddings = self.model.encode(chunks, normalize_embeddings=True).tolist()
        metadatas = [{"doc": filename, "chunk": i} for i in range(len(chunks))]
        # 幂等：同名文档先删旧块再写入
        try:
            self._col.delete(where={"doc": filename})
        except Exception:
            pass
        self._col.add(ids=ids, embeddings=embeddings,
                      documents=chunks, metadatas=metadatas)
        logger.info(f"[KB] 文档 {filename} 入库 {len(chunks)} 块")
        return {"doc": filename, "chunks": len(chunks)}

    def list_documents(self) -> list:
        try:
            data = self._col.get(include=["metadatas"])
        except Exception:
            return []
        docs = {}
        for m in data.get("metadatas") or []:
            d = (m or {}).get("doc", "")
            if d:
                docs[d] = docs.get(d, 0) + 1
        return [{"doc": d, "chunks": c} for d, c in docs.items()]

    def delete_document(self, filename: str):
        self._col.delete(where={"doc": filename})

    def search(self, question: str, top_k: int = 4) -> list:
        self._ensure_model()
        q = self.model.encode([question], normalize_embeddings=True).tolist()[0]
        res = self._col.query(query_embeddings=[q], n_results=top_k)
        out = []
        for i, doc in enumerate(res["documents"][0]):
            meta = res["metadatas"][0][i] or {}
            out.append({"doc": meta.get("doc", ""), "text": doc,
                        "distance": round(res["distances"][0][i], 4)})
        return out

    def answer(self, question: str):
        """检索知识库并用 LLM 回答。返回 (回答 or None, 引用文档列表)。"""
        hits = self.search(question, top_k=4)
        if not hits:
            return None, []
        refs = list(dict.fromkeys(h["doc"] for h in hits))
        src = "\n".join(
            f"[{i + 1}]（文档：{h['doc']}）{h['text'][:400]}" for i, h in enumerate(hits))
        system = (
            "你是企业知识库助手。只根据以下文档片段用中文回答用户问题；"
            "片段中没有答案就说'知识库中没有相关信息'。引用时标注（文档名）。不要编造。"
        )
        user = f"问题：{question}\n\n知识库片段：\n{src}"
        try:
            from openai import OpenAI
            resp = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL).chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                temperature=0.2,
                max_tokens=500,
            )
            return resp.choices[0].message.content.strip(), refs
        except Exception as e:
            logger.warning(f"[KB] LLM 回答失败: {e}")
            return None, refs


# 全局单例
kb = KnowledgeBase()
