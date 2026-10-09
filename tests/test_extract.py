import unittest

from src.collector.extract import extract_items


class ExtractTests(unittest.TestCase):
    def test_extract_items_handles_nested_dict_payloads(self) -> None:
        payload = {"data": {"list": [{"id": 1}, {"id": 2}]}}
        self.assertEqual(len(extract_items(payload)), 2)
