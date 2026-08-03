"""
SQL生成器 — 用LLM把自然语言问题翻译成SQL语句
v3.0: JSON结构化输出，一次调用同时完成 生成SQL + NL回译（省一次LLM往返）

优化点：
  · generate() 改为 JSON 模式输出 {"sql": "...", "explanation": "..."}，
    删掉独立 explain_sql 调用（原来每次查询多等一次LLM）。
  · 删掉可见思考过程输出，只输出JSON，减少输出token、降低延迟。
  · 注入"全量表/字段清单"到 prompt，防止LLM编造数据库里不存在的标识符。
  · 空返回立即重试，去掉原来 sleep(2) 的固定等待。
"""
import asyncio
import json
import re
import sqlite3
import time
import logging
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, DEMO_DB_PATH

logger = logging.getLogger(__name__)

# 全量表/字段清单缓存（60s）：防止LLM编造数据库里不存在的标识符
_vocab_cache = {"ts": 0.0, "text": ""}


def get_identifier_vocab() -> str:
    """从数据库读取全量表/字段清单，注入 prompt。60s 缓存，异常时返回空串降级。"""
    now = time.time()
    if _vocab_cache["text"] and now - _vocab_cache["ts"] < 60:
        return _vocab_cache["text"]
    try:
        conn = sqlite3.connect(DEMO_DB_PATH)
        tables = {}
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'"
        ):
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
            tables[name] = cols
        conn.close()
    except Exception:
        return ""
    lines = [f"- {name}: {', '.join(cols)}" for name, cols in tables.items()]
    text = "## 数据库全部表/字段清单（只能使用这里的表和字段，严禁编造其他名字）\n" + "\n".join(lines)
    _vocab_cache.update(ts=now, text=text)
    return text


SQL_GENERATION_SYSTEM_PROMPT = """你是SQL生成助手。根据用户问题和数据库Schema，生成SQLite查询语句。
只输出一个JSON对象，不要输出任何其他内容。JSON格式：
{"sql": "SQL语句", "explanation": "用1-2句中文说明这条SQL查了什么、用了什么条件"}

## 8条铁律
1. 只能生成SELECT语句，禁止INSERT/UPDATE/DELETE/DROP等写操作
2. 只能使用"数据库全部表/字段清单"和Schema中存在的表和字段，严禁编造
3. 多表查询用外键关联（order_items.order_id→orders.order_id, order_items.product_id→products.product_id, orders.customer_id→customers.customer_id）
4. 中文模糊匹配用 LIKE '%关键词%'
5. 聚合查询必须GROUP BY，SELECT里非聚合字段必须都在GROUP BY里
6. LIMIT默认不超过100
7. SQLite不支持COMMENT，不要在SQL里用
8. 金额/数量默认按从大到小排序（ORDER BY ... DESC）

## 中文时间表达 → SQLite日期函数（重要！不要硬编码日期数字）
| 用户说 | SQLite写法 |
|--------|-----------|
| 上个月/上月 | strftime('%Y-%m', order_date) = strftime('%Y-%m', 'now', '-1 month') |
| 本月/这个月 | strftime('%Y-%m', order_date) = strftime('%Y-%m', 'now') |
| 昨天 | DATE(order_date) = DATE('now', '-1 day') |
| 今天 | DATE(order_date) = DATE('now') |
| 最近7天/过去一周 | order_date >= DATETIME('now', '-7 days') |
| 最近30天 | order_date >= DATETIME('now', '-30 days') |
| 今年 | strftime('%Y', order_date) = strftime('%Y', 'now') |
| 去年 | strftime('%Y', order_date) = strftime('%Y', 'now', '-1 year') |
| 下个月 | strftime('%Y-%m', order_date) = strftime('%Y-%m', 'now', '+1 month') |
| 明年 | strftime('%Y', order_date) = strftime('%Y', 'now', '+1 year') |
| 某一年（如2020年） | strftime('%Y', order_date) = '2020' |

**关键原则：用户问了具体时间，WHERE里就必须写时间过滤条件！**
- 问"2020年的销售额" → 必须 WHERE strftime('%Y', order_date)='2020'，禁止返回全部年份
- 问"下个月的订单" → 必须 WHERE ... '+1 month'，禁止返回全部月份
- 时间范围外没有数据 → 查询结果自然为空，这是正确的

## 查产品注意
- 用户说具体产品名（如"显示器""机械键盘"）→ 查 product_name LIKE '%关键词%'，不是 category
- category只有5个固定值：电子产品、家居、服装、食品、办公用品
- 用户明确说类别名（如"电子产品"）时才用 category='电子产品'
- 不确定时优先用 product_name LIKE

## 示例（只展示JSON格式，不要照抄表名以外的东西）
示例1（聚合排名）：本月销售额最高的5个产品
{"sql": "SELECT p.product_name, SUM(oi.quantity * oi.unit_price) AS total_sales FROM products p JOIN order_items oi ON p.product_id = oi.product_id JOIN orders o ON oi.order_id = o.order_id WHERE o.status NOT IN ('已退款', '已取消') AND strftime('%Y-%m', o.order_date) = strftime('%Y-%m', 'now') GROUP BY p.product_name ORDER BY total_sales DESC LIMIT 5", "explanation": "本月各产品销售额排名取前5，已排除退款和取消订单"}

示例2（相对时间聚合）：上个月的订单总金额是多少
{"sql": "SELECT SUM(total_amount) AS monthly_total, COUNT(*) AS order_count FROM orders WHERE strftime('%Y-%m', order_date) = strftime('%Y-%m', 'now', '-1 month') AND status NOT IN ('已退款', '已取消')", "explanation": "统计上个月订单总金额和单数，排除退款和取消"}

示例3（分组统计）：每个产品类别有多少在售产品，按数量从多到少排
{"sql": "SELECT category, COUNT(*) AS product_count FROM products WHERE is_active = 1 GROUP BY category ORDER BY product_count DESC", "explanation": "按类别统计在售产品数量并降序排列"}

示例4（多表+条件）：华南地区金卡以上会员总共消费了多少
{"sql": "SELECT c.region, c.member_level, SUM(o.total_amount) AS total_spent, COUNT(DISTINCT c.customer_id) AS customer_count FROM customers c JOIN orders o ON c.customer_id = o.customer_id WHERE c.region = '华南' AND c.member_level IN ('金卡', '钻石') AND o.status NOT IN ('已退款', '已取消') GROUP BY c.region, c.member_level", "explanation": "华南地区金卡和钻石会员的消费总额"}

示例5（销量最低）：本月销量最差的5个产品
{"sql": "SELECT p.product_name, p.category, COALESCE(SUM(oi.quantity*oi.unit_price),0) AS total_sales FROM products p LEFT JOIN order_items oi ON p.product_id=oi.product_id LEFT JOIN orders o ON oi.order_id=o.order_id AND strftime('%Y-%m',o.order_date)=strftime('%Y-%m','now') AND o.status NOT IN ('已退款','已取消') WHERE p.is_active=1 GROUP BY p.product_name ORDER BY total_sales ASC LIMIT 5", "explanation": "本月各产品销售额升序取前5，含无订单产品"}"""


SQL_FIX_SYSTEM_PROMPT = """你是一个SQL修正专家。用户生成的SQL有错误，请分析错误原因并修正。

## 常见错误类型及修正策略：
1. **语法错误** → 检查关键字拼写、括号匹配、逗号位置
2. **字段不存在** → 只能使用"数据库全部表/字段清单"中列出的表和字段，不要编造
3. **JOIN条件错误** → 检查外键关系，确保ON条件正确
4. **聚合缺失GROUP BY** → SELECT中有非聚合字段时，必须加到GROUP BY
5. **日期函数错误** → SQLite用 strftime，不是 DATE_FORMAT 或 EXTRACT

请输出修正后的纯SQL语句，不要解释和markdown标记。"""


class SQLGenerator:
    """LLM驱动的SQL生成器：自然语言→SQL + 修正 + 回译（Few-shot版）"""

    def __init__(self):
        self.client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

    @staticmethod
    def _extract_sql(text: str) -> str:
        """从JSON/标签/代码块中提取SQL。支持 {"sql":...}、<sql> 标签、```sql 代码块、裸SELECT"""
        if not text:
            return ""

        # 方法0: JSON 对象（v3.0 结构化输出）
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and obj.get("sql"):
                return obj["sql"].strip()
        except Exception:
            pass

        # 方法1: <sql>...</sql> 标签
        match = re.search(r'<sql>\s*(.*?)\s*</sql>', text, re.DOTALL)
        if match:
            return match.group(1).strip()

        # 方法2: ```sql ... ``` 代码块
        match = re.search(r'```sql\s*(.*?)\s*```', text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()

        # 方法3: 直接找SELECT语句（LLM有时忘了标签但输出了SQL）
        match = re.search(r'(SELECT\s+.+?(?:;|$))', text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip().rstrip(';')

        # 兜底：清理markdown标记后返回
        sql = text.strip()
        sql = sql.replace("```sql", "").replace("```", "").strip()
        return sql

    @staticmethod
    def _extract_explanation(text: str) -> str:
        """从JSON输出中提取NL回译。非JSON时返回空串（调用方用兜底文案）"""
        if not text:
            return ""
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and obj.get("explanation"):
                return str(obj["explanation"]).strip()
        except Exception:
            pass
        return ""

    def _build_schema_prompt(self, schema_context: dict) -> str:
        """把检索到的Schema拼成带关系提示的Prompt格式"""
        parts = ["## 可用数据库Schema\n"]

        all_tables = {}
        for t in schema_context.get("tables", []):
            all_tables[t["table"]] = t

        # 表结构详情
        for table_name, table_info in all_tables.items():
            parts.append(f"\n### 表: {table_name}")
            parts.append(table_info.get("doc", ""))

        # 表关系速查
        parts.append("\n## 表关系速查")
        if "order_items" in all_tables and "orders" in all_tables and "products" in all_tables:
            parts.append("- order_items.order_id → orders.order_id（订单明细→订单）")
            parts.append("- order_items.product_id → products.product_id（订单明细→产品）")
        if "orders" in all_tables and "customers" in all_tables:
            parts.append("- orders.customer_id → customers.customer_id（订单→客户）")

        parts.append("\n## 输出要求")
        parts.append("只输出JSON对象：{\"sql\": \"SQL语句\", \"explanation\": \"1-2句中文说明\"}，不要输出其他内容。")

        return "\n".join(parts)

    @staticmethod
    def _build_intent_guidance(intent: str = "") -> str:
        """
        根据意图类型返回SQL生成指引，注入到用户消息中。
        不同意图有不同的"套路"，让LLM少走弯路。
        """
        guides = {
            "sales_aggregation": """
## 当前意图：销售统计
套路：先确定时间范围 → 用SUM/AVG/COUNT聚合 → 按需要的维度GROUP BY。
关键提醒：必须用 strftime() 处理时间，必须过滤"已退款"和"已取消"状态。
""",
            "ranking": """
## 当前意图：排名
套路：先聚合算出指标 → ORDER BY 指标 DESC → LIMIT 取前N名。
关键提醒：排名必须加 ORDER BY + LIMIT，多表JOIN注意外键关系。
""",
            "filtering": """
## 当前意图：条件筛选
套路：确定筛选条件 → WHERE精确过滤 → 可加排序。
关键提醒：WHERE条件里的值要匹配数据库中实际格式（如"华南"不是"华南区"、"金卡"不是"金牌"）。
""",
            "comparison": """
## 当前意图：对比分析
套路：需要两个时间段 → 分别算 → 放在一起比较（可用子查询或UNION）。
关键提醒：算增长率时注意除零，用 ROUND() 美化百分比。
""",
            "grouping": """
## 当前意图：分组统计
套路：确定分组维度 → GROUP BY 维度 → 对每个组做聚合 → 可加排序。
关键提醒：SELECT里非聚合字段必须都在GROUP BY里，否则SQLite报错。
""",
            "detail_lookup": """
## 当前意图：明细查询
套路：直接SELECT需要的字段 → WHERE过滤 → 可加LIMIT。
关键提醒：不需要GROUP BY和聚合函数，返回原始记录即可。
""",
            "analysis": """
## 当前意图：综合分析（销量+差评+原因）
套路：分两步——①先找到表现差的产品（低销量/低评分）→ ②查它们的差评内容。
关键提醒：
  - "卖的不好" = 销量低或评分低，需要用 ORDER BY sales ASC 或 WHERE avg_rating < 3.5
  - "什么原因" = 需要 JOIN product_reviews 表查差评
  - 如果数据库中评分/评论不够，只返回销量排名数据，并说明"暂无足够评论数据进行分析"
  - 多表JOIN：products → order_items → orders (销量) + products → product_reviews (评论)
""",
        }
        return guides.get(intent, "")

    async def generate(self, question: str, schema_context: dict, intent: str = "") -> dict:
        """
        根据用户问题和相关Schema生成SQL（JSON结构化输出 + 熔断保护）。

        返回: {"sql": str, "explanation": str} —— 一次LLM调用同时完成生成与回译。

        异步化：用 asyncio.to_thread 把阻塞的OpenAI网络调用丢到线程池，
        事件循环保持空闲，多用户并发时不会互相卡死。
        """
        from middleware.auth_middleware import circuit_breaker

        if not circuit_breaker.before_call():
            raise RuntimeError("LLM服务暂时不可用（熔断保护中），请稍后重试或使用缓存结果。")

        schema_prompt = self._build_schema_prompt(schema_context)
        intent_guidance = self._build_intent_guidance(intent)
        vocab = get_identifier_vocab()
        user_content = f"{vocab}\n{schema_prompt}\n{intent_guidance}\n用户问题：{question}"

        def _call():
            return self.client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": SQL_GENERATION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.1,
                max_tokens=600,
                response_format={"type": "json_object"},
            )

        try:
            response = await asyncio.to_thread(_call)
            circuit_breaker.on_success()
        except Exception as e:
            circuit_breaker.on_failure()
            raise

        raw = response.choices[0].message.content or ""
        sql = self._extract_sql(raw)
        explanation = self._extract_explanation(raw)
        usage = response.usage.total_tokens if response.usage else 0

        # 空返回立即重试一次（去掉原来的 sleep(2) 固定等待）
        if not sql:
            logger.warning("[SQLGen] 首轮未提取到SQL，立即重试...")
            try:
                response = await asyncio.to_thread(_call)
                raw2 = response.choices[0].message.content or ""
                sql = self._extract_sql(raw2)
                explanation = self._extract_explanation(raw2)
                usage = response.usage.total_tokens if response.usage else 0
            except Exception as e:
                logger.error(f"[SQLGen] 重试异常: {e}")

        return {"sql": sql, "explanation": explanation, "tokens": usage}

    async def fix_sql(self, question: str, schema_context: dict, original_sql: str, error_msg: str) -> str:
        """修正失败的SQL — 带错误分类引导的重写（异步 + 注入全量表/字段清单）"""
        from middleware.auth_middleware import circuit_breaker

        if not circuit_breaker.before_call():
            raise RuntimeError("LLM服务暂时不可用（熔断保护中），无法修正SQL。请稍后重试。")

        schema_prompt = self._build_schema_prompt(schema_context)
        vocab = get_identifier_vocab()

        def _call():
            return self.client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": SQL_FIX_SYSTEM_PROMPT},
                    {"role": "user", "content": f"""{vocab}
{schema_prompt}

用户问题：{question}

上次生成的SQL（有错误）：
{original_sql}

错误信息：{error_msg}

请分析错误类型并修正SQL，只输出修正后的纯SQL语句。"""},
                ],
                temperature=0.1,
                max_tokens=500,
            )

        try:
            response = await asyncio.to_thread(_call)
            circuit_breaker.on_success()
        except Exception as e:
            circuit_breaker.on_failure()
            raise

        sql = response.choices[0].message.content.strip()
        sql = sql.replace("```sql", "").replace("```", "").strip()
        usage = response.usage.total_tokens if response.usage else 0
        return {"sql": sql, "tokens": usage}

    def explain_sql(self, sql: str) -> str:
        """NL回译：把SQL翻译回自然语言（v3.0已合并到generate，保留给外部兼容调用）"""
        response = self.client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "你是一个SQL翻译助手。将SQL语句翻译成通俗易懂的自然语言，说明查了什么表、做了什么聚合、用了什么条件。"},
                {"role": "user", "content": f"请用自然语言解释这条SQL查询做了什么：\n{sql}"},
            ],
            temperature=0.1,
            max_tokens=200,
        )
        return response.choices[0].message.content.strip()

    async def answer_stream(self, question: str, sql: str, query_result: dict,
                            stream_cb) -> str:
        """
        基于查询结果生成口语化回答（LLM，流式输出）。

        - stream_cb(delta)：每产生一段文本回调一次（在工作线程里调用，调用方需线程安全转发）。
        - 返回完整回答文本；LLM不可用/失败时返回空串，调用方降级为规则版回答。
        """
        if not query_result.get("data"):
            return ""

        from middleware.auth_middleware import circuit_breaker
        if not circuit_breaker.before_call():
            return ""

        summary = json.dumps(query_result["data"][:10], ensure_ascii=False, default=str)
        system_prompt = (
            "你是数据分析助手。根据用户问题、SQL和查询结果，用1-3句口语化的中文直接回答。"
            "不要复述SQL，不要markdown，不要啰嗦。数据不足就如实说明。"
        )
        user_prompt = f"用户问题: {question}\nSQL: {sql}\n查询结果: {summary}\n请回答。"

        def _stream() -> str:
            resp = self.client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=200,
                stream=True,
            )
            parts = []
            for chunk in resp:
                if not chunk or not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    parts.append(delta)
                    stream_cb(delta)
            return "".join(parts)

        try:
            text = await asyncio.to_thread(_stream)
            circuit_breaker.on_success()
            return text.strip()
        except Exception as e:
            logger.warning(f"[AnswerStream] LLM回答失败: {e}")
            circuit_breaker.on_failure()
            return ""


# 全局单例
sql_generator = SQLGenerator()
