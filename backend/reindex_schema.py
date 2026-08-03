# -*- coding: utf-8 -*-
"""
重建 Schema 向量索引，使其与当前配置的 embedding 模型维度一致。

用途：如果换过 embedding 模型（如 large→small），旧的 ChromaDB 索引维度
和当前模型不匹配，向量检索会报 "Collection expecting embedding with dimension of X, got Y"。
运行本脚本用当前模型重建即可。

用法：在 backend 目录运行  .venv/Scripts/python reindex_schema.py
"""
import sys
import io
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from db.init_db import get_schema_descriptions
from agent.schema_retriever import schema_retriever

print("读取数据库 Schema...")
schemas = get_schema_descriptions()
print(f"共 {len(schemas)} 张表，开始用当前 embedding 模型重建索引...")

t0 = time.time()
schema_retriever.index_schemas(schemas, force=True)
elapsed = time.time() - t0

print(f"✅ 重建完成，耗时 {elapsed:.1f}s，集合文档数: {schema_retriever.collection.count()}")
print("现在向量检索维度已与模型一致。")
