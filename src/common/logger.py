"""运行日志模块：每次运行一个日志文件，控制台只放行简要进度。

设计要点
--------
1. **一次运行 = 一个日志文件** ``logs/spider_YYYYMMDD_HHMMSS.log``，
   run_id 同时写进日志文件名和 MySQL 备份行，方便对账。
2. **两套日志模式**（``init(mode=...)``）：
   - ``quiet``（导入调用 / Agent ``run_crawl`` 默认路径）：root+文件+控制台
     全部 INFO —— DEBUG 记录在 logger 层即被丢弃，AES key/iv、原始加密报文、
     解密明文大文本（均走 ``file_only``/``log_block``）**完全不落盘不输出**；
     INFO 只留业务关键点（run_id/关键词/页码/limit/条目数/详情数/入库行数/统计）。
   - ``debug``（``python -m src.collector.main`` __main__ 本地逆向调试）：
     root+文件 DEBUG 全收（含完整请求/响应报文与明文），控制台 DEBUG 但
     ``file_only`` 记录仍被过滤 —— 报文块只进文件，控制台保持简要进度。
3. **双通道输出**：控制台 handler 永远挂 ``file_only`` 过滤器（报文块
   不进控制台）；文件 handler 级别随模式切换；ERROR（含异常 traceback
   仅文件）两种模式都照常输出。
4. **低耦合**：本模块不依赖项目内任何其它模块，详情采集、AI 分析等
   后续模块可直接 ``import logger`` 复用。

典型用法
--------
    from src.common import logger

    logger.init(run_id, mode="quiet")      # 或 mode="debug"（默认 debug 兼容旧调用）
    log = logger.get_logger()
    log.info("[2/4] 列表接口完成")          # 控制台 + 文件（两种模式都可见）
    logger.file_only(log, raw_json)        # 仅 debug 模式文件（quiet 模式丢弃）
    logger.log_block(log, "响应明文", text) # 多行报文块，同上
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from src.settings import LOG_DIR  # 路径常量统一由 settings 管理（本模块只引用）

# 记录属性名：True 表示"只允许进文件，不进控制台"
_FILE_ONLY_ATTR = "file_only"

# 两套日志模式（见模块 docstring）：quiet=导入/Agent 生产路径，debug=CLI 逆向调试
MODE_QUIET = "quiet"
MODE_DEBUG = "debug"
_VALID_MODES = (MODE_QUIET, MODE_DEBUG)

_ROOT_NAME = "spider"
_TIME_FMT = "%Y-%m-%d %H:%M:%S"

_root_logger: logging.Logger | None = None
_run_id: str | None = None
_log_file: Path | None = None
_mode: str = MODE_DEBUG  # get_logger() 惰性 init() 的默认（兼容旧行为）


class _ConsoleOnlyFilter(logging.Filter):
    """挂在控制台 handler 上：拦下 file_only 记录，文件 handler 不受它影响。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, _FILE_ONLY_ATTR, False)


def _console_formatter() -> logging.Formatter:
    # 控制台走简短格式，方便人眼看进度
    return logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", _TIME_FMT)


def _file_formatter() -> logging.Formatter:
    # 文件里保留级别/记录器名，便于回溯是哪个模块写的
    return logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s", _TIME_FMT
    )


def init(
    run_id: str | None = None,
    log_dir: Path | str | None = None,
    mode: str = MODE_DEBUG,
) -> Path:
    """初始化（或复用）本次运行的根 logger，返回日志文件路径。

    :param run_id:   运行标识；不传则用 ``YYYYmmdd_HHMMSS``。
                     重复调用且 run_id、mode 一致时幂等，不重复建 handler。
    :param log_dir:  日志目录，默认项目下 ``logs/``。
    :param mode:     ``"quiet"``（导入/Agent 路径：INFO 级，DEBUG 全屏蔽）或
                     ``"debug"``（CLI __main__：DEBUG 级，密钥/报文/明文入文件）。
                     默认 ``debug`` 兼容未显式传模式的旧调用（如单元测试）。
    """
    global _root_logger, _run_id, _log_file, _mode

    if mode not in _VALID_MODES:
        raise ValueError(f"mode 必须是 {_VALID_MODES} 之一，收到: {mode!r}")

    directory = Path(log_dir) if log_dir else LOG_DIR
    new_run_id = run_id or time.strftime("%Y%m%d_%H%M%S")

    # 已初始化、同一次运行、同一模式、且 handler 还在 -> 幂等返回
    # （handler 被外部拆掉时会落到这里重建，避免"同 run_id 却没有输出"）
    if (
        _root_logger is not None
        and _run_id == new_run_id
        and _mode == mode
        and _root_logger.handlers
    ):
        return _log_file  # type: ignore[return-value]

    # 换了 run_id/mode：先拆掉旧 handler，避免同一进程多次运行时重复输出
    if _root_logger is not None:
        for handler in list(_root_logger.handlers):
            _root_logger.removeHandler(handler)
            handler.close()

    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / f"spider_{new_run_id}.log"

    root = logging.getLogger(_ROOT_NAME)
    # quiet: DEBUG 在 logger 层直接丢弃（连文件都不写）；debug: 全量放行
    root.setLevel(logging.INFO if mode == MODE_QUIET else logging.DEBUG)
    root.propagate = False  # 不往 python 根 logger 再冒泡，防止重复打印

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO if mode == MODE_QUIET else logging.DEBUG)
    file_handler.setFormatter(_file_formatter())

    console_handler = logging.StreamHandler(stream=sys.stdout)
    # 两种模式控制台都过滤 file_only（报文块永远不进控制台，只进 debug 文件）
    console_handler.setLevel(logging.INFO if mode == MODE_QUIET else logging.DEBUG)
    console_handler.setFormatter(_console_formatter())
    console_handler.addFilter(_ConsoleOnlyFilter())

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    _root_logger = root
    _run_id = new_run_id
    _log_file = log_path
    _mode = mode
    return log_path


def get_logger(name: str | None = None) -> logging.Logger:
    """拿到本次运行的 logger。

    :param name: 子模块名（如 ``"detail"``），返回 ``spider.detail``，
                 自动继承根 logger 的两个 handler；不传则返回根 logger。
    """
    if _root_logger is None:
        init()  # 惰性初始化，保证 import 后直接用也不会报错
    if not name:
        return _root_logger  # type: ignore[return-value]
    return logging.getLogger(f"{_ROOT_NAME}.{name}")


def current_run_id() -> str | None:
    """本次运行的 run_id（写 MySQL 备份行时用它对齐日志）。"""
    return _run_id


def current_log_file() -> Path | None:
    """本次运行的日志文件路径。"""
    return _log_file


def current_mode() -> str:
    """本次运行的日志模式（``"quiet"`` / ``"debug"``）。"""
    return _mode


def file_only(log: logging.Logger, msg: str) -> None:
    """DEBUG 级、只写文件的单行日志（完整报文用它）。"""
    log.debug(msg, extra={_FILE_ONLY_ATTR: True})


def log_block(log: logging.Logger, title: str, text: str) -> None:
    """把一段多行报文（JSON/HTML 片段）整块写进日志文件，控制台静默。"""
    border = "-" * 20 + f" {title} " + "-" * 20
    log.debug(border, extra={_FILE_ONLY_ATTR: True})
    log.debug(text, extra={_FILE_ONLY_ATTR: True})
    log.debug("-" * (len(title) + 44), extra={_FILE_ONLY_ATTR: True})


def log_exception(log: logging.Logger, exc: BaseException, headline: str = "未捕获异常") -> None:
    """异常留痕：``headline: repr(exc)`` 一行进控制台+文件，完整 traceback 仅进文件。

    供 ``run_crawl`` / CLI 顶层捕获等场景调用，保证任何未捕获异常都能
    在 ``logs/spider_<run_id>.log`` 里查到（控制台只打一行不刷屏）。
    """
    import traceback as _traceback

    log.error(f"{headline}: {exc!r}")
    log.error(
        "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__)),
        extra={_FILE_ONLY_ATTR: True},
    )
