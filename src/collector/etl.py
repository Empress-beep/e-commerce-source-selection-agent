"""ETL：从 api_backup 备份表汇总生成 product_info 业务表（AI选品字段）。

数据源结合（不重复采集，只做读取 + 清洗 + 合并）
------------------------------------------------
====================  ==========================================================
api_backup 行          进入 product_info 的字段
====================  ==========================================================
``category=list``      卡片指标 buy_count/year_sale_count/exposure_count/
                       click_count + 卡片快照（card_price、shop_name、market、
                       h12/h24 发货率、资质兜底、product_url、cover_photo）；
``category=page``      详情页 SSR 解析：product_name、wholesale_price、
                       retail_price、tier_prices、colors、sku_stock、stock_total；
``content``            cate_name、product_no、complaint_types；
``shop``               供应商画像 39 字段中的比率/评分/保证金/在售数/资质；
``grade``              7日缺货率/退货率峰值与趋势序列；
``trade``              成交记录前 2 页聚合（总数/条数/件数/质量分均值）。
====================  ==========================================================

清洗口径
--------
* 比率统一 0~1：``"11.40%"`` -> 0.114，已是小数的原样保留；
* ``rate_trade_score`` 为 0~5 评分（不做百分比换算）；
* 数字宽松解析（str/int/float/None），解析不了 -> NULL，不中断整表；
* 同一 run 内相同 ``shop_name`` 的行，把店铺画像字段横向补齐
  （详情只采前 20 条，其余同店商品共享画像）；
* 追溯三列 ``request_url``/``http_status``/``api_trace`` 直取 api_backup
  （请求地址 + 状态码 + 逐接口调用明细，审计可回放）；
* 写入走 UPSERT，唯一键 ``(run_id, keyword, product_id)``，重跑覆盖。
"""

from __future__ import annotations

import json
from typing import Any

from src.common import database

# 业务表 DDL（幂等）
BUSINESS_SQL = database.SQL_DIR / "business_tables.sql"

# 读备份表只取这些列（decoded_json 含明文；api_url/http_status/decrypt_ok 供追溯）
BACKUP_COLS = (
    "category, api_name, api_url, keyword, page_no, product_id, "
    "http_status, decrypt_ok, decoded_json"
)

# 详情域 api_name -> 逻辑名（与 collector.main.DETAIL_APIS 对应）
_DETAIL_API_KEYS = {
    "ProductFactory/GetProductContent": "content",
    "shopfactory/getshopandsupplier": "shop",
    "ProductFactory/GetProductGrade": "grade",
    "TradeFactory/TradeRecordGetPageList": "trade",
}

# 详情行画像横向补齐的列（同 shop_name 共享）
_SHOP_PROPAGATE_COLS = (
    "supplier_id",
    "rate_trade_score",
    "pin_rate",
    "stock_out_rate",
    "return_goods_rate",
    "bond_balance",
    "h12_delivery_rate",
    "h24_delivery_rate",
    "total_product_count",
    "new_product_count",
    "qualifications",
)


# --------------------------------------------------------------------------
# 宽松清洗工具
# --------------------------------------------------------------------------
def _loads(value: Any) -> Any:
    """JSON 字符串 -> 对象；已是对象/解析失败则原样返回。"""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


def _f(value: Any) -> float | None:
    """宽松浮点：``"48.00"``/48/48.0 -> 48.0，其余 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def _i(value: Any) -> int | None:
    f = _f(value)
    return int(f) if f is not None else None


def _ratio(value: Any) -> float | None:
    """比率统一 0~1：``"11.40%"`` -> 0.114；已是小数 -> 原样。"""
    if isinstance(value, str) and value.strip().endswith("%"):
        f = _f(value.strip()[:-1])
        return f / 100 if f is not None else None
    return _f(value)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


# --------------------------------------------------------------------------
# 源数据收集
# --------------------------------------------------------------------------
def _decode(row: dict) -> Any:
    text = row.get("decoded_json")
    if not isinstance(text, str) or not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def collect_sources(backup_rows: list[dict]) -> dict:
    """按行类别把备份行拆成卡片/页面/详情索引 + 逐商品请求追溯。

    :returns: ``{"cards": {(kw, pid): card}, "list_page_no": {kw: page},
                 "pages": {pid: parsed},
                 "page_meta": {pid: {"url": ..., "status": ...}},
                 "trace": {pid: [{"api_name","api_url","http_status","decrypt_ok"}]},
                 "content"/"shop"/"grade": {pid: Result},
                 "trade": {pid: [Result, ...]}}``
    """
    cards: dict[tuple, dict] = {}
    list_page_no: dict[str, int] = {}
    pages: dict[Any, dict] = {}
    page_meta: dict[Any, dict] = {}
    trace: dict[Any, dict] = {}
    single: dict[str, dict] = {"content": {}, "shop": {}, "grade": {}}
    trades: dict[Any, dict[str, dict]] = {}

    for row in backup_rows:
        category = row.get("category")
        pid = row.get("product_id")

        # 追溯先行：page/detail 行都记一条（按 api_name+明文去重，同页重复不累加）
        if category in ("page", "detail") and pid is not None:
            entry_key = (row.get("api_name"), row.get("decoded_json"))
            trace.setdefault(pid, {})[entry_key] = {
                "api_name": row.get("api_name"),
                "api_url": row.get("api_url"),
                "http_status": row.get("http_status"),
                "decrypt_ok": row.get("decrypt_ok"),
            }

        data = _decode(row)
        if data is None:
            continue

        if category == "list":
            kw = row.get("keyword") or ""
            if row.get("page_no") is not None:
                list_page_no[kw] = row.get("page_no")
            items = (data.get("Result") or {}).get("Items") or []
            for item in items:
                if isinstance(item, dict) and item.get("ProductId"):
                    cards[(kw, item.get("ProductId"))] = item
        elif category == "page" and isinstance(data, dict):
            if pid is not None:
                pages[pid] = data
                page_meta[pid] = {
                    "url": row.get("api_url"),
                    "status": row.get("http_status"),
                }
        elif category == "detail":
            key = _DETAIL_API_KEYS.get(row.get("api_name") or "")
            res = data.get("Result") if isinstance(data, dict) else None
            if not key or pid is None or res is None:
                continue
            if key == "trade":
                # 多关键词会重复采到同一页：按明文去重后保留 page1+page2 两份
                trades.setdefault(pid, {}).setdefault(row.get("decoded_json"), res)
            else:
                # 同商品多关键词只保留一份（内容与关键词无关）
                single[key].setdefault(pid, res)

    return {
        "cards": cards,
        "list_page_no": list_page_no,
        "pages": pages,
        "page_meta": page_meta,
        "trace": {pid: list(by_key.values()) for pid, by_key in trace.items()},
        "content": single["content"],
        "shop": single["shop"],
        "grade": single["grade"],
        "trade": {pid: list(by_text.values()) for pid, by_text in trades.items()},
    }


# --------------------------------------------------------------------------
# 行组装
# --------------------------------------------------------------------------
def _build_row(
    run_id: str,
    keyword: str,
    page_no: int | None,
    pid: Any,
    card: dict | None,
    page: dict | None,
    content: dict | None,
    shop: dict | None,
    grade: dict | None,
    trades: list[dict],
    page_meta: dict | None = None,
    trace: list[dict] | None = None,
) -> dict:
    """把一个 (关键词, 商品) 的四类源数据合成一行 product_info（含请求追溯）。"""
    card = card or {}
    page = page or {}
    content = content or {}
    shop = shop or {}
    grade = grade or {}

    # ---- 商品基础信息 ----
    product_name = page.get("product_name") or card.get("ProductName")

    # ---- 价格 ----
    wholesale_price = _f(page.get("wholesale_price")) or _f(card.get("Price"))
    tier_prices = page.get("tier_prices") if isinstance(page.get("tier_prices"), list) else None

    # ---- SKU / 库存（SSR 独有） ----
    sku = page.get("sku") if isinstance(page.get("sku"), list) else None
    colors = page.get("colors") if isinstance(page.get("colors"), list) else None

    # ---- 供应商画像（店铺接口优先，卡片/趋势兜底） ----
    quals = shop.get("QualificationList")
    if not isinstance(quals, list) or not quals:
        quals = card.get("SupplierQualificationExDtos")
    supplier_id = None
    for q in quals or []:
        if isinstance(q, dict) and q.get("SupplierId"):
            supplier_id = _i(q.get("SupplierId"))
            break
    if not supplier_id:
        supplier_id = _i(card.get("SupplierId")) or None
        if supplier_id == 0:
            supplier_id = None

    h12 = _ratio(shop.get("H12DeliveryRate"))
    if h12 is None:
        h12 = _ratio(card.get("H12DeliveryRate"))
    h24 = _ratio(shop.get("H24DeliveryRate"))
    if h24 is None:
        h24 = _ratio(card.get("H24DeliveryRate"))

    # ---- 履约风险（grade） ----
    grade_trend = {
        "out_of_stock": _loads(grade.get("OutOfStockRateJson")),
        "return_rate": _loads(grade.get("ReturnRateJson")),
        "creation_time": _loads(grade.get("CreationTimeJson")),
        "max_out_of_stock_rate": _f(grade.get("MaxOutOfStockRate")),
        "min_out_of_stock_rate": _f(grade.get("MinOutOfStockRate")),
        "max_return_rate": _f(grade.get("MaxReturnRate")),
        "min_return_rate": _f(grade.get("MinReturnRate")),
    }
    if all(v is None for v in grade_trend.values()):
        grade_trend = None
    oos_max = _f(grade.get("MaxOutOfStockRate"))
    if oos_max is None and isinstance(grade_trend, dict):
        seq = [x for x in (grade_trend.get("out_of_stock") or []) if _f(x) is not None]
        oos_max = max((_f(x) for x in seq), default=None)
    ret_max = _f(grade.get("MaxReturnRate"))
    if ret_max is None and isinstance(grade_trend, dict):
        seq = [x for x in (grade_trend.get("return_rate") or []) if _f(x) is not None]
        ret_max = max((_f(x) for x in seq), default=None)

    # ---- 交易佐证（trade 前2页聚合） ----
    totals: list[int] = []
    sample_count = 0
    qty_values: list[float] = []
    quality_values: list[float] = []
    for res in trades:
        if not isinstance(res, dict):
            continue
        total = _i(res.get("TotalItems"))
        if total:
            totals.append(total)
        items = res.get("Items") or []
        sample_count += len(items)
        for it in items:
            if not isinstance(it, dict):
                continue
            q = _f(it.get("Quantity"))
            if q is not None:
                qty_values.append(q)
            rate = it.get("ProductRate")
            if isinstance(rate, dict):
                score = _f(rate.get("ProductQualityScore"))
                if score is not None:
                    quality_values.append(score)
    trade_total = max(totals) if totals else None

    # ---- 数据来源留痕 ----
    sources = []
    if card:
        sources.append("list")
    if page:
        sources.append("page")
    if content:
        sources.append("content")
    if shop:
        sources.append("shop")
    if grade:
        sources.append("grade")
    if trades:
        sources.append("trade")

    return {
        "run_id": run_id,
        "keyword": keyword,
        "page_no": page_no,
        "product_id": int(pid),
        "product_name": product_name,
        "product_no": content.get("ProductNo") or card.get("ProductNo"),
        "product_url": card.get("Url"),
        "listing_time": card.get("CreationTime"),
        "cate_name": content.get("ProductCateName"),
        "cover_photo": card.get("Photo"),
        "wholesale_price": wholesale_price,
        "retail_price": _f(page.get("retail_price")) or _f(card.get("RetailPrice")),
        "card_price": _f(card.get("Price")),
        "tier_prices": tier_prices,
        "colors": colors,
        "sku_stock": sku,
        "stock_total": _i(page.get("stock_total")),
        "buy_count": _i(card.get("BuyCount")),
        "year_sale_count": _i(card.get("YearSaleCount")),
        "exposure_count": _i(card.get("ExposureCount")),
        "click_count": _i(card.get("ClickCount")),
        "shop_name": card.get("ShopName"),
        "shop_number": card.get("ShopNumber"),
        "supplier_id": supplier_id,
        "market_name": card.get("MarketName"),
        "market_area_name": card.get("MarketAreaName"),
        "shop_age": _i(shop.get("ShopAge")) or _i(card.get("ShopAge")),
        "rate_trade_score": _f(shop.get("RateTradeScore")),
        "pin_rate": _ratio(shop.get("PinRate")),
        "stock_out_rate": _ratio(shop.get("StockOutRate")),
        "return_goods_rate": _ratio(shop.get("ReturnGoodsRate")),
        "bond_balance": _f(shop.get("BondBalance")),
        "h12_delivery_rate": h12,
        "h24_delivery_rate": h24,
        "total_product_count": _i(shop.get("TotalProductCount")),
        "new_product_count": _i(shop.get("NewProductCount")),
        "qualifications": quals if isinstance(quals, list) and quals else None,
        "grade_out_of_stock_max": oos_max,
        "grade_return_rate_max": ret_max,
        "grade_trend": grade_trend,
        "complaint_types": (
            content.get("ProductComplaintType")
            if isinstance(content.get("ProductComplaintType"), list)
            else None
        ),
        "trade_total_items": trade_total,
        "trade_sample_count": sample_count or None,
        "trade_qty_sum": int(sum(qty_values)) if qty_values else None,
        "trade_avg_quality": _mean(quality_values),

        # ---- 请求追溯（企业审计：请求地址 + 状态码 + 逐接口调用明细） ----
        "request_url": (page_meta or {}).get("url") or card.get("Url"),
        "http_status": (page_meta or {}).get("status"),
        "api_trace": trace or None,
        "data_source": ",".join(sources),
    }


def _propagate_shop_fields(rows: list[dict]) -> None:
    """同 run 同 shop_name：店铺画像字段从有值的行横向补齐到缺值的行。"""
    donors: dict[tuple, dict] = {}
    for row in rows:
        shop_name = row.get("shop_name")
        if not shop_name:
            continue
        key = (row.get("run_id"), shop_name)
        donor = donors.get(key)
        # 选字段最全的行做供体
        filled = sum(1 for c in _SHOP_PROPAGATE_COLS if row.get(c) is not None)
        if donor is None or filled > sum(
            1 for c in _SHOP_PROPAGATE_COLS if donor.get(c) is not None
        ):
            donors[key] = row
    for row in rows:
        shop_name = row.get("shop_name")
        donor = donors.get((row.get("run_id"), shop_name)) if shop_name else None
        if donor is None or donor is row:
            continue
        for col in _SHOP_PROPAGATE_COLS:
            if row.get(col) is None and donor.get(col) is not None:
                row[col] = donor[col]


def build_product_info_rows(run_id: str, backup_rows: list[dict]) -> list[dict]:
    """把一个 run 的备份行合成 product_info 行列表（不碰数据库）。"""
    src = collect_sources(backup_rows)
    cards, pages = src["cards"], src["pages"]

    # 行全集 = 列表卡片(每关键词每商品) ∪ 详情页(兜底)
    keys = set(cards.keys())
    card_pids = {pid for _, pid in keys}
    for pid in pages:
        if pid not in card_pids:
            keys.add(("", pid))

    rows: list[dict] = []
    for keyword, pid in sorted(keys, key=lambda k: (str(k[0]), str(k[1]))):
        card = cards.get((keyword, pid))
        if card is None:
            # 只有 page 没有本关键词卡片时，借用任意关键词的同商品卡片
            card = next(
                (c for (kw, p), c in cards.items() if p == pid), None
            )
        row = _build_row(
            run_id=run_id,
            keyword=keyword,
            page_no=src["list_page_no"].get(keyword),
            pid=pid,
            card=card,
            page=pages.get(pid),
            content=src["content"].get(pid),
            shop=src["shop"].get(pid),
            grade=src["grade"].get(pid),
            trades=src["trade"].get(pid) or [],
            page_meta=src["page_meta"].get(pid),
            trace=src["trace"].get(pid),
        )
        rows.append(row)

    _propagate_shop_fields(rows)
    return rows


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def run_etl(log, run_id: str) -> int:
    """读 ``api_backup(run_id)`` -> 清洗合并 -> UPSERT ``product_info``。

    :param log: 运行 logger（只打进度与条数，不打明文）。
    :returns: 写入/更新行数。
    :raises database.DatabaseError: DDL/读/写失败（调用方降级）。
    """
    ddl = database.run_sql_file(BUSINESS_SQL)
    log.debug(f"      业务表就绪 spider_db.product_info（DDL {ddl} 句）")

    backup_rows = database.fetch_all(
        "api_backup",
        where="run_id = %s AND category IN (%s, %s, %s)",
        params=[run_id, "list", "page", "detail"],
        cols=BACKUP_COLS,
    )
    rows = build_product_info_rows(run_id, backup_rows)
    if not rows:
        log.warning(f"      run_id={run_id} 备份表无可汇总数据，product_info 未写入")
        return 0

    written = database.upsert_many(
        "product_info",
        rows,
        conflict_cols=["run_id", "keyword", "product_id"],
    )
    log.debug(
        f"      product_info UPSERT {written} 行 "
        f"(源: 备份 {len(backup_rows)} 行)"
    )
    return written
