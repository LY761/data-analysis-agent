"""
数据分析Agent 全局配置
所有模块共用这一份配置，支持环境变量覆盖
"""
import os
from dotenv import load_dotenv

load_dotenv()

# HuggingFace镜像（国内加速下载模型）
HF_ENDPOINT = os.getenv("HF_ENDPOINT", "https://hf-mirror.com")
os.environ["HF_ENDPOINT"] = HF_ENDPOINT

# LLM配置（OpenAI兼容接口，默认用DeepSeek）
LLM_API_KEY = os.getenv("LLM_API_KEY", "sk-your-api-key-here")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

# NL回答增强：普通路径（非流式）也用LLM生成口语化回答（失败自动降级规则版）
NL_ANSWER_LLM = os.getenv("NL_ANSWER_LLM", "true").lower() == "true"
# 联网搜索：知识类问题（非数据库查询）先上网找资料再回答，失败自动降级纯LLM
WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "true").lower() == "true"

# 嵌入模型配置（本地运行，不需要网络和API Key）
# bge-small: 384维/130MB ~50ms编码 | bge-large: 1024维/1.3GB ~500ms编码
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")

# ChromaDB向量库持久化目录
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./chroma_db")

# 数据库路径（Demo用SQLite）
DEMO_DB_PATH = os.getenv("DEMO_DB_PATH", "./demo_sales.db")

# SQL执行限制
MAX_RESULT_ROWS = int(os.getenv("MAX_RESULT_ROWS", "1000"))    # 最大返回行数
QUERY_TIMEOUT_SEC = int(os.getenv("QUERY_TIMEOUT_SEC", "10"))   # 查询超时（秒）
MAX_RETRY_COUNT = int(os.getenv("MAX_RETRY_COUNT", "2"))        # SQL修正最大重试次数

# Redis缓存（可选 — 不配置则自动跳过，走无缓存模式）
REDIS_URL = os.getenv("REDIS_URL", "")
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))  # 缓存有效期（秒）

# Langfuse全链路追踪（可选 — 不配置则降级为本地日志）
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

# 数据库后端: "sqlite"（默认） | "mysql"（生产）
DB_TYPE = os.getenv("DB_TYPE", "sqlite")
MYSQL_URL = os.getenv("MYSQL_URL", "")

# 向量库后端: "chromadb"（默认） | "milvus"（生产）
VECTOR_STORE = os.getenv("VECTOR_STORE", "chromadb")
MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")

# 认证配置
JWT_SECRET = os.getenv("JWT_SECRET", "data-agent-secret-change-in-production")
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "24"))
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "false").lower() == "true"  # Demo默认关闭认证

# 搜索结果缓存 TTL（秒）
COMPETITOR_CACHE_TTL = int(os.getenv("COMPETITOR_CACHE_TTL", "3600"))
# 爬虫请求间隔（秒）
COMPETITOR_REQUEST_DELAY = float(os.getenv("COMPETITOR_REQUEST_DELAY", "2.0"))
# 我方公司名称（用于内部对比）
OUR_COMPANY_NAME = os.getenv("OUR_COMPANY_NAME", "泛翼时代")
# competitor-scraper 项目路径（竞品JSON/真实差评数据源，原硬编码 C:/Users/LY/...）
COMPETITOR_SCRAPER_PATH = os.getenv("COMPETITOR_SCRAPER_PATH", r"C:\Users\LY\competitor-scraper")

# 服务器配置
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
