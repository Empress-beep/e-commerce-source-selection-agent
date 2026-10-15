# -*- coding: utf-8 -*-
"""P2 素材(图片)生成：审核通过商品 -> 千问生图 -> 四道关 -> 落库 + 待人工审核 Excel。

设计决策（与产品文档同步，勿随意改动）：
----------------------------------------
* 入口 = ``product_material.audit_status='pass'`` 的商品（素材文案 pass 已蕴含选品 pass）；
* 每商品固定 2 个槽位：``scene``（场景氛围图）+ ``selling``（卖点特写图）；
  **两槽位都传 ``product_info.cover_photo`` 做 i2i 参考**（保证同商品视觉一致，
  避免纯文生图质感偏差）；主图缺失时该槽位降级 t2i；
* 质检失败的图移入 ``output/images/<pid>/rejected/`` 子目录保留回溯，不进审核 Excel；
* 调试 JSON ``attempts`` 字段逐次记录：model / origin_url / image_path / qc_result /
  error，额度去向与失败原因全量可追溯；
* 生图模型：``qwen-image-3.0``（同一视觉模型，无降级链/无付费兜底）；
  有主图时 scene+selling 均走 i2i，主图缺失才走 t2i；
* **免费额度耗尽即停**：识别配额类错误 -> 当前及剩余待生成槽位一律
  ``gen_status='failed'``（``gen_error='免费额度耗尽'``）；
* 成败都落库（failed 记 gen_error / qc_reasons_json，断点续跑基础）；Excel 全量收录
  本批 success+failed（与 DB 对齐），一行一图按 product_id 分组，失败行标
  ``N/A(失败)`` + gen_error；
* 四道关：①技术校验（下载/PIL解码/尺寸体积）②内容合规（API审核+watermark=false）
  ③AI看图质检（A 图像质量 / B 事实一致，两道子检查）④人工 Excel；
* 调用方式：DashScope ``ImageGeneration.call`` 同步 multimodal-generation 端点
  （T2I content=[{text}]；I2I content=[{text},{image:主图}]）。

测试入口::

    python src/material/image_generator.py --product-ids 1,2
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

from dashscope import MultiModalConversation
from dashscope.aigc.image_generation import ImageGeneration
from dashscope.common.error import DashScopeException, TimeoutException
from openpyxl import Workbook

# sys.path 引导：本文件在 src/material/ 下，parents[2] = 项目根
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.common import database  # noqa: E402
from src.material.generator import (  # noqa: E402
    _extract_text,
    _loads,
    _parse_json_object,
    _query_info_rows,
)
from src.settings import OUTPUT_DIR  # noqa: E402

# ---------------------------------------------------------------------------
# 模型与调度配置
# ---------------------------------------------------------------------------
QC_MODEL = os.environ.get("IMAGE_QC_MODEL", "qwen3.7-flash-2026-07-15")
WORKSPACE_ID = os.environ.get("DASHSCOPE_WORKSPACE_ID", "").strip()
IMAGE_MODEL = "qwen-image-3.0"
PROMPT_VERSION = "PV-20261015-1"
MAX_RETRY = 2
ITEM_DELAY_SECONDS = 4.0
IMAGE_SIZE = "1328*1328"

TABLE = "product_image"
INFO_TABLE = "product_info"
MATERIAL_TABLE = "product_material"
SLOTS = ("scene", "selling")

_QUOTA_MARKERS = (
    "RateQuota", "AllocationQuota", "InsufficientQuota", "quota",
    "额度", "Throttling", "Rate limit", "rate_limit",
)

EXCEL_COLUMNS = (
    "id", "product_id", "slot", "product_name", "gen_status", "gen_error",
    "model", "source",
    "image_path", "image_link", "prompt", "machine_check",
    "qc_result", "qc_reasons",
    "audit_status", "audit_operator", "audit_comment",
)

_NEGATIVE_HINT = (
    "画面要求：无任何文字/水印/logo文字/乱码字符，主体结构正常（无多余肢体、无畸形），"
    "构图清晰不模糊，颜色与商品事实一致，电商主图风格干净背景。"
)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `product_image` (
    `id`                BIGINT          NOT NULL AUTO_INCREMENT,
    `product_id`        BIGINT          NOT NULL                COMMENT '商品ID(product_info.product_id)',
    `slot`              VARCHAR(16)     NOT NULL                COMMENT '图片槽位: scene=场景图 / selling=卖点图（有主图时均走i2i）',
    `gen_status`        VARCHAR(16)     NOT NULL DEFAULT 'pending' COMMENT '机器生成状态: pending/generating/success/failed',
    `gen_error`         VARCHAR(500)    NULL                    COMMENT '生成失败原因(gen_status=failed; 含免费额度耗尽)',
    `prompt`            TEXT            NULL                    COMMENT '实际使用的生图prompt(含事实约束与负向词)',
    `source`            VARCHAR(8)      NULL                    COMMENT '生成方式: t2i=文生图 / i2i=图生图',
    `image_path`        VARCHAR(512)    NULL                    COMMENT '本地图片相对路径(output/images/<product_id>/...)',
    `origin_url`        VARCHAR(1024)   NULL                    COMMENT '模型返回的原始图片URL(下载前)',
    `model`             VARCHAR(64)     NULL                    COMMENT '实际使用的生图模型',
    `prompt_version`    VARCHAR(32)     NULL                    COMMENT '提示词版本(调优钩子, 如 PV-20261015-1)',
    `machine_check`     VARCHAR(500)    NULL                    COMMENT '技术校验结论: pass 或 fail:原因摘要',
    `qc_result`         VARCHAR(16)     NULL                    COMMENT 'AI看图质检结论: pass / fail',
    `qc_reasons_json`   JSON            NULL                    COMMENT 'AI质检失败原因JSON数组(带原因重试依据)',
    `audit_status`      VARCHAR(16)     NULL                    COMMENT '人工审核状态: NULL=待审 pass/hold/reject',
    `audit_operator`    VARCHAR(64)     NULL                    COMMENT '人工审核人',
    `audit_comment`     TEXT            NULL                    COMMENT '人工审核备注(reject必填原因)',
    `audit_time`        DATETIME        NULL                    COMMENT '人工审核时间',
    `created_at`        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
    `updated_at`        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最近更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_product_slot` (`product_id`, `slot`),
    KEY `idx_gen_status` (`gen_status`),
    KEY `idx_audit_status` (`audit_status`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci
  COMMENT = 'AI素材(图片)生成表: 素材pass商品 -> 千问生图; 双状态机同构product_material';
"""


class QuotaExhaustedError(RuntimeError):
    """免费生图额度耗尽/限流配额——整批停跑信号。"""


# ---------------------------------------------------------------------------
# 基础小工具
# ---------------------------------------------------------------------------
def _api_key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("缺少系统环境变量 DASHSCOPE_API_KEY（禁止硬编码 API-KEY）")
    return key


def _is_quota_error(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker.lower() in lowered for marker in _QUOTA_MARKERS)


def _parse_colors(info_row: dict) -> list[str]:
    raw = info_row.get("colors")
    data = _loads(raw)
    if not isinstance(data, list):
        return []
    names: list[str] = []
    for item in data:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name:
                names.append(name)
        elif isinstance(item, str) and item.strip():
            names.append(item.strip())
    return names


def _get_brief(material_row: dict | None) -> dict:
    if not material_row:
        return {}
    content = _loads(material_row.get("content_json"))
    if not isinstance(content, dict):
        return {}
    brief = content.get("brief")
    return brief if isinstance(brief, dict) else {}


# ---------------------------------------------------------------------------
# Prompt 组装（模板拼接 + 事实约束 + 负向词）
# ---------------------------------------------------------------------------
def _build_prompt(slot: str, info_row: dict, brief: dict, feedback: str = "") -> str:
    """按槽位组装生图 prompt：brief 四件套 + 品类/颜色事实 + 负向词。

    :raises ValueError: brief 必填字段缺失。
    """
    product_name = str(info_row.get("product_name") or "").strip()
    if not product_name:
        raise ValueError("product_info.product_name 为空，无法组装 prompt")
    colors = _parse_colors(info_row)
    color_text = "、".join(colors[:5]) if colors else "以商品主图颜色为准"
    cover = str(info_row.get("cover_photo") or "").strip()
    has_ref = bool(cover)

    if slot == "scene":
        scene = str(brief.get("scene") or "").strip()
        style = str(brief.get("style") or "").strip()
        palette = str(brief.get("palette") or "").strip()
        if not scene:
            raise ValueError("brief.scene 缺失，无法组装场景图 prompt")
        parts = [
            f"电商场景氛围图：{scene}",
            f"商品主体：{product_name}",
        ]
        if has_ref:
            parts.append("严格保持与参考图同款同色同材质，只更换背景场景，商品外观不得改变")
        parts.append(f"颜色（限事实）：{color_text}")
        if style:
            parts.append(f"风格：{style}")
        if palette:
            parts.append(f"色调：{palette}")
        parts.append(_NEGATIVE_HINT)
        if feedback:
            parts.append(f"【修正要求】上一次生成未通过质检：{feedback}。请生成时避免同样问题。")
        return "\n".join(parts)

    if slot == "selling":
        point = str(brief.get("selling_point") or "").strip()
        style = str(brief.get("style") or "").strip()
        if not point:
            raise ValueError("brief.selling_point 缺失，无法组装卖点图 prompt")
        parts = [
            f"商品卖点特写图：{point}",
            f"商品主体：{product_name}，严格保持与参考图同款同色",
            f"颜色（限事实）：{color_text}",
        ]
        if style:
            parts.append(f"风格：{style}")
        parts.append(_NEGATIVE_HINT)
        if feedback:
            parts.append(f"【修正要求】上一次生成未通过质检：{feedback}。请生成时避免同样问题。")
        return "\n".join(parts)

    raise ValueError(f"未知图片槽位: {slot}")


def _validate_prompt_facts(prompt: str, info_row: dict) -> None:
    product_name = str(info_row.get("product_name") or "").strip()
    if product_name and product_name[:8] not in prompt:
        raise ValueError(f"prompt 缺少商品标题事实: {product_name[:12]}")


# ---------------------------------------------------------------------------
# 模型调用（同步 multimodal-generation + 额度识别）
# ---------------------------------------------------------------------------
def _extract_image_url(resp) -> str:
    output = getattr(resp, "output", None)
    choices = getattr(output, "choices", None) or []
    if not choices:
        raise RuntimeError(f"生图响应缺少 choices: message={getattr(resp, 'message', resp)}")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                url = part.get("image") or part.get("url")
                if url:
                    return str(url)
    raise RuntimeError("生图响应 content 中未找到 image URL")


def _call_image_model(prompt: str, ref_image: str | None) -> tuple[str, str]:
    """调用生图模型，返回 ``(url, model)``。

    :raises QuotaExhaustedError: 配额/额度错误。
    :raises RuntimeError: 其他调用异常。
    """
    content: list[dict] = [{"text": prompt}]
    if ref_image:
        content.append({"image": ref_image})
    messages = [{"role": "user", "content": content}]
    try:
        resp = ImageGeneration.call(
            model=IMAGE_MODEL,
            messages=messages,
            api_key=_api_key(),
            **({"workspace": WORKSPACE_ID} if WORKSPACE_ID else {}),
            size=IMAGE_SIZE,
            n=1,
            watermark=False,
            prompt_extend=False,
        )
    except (TimeoutError, ConnectionError, OSError) as exc:
        raise RuntimeError(f"网络异常 {type(exc).__name__}: {exc}") from exc
    except (TimeoutException, DashScopeException) as exc:
        text = f"{type(exc).__name__}: {exc}"
        if _is_quota_error(text):
            raise QuotaExhaustedError(f"{IMAGE_MODEL}: {text}") from exc
        raise RuntimeError(f"生图调用异常 {text}") from exc
    except Exception as exc:  # noqa: BLE001
        text = f"{type(exc).__name__}: {exc}"
        if _is_quota_error(text):
            raise QuotaExhaustedError(f"{IMAGE_MODEL}: {text}") from exc
        raise RuntimeError(f"生图调用异常 {text}") from exc

    if getattr(resp, "status_code", None) != HTTPStatus.OK:
        message = str(getattr(resp, "message", resp))
        code = str(getattr(resp, "code", "") or "")
        text = f"{code} {message}"
        if _is_quota_error(text):
            raise QuotaExhaustedError(f"{IMAGE_MODEL}: {text}")
        raise RuntimeError(
            f"生图失败 status_code={getattr(resp, 'status_code', None)} message={message}"
        )
    try:
        return _extract_image_url(resp), IMAGE_MODEL
    except RuntimeError as exc:
        raise RuntimeError(f"生图响应解析失败: {exc}") from exc


# ---------------------------------------------------------------------------
# 技术校验与下载（关卡①）
# ---------------------------------------------------------------------------
def _looks_like_data_url(text: str) -> bool:
    if text.startswith("data:image"):
        return True
    head = text[:200] or ""
    return len(text) > 200 and "://" not in text[:40] and re.fullmatch(
        r"[A-Za-z0-9+/=\s]+", head
    ) is not None


def _download_image(url_or_b64: str, dest: Path) -> Path:
    """下载/解码图片到 dest，PIL 技术校验（尺寸/体积），返回落盘路径。"""
    import requests
    from PIL import Image

    dest.parent.mkdir(parents=True, exist_ok=True)
    if _looks_like_data_url(url_or_b64):
        raw = url_or_b64.split(",", 1)[1] if "," in url_or_b64 else url_or_b64
        try:
            data = base64.b64decode(raw, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"base64 解码失败: {exc}") from exc
    else:
        try:
            resp = requests.get(url_or_b64, timeout=60)
            resp.raise_for_status()
            data = resp.content
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"图片下载失败: {exc}") from exc

    if len(data) < 5 * 1024:
        raise RuntimeError(f"图片体积异常偏小: {len(data)}B")
    dest.write_bytes(data)
    try:
        with Image.open(dest) as im:
            im.verify()
        with Image.open(dest) as im:
            width, height = im.size
            if width < 512 or height < 512:
                raise RuntimeError(f"图片分辨率不达标: {width}x{height}")
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"PIL 解码失败: {exc}") from exc
    return dest


# ---------------------------------------------------------------------------
# AI 看图质检（关卡③：A 图像质量 / B 事实一致）
# ---------------------------------------------------------------------------
_QC_SYSTEM = (
    "你是电商主图质检员。对给定图片做两道子检查并只输出 JSON：\n"
    "A. 图像质量：无文字/水印/乱码，主体结构正常（无多余肢体/畸形），画面清晰不模糊；\n"
    "B. 事实一致：主体品类与商品一致；颜色在允许颜色列表内；与场景描述匹配。\n"
    '输出格式：{"pass": true/false, "reasons": ["原因1", "原因2"]}，只输出 JSON 本身。'
)


def _qc_image_with_qwen(image_path: Path, prompt: str, info_row: dict) -> tuple[bool, list[str]]:
    """千问看图质检：两道子检查，返回 ``(pass, reasons)``。"""
    colors = _parse_colors(info_row)
    user_text = (
        f"【生图prompt】\n{prompt}\n\n"
        f"【商品标题】{info_row.get('product_name')}\n"
        f"【允许颜色】{'、'.join(colors) if colors else '以主图为准'}\n"
        "请按 System 规范输出 JSON。"
    )
    messages = [
        {"role": "system", "content": [{"text": _QC_SYSTEM}]},
        {"role": "user", "content": [
            {"text": user_text},
            {"image": str(image_path)},
        ]},
    ]
    try:
        resp = MultiModalConversation.call(
            model=QC_MODEL,
            **({"workspace": WORKSPACE_ID} if WORKSPACE_ID else {}),
            messages=messages,
            api_key=_api_key(),
            enable_thinking=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"质检模型调用异常 {type(exc).__name__}: {exc}") from exc
    if getattr(resp, "status_code", None) != HTTPStatus.OK:
        raise RuntimeError(f"质检调用失败 message={getattr(resp, 'message', resp)}")
    raw = _extract_text(resp)
    data = _parse_json_object(raw)
    passed = bool(data.get("pass"))
    reasons_raw = data.get("reasons") or []
    reasons = (
        [str(r) for r in reasons_raw]
        if isinstance(reasons_raw, list)
        else [str(reasons_raw)]
    )
    return passed, reasons


# ---------------------------------------------------------------------------
# DB 生命周期（gen_status 断点；一行一图 UNIQUE(product_id, slot)）
# ---------------------------------------------------------------------------
_table_ready = False


def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    with database.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL)
    _table_ready = True


def _query_material_pass_ids(product_ids: list | None = None) -> list[int]:
    """查素材审核 pass 的去重商品 ID（入口=product_material.audit_status='pass'）。"""
    rows = database.fetch_all(
        MATERIAL_TABLE,
        where="audit_status=%s",
        params=["pass"],
        cols="product_id",
    )
    pass_ids = sorted({int(r["product_id"]) for r in rows if r.get("product_id")})
    if not product_ids:
        return pass_ids
    wanted: set[int] = set()
    for x in product_ids:
        try:
            wanted.add(int(x))
        except (TypeError, ValueError):
            continue
    return [pid for pid in pass_ids if pid in wanted]


def _query_material_rows(pids: list[int]) -> dict[int, dict]:
    if not pids:
        return {}
    placeholders = ", ".join(["%s"] * len(pids))
    rows = database.fetch_all(
        MATERIAL_TABLE,
        where=f"product_id IN ({placeholders})",
        params=pids,
    )
    out: dict[int, dict] = {}
    for row in rows:
        try:
            pid = int(row.get("product_id"))
        except (TypeError, ValueError):
            continue
        out[pid] = row
    return out


def _query_image_rows(pids: list[int]) -> dict[tuple[int, str], dict]:
    if not pids:
        return {}
    placeholders = ", ".join(["%s"] * len(pids))
    rows = database.fetch_all(
        TABLE,
        where=f"product_id IN ({placeholders})",
        params=pids,
    )
    out: dict[tuple[int, str], dict] = {}
    for row in rows:
        try:
            key = (int(row.get("product_id")), str(row.get("slot")))
        except (TypeError, ValueError):
            continue
        out[key] = row
    return out


def _insert_pending_slots(pids: list[int]) -> int:
    """预写 pending 行（仅补不存在的 product_id+slot，不动已有行）。"""
    existing = _query_image_rows(pids)
    rows = []
    for pid in pids:
        for slot in SLOTS:
            if (pid, slot) not in existing:
                rows.append({"product_id": pid, "slot": slot, "gen_status": "pending"})
    return database.insert_many(TABLE, rows) if rows else 0


def _mark_generating(pid: int, slot: str) -> None:
    with database.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE `product_image` SET `gen_status`='generating', "
                "`gen_error`=NULL WHERE `product_id`=%s AND `slot`=%s",
                (pid, slot),
            )


def _mark_success(pid: int, slot: str, payload: dict) -> None:
    """落库成功行；audit 四字段重置（新版本图片需重新人工审核）。"""
    with database.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE `product_image` SET "
                "`gen_status`='success', `gen_error`=NULL, "
                "`prompt`=%s, `source`=%s, `image_path`=%s, `origin_url`=%s, "
                "`model`=%s, `prompt_version`=%s, `machine_check`=%s, "
                "`qc_result`=%s, `qc_reasons_json`=%s, "
                "`audit_status`=NULL, `audit_operator`=NULL, "
                "`audit_comment`=NULL, `audit_time`=NULL "
                "WHERE `product_id`=%s AND `slot`=%s",
                (
                    payload.get("prompt"),
                    payload.get("source"),
                    payload.get("image_path"),
                    payload.get("origin_url"),
                    payload.get("model"),
                    PROMPT_VERSION,
                    payload.get("machine_check", "pass"),
                    payload.get("qc_result", "pass"),
                    json.dumps(payload.get("qc_reasons") or [], ensure_ascii=False),
                    pid,
                    slot,
                ),
            )


def _mark_failed(
    pid: int, slot: str, error: str, qc_reasons: list[str] | None = None
) -> None:
    with database.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE `product_image` SET "
                "`gen_status`='failed', `gen_error`=%s, `qc_reasons_json`=%s "
                "WHERE `product_id`=%s AND `slot`=%s",
                (
                    str(error)[:500],
                    json.dumps(qc_reasons or [], ensure_ascii=False),
                    pid,
                    slot,
                ),
            )


# ---------------------------------------------------------------------------
# 单槽位流水线（prompt -> 模型 -> 下载 -> 两道质检；失败带原因重试）
# ---------------------------------------------------------------------------
def _quarantine_rejected(dest: Path) -> Path:
    """质检失败的图移入 rejected/ 子目录（保留可回溯，不进审核 Excel）。"""
    rejected_dir = dest.parent / "rejected"
    rejected_dir.mkdir(parents=True, exist_ok=True)
    target = rejected_dir / dest.name
    if dest.exists():
        dest.rename(target)
    return target


def _generate_one_slot(
    pid: int, slot: str, info_row: dict, brief: dict
) -> tuple[bool, dict | str, list[dict]]:
    """单槽位完整流水线。

    :returns: ``(成功?, 成功载荷dict|失败原因, 逐次attempt追踪列表)``；
              配额耗尽抛 ``QuotaExhaustedError``（上层停批）。
    """
    cover = str(info_row.get("cover_photo") or "").strip()
    ref_image = cover or None  # scene/selling 都传主图，保证视觉一致

    feedback = ""
    last_err = "未执行"
    attempts: list[dict] = []
    for attempt_no in range(1, 2 + MAX_RETRY):  # 1 首试 + MAX_RETRY 次重试
        attempt: dict = {
            "product_id": pid,
            "slot": slot,
            "attempt": attempt_no,
            "source": "i2i" if ref_image else "t2i",
            "ref_image": ref_image,
            "model": None,
            "origin_url": None,
            "image_path": None,
            "qc_result": None,
            "qc_reasons": None,
            "error": None,
        }
        attempts.append(attempt)
        try:
            prompt = _build_prompt(slot, info_row, brief, feedback)
            _validate_prompt_facts(prompt, info_row)
        except ValueError as exc:
            return False, f"prompt 组装失败: {exc}", attempts
        attempt["prompt"] = prompt[:500]
        try:
            url, model_used = _call_image_model(prompt, ref_image)
        except QuotaExhaustedError:
            attempt["error"] = "免费额度耗尽"
            raise
        except RuntimeError as exc:
            last_err = str(exc)
            feedback = last_err
            attempt["error"] = last_err
            continue
        attempt["model"] = model_used
        attempt["origin_url"] = (url or "")[:200]

        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        dest = OUTPUT_DIR / "images" / str(pid) / f"{slot}_{ts}.png"
        try:
            _download_image(url, dest)
        except RuntimeError as exc:
            last_err = str(exc)
            feedback = last_err
            attempt["error"] = last_err
            continue

        try:
            qc_pass, reasons = _qc_image_with_qwen(dest, prompt, info_row)
        except RuntimeError as exc:
            last_err = str(exc)
            feedback = last_err
            attempt["error"] = last_err
            rejected = _quarantine_rejected(dest)
            attempt["image_path"] = _rel_or_abs(rejected)
            continue
        if not qc_pass:
            last_err = "AI质检未通过: " + "；".join(reasons[:4])
            feedback = last_err
            attempt["error"] = last_err
            attempt["qc_result"] = "fail"
            attempt["qc_reasons"] = reasons
            rejected = _quarantine_rejected(dest)
            attempt["image_path"] = _rel_or_abs(rejected)
            continue

        rel_path = _rel_or_abs(dest)
        attempt["image_path"] = rel_path
        attempt["qc_result"] = "pass"
        attempt["qc_reasons"] = reasons
        return True, {
            "prompt": prompt,
            "source": attempt["source"],
            "image_path": rel_path,
            "origin_url": url[:1024],
            "model": model_used,
            "machine_check": "pass",
            "qc_result": "pass",
            "qc_reasons": reasons,
        }, attempts
    return False, last_err, attempts


def _rel_or_abs(path: Path) -> str:
    if path.is_relative_to(OUTPUT_DIR):
        return str(path.relative_to(OUTPUT_DIR))
    return str(path)


# ---------------------------------------------------------------------------
# Excel 导出（一行一图 / 按 product_id 分组 / HYPERLINK）
# ---------------------------------------------------------------------------
def _export_audit_excel(
    image_rows: list[dict], info_rows: dict[int, dict], ts: str
) -> Path:
    """导出 Excel：一行一图，按 product_id 分组；success+failed 全量收录与 DB 对齐。

    成功行：图片列 HYPERLINK，audit 三列留空待人工填；
    失败行：gen_status/gen_error 标明失败原因，audit 三列留空（无需审）。
    """
    excel_dir = OUTPUT_DIR / "excel"
    excel_dir.mkdir(parents=True, exist_ok=True)
    path = excel_dir / f"{ts}_pending_image_audit.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "图片审核"
    ws.append(list(EXCEL_COLUMNS))

    slot_order = {slot: idx for idx, slot in enumerate(SLOTS)}
    status_order = {"success": 0, "failed": 1, "generating": 2, "pending": 3}
    ordered = sorted(
        image_rows,
        key=lambda r: (
            int(r.get("product_id") or 0),
            slot_order.get(str(r.get("slot")), 99),
            status_order.get(str(r.get("gen_status") or ""), 9),
            int(r.get("id") or 0),
        ),
    )
    for m in ordered:
        info = info_rows.get(int(m.get("product_id") or 0)) or {}
        rel = str(m.get("image_path") or "")
        abs_path = (OUTPUT_DIR / rel).resolve() if rel else None
        link = None
        if abs_path and abs_path.exists():
            posix = str(abs_path).replace("\\", "/")
            link = f'=HYPERLINK("file:///{posix}","打开图片")'
        reasons = _loads(m.get("qc_reasons_json"))
        gen_status = str(m.get("gen_status") or "")
        is_failed = gen_status == "failed"
        row = {
            "id": m.get("id"),
            "product_id": m.get("product_id"),
            "slot": m.get("slot"),
            "product_name": info.get("product_name"),
            "gen_status": gen_status,
            "gen_error": m.get("gen_error"),
            "model": m.get("model"),
            "source": m.get("source"),
            "image_path": rel or None,
            "image_link": link,
            "prompt": str(m.get("prompt") or "")[:255] or None,
            "machine_check": m.get("machine_check"),
            "qc_result": m.get("qc_result"),
            "qc_reasons": "；".join(reasons)
            if isinstance(reasons, list) and reasons
            else None,
            "audit_status": None,
            "audit_operator": None,
            "audit_comment": None,
        }
        if is_failed:
            row["audit_status"] = "N/A(失败)"
        ws.append([row.get(col) for col in EXCEL_COLUMNS])
    for idx, col in enumerate(EXCEL_COLUMNS, 1):
        letter = ws.cell(row=1, column=idx).column_letter
        ws.column_dimensions[letter].width = (
            36
            if col in ("prompt", "image_link")
            else 40
            if col == "gen_error"
            else 24
            if col in ("product_name", "qc_reasons")
            else max(12, min(20, len(col) + 4))
        )
    wb.save(path)
    return path


def _dump_debug_json(payload: dict, ts: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{ts}_image_gen.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def generate_goods_image(
    product_ids: list | None = None,
    export_excel: bool = True,
) -> dict:
    """P2 图片生成主流程：素材 pass 商品 -> 双槽位生图 -> 四道关 -> 落库/Excel。

    :param product_ids:   可选二次圈定（与 DB 中素材 pass 集合取交集）；
                          None/空 = 全部 ``product_material.audit_status='pass'`` 商品。
    :param export_excel:  False 跳过 Excel/JSON 落盘（单测用）。
    :returns: ``{"success", "count", "generated", "failed", "skipped_success",
                "quota_exhausted", "excel_path", "json_path", "failures", "message"}``
    """
    _ensure_table()
    pids = _query_material_pass_ids(product_ids)
    if not pids:
        return {
            "success": False,
            "count": 0,
            "generated": 0,
            "failed": 0,
            "skipped_success": 0,
            "quota_exhausted": False,
            "excel_path": None,
            "json_path": None,
            "failures": [],
            "message": "无素材审核通过（product_material.audit_status='pass'）商品可生图",
        }

    info_rows = _query_info_rows(pids)
    material_rows = _query_material_rows(pids)
    _insert_pending_slots(pids)
    image_rows = _query_image_rows(pids)

    todo: list[tuple[int, str]] = []
    skipped = 0
    for pid in pids:
        for slot in SLOTS:
            status = str(image_rows.get((pid, slot), {}).get("gen_status") or "pending")
            if status == "success":
                skipped += 1
            else:
                todo.append((pid, slot))
    if skipped:
        print(f"[生图] 断点命中：跳过已 success {skipped} 槽位，待生成 {len(todo)} 个")

    generated = 0
    failed = 0
    failures: list[dict] = []
    all_attempts: list[dict] = []
    quota_exhausted = False
    aborted_remaining = False

    for seq, (pid, slot) in enumerate(todo, 1):
        if aborted_remaining:
            _mark_failed(pid, slot, "免费额度耗尽")
            failed += 1
            failures.append({"product_id": pid, "slot": slot, "error": "免费额度耗尽"})
            continue

        info_row = info_rows.get(pid)
        brief = _get_brief(material_rows.get(pid))
        print(f"[生图] ({seq}/{len(todo)}) product_id={pid} slot={slot} ...")
        _mark_generating(pid, slot)
        if not info_row:
            reason = "product_info 缺失"
            _mark_failed(pid, slot, reason)
            failed += 1
            failures.append({"product_id": pid, "slot": slot, "error": reason})
            print(f"[生图]   -> failed: {reason}")
            continue
        if not brief:
            reason = "product_material.content_json.brief 缺失（先跑 P1 文案生成）"
            _mark_failed(pid, slot, reason)
            failed += 1
            failures.append({"product_id": pid, "slot": slot, "error": reason})
            print(f"[生图]   -> failed: {reason}")
            continue

        try:
            ok, result, attempts = _generate_one_slot(pid, slot, info_row, brief)
        except QuotaExhaustedError as exc:
            quota_exhausted = True
            _mark_failed(pid, slot, "免费额度耗尽", qc_reasons=[str(exc)])
            failed += 1
            failures.append({"product_id": pid, "slot": slot, "error": "免费额度耗尽"})
            all_attempts.append(
                {
                    "product_id": pid,
                    "slot": slot,
                    "attempt": None,
                    "error": "免费额度耗尽",
                }
            )
            print(
                f"[生图]   -> 免费额度耗尽: {exc}；"
                f"剩余 {len(todo) - seq} 槽位全部停跑"
            )
            aborted_remaining = True
            continue

        all_attempts.extend(attempts)
        if ok:
            _mark_success(pid, slot, result)
            generated += 1
            print(
                f"[生图]   -> success model={result.get('model')} "
                f"source={result.get('source')}"
            )
        else:
            _mark_failed(pid, slot, str(result))
            failed += 1
            failures.append({"product_id": pid, "slot": slot, "error": str(result)})
            print(f"[生图]   -> failed: {result}")
        if seq < len(todo) and ITEM_DELAY_SECONDS > 0:
            time.sleep(ITEM_DELAY_SECONDS)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    refreshed = _query_image_rows(pids)
    # Excel 全量收录本批 success+failed（与 DB 对齐）；已人工审过的 success 跳过
    exportable = [
        row
        for (_pid, _slot), row in refreshed.items()
        if str(row.get("gen_status")) in ("success", "failed")
        and not (
            str(row.get("gen_status")) == "success" and row.get("audit_status")
        )
    ]
    excel_path: Path | None = None
    json_path: Path | None = None
    if export_excel and exportable:
        excel_path = _export_audit_excel(exportable, info_rows, ts)
        print(
            f"[生图] 审核 Excel: output/excel/{excel_path.name}"
            f"（{len(exportable)} 行，含成功+失败）"
        )
    if export_excel:
        json_path = _dump_debug_json(
            {
                "generated_at": ts,
                "qc_model": QC_MODEL,
                "prompt_version": PROMPT_VERSION,
                "image_model": IMAGE_MODEL,
                "target_count": len(pids),
                "todo_slots": len(todo),
                "generated": generated,
                "failed": failed,
                "skipped_success": skipped,
                "quota_exhausted": quota_exhausted,
                "failures": failures,
                "attempts": all_attempts,
            },
            ts,
        )
        print(f"[生图] 调试 JSON: output/{json_path.name}")

    success = generated > 0 or (not todo and skipped > 0)
    message = (
        f"图片生成完成：目标 {len(pids)} 个商品 / 本次生成 {generated} 张 / "
        f"断点跳过 {skipped} / 失败 {failed}"
        + ("；免费额度已耗尽" if quota_exhausted else "")
        + (f"；待审核 Excel: output/excel/{excel_path.name}" if excel_path else "")
    )
    print(f"[生图] {message}")
    return {
        "success": success,
        "count": len(pids),
        "generated": generated,
        "failed": failed,
        "skipped_success": skipped,
        "quota_exhausted": quota_exhausted,
        "excel_path": str(excel_path) if excel_path else None,
        "json_path": str(json_path) if json_path else None,
        "failures": failures,
        "message": message,
    }


def main() -> int:
    """CLI：``python src/material/image_generator.py --product-ids 1,2``。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    import argparse

    parser = argparse.ArgumentParser(
        description="P2 素材(图片)生成：素材pass商品 -> 千问生图 -> 人工审核Excel"
    )
    parser.add_argument(
        "--product-ids",
        default="",
        help="逗号分隔商品ID（可选，默认全部 product_material.audit_status='pass'）",
    )
    parser.add_argument(
        "--no-excel", action="store_true", help="只生成入库，不导出待审核 Excel/JSON"
    )
    args = parser.parse_args()
    pids = [int(x) for x in re.split(r"[,\s]+", args.product_ids) if x.strip()] or None
    result = generate_goods_image(product_ids=pids, export_excel=not args.no_excel)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
