import unittest

from src.collector.pipeline import normalize_items, stable_key


class PipelineTests(unittest.TestCase):
    def test_stable_key_prefers_known_identifiers(self) -> None:
        self.assertEqual(stable_key({"bm": "123", "name": "x"}), "bm:123")

    def test_normalize_items_dedupes(self) -> None:
        items = [{"id": 1}, {"id": 1}, {"id": 2}]
        self.assertEqual(len(normalize_items(items)), 2)
