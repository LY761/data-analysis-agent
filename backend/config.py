"""
数据分析Agent 全局配置
所有模块共用这一份配置，支持环境变量覆盖
"""
import os
from dotenv import load_dotenv

load_dotenv()

# 运行环境：demo 保持本地开箱即用，production 启用严格配置校验
APP_ENV = os.getenv("APP_ENV", "demo").lower()

# HuggingFace镜像（国内加速下载模型）
HF_ENDPOINT = os.getenv("HF_ENDPOINT", "https://hf-mirror.com")
os.environ["HF_ENDPOINT"] = HF_ENDPOINT

# LLM配置（OpenAI兼容接口，默认用DeepSeek）
LLM_API_KEY = os.getenv("LLM_API_KEY", "sk-your-api-key-here")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")

# NL回答增强：普通路径（非流式）也用LLM生成口语化回答（失败自动降级规则版）
NL_ANSWER_LLM = os.getenv("NL_ANSWER_LLM", "false").lower() == "true"

# 嵌入模型配置（本地运行，不需要网络和API Key）
# bge-small: 384维/130MB ~50ms编码 | bge-large: 1024维/1.3GB ~500ms编码
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
EMBEDDING_BACKEND = os.getenv("EMBEDDING_BACKEND", "sentence_transformer").lower()
EMBEDDING_DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "384"))

# ChromaDB向量库持久化目录
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./chroma_db")

# 数据库路径（Demo用SQLite）
DEMO_DB_PATH = os.getenv("DEMO_DB_PATH", "./demo_sales.db")
STANDARD_DATA_DB_PATH = os.getenv("STANDARD_DATA_DB_PATH", "./data/standard_ecommerce.db")

# SQL执行限制
MAX_RESULT_ROWS = int(os.getenv("MAX_RESULT_ROWS", "1000"))    # 最大返回行数
QUERY_TIMEOUT_SEC = int(os.getenv("QUERY_TIMEOUT_SEC", "10"))   # 查询超时（秒）
MAX_RETRY_COUNT = int(os.getenv("MAX_RETRY_COUNT", "1"))        # SQL修正最大重试次数

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
INTEGRATION_TOKEN = os.getenv("INTEGRATION_TOKEN", "")

# 服务器配置
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:8100,http://localhost:8100",
    ).split(",")
    if origin.strip()
]

def validate_runtime_config() -> None:
    """生产模式缺少关键安全配置时拒绝启动。"""
    if APP_ENV != "production":
        return

    errors = []
    if not AUTH_ENABLED:
        errors.append("AUTH_ENABLED must be true")
    if JWT_SECRET == "data-agent-secret-change-in-production" or len(JWT_SECRET) < 32:
        errors.append("JWT_SECRET must be a strong production secret")
    if not INTEGRATION_TOKEN or len(INTEGRATION_TOKEN) < 24:
        errors.append("INTEGRATION_TOKEN must be configured")
    if not LLM_API_KEY or LLM_API_KEY == "sk-your-api-key-here":
        errors.append("LLM_API_KEY must be configured")
    if errors:
        raise RuntimeError("Invalid production configuration: " + "; ".join(errors))
