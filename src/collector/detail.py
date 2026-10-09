"""详情页采集：SSR HTML 解析当前商品详情 + 四个加密信封接口。

当前商品详情的来源（与列表接口字段互补，不重复）
------------------------------------------------
* **详情页 SSR HTML 解析**（:func:`parse_product_detail`）—— 批发价/阶梯价/
  颜色尺码 SKU/可售库存/标题只在 ``/item/<id>.html`` 服务端渲染里，
  **没有对应接口**（已排查 detailproduct bundle 全部 API，无价格/SKU 接口）。
  渲染前提：请求 Cookie 含 ``.AspNetCore.Identity.Application``
  （缺失时服务端只回游客占位 ￥0.00、无 SKU，实测三组对照确认）。
* **列表接口 GetPageList** 只提供卡片指标（销量/曝光/点击/店铺评分），
  详情页上没有这些字段 —— 两者在 ETL 层合并成 ``product_info``。

接口清单（全部与列表接口同一套信封 KM/Ver/Content/Sign/RsaPubAes/IV/TimesTamp）
--------------------------------------------------------------------------------
1. ``ProductFactory/GetProductContent``  {ProductId, TmpVer, Access}
      当前商品详情正文：类目/货号/投诉类型/图文/属性串（5 个内层字段）。
2. ``shopfactory/getshopandsupplier``    {SupplierId, PostageType, TmpVer, Access}
      供应商 & 档口实力画像 39 字段：评分/缺货率/退货率/店龄/保证金/资质/在售数…。
3. ``ProductFactory/GetProductGrade``    {ProductId}
      缺货率、退货率近 7 日序列 + 极值（履约风险趋势）。
4. ``TradeFactory/TradeRecordGetPageList`` {ProductId, Page, TotalItems}
      成交/评价记录（销量佐证、质量分、收货时间、SKU 热度）。
      ``TotalItems`` = **本端已加载条数**（第1页发 0，第2页发第1页实际条数；
      实测 19/20 均返回与第1页不相交的第二页，发 0 则回空）。

参数来源（全部动态，不硬编码）
--------------------------------
* ``ProductId``  —— 搜索接口仅提供 **ProductId / Url 两个字段**（目标发现）；
* ``Access``     —— 每个商品详情页 HTML 服务端渲染的 ``var accessC = "..."``
                    （逐页不同，实测 416/448 位）→ **每商品重新 GET HTML 提取**；
* ``TmpVer``     —— 前端 JS bundle 内 ``productDetail={tmpVer:"..."}``，
                    **不硬编码**：从详情页 ``var moudles=[...]`` 组出 bundle URL
                    （``https://j.17qcc.com/combo?<模块名>&...``）后正则提取，
                    进程内缓存；提取失败抛 :class:`DetailError`（报告确认项 2）；
* ``SupplierId/PostageType`` —— 详情页内联 ``var pageParam = {...}``；
* ``Page/TotalItems``        —— 分页初值 1 / 0，第2页回填第1页条数。

阶段约束
--------
报告确认项 1/4 已落实：每商品重新 GET HTML 提取 accessC；响应在采集侧即用
本次 bootstrap 的 key/iv 解密，完整明文直接回填 ``api_backup`` 详情行
（``decoded_json``），业务字段由 :mod:`collector.etl` 从备份表汇总。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape as html_unescape
from typing import Any
from urllib.parse import urljoin

import requests

from .bootstrap import Tokens
from .envelope import DEFAULT_GUEST_ID, PostResult, post

# ---- 接口地址（同一信封体系，仅路径不同） ----
API_BASE = "https://newopenapiweb.17qcc.com/api/services/app/"
DETAIL_API_URL = API_BASE + "ProductFactory/GetProductContent"
SHOP_API_URL = API_BASE + "shopfactory/getshopandsupplier"
GRADE_API_URL = API_BASE + "ProductFactory/GetProductGrade"
TRADE_API_URL = API_BASE + "TradeFactory/TradeRecordGetPageList"

# 前端把详情页模块名数组（HTML 里的 var moudles）拼成的静态资源聚合地址；
# 实测**不带版本号**也能取到最新内容（如 combo?detailproduct -> 含 tmpVer）
BUNDLE_BASE = "https://j.17qcc.com/combo?"

SITE_ORIGIN = "https://www.17qcc.com"

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)

# 详情页 HTML: var accessC = "4B96C4..."; （十六进制，长度随页而变）
_ACCESS_C_RE = re.compile(r'var\s+accessC\s*=\s*"([0-9A-Fa-f]+)"')
# 详情页 HTML: var pageParam = { "cateId": ..., "suppliereId": 8206, ... };
_PAGE_PARAM_RE = re.compile(r"var\s+pageParam\s*=\s*(\{[^;]*?\})\s*;", re.S)
# 详情页 HTML: var moudles = [ ['ajax', ..., 'detailproduct'], ['fly', ...] ];
_MODULE_LIST_RE = re.compile(r"var\s+moudles\s*=\s*(\[[\s\S]*?\])\s*;")
_MODULE_GROUP_RE = re.compile(r"\[([^\[\]]*)\]")
_MODULE_NAME_RE = re.compile(r"['\"]([^'\"]+)['\"]")
# bundle 内: productDetail={tmpVer:"2023042202", ...}
_TMP_VER_RE = re.compile(r'tmpVer\s*[:=]\s*"([0-9A-Za-z._-]+)"')

# ---- 当前商品详情（SSR HTML）解析 ----
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
# data-mixprice='[{"Num":1,"Money":48.0}]'（单/双引号均兼容，HTML 实体转义）
_MIXPRICE_ATTR_RE = re.compile(r"""data-mixprice=(?:"([^"]+)"|'([^']+)')""")
# 起批价: <p class="pri_de_price"><span>￥</span><em>48.00</em></p>
_WHOLESALE_PRICE_RE = re.compile(
    r'class="pri_de_price"><span>￥</span><em>([\d.]+)</em>'
)
# 颜色块: <div class="pro_color cleafix"> ... <li vid="28320" ... title="白色">
_COLOR_BLOCK_RE = re.compile(r'class="pro_color cleafix">(.*?)</div>', re.S)
_COLOR_ITEM_RE = re.compile(r'<li[^>]*\bvid="(\d+)"[^>]*>.*?title="([^"]*)"', re.S)
# 尺码块: <div class="pro_size cleafix" vid="28320" > <ul class="pro_size_item">...</ul>
_SIZE_BOX_SPLIT = '<div class="pro_size cleafix"'
_SIZE_UL_RE = re.compile(r'<ul class="pro_size_item">(.*?)</ul>', re.S)
_SIZE_ITEM_RE = re.compile(r"<li>(.*?)</li>", re.S)
_SIZE_NAME_RE = re.compile(r'class="size_name" title="([^"]*)"')
_SIZE_PRICE_RE = re.compile(r'<span class="size_price" price="([^"]*)"')
_SIZE_STOCK_RE = re.compile(r"约(\d+)件可售")

# 进程内 TmpVer 缓存（站点不发版就不变；发版后重启进程即刷新）
_TMP_VER_CACHE: str | None = None


class DetailError(RuntimeError):
    """详情采集异常（详情页缺参数、TmpVer 提取失败、接口非 200 等）。"""


@dataclass(frozen=True)
class DetailPage:
    """一次详情页 GET 的解析结果（accessC / pageParam / 原始 HTML）。"""

    url: str
    html: str
    access_c: str
    page_param: dict

    @property
    def supplier_id(self) -> Any:
        return self.page_param.get("suppliereId")

    @property
    def postage_type(self) -> Any:
        return self.page_param.get("PostageType")


def normalize_detail_url(url: str) -> str:
    """列表返回的 Url 可能是相对路径，统一补成绝对 URL。"""
    if not isinstance(url, str) or not url.strip():
        raise DetailError("商品详情 Url 为空")
    return urljoin(SITE_ORIGIN + "/", url.strip())


def _page_headers(user_agent: str | None = None, cookie: str | None = None) -> dict:
    headers = {
        "User-Agent": user_agent or _DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": SITE_ORIGIN + "/",
    }
    if cookie:
        # 登录身份 Cookie 决定 SSR 是否渲染当前商品价格/SKU/库存
        headers["Cookie"] = cookie
    return headers


# --------------------------------------------------------------------------
# 详情页 HTML 解析
# --------------------------------------------------------------------------
def extract_access_c(html: str, source: str = "详情页") -> str:
    """从 HTML 里取服务端渲染的 ``accessC``。"""
    match = _ACCESS_C_RE.search(html or "")
    if not match:
        raise DetailError(f"{source}未找到 var accessC（商品可能已下架）: {source}")
    return match.group(1)


def extract_page_param(html: str) -> dict:
    """从 HTML 里取内联 ``pageParam``（suppliereId / PostageType 等）。"""
    match = _PAGE_PARAM_RE.search(html or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_detail_page(detail_url: str, html: str) -> DetailPage:
    """把详情页 HTML 解析成 :class:`DetailPage`（不含网络请求）。"""
    url = normalize_detail_url(detail_url)
    return DetailPage(
        url=url,
        html=html,
        access_c=extract_access_c(html, url),
        page_param=extract_page_param(html),
    )


def parse_product_detail(html: str, page_param: dict | None = None) -> dict:
    """从登录态 SSR HTML 解析**当前商品详情**（价格/SKU/库存/标题）。

    这些字段只在详情页服务端渲染里、没有对应接口；请求 Cookie 必须含
    ``.AspNetCore.Identity.Application``，否则页面是游客占位
    （``guest_render=True``、价格 0、无 SKU），调用方据此留痕。

    :param html:       详情页完整 HTML（:class:`DetailPage.html`）。
    :param page_param: 可选，页面内联 ``pageParam``（补 ``retail_price``）。
    :returns: 解析结果字典（字段缺失时为 None / 空列表，不抛异常）::

        {
          "product_name":   "2025春季新款港风复古休闲板鞋…",
          "wholesale_price": 48.0,
          "retail_price":   168.0,
          "tier_prices":    [{"num": 1, "money": 48.0}, ...],
          "colors":         [{"vid": "28320", "name": "白色"}, ...],
          "sku":            [{"vid": "28320", "color": "白色",
                              "sizes": [{"name": "39", "price": 48.0,
                                         "stock": 997}, ...]}, ...],
          "stock_total":    9970,
          "guest_render":   False,
        }
    """
    html = html or ""

    # 标题: <title>商品名-青创网</title>
    product_name = None
    m = _TITLE_RE.search(html)
    if m:
        product_name = re.sub(r"[-–—]\s*青创网\s*$", "", m.group(1).strip()) or None

    # 起批价（页面主价格）
    wholesale_price = None
    m = _WHOLESALE_PRICE_RE.search(html)
    if m:
        try:
            wholesale_price = float(m.group(1))
        except ValueError:
            wholesale_price = None

    # 阶梯价: data-mixprice 是 HTML 实体转义的 JSON 数组
    tier_prices: list[dict] = []
    m = _MIXPRICE_ATTR_RE.search(html)
    if m:
        try:
            raw = html_unescape(m.group(1) or m.group(2) or "")
            data = json.loads(raw)
            if isinstance(data, list):
                for it in data:
                    if isinstance(it, dict):
                        tier_prices.append(
                            {"num": it.get("Num"), "money": it.get("Money")}
                        )
        except (json.JSONDecodeError, TypeError):
            tier_prices = []

    # 颜色分类
    colors: list[dict] = []
    m = _COLOR_BLOCK_RE.search(html)
    if m:
        for vid, name in _COLOR_ITEM_RE.findall(m.group(1)):
            colors.append({"vid": str(vid), "name": name})

    # 尺码/库存: 每个颜色一个 pro_size 块（块内 ul 再切 li）
    vid_name = {c["vid"]: c["name"] for c in colors}
    sku: list[dict] = []
    stock_total = 0
    has_stock_text = False
    for chunk in html.split(_SIZE_BOX_SPLIT)[1:]:
        vm = re.match(r'[^>]*?\bvid="(\d+)"', chunk)
        vid = vm.group(1) if vm else ""
        ul = _SIZE_UL_RE.search(chunk)
        sizes: list[dict] = []
        if ul:
            for li in _SIZE_ITEM_RE.findall(ul.group(1)):
                nm = _SIZE_NAME_RE.search(li)
                if not nm:
                    continue
                price = None
                pm = _SIZE_PRICE_RE.search(li)
                if pm:
                    try:
                        price = float(pm.group(1))
                    except ValueError:
                        price = None
                stock = None
                sm = _SIZE_STOCK_RE.search(li)
                if sm:
                    stock = int(sm.group(1))
                    has_stock_text = True
                    stock_total += stock
                sizes.append({"name": nm.group(1), "price": price, "stock": stock})
        if sizes:
            sku.append({"vid": vid, "color": vid_name.get(vid), "sizes": sizes})

    # 零零售价（pageParam.RetailPrice，调用方通常已解析）
    retail_price = None
    if isinstance(page_param, dict):
        rp = page_param.get("RetailPrice")
        if isinstance(rp, (int, float)):
            retail_price = float(rp)

    # 游客占位判定: 无起批价 / 阶梯价全 0 且无任何库存文本
    guest_render = (wholesale_price in (None, 0)) or (
        bool(tier_prices)
        and all((t.get("money") or 0) == 0 for t in tier_prices)
        and not has_stock_text
    )

    return {
        "product_name": product_name,
        "wholesale_price": wholesale_price,
        "retail_price": retail_price,
        "tier_prices": tier_prices,
        "colors": colors,
        "sku": sku,
        "stock_total": stock_total if has_stock_text else None,
        "guest_render": guest_render,
    }


def fetch_detail_page(
    detail_url: str,
    session: requests.Session | None = None,
    timeout: float = 20.0,
    user_agent: str | None = None,
    cookie: str | None = None,
) -> DetailPage:
    """GET 商品详情页 HTML 并解析 accessC / pageParam。

    报告确认项 1：**每个商品都重新 GET HTML 提取**，不复用其它商品的值。
    :param cookie: 登录 Cookie（含 ``.AspNetCore.Identity.Application``），
                   给定时 SSR 才会渲染当前商品价格/SKU/库存。
    :raises DetailError: 请求失败 / 非 200 / 页面没有 accessC。
    """
    url = normalize_detail_url(detail_url)
    sess = session or requests.Session()
    try:
        resp = sess.get(url, headers=_page_headers(user_agent, cookie), timeout=timeout)
    except requests.RequestException as exc:
        raise DetailError(f"详情页请求失败 {url}: {exc!r}") from exc
    if resp.status_code != 200:
        raise DetailError(f"详情页 HTTP {resp.status_code}: {url}")
    return parse_detail_page(url, resp.text)


def fetch_access_c(
    detail_url: str,
    session: requests.Session | None = None,
    timeout: float = 20.0,
    user_agent: str | None = None,
    cookie: str | None = None,
) -> str:
    """只取 ``accessC``（内部同样重新 GET 整页，兼容旧调用）。"""
    return fetch_detail_page(detail_url, session, timeout, user_agent, cookie).access_c


# --------------------------------------------------------------------------
# TmpVer 动态提取（报告确认项 2：不硬编码）
# --------------------------------------------------------------------------
def build_bundle_url(html: str) -> str:
    """详情页 ``var moudles=[[...],[...]]`` -> bundle 聚合 URL。

    取含 ``detailproduct`` 的那一组模块名（版本号由站点维护，实测省略也可用）。
    页面结构变化时回退到单模块 ``combo?detailproduct``（同样含 tmpVer 定义）。
    """
    groups: list[list[str]] = []
    match = _MODULE_LIST_RE.search(html or "")
    if match:
        for inner in _MODULE_GROUP_RE.findall(match.group(1)):
            names = _MODULE_NAME_RE.findall(inner)
            if names:
                groups.append(names)
    for names in groups:
        if "detailproduct" in names:
            return BUNDLE_BASE + "&".join(names)
    return BUNDLE_BASE + "detailproduct"


def fetch_tmp_ver(
    html: str | None = None,
    session: requests.Session | None = None,
    timeout: float = 20.0,
    user_agent: str | None = None,
) -> str:
    """动态提取 ``productDetail={tmpVer:"..."}``（进程内缓存）。

    顺序：缓存 -> 页面内联(通常没有) -> ``moudles`` 组出的 bundle。
    :raises DetailError: 三处都取不到。
    """
    global _TMP_VER_CACHE
    if _TMP_VER_CACHE:
        return _TMP_VER_CACHE

    if html:
        inline = _TMP_VER_RE.search(html)
        if inline:
            _TMP_VER_CACHE = inline.group(1)
            return _TMP_VER_CACHE

    bundle_url = build_bundle_url(html or "")
    sess = session or requests.Session()
    try:
        resp = sess.get(bundle_url, headers=_page_headers(user_agent), timeout=timeout)
    except requests.RequestException as exc:
        raise DetailError(f"bundle 请求失败 {bundle_url}: {exc!r}") from exc
    if resp.status_code != 200:
        raise DetailError(f"bundle HTTP {resp.status_code}: {bundle_url}")
    match = _TMP_VER_RE.search(resp.text)
    if not match:
        raise DetailError(f"bundle 内未找到 tmpVer 定义: {bundle_url}")
    _TMP_VER_CACHE = match.group(1)
    return _TMP_VER_CACHE


def reset_tmp_ver_cache() -> None:
    """清空 TmpVer 缓存（测试/发版后刷新用）。"""
    global _TMP_VER_CACHE
    _TMP_VER_CACHE = None


# --------------------------------------------------------------------------
# 参数组装（字段顺序与前端 $.qccAjax 实抓一致）
# --------------------------------------------------------------------------
def build_detail_params(product_id: int | str, access_c: str, tmp_ver: str) -> dict:
    """GetProductContent 明文参数。"""
    if not tmp_ver:
        raise DetailError("TmpVer 为空，无法组装详情参数")
    return {"ProductId": int(product_id), "TmpVer": tmp_ver, "Access": access_c}


def build_shop_params(page_param: dict, access_c: str, tmp_ver: str) -> dict:
    """getshopandsupplier 明文参数（SupplierId 来自页面 pageParam）。"""
    supplier_id = (page_param or {}).get("suppliereId")
    if supplier_id in (None, "", 0):
        raise DetailError("pageParam 缺少 suppliereId，无法请求供应商画像")
    if not tmp_ver:
        raise DetailError("TmpVer 为空，无法组装供应商画像参数")
    return {
        "SupplierId": int(supplier_id),
        "PostageType": (page_param or {}).get("PostageType", 0),
        "TmpVer": tmp_ver,
        "Access": access_c,
    }


def build_grade_params(product_id: int | str) -> dict:
    """GetProductGrade 明文参数。"""
    return {"ProductId": int(product_id)}


def build_trade_params(product_id: int | str, page: int = 1, total_items: int = 0) -> dict:
    """TradeRecordGetPageList 明文参数（初值与前端一致：第 1 页、0 已取）。"""
    return {"ProductId": int(product_id), "Page": int(page), "TotalItems": int(total_items)}


# --------------------------------------------------------------------------
# 请求封装
# --------------------------------------------------------------------------
def post_api(
    tokens: Tokens,
    api_url: str,
    params: dict,
    referer_url: str | None = None,
    guest_id: str = DEFAULT_GUEST_ID,
    login: bool = True,
    timeout: float = 30.0,
) -> PostResult:
    """POST 任意详情域接口，返回 :class:`PostResult`（含真实发出的信封体）。

    详情域接口与列表接口请求头差异：Accept 用 jQuery 风格，
    ``Qcc-Pc-Referer`` 指向当前商品页（前端 $.qccAjax 行为）。
    """
    extra_headers = {"Accept": "application/json, text/javascript, */*; q=0.01",
                     "Referer": SITE_ORIGIN + "/"}
    if referer_url:
        extra_headers["Qcc-Pc-Referer"] = normalize_detail_url(referer_url)
    return post(
        tokens,
        params,
        guest_id=guest_id,
        timeout=timeout,
        login=login,
        with_headers=True,
        api_url=api_url,
        extra_headers=extra_headers,
    )


def post_detail(
    tokens: Tokens,
    product_id: int | str,
    detail_url: str,
    access_c: str,
    tmp_ver: str,
    guest_id: str = DEFAULT_GUEST_ID,
    login: bool = True,
    timeout: float = 30.0,
) -> PostResult:
    """POST GetProductContent（当前商品详情正文）。"""
    params = build_detail_params(product_id, access_c, tmp_ver)
    return post_api(tokens, DETAIL_API_URL, params, referer_url=detail_url,
                    guest_id=guest_id, login=login, timeout=timeout)


def post_shop_supplier(
    tokens: Tokens,
    page_param: dict,
    access_c: str,
    tmp_ver: str,
    detail_url: str,
    guest_id: str = DEFAULT_GUEST_ID,
    login: bool = True,
    timeout: float = 30.0,
) -> PostResult:
    """POST getshopandsupplier（供应商 & 档口实力画像）。"""
    params = build_shop_params(page_param, access_c, tmp_ver)
    return post_api(tokens, SHOP_API_URL, params, referer_url=detail_url,
                    guest_id=guest_id, login=login, timeout=timeout)


def post_product_grade(
    tokens: Tokens,
    product_id: int | str,
    detail_url: str,
    guest_id: str = DEFAULT_GUEST_ID,
    login: bool = True,
    timeout: float = 30.0,
) -> PostResult:
    """POST GetProductGrade（缺货率/退货率 7 日趋势）。"""
    return post_api(tokens, GRADE_API_URL, build_grade_params(product_id),
                    referer_url=detail_url, guest_id=guest_id, login=login, timeout=timeout)


def post_trade_records(
    tokens: Tokens,
    product_id: int | str,
    detail_url: str,
    page: int = 1,
    total_items: int = 0,
    guest_id: str = DEFAULT_GUEST_ID,
    login: bool = True,
    timeout: float = 30.0,
) -> PostResult:
    """POST TradeRecordGetPageList（成交/评价记录）。

    ``total_items`` = 本端已加载条数：第1页发 0；第2页发第1页实际条数
    （实测发 0 回空、发 19/20 均返回与第1页不相交的第二页）。
    """
    params = build_trade_params(product_id, page, total_items)
    return post_api(tokens, TRADE_API_URL, params, referer_url=detail_url,
                    guest_id=guest_id, login=login, timeout=timeout)
