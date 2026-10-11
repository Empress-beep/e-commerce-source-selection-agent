"""命令行入口：列表接口（解密 + 备份） -> 详情页SSR解析 + 详情域四接口（采集即解密回填） -> ETL。

双入口模式
----------
* **命令行**：``python -m src.collector.main --keyword xx --page xx``（单独爬虫调试）；
* **程序导入**：``from src.collector.main import run_crawl`` ->
  ``run_crawl(keyword, page, limit, cateid, resume)``，Function-Call Agent 用入口
  （``resume`` 非空 = 断点续爬），内部复用同一套 ``main()``，返回采集条数/入库统计结果字典。

运行示例
--------
    python -m src.collector.main --keyword 春季男鞋 --page 1
    python -m src.collector.main --keywords 春季老爹鞋男,春季板鞋男,复古休闲男鞋   # 批量
    python -m src.collector.main --keyword 手机 --detail-limit 3
    python -m src.collector.main --bootstrap-only                    # 只验证首页密钥
    python -m src.collector.main --no-db                             # 只写日志不入库
    python -m src.collector.main --no-etl                            # 只备份不汇总业务表
    python -m src.collector.main --resume 20261009_125128            # 断点续爬（接续指定 run_id）
    python -m src.collector.main --anon --detail-limit 0             # 匿名、不采详情

输出约定（三通道各司其职 + 两套日志模式）
------------------------------------------
* **控制台**：只打简要进度，不含任何明文报文（``file_only`` 过滤器恒开）；
* **日志文件 ``logs/spider_<run_id>.log``**：级别随入口切换 ——
  - ``python -m src.collector.main``（``__main__``，本地逆向调试）= **debug 模式**：
    DEBUG 全收，含请求信封、服务端原始响应、解密后的完整明文、字段对照表；
  - ``from src.collector.main import run_crawl``（Agent 导入调用）= **quiet 模式**：
    INFO 级，DEBUG 全屏蔽（AES key/iv、原始报文、解密明文大文本不落盘），
    INFO 只留业务关键点（run_id/关键词/页码/limit/列表条目数/详情采集数/
    product_info 入库行数/最终统计）；ERROR 与异常 traceback 两模式都照常输出；
* **MySQL ``spider_db.api_backup``**：一次请求一行 —— ``raw_json`` 原样
  （列表/详情接口为 JSON，详情页为 HTML），``decoded_json`` 为解密明文
  或页面解析结果（失败为 NULL）。

采集口径（数据源结合，冗余接口已删）
------------------------------------
* 搜索接口 ``GetPageList`` **只做目标发现**（``_extract_targets`` 仅取
  ``ProductId``/``Url``），其卡片同时作为销量/曝光/点击指标来源；
* 每商品重新 GET 详情页 HTML（登录 Cookie，提取 ``accessC``/``pageParam``/
  moudles）-> **SSR 解析当前商品详情**（批发价/阶梯价/颜色尺码/库存）落
  ``category=page`` 备份行；游客占位时打 WARN 提醒登录态失效；
* 动态取 ``TmpVer``（bundle 正则，不硬编码）-> 依次请求
  ``GetProductContent`` / ``getshopandsupplier`` / ``GetProductGrade`` /
  ``TradeRecordGetPageList``（默认前 2 页），响应**采集即用本次 key/iv
  解密**回填详情行 ``decoded_json``；
* 风控节奏：商品间 ``--item-delay``（默认 3s），关键词间
  ``--keyword-delay``（默认 180s）；详情默认只采列表前 20 条；
* **断点续爬**：``--resume <run_id>`` 以 ``api_backup`` 成功行
  （``decrypt_ok=1``）为唯一事实源推导断点——列表已完成的关键词直接复用
  备份明文（不再发列表请求），详情按「商品 × 接口域」判定缺哪补哪；
  不新建状态表、不预写占位行（待采集集合 = list 全量明文前 N − 已成功
  集合现场推导）；中断（Ctrl+C/异常/登录态失效）后用原 run_id 续跑，
  末尾 ETL 幂等重跑收口，``product_info`` 与一次跑完的结果一致；
  续跑时 ``--keywords/--cateid/--detail-apis`` 等参数须与原 run 一致；
* 运行末尾 ETL（``src.collector.etl``）把备份表汇总成 ``product_info``
  业务表（清洗后的 AI 选品字段），``--no-etl`` 可关闭；ETL 后执行详情准入
  清理：未成功拉取详情的列表目标（``api_backup`` 无该商品 ``category='page'``
  行）直接从 ``product_info`` 丢弃，实际行数不超过 ``--detail-limit``，
  列表卡片仍全量保留在 ``api_backup``（目标发现/销量指标来源）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# sys.path 引导：把项目根（src/ 的上一级）加进搜索路径，
# 保证从任意工作目录启动（python -m src.collector.main / Agent 导入）都能 import src.*
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.common import database  # noqa: E402
from src.common import logger as run_logger  # noqa: E402

from .bootstrap import Tokens, bootstrap  # noqa: E402
from .decode import decrypt_result  # noqa: E402
from .detail import (  # noqa: E402
    DetailError,
    fetch_detail_page,
    fetch_tmp_ver,
    parse_product_detail,
    post_detail,
    post_product_grade,
    post_shop_supplier,
    post_trade_records,
)
from .envelope import API_URL, DEFAULT_GUEST_ID, PostResult, business_params, post  # noqa: E402
from .session_login import LOGIN_COOKIE  # noqa: E402

# 备份表名（业务表 product_info 由 ETL 阶段写入）
BACKUP_TABLE = "api_backup"

# 两套日志模式的判定：python -m src.collector.main（__main__）= 本地逆向调试走 debug；
# run_crawl 等导入路径（模块名 src.collector.main）= Agent 生产路径走 quiet。
_CLI_MODE = __name__ == "__main__"


def _log_mode() -> str:
    """本次运行的日志模式：``__main__`` -> ``"debug"``，导入调用 -> ``"quiet"``。"""
    return "debug" if _CLI_MODE else "quiet"

LIST_API_NAME = "SearchFactory/GetPageList"
DETAIL_API_NAME = "ProductFactory/GetProductContent"
# 详情页 HTML 备份行（当前商品详情 SSR 解析来源，非加密接口）
PAGE_API_NAME = "GET /item/<id>.html"

# 详情阶段可选接口：键 -> (api_name, 简称, 用途备注)
# 当前商品价格/SKU/库存来自详情页 SSR HTML 解析（category=page 行），
# 搜索接口 GetPageList 只做目标发现 + 销量/曝光/点击指标
DETAIL_APIS = {
    "content": (DETAIL_API_NAME, "正文", "详情正文: 类目/投诉/图文/属性"),
    "shop": ("shopfactory/getshopandsupplier", "店铺", "供应商&档口实力画像(39字段)"),
    "grade": ("ProductFactory/GetProductGrade", "分级", "缺货率/退货率7日趋势"),
    "trade": ("TradeFactory/TradeRecordGetPageList", "成交", "成交/评价记录"),
}
DETAIL_API_ORDER = ("content", "shop", "grade", "trade")


# --------------------------------------------------------------------------
# 解析与提取
# --------------------------------------------------------------------------
def _try_decrypt(resp: dict, tokens: Tokens) -> tuple[str | None, str | None]:
    """解密响应 Result；返回 ``(完整明文JSON串, 错误信息)``，失败时明文为 None。"""
    result = resp.get("Result", resp.get("result"))
    if not isinstance(result, str) or not result:
        return None, "响应缺少 Result 密文字段"
    try:
        return decrypt_result(result, tokens), None
    except Exception as exc:  # noqa: BLE001 - 任何解密异常都要留痕但不中断
        return None, f"解密失败: {exc!r}"


def _page_meta(decoded_text: str | None) -> dict:
    """从解密明文里取分页/条目统计（拿不到就返回空字典）。"""
    meta: dict = {}
    if not decoded_text:
        return meta
    try:
        data = json.loads(decoded_text)
    except json.JSONDecodeError:
        return meta
    if not isinstance(data, dict):
        return meta
    res = data.get("Result")
    if isinstance(res, dict):
        for key in ("TotalCount", "TotalPages", "Page", "PageSize", "HasNextPage",
                    "Pages", "TotalItems"):
            if key in res:
                meta[key] = res[key]
        items = res.get("Items")
        if isinstance(items, list):
            meta["item_count"] = len(items)
    return meta


def _extract_targets(decoded_text: str | None, limit: int) -> list[dict]:
    """搜索接口只做目标发现：**仅取 ``ProductId`` 与 ``Url`` 两个字段**。

    商品信息不从搜索接口取：价格/SKU/库存由详情页 SSR 解析（category=page），
    销量/曝光/点击等卡片指标在 ETL 阶段从本接口 ``decoded_json`` 汇总。
    """
    if not decoded_text or limit <= 0:
        return []
    try:
        data = json.loads(decoded_text)
        items = data["Result"]["Items"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    targets: list[dict] = []
    for item in items:
        if len(targets) >= limit:
            break
        if not isinstance(item, dict):
            continue
        pid, url = item.get("ProductId"), item.get("Url")
        if pid in (None, "", 0) or not url:
            continue
        targets.append({"product_id": pid, "url": url})
    return targets


# --------------------------------------------------------------------------
# 断点续爬（--resume）：api_backup 成功行 -> 断点快照推导（不新建状态表）
# --------------------------------------------------------------------------
# 详情域 api_name -> 键（content/shop/grade/trade），断点判定用
_API_NAME_TO_KEY = {v[0]: k for k, v in DETAIL_APIS.items()}


def _build_checkpoint(rows: list[dict]) -> dict:
    """由 ``api_backup`` 成功行（调用方已过滤 ``decrypt_ok=1``）推导断点快照。

    :returns: ``{"list_keywords": set[str], "products": {pid: {域键: True}}}``。
        list 行仅收录 ``item_count`` 非空的（``Result=null`` 失效行为 NULL，
        SQL 已过滤，这里再兜一层）；page/detail 行按 ``product_id`` 归并，
        detail 行经 :data:`_API_NAME_TO_KEY` 映射到 content/shop/grade/trade。
        表里没有行的实体 = 待采集（不预写占位行，由调用方与 list 明文求差集）。
    """
    list_keywords: set[str] = set()
    products: dict[int, dict] = {}
    for row in rows or []:
        cat = row.get("category")
        pid = row.get("product_id")
        if cat == "list":
            kw = row.get("keyword")
            if kw and row.get("item_count") is not None:
                list_keywords.add(str(kw))
        elif cat == "page" and pid:
            products.setdefault(int(pid), {})["page"] = True
        elif cat == "detail" and pid:
            key = _API_NAME_TO_KEY.get(row.get("api_name"))
            if key:
                products.setdefault(int(pid), {})[key] = True
    return {"list_keywords": list_keywords, "products": products}


def _product_done(checkpoint: dict, product_id, wanted) -> bool:
    """商品断点判定：page 行 + ``wanted`` 各详情域均已有成功行才算完成。"""
    done = checkpoint["products"].get(int(product_id), {})
    if not done.get("page"):
        return False
    return all(done.get(k) for k in wanted)


def _load_checkpoint(log, run_id: str) -> dict:
    """从 ``api_backup`` 加载断点快照（一次查询；DB 异常按空断点全量重采）。"""
    try:
        rows = database.fetch_all(
            BACKUP_TABLE,
            where=(
                "run_id = %s AND decrypt_ok = 1 AND ("
                "(category = 'list' AND keyword IS NOT NULL AND item_count IS NOT NULL)"
                " OR (category = 'page' AND product_id IS NOT NULL)"
                " OR (category = 'detail' AND product_id IS NOT NULL))"
            ),
            params=[run_id],
            cols="category, api_name, keyword, product_id, item_count",
        )
    except database.DatabaseError as exc:
        log.warning(f"      断点加载失败（按无断点全量重采）: {exc}")
        return {"list_keywords": set(), "products": {}}
    return _build_checkpoint(rows)


def _load_list_decoded(log, run_id: str, keyword: str) -> str | None:
    """取该关键词最新的列表成功明文（断点命中时复用，不再请求列表接口）。"""
    try:
        rows = database.fetch_all(
            BACKUP_TABLE,
            where=(
                "run_id = %s AND keyword = %s AND category = 'list' "
                "AND decrypt_ok = 1 AND item_count IS NOT NULL"
            ),
            params=[run_id, keyword],
            cols="id, decoded_json",
        )
    except database.DatabaseError as exc:
        log.warning(f"      列表断点明文读取失败（重采列表）: {exc}")
        return None
    if not rows:
        return None
    best = max(rows, key=lambda r: r.get("id") or 0)
    text = best.get("decoded_json")
    return text if text else None


# --------------------------------------------------------------------------
# 明文落日志（控制台永远不打印明文）
# --------------------------------------------------------------------------
def _flatten(obj, prefix="", depth=0, max_depth=2):
    """把嵌套明文拍平成 (字段路径, 值) 列表，便于对照挑字段。"""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict) and depth < max_depth:
                out.extend(_flatten(v, path, depth + 1, max_depth))
            elif isinstance(v, list) and v and depth < max_depth:
                out.extend(_flatten(v, path, depth + 1, max_depth))
            else:
                out.append((path, v))
    elif isinstance(obj, list):
        if obj:
            out.extend(_flatten(obj[0], f"{prefix}[]", depth + 1, max_depth))
        else:
            out.append((f"{prefix}[]", obj))
    else:
        out.append((prefix, obj))
    return out


def _is_filled(v) -> bool:
    if v is None:
        return False
    if isinstance(v, (str, list, dict)) and len(v) == 0:
        return False
    return True


def _brief(v, limit=48) -> str:
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False)
    elif isinstance(v, str):
        s = v
    else:
        s = str(v)
    s = s.replace("\n", "\\n")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _typename(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _fields_table(data, items, max_depth: int = 2) -> str:
    """字段对照表（字符串形式，交给 logger 只进文件）。"""
    lines: list[str] = ["[字段对照]"]
    if isinstance(data, dict):
        lines.append(f"  顶层({len(data)}): {', '.join(map(str, data.keys()))}")
        res = data.get("Result")
        if isinstance(res, dict):
            meta = {k: v for k, v in res.items() if k != "Items"}
            lines.append(f"  Result({len(res)}): {', '.join(map(str, res.keys()))}")
            lines.append("  Result 元数据: " + json.dumps(meta, ensure_ascii=False))
    if not items:
        return "\n".join(lines)

    order, rows = [], {}
    for it in items:
        for path, val in _flatten(it, "", 0, max_depth):
            row = rows.get(path)
            if row is None:
                row = {"filled": 0, "type": _typename(val), "sample": None, "raw": val}
                rows[path] = row
                order.append(path)
            if _is_filled(val):
                row["filled"] += 1
                if row["sample"] is None:
                    row["sample"] = _brief(val)

    total = len(items)
    # 丢掉"空集合占位行"：同名字段还有子字段时，父行 0/N 没有意义
    order = [
        p
        for p in order
        if not (
            rows[p]["filled"] == 0
            and isinstance(rows[p].get("raw"), (list, dict))
            and any(q.startswith(p + "[") or q.startswith(p + ".") for q in order)
        )
    ]
    width = max((len(p) for p in order), default=10) + 2
    lines.append(
        f"  Items 字段共 {len(order)} 个（嵌套深度<={max_depth}，示例=本页首个非空值）"
    )
    lines.append(
        "  " + "字段路径".ljust(width) + "非空".ljust(9) + "类型".ljust(8) + "示例值"
    )
    for path in order:
        row = rows[path]
        lines.append(
            "  "
            + path.ljust(width)
            + f"{row['filled']}/{total}".ljust(9)
            + row["type"].ljust(8)
            + (row["sample"] or "")
        )
    return "\n".join(lines)


def _samples_text(items: list, show: int) -> str:
    if not items or show <= 0:
        return ""
    lines = [f"[明文样本] 本页 {len(items)} 条，展示前 {min(show, len(items))} 条"]
    for i, it in enumerate(items[:show], 1):
        lines.append(f"  [{i}] {it.get('ProductName')}")
        lines.append(
            f"      价格={it.get('Price')}  店铺={it.get('ShopName')}  "
            f"链接={it.get('Url')}  曝光={it.get('ExposureCount')}"
        )
    rest = len(items) - show
    if rest > 0:
        lines.append(f"  ... 其余 {rest} 条（全文见 decoded_json / --out 文件）")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 备份入库（失败降级，绝不打断采集）
# --------------------------------------------------------------------------
def _store(log, db_enabled: bool, row: dict) -> int | None:
    if not db_enabled:
        return None
    try:
        return database.insert_one(BACKUP_TABLE, row)
    except database.DatabaseError as exc:
        log.warning(f"      入库失败（本次降级为仅写日志）: {exc}")
        return None


def _init_db(log, enabled: bool) -> bool:
    if not enabled:
        log.debug("      数据库: 已用 --no-db 关闭")
        return False
    try:
        count = database.init_schema()
        log.debug(f"      数据库就绪 spider_db.{BACKUP_TABLE}（DDL {count} 句）")
        return True
    except database.DatabaseError as exc:
        log.warning(f"      数据库不可用，本次仅写日志: {exc}")
        return False


# --------------------------------------------------------------------------
# 详情页 SSR 解析落备份（category=page：当前商品详情的唯一来源）
# --------------------------------------------------------------------------
def _store_detail_page(
    log,
    db_enabled: bool,
    run_id: str,
    tokens: Tokens,
    args,
    keyword: str,
    product_id,
    detail_url: str,
    html: str,
    parsed: dict,
) -> int | None:
    """详情页 HTML + 当前商品详情解析结果 -> ``category=page`` 备份行。"""
    row = {
        "run_id": run_id,
        "category": "page",
        "api_name": PAGE_API_NAME.replace("<id>", str(product_id)),
        "api_url": detail_url,
        "keyword": keyword,
        "page_no": None,
        "item_count": len(parsed.get("sku") or []),
        "product_id": int(product_id),
        "detail_url": detail_url,
        "guest_id": args.guest_id,
        "login_flag": None,  # 页面 GET 不带 App-Is-Login 头，登录态看 guest_render
        "http_status": 200,
        "aes_key": tokens.aes_key,  # 列要求非空；HTML 无加密，仅供同 run 溯源
        "iv": tokens.iv,
        "req_body": json.dumps({"Method": "GET", "Cookie": "login"}, ensure_ascii=False),
        "raw_json": html,  # 详情页原始 HTML，原样不加工
        "decoded_json": json.dumps(parsed, ensure_ascii=False),  # SSR 解析结果
        "decrypt_ok": 1 if not parsed.get("guest_render") else 0,
        "error_msg": None,
        "remark": (
            "详情页SSR解析: 批发价/阶梯价/颜色尺码/库存; "
            f"guest_render={parsed.get('guest_render')}"
        )[:500],
    }
    return _store(log, db_enabled, row)


# --------------------------------------------------------------------------
# 详情域单次调用：请求 -> 解密 -> 备份（失败只跳过该接口，不影响同商品其它接口）
# --------------------------------------------------------------------------
def _run_detail_call(
    log,
    db_enabled: bool,
    run_id: str,
    tokens: Tokens,
    args,
    api_key: str,
    call,
    product_id,
    detail_url: str,
    keyword: str = "",
    note: str = "",
) -> str | None:
    """执行一个详情域接口调用并落备份行，返回解密明文（失败返回 None）。

    :param call: 无参可调用对象（延迟绑定，便于逐接口 try/except）。
    :param note: 追加到 remark 的补充说明（如 ``成交记录第2页``）。
    """
    api_name, short, remark = DETAIL_APIS[api_key]
    try:
        result = call()
    except DetailError as exc:
        log.warning(f"      {product_id} {short}跳过: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - HTTP/解包异常都要留痕但不断流
        log.warning(f"      {product_id} {short}请求失败: {exc!r}")
        return None

    assert isinstance(result, PostResult)
    raw_json = json.dumps(result.json, ensure_ascii=False)
    decoded_text, dec_err = _try_decrypt(result.json, tokens)
    meta = _page_meta(decoded_text)

    full_remark = remark + (f"; {note}" if note else "")
    row = {
        "run_id": run_id,
        "category": "detail",
        "api_name": api_name,
        "api_url": result.url,
        "keyword": keyword or None,
        "page_no": None,
        "item_count": meta.get("item_count"),
        "product_id": int(product_id),
        "detail_url": detail_url,
        "guest_id": args.guest_id,
        "login_flag": result.headers.get("App-Is-Login"),
        "http_status": 200,
        "aes_key": tokens.aes_key,  # 每次首页下发的一次性密钥，随行保存
        "iv": tokens.iv,
        "req_body": json.dumps(result.request_body, ensure_ascii=False),
        "raw_json": raw_json,
        "decoded_json": decoded_text,  # 确认项4: 解密明文直接回填详情行
        "decrypt_ok": 1 if decoded_text else 0,
        "error_msg": dec_err,
        "remark": full_remark[:500],
    }
    row_id = _store(log, db_enabled, row)
    run_logger.log_block(log, f"{api_name} 明文 product_id={product_id} {note}".strip(),
                         decoded_text if decoded_text else raw_json)
    log.debug(
        f"      {product_id} {short}{('[' + note + ']') if note else ''} "
        f"解密={'成功' if decoded_text else '失败'} "
        f"明文={len(decoded_text) if decoded_text else 0}B "
        f"备份行={row_id if row_id else '未入库'}"
    )
    return decoded_text


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="17qcc 搜索/详情接口纯协议采集器")
    # 列表接口
    ap.add_argument("--keyword", default="春季男鞋", help="单关键词（--keywords 未给时生效）")
    ap.add_argument(
        "--keywords",
        default="",
        help="批量关键词，逗号分隔，如 春季老爹鞋男,春季板鞋男,复古休闲男鞋（覆盖 --keyword）",
    )
    ap.add_argument(
        "--cateid", default="0",
        help="类目限制 id，0=不限（默认）；如男鞋 50011740，由业务人员按需指定",
    )
    ap.add_argument("--spm", default="1.5.4-4.4")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--page-size", default="83")
    ap.add_argument("--guest-id", default=DEFAULT_GUEST_ID, help="App-GuestId 请求头")
    ap.add_argument("--anon", action="store_true", help="不带登录态(匿名)请求")
    ap.add_argument("--bootstrap-only", action="store_true", help="只拉首页验证 key/iv")
    # 详情接口
    ap.add_argument(
        "--detail-limit",
        type=int,
        default=20,
        help="用列表前 N 条的 ProductId/Url 抓详情（默认20, 风控最小化）, 0=关闭详情阶段",
    )
    ap.add_argument("--no-detail", action="store_true", help="等价 --detail-limit 0")
    ap.add_argument(
        "--detail-apis",
        default=",".join(DETAIL_API_ORDER),
        help="详情域接口选择，逗号分隔: content/shop/grade/trade（默认全采）",
    )
    ap.add_argument(
        "--trade-pages",
        type=int,
        default=2,
        help="成交记录采集页数（1 或 2，默认2；第2页 total_items=第1页实际条数）",
    )
    # 风控节奏
    ap.add_argument("--item-delay", type=float, default=3.0,
                    help="相邻商品详情采集间隔秒数（默认3）")
    ap.add_argument("--keyword-delay", type=float, default=180.0,
                    help="相邻关键词批次间隔秒数（默认180=3分钟）")
    # 输出通道
    ap.add_argument("--no-db", action="store_true", help="不写 MySQL，只写日志")
    ap.add_argument("--no-etl", action="store_true", help="跳过 product_info 业务表 ETL")
    ap.add_argument("--run-id", help="自定义运行标识(默认 YYYYmmdd_HHMMSS)")
    ap.add_argument(
        "--resume",
        default="",
        metavar="RUN_ID",
        help="断点续爬：接续采集指定 run_id（需数据库；"
             "keywords/cateid/detail-apis 等参数与原 run 保持一致）",
    )
    ap.add_argument("--out", help="把解密后的列表明文 JSON 另存到该文件")
    # 明细落日志的开关（不影响控制台：控制台永远只有简要进度）
    ap.add_argument("--full", action="store_true", help="日志文件里写完整明文 JSON")
    ap.add_argument("--show", type=int, default=3, help="日志文件里的明文样本条数")
    ap.add_argument("--no-fields", action="store_true", help="不生成字段对照表")
    ap.add_argument("--fields-depth", type=int, default=2, help="字段对照表嵌套展开深度")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    detail_limit = 0 if args.no_detail else args.detail_limit
    keywords = [k.strip() for k in (args.keywords or "").split(",") if k.strip()]
    if not keywords:
        keywords = [args.keyword]

    # ---- [1/5] 运行初始化：日志文件 + 数据库 ----
    resume_id = (args.resume or "").strip()
    run_id = resume_id or args.run_id or time.strftime("%Y%m%d_%H%M%S")
    log_path = run_logger.init(run_id, mode=_log_mode())
    log = run_logger.get_logger()
    log.info(
        f"[1/5] 运行开始 run_id={run_id} 关键词={len(keywords)}个 "
        f"[{','.join(keywords)}]{'（断点续跑）' if resume_id else ''}"
    )
    log.debug(f"      日志文件: {log_path}")
    db_enabled = _init_db(log, not args.no_db)

    # ---- 断点续爬：api_backup 成功行推导断点快照（不新建状态表） ----
    checkpoint = None
    if resume_id:
        if not db_enabled:
            log.error("[x] --resume 依赖数据库推导断点，--no-db 模式不支持续跑")
            return 2
        checkpoint = _load_checkpoint(log, run_id)
        log.info(
            f"      断点快照: 已完成列表关键词 {len(checkpoint['list_keywords'])} 个"
            f"[{','.join(sorted(checkpoint['list_keywords'])) or '无'}]；"
            f"已有详情数据商品 {len(checkpoint['products'])} 个"
        )

    # 详情接口选择（提前解析，避免每个关键词重复处理）
    wanted = [k.strip() for k in (args.detail_apis or "").split(",") if k.strip()]
    unknown = [k for k in wanted if k not in DETAIL_APIS]
    if unknown:
        log.warning(f"      未知 --detail-apis 项已忽略: {','.join(unknown)}")
    wanted = [k for k in DETAIL_API_ORDER if k in wanted]

    list_ok = 0
    detail_total = 0
    page_total = 0
    counters = {k: 0 for k in DETAIL_API_ORDER}

    resume_stats = {"kw_skipped": 0, "list_reused": 0,
                    "prod_skipped": 0, "prod_partial": 0}
    prev_requested = False  # 上一关键词是否实际发过请求（风控延迟只连着"干活"的关键词）

    for kw_idx, keyword in enumerate(keywords):
        # ---- 断点：列表明文复用 / 全完成关键词整段跳过 ----
        list_decoded: str | None = None
        if checkpoint is not None and keyword in checkpoint["list_keywords"]:
            list_decoded = _load_list_decoded(log, run_id, keyword)
            cp_targets = (
                _extract_targets(list_decoded, detail_limit) if list_decoded else []
            )
            cp_count = (
                _page_meta(list_decoded).get("item_count") if list_decoded else None
            )
            if list_decoded is None or (
                detail_limit > 0 and cp_count and not cp_targets
            ):
                # 明文缺失/提不出目标（结构异常）-> 放弃该关键词的列表断点
                log.warning(f"      keyword={keyword} 列表备份不可用，重采列表")
                list_decoded = None
            else:
                pending = [
                    t for t in cp_targets
                    if not _product_done(checkpoint, t["product_id"], wanted)
                ]
                if not pending:
                    log.info(
                        f"      keyword={keyword} 断点已完成"
                        f"（列表+详情 {len(cp_targets)} 条），整段跳过"
                    )
                    resume_stats["kw_skipped"] += 1
                    list_ok += 1  # 断点已完成视同列表成功（决定退出码）
                    continue
                log.info(
                    f"      keyword={keyword} 列表断点命中，复用备份明文；"
                    f"待补详情 {len(pending)}/{len(cp_targets)} 条"
                )

        # ---- 批次间隔（风控：仅当上一关键词实际发过请求时等待） ----
        if prev_requested and args.keyword_delay > 0:
            log.debug(f"      关键词间隔等待 {args.keyword_delay:.0f}s（风控）...")
            time.sleep(args.keyword_delay)

        # ---- [2/5] 首页密钥（每关键词重新引导，key/iv 一次性；
        #      续跑即使列表复用，详情接口加密仍需本次 tokens） ----
        log.debug(f"[2/5] GET 首页取 key/iv keyword={keyword} ...")
        prev_requested = True  # 即将发首页 GET（成败都算一次请求，风控照等）
        try:
            tokens = bootstrap(keyword=keyword, cateid=args.cateid, spm=args.spm)
        except Exception as exc:  # noqa: BLE001
            log.error(f"[x] 首页引导失败 keyword={keyword}: {exc!r}")
            continue
        run_logger.log_block(
            log, f"首页密钥材料 keyword={keyword}",
            json.dumps(tokens.as_dict(), ensure_ascii=False, indent=2),
        )
        log.debug(f"      密钥就绪 aes_len={len(tokens.aes_key)} iv_len={len(tokens.iv)}")

        if args.bootstrap_only:
            log.info("[完成] --bootstrap-only，未发业务请求")
            return 0

        # ---- [3/5] 列表接口（断点命中：复用备份明文，不发请求、不重复入库） ----
        if list_decoded is not None:
            decoded_text = list_decoded
            meta = _page_meta(decoded_text)
            log.info(
                f"[3/5] {LIST_API_NAME} keyword={keyword} 断点复用备份明文 "
                f"条目={meta.get('item_count')}（本次未发列表请求）"
            )
            list_ok += 1
            resume_stats["list_reused"] += 1
        else:
            params = business_params(
                keyword=keyword, cateid=args.cateid, spm=args.spm,
                page=args.page, page_size=args.page_size,
            )
            log.info(f"[3/5] POST {LIST_API_NAME} keyword={keyword} page={args.page}")
            run_logger.log_block(
                log, "列表请求明文参数", json.dumps(params, ensure_ascii=False, indent=2)
            )
            try:
                result = post(
                    tokens, params, guest_id=args.guest_id, login=not args.anon,
                    with_headers=True,
                )
            except Exception as exc:  # noqa: BLE001 - requests.HTTPError 等
                log.error(f"[x] 列表请求失败 keyword={keyword}: {exc!r}")
                continue

            assert isinstance(result, PostResult)
            resp, rh = result.json, result.headers
            raw_json = json.dumps(resp, ensure_ascii=False)
            run_logger.log_block(log, "列表响应原始报文", raw_json)

            decoded_text, dec_err = _try_decrypt(resp, tokens)
            meta = _page_meta(decoded_text)
            ok = resp.get("success", resp.get("Success", True))

            row = {
                "run_id": run_id,
                "category": "list",
                "api_name": LIST_API_NAME,
                "api_url": result.url,
                "keyword": keyword,
                "page_no": args.page,
                "item_count": meta.get("item_count"),
                "product_id": None,
                "detail_url": None,
                "guest_id": args.guest_id,
                "login_flag": rh.get("App-Is-Login"),
                "http_status": 200,
                # key/iv 每次首页下发都不同（一次性），不随行保存则本行密文日后解不开
                "aes_key": tokens.aes_key,
                "iv": tokens.iv,
                "req_body": json.dumps(result.request_body, ensure_ascii=False),
                "raw_json": raw_json,
                "decoded_json": decoded_text,
                "decrypt_ok": 1 if decoded_text else 0,
                "error_msg": dec_err,
                "remark": (
                    "目标发现接口: 仅取 ProductId/Url; 卡片指标(销量/曝光/点击)由ETL汇总; "
                    f"TotalCount={meta.get('TotalCount')} TotalPages={meta.get('TotalPages')} "
                    f"HasNextPage={meta.get('HasNextPage')}"
                )[:500],
            }
            row_id = _store(log, db_enabled, row)

            log.info(
                f"      HTTP=200 登录={rh.get('App-Is-Login')} "
                f"解密={'成功' if decoded_text else '失败'} "
                f"条目={meta.get('item_count')} 备份行={row_id if row_id else '未入库'}"
            )
            if dec_err:
                log.warning(f"      解密异常(已原样备份 raw_json): {dec_err}")
            if not ok or decoded_text is None:
                log.warning(f"[x] 本关键词列表不可用（原始报文已备份），跳过详情: {keyword}")
                continue
            # 解密成功但 Result=null（登录态失效/风控空响应）也按列表不可用处理，
            # 否则后续 .get 链会以 NoneType AttributeError 崩掉整个批次
            try:
                _parsed = json.loads(decoded_text)
            except json.JSONDecodeError:
                _parsed = None
            if isinstance(_parsed, dict) and _parsed.get("Result") is None:
                log.warning(
                    f"[x] 列表明文 Result=null（App-Is-Login={rh.get('App-Is-Login')}，"
                    f"疑似登录态失效），跳过详情: {keyword}"
                )
                continue
            list_ok += 1

        # ---- 公共：明文展示/落盘 + 目标提取（列表现采/断点复用两条路径共用） ----
        if args.full:
            run_logger.log_block(log, "列表明文全文", decoded_text)
        if args.show > 0:
            _res0 = json.loads(decoded_text).get("Result")
            items0 = _res0.get("Items") if isinstance(_res0, dict) else None
            samples = _samples_text(items0 or [], args.show)
            if samples:
                run_logger.log_block(log, "明文样本", samples)
        if not args.no_fields:
            data = json.loads(decoded_text)
            _res = data.get("Result") if isinstance(data, dict) else None
            items = _res.get("Items") if isinstance(_res, dict) else None
            run_logger.log_block(
                log, "字段对照", _fields_table(data, items or [], args.fields_depth)
            )
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(decoded_text, encoding="utf-8")
            log.info(f"      明文已写入 {args.out}")

        # ---- [4/5] 详情页 SSR 解析 + 详情域接口（采集即解密回填） ----
        targets = _extract_targets(decoded_text, detail_limit)
        log.info(
            f"[4/5] 详情采集 keyword={keyword}: 目标 {len(targets)} 条 x "
            f"{len(wanted)} 接口 [{','.join(wanted) or '无'}] "
            f"(detail_limit={detail_limit}{' --no-detail' if args.no_detail else ''})"
        )
        if args.anon and targets:
            log.info("      --anon 匿名态无详情权限，详情阶段跳过")
            targets = []

        tmp_ver_logged = False
        for idx, t in enumerate(targets):
            pid, url = t["product_id"], t["url"]
            # ---- 断点：page + 所需详情域均有成功行 -> 整商品跳过（不请求、不计延迟） ----
            done_map = checkpoint["products"].get(int(pid), {}) if checkpoint else {}
            missing_apis = [k for k in wanted if not done_map.get(k)]
            if checkpoint is not None and done_map.get("page") and not missing_apis:
                resume_stats["prod_skipped"] += 1
                continue
            # 商品间间隔（风控：默认 3s；仅对实际发请求的商品生效）
            if idx > 0 and args.item_delay > 0:
                time.sleep(args.item_delay)
            if checkpoint is not None and done_map.get("page"):
                resume_stats["prod_partial"] += 1  # page 已完成，重抓仅为取动态参数
            # 1) GET 详情页 HTML（page 缺失=首次采集落库；page 已完成=重抓仅为
            #    取 accessC/pageParam 供缺失接口用，不重复入库）
            try:
                page = fetch_detail_page(url, session=tokens.session, cookie=LOGIN_COOKIE)
            except DetailError as exc:
                log.warning(f"      {pid} 详情页跳过: {exc}")
                continue
            parsed = parse_product_detail(page.html, page.page_param)
            if done_map.get("page"):
                page_row_id = None  # 断点已有成功 page 行，重抓结果不再追加
            else:
                page_row_id = _store_detail_page(
                    log, db_enabled, run_id, tokens, args, keyword, pid, url,
                    page.html, parsed,
                )
            page_total += 1
            if parsed.get("guest_render"):
                log.warning(
                    f"      {pid} 详情页为游客占位(价格/SKU缺失) —— 检查登录Cookie是否过期"
                )
            log.debug(
                f"      {pid} 详情页 价格={parsed.get('wholesale_price')} "
                f"SKU={len(parsed.get('colors') or [])}色"
                f"/{len(parsed.get('sku') and parsed['sku'][0]['sizes'] or [])}码 "
                f"库存={parsed.get('stock_total')} "
                f"备份行={page_row_id if page_row_id else ('断点已有' if done_map.get('page') else '未入库')}"
            )

            # 2) TmpVer 动态提取（确认项2：不硬编码，进程内缓存；
            #    仅缺失 content/shop 时需要）
            tmp_ver = None
            if "content" in missing_apis or "shop" in missing_apis:
                try:
                    tmp_ver = fetch_tmp_ver(html=page.html, session=tokens.session)
                except DetailError as exc:
                    log.warning(f"      {pid} TmpVer 提取失败，本商品跳过: {exc}")
                    continue
                if not tmp_ver_logged:
                    log.debug(f"      TmpVer 动态提取={tmp_ver}（源=bundle combo，进程内缓存）")
                    tmp_ver_logged = True

            # 3) 详情域接口（断点续跑只补缺失域；默认参数绑定到循环变量，
            #    避免闭包拿到最后一次迭代的值）
            calls = {
                "content": lambda p=page, i=pid, v=tmp_ver: post_detail(
                    tokens, i, p.url, p.access_c, v,
                    guest_id=args.guest_id, login=not args.anon,
                ),
                "shop": lambda p=page, v=tmp_ver: post_shop_supplier(
                    tokens, p.page_param, p.access_c, v, p.url,
                    guest_id=args.guest_id, login=not args.anon,
                ),
                "grade": lambda p=page, i=pid: post_product_grade(
                    tokens, i, p.url, guest_id=args.guest_id, login=not args.anon,
                ),
            }
            for key in missing_apis:
                if key == "trade":
                    # 第1页: TotalItems=0（前端初值）
                    trade_p1 = _run_detail_call(
                        log, db_enabled, run_id, tokens, args, "trade",
                        lambda i=pid, u=url: post_trade_records(
                            tokens, i, u, page=1, total_items=0,
                            guest_id=args.guest_id, login=not args.anon,
                        ),
                        pid, url, keyword=keyword, note="第1页",
                    )
                    if trade_p1 is None:
                        continue
                    counters["trade"] += 1
                    # 第2页: total_items=第1页实际条数（实测发0回空，发19/20均正确）
                    if args.trade_pages < 2:
                        continue
                    tmeta = _page_meta(trade_p1)
                    loaded = tmeta.get("item_count") or 0
                    total = tmeta.get("TotalItems") or 0
                    if loaded <= 0 or total <= loaded:
                        log.debug(
                            f"      {pid} 成交记录仅1页(总{total}/已取{loaded})，跳过第2页"
                        )
                        continue
                    trade_p2 = _run_detail_call(
                        log, db_enabled, run_id, tokens, args, "trade",
                        lambda i=pid, u=url, n=loaded: post_trade_records(
                            tokens, i, u, page=2, total_items=n,
                            guest_id=args.guest_id, login=not args.anon,
                        ),
                        pid, url, keyword=keyword, note="第2页",
                    )
                    if trade_p2 is not None:
                        counters["trade"] += 1
                    continue
                if _run_detail_call(
                    log, db_enabled, run_id, tokens, args, key, calls[key], pid, url,
                    keyword=keyword,
                ) is not None:
                    counters[key] += 1

        log.info(f"      keyword={keyword} 批次完成 (详情页 {page_total} 个累计)")

    # ---- [5/5] ETL：备份表 -> product_info 业务表 ----
    if db_enabled and not args.no_etl:
        log.debug("[5/5] ETL 汇总 spider_db.product_info ...")
        try:
            from .etl import run_etl

            etl_rows = run_etl(log, run_id)
            log.info(f"      product_info 写入/更新 {etl_rows} 行")
            # 详情准入：丢弃“列表有 id 但未成功拉取详情”的商品
            # （ETL 把 list 卡片行全量汇总，行数可能超过 detail_limit）
            dropped = _drop_detailless_products(log, run_id)
            if dropped:
                kept = _count_rows("product_info", run_id)
                log.info(
                    f"      丢弃未拉取详情商品 {dropped} 行 -> "
                    f"product_info 保留 {kept} 行（<= detail_limit {detail_limit}）"
                )
        except Exception as exc:  # noqa: BLE001 - ETL 失败不影响已备份数据
            log.warning(f"      ETL 失败（备份数据不受影响）: {exc!r}")
    else:
        reason = "--no-etl" if args.no_etl else "--no-db 未入库"
        log.info(f"[5/5] ETL 跳过（{reason}）")

    db_note = f"spider_db.{BACKUP_TABLE}" if db_enabled else "仅日志(--no-db 未入库)"
    detail_done = sum(counters.values())
    parts = " + ".join(
        f"{DETAIL_APIS[k][1]}{counters[k]}" for k in DETAIL_API_ORDER if counters[k]
    ) or "无"
    log.info(
        f"[完成] 关键词 {list_ok}/{len(keywords)} 成功；详情页 {page_total} 个 + "
        f"详情接口 {detail_done} 条({parts}) -> {db_note}；日志 {log_path}"
    )
    if checkpoint is not None:
        log.info(
            f"      断点续跑: 跳过已完成关键词 {resume_stats['kw_skipped']} 个, "
            f"复用列表明文 {resume_stats['list_reused']} 个, "
            f"跳过已完成商品 {resume_stats['prod_skipped']} 个, "
            f"半成品补采 {resume_stats['prod_partial']} 个"
        )
    if list_ok == 0:
        return 3
    return 0


def _drop_detailless_products(log, run_id: str) -> int:
    """详情准入清理：丢弃未成功拉取详情的商品，返回删除行数（失败返回 0）。

    ETL ``build_product_info_rows`` 把 ``list`` 卡片行全量汇总进
    ``product_info``（含 ``detail_limit`` 之外、详情未采的商品），行数可能
    超过 ``--detail-limit``。本函数在 ETL 之后按 ``api_backup`` 是否存在该
    商品的 ``category='page'`` 行决定去留：

    * 有 page 行（详情页 GET 成功并落备份，游客占位页也算已拉取）-> 保留；
    * 无 page 行（``fetch_detail_page`` 失败/未采/``--no-detail``）-> 删除。

    多关键词 ``--keywords`` 共用同一 ``run_id`` 与一次 ETL，本删除一次执行
    即覆盖整批；``api_backup`` 原始备份不受影响。失败只告警不阻断主流程。
    """
    if not run_id:
        return 0
    sql = (
        "DELETE pi FROM `product_info` pi "
        "LEFT JOIN (SELECT DISTINCT `product_id` FROM `api_backup` "
        "           WHERE `run_id` = %s AND `category` = 'page') d "
        "        ON d.`product_id` = pi.`product_id` "
        "WHERE pi.`run_id` = %s AND d.`product_id` IS NULL"
    )
    try:
        with database.connection() as conn:
            with conn.cursor() as cur:
                affected = cur.execute(sql, (run_id, run_id))
        return int(affected or 0)
    except Exception as exc:  # noqa: BLE001 - 清理失败不阻断主流程
        log.warning(f"      详情准入清理失败（product_info 保留原样）: {exc!r}")
        return 0


def _count_rows(table: str, run_id: str) -> int:
    """Agent 入口统计：按 ``run_id`` 数表行数（表缺失/DB 不可用时返回 0）。"""
    if table not in (BACKUP_TABLE, "product_info"):
        return 0
    try:
        with database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM `{table}` WHERE run_id = %s", (run_id,))
                row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001 - 统计失败不阻断主流程
        return 0


def _log_uncaught(run_id: str, headline: str, exc: BaseException,
                  log_dir: Path | str | None = None) -> None:
    """把未捕获异常留痕到 ``logs/spider_<run_id>.log``（任何路径都不允许静默丢错）。

    * run 日志 handler 就绪（``run_logger`` 当前 run_id 一致）-> 走 logger，
      ERROR 一行进控制台+文件、完整 traceback 仅进文件；
    * handler 未就绪（init 前失败/写日志异常）-> 兜底直接以追加模式写同名文件。
    """
    try:
        if (
            log_dir is None
            and run_logger.current_run_id() == run_id
            and run_logger.current_log_file() is not None
        ):
            run_logger.log_exception(run_logger.get_logger(), exc, headline=headline)
            return
    except Exception:  # noqa: BLE001 - logger 通道失败必须落到文件兜底
        pass
    try:
        directory = Path(log_dir) if log_dir else run_logger.LOG_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"spider_{run_id}.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} ERROR    "
                     f"[spider] {headline}: {exc!r}\n")
            fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:  # noqa: BLE001 - 兜底也失败则无法再处理，交由调用方回传错误
        pass


def run_crawl(
    keyword: str,
    page: int = 1,
    limit: int = 20,
    cateid: str = "0",
    resume: str = "",
) -> dict:
    """Agent 双入口：单关键词「采集 + 清洗 + 入库」全流程（Function-Call Agent 导入调用）。

    与命令行入口 ``python -m src.collector.main`` 共用同一套 ``main()`` 内部逻辑
    （不重复实现）：首页引导 -> 列表目标发现 -> 详情页 SSR + 详情域接口
    （商品间 ``--item-delay`` 默认 3s）-> ETL 清洗写入 ``product_info``。

    参数
    ----
    keyword : 搜索关键词
    page    : 采集页码（对应 ``--page``）
    limit   : 本关键词详情采集商品数上限（对应 ``--detail-limit``）。
              ETL 后仅保留成功拉取详情的商品（``api_backup`` 存在该商品
              ``category='page'`` 行），无详情的列表目标直接丢弃，
              ``product_info`` 行数不超过本上限；列表卡片仍全量备份进
              ``api_backup``（目标发现/销量指标来源）
    cateid  : 类目限制 id（对应 ``--cateid``）。默认 ``"0"``=不限类目；
              如男鞋 ``50011740``，由业务人员按需指定
    resume  : 断点续爬标识（对应 ``--resume <run_id>``）。非空 = 接续采集
              指定 run_id 的中断批次（依赖数据库，按 ``api_backup`` 成功行
              跳过已完成关键词/商品、半成品补采）；keyword/cateid 必须与
              原运行一致。空 = 普通新采集（生成新 run_id）

    返回
    ----
    ``{"success": bool, "count": int, "message": str, "run_id": str,
       "backup_count": int}``

    * ``count``        = 本次清洗写入 ``product_info`` 的条数
      （Agent 判断「采集条数是否为 0」并决定是否终止任务的依据）；
      断点续跑时为该 run_id 的累计条数（幂等 ETL）；
    * ``backup_count`` = 本次 ``api_backup`` 原始备份行数（原始数据入库核对）。
    """
    resume_id = str(resume or "").strip()
    run_id = resume_id or time.strftime("%Y%m%d_%H%M%S")
    argv = [
        "--keyword", str(keyword),
        "--page", str(int(page)),
        "--detail-limit", str(int(limit)),
        "--cateid", str(cateid or "0"),
    ]
    if resume_id:
        # --resume 优先于 --run-id（main() 内 run_id = resume_id），不重复传
        argv += ["--resume", resume_id]
    else:
        argv += ["--run-id", run_id]
    try:
        code = main(argv)
    except Exception as exc:  # noqa: BLE001 - Agent 工具层只回结果不抛异常
        # 爬虫异常必须落运行日志（此前仅返回 message，spider_<run_id>.log 静默断尾）
        _log_uncaught(run_id, f"采集异常终止 keyword={keyword}", exc)
        return {
            "success": False,
            "count": 0,
            "message": f"采集异常终止 keyword={keyword}: {exc!r}",
            "run_id": run_id,
            "backup_count": _count_rows(BACKUP_TABLE, run_id),
        }
    backup = _count_rows(BACKUP_TABLE, run_id)
    clean = _count_rows("product_info", run_id)
    success = code == 0
    return {
        "success": success,
        "count": clean,
        "message": (
            f"keyword={keyword} run_id={run_id}"
            f"{'（断点续跑）' if resume_id else ''} "
            f"api_backup 原始入库 {backup} 行, product_info 清洗入库 {clean} 行"
            + ("" if success else "（列表阶段未成功，请检查关键词/风控）")
        ),
        "keyword": keyword,
        "run_id": run_id,
        "backup_count": backup,
    }


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 命令行异常也必须留痕运行日志
        _log_uncaught(
            run_logger.current_run_id() or time.strftime("%Y%m%d_%H%M%S"),
            "命令行运行异常",
            exc,
        )
        raise
