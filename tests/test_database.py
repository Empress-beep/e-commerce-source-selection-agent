import json
import os
import unittest

from src.common import database
from src.settings import SQL_DIR

SQL_FILE = SQL_DIR / "backup_tables.sql"


class InsertSqlTests(unittest.TestCase):
    def test_build_insert_sql_is_parameterized(self) -> None:
        sql, params = database.build_insert_sql(
            "api_backup", {"api_name": "SearchFactory/GetPageList", "item_count": 83}
        )
        self.assertIn("INSERT INTO `api_backup`", sql)
        self.assertIn("`api_name`, `item_count`", sql)
        self.assertEqual(sql.count("%s"), 2)
        self.assertEqual(params, ["SearchFactory/GetPageList", 83])

    def test_build_insert_sql_normalizes_values(self) -> None:
        _, params = database.build_insert_sql(
            "api_backup", {"flag": True, "meta": {"a": 1}, "raw_json": "x"}
        )
        self.assertEqual(params[0], 1)  # bool -> 0/1
        self.assertEqual(params[1], json.dumps({"a": 1}, ensure_ascii=False))  # dict -> JSON
        self.assertEqual(params[2], "x")

    def test_rejects_bad_table_name(self) -> None:
        with self.assertRaises(database.DatabaseError):
            database.build_insert_sql("api_backup; DROP TABLE x", {"a": 1})

    def test_rejects_bad_column_name(self) -> None:
        with self.assertRaises(database.DatabaseError):
            database.build_insert_sql("api_backup", {"a`b": 1})

    def test_rejects_empty_row(self) -> None:
        with self.assertRaises(database.DatabaseError):
            database.build_insert_sql("api_backup", {})


class SqlFileTests(unittest.TestCase):
    def test_split_statements_skips_comments(self) -> None:
        text = "-- comment\nCREATE TABLE t (\n  a INT\n);\n\nUSE spider_db;\n"
        stmts = database.split_sql_statements(text)
        self.assertEqual(len(stmts), 2)
        self.assertTrue(stmts[0].startswith("CREATE TABLE"))
        self.assertEqual(stmts[1], "USE spider_db")

    def test_backup_sql_file_parses_into_three_statements(self) -> None:
        stmts = database.split_sql_statements(SQL_FILE.read_text(encoding="utf-8"))
        self.assertEqual(len(stmts), 3)
        self.assertIn("CREATE DATABASE IF NOT EXISTS", stmts[0])
        self.assertIn("CREATE TABLE IF NOT EXISTS `api_backup`", stmts[2])
        self.assertIn("decoded_json", stmts[2])


class ConfigTests(unittest.TestCase):
    def test_config_reads_env_overrides(self) -> None:
        old = os.environ.get("DB_HOST")
        try:
            os.environ["DB_HOST"] = "10.0.0.9"
            cfg = database.config()
            self.assertEqual(cfg["host"], "10.0.0.9")
            self.assertEqual(cfg["database"], "spider_db")
            self.assertEqual(cfg["charset"], "utf8mb4")
        finally:
            if old is None:
                os.environ.pop("DB_HOST", None)
            else:
                os.environ["DB_HOST"] = old


if __name__ == "__main__":
    unittest.main()
