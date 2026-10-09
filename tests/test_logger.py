import logging
import tempfile
import unittest
from pathlib import Path

from src.common import logger as run_logger


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class LoggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = run_logger.init(run_id="unit_test_run", log_dir=self.tmp.name)

    def tearDown(self) -> None:
        root = logging.getLogger("spider")
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        self.tmp.cleanup()

    def _console_handler(self) -> logging.Handler:
        for handler in logging.getLogger("spider").handlers:
            if not isinstance(handler, logging.FileHandler):
                return handler
        raise AssertionError("未找到控制台 handler")

    def test_creates_run_scoped_log_file(self) -> None:
        self.assertTrue(Path(self.log_path).exists())
        self.assertEqual(Path(self.log_path).name, "spider_unit_test_run.log")
        self.assertEqual(run_logger.current_run_id(), "unit_test_run")

    def test_file_only_record_reaches_file_not_console(self) -> None:
        console = self._console_handler()
        cap = _Capture()
        for flt in console.filters:
            cap.addFilter(flt)  # 复用"控制台只放行非 file_only"的过滤器

        root = logging.getLogger("spider")
        root.addHandler(cap)

        log = run_logger.get_logger()
        log.info("进度行-应到控制台")
        run_logger.file_only(log, "密文-仅文件")
        run_logger.log_block(log, "标题", "第一行\n第二行")

        texts = [r.getMessage() for r in cap.records]
        self.assertIn("进度行-应到控制台", texts)
        self.assertNotIn("密文-仅文件", texts)
        self.assertNotIn("第一行\n第二行", texts)

        file_text = Path(self.log_path).read_text(encoding="utf-8")
        self.assertIn("进度行-应到控制台", file_text)
        self.assertIn("密文-仅文件", file_text)
        self.assertIn("第一行", file_text)

    def test_named_child_logger_shares_run_file(self) -> None:
        detail_log = run_logger.get_logger("detail")
        detail_log.info("详情模块进度")
        detail_log.debug("详情密文", extra={run_logger._FILE_ONLY_ATTR: True})

        file_text = Path(self.log_path).read_text(encoding="utf-8")
        self.assertIn("详情模块进度", file_text)
        self.assertIn("[spider.detail]", file_text)  # 文件里带记录器名，便于回溯
        self.assertIn("详情密文", file_text)


class QuietModeTests(unittest.TestCase):
    """quiet 模式（Agent 导入 run_crawl 路径）：INFO 级，DEBUG 全屏蔽。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = run_logger.init(
            run_id="quiet_run", log_dir=self.tmp.name, mode="quiet"
        )

    def tearDown(self) -> None:
        root = logging.getLogger("spider")
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        self.tmp.cleanup()

    def test_debug_and_payload_records_fully_suppressed(self) -> None:
        log = run_logger.get_logger()
        self.assertEqual(run_logger.current_mode(), "quiet")
        log.debug("调试行-不应落盘")
        run_logger.file_only(log, "密钥iv-不应落盘")
        run_logger.log_block(log, "首页密钥材料", '{"aes_key":"SECRET_KEY_XYZ"}')

        text = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn("调试行-不应落盘", text)
        self.assertNotIn("密钥iv-不应落盘", text)
        self.assertNotIn("SECRET_KEY_XYZ", text)
        self.assertNotIn(" DEBUG ", text)  # 文件里一条 DEBUG 记录都没有

    def test_info_key_points_and_error_traceback_written(self) -> None:
        log = run_logger.get_logger()
        log.info("[1/5] 运行开始 run_id=quiet_run 关键词=1个 [春季老爹鞋男]")
        log.info("      HTTP=200 登录=1 解密=成功 条目=21 备份行=557")
        try:
            raise ValueError("boom-quiet")
        except ValueError as exc:
            run_logger.log_exception(log, exc, headline="采集异常终止")

        text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("[1/5] 运行开始 run_id=quiet_run", text)
        self.assertIn("条目=21", text)
        self.assertIn("采集异常终止: ValueError('boom-quiet')", text)
        self.assertIn("Traceback (most recent call last)", text)  # ERROR 堆栈两模式都在
        # 时间 + 级别字段保留
        self.assertRegex(
            text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO {2,}\[spider\]"
        )

    def test_console_gets_info_not_debug(self) -> None:
        cap = _Capture()
        for handler in logging.getLogger("spider").handlers:
            if not isinstance(handler, logging.FileHandler):
                for flt in handler.filters:
                    cap.addFilter(flt)
        logging.getLogger("spider").addHandler(cap)
        try:
            log = run_logger.get_logger()
            log.debug("控制台-debug-不应出现")
            log.info("控制台-info-应出现")
        finally:
            logging.getLogger("spider").removeHandler(cap)
        texts = [r.getMessage() for r in cap.records]
        self.assertIn("控制台-info-应出现", texts)
        self.assertNotIn("控制台-debug-不应出现", texts)


class DebugModeTests(unittest.TestCase):
    """debug 模式（python -m src.collector.main __main__ 路径）：DEBUG 全量入文件。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = run_logger.init(run_id="debug_run", log_dir=self.tmp.name)

    def tearDown(self) -> None:
        root = logging.getLogger("spider")
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        self.tmp.cleanup()

    def test_debug_payload_reaches_file_not_console(self) -> None:
        self.assertEqual(run_logger.current_mode(), "debug")
        console = None
        for handler in logging.getLogger("spider").handlers:
            if not isinstance(handler, logging.FileHandler):
                console = handler
        self.assertIsNotNone(console)
        cap = _Capture()
        for flt in console.filters:
            cap.addFilter(flt)
        logging.getLogger("spider").addHandler(cap)
        try:
            log = run_logger.get_logger()
            run_logger.log_block(log, "首页密钥材料", '{"iv":"BLOCK_IV_VALUE"}')
            run_logger.file_only(log, "调试行-仅文件")
            log.debug("调试行-控制台简要进度")
        finally:
            logging.getLogger("spider").removeHandler(cap)

        file_text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("BLOCK_IV_VALUE", file_text)
        self.assertIn("调试行-仅文件", file_text)
        self.assertIn("DEBUG", file_text)
        cap_texts = [r.getMessage() for r in cap.records]
        self.assertIn("调试行-控制台简要进度", cap_texts)  # 非 file_only 的 DEBUG 进控制台
        self.assertNotIn("调试行-仅文件", cap_texts)
        self.assertNotIn("BLOCK_IV_VALUE", cap_texts)  # 报文块永不进控制台


class ModeSwitchTests(unittest.TestCase):
    """同 run_id 换 mode 必须重建 handler（幂等判断含 mode）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        root = logging.getLogger("spider")
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        self.tmp.cleanup()

    def test_same_run_id_switch_mode_rebuilds(self) -> None:
        path = run_logger.init(run_id="switch_run", log_dir=self.tmp.name, mode="quiet")
        self.assertEqual(run_logger.current_mode(), "quiet")
        run_logger.get_logger().debug("quiet下被屏蔽")
        self.assertNotIn("quiet下被屏蔽", Path(path).read_text(encoding="utf-8"))

        run_logger.init(run_id="switch_run", log_dir=self.tmp.name, mode="debug")
        self.assertEqual(run_logger.current_mode(), "debug")
        run_logger.get_logger().debug("debug下放行")
        self.assertIn("debug下放行", Path(path).read_text(encoding="utf-8"))
        # 同名文件只有一个（不重复建 file handler）
        self.assertEqual(len(list(Path(self.tmp.name).glob("spider_*.log"))), 1)

    def test_invalid_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            run_logger.init(run_id="bad_mode", log_dir=self.tmp.name, mode="verbose")


class CliLogModeTests(unittest.TestCase):
    """入口判定：导入（Agent run_crawl）-> quiet，__main__（CLI 调试）-> debug。"""

    def test_imported_module_uses_quiet_mode(self) -> None:
        import src.collector.main as collector_main

        self.assertFalse(collector_main._CLI_MODE)
        self.assertEqual(collector_main._log_mode(), "quiet")


if __name__ == "__main__":
    unittest.main()
