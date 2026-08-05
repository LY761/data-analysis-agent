# 📊 Data Analysis Agent

基于 **NL2SQL 的数据分析助手** — 用自然语言查数据、做市场情报、知识问答。
面向电商场景（选品 / 竞品 / 商品研究），本地优先、零依赖外部向量服务、免费开源。

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.1xx-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

## ✨ 功能特性

### 核心：自然语言查数据库（NL2SQL）
- 意图路由（规则快路径 0 Token + LLM 兜底）
- 查询澄清、Schema 语义检索（本地 BGE 中文嵌入）
- SQL 生成 + 四道校验闸门 + 失败自动重试
- 自然语言回答（LLM 增强，失败降级规则版）
- 智能图表推荐（日期值识别 / Top-N 柱状图 / 趋势图）

### 数据与存储
- 支持 **SQLite**（默认演示库）/ **MySQL** 运行时切换
- **上传 CSV / Excel 自动建表入库**，立即可问
- 向量库 **ChromaDB**（默认）/ **Milvus** 可切换
- 查询缓存、对话记忆（语义分组 + 自适应窗口）

### 联网与知识
- **企业知识库问答（RAG）**：上传 txt/md/pdf/csv，基于文档带引用回答
- **联网搜索**：知识类问题先搜（必应中国 / 百度 / 维基兜底）再总结回答

### 市场情报（电商）
- 选品分析、商品研究、竞品分析
- **粘贴数据分析**：把看到的真实商品/市场信息贴进来，LLM 直接分析（绕开反爬）
- Amazon / 京东爬虫（curl_cffi Chrome 指纹）

### 工程化
- 检索质量看板（metrics.html）、链路追踪、LLM 熔断、限流
- JWT 认证（可开关）、数据脱敏、Excel 导出
- 100 条评测集 + 106 个自动化测试

## 🚀 快速开始

```bash
# 1. 创建虚拟环境并安装依赖
cd backend
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements.txt

# 2. 配置 LLM（OpenAI 兼容接口，默认 DeepSeek）
copy .env.example .env          # 然后编辑填入 LLM_API_KEY

# 3. 启动
python -m uvicorn main:app --reload
```

打开 http://localhost:8000 即可使用；指标看板在 http://localhost:8000/metrics.html。

> 首次启动自动初始化演示数据库（450+ 行电商样本数据），无需任何配置即可体验。

## 🔧 环境变量（.env）

| 变量 | 默认 | 说明 |
|------|------|------|
| `LLM_API_KEY` | - | LLM 密钥（OpenAI 兼容） |
| `LLM_BASE_URL` | `https://api.deepseek.com` | LLM 接口地址 |
| `LLM_MODEL` | `gpt-4o-mini` | 模型名 |
| `DB_TYPE` | `sqlite` | `sqlite` / `mysql` |
| `MYSQL_URL` | - | MySQL 连接串（DB_TYPE=mysql 时） |
| `DEMO_DB_PATH` | `./demo_sales.db` | SQLite 文件路径 |
| `VECTOR_STORE` | `chromadb` | `chromadb` / `milvus` |
| `EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 本地嵌入模型 |
| `WEB_SEARCH_ENABLED` | `true` | 知识类问题联网搜索 |
| `NL_ANSWER_LLM` | `true` | 查询结果 LLM 口语化回答 |
| `AUTH_ENABLED` | `false` | 开启 JWT 认证 |
| `COMPETITOR_SCRAPER_PATH` | `C:/Users/LY/competitor-scraper` | 竞品数据目录 |

## 🗂️ 目录结构

```
backend/
├── main.py                 # FastAPI 入口
├── agent/                  # NL2SQL 工作流（LangGraph 7 节点）
│   ├── workflow.py         # 查询流水线
│   ├── agent_router.py     # 意图路由
│   ├── knowledge_base.py   # 企业知识库（RAG）
│   ├── web_search.py       # 联网搜索
│   ├── crawler.py          # Amazon/京东爬虫
│   └── market_intelligence/# 选品/商品研究/粘贴分析
├── api/routes.py           # REST API
├── db/                     # 执行器/连接管理/初始化
├── services/               # 导出/BI/指标/数据接入
├── eval/                   # 100 条评测集
└── tests/                  # 106 个自动化测试
frontend/
└── index.html              # 纯 JS 前端（聊天/面板/看板）
docs/                       # 架构文档与示例知识库文档
```

## 📄 数据与隐私

- 所有数据本地存储（SQLite + ChromaDB），不采集任何遥测。
- 演示数据库为随机生成的样本数据（固定随机种子，可复现）。
- 爬虫仅用于公开页面信息抓取，请遵守目标平台条款。

## 🧪 测试

```bash
cd backend
../.venv/Scripts/python.exe -m pytest tests/ -q
```

## 📜 License

MIT
