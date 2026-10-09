"""MySQL 备份库封装：连接 / 建库建表 / 执行 SQL 文件 / 通用插入。

设计原则
--------
1. **业务代码不写 SQL**：调用方只传表名 + 字典，拼接、转义全部在这里做；
2. **标识符白名单**：表名/列名必须是 ``[A-Za-z][A-Za-z0-9_]*``，杜绝注入；
3. **异常可控**：所有底层异常统一包装成 :class:`DatabaseError` 并带上表名/
   列名等上下文，采集主流程 ``except DatabaseError`` 一行即可降级（备份失败
   不应中断采集）；
4. **配置外置**：连接参数全部走环境变量，换机器/换库不改代码。

环境变量（凭据只走环境变量，见 config/.env.example）
----------------------------------------------------
    DB_HOST=127.0.0.1  DB_PORT=3306  DB_USER=<你的账号>  DB_PASSWORD=<你的密码>  DB_NAME=spider_db

典型用法
--------
    from src.common import database

    database.init_schema()                      # 幂等：建库 + 跑 sql/*.sql
    row_id = database.insert_one("api_backup", {"api_name": "...", "raw_json": "..."})

    环境变量可写在 ``config/.env``（导入 :mod:`src.settings` 时自动注入）。
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

import pymysql
from pymysql.cursors import Cursor

from src.settings import ROOT_DIR, SQL_DIR  # 路径常量统一由 settings 管理

_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# 建库后默认选中的业务库名
DEFAULT_DB_NAME = "spider_db"


class DatabaseError(RuntimeError):
    """数据库操作失败（连接、DDL、插入等），message 内含足够排障上下文。"""


def config(database: str | None = None) -> dict[str, Any]:
    """读取连接配置（账号密码等凭据必须来自环境变量，禁止硬编码）。"""
    return {
        "host": os.environ.get("DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("DB_PORT", "3306")),
        "user": os.environ.get("DB_USER", ""),
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": database or os.environ.get("DB_NAME", DEFAULT_DB_NAME),
        "charset": "utf8mb4",
        "autocommit": True,
        "connect_timeout": 10,
        "read_timeout": 60,
        "write_timeout": 60,
    }


# 连接目标的"未指定"哨兵：与显式 database=None（=不选库，建库/USE 前用）区分
_UNSET = object()


@contextmanager
def connection(database: Any = _UNSET) -> Iterator[pymysql.connections.Connection]:
    """上下文管理器形式的连接：正常退出自动 commit+close，异常自动 rollback。

    :param database: 不传 -> 连默认库(config()["database"])；
                    传 ``None`` -> 连服务器但**不选库**（CREATE DATABASE/USE 之前用）；
                    传库名 -> 连指定库。
    """
    cfg = config()
    if database is None:
        cfg.pop("database", None)
    elif database is not _UNSET:
        cfg["database"] = database
    conn = None
    try:
        conn = pymysql.connect(**cfg)
        yield conn
        conn.commit()
    except DatabaseError:
        if conn is not None:
            conn.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 - 统一包装成 DatabaseError
        if conn is not None:
            conn.rollback()
        raise DatabaseError(f"数据库连接异常: {exc!r}") from exc
    finally:
        if conn is not None:
            conn.close()


def _check_ident(kind: str, name: str) -> str:
    """表名/列名白名单校验，返回原名。"""
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise DatabaseError(f"非法{kind}标识符: {name!r}（只允许字母/数字/下划线，字母开头）")
    return name


def _quote(name: str) -> str:
    return f"`{_check_ident('列', name)}`"


def ensure_database() -> None:
    """CREATE DATABASE IF NOT EXISTS（不选库，纯幂等）。"""
    db_name = _check_ident("库", config()["database"])
    try:
        with connection(database=None) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
                )
    except DatabaseError as exc:
        raise DatabaseError(f"建库失败 ({db_name}): {exc}") from exc


def split_sql_statements(sql_text: str) -> list[str]:
    """按分号拆分 SQL 文本。

    简单可靠即可：本项目 DDL 里不含存储过程/触发器那种带分号的
    BEGIN...END 块；字符串字面量里的分号已用 '' 空串规避（建表脚本
    注释统一走 COMMENT，不放分号）。
    """
    statements: list[str] = []
    buf: list[str] = []
    for line in sql_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--") or stripped.startswith("#"):
            continue  # 跳过整行注释
        buf.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";").strip()
            if stmt:
                statements.append(stmt)
            buf = []
    tail = "\n".join(buf).strip().rstrip(";").strip()
    if tail:
        statements.append(tail)
    return statements


def run_sql_file(path: Path | str | None = None, verbose: bool = False) -> int:
    """执行 SQL 文件（DDL 脚本），返回执行的语句条数。

    :param path:    默认 ``sql/backup_tables.sql``。
    :param verbose: True 时把每条 DDL 打到控制台（排查建表问题用）。
    """
    sql_path = Path(path) if path else SQL_DIR / "backup_tables.sql"
    if not sql_path.exists():
        raise DatabaseError(f"SQL 文件不存在: {sql_path}")
    try:
        sql_text = sql_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatabaseError(f"SQL 文件读取失败 {sql_path}: {exc}") from exc

    statements = split_sql_statements(sql_text)
    if not statements:
        raise DatabaseError(f"SQL 文件没有任何可执行语句: {sql_path}")

    try:
        # DDL 里含 CREATE DATABASE/USE，因此先连"无库"连接
        with connection(database=None) as conn:
            with conn.cursor() as cur:
                for idx, stmt in enumerate(statements, 1):
                    cur.execute(stmt)
                    if verbose:
                        _log_ddl(sql_path.name, idx, stmt)
    except DatabaseError as exc:
        raise DatabaseError(f"执行 {sql_path} 失败: {exc}") from exc
    return len(statements)


def _log_ddl(fname: str, idx: int, stmt: str) -> None:
    first_line = stmt.splitlines()[0][:80]
    print(f"      [DDL] {fname} #{idx}: {first_line}")


def init_schema(sql_path: Path | str | None = None, verbose: bool = False) -> int:
    """幂等初始化：建库 + 执行建表脚本。返回执行的语句条数。

    :param verbose: True 时把每条 DDL 打到控制台。
    """
    ensure_database()
    return run_sql_file(sql_path, verbose=verbose)


def _normalize_value(value: Any) -> Any:
    """值预处理：dict/list 自动转 JSON 字符串，其余原样交给驱动参数化。"""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def build_insert_sql(table: str, row: Mapping[str, Any]) -> tuple[str, list[Any]]:
    """由 ``{列: 值}`` 生成参数化 INSERT 语句（供 insert_one / 单测复用）。

    :returns: ``(SQL 模板, 参数列表)`` —— 模板里是 %s 占位符，天然防注入。
    """
    _check_ident("表", table)
    if not isinstance(row, Mapping) or not row:
        raise DatabaseError(f"插入 {table} 的行数据不能为空")
    columns = [_check_ident("列", col) for col in row.keys()]
    placeholders = ", ".join(["%s"] * len(columns))
    sql = (
        f"INSERT INTO `{table}` ({', '.join(f'`{c}`' for c in columns)}) "
        f"VALUES ({placeholders})"
    )
    params = [_normalize_value(v) for v in row.values()]
    return sql, params


def insert_one(table: str, row: Mapping[str, Any]) -> int:
    """插入一行，返回自增主键 id。

    失败抛 :class:`DatabaseError`；调用方按业务需要决定是否降级。
    """
    sql, params = build_insert_sql(table, row)
    try:
        with connection() as conn:
            with conn.cursor(cursor=Cursor) as cur:
                affected = cur.execute(sql, params)
                last_id = cur.lastrowid or 0
    except DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DatabaseError(f"插入 {table} 失败: {exc!r} | 列={list(row.keys())}") from exc
    if affected != 1:
        raise DatabaseError(f"插入 {table} 影响行数异常: {affected}")
    return int(last_id)


def insert_many(table: str, rows: list[Mapping[str, Any]]) -> int:
    """批量插入（同一套列），返回插入行数。行之间列集合必须一致。"""
    if not rows:
        return 0
    sql, first_params = build_insert_sql(table, rows[0])
    batch = [first_params]
    for row in rows[1:]:
        _, params = build_insert_sql(table, row)
        batch.append(params)
    try:
        with connection() as conn:
            with conn.cursor(cursor=Cursor) as cur:
                affected = cur.executemany(sql, batch)
    except DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DatabaseError(f"批量插入 {table} 失败: {exc!r}") from exc
    return int(affected)


def fetch_one(table: str, where: str = "", params: list[Any] | None = None) -> dict | None:
    """按条件取一行（读回校验/排查用）。where 为裸条件表达式如 ``id=%s``。"""
    _check_ident("表", table)
    sql = f"SELECT * FROM `{table}`"
    if where:
        sql += f" WHERE {where}"
    try:
        with connection() as conn:
            with conn.cursor(cursor=pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, params or [])
                return cur.fetchone()
    except DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DatabaseError(f"查询 {table} 失败: {exc!r}") from exc


def fetch_all(
    table: str,
    where: str = "",
    params: list[Any] | None = None,
    cols: str = "*",
) -> list[dict]:
    """按条件取多行（ETL 读备份表用）。where/params 必须参数化成对出现。

    :param cols: 只允许逗号分隔的合法列名/``*``（白名单逐个校验，防注入）。
    """
    _check_ident("表", table)
    if cols.strip() == "*":
        col_sql = "*"
    else:
        col_sql = ", ".join(_quote(c.strip()) for c in cols.split(",") if c.strip())
    sql = f"SELECT {col_sql} FROM `{table}`"
    if where:
        sql += f" WHERE {where}"
    try:
        with connection() as conn:
            with conn.cursor(cursor=pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, params or [])
                return list(cur.fetchall() or [])
    except DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DatabaseError(f"查询 {table} 失败: {exc!r}") from exc


def build_upsert_sql(
    table: str, row_keys: list[str], update_cols: list[str]
) -> str:
    """生成参数化 UPSERT 模板（``%s`` 占位），唯一键冲突时用新值覆盖旧值。

    MySQL 8.0.19+ 行别名语法（``VALUES(...) AS new ON DUPLICATE KEY UPDATE``），
    取代已废弃的 ``VALUES(col)`` 写法；模板建一次，:func:`upsert_many` 逐行复用。

    :param table:       目标表（白名单校验）。
    :param row_keys:    INSERT 列（顺序即参数顺序）。
    :param update_cols: 冲突时更新的列（必须是 ``row_keys`` 子集）。
    """
    _check_ident("表", table)
    if not row_keys:
        raise DatabaseError(f"UPSERT {table} 列集合为空")
    cols = [_check_ident("列", c) for c in row_keys]
    updates = [_check_ident("列", c) for c in update_cols]
    unknown = [c for c in updates if c not in cols]
    if unknown:
        raise DatabaseError(f"UPSERT {table} 更新列不在插入列中: {unknown}")
    if not updates:
        raise DatabaseError(f"UPSERT {table} 更新列不能为空")
    placeholders = ", ".join(["%s"] * len(cols))
    col_sql = ", ".join(f"`{c}`" for c in cols)
    upd_sql = ", ".join(f"`{c}` = `new`.`{c}`" for c in updates)
    return (
        f"INSERT INTO `{table}` ({col_sql}) VALUES ({placeholders}) AS `new` "
        f"ON DUPLICATE KEY UPDATE {upd_sql}"
    )


def upsert_many(
    table: str,
    rows: list[Mapping[str, Any]],
    conflict_cols: list[str],
    update_cols: list[str] | None = None,
) -> int:
    """批量 UPSERT（唯一键 = ``conflict_cols``，冲突则覆盖 ``update_cols``）。

    行之间列集合必须一致；``update_cols`` 缺省为全部插入列去掉冲突列。
    返回实际处理行数；失败抛 :class:`DatabaseError`。
    """
    if not rows:
        return 0
    row_keys = list(rows[0].keys())
    for row in rows[1:]:
        if list(row.keys()) != row_keys:
            raise DatabaseError(
                f"UPSERT {table} 各行列集合必须一致: {list(row.keys())} != {row_keys}"
            )
    conflict = [_check_ident("列", c) for c in conflict_cols]
    missing = [c for c in conflict if c not in row_keys]
    if missing:
        raise DatabaseError(f"UPSERT {table} 冲突列不在插入列中: {missing}")
    updates = update_cols if update_cols is not None else [
        c for c in row_keys if c not in conflict
    ]
    sql = build_upsert_sql(table, row_keys, updates)
    batch = [_normalize_value(row[c]) for row in rows for c in row_keys]
    try:
        with connection() as conn:
            with conn.cursor(cursor=Cursor) as cur:
                for i in range(len(rows)):
                    cur.execute(sql, batch[i * len(row_keys): (i + 1) * len(row_keys)])
    except DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DatabaseError(f"批量 UPSERT {table} 失败: {exc!r}") from exc
    return len(rows)
