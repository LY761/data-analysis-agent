# -*- coding: utf-8 -*-
"""
补充"本月"(当前月) 的订单数据，解决 demo 数据没有当月订单导致本月类查询全空的问题。

用法：在 backend 目录下运行  .venv/Scripts/python seed_current_month.py
可重复运行：只会插入 order_id > 当前最大值 的新订单，不会重复。
"""
import sqlite3
import random
import sys
import os
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_sales.db")

# 8月重点铺货的产品（product_id: 目标销量），让这些产品本月超过上月
# 数据参考：7月 咖啡豆75/枕头54/显示器53/办公椅46/跑鞋40 等
# 注意：脚本幂等，重复运行会追加新订单（不覆盖已插入的）。
# 二次铺货：给7月销量适中、容易反超的产品补量，让"本月vs上月"赢家更丰富。
PLAN = {
    3: 35,   # 27寸4K显示器（保持7月高位，本月不及上月，作为"下跌"样例）
    8: 60,   # 记忆棉枕头：+40 反超7月54
    1: 40,   # 机械键盘K100：+25 反超7月
    2: 30,   # 无线蓝牙耳机Pro：保持赢家
    6: 35,   # 智能台灯L2：+20 反超7月
    12: 15,  # 冬季羽绒服（7月高，保持下跌）
    9: 35,   # 纯棉T恤(3件装)：+18 反超7月
    4: 22,   # USB-C扩展坞：保持赢家
    7: 12,   # 北欧风书桌（7月高，保持下跌）
    11: 28,  # 运动跑鞋Air（7月高，保持下跌）
}


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    max_order = cur.execute("SELECT MAX(order_id) FROM orders").fetchone()[0] or 0
    max_item = cur.execute("SELECT MAX(item_id) FROM order_items").fetchone()[0] or 0

    payment_methods = [r[0] for r in cur.execute(
        "SELECT DISTINCT payment_method FROM orders").fetchall()]
    customers = [r[0] for r in cur.execute(
        "SELECT customer_id FROM customers ORDER BY customer_id").fetchall()]
    if not payment_methods or not customers:
        print("数据异常：无支付方式或客户，跳过")
        return

    # 8月重点铺货的产品（product_id: 目标销量），让这些产品本月超过上月
    dates = ["2026-08-01", "2026-08-01", "2026-08-02"]  # 大部分落8/1，少部分今天

    order_id = max_order + 1
    item_id = max_item + 1
    inserted_orders = 0

    for pid, target in PLAN.items():
        unit_price = cur.execute(
            "SELECT unit_price FROM products WHERE product_id=?", (pid,)).fetchone()
        if not unit_price:
            continue
        unit_price = unit_price[0]

        n_lines = random.randint(3, 6)
        qs = [max(1, target // n_lines) for _ in range(n_lines)]
        for i in range(target - sum(qs)):     # 把余量补进去
            qs[i % len(qs)] += 1

        for q in qs:
            oid = order_id
            cid = random.choice(customers)
            date = random.choice(dates)
            total = round(q * unit_price, 2)
            pay = random.choice(payment_methods)

            cur.execute(
                """INSERT INTO orders
                   (order_id, customer_id, order_date, total_amount,
                    discount_amount, payment_method, status)
                   VALUES (?,?,?,?,?,?,?)""",
                (oid, cid, date, total, 0, pay, "已完成"))
            cur.execute(
                """INSERT INTO order_items
                   (item_id, order_id, product_id, quantity, unit_price)
                   VALUES (?,?,?,?,?)""",
                (item_id, oid, pid, q, unit_price))

            order_id += 1
            item_id += 1
            inserted_orders += 1

    conn.commit()
    conn.close()

    print(f"✅ 已插入 {inserted_orders} 条本月订单 (order_id {max_order+1}~{order_id-1})")
    print("现在「本月销售额」「本月vs上月」「本月热销」等查询都能出结果了。")


if __name__ == "__main__":
    main()
