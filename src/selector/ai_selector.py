"""独立AI选品模块：``ai_select_product`` —— 千问大模型多维度选品评估。

纯独立函数，**不包含任何 agent 工具调用逻辑**：

* 输入：``query_product_list`` 从 MySQL 查出的候选商品字典列表 + 本次筛选条件
  字典（只做归档，不参与条件过滤）；
* **分片批量调用**：候选超过每批上限（默认 10，强制 8~12 区间）自动切片循环
  调用大模型，避免一次性 50~100 条导致上下文超限/token 爆炸；单批失败跳过
  继续下一批（降级），最终合并所有成功批次的结果；
* 模型：百炼千问 ``qwen3.7-flash``（多模态接口 ``MultiModalConversation``，
  百炼新版业务空间调用方式，``workspace`` -> ``X-DashScope-WorkSpace`` 头）；
* 密钥：只读系统环境变量 ``DASHSCOPE_API_KEY``，**禁止硬编码**；
* 输出：内存返回选品结果列表 + 持久化双输出（全部分片合并后统一写一次）：
  1) MySQL 表 ``product_select_result``（``risk_list`` -> ``risk_list_json``，
     ``select_condition`` -> ``select_condition_json``，并记录自增主键
     ``id`` 供审核回写匹配）—— 只 INSERT 新行，
     **绝不修改 product_info 业务表**；
  2) ``output/<时间戳>_ai_select.json``（含各分片原始输出，调试用）
     + ``output/excel/<时间戳>_pending_audit.xlsx``（openpyxl **待审核**
     报表：第一列数据库主键 ``id``，risk_list 逗号拼接，末尾
     ``audit_status``/``audit_operator``/``audit_comment`` 三列留空
     交业务人工填写，填完经 ``excel_audit_import.py`` 批量回写）。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

import dashscope
import pymysql
from dashscope import MultiModalConversation
from dashscope.common.error import DashScopeException, TimeoutException
from openpyxl import Workbook

from src.settings import OUTPUT_DIR  # 路径常量统一由 settings 管理

# ---- 百炼模型与业务空间（新版业务空间调用方式；密钥/业务空间只走环境变量） ----
MODEL = "qwen3.7-flash"
# 业务空间 ID：读环境变量 DASHSCOPE_WORKSPACE_ID（config/.env），
# 留空 = 使用账号默认业务空间（不携带 workspace 参数），禁止硬编码
WORKSPACE_ID = os.environ.get("DASHSCOPE_WORKSPACE_ID", "").strip()

# 分片上限：建议每批 8~12 个商品，默认 10；传入值强制收敛到 [1, 12]
DEFAULT_BATCH_SIZE = 10
MAX_BATCH_SIZE = 12

# 结果字段（输出强制要求，Excel 表头与之一致）
RESULT_FIELDS = (
    "product_id", "product_name", "composite_score",
    "recommend_level", "risk_list", "reason",
)

# ---- 内置 system 系统提示词 ----
SYSTEM_PROMPT = (
    "你是跨境供应链AI选品分析助手，根据传入的候选商品数据做多维度选品评估。\n"
    "评估维度：利润空间、销量规模、库存情况、档口店铺实力、退货&缺货履约风险、"
    "发货时效、新品潜力。\n"
    "注意事项：\n"
    "1.buy_count为平台累计总销量，不等同7天/30天销量；\n"
    "2.complaint_types属于平台全局投诉字典，不可直接判定单个商品风险，仅作为参考；\n"
    "3.NULL代表未采集到数据，不等于数值0。\n"
    "输出强制要求：返回标准json数组，每一个商品结果字段：product_id、product_name、"
    "composite_score(0-100综合得分)、recommend_level(强烈推荐/可考虑/不建议)、"
    "risk_list数组、reason选品理由。\n"
    "禁止输出多余闲聊文本，禁止编造不存在的商品与字段。"
)

# ---- MySQL（只写 product_select_result，不碰 product_info） ----
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `product_select_result` (
    `id`                   BIGINT          NOT NULL AUTO_INCREMENT,
    `product_id`           BIGINT          NOT NULL                COMMENT '商品ID(product_info.product_id)',
    `product_name`         VARCHAR(500)    NULL                    COMMENT '商品标题',
    `composite_score`      DECIMAL(5,2)    NULL                    COMMENT '综合得分0-100',
    `recommend_level`      VARCHAR(16)     NULL                    COMMENT '推荐等级: 强烈推荐/可考虑/不建议',
    `risk_list_json`       JSON            NULL                    COMMENT '风险清单JSON数组(risk_list转json字符串)',
    `reason`               TEXT            NULL                    COMMENT '选品理由',
    `select_condition_json` JSON           NULL                    COMMENT '本次筛选条件JSON(select_condition归档)',
    `audit_status`        VARCHAR(16)    NOT NULL DEFAULT 'pending' COMMENT '人工审核状态: pending/pass/reject/hold',
    `audit_operator`      VARCHAR(64)    NULL                    COMMENT '人工审核人',
    `audit_comment`       TEXT           NULL                    COMMENT '人工审核备注(reject必填驳回原因)',
    `audit_time`          DATETIME       NULL                    COMMENT '人工审核时间',
    `created_at`           DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
    PRIMARY KEY (`id`),
    KEY `idx_product_id` (`product_id`),
    KEY `idx_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='AI选品结果表(只增不改, 源数据在product_info)'
"""

_INSERT_SQL = (
    "INSERT INTO `product_select_result` "
    "(`product_id`, `product_name`, `composite_score`, `recommend_level`, "
    "`risk_list_json`, `reason`, `select_condition_json`) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
)


def _get_connection() -> pymysql.connections.Connection:
    """封装数据库连接（连接参数全部走环境变量，见 config/.env.example）。"""
    return pymysql.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", ""),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "spider_db"),
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10,
        read_timeout=60,
        write_timeout=60,
    )


# --------------------------------------------------------------------------
# 结果解析与校验
# --------------------------------------------------------------------------
def _extract_text(resp) -> str:
    """从多模态响应中抽取纯文本（content 支持 list[dict]/str/对象）。"""
    content = resp.output.choices[0].message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text") or "")
            else:
                parts.append(getattr(part, "text", "") or str(part))
        return "".join(parts)
    return str(content or "")


def _parse_json_array(text: str) -> list:
    """剥掉代码围栏等包装，解析出标准 JSON 数组（失败抛异常）。"""
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # 兜底：截取最外层 [ ... ] 再解析
        start, end = cleaned.find("["), cleaned.rfind("]")
        if start < 0 or end <= start:
            raise
        data = json.loads(cleaned[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("模型输出不是JSON数组")
    return data


def _validate_results(data: list, allowed_ids: set) -> list[dict]:
    """结构校验 + 防编造（product_id 必须在本批候选内），字段归一化。"""
    results: list[dict] = []
    skipped = 0
    for item in data:
        if not isinstance(item, dict):
            skipped += 1
            continue
        try:
            pid = int(item.get("product_id"))
        except (TypeError, ValueError):
            skipped += 1
            continue
        if allowed_ids and pid not in allowed_ids:
            skipped += 1  # 编造的/不在本批候选中的商品直接丢弃
            continue
        score = item.get("composite_score")
        try:
            score = round(float(score), 2)
        except (TypeError, ValueError):
            score = None
        risk = item.get("risk_list")
        if isinstance(risk, str):
            risk = [x.strip() for x in risk.split(",") if x.strip()]
        elif not isinstance(risk, list):
            risk = []
        results.append({
            "product_id": pid,
            "product_name": str(item.get("product_name") or ""),
            "composite_score": score,
            "recommend_level": str(item.get("recommend_level") or "").strip(),
            "risk_list": [str(x) for x in risk],
            "reason": str(item.get("reason") or ""),
        })
    if skipped:
        print(f"[AI选品] 校验剔除 {skipped} 条无效/编造记录")
    if not results:
        raise ValueError("校验后无有效选品结果")
    return results


# --------------------------------------------------------------------------
# 单批次：调用模型 + 解析校验（失败返回 None，不中断后续分片）
# --------------------------------------------------------------------------
def _evaluate_batch(
    batch: list[dict], condition_json: str, label: str,
) -> list[dict] | None:
    """对一个分片（<=12 条）调用千问评估；任何失败返回 None。"""
    allowed_ids = set()
    for row in batch:
        try:
            allowed_ids.add(int(row.get("product_id")))
        except (TypeError, ValueError):
            continue
    candidate_json = json.dumps(batch, ensure_ascii=False, default=str)
    messages = [
        {"role": "system", "content": [{"text": SYSTEM_PROMPT}]},
        {"role": "user", "content": [{"text": (
            "候选商品数据（来自MySQL product_info，JSON数组）：\n"
            f"{candidate_json}\n\n"
            "本次筛选条件（仅归档参考）：\n"
            f"{condition_json}\n\n"
            "请严格按system要求输出JSON数组，只输出JSON本身，不要任何其他文本。"
        )}]},
    ]

    # ---- 调用（异常分三类捕获） ----
    call_kwargs: dict = {
        "model": MODEL,
        "messages": messages,
        "enable_thinking": False,
    }
    if WORKSPACE_ID:  # 配置了业务空间才携带 workspace 参数
        call_kwargs["workspace"] = WORKSPACE_ID
    try:
        resp = MultiModalConversation.call(**call_kwargs)
    except (TimeoutError, ConnectionError, OSError) as exc:
        print(f"[ERROR] {label} 网络异常: {type(exc).__name__}: {exc}")
        return None
    except (TimeoutException, DashScopeException) as exc:
        print(f"[ERROR] {label} 模型调用异常: {type(exc).__name__}: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - 兜底模型/SDK异常
        print(f"[ERROR] {label} 模型调用异常: {type(exc).__name__}: {exc}")
        return None

    if getattr(resp, "status_code", None) != HTTPStatus.OK:
        print(f"[ERROR] {label} 模型调用异常: "
              f"status_code={getattr(resp, 'status_code', None)} "
              f"message={getattr(resp, 'message', resp)}")
        return None
    try:
        raw_text = _extract_text(resp)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] {label} 模型调用异常: 响应结构解析失败 {type(exc).__name__}: {exc}")
        return None
    if not raw_text.strip():
        print(f"[ERROR] {label} 模型调用异常: 返回内容为空")
        return None

    # ---- JSON 解析 + 校验 ----
    try:
        data = _parse_json_array(raw_text)
        results = _validate_results(data, allowed_ids)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[ERROR] {label} JSON解析异常: {exc} | 原始输出前200字: {raw_text[:200]!r}")
        return None

    _evaluate_batch.last_raw_text = raw_text  # 供调试备份收集
    return results


_evaluate_batch.last_raw_text = ""


# --------------------------------------------------------------------------
# 持久化双输出：MySQL + 本地 JSON/Excel（所有分片合并后统一调用一次）
# --------------------------------------------------------------------------
def _persist_mysql(results: list[dict], select_condition: dict) -> list[tuple[int, dict]]:
    """选品结果逐行写入 product_select_result（只增不改，失败不中断）。

    逐行 INSERT 并收集自增主键 ``id``：主键是待审核 Excel 与数据库
    匹配回写的唯一键，返回 ``[(id, 结果行)]``（顺序与 results 一致，
    写库失败的行跳过不入列表）。入库整体失败返回 ``[]``。
    """
    condition_json = json.dumps(select_condition or {}, ensure_ascii=False, default=str)
    inserted: list[tuple[int, dict]] = []
    conn = None
    try:
        conn = _get_connection()
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL)
            for r in results:
                cur.execute(
                    _INSERT_SQL,
                    (
                        r["product_id"], r["product_name"], r["composite_score"],
                        r["recommend_level"],
                        json.dumps(r["risk_list"], ensure_ascii=False),
                        r["reason"], condition_json,
                    ),
                )
                inserted.append((int(cur.lastrowid or 0), r))
    except pymysql.MySQLError as exc:
        print(f"[ERROR] 选品结果入库失败(不影响内存返回): {exc}")
    finally:
        if conn is not None:
            conn.close()
    if inserted:
        print(f"[持久化] MySQL spider_db.product_select_result +{len(inserted)} 行")
    return inserted


def _persist_files(
    results: list[dict], select_condition: dict, raw_texts: str,
    candidate_count: int, ts: str, batch_meta: dict,
    inserted: list[tuple[int, dict]] | None = None,
) -> tuple[Path | None, Path | None]:
    """落两份本地文件：完整JSON备份（调试）+ 待审核Excel（人工审核回写用）。

    待审核 Excel（``{ts}_pending_audit.xlsx``，存 ``output/excel/``）：
    第一列 = 数据库主键 ``id``（回写匹配键，绝不能缺），其后为 AI 原始
    6 字段，末尾追加 ``audit_status`` / ``audit_operator`` /
    ``audit_comment`` 三列**空字符串**，交业务人员手工填写。
    """
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        # ① 完整原始选品结果 JSON 备份（含各分片原始输出）
        payload = {
            "generated_at": ts,
            "model": MODEL,
            "workspace_id": WORKSPACE_ID,
            "candidate_count": candidate_count,
            **batch_meta,
            "select_condition": select_condition,
            "raw_text": raw_texts,
            "results": results,
        }
        json_path = OUTPUT_DIR / f"{ts}_ai_select.json"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        # ② 待审核 Excel（id 主键 + AI 6 字段 + 3 个空审核列）
        inserted = inserted or []
        if len(inserted) != len(results):
            print(
                f"[WARN] 待审核Excel仅包含成功入库的 {len(inserted)}/{len(results)} 行"
                "（未入库行无主键id，无法回写审核结果）"
            )
        audit_headers = ["audit_status", "audit_operator", "audit_comment"]
        xlsx_path = OUTPUT_DIR / "excel" / f"{ts}_pending_audit.xlsx"
        xlsx_path.parent.mkdir(parents=True, exist_ok=True)
        wb = Workbook()
        ws = wb.active
        ws.title = "待审核选品结果"
        ws.append(["id", *RESULT_FIELDS, *audit_headers])
        for row_id, r in inserted:
            ws.append([
                row_id,
                r["product_id"], r["product_name"], r["composite_score"],
                r["recommend_level"], ",".join(r["risk_list"]), r["reason"],
                "", "", "",  # 三列审核字段生成时全部为空，业务人员手工填写
            ])
        for col, width in zip(
            "ABCDEFGHIJ", (10, 12, 42, 16, 14, 34, 70, 14, 14, 30)
        ):
            ws.column_dimensions[col].width = width
        wb.save(xlsx_path)
        print(f"[持久化] JSON: {json_path.name}")
        print(f"[持久化] 待审核Excel: excel/{xlsx_path.name}"
              "（id列+末尾三列待人工填写）")
        return json_path, xlsx_path
    except (OSError, TypeError, ValueError) as exc:
        print(f"[ERROR] 本地文件写出失败(不影响内存返回): {exc}")
        return None, None


# --------------------------------------------------------------------------
# 主函数
# --------------------------------------------------------------------------
def ai_select_product(
    candidate_list: list[dict],
    select_condition: dict | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[dict]:
    """千问大模型AI选品评估（纯独立函数，无 agent 工具调用）。

    候选超过 ``batch_size`` 时自动分片循环调用大模型（每批上限 12 条，
    避免一次性传入 50~100 条造成上下文超限/token 爆炸），单批失败跳过
    继续，最终合并所有成功批次的结果统一持久化。

    :param candidate_list: ``query_product_list`` 返回的候选商品字典列表
    :param select_condition: 本次筛选条件字典（归档用，不参与过滤）
    :param batch_size: 每批商品数（建议 8~12，默认 10，强制上限 12）
    :returns: 选品结果字典列表；全部失败或入参为空时返回 ``[]``
    """
    if not candidate_list:
        print("[AI选品] 候选商品列表为空，跳过评估")
        return []
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        print("[ERROR] 环境变量 DASHSCOPE_API_KEY 未设置，禁止硬编码密钥")
        return []
    dashscope.api_key = api_key

    # ---- 分片参数收敛：每批最多 12 条（建议 8~12，默认 10） ----
    try:
        batch_size = int(batch_size)
    except (TypeError, ValueError):
        batch_size = DEFAULT_BATCH_SIZE
    batch_size = max(1, min(batch_size, MAX_BATCH_SIZE))

    condition_json = json.dumps(select_condition or {}, ensure_ascii=False, default=str)
    chunks = [
        candidate_list[i:i + batch_size]
        for i in range(0, len(candidate_list), batch_size)
    ]
    total = len(chunks)
    print(f"[AI选品] 候选 {len(candidate_list)} 条 -> 分 {total} 片"
          f"（每批<={batch_size}）调用 {MODEL} (workspace={WORKSPACE_ID})")

    # ---- 分片循环调用大模型 ----
    merged: list[dict] = []
    seen: set[int] = set()
    raw_parts: list[str] = []
    ok_batches = 0
    for idx, batch in enumerate(chunks, 1):
        label = f"分片 {idx}/{total}"
        print(f"[AI选品] {label} 本批 {len(batch)} 条 ...")
        results = _evaluate_batch(batch, condition_json, label)
        if results is None:
            print(f"[WARN] {label} 失败，跳过并继续下一批")
            continue
        ok_batches += 1
        raw_parts.append(_evaluate_batch.last_raw_text)
        new_rows = 0
        for r in results:
            if r["product_id"] in seen:
                continue
            seen.add(r["product_id"])
            merged.append(r)
            new_rows += 1
        print(f"[AI选品] {label} 完成 +{new_rows} 条，累计 {len(merged)} 条")

    if not merged:
        print("[ERROR] 所有分片均无有效选品结果")
        return []
    print(f"[AI选品] 汇总 {len(merged)} 条有效结果（成功分片 {ok_batches}/{total}）")

    # ---- 持久化双输出（合并后统一写一次） ----
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    batch_meta = {
        "batch_size": batch_size,
        "batch_count": total,
        "batches_ok": ok_batches,
    }
    inserted = _persist_mysql(merged, select_condition or {})
    _persist_files(
        merged, select_condition or {}, "\n\n".join(raw_parts),
        len(candidate_list), ts, batch_meta, inserted,
    )
    return merged
