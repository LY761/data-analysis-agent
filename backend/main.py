"""
数据分析Agent — 主入口
FastAPI服务 + NL→SQL→图表 全流程
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from api.routes import router
from db.init_db import init_demo_db, get_schema_descriptions
from agent.schema_retriever import schema_retriever
from config import CORS_ORIGINS, HOST, PORT, DEMO_DB_PATH, validate_runtime_config

validate_runtime_config()

# ═══════════════════════════════════════════════════════════════
# 启动初始化（智能跳过，不重复做）
# ═══════════════════════════════════════════════════════════════
print("=" * 60)
print("  数据分析Agent — NL2SQL + 智能可视化")
print("=" * 60)

# 第一步：数据库（已有就跳过，不重复初始化）
db_exists = os.path.exists(DEMO_DB_PATH)
if db_exists:
    print(f"\n[1/3] 数据库已存在: {DEMO_DB_PATH} — 跳过初始化")
else:
    print("\n[1/3] 首次启动，初始化演示数据库...")
    init_demo_db()
# 无论数据库是否已存在都注册 demo 连接（否则 /api/db/list 永远为空）
from db.connection_manager import register_demo_db
register_demo_db()

from services.retrieval_metrics import initialize_retrieval_metrics
initialize_retrieval_metrics(DEMO_DB_PATH)

# 第二步：Schema目录和向量索引
schemas = get_schema_descriptions()
schema_retriever.set_schema_catalog(schemas)
col_count = schema_retriever.collection.count() if hasattr(schema_retriever, "collection") else 0
if col_count > 0:
    print(f"\n[2/3] Schema索引已存在({col_count}条) — 跳过")
else:
    print("\n[2/3] 索引数据库Schema到向量库...")
    schema_retriever.index_schemas(schemas)

# 第三步：Schema增量更新监听
print("\n[3/3] 启动Schema增量更新监听...")
from schema_watcher import schema_watcher
schema_watcher.start()



# ═══════════════════════════════════════════════════════════════
# FastAPI 应用
# ═══════════════════════════════════════════════════════════════
app = FastAPI(
    title="数据分析Agent",
    description="企业级NL2SQL + 智能可视化Agent",
    version="3.0.0",
)

app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

from middleware.auth_middleware import AuthMiddleware
app.add_middleware(AuthMiddleware)

from api.report_routes import router as report_router
app.include_router(report_router, prefix="/api")

app.include_router(router, prefix="/api")

from api.integration_routes import router as integration_router
app.include_router(integration_router, prefix="/api")

from api.capability_routes import router as capability_router
app.include_router(capability_router, prefix="/api")

from api.semantic_routes import router as semantic_router
app.include_router(semantic_router, prefix="/api")

from api.data_product_routes import router as data_product_router
app.include_router(data_product_router, prefix="/api")

from api.workflow_routes import router as workflow_router
app.include_router(workflow_router, prefix="/api")

from api.dashboard_routes import router as dashboard_router
app.include_router(dashboard_router, prefix="/api")

from api.evaluation_routes import router as evaluation_router
app.include_router(evaluation_router, prefix="/api")


@app.middleware("http")
async def no_cache_html(request, call_next):
    """HTML/前端静态文件禁用缓存，避免改了代码但浏览器还显示旧页面"""
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/index.html") or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


frontend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
if os.path.exists(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

print(f"""
{'='*60}
  服务启动: http://localhost:{PORT}
  API文档: http://localhost:{PORT}/docs
{'='*60}

快捷查询: 前端10个卡片，点一下出结果（0 Token）
智能分析: 前端分析按钮，差评+建议
自由提问: 输入框，Agent流水线（意图分类+SQL生成）
""")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
