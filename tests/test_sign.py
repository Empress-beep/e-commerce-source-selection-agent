import unittest

from src.collector.sign import build_sign, run_self_check


class SignTests(unittest.TestCase):
    def test_run_self_check_is_callable(self) -> None:
        run_self_check()

    def test_build_sign_defaults_to_empty_string(self) -> None:
        self.assertEqual(build_sign("x", {"page": 1}), "")
