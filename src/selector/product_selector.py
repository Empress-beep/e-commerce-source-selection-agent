"""product_info 业务表独立初筛查询（pymysql 参数化版）。

按硬性条件剔除不合格商品，返回候选商品字典列表。
全部筛选参数可空可选：不传则该条件不启用；条件值一律走 %s 占位符
预编译传参，不拼接任何用户输入，杜绝 SQL 注入。
比较条件会自动剔除该字段为 NULL 的行（NULL 不满足比较 = 硬性不合格）。
run_id 批次隔离：单个值 -> ``WHERE run_id = %s`` 精确匹配；逗号分隔多个
（多关键词轮次各产生一个 run_id）自动展开 ``run_id IN (...)``；空/未传
不加该条件。Agent 调用时该参数由执行器从 state 自动注入（用户未给任何
筛选条件时也带上），独立脚本调试可不传。

用法：
    rows = query_product_list(cate_name="男鞋", max_wholesale_price=50,
                              min_buy_count=500, max_return_goods_rate=0.05)
    rows = query_product_list(run_id="20261008_145834")              # 只查本轮
    rows = query_product_list(run_id="20261008_145834,20261008_146012")  # 多批次
"""
from __future__ import annotations

import os

import pymysql
from pymysql.cursors import DictCursor

try:  # 项目内运行：把 config/.env 的 DB_* 注入环境变量（独立拷走时自动跳过）
    from src.settings import load_env
except ImportError:  # pragma: no cover - 独立脚本场景
    load_env = None
if load_env is not None:
    load_env()


def get_connection() -> pymysql.connections.Connection:
    """封装数据库连接（账号密码等凭据必须来自环境变量，见 config/.env.example）。"""
    return pymysql.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", ""),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "spider_db"),
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10,
        read_timeout=60,
        write_timeout=60,
    )


def query_product_list(
    cate_name: str | None = None,
    max_wholesale_price: float | None = None,
    min_buy_count: int | None = None,
    max_return_goods_rate: float | None = None,
    run_id: str | None = None,
) -> list[dict]:
    """对 product_info 做条件初筛，返回候选商品列表（字典列表）。

    :param cate_name: 类目名，模糊匹配（如 "男鞋" 命中 "男鞋 > 休闲鞋"）
    :param max_wholesale_price: 最高拿货价（元），超过则剔除；NULL 行自动剔除
    :param min_buy_count: 最低销量，低于则剔除；NULL 行自动剔除
    :param max_return_goods_rate: 最大退货率（0~1，0.05 = 5%），超过则剔除
    :param run_id: 采集批次标识（可选，批次隔离）：单个值 -> ``WHERE run_id = %s``；
        逗号分隔多个 -> ``run_id IN (...)``（多关键词轮次）；空/未传不加条件。
        Agent 调用由执行器自动注入 state 保存的本轮 run_id，模型固定传 ""。
    :returns: 每条字典对应一行记录，按销量降序；查询失败返回空列表
    """
    conditions: list[str] = []
    params: list = []

    # 批次隔离：只读指定采集 run 的数据（多关键词轮次逗号分隔展开 IN）
    run_ids = [r.strip() for r in str(run_id or "").split(",") if r.strip()]
    if len(run_ids) == 1:
        conditions.append("`run_id` = %s")
        params.append(run_ids[0])
    elif run_ids:
        conditions.append("`run_id` IN (" + ", ".join(["%s"] * len(run_ids)) + ")")
        params.extend(run_ids)

    if cate_name:
        conditions.append("`cate_name` LIKE %s")
        params.append(f"%{cate_name}%")
    if max_wholesale_price is not None:
        conditions.append("`wholesale_price` <= %s")
        params.append(max_wholesale_price)
    if min_buy_count is not None:
        conditions.append("`buy_count` >= %s")
        params.append(min_buy_count)
    if max_return_goods_rate is not None:
        conditions.append("`return_goods_rate` <= %s")
        params.append(max_return_goods_rate)

    # 表名/列名为代码内常量，条件值全部走 %s 参数化占位
    sql = "SELECT * FROM `product_info`"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY `buy_count` DESC"

    conn = None
    try:
        conn = get_connection()
        with conn.cursor(DictCursor) as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        return list(rows)
    except pymysql.MySQLError as exc:
        print(f"[ERROR] product_info 初筛查询失败: {exc}")
        return []
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    # 演示1：单条件 —— 类目包含"鞋"（命中全部鞋类类目）
    rows = query_product_list(cate_name="鞋")
    print(f"[演示1] 类目含'鞋': {len(rows)} 条")

    # 演示2：双硬性条件 —— 拿货价 <= 50 且 销量 >= 50
    rows = query_product_list(max_wholesale_price=50, min_buy_count=50)
    print(f"[演示2] 拿货价<=50 且 销量>=50: {len(rows)} 条")

    # 演示3：四条件组合初筛
    rows = query_product_list(
        cate_name="板鞋",
        max_wholesale_price=50,
        min_buy_count=50,
        max_return_goods_rate=0.05,
    )
    print(f"[演示3] 组合初筛: {len(rows)} 条候选")
    for r in rows[:10]:
        name = (r.get("product_name") or "")[:24]
        print(
            f"  {r['product_id']} | {name} | 拿货{r.get('wholesale_price')} "
            f"| 销量{r.get('buy_count')} | 退货率{r.get('return_goods_rate')} "
            f"| {r.get('shop_name')}"
        )

    # 演示4：不传任何参数 = 不启用任何条件（返回全表，按销量降序）
    rows = query_product_list()
    print(f"[演示4] 无条件(全表): {len(rows)} 条")
