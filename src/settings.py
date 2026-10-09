"""全项目路径常量统一管理（唯一允许定义目录/文件路径的位置）。

任何模块需要目录/文件路径时一律 ``from src.settings import ...``，
禁止在业务代码里硬写 ``"logs"`` / ``"output"`` / ``"sql"`` 之类的相对路径字符串。

同时负责把 ``config/.env`` 的 KEY=VALUE 注入环境变量（不覆盖系统里已存在的
同名变量），使 ``.env`` 成为可落盘的本地配置来源。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

# ---- 目录常量（全部由本文件推导，其它模块只引用） ----
ROOT_DIR = Path(__file__).resolve().parents[1]  # 项目根目录
SRC_DIR = ROOT_DIR / "src"                      # 源码包根
CONFIG_DIR = ROOT_DIR / "config"                # 配置目录
ENV_FILE = CONFIG_DIR / ".env"                  # 本地环境变量文件（不入库）
LOG_DIR = ROOT_DIR / "logs"                     # 运行日志：spider_<run_id>.log / agent_<ts>.log
OUTPUT_DIR = ROOT_DIR / "output"                # AI 选品 JSON/Excel 落盘
INPUT_DIR = ROOT_DIR / "input"                  # 输入数据
SQL_DIR = ROOT_DIR / "sql"                      # DDL：init_table.sql（一键建库）/ backup_tables.sql / business_tables.sql
TESTS_DIR = ROOT_DIR / "tests"                  # 单元测试
JS_REVERSE_CACHE_DIR = ROOT_DIR / "js_reverse_cache"  # 逆向素材缓存（gitignore）


def load_env(path: Path | str | None = None) -> int:
    """把 ``.env`` 文件里的 ``KEY=VALUE`` 注入 ``os.environ``（python-dotenv 解析）。

    * 已存在于环境变量的键**不覆盖**（系统/CI 真实配置优先）；
    * 支持 ``#`` 注释行、空行与成对引号（由 ``dotenv_values`` 解析）；
    * 文件不存在时静默返回 0（不报错）。

    :returns: 实际注入的键数量。
    """
    env_path = Path(path) if path else ENV_FILE
    if not env_path.exists():
        return 0
    applied = 0
    try:
        values = dotenv_values(env_path)
    except (OSError, ValueError):
        return 0
    for key, value in values.items():
        if not key:
            continue
        if key not in os.environ:
            os.environ[key] = value if value is not None else ""
            applied += 1
    return applied


# 导入即加载：任何 ``from src.settings import ...`` 都会带上 .env 配置
load_env()
