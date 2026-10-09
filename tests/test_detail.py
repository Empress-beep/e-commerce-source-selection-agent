import json
import unittest
from pathlib import Path

from src.collector import detail
from src.collector.detail import (
    DetailError,
    build_bundle_url,
    build_detail_params,
    build_grade_params,
    build_shop_params,
    build_trade_params,
    extract_access_c,
    extract_page_param,
    fetch_detail_page,
    fetch_tmp_ver,
    parse_detail_page,
    parse_product_detail,
    reset_tmp_ver_cache,
)
from src.collector.main import _extract_targets, _page_meta

# 真实登录态详情页快照（2026-10-07 抓取，含价格/SKU/库存）
FIXTURE_HTML = Path(__file__).with_name("fixtures_detail_4476128.html")

# 精简版详情页 HTML 夹具（字段与真实页面一致）
SAMPLE_HTML = """
<html><head>
<script nonce="x">var accessC = "16D59EADCE5D24C526B8056A5A3D5875";
        var moudles = [
            ['ajax', 'imglazy', 'pager', 'template', 'rollload', 'ajaxmethord', 'magnifier', 'detailproduct'],
            ['fly', 'yiqia', 'toolbar', 'shopcart', 'detect', 'qtcharts', 'qa']
        ];
        var pageParam = { "cateId": 201309910, "suppliereId": 8206, "shopId": 5847,
            "productId": 4476128, "PostageType": 2, "RetailPrice": 168.00 };
</script>
</head><body></body></html>
"""


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeSession:
    """最小假 session：记录请求 URL/头，按剧本返回响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []
        self.headers = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        self.headers.append(headers or {})
        return self.responses.pop(0)


class DetailPageParseTests(unittest.TestCase):
    def test_extract_access_c(self) -> None:
        self.assertEqual(
            extract_access_c(SAMPLE_HTML), "16D59EADCE5D24C526B8056A5A3D5875"
        )

    def test_extract_access_c_missing_raises(self) -> None:
        with self.assertRaises(DetailError):
            extract_access_c("<html>没有 accessC</html>")

    def test_extract_page_param(self) -> None:
        param = extract_page_param(SAMPLE_HTML)
        self.assertEqual(param["suppliereId"], 8206)
        self.assertEqual(param["PostageType"], 2)
        self.assertEqual(param["productId"], 4476128)

    def test_parse_detail_page(self) -> None:
        page = parse_detail_page("https://www.17qcc.com/item/4476128.html", SAMPLE_HTML)
        self.assertEqual(page.supplier_id, 8206)
        self.assertEqual(page.postage_type, 2)
        self.assertEqual(page.access_c, "16D59EADCE5D24C526B8056A5A3D5875")
        self.assertTrue(page.url.startswith("https://www.17qcc.com/item/"))

    def test_page_param_absent_is_empty_dict(self) -> None:
        self.assertEqual(extract_page_param("<html></html>"), {})


class TmpVerTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_tmp_ver_cache()

    def tearDown(self) -> None:
        reset_tmp_ver_cache()

    def test_bundle_url_picks_detailproduct_group(self) -> None:
        url = build_bundle_url(SAMPLE_HTML)
        self.assertEqual(
            url,
            "https://j.17qcc.com/combo?"
            "ajax&imglazy&pager&template&rollload&ajaxmethord&magnifier&detailproduct",
        )

    def test_bundle_url_fallback_single_module(self) -> None:
        self.assertEqual(
            build_bundle_url("<html>没有 moudles</html>"),
            "https://j.17qcc.com/combo?detailproduct",
        )

    def test_tmp_ver_from_bundle_not_hardcoded(self) -> None:
        session = FakeSession([FakeResponse(text='productDetail={tmpVer:"99887766",x:1}')])
        self.assertEqual(fetch_tmp_ver(html=SAMPLE_HTML, session=session), "99887766")
        self.assertIn("detailproduct", session.urls[0])

    def test_tmp_ver_cached_across_calls(self) -> None:
        session = FakeSession([FakeResponse(text='tmpVer:"11223344"')])
        fetch_tmp_ver(html=SAMPLE_HTML, session=session)
        again = fetch_tmp_ver(html=SAMPLE_HTML, session=FakeSession([]))  # 无网络
        self.assertEqual(again, "11223344")

    def test_tmp_ver_inline_html_short_circuit(self) -> None:
        html = '<script>productDetail={tmpVer:"55667788"}</script>'
        val = fetch_tmp_ver(html=html, session=FakeSession([]))
        self.assertEqual(val, "55667788")

    def test_tmp_ver_failure_raises(self) -> None:
        session = FakeSession([FakeResponse(text="// 没有 tmpVer 定义")])
        with self.assertRaises(DetailError):
            fetch_tmp_ver(html=SAMPLE_HTML, session=session)

    def test_tmp_ver_no_hardcoded_constant(self) -> None:
        # 报告确认项 2：模块里不允许再出现旧的硬编码常量
        import pathlib

        src = pathlib.Path(detail.__file__).read_text(encoding="utf-8")
        self.assertNotIn('TMP_VER = "', src)
        self.assertNotIn('tmp_ver: str = "', src)


class ParamBuilderTests(unittest.TestCase):
    def test_detail_params_uses_passed_tmp_ver(self) -> None:
        params = build_detail_params(4476128, "ABCD", "2099010101")
        self.assertEqual(
            list(params), ["ProductId", "TmpVer", "Access"]
        )
        self.assertEqual(params["TmpVer"], "2099010101")
        self.assertEqual(params["ProductId"], 4476128)

    def test_detail_params_rejects_empty_tmp_ver(self) -> None:
        with self.assertRaises(DetailError):
            build_detail_params(1, "ABCD", "")

    def test_shop_params_from_page_param(self) -> None:
        params = build_shop_params(
            {"suppliereId": 8206, "PostageType": 2}, "ABCD", "2099010101"
        )
        self.assertEqual(
            list(params), ["SupplierId", "PostageType", "TmpVer", "Access"]
        )
        self.assertEqual(params["SupplierId"], 8206)
        self.assertEqual(params["PostageType"], 2)

    def test_shop_params_requires_supplier(self) -> None:
        with self.assertRaises(DetailError):
            build_shop_params({}, "ABCD", "2099010101")

    def test_grade_and_trade_params(self) -> None:
        self.assertEqual(build_grade_params("4476128"), {"ProductId": 4476128})
        self.assertEqual(
            build_trade_params(4476128),
            {"ProductId": 4476128, "Page": 1, "TotalItems": 0},
        )

    def test_trade_page2_uses_loaded_count(self) -> None:
        # 第2页 TotalItems=第1页实际条数（前端"已加载"语义，实测发0回空）
        self.assertEqual(
            build_trade_params(4476128, page=2, total_items=19),
            {"ProductId": 4476128, "Page": 2, "TotalItems": 19},
        )


class ParseProductDetailTests(unittest.TestCase):
    """当前商品详情 = 登录态 SSR HTML 解析（无对应接口）。"""

    PAGE_SAMPLE = """
    <html><head><title>2025春季新款港风复古休闲板鞋-青创网</title></head><body>
    <div class="pro_color cleafix">
        <span class="title">颜色分类</span>
        <ul>
            <li vid="28320" class='J_clr'><a title="白色"><img src="x.jpg"/></a></li>
            <li vid="28321" class='J_clr'><a title="黑色"><img src="y.jpg"/></a></li>
        </ul>
    </div>
    <div class="skuinfo">
        <div class="pro_size cleafix" vid="28320">
            <span class="title">尺码</span>
            <ul class="pro_size_item">
                <li><span class="size_name" title="39">39</span>
                    <span class="size_price" price="48.00">48.00元</span>
                    <span class="size_sale">约997件可售</span>
                    <div class="size_num"><span></span></div></li>
                <li><span class="size_name" title="40">40</span>
                    <span class="size_price" price="48.00">48.00元</span>
                    <span class="size_sale">约999件可售</span></li>
            </ul>
        </div>
        <div class="pro_size cleafix" vid="28321">
            <ul class="pro_size_item">
                <li><span class="size_name" title="39">39</span>
                    <span class="size_price" price="48.00">48.00元</span>
                    <span class="size_sale">约5件可售</span></li>
            </ul>
        </div>
    </div>
    <dl class="pri_de"><dd><p class="pri_de_price"><span>￥</span><em>48.00</em></p>
        <p class="pri_de_num"><span>1件</span></p></dd></dl>
    <div data-mixprice='[{"Num":1,"Money":48.0},{"Num":10,"Money":45.0}]'></div>
    </body></html>
    """

    def test_parse_full_page(self) -> None:
        parsed = parse_product_detail(self.PAGE_SAMPLE, {"RetailPrice": 168.0})
        self.assertEqual(parsed["product_name"], "2025春季新款港风复古休闲板鞋")
        self.assertEqual(parsed["wholesale_price"], 48.0)
        self.assertEqual(parsed["retail_price"], 168.0)
        self.assertEqual(
            parsed["tier_prices"],
            [{"num": 1, "money": 48.0}, {"num": 10, "money": 45.0}],
        )
        self.assertEqual(
            parsed["colors"], [{"vid": "28320", "name": "白色"},
                               {"vid": "28321", "name": "黑色"}]
        )
        self.assertEqual(len(parsed["sku"]), 2)
        first = parsed["sku"][0]
        self.assertEqual(first["color"], "白色")
        self.assertEqual(
            first["sizes"],
            [{"name": "39", "price": 48.0, "stock": 997},
             {"name": "40", "price": 48.0, "stock": 999}],
        )
        self.assertEqual(parsed["stock_total"], 997 + 999 + 5)
        self.assertFalse(parsed["guest_render"])

    def test_guest_placeholder_detected(self) -> None:
        # 游客版页面（无价格/SKU 文本）必须标 guest_render
        parsed = parse_product_detail("<html><head><title>商品-青创网</title></head></html>")
        self.assertIsNone(parsed["wholesale_price"])
        self.assertEqual(parsed["colors"], [])
        self.assertEqual(parsed["sku"], [])
        self.assertTrue(parsed["guest_render"])

    def test_real_fixture(self) -> None:
        """真实登录态快照：价格/SKU/库存与线上一致（2026-10-07 实抓）。"""
        if not FIXTURE_HTML.exists():
            self.skipTest("缺真实详情页夹具")
        parsed = parse_product_detail(
            FIXTURE_HTML.read_text(encoding="utf-8", errors="replace"),
            {"RetailPrice": 168.0},
        )
        self.assertFalse(parsed["guest_render"])
        self.assertEqual(parsed["wholesale_price"], 48.0)
        self.assertEqual(parsed["retail_price"], 168.0)
        self.assertTrue(parsed["product_name"].startswith("2025春季新款港风"))
        self.assertEqual(
            [c["name"] for c in parsed["colors"]], ["白色", "黑色", "卡其"]
        )
        self.assertTrue(parsed["stock_total"] and parsed["stock_total"] > 9000)

    def test_fetch_detail_page_sends_cookie(self) -> None:
        session = FakeSession([FakeResponse(text=SAMPLE_HTML)])
        fetch_detail_page("https://www.17qcc.com/item/1.html",
                          session=session, cookie="token=abc")
        self.assertEqual(session.headers[0].get("Cookie"), "token=abc")
        session2 = FakeSession([FakeResponse(text=SAMPLE_HTML)])
        fetch_detail_page("https://www.17qcc.com/item/1.html", session=session2)
        self.assertNotIn("Cookie", session2.headers[0])


class ExtractTargetsTests(unittest.TestCase):
    """搜索接口只做目标发现：仅 ProductId/Url，不夹带商品信息字段。"""

    def test_only_product_id_and_url(self) -> None:
        text = json.dumps(
            {
                "Result": {
                    "Items": [
                        {
                            "ProductId": 4476128,
                            "Url": "https://www.17qcc.com/item/4476128.html",
                            "ProductName": "不该被取走",
                            "Price": "48.00",
                        }
                    ]
                }
            }
        )
        self.assertEqual(
            _extract_targets(text, 5),
            [{"product_id": 4476128, "url": "https://www.17qcc.com/item/4476128.html"}],
        )

    def test_skips_invalid_items(self) -> None:
        text = json.dumps(
            {"Result": {"Items": [{"ProductId": 0, "Url": ""}, "junk"]}}
        )
        self.assertEqual(_extract_targets(text, 5), [])

    def test_page_meta_items_dict_result(self) -> None:
        meta = _page_meta(
            json.dumps({"Result": {"Items": [{}, {}], "TotalCount": 83}})
        )
        self.assertEqual(meta["item_count"], 2)
        self.assertEqual(meta["TotalCount"], 83)


if __name__ == "__main__":
    unittest.main()
