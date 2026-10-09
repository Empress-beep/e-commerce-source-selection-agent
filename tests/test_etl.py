"""ETL 单测：api_backup 合成行 + 清洗口径 + UPSERT 模板（不连真实库）。"""

import json
import unittest

from src.collector.etl import (
    _f,
    _i,
    _ratio,
    build_product_info_rows,
    collect_sources,
)
from src.common import database


def _backup_row(category, api_name, keyword, product_id, decoded, page_no=None,
                api_url=None, http_status=None, decrypt_ok=1):
    return {
        "category": category,
        "api_name": api_name,
        "api_url": api_url,
        "keyword": keyword,
        "page_no": page_no,
        "product_id": product_id,
        "http_status": http_status,
        "decrypt_ok": decrypt_ok,
        "decoded_json": json.dumps(decoded, ensure_ascii=False)
        if not isinstance(decoded, str)
        else decoded,
    }


LIST_ROWS = [
    _backup_row(
        "list", "SearchFactory/GetPageList", "春季男鞋", None,
        {"Result": {"Items": [
            {
                "ProductId": 4476128,
                "Url": "https://www.17qcc.com/item/4476128.html",
                "ProductName": "卡片名-春季款",
                "Price": "48.00",
                "RetailPrice": 168.0,
                "Photo": "https://pics.17qcc.com/a.jpg",
                "BuyCount": 41,
                "YearSaleCount": 18,
                "ExposureCount": 100,
                "ClickCount": 20,
                "ShopName": "力度鞋业",
                "ShopNumber": "艺兴堂F09",
                "MarketName": "泉州幸福街",
                "MarketAreaName": "总站商贸城",
                "ShopAge": 7,
                "CreationTime": "2024-09-24",
                "SupplierId": 0,
                "H24DeliveryRate": 0.8,
            },
            {
                "ProductId": 4476129,
                "Url": "https://www.17qcc.com/item/4476129.html",
                "ProductName": "同店另一款",
                "Price": "55.00",
                "ShopName": "力度鞋业",
                "H24DeliveryRate": 0.8,
            },
        ]}},
        page_no=1,
    )
]

PAGE_ROWS = [
    _backup_row(
        "page", "GET /item/4476128.html", "春季男鞋", 4476128,
        {
            "product_name": "SSR标题",
            "wholesale_price": 48.0,
            "retail_price": 168.0,
            "tier_prices": [{"num": 1, "money": 48.0}],
            "colors": [{"vid": "28320", "name": "白色"}],
            "sku": [{"vid": "28320", "color": "白色",
                     "sizes": [{"name": "39", "price": 48.0, "stock": 997}]}],
            "stock_total": 997,
            "guest_render": False,
        },
        api_url="https://www.17qcc.com/item/4476128.html",
        http_status=200,
    )
]

CONTENT_ROWS = [
    _backup_row(
        "detail", "ProductFactory/GetProductContent", "春季男鞋", 4476128,
        {"Result": {
            "ProductCateName": "休闲板鞋",
            "ProductNo": "L860",
            "ProductComplaintType": [{"SysName": "品质投诉", "Status": 1}],
        }},
        api_url="https://newopenapiweb.17qcc.com/api/services/app/ProductFactory/GetProductContent",
        http_status=200,
    )
]

SHOP_ROWS = [
    _backup_row(
        "detail", "shopfactory/getshopandsupplier", "春季男鞋", 4476128,
        {"Result": {
            "RateTradeScore": "5",
            "PinRate": "11.40%",
            "StockOutRate": "0.30%",
            "ReturnGoodsRate": "1.40%",
            "BondBalance": "1000",
            "H12DeliveryRate": 0.326,
            "H24DeliveryRate": 0.848,
            "TotalProductCount": 914,
            "NewProductCount": 112,
            "ShopAge": 7,
            "QualificationList": [{"SupplierId": 8206, "Name": "营业执照"}],
        }},
        api_url="https://newopenapiweb.17qcc.com/api/services/app/shopfactory/getshopandsupplier",
        http_status=200,
    )
]

GRADE_ROWS = [
    _backup_row(
        "detail", "ProductFactory/GetProductGrade", "春季男鞋", 4476128,
        {"Result": {
            "OutOfStockRateJson": "[0.01,0.02,0.03,0.0,0.0,0.0,0.0]",
            "ReturnRateJson": "[0.0,0.0,0.0,0.0,0.0,0.0,0.0]",
            "CreationTimeJson": '["10.01","10.02","10.03","10.04","10.05","10.06","10.07"]',
            "MaxOutOfStockRate": 0.03,
            "MinOutOfStockRate": 0.0,
            "MaxReturnRate": 0.0,
            "MinReturnRate": 0.0,
        }},
        api_url="https://newopenapiweb.17qcc.com/api/services/app/ProductFactory/GetProductGrade",
        http_status=200,
    )
]

TRADE_ROWS = [
    _backup_row(
        "detail", "TradeFactory/TradeRecordGetPageList", "春季男鞋", 4476128,
        {"Result": {"Pages": 2, "TotalItems": 40, "Items": [
            {"TradeId": 1, "Quantity": 1.0, "ProductRate": {"ProductQualityScore": 5}},
            {"TradeId": 2, "Quantity": 2.0, "ProductRate": {"ProductQualityScore": 4}},
        ]}},
        api_url="https://newopenapiweb.17qcc.com/api/services/app/TradeFactory/TradeRecordGetPageList",
        http_status=200,
    ),
    _backup_row(
        "detail", "TradeFactory/TradeRecordGetPageList", "春季男鞋", 4476128,
        {"Result": {"Pages": 2, "TotalItems": 40, "Items": [
            {"TradeId": 3, "Quantity": 3.0, "ProductRate": {"ProductQualityScore": 5}},
        ]}},
        api_url="https://newopenapiweb.17qcc.com/api/services/app/TradeFactory/TradeRecordGetPageList",
        http_status=200,
    ),
    # 同一页被另一关键词重复采集 -> 按明文去重
    _backup_row(
        "detail", "TradeFactory/TradeRecordGetPageList", "", 4476128,
        {"Result": {"Pages": 2, "TotalItems": 40, "Items": [
            {"TradeId": 1, "Quantity": 1.0, "ProductRate": {"ProductQualityScore": 5}},
            {"TradeId": 2, "Quantity": 2.0, "ProductRate": {"ProductQualityScore": 4}},
        ]}},
        api_url="https://newopenapiweb.17qcc.com/api/services/app/TradeFactory/TradeRecordGetPageList",
        http_status=200,
    ),
]

ALL_ROWS = LIST_ROWS + PAGE_ROWS + CONTENT_ROWS + SHOP_ROWS + GRADE_ROWS + TRADE_ROWS


class CleanHelperTests(unittest.TestCase):
    def test_f(self) -> None:
        self.assertEqual(_f("48.00"), 48.0)
        self.assertEqual(_f(3), 3.0)
        self.assertIsNone(_f(None))
        self.assertIsNone(_f(True))
        self.assertIsNone(_f("abc"))

    def test_i(self) -> None:
        self.assertEqual(_i("41"), 41)
        self.assertIsNone(_i("x"))

    def test_ratio(self) -> None:
        self.assertAlmostEqual(_ratio("11.40%"), 0.114)
        self.assertAlmostEqual(_ratio("0.30%"), 0.003)
        self.assertAlmostEqual(_ratio(0.848), 0.848)
        self.assertIsNone(_ratio(None))


class CollectSourcesTests(unittest.TestCase):
    def test_split_by_category(self) -> None:
        src = collect_sources(ALL_ROWS)
        self.assertIn(("春季男鞋", 4476128), src["cards"])
        self.assertIn(4476128, src["pages"])
        self.assertIn(4476128, src["content"])
        self.assertIn(4476128, src["shop"])
        self.assertEqual(len(src["trade"][4476128]), 2)  # 重复页去重
        self.assertEqual(src["list_page_no"]["春季男鞋"], 1)
        # 追溯源: 详情页元信息 + 逐接口调用明细
        self.assertEqual(src["page_meta"][4476128]["url"],
                         "https://www.17qcc.com/item/4476128.html")
        self.assertEqual(src["page_meta"][4476128]["status"], 200)
        self.assertEqual(len(src["trace"][4476128]), 6)


class BuildRowsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = build_product_info_rows("run1", ALL_ROWS)
        cls.by_pid = {r["product_id"]: r for r in cls.rows}

    def test_row_universe(self) -> None:
        # 两张卡片 + SSR页 -> 两行（同关键词）
        self.assertEqual(len(self.rows), 2)
        self.assertEqual(
            {(r["keyword"], r["product_id"]) for r in self.rows},
            {("春季男鞋", 4476128), ("春季男鞋", 4476129)},
        )

    def test_ssr_detail_fields(self) -> None:
        r = self.by_pid[4476128]
        self.assertEqual(r["product_name"], "SSR标题")  # SSR 优先于卡片名
        self.assertEqual(r["wholesale_price"], 48.0)
        self.assertEqual(r["retail_price"], 168.0)
        self.assertEqual(r["stock_total"], 997)
        self.assertEqual(r["sku_stock"][0]["sizes"][0]["stock"], 997)

    def test_card_metrics(self) -> None:
        r = self.by_pid[4476128]
        self.assertEqual(r["buy_count"], 41)
        self.assertEqual(r["year_sale_count"], 18)
        self.assertEqual(r["exposure_count"], 100)
        self.assertEqual(r["click_count"], 20)
        self.assertEqual(r["card_price"], 48.0)
        self.assertEqual(r["product_url"], "https://www.17qcc.com/item/4476128.html")
        self.assertEqual(r["page_no"], 1)
        self.assertEqual(r["listing_time"], "2024-09-24")  # 上架时间=卡片CreationTime

    def test_shop_cleaning(self) -> None:
        r = self.by_pid[4476128]
        self.assertEqual(r["rate_trade_score"], 5.0)
        self.assertAlmostEqual(r["pin_rate"], 0.114)
        self.assertAlmostEqual(r["stock_out_rate"], 0.003)
        self.assertAlmostEqual(r["return_goods_rate"], 0.014)
        self.assertEqual(r["bond_balance"], 1000.0)
        self.assertAlmostEqual(r["h12_delivery_rate"], 0.326)
        self.assertAlmostEqual(r["h24_delivery_rate"], 0.848)  # 店铺接口优先于卡片
        self.assertEqual(r["supplier_id"], 8206)
        self.assertEqual(r["total_product_count"], 914)

    def test_content_and_grade(self) -> None:
        r = self.by_pid[4476128]
        self.assertEqual(r["cate_name"], "休闲板鞋")
        self.assertEqual(r["product_no"], "L860")
        self.assertEqual(r["complaint_types"], [{"SysName": "品质投诉", "Status": 1}])
        self.assertAlmostEqual(r["grade_out_of_stock_max"], 0.03)
        self.assertEqual(r["grade_trend"]["creation_time"][0], "10.01")

    def test_trade_aggregation(self) -> None:
        r = self.by_pid[4476128]
        self.assertEqual(r["trade_total_items"], 40)
        self.assertEqual(r["trade_sample_count"], 3)  # 2页, 重复页去重
        self.assertEqual(r["trade_qty_sum"], 6)
        self.assertAlmostEqual(r["trade_avg_quality"], 4.67)

    def test_data_source(self) -> None:
        self.assertEqual(
            self.by_pid[4476128]["data_source"],
            "list,page,content,shop,grade,trade",
        )
        self.assertEqual(self.by_pid[4476129]["data_source"], "list")

    def test_traceability(self) -> None:
        """企业追溯：商品id + 请求地址 + 状态码 + 逐接口调用明细。"""
        r = self.by_pid[4476128]
        self.assertEqual(r["request_url"], "https://www.17qcc.com/item/4476128.html")
        self.assertEqual(r["http_status"], 200)
        trace = r["api_trace"]
        self.assertEqual(len(trace), 6)  # page + content/shop/grade + trade x2(明文去重)
        self.assertTrue(all(e["http_status"] == 200 for e in trace))
        self.assertTrue(all(e["api_url"] for e in trace))
        names = [e["api_name"] for e in trace]
        self.assertIn("GET /item/4476128.html", names)
        # 未采详情的商品：请求地址兜底卡片 Url，状态码/追溯为 NULL
        r2 = self.by_pid[4476129]
        self.assertEqual(r2["request_url"], "https://www.17qcc.com/item/4476129.html")
        self.assertIsNone(r2["http_status"])
        self.assertIsNone(r2["api_trace"])

    def test_shop_propagation(self) -> None:
        # 4476129 没采店铺接口，但同店 -> 横向补齐画像
        r = self.by_pid[4476129]
        self.assertEqual(r["rate_trade_score"], 5.0)
        self.assertAlmostEqual(r["pin_rate"], 0.114)
        self.assertEqual(r["supplier_id"], 8206)

    def test_no_card_page_fallback(self) -> None:
        rows = build_product_info_rows("run1", PAGE_ROWS)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["keyword"], "")
        self.assertIsNone(rows[0]["buy_count"])
        self.assertEqual(rows[0]["wholesale_price"], 48.0)


class UpsertSqlTests(unittest.TestCase):
    def test_alias_syntax(self) -> None:
        sql = database.build_upsert_sql(
            "product_info",
            ["run_id", "keyword", "product_id", "buy_count"],
            ["buy_count"],
        )
        self.assertIn("AS `new`", sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertIn("`buy_count` = `new`.`buy_count`", sql)
        self.assertEqual(sql.count("%s"), 4)

    def test_rejects_unknown_update_col(self) -> None:
        with self.assertRaises(database.DatabaseError):
            database.build_upsert_sql("t", ["a"], ["b"])


if __name__ == "__main__":
    unittest.main()
