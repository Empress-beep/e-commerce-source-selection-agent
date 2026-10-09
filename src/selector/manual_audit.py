"""人工审核状态机：``manual_audit_product`` —— 审核写入的**唯一**校验入口。

设计约束
--------
1. **单一校验实现**：状态机流转、``reject`` 必填驳回原因、终态
   ``pass``/``reject`` 禁止重复修改 —— 全部只写在这里；
   ``excel_audit_import`` 等导入脚本**必须直接复用**本函数，
   禁止另写一套校验（避免两套逻辑不一致）；
2. **只动审核 4 字段**：``audit_status`` / ``audit_operator`` /
   ``audit_comment`` / ``audit_time``，按主键 ``id`` 单行 UPDATE，
   **绝不触碰** AI 原始评估字段（composite_score / recommend_level /
   risk_list_json / reason / select_condition_json 等）；
3. **连接统一**：只用 :mod:`src.common.database`（读当前行
   ``database.fetch_one``、更新 ``database.connection()``）；校验在
   连接层之外抛出 :class:`ValueError`，报错信息原样透出给导入脚本。

状态机
------
======================  ============================================
当前 ``audit_status``    允许流转到
======================  ============================================
``pending``             ``pass`` / ``reject`` / ``hold``
``hold``                ``pass`` / ``reject`` / ``hold``（复审/改备注）
``pass`` / ``reject``   **终态，任何再次修改一律拒绝**
======================  ============================================

校验规则
--------
* ``audit_status`` 仅允许 ``pass`` / ``reject`` / ``hold``
  （大小写不敏感，写库归一为小写）；
* ``reject`` 的 ``audit_comment`` 必须非空（驳回必须给出原因）；
* 终态 ``pass``/``reject`` 禁止重复修改（报错，不更新）；
* ``id`` 不存在 -> 报错。

失败一律抛 :class:`ValueError`（校验）或 :class:`DatabaseError`（DB），
调用方自行捕获逐行处理。
"""
from __future__ import annotations

from typing import Any

from src.common import database

# 允许写入的审核状态（非终态候选集合）
ALLOWED_AUDIT_STATUS = ("pass", "reject", "hold")
# 终态：一旦写入禁止任何再次修改
TERMINAL_AUDIT_STATUS = ("pass", "reject")
# 状态机：当前状态 -> 允许流转到的下一批状态（空元组 = 终态）
STATUS_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("pass", "reject", "hold"),
    "hold": ("pass", "reject", "hold"),
    "pass": (),
    "reject": (),
}

TABLE = "product_select_result"

# 唯一允许 UPDATE 的 4 个审核字段（绝对不能扩到 AI 原始字段）
_UPDATE_SQL = (
    "UPDATE `product_select_result` "
    "SET `audit_status`=%s, `audit_operator`=%s, `audit_comment`=%s, `audit_time`=NOW() "
    "WHERE `id`=%s"
)


def manual_audit_product(
    select_result_id: Any,
    audit_status: Any,
    audit_operator: Any = None,
    audit_comment: Any = None,
) -> dict:
    """对 ``product_select_result`` 单行执行人工审核写入（状态机校验 + 单行更新）。

    :param select_result_id: ``product_select_result.id`` 数据库主键
    :param audit_status:     目标审核状态，仅 ``pass`` / ``reject`` / ``hold``
    :param audit_operator:   审核人（可空）
    :param audit_comment:    审核备注；``reject`` 时**必填**
    :returns: ``{"success": True, "id", "old_status", "audit_status", ...}``
    :raises ValueError: 校验失败（非法状态/终态重复修改/reject缺备注/id不存在等）
    :raises DatabaseError: 数据库连接或执行失败
    """
    # ---- 主键校验 ----
    try:
        row_id = int(select_result_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"非法的 select_result_id: {select_result_id!r}") from exc

    # ---- 目标状态归一化与合法性 ----
    new_status = str(audit_status or "").strip().lower()
    if not new_status:
        raise ValueError("audit_status 不能为空（仅允许 pass / reject / hold）")
    if new_status not in ALLOWED_AUDIT_STATUS:
        raise ValueError(
            f"非法 audit_status={audit_status!r}（仅允许 {'/'.join(ALLOWED_AUDIT_STATUS)}）"
        )

    operator = str(audit_operator or "").strip()
    comment = str(audit_comment or "").strip()
    if new_status == "reject" and not comment:
        raise ValueError("reject 状态必须填写 audit_comment 驳回原因")

    # ---- 读当前行 + 状态机校验（在校验层抛 ValueError，报错信息不被连接层包装） ----
    current = database.fetch_one(TABLE, where="id=%s", params=[row_id])
    if not current:
        raise ValueError(f"product_select_result 中不存在 id={row_id}")
    old_status = str(current.get("audit_status") or "pending").strip().lower() or "pending"
    if old_status in TERMINAL_AUDIT_STATUS:
        raise ValueError(f"id={row_id} 已处于终态 {old_status}，禁止重复修改")
    allowed_next = STATUS_TRANSITIONS.get(old_status)
    if allowed_next is None:
        raise ValueError(f"未知的当前 audit_status={old_status!r}（id={row_id}）")
    if new_status not in allowed_next:
        raise ValueError(f"状态机不允许 {old_status} -> {new_status}（id={row_id}）")

    # ---- 单行 UPDATE：只写 4 个审核字段 ----
    with database.connection() as conn:
        with conn.cursor() as cur:
            affected = cur.execute(
                _UPDATE_SQL, (new_status, operator or None, comment or None, row_id)
            )
    if affected != 1:
        raise ValueError(f"id={row_id} 更新影响行数异常: {affected}（可能已被并发修改）")

    print(
        f"[人工审核] id={row_id} {old_status} -> {new_status}"
        f" 审核人={operator or '-'} 备注={comment or '-'}"
    )
    return {
        "success": True,
        "id": row_id,
        "old_status": old_status,
        "audit_status": new_status,
        "audit_operator": operator,
        "audit_comment": comment,
    }
