"""运行日志留痕测试：爬虫未捕获异常 / Agent 控制台双写必须落盘。"""

from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.common import logger as run_logger  # noqa: E402
from src.collector.main import _log_uncaught  # noqa: E402


class _Capture(logging.Handler):
    """截取控制台通道的记录（验证 headline 进控制台、traceback 不进）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _close_run_handlers() -> None:
    """关闭 run 日志 handler（Windows 下 FileHandler 不关会锁死临时目录）。"""
    root = logging.getLogger("spider")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


class _RunLogFileMixin(unittest.TestCase):
    """init run 日志到临时目录，用例结束后先关 handler 再删目录（防 Windows 文件锁）。"""

    run_id = "log_test_run"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        run_logger.init(run_id=self.run_id, log_dir=self.tmp.name)
        self.log_path = Path(self.tmp.name) / f"spider_{self.run_id}.log"

    def tearDown(self) -> None:
        _close_run_handlers()
        self.tmp.cleanup()


class LogExceptionTests(_RunLogFileMixin):
    run_id = "exc_test"

    def test_error_line_to_console_and_traceback_to_file(self) -> None:
        root = run_logger.get_logger()
        # 复用控制台 handler 的过滤器：file_only 记录不进"控制台"采集器
        console = _Capture()
        for handler in root.handlers:
            if not isinstance(handler, logging.FileHandler):
                for flt in handler.filters:
                    console.addFilter(flt)
        root.addHandler(console)
        try:
            try:
                raise ValueError("boom-exception")
            except ValueError as exc:
                run_logger.log_exception(root, exc, headline="采集异常终止")
        finally:
            root.removeHandler(console)

        file_text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("采集异常终止: ValueError('boom-exception')", file_text)
        self.assertIn("Traceback (most recent call last)", file_text)
        self.assertIn("ValueError: boom-exception", file_text)
        # 控制台通道只收一行 headline，不收 traceback
        console_text = "".join(r.getMessage() for r in console.records)
        self.assertIn("采集异常终止: ValueError('boom-exception')", console_text)
        self.assertNotIn("Traceback", console_text)


class LogUncaughtTests(_RunLogFileMixin):
    run_id = "uncaught_a"

    def test_logger_channel_when_run_active(self) -> None:
        try:
            raise KeyError("missing-key")
        except KeyError as exc:
            _log_uncaught(self.run_id, "采集异常终止 keyword=测试", exc)
        text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("采集异常终止 keyword=测试: KeyError('missing-key')", text)
        self.assertIn("Traceback (most recent call last)", text)

    def test_fallback_direct_append_when_handler_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                raise RuntimeError("handler-not-ready")
            except RuntimeError as exc:
                # run_id 与当前 logger 不一致 + 指定 log_dir -> 直接追加写文件
                _log_uncaught("uncaught_b", "采集异常终止 keyword=兜底", exc, log_dir=tmp)
            path = Path(tmp) / "spider_uncaught_b.log"
            self.assertTrue(path.exists())
            text = path.read_text(encoding="utf-8")
            self.assertIn("采集异常终止 keyword=兜底: RuntimeError('handler-not-ready')", text)
            self.assertIn("Traceback (most recent call last)", text)

    def test_fallback_appends_not_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(2):
                try:
                    raise OSError(f"err-{i}")
                except OSError as exc:
                    _log_uncaught("uncaught_c", "重复写入", exc, log_dir=tmp)
            text = (Path(tmp) / "spider_uncaught_c.log").read_text(encoding="utf-8")
            self.assertEqual(text.count("重复写入"), 2)


class AgentTeeTests(unittest.TestCase):
    def test_start_log_captures_print_and_restores_streams(self) -> None:
        from src.agent import function_call_agent as fca

        out_before, err_before = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent_test.log"
            try:
                fca._start_agent_log("测试指令", log_path=path)
                print("agent-tee-行-应进日志")
                sys.stderr.write("agent-stderr-行\n")
                self.assertIsNot(sys.stdout, out_before)
            finally:
                fca._stop_agent_log()
            # 流已恢复
            self.assertIs(sys.stdout, out_before)
            self.assertIs(sys.stderr, err_before)
            text = path.read_text(encoding="utf-8")
            self.assertIn("测试指令", text)
            self.assertIn("agent-tee-行-应进日志", text)
            self.assertIn("agent-stderr-行", text)
            self.assertIn("运行日志已写入", text)
        # 停止后状态清空，重复 stop 安全
        self.assertIsNone(fca._stop_agent_log())


if __name__ == "__main__":
    unittest.main()
