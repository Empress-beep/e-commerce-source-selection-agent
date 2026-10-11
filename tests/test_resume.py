# -*- coding: utf-8 -*-
"""断点续爬：api_backup 成功行 -> 断点快照推导（纯函数单测）。

覆盖：_build_checkpoint 归并逻辑 / _product_done 域粒度判定 /
parse_args --resume / --no-db 拒绝续跑 /
run_crawl(resume=) argv 透传 / Agent 工具 schema+执行器。
"""
import unittest
from unittest import mock

import src.collector.main as collector_main
from src.collector.main import (
    _API_NAME_TO_KEY,
    _build_checkpoint,
    _product_done,
    DETAIL_APIS,
    main,
    parse_args,
)


def _rows():
    return [
        # 列表成功行（keyword 归入已完成列表）
        {"category": "list", "api_name": "SearchFactory/GetPageList",
         "keyword": "春季运动鞋", "product_id": None, "item_count": 83},
        # 列表 Result=null 失效行（item_count NULL -> builder 兜底过滤）
        {"category": "list", "api_name": "SearchFactory/GetPageList",
         "keyword": "复古休闲男鞋", "product_id": None, "item_count": None},
        # 商品 1：page + content + trade 完成，缺 shop/grade
        {"category": "page", "api_name": "GET /item/4476128.html",
         "keyword": "春季运动鞋", "product_id": 4476128, "item_count": 2},
        {"category": "detail", "api_name": DETAIL_APIS["content"][0],
         "keyword": "春季运动鞋", "product_id": 4476128, "item_count": None},
        {"category": "detail", "api_name": DETAIL_APIS["trade"][0],
         "keyword": "春季运动鞋", "product_id": 4476128, "item_count": 20},
        # 商品 2：只有 shop 详情行（无 page，孤儿行——page 抓取失败但域接口成功的场景）
        {"category": "detail", "api_name": DETAIL_APIS["shop"][0],
         "keyword": "春季运动鞋", "product_id": 5355915, "item_count": None},
    ]


class BuildCheckpointTest(unittest.TestCase):
    def test_mixed_rows(self):
        cp = _build_checkpoint(_rows())
        self.assertIn("春季运动鞋", cp["list_keywords"])
        # item_count NULL 的列表行不计入
        self.assertNotIn("复古休闲男鞋", cp["list_keywords"])
        p1 = cp["products"][4476128]
        self.assertTrue(p1.get("page"))
        self.assertTrue(p1.get("content"))
        self.assertTrue(p1.get("trade"))
        self.assertFalse(p1.get("shop"))
        self.assertFalse(p1.get("grade"))
        # 孤儿 detail 行也归并（判定时因缺 page 仍算未完成）
        self.assertIn(5355915, cp["products"])

    def test_empty_rows(self):
        cp = _build_checkpoint([])
        self.assertEqual(cp["list_keywords"], set())
        self.assertEqual(cp["products"], {})

    def test_api_name_to_key_covers_all(self):
        self.assertEqual(set(_API_NAME_TO_KEY), {v[0] for v in DETAIL_APIS.values()})


class ProductDoneTest(unittest.TestCase):
    def setUp(self):
        self.cp = _build_checkpoint(_rows())

    def test_partial_product(self):
        # 商品 1：page+content+trade -> 要求 content/trade 时算完成
        self.assertTrue(_product_done(self.cp, 4476128, ["content", "trade"]))
        # 缺 shop/grade 时未完成
        self.assertFalse(_product_done(self.cp, 4476128, ["content", "shop"]))
        self.assertFalse(
            _product_done(self.cp, 4476128, ["content", "shop", "grade", "trade"])
        )

    def test_no_page_never_done(self):
        # 商品 2：有 shop 但无 page -> 任何要求下都未完成
        self.assertFalse(_product_done(self.cp, 5355915, ["shop"]))
        self.assertFalse(_product_done(self.cp, 5355915, []))

    def test_unknown_product(self):
        self.assertFalse(_product_done(self.cp, 99999999, ["content"]))

    def test_wanted_empty_requires_page(self):
        self.assertTrue(_product_done(self.cp, 4476128, []))  # page 已完成


class ResumeArgTest(unittest.TestCase):
    def test_resume_arg(self):
        args = parse_args(["--resume", "20260101_000000"])
        self.assertEqual(args.resume, "20260101_000000")

    def test_resume_default_empty(self):
        args = parse_args([])
        self.assertEqual(args.resume, "")


class NoDbRefuseTest(unittest.TestCase):
    def test_no_db_refused(self):
        # --no-db 下 --resume 必须拒绝（断点在 DB），退出码 2，且不发任何网络请求
        code = main(["--resume", "20990101_000000", "--no-db"])
        self.assertEqual(code, 2)


class RunCrawlResumeArgvTest(unittest.TestCase):
    """Agent 入口 run_crawl(resume=) -> main() argv 透传。"""

    def test_resume_builds_resume_argv(self):
        with mock.patch.object(collector_main, "main", return_value=0) as m_main, \
             mock.patch.object(collector_main, "_count_rows", return_value=0):
            r = collector_main.run_crawl(
                "春季运动鞋", limit=3, cateid="0", resume="20261011_111424"
            )
        argv = m_main.call_args[0][0]
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], "20261011_111424")
        self.assertNotIn("--run-id", argv)
        self.assertEqual(r["run_id"], "20261011_111424")
        self.assertIn("断点续跑", r["message"])

    def test_normal_run_uses_new_run_id(self):
        with mock.patch.object(collector_main, "main", return_value=0) as m_main, \
             mock.patch.object(collector_main, "_count_rows", return_value=0):
            r = collector_main.run_crawl("春季运动鞋", limit=3)
        argv = m_main.call_args[0][0]
        self.assertNotIn("--resume", argv)
        self.assertIn("--run-id", argv)
        self.assertEqual(argv[argv.index("--run-id") + 1], r["run_id"])
        self.assertNotIn("断点续跑", r["message"])


class AgentToolResumeTest(unittest.TestCase):
    """function_call_agent：工具 schema 声明 resume + 执行器透传。"""

    def test_schema_has_resume_param(self):
        from src.agent.function_call_agent import TOOLS
        tool = next(
            t["function"] for t in TOOLS if t["function"]["name"] == "run_crawl"
        )
        self.assertIn("resume", tool["parameters"]["properties"])

    def test_executor_passes_resume(self):
        from src.agent import function_call_agent as fca

        def fake_run_crawl(keyword, page=1, limit=20, cateid="0", resume=""):
            return {
                "success": True, "count": 2, "message": "ok",
                "run_id": resume or "generated", "backup_count": 3,
            }

        state = {
            "last_crawl_ts": None, "crawl_runs": [],
            "run_id": None, "run_ids": [],
            "terminated": False, "terminate_reason": None,
        }
        with mock.patch.object(fca, "run_crawl", fake_run_crawl):
            result = fca._execute_tool(
                "run_crawl",
                {"keyword": "春季运动鞋", "resume": "20261011_111424"},
                state,
            )
        self.assertEqual(result["run_id"], "20261011_111424")
        self.assertEqual(state["run_ids"], ["20261011_111424"])


if __name__ == "__main__":
    unittest.main()
