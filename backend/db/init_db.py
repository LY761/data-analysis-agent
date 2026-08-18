"""
演示数据库初始化 — 5张表（产品/客户/订单/订单明细/产品评论）+ 450+行样本数据
时间范围: 2026年1月-7月（当前时间）
"""
import sqlite3
import os
import random
from config import DEMO_DB_PATH

# 固定随机种子，保证每次生成的数据一致
random.seed(42)


def _mysql_schema_descriptions(url: str) -> list:
    """从 MySQL information_schema 动态生成 Schema 描述（表/列/主键/注释）"""
    import pymysql
    import urllib.parse
    u = urllib.parse.urlparse(url)
    conn = pymysql.connect(host=u.hostname, port=u.port or 3306,
                           user=u.username, password=u.password,
                           database=u.path.lstrip("/"), charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name, column_name, column_type, column_key, column_comment
                FROM information_schema.columns
                WHERE table_schema = DATABASE()
                ORDER BY table_name, ordinal_position
            """)
            rows = cur.fetchall()
    finally:
        conn.close()

    by_table = {}
    for t, c, typ, key, comment in rows:
        by_table.setdefault(t, []).append((c, typ, key, comment or ""))

    result = []
    for table, cols in by_table.items():
        col_lines = []
        col_list = []
        for c, typ, key, comment in cols:
            pk = " PRIMARY KEY" if key == "PRI" else ""
            col_lines.append(f"    {c} {typ}{pk} COMMENT '{comment}'")
            col_list.append({"name": c, "type": typ, "comment": comment})
        ddl = "CREATE TABLE " + table + " (\n" + ",\n".join(col_lines) + "\n);"
        result.append({
            "table": table,
            "ddl": ddl,
            "description": f"表 {table}：包含列 " + "、".join(c for c, *_ in cols[:12]) + " 等",
            "columns": col_list,
            "sample_queries": [],
        })
    return result


def get_schema_descriptions(db_type: str = "sqlite", path_or_url: str = "") -> list:
    """返回完整Schema（中文描述），供Schema检索器索引。
    - sqlite: 演示库硬编码 Schema
    - mysql: 从 information_schema 动态发现（需传 path_or_url=连接URL）
    """
    if db_type == "mysql" and path_or_url:
        try:
            return _mysql_schema_descriptions(path_or_url)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"[Schema] MySQL Schema 发现失败，回退演示Schema: {e}")
    return [
        {
            "table": "products",
            "ddl": """
CREATE TABLE products (
    product_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_name TEXT NOT NULL COMMENT '产品名称',
    category TEXT NOT NULL COMMENT '产品类别',
    unit_price REAL NOT NULL COMMENT '单价',
    cost_price REAL NOT NULL COMMENT '成本价',
    stock_quantity INTEGER NOT NULL COMMENT '库存数量',
    supplier TEXT COMMENT '供应商',
    created_date DATE NOT NULL COMMENT '上架日期',
    is_active INTEGER DEFAULT 1 COMMENT '是否在售(1是/0否)'
);
            """.strip(),
            "description": "产品信息表：存储所有在售产品的基本信息，包括名称、类别、价格、库存等",
            "columns": [
                {"name": "product_id", "type": "INTEGER", "comment": "产品ID（主键）"},
                {"name": "product_name", "type": "TEXT", "comment": "产品名称"},
                {"name": "category", "type": "TEXT", "comment": "产品类别（电子产品/家居/服装/食品/办公用品）"},
                {"name": "unit_price", "type": "REAL", "comment": "销售单价（元）"},
                {"name": "cost_price", "type": "REAL", "comment": "成本价（元）"},
                {"name": "stock_quantity", "type": "INTEGER", "comment": "当前库存数量"},
                {"name": "supplier", "type": "TEXT", "comment": "供应商名称"},
                {"name": "created_date", "type": "DATE", "comment": "上架日期"},
                {"name": "is_active", "type": "INTEGER", "comment": "是否在售(1=在售, 0=下架)"},
            ],
            "sample_queries": [
                "查询所有在售产品的名称和价格",
                "库存不足100的产品有哪些？",
                "每个类别的产品数量和平均价格",
            ],
        },
        {
            "table": "customers",
            "ddl": """
CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL COMMENT '客户姓名',
    region TEXT NOT NULL COMMENT '所在地区',
    city TEXT NOT NULL COMMENT '所在城市',
    member_level TEXT DEFAULT '普通' COMMENT '会员等级',
    phone TEXT COMMENT '手机号',
    register_date DATE NOT NULL COMMENT '注册日期',
    total_spent REAL DEFAULT 0 COMMENT '累计消费金额'
);
            """.strip(),
            "description": "客户信息表：存储注册客户的个人信息和消费汇总",
            "columns": [
                {"name": "customer_id", "type": "INTEGER", "comment": "客户ID（主键）"},
                {"name": "customer_name", "type": "TEXT", "comment": "客户姓名"},
                {"name": "region", "type": "TEXT", "comment": "所在地区（华北/华东/华南/华中/西南/西北）"},
                {"name": "city", "type": "TEXT", "comment": "所在城市"},
                {"name": "member_level", "type": "TEXT", "comment": "会员等级（普通/银卡/金卡/钻石）"},
                {"name": "phone", "type": "TEXT", "comment": "手机号"},
                {"name": "register_date", "type": "DATE", "comment": "注册日期"},
                {"name": "total_spent", "type": "REAL", "comment": "累计消费金额（元）"},
            ],
            "sample_queries": [
                "各地区的客户数量统计",
                "金卡以上会员的消费总额排名",
                "上月新注册的客户有哪些？",
            ],
        },
        {
            "table": "orders",
            "ddl": """
CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL COMMENT '客户ID',
    order_date DATETIME NOT NULL COMMENT '下单时间',
    total_amount REAL NOT NULL COMMENT '订单总金额',
    discount_amount REAL DEFAULT 0 COMMENT '优惠金额',
    payment_method TEXT NOT NULL COMMENT '支付方式',
    status TEXT DEFAULT '已支付' COMMENT '订单状态',
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);
            """.strip(),
            "description": "订单表（也叫营业额表/销售额表）：存储所有订单汇总信息，包含客户、金额、支付方式。可查营业额/销售额/营收/收入/订单数等",
            "columns": [
                {"name": "order_id", "type": "INTEGER", "comment": "订单ID（主键）"},
                {"name": "customer_id", "type": "INTEGER", "comment": "客户ID（外键关联customers表）"},
                {"name": "order_date", "type": "DATETIME", "comment": "下单时间"},
                {"name": "total_amount", "type": "REAL", "comment": "订单总金额（元）"},
                {"name": "discount_amount", "type": "REAL", "comment": "优惠金额（元）"},
                {"name": "payment_method", "type": "TEXT", "comment": "支付方式（微信/支付宝/银行卡/货到付款）"},
                {"name": "status", "type": "TEXT", "comment": "订单状态（已支付/已发货/已完成/已退款/已取消）"},
            ],
            "sample_queries": [
                "本月订单总金额和订单数量",
                "各支付方式的订单数量和金额占比",
                "过去7天每天的销售额趋势",
                "哪个地区的客户下单最多？",
            ],
        },
        {
            "table": "order_items",
            "ddl": """
CREATE TABLE order_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL COMMENT '订单ID',
    product_id INTEGER NOT NULL COMMENT '产品ID',
    quantity INTEGER NOT NULL COMMENT '购买数量',
    unit_price REAL NOT NULL COMMENT '成交单价',
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);
            """.strip(),
            "description": "订单明细表：记录每个订单中每种产品的购买明细",
            "columns": [
                {"name": "item_id", "type": "INTEGER", "comment": "明细ID（主键）"},
                {"name": "order_id", "type": "INTEGER", "comment": "订单ID（外键关联orders表）"},
                {"name": "product_id", "type": "INTEGER", "comment": "产品ID（外键关联products表）"},
                {"name": "quantity", "type": "INTEGER", "comment": "购买数量"},
                {"name": "unit_price", "type": "REAL", "comment": "成交单价（元）"},
            ],
            "sample_queries": [
                "销量前10的产品及销量",
                "每个产品的总销售额排名",
                "客户购买明细查询",
            ],
        },
        {
            "table": "product_reviews",
            "ddl": """
CREATE TABLE product_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL COMMENT '产品ID',
    customer_id INTEGER NOT NULL COMMENT '客户ID',
    rating INTEGER NOT NULL COMMENT '评分(1-5星)',
    review_text TEXT COMMENT '评价内容',
    sentiment TEXT COMMENT '情感标签(好评/中评/差评)',
    review_date DATE NOT NULL COMMENT '评价日期',
    FOREIGN KEY (product_id) REFERENCES products(product_id),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);
            """.strip(),
            "description": "产品评论表：存储客户对已购产品的评价和评分，用于分析产品优缺点",
            "columns": [
                {"name": "review_id", "type": "INTEGER", "comment": "评论ID（主键）"},
                {"name": "product_id", "type": "INTEGER", "comment": "产品ID（外键关联products表）"},
                {"name": "customer_id", "type": "INTEGER", "comment": "客户ID（外键关联customers表）"},
                {"name": "rating", "type": "INTEGER", "comment": "评分（1-5星）"},
                {"name": "review_text", "type": "TEXT", "comment": "评价文字内容"},
                {"name": "sentiment", "type": "TEXT", "comment": "情感标签（好评/中评/差评）"},
                {"name": "review_date", "type": "DATE", "comment": "评价日期"},
            ],
            "sample_queries": [
                "评分最高的5个产品",
                "每个产品的平均评分和评价数量",
                "差评最多的产品及差评原因",
                "本月好评率趋势",
            ],
        },
    ]


def build_full_sqlite_schemas(db_path: str = "") -> list:
    """硬编码演示表描述 + sqlite_master 动态发现的上传/接入表，合并成完整 Schema。
    数据接入（上传 CSV/Excel 建表）后调用，让 NL2SQL 立刻能问新表。"""
    import logging
    logger = logging.getLogger(__name__)
    active_db_path = db_path or DEMO_DB_PATH
    base = get_schema_descriptions("sqlite", active_db_path)
    try:
        conn = sqlite3.connect(active_db_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' "
            "AND name NOT IN ('query_cache','conversation_history','retrieval_log')"
        ).fetchall()]
        table_names = set(tables)
        base = [schema for schema in base if schema["table"] in table_names]
        known = {schema["table"] for schema in base}
        for t in tables:
            if t in known:
                continue
            cols = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
            col_defs = ", ".join(f'"{c[1]}" {c[2]}' for c in cols)
            col_list = [{"name": c[1], "type": c[2], "comment": ""} for c in cols]
            base.append({
                "table": t,
                "ddl": f"CREATE TABLE {t} ({col_defs});",
                "description": f"表 {t}：上传/接入的数据表，包含列 " + "、".join(c[1] for c in cols[:12]) + " 等",
                "columns": col_list,
                "sample_queries": [],
            })
        conn.close()
    except Exception as e:
        logger.warning(f"[Schema] 动态发现失败: {e}")
    return base


def init_demo_db():
    """创建SQLite数据库并填充样本数据（时间范围2026年1-7月）"""
    # 检查是否已有数据（含表和数据行）
    if os.path.exists(DEMO_DB_PATH):
        conn = sqlite3.connect(DEMO_DB_PATH)
        has_data = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'query_cache' AND name NOT LIKE 'conversation_history'").fetchone()[0]
        conn.close()
        if has_data >= 5:  # 5张核心表已存在
            print(f"    数据库已存在且有数据，跳过初始化")
            return DEMO_DB_PATH
        os.remove(DEMO_DB_PATH)

    conn = sqlite3.connect(DEMO_DB_PATH)
    cursor = conn.cursor()

    # 建表
    cursor.executescript("""
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            category TEXT NOT NULL,
            unit_price REAL NOT NULL,
            cost_price REAL NOT NULL,
            stock_quantity INTEGER NOT NULL,
            supplier TEXT,
            created_date DATE NOT NULL,
            is_active INTEGER DEFAULT 1
        );

        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            region TEXT NOT NULL,
            city TEXT NOT NULL,
            member_level TEXT DEFAULT '普通',
            phone TEXT,
            register_date DATE NOT NULL,
            total_spent REAL DEFAULT 0
        );

        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            order_date DATETIME NOT NULL,
            total_amount REAL NOT NULL,
            discount_amount REAL DEFAULT 0,
            payment_method TEXT NOT NULL,
            status TEXT DEFAULT '已支付',
            FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
        );

        CREATE TABLE order_items (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(order_id),
            FOREIGN KEY (product_id) REFERENCES products(product_id)
        );

        CREATE TABLE product_reviews (
            review_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            rating INTEGER NOT NULL,
            review_text TEXT,
            sentiment TEXT,
            review_date DATE NOT NULL,
            FOREIGN KEY (product_id) REFERENCES products(product_id),
            FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
        );
    """)

    # ═══════════════════════════════════════════════════════════
    # 产品数据 (20个产品，5个类别)
    # ═══════════════════════════════════════════════════════════
    products = [
        ("机械键盘K100", "电子产品", 399.00, 250.00, 150, "深圳数码科技", "2026-01-15", 1),
        ("无线蓝牙耳机Pro", "电子产品", 299.00, 180.00, 300, "深圳数码科技", "2026-02-20", 1),
        ("27寸4K显示器", "电子产品", 2499.00, 1800.00, 80, "华光电子", "2026-01-10", 1),
        ("USB-C扩展坞", "电子产品", 159.00, 90.00, 500, "深圳数码科技", "2026-03-05", 1),
        ("人体工学办公椅", "家居", 1299.00, 800.00, 60, "优居家具有限公司", "2026-01-20", 1),
        ("智能台灯L2", "家居", 199.00, 110.00, 200, "优居家具有限公司", "2026-02-15", 1),
        ("北欧风书桌", "家居", 899.00, 550.00, 40, "优居家具有限公司", "2026-03-01", 1),
        ("记忆棉枕头", "家居", 149.00, 70.00, 350, "舒睡家纺", "2026-04-10", 1),
        ("纯棉T恤(3件装)", "服装", 129.00, 60.00, 800, "时尚纺织", "2026-03-15", 1),
        ("商务衬衫白色", "服装", 259.00, 130.00, 400, "时尚纺织", "2026-04-01", 1),
        ("运动跑鞋Air", "服装", 599.00, 350.00, 250, "锋线体育用品", "2026-02-28", 1),
        ("冬季羽绒服", "服装", 899.00, 500.00, 120, "时尚纺织", "2026-01-10", 1),
        ("有机绿茶礼盒", "食品", 188.00, 100.00, 300, "天然食品公司", "2026-04-20", 1),
        ("坚果混合装500g", "食品", 79.00, 40.00, 600, "天然食品公司", "2026-05-01", 1),
        ("进口咖啡豆250g", "食品", 129.00, 75.00, 200, "天然食品公司", "2026-05-10", 1),
        ("有机蜂蜜500g", "食品", 69.00, 35.00, 450, "天然食品公司", "2026-06-01", 1),
        ("A4打印纸(5包)", "办公用品", 99.00, 60.00, 1000, "文仪办公", "2026-01-05", 1),
        ("无线鼠标M3", "办公用品", 89.00, 45.00, 400, "华光电子", "2026-02-10", 1),
        ("笔记本套装", "办公用品", 49.00, 25.00, 700, "文仪办公", "2026-03-20", 1),
        ("桌面文件架", "办公用品", 39.00, 18.00, 350, "文仪办公", "2026-04-15", 1),
    ]
    cursor.executemany(
        "INSERT INTO products (product_name, category, unit_price, cost_price, stock_quantity, supplier, created_date, is_active) VALUES (?,?,?,?,?,?,?,?)",
        products,
    )

    # ═══════════════════════════════════════════════════════════
    # 客户数据 (30个客户，覆盖6个地区)
    # ═══════════════════════════════════════════════════════════
    regions = ["华北", "华东", "华南", "华中", "西南", "西北"]
    cities_map = {
        "华北": ["北京", "天津", "石家庄"],
        "华东": ["上海", "杭州", "南京"],
        "华南": ["深圳", "广州", "东莞"],
        "华中": ["武汉", "长沙", "郑州"],
        "西南": ["成都", "重庆", "昆明"],
        "西北": ["西安", "兰州", "乌鲁木齐"],
    }
    levels = ["普通", "普通", "普通", "普通", "银卡", "银卡", "金卡", "钻石"]
    surnames = ["张", "王", "李", "赵", "陈", "杨", "黄", "周", "吴", "刘",
                "孙", "朱", "马", "胡", "林", "何", "高", "罗", "郑", "梁"]
    given_names = ["伟", "芳", "娜", "敏", "静", "强", "磊", "洋", "勇", "艳",
                   "涛", "超", "明", "丽", "军", "华", "杰", "霞", "辉", "宇"]

    customers = []
    for i in range(1, 51):
        name = random.choice(surnames) + random.choice(given_names)
        region = random.choice(regions)
        city = random.choice(cities_map[region])
        level = random.choice(levels)
        phone = f"138{random.randint(10000000, 99999999)}"
        reg_date = f"{random.randint(2025,2026)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}"
        total = round(random.uniform(500, 15000), 2)
        customers.append((name, region, city, level, phone, reg_date, total))

    cursor.executemany(
        "INSERT INTO customers (customer_name, region, city, member_level, phone, register_date, total_spent) VALUES (?,?,?,?,?,?,?)",
        customers,
    )

    # ═══════════════════════════════════════════════════════════
    # 订单数据 (1000+订单，覆盖2025-08到2026-07，含季节趋势)
    # ═══════════════════════════════════════════════════════════
    payment_methods = ["微信", "微信", "微信", "支付宝", "支付宝", "银行卡", "货到付款"]
    statuses = ["已支付", "已支付", "已支付", "已支付", "已发货", "已发货",
                "已完成", "已完成", "已完成", "已完成", "已退款", "已取消"]

    # 季节权重：不同月份销量不同（11月双11高峰，2月春节低谷）
    month_weights = {8:0.8, 9:0.9, 10:1.0, 11:1.5, 12:1.2, 1:0.7, 2:0.5, 3:1.0, 4:1.1, 5:1.1, 6:1.3, 7:1.0}
    year_months = [(8,2025),(9,2025),(10,2025),(11,2025),(12,2025),
                   (1,2026),(2,2026),(3,2026),(4,2026),(5,2026),(6,2026),(7,2026)]
    total_orders = 1000

    orders_data = []
    order_items_data = []
    for i in range(1, total_orders + 1):
        customer_id = random.randint(1, 50)
        # 按月份权重分配
        month_idx = random.choices(range(12), weights=[month_weights[m] for m,y in year_months])[0]
        month, year = year_months[month_idx]
        day = random.randint(1, 28)
        order_date = f"{year}-{month:02d}-{day:02d} {random.randint(8,22):02d}:{random.randint(0,59):02d}:00"
        discount = random.choice([0, 0, 0, 5, 10, 20, 50])
        payment = random.choice(payment_methods)
        status = random.choice(statuses)

        num_items = random.randint(1, 5)
        total = 0
        for _ in range(num_items):
            product_id = random.randint(1, 20)
            qty = random.randint(1, 5)
            unit_price = [p[2] for p in products][product_id - 1]
            total += unit_price * qty
            order_items_data.append((i, product_id, qty, unit_price))

        total = round(total - discount, 2)
        if total < 0:
            total = round(total + discount, 2)
            discount = 0
        orders_data.append((customer_id, order_date, total, discount, payment, status))

    cursor.executemany(
        "INSERT INTO orders (customer_id, order_date, total_amount, discount_amount, payment_method, status) VALUES (?,?,?,?,?,?)",
        orders_data,
    )
    cursor.executemany(
        "INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (?,?,?,?)",
        order_items_data,
    )

    # 更新客户累计消费
    cursor.executescript("""
        UPDATE customers SET total_spent = (
            SELECT COALESCE(SUM(o.total_amount), 0)
            FROM orders o
            WHERE o.customer_id = customers.customer_id AND o.status NOT IN ('已退款', '已取消')
        );
    """)

    # ═══════════════════════════════════════════════════════════
    # 产品评论数据 (每个产品3-8条评论，共约100条)
    # ═══════════════════════════════════════════════════════════
    review_templates = {
        "好评": [
            ("质量很好，性价比高，推荐购买！", 5),
            ("物流很快，包装严实，产品跟描述一致", 5),
            ("用了两周了，非常满意，会回购的", 5),
            ("做工精细，材质不错，比预期好很多", 5),
            ("客服态度好，产品质量过硬，五星好评", 5),
            ("第二次购买了，给公司采购的，同事们都说好", 4),
            ("整体不错，就是价格小贵，但质量确实好", 4),
        ],
        "中评": [
            ("一般般吧，没有想象中那么好", 3),
            ("产品还行，但物流太慢了，等了一周才到", 3),
            ("用了一个月出现小问题，联系客服解决了", 3),
            ("性价比一般，同价位有更好的选择", 3),
            ("功能基本够用，但说明书太简单了看不懂", 2),
        ],
        "差评": [
            ("质量太差了，用了不到一周就坏了！", 1),
            ("收到的货跟图片完全不符，被坑了", 1),
            ("包装破损，产品有明显划痕，客服态度恶劣", 1),
            ("完全不值这个价，退货还要自己出运费", 2),
            ("噪音很大，影响办公，非常后悔买这个", 1),
        ],
    }

    # 为每个产品生成评论（好评为主，模拟真实分布）
    review_data = []
    for product_id in range(1, 21):
        num_reviews = random.randint(10, 20)
        # 80%好评，12%中评，8%差评（模拟真实电商分布）
        for _ in range(num_reviews):
            r = random.random()
            if r < 0.80:
                sentiment = "好评"
            elif r < 0.92:
                sentiment = "中评"
            else:
                sentiment = "差评"

            templates = review_templates[sentiment]
            text, rating = random.choice(templates)
            # 评分在模板基础上有±1波动
            rating = max(1, min(5, rating + random.choice([-1, 0, 0, 0, 1])))

            customer_id = random.randint(1, 30)
            review_date = f"{random.randint(2025,2026)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}"

            review_data.append((product_id, customer_id, rating, text, sentiment, review_date))

    cursor.executemany(
        "INSERT INTO product_reviews (product_id, customer_id, rating, review_text, sentiment, review_date) VALUES (?,?,?,?,?,?)",
        review_data,
    )

    conn.commit()
    conn.close()

    # 统计
    review_count = len(review_data)
    good = sum(1 for r in review_data if r[4] == "好评")
    mid = sum(1 for r in review_data if r[4] == "中评")
    bad = sum(1 for r in review_data if r[4] == "差评")

    print(f"[OK] Demo database created: {DEMO_DB_PATH}")
    print(f"     产品: {len(products)} | 客户: {len(customers)} | 订单: {len(orders_data)} | 订单明细: {len(order_items_data)} | 评论: {review_count}")
    print(f"     评论分布: 好评{good}({good*100//review_count}%) 中评{mid}({mid*100//review_count}%) 差评{bad}({bad*100//review_count}%)")
    print(f"     时间范围: 2026年1月 - 2026年7月")
    return DEMO_DB_PATH


if __name__ == "__main__":
    init_demo_db()
