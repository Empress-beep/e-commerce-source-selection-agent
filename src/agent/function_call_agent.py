"""手搓原生 Function-Call Agent（纯 Python，零 Agent 框架）。

不用 LangChain / Dify / LlamaIndex 等任何框架，直接维护 ``messages`` 上下文
列表，手写「模型 -> function_call 解析 -> 工具执行 -> 结果回填 -> 多轮调度」
循环；配合**交互式常驻会话**实现分段任务执行（采集选品 -> 人工审核 -> 导入回写）。

模型
----
* ``qwen3.7-flash``（百炼 DashScope ``MultiModalConversation`` 原生 Function Call）
* 业务空间 ID 读环境变量 ``DASHSCOPE_WORKSPACE_ID``（``workspace`` 请求头，留空不传）
* API-KEY 强制读环境变量 ``DASHSCOPE_API_KEY``（可写在 ``config/.env``，
  随 ``src.settings`` 导入自动注入），禁止硬编码

工具（全部外部导入/注册，本文件不重写任何业务内部逻辑）
--------------------------------------------------------
* ``run_crawl``            <- ``from src.collector.main import run_crawl``（采集+清洗+入库）
* ``query_product_list``   <- ``from src.selector.product_selector import query_product_list``（MySQL 初筛，run_id 批次隔离由执行器注入）
* ``ai_select_product``    <- ``from src.selector.ai_selector import ai_select_product``（分片 AI 选品）
* ``import_audit_excel``   <- ``from src.selector.excel_audit_import import import_audit_excel``
  （Agent 内工具封装：读取**人类已填写完毕**的审核 Excel -> 复用
  ``src/selector/manual_audit.py`` 状态机校验 -> 批量回写 MySQL -> 归档 ->
  返回统计；工具自身**不产生任何审核决策**）
* ``manual_audit_product``  —— 审核工具**仅人类调用**：只注册 schema + 拒绝空壳，
  大模型禁止自主调用做审核决策；Agent 只产出待审核产物（``*_pending_audit.xlsx``）
* ``generate_goods_mateial`` / ``simulate_goods_publish``
  —— 预留扩展工具：只注册 schema + 空壳，打印“待后续迭代实现”

交互式常驻会话（分段任务执行，解决任务割裂）
--------------------------------------------
1. 首轮自然语言指令：``run_crawl`` 多关键词采集 -> ``query_product_list``
   初筛 -> ``ai_select_product`` 分片选品，产出 ``*_pending_audit.xlsx``；
2. AI 选品完成后**不退出**：打印待审核提示，保留 ``messages`` 上下文与内存
   ``state``（crawl_runs / candidates / excel_path / json_path 跨轮不丢），
   等待用户继续输入；
3. 【完全在 Agent 外部、人类本地操作】业务人员打开 Excel 手工填写
   ``audit_status`` (pass/reject/hold)、``audit_operator``、``audit_comment``
   并保存（reject 必须写驳回原因）；
4. 用户输入“已审核完成，执行后续操作” -> 调用 ``import_audit_excel`` 导入
   回写（读取 / 状态校验 / 同步 MySQL / 归档，纯机械 IO）；
5. 查询 ``audit_status='pass'`` 商品 ID -> 调用 ``generate_goods_mateial``
   （预留空壳，商品 ID 由执行器注入）；
6. 继续回到等待用户输入状态；只有输入 ``exit`` 才结束 Agent 进程。

业务红线
--------
* 审核决策 pass/reject/hold **必须由人类**在 Excel 手工填写；
* 大模型**禁止生成、禁止自主决定**审核结果，禁止编造审核状态/审核人/备注；
* Agent 仅负责读取人类已保存的 Excel，执行解析、状态机校验、数据库同步、
  归档等机械 IO；``import_audit_excel`` 工具内部同样不生成任何审核结果。

异常处理
--------
* 网络/模型调用异常：重试后中止本轮并给出错误信息（会话不退出）；
* 模型返回解析异常：兼容 ``tool_calls`` 原生结构 + 正文 JSON 兜底解析；
* 工具执行异常：捕获后回填 ``{"error": ...}``，由模型决定下一步，不中断循环；
* **运行日志**：控制台 stdout/stderr 全量双写 ``logs/agent_<ts>.log``
  （调度行/工具结果/爬虫 INFO 进度/异常 traceback 均落盘，错误不留白）。

运行入口
--------
::

    python -m src.agent.function_call_agent

进入交互式控制台，支持多轮对话：第一轮执行采集选品；人工本地改完审核
Excel 后输入“已审核完成，执行后续操作”接续跑导入回写链路；输入 ``exit``
退出整个 Agent 程序。
"""

from __future__ import annotations

import contextlib
import glob
import io
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# sys.path 引导：把项目根（src/ 的上一级）加进搜索路径，
# 保证从任意工作目录启动（python -m / 直接脚本 / Agent 导入）都能 import src.*
# 本文件在 src/agent/ 下：parents[0]=src/agent, parents[1]=src, parents[2]=项目根
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dashscope import MultiModalConversation  # noqa: E402

# ---------------------------------------------------------------------------
# 项目已有模块：只导入调用，禁止重写内部逻辑
# ---------------------------------------------------------------------------
from src.collector.main import run_crawl  # noqa: E402
from src.selector.ai_selector import ai_select_product  # noqa: E402
from src.selector.excel_audit_import import import_audit_excel as _run_excel_import  # noqa: E402
from src.selector.product_selector import query_product_list  # noqa: E402
from src.settings import LOG_DIR, OUTPUT_DIR  # noqa: E402 路径常量统一由 settings 管理

# ---------------------------------------------------------------------------
# 模型与调度配置
# ---------------------------------------------------------------------------
MODEL = "qwen3.7-flash-2026-07-15"
# 新版百炼业务空间 ID：只从环境变量读取（config/.env），禁止硬编码；
# 留空 = 使用账号默认业务空间（不携带 workspace 参数）
WORKSPACE_ID = os.environ.get("DASHSCOPE_WORKSPACE_ID", "").strip()
MAX_STEPS = 16            # 最大调度轮次（每轮可含多个 tool_calls）
MODEL_RETRY = 3           # 模型网络/服务异常重试次数
KEYWORD_DELAY = 180.0     # 关键词间风控间隔（默认 3 分钟）

# ---------------------------------------------------------------------------
# Agent 运行日志：stdout/stderr 双写 -> logs/agent_<ts>.log
# （调度行、工具返回、被调模块 print、爬虫 INFO 进度、异常 traceback 全量落盘）
# ---------------------------------------------------------------------------
class _Tee:
    """控制台流的双写包装：原样显示的同时追加进 Agent 运行日志文件。"""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, s):
        try:
            self._stream.write(s)
        except Exception:  # noqa: BLE001 - 控制台写失败不能影响落盘
            pass
        try:
            self._fh.write(s)
            self._fh.flush()
        except Exception:  # noqa: BLE001 - 落盘失败不能影响控制台
            pass
        return len(s) if isinstance(s, str) else 0

    def flush(self):
        for target in (self._stream, self._fh):
            try:
                target.flush()
            except Exception:  # noqa: BLE001
                pass

    def __getattr__(self, name):
        # isatty / fileno / reconfigure 等属性委托给原控制台流
        return getattr(self._stream, name)


_AGENT_LOG: dict = {}


def _start_agent_log(instruction: str, log_path: Path | str | None = None) -> Path:
    """接管 stdout/stderr 开启 Agent 运行日志，返回日志文件路径（幂等）。"""
    if _AGENT_LOG:
        return _AGENT_LOG["path"]  # type: ignore[return-value]
    path = Path(log_path) if log_path else (
        LOG_DIR / f"agent_{time.strftime('%Y%m%d_%H%M%S')}.log"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("w", encoding="utf-8")
    saved = (sys.stdout, sys.stderr)
    sys.stdout = _Tee(saved[0], fh)
    sys.stderr = _Tee(saved[1], fh)
    _AGENT_LOG.update({"fh": fh, "saved": saved, "path": path})
    print("=" * 64)
    print(f"Function-Call Agent 运行日志 | 启动 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"模型={MODEL} | workspace={WORKSPACE_ID}")
    print(f"指令: {instruction}")
    print(f"日志文件: {path}")
    print("=" * 64)
    return path


def _stop_agent_log() -> Path | None:
    """恢复控制台、关闭日志文件，返回日志路径。"""
    if not _AGENT_LOG:
        return None
    path = _AGENT_LOG.get("path")
    print(f"[Agent] 运行日志已写入: {path}")
    saved = _AGENT_LOG.get("saved")
    if saved and len(saved) == 2:
        sys.stdout, sys.stderr = saved
    fh = _AGENT_LOG.get("fh")
    if fh:
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass
    _AGENT_LOG.clear()
    return path

SYSTEM_PROMPT = """\
你是「17qcc 电商选品运营 Agent」，通过 Function Call 调度工具完成用户的采买选品任务。
本 Agent 是**交互式常驻会话**：控制台会把用户的多轮输入依次发给你；执行器状态（state）
跨轮保留，同一任务链路不会重复采集、不会重复 AI 选品。

可用工具：
1. run_crawl(keyword, page, limit, cateid, resume)：执行商品爬虫采集、数据清洗并入库；返回 count=本次入库条数。
   cateid 可选，类目限制 id，默认不传=不限类目；仅当用户明确要求限定类目时才传
   （如男鞋 50011740）。
   resume 可选断点续爬：仅当用户要求继续/补跑此前中断的采集时，传上次采集的 run_id
   （可从此前工具返回/汇总中的 run_id 获取）；此时 keyword/cateid 必须与原运行一致，
   普通新采集不传。
2. query_product_list(cate_name, max_wholesale_price, min_buy_count, max_return_goods_rate, run_id)：
   MySQL 硬性规则初筛，业务条件全部可选；run_id 由执行器自动注入本轮采集批次
   （只查本轮 run_crawl 的数据，隔离历史脏数据），你固定传空字符串 "" 即可
   —— 用户未指定任何筛选条件时同样照传 ""；返回初筛通过条数与候选预览。
3. ai_select_product(candidate_list, select_condition)：分片 AI 智能选品并持久化。
   candidate_list 由执行器自动注入初筛结果，你固定传空数组 [] 即可；
   select_condition 传本次筛选条件字典（用于归档）。
4. manual_audit_product：**仅人类调用，你（大模型）禁止自主调用此工具做审核决策**；
   审核动作由业务人员在待审核Excel中手工填写（audit_status/audit_operator/audit_comment），
   再由 import_audit_excel 工具（或独立脚本 excel_audit_import.py）导入完成。
5. import_audit_excel(excel_file_path)：【机械IO工具，绝不产生任何审核决策】
   读取**人类已提前编辑并保存**的审核 Excel -> 复用 manual_audit_product 状态机校验 ->
   逐行回写 MySQL product_select_result -> 归档 json_dump/finished_audit -> 返回统计
   （成功更新/跳过未填写/失败条数 + 失败明细）。
   excel_file_path 由执行器自动注入 state 中保存的待审核 Excel 路径，你固定传空字符串 "" 即可。
   触发时机：仅当用户明确表示“已审核完成，执行后续操作”时调用。
6. generate_goods_mateial / simulate_goods_publish：预留工具（返回“待后续迭代实现”）。

审核业务红线（最高优先级）：
- pass/reject/hold 审核决策**只能由人类**在 Excel 中手工填写；
- 你（大模型）禁止生成、禁止输出、禁止自主决定任何审核状态/审核人/审核备注，
  禁止编造审核结论，禁止替用户填写 Excel，禁止在回复里给出逐行审核建议；
- import_audit_excel 工具本身也只读取人类已写好的内容做校验与同步，不会生成任何审核结果。

严格调度链路（不可跳步、不可乱序）：
- 第一步：从用户指令解析出 关键词（可能多个）、页码、采集数量上限、初筛条件；
- 第二步：每个关键词单独调用一次 run_crawl（禁止把多个关键词合并进一次调用）；
- 若某次 run_crawl 返回 count=0：立即停止一切后续工具调用，直接回复“采集条数为 0，任务终止”及原因；
- 第三步：全部关键词采集完成后，调用一次 query_product_list（业务条件参数严格
  取自用户指令；run_id 固定传空字符串 ""，由执行器注入本轮采集批次做数据隔离）；
- 若初筛 count=0：不调用 AI 选品，直接回复无合格货源；
- 第四步：调用一次 ai_select_product（candidate_list 传 []，select_condition 传初筛条件）；
- 第五步：用简短简体中文汇总结果，列出入库表名、AI选品结果表名、Excel 路径、JSON 路径（以工具返回为准）。

审核后置链路（用户输入“已审核完成，执行后续操作”时，严格按序执行）：
- 第一步：调用一次 import_audit_excel(excel_file_path="")，并如实转述返回统计
  （成功更新 / 跳过未填写 / 失败条数 + 失败明细）；
- 第二步：调用一次 generate_goods_mateial(product_ids=[])，
  商品 ID 列表由执行器自动注入审核通过（audit_status='pass'）的商品；
- 本阶段**禁止**调用 run_crawl / query_product_list / ai_select_product
  （state 跨轮保留，不会重复执行爬虫、不会重复 AI 选品）；
- 结束后用简短简体中文汇总导入统计。

硬性约束：
- 只能通过 function call 调用工具，禁止在正文里手写工具调用 JSON；
- 参数严格取自用户指令，不臆造、不编造任何数据；
- 工具返回 error/失败时如实转述，不得粉饰；
- 全程简体中文，回复保持简短。
"""

# ---------------------------------------------------------------------------
# 工具注册清单（JSON Schema，供百炼 Function Call）
# ---------------------------------------------------------------------------
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "run_crawl",
            "description": (
                "根据关键词、页码、采集数量上限，执行商品爬虫采集与数据清洗，"
                "写入原始备份表 api_backup 与业务表 product_info。一次调用只处理一个关键词。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词"},
                    "page": {"type": "integer", "description": "采集页码，用户未指定时默认 1"},
                    "limit": {"type": "integer", "description": "本关键词最大采集商品数量，用户未指定时默认 20"},
                    "cateid": {
                        "type": "string",
                        "description": "可选类目限制 id，不传或传 \"0\" = 不限类目；仅用户明确要求限定类目时传（如男鞋 50011740）",
                    },
                    "resume": {
                        "type": "string",
                        "description": "可选断点续爬标识：用户要求继续/补跑此前中断的采集时，传上次采集的 run_id（其余参数 keyword/cateid 必须与原运行一致）；普通新采集不传或传空字符串 \"\"",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_product_list",
            "description": (
                "对已采集入库的商品做硬性规则初筛（类目/拿货价上限/销量下限/退货率上限），"
                "剔除不合格货源，返回候选商品列表。参数全部可选，只传用户要求的条件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cate_name": {"type": "string", "description": "类目名称关键词（模糊匹配），用户未指定则不传"},
                    "max_wholesale_price": {"type": "number", "description": "拿货价上限，用户未指定则不传"},
                    "min_buy_count": {"type": "integer", "description": "销量下限，用户未指定则不传"},
                    "max_return_goods_rate": {"type": "number", "description": "退货率上限(0-1小数)，用户未指定则不传"},
                    "run_id": {
                        "type": "string",
                        "description": "采集批次标识，由执行器自动注入本轮 run_crawl 的 run_id（做批次隔离），调用时固定传空字符串 \"\"",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ai_select_product",
            "description": (
                "对初筛候选商品做多维度 AI 智能选品打分，自动分片批量调用模型防 token 溢出，"
                "结果持久化 MySQL + JSON 调试文件 + Excel 业务审核报表。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_list": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "候选商品列表，由执行器自动注入初筛结果，调用时固定传空数组 []",
                    },
                    "select_condition": {
                        "type": "object",
                        "description": "本次选品条件字典（归档用），如 {\"max_wholesale_price\":80,\"max_return_goods_rate\":0.18}",
                    },
                },
                "required": ["candidate_list", "select_condition"],
            },
        },
    },
    # ---- 预留扩展工具：只注册 schema，业务空壳不实现 ----
    {
        "type": "function",
        "function": {
            "name": "manual_audit_product",
            "description": (
                "【仅人类调用，Agent大模型禁止自主调用此工具做审核决策；"
                "推荐业务人员使用Excel填写后由 import_audit_excel 工具（或 excel_audit_import.py 脚本）"
                "批量完成审核】人工审核选品结果：更新审核状态、审核人、审核备注、审核时间。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_ids": {"type": "array", "items": {"type": "integer"}, "description": "待审核商品ID列表"},
                    "audit_status": {"type": "string", "description": "审核状态，如 通过/驳回/待审"},
                    "auditor": {"type": "string", "description": "审核人"},
                    "audit_remark": {"type": "string", "description": "审核备注"},
                },
                "required": ["product_ids", "audit_status", "auditor"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_audit_excel",
            "description": (
                "【本工具不会产生任何审核决策】导入**人类已提前编辑并保存**的审核Excel"
                "（audit_status/audit_operator/audit_comment 必须由人手工填写完毕）："
                "逐行读取 -> 复用 manual_audit_product 状态机校验 -> 逐行回写 MySQL "
                "product_select_result -> 归档 json_dump/*_finished_audit.json 与 "
                "excel/*_finished_audit.xlsx -> 返回统计（成功更新/跳过未填写/失败条数+失败明细）。"
                "仅当用户说“已审核完成，执行后续操作”时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "excel_file_path": {
                        "type": "string",
                        "description": (
                            "人工填写完毕的Excel完整文件路径；"
                            "由执行器自动注入 state['excel_path']，调用时固定传空字符串 \"\""
                        ),
                    },
                },
                "required": ["excel_file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_goods_mateial",
            "description": (
                "【预留】对审核通过商品自动生成上架标题、卖点、详情、图片 prompt 素材；"
                "product_ids 由执行器自动注入审核通过（audit_status='pass'）商品，调用时传 [] 即可。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_ids": {"type": "array", "items": {"type": "integer"}, "description": "待生成素材的商品ID列表"},
                },
                "required": ["product_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "simulate_goods_publish",
            "description": "【预留】素材审核完成后模拟商品上架流程。",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_ids": {"type": "array", "items": {"type": "integer"}, "description": "待上架的商品ID列表"},
                },
                "required": ["product_ids"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 预留工具空壳（只打印提示，不实现业务）
# ---------------------------------------------------------------------------
def manual_audit_product(**kwargs) -> dict:  # noqa: ARG001
    """【仅人类调用】审核决策禁止由大模型自主执行。

    Agent 只产出待审核产物（*_pending_audit.xlsx）；审核动作交给人
    （本地 Excel 手工填写）+ ``import_audit_excel`` 工具/独立脚本导入
    （校验统一走 ``src/selector/manual_audit.py``）。
    """
    print("[仅人类调用] Agent 禁止自主执行审核，请业务人员填写Excel后由 import_audit_excel 导入")
    return {
        "success": False,
        "message": (
            "【仅人类调用】Agent大模型禁止自主调用此工具做审核决策；"
            "推荐业务人员使用Excel填写后由 import_audit_excel 工具批量完成审核"
        ),
    }


def generate_goods_mateial(**kwargs) -> dict:  # noqa: ARG001
    """【预留】对审核通过商品自动生成上架标题、卖点、详情、图片 prompt 素材。"""
    print("[预留工具] generate_goods_mateial 待后续迭代实现")
    return {"success": False, "message": "待后续迭代实现"}


def simulate_goods_publish(**kwargs) -> dict:  # noqa: ARG001
    """【预留】素材审核完成后模拟商品上架流程。"""
    print("[预留工具] simulate_goods_publish 待后续迭代实现")
    return {"success": False, "message": "待后续迭代实现"}


# ---------------------------------------------------------------------------
# import_audit_excel：Excel 审核导入工具（机械 IO，禁止产生审核决策）
# ---------------------------------------------------------------------------
def _replay(text: str) -> None:
    """把被截获的输出回放到当前控制台流（tee 双写控制台+运行日志）。"""
    if not text:
        return
    try:
        sys.stdout.write(text)
    except Exception:  # noqa: BLE001
        pass


def _parse_audit_import_stats(text: str) -> dict:
    """从 import_audit_excel 的标准输出解析统计：成功/跳过/失败 + 失败明细。"""
    def _num(pattern: str) -> int:
        m = re.search(pattern, text)
        return int(m.group(1)) if m else 0

    failures = [
        {"row": int(row), "id": id_text, "error": err}
        for row, id_text, err in re.findall(
            r"^[ \t]+第(\d+)行 id=(\S+?): (.+)$", text, re.M
        )
    ]
    m_err = re.search(r"^\[ERROR\] (.+)$", text, re.M)
    return {
        "success": _num(r"\[汇总\] 成功更新 (\d+) 条"),
        "skipped": _num(r"\[汇总\] 跳过 (\d+) 条未填写审核状态"),
        "failed": _num(r"\[汇总\] 失败 (\d+) 条"),
        "failures": failures,
        "error": m_err.group(1).strip() if m_err else "",
    }


def import_audit_excel(excel_file_path: str = "", state: dict | None = None) -> dict:
    """导入**人工填写完毕**的审核 Excel 并同步数据库（机械 IO）。

    完整复用 ``src.selector.excel_audit_import.import_audit_excel``：逐行读取 ->
    ``manual_audit_product`` 状态机校验（流转规则/reject必填备注/终态禁止改）->
    单行 UPDATE 回写 -> 归档 ``output/json_dump/*_finished_audit.json`` 与
    ``output/excel/*_finished_audit.xlsx`` -> 返回统计。

    红线：本工具**不会产生任何审核决策**——审核状态只读取人类已保存在 Excel
    中的内容；模型禁止生成、禁止自主决定 pass/reject/hold。
    """
    if state is None:
        state = {}
    path = str(excel_file_path or "").strip() or str(state.get("excel_path") or "")
    if not path:
        return {
            "success": False,
            "error": "缺少 excel_file_path（state['excel_path'] 为空，可能尚未执行 AI 选品）",
        }
    if not Path(path).exists():
        return {"success": False, "error": f"Excel 文件不存在: {path}"}

    print(f"[Agent] 调用 import_audit_excel path={path}"
          "（只读取人类已填写内容做校验与同步，工具不产生任何审核决策）")
    buf = io.StringIO()
    rc = 0
    try:
        with contextlib.redirect_stdout(buf):
            rc = int(_run_excel_import(path) or 0)
    except SystemExit as exc:  # excel_audit_import 缺列时 sys.exit(2)
        rc = exc.code if isinstance(exc.code, int) else 2
    except Exception as exc:  # noqa: BLE001 - 导入整体异常 -> 回填 error
        _replay(buf.getvalue())
        return {"success": False, "error": f"导入执行异常: {exc!r}"}
    out = buf.getvalue()
    _replay(out)  # 回放逐行日志/归档路径/统计汇总（控制台 + 运行日志）

    stats = _parse_audit_import_stats(out)
    if rc != 0:
        return {
            "success": False,
            "excel_path": path,
            "error": stats.get("error") or f"导入失败（退出码 {rc}）",
            "success_updated": stats["success"],
            "skipped_not_filled": stats["skipped"],
            "failed": stats["failed"],
            "failures": stats["failures"],
        }

    # ---- 审核通过商品 ID（机械查询，仅为下游工具注入，不做任何决策） ----
    passed_ids: list = []
    try:
        from src.common import database  # 延迟导入：仅本工具回查用
        rows = database.fetch_all(
            "product_select_result",
            where="audit_status=%s ORDER BY id",
            params=["pass"],
            cols="product_id",
        )
        passed_ids = [r.get("product_id") for r in rows if r.get("product_id")]
    except Exception as exc:  # noqa: BLE001
        print(f"[Agent][WARN] 查询审核通过商品ID失败: {exc!r}")

    state["passed_product_ids"] = passed_ids
    state["audit_pending"] = False
    return {
        "success": True,
        "excel_path": path,
        "success_updated": stats["success"],
        "skipped_not_filled": stats["skipped"],
        "failed": stats["failed"],
        "failures": stats["failures"],
        "passed_count": len(passed_ids),
        "passed_product_ids": passed_ids,
        "message": (
            f"导入完成：成功更新 {stats['success']} 条 / "
            f"跳过 {stats['skipped']} 条未填写审核状态 / 失败 {stats['failed']} 条；"
            f"审核通过（audit_status='pass'）商品 {len(passed_ids)} 个"
        ),
    }


# ---------------------------------------------------------------------------
# 小工具函数
# ---------------------------------------------------------------------------
def _get(obj, key, default=None):
    """兼容 dict / DashScope 对象的属性读取。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _api_key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("缺少系统环境变量 DASHSCOPE_API_KEY（禁止硬编码 API-KEY）")
    return key


def _content_text(content) -> str:
    """把模型 content（字符串 / [{"text":...}] / None）拍平成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                t = part.get("text")
                if t:
                    parts.append(str(t))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return str(content)


def _as_number(value):
    """工具参数宽松转换：数字字符串 -> float/None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value, default=None):
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _latest_output(pattern: str) -> str | None:
    """取 output/ 目录下最新一个匹配文件（ai_selector 落盘的报表/调试文件）。"""
    files = glob.glob(str(OUTPUT_DIR / pattern))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


# ---------------------------------------------------------------------------
# 模型调用（网络异常重试）与返回解析（含正文 JSON 兜底）
# ---------------------------------------------------------------------------
def _call_model(messages: list[dict]):
    last_exc = None
    kwargs: dict = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "api_key": _api_key(),
        "enable_thinking": False,
    }
    if WORKSPACE_ID:  # 配置了业务空间才携带 workspace 参数
        kwargs["workspace"] = WORKSPACE_ID
    for attempt in range(1, MODEL_RETRY + 1):
        try:
            return MultiModalConversation.call(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 网络/超时/服务端异常统一兜底
            last_exc = exc
            print(f"[Agent][WARN] 模型调用异常 第{attempt}/{MODEL_RETRY}次: {exc!r}")
            if attempt < MODEL_RETRY:
                time.sleep(3 * attempt)
    raise RuntimeError(f"模型调用失败（已重试 {MODEL_RETRY} 次）: {last_exc!r}")


def _extract_message(output) -> tuple[list[dict], list, list | str | None, str]:
    """解析模型返回 -> (规范化 tool_calls, 原始 tool_calls, 原始 content, 正文文本)。

    tool_calls 规范化为 ``[{"id","name","arguments"}]``；解析失败抛 ValueError。
    """
    choices = _get(output, "choices") or []
    if not choices:
        raise ValueError(f"模型返回缺少 choices: {str(output)[:300]}")
    msg = _get(choices[0], "message") or {}
    raw_tcs = _get(msg, "tool_calls") or []
    norm = []
    for idx, tc in enumerate(raw_tcs):
        fn = _get(tc, "function") or {}
        name = _get(fn, "name") or _get(tc, "name")
        args = _get(fn, "arguments") or _get(tc, "arguments") or "{}"
        norm.append({
            "id": _get(tc, "id") or f"call_noid_{idx}",
            "name": name,
            "arguments": args,
        })
    content = _get(msg, "content")
    text = _content_text(content)
    return norm, raw_tcs, content, text


def _parse_args(arguments) -> dict:
    """function_call arguments（JSON 字符串/字典）-> dict；失败抛 ValueError。"""
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    s = str(arguments).strip()
    # 剥代码围栏
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.S)
    if m:
        s = m.group(1)
    try:
        data = json.loads(s)
    except json.JSONDecodeError as exc:
        raise ValueError(f"工具参数 JSON 解析失败: {exc}; 原文={s[:200]}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"工具参数必须是对象，实际: {type(data).__name__}")
    return data


def _parse_text_tool_calls(text: str) -> list[dict]:
    """兜底：模型把工具调用写进正文时，从文本中提取工具调用 JSON。"""
    if not text or "{" not in text:
        return []
    found: list[dict] = []
    dec = json.JSONDecoder()
    idx = 0
    while True:
        i = text.find("{", idx)
        if i < 0:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            idx = i + 1
            continue
        idx = end
        if isinstance(obj, dict):
            name = obj.get("name") or obj.get("tool")
            args = obj.get("arguments") if "arguments" in obj else obj.get("parameters")
            if name and isinstance(args, dict) and str(name).isidentifier():
                found.append({
                    "id": f"call_text_{len(found)}",
                    "name": str(name),
                    "arguments": json.dumps(args, ensure_ascii=False),
                })
    return found


# ---------------------------------------------------------------------------
# 工具执行（业务全部走外部导入；此处只做编排/状态/风控间隔）
# ---------------------------------------------------------------------------
def _execute_tool(name: str, args: dict, state: dict) -> dict:
    if name == "run_crawl":
        keyword = str(args.get("keyword") or "").strip()
        if not keyword:
            return {"success": False, "error": "缺少必需参数 keyword"}
        # 关键词间风控间隔（默认 180s=3 分钟）
        if state["last_crawl_ts"]:
            wait = KEYWORD_DELAY - (time.time() - state["last_crawl_ts"])
            if wait > 0:
                print(f"[Agent] 关键词间隔等待 {wait:.0f}s（风控默认 180s）...")
                time.sleep(wait)
        page = _as_int(args.get("page"), 1)
        limit = _as_int(args.get("limit"), 20)
        cateid = str(args.get("cateid") or "0").strip() or "0"
        resume = str(args.get("resume") or "").strip()
        print(
            f"[Agent] 调用 run_crawl keyword={keyword} page={page} "
            f"limit={limit} cateid={cateid}"
            f"{' resume=' + resume if resume else ''} ..."
        )
        result = run_crawl(
            keyword, page=page, limit=limit, cateid=cateid, resume=resume
        )
        state["last_crawl_ts"] = time.time()
        state["crawl_runs"].append(result)
        # 批次 run_id 保存到 state：query_product_list 注入做数据隔离
        rid = str(result.get("run_id") or "").strip()
        if rid:
            state["run_id"] = rid
            if rid not in state["run_ids"]:
                state["run_ids"].append(rid)
        if int(result.get("count") or 0) <= 0:
            state["terminated"] = True
            state["terminate_reason"] = (
                f"采集条数为 0，任务终止（{result.get('message')}）"
            )
        return result

    if name == "query_product_list":
        # 批次隔离：执行器注入 state 保存的 run_id（模型固定传空），
        # 即使用户未指定任何筛选条件也带上，只查本轮采集、隔离历史脏数据
        injected_run_id = (
            ",".join(state.get("run_ids") or [])
            or str(state.get("run_id") or "")
        )
        res = query_product_list(
            cate_name=args.get("cate_name"),
            max_wholesale_price=_as_number(args.get("max_wholesale_price")),
            min_buy_count=_as_int(args.get("min_buy_count")),
            max_return_goods_rate=_as_number(args.get("max_return_goods_rate")),
            run_id=injected_run_id or None,
        )
        # 同一商品可能被多个关键词分别采到 -> 按 product_id 去重（编排层职责）
        seen: set = set()
        uniq: list[dict] = []
        for row in res:
            pid = row.get("product_id")
            if pid in seen:
                continue
            seen.add(pid)
            uniq.append(row)
        state["candidates"] = uniq
        state["select_condition"] = {
            k: v for k, v in args.items() if v is not None and v != ""
        }
        print(
            f"[Agent] query_product_list 初筛通过 {len(uniq)} 条"
            f"（原始 {len(res)} 条，run_id={injected_run_id or '未过滤'}）"
        )
        if not uniq:
            return {"success": True, "count": 0, "message": "初筛无合格货源"}
        preview_keys = (
            "product_id", "product_name", "wholesale_price",
            "buy_count", "return_goods_rate",
        )
        preview = [{k: r.get(k) for k in preview_keys} for r in uniq[:5]]
        return {
            "success": True,
            "count": len(uniq),
            "message": f"初筛通过 {len(uniq)} 条（已按 product_id 去重，候选列表由执行器持有）",
            "candidates_preview": preview,
            "all_product_ids": [r.get("product_id") for r in uniq],
        }

    if name == "ai_select_product":
        candidates = state["candidates"]
        if not candidates:
            return {"success": False, "error": "无候选商品，需先成功执行 query_product_list"}
        condition = args.get("select_condition") or state.get("select_condition") or {}
        print(f"[Agent] 调用 ai_select_product 候选 {len(candidates)} 条 condition={condition}")
        # 大候选列表由执行器注入（模型无法回传），内部自动分片 + 双通道持久化
        results = ai_select_product(candidates, condition)
        state["ai_results"] = results
        state["json_path"] = _latest_output("*_ai_select.json")
        # 待审核 Excel 落在 output/excel/ 子目录（首列id + 末尾三列空审核列）
        state["excel_path"] = _latest_output("excel/*_pending_audit.xlsx")
        state["audit_pending"] = True  # 标记：等待人类填写 Excel 后导入
        levels: dict = {}
        for r in results:
            lv = r.get("recommend_level") or "未知"
            levels[lv] = levels.get(lv, 0) + 1
        print(f"[Agent] ai_select_product 完成 {len(results)} 条 分布={levels}")
        return {
            "success": True,
            "count": len(results),
            "recommend_summary": levels,
            "excel_path": state["excel_path"],
            "json_path": state["json_path"],
            "message": f"AI 选品完成 {len(results)} 条，结果已写 MySQL/JSON/Excel",
        }

    if name == "import_audit_excel":
        # excel_file_path 模型可传 ""，执行器注入 state 中保存的待审核 Excel 路径
        return import_audit_excel(
            excel_file_path=args.get("excel_file_path"),
            state=state,
        )

    # ---- 预留工具空壳 ----
    if name == "manual_audit_product":
        return manual_audit_product(**args)
    if name == "generate_goods_mateial":
        # 审核通过的商品ID列表由执行器注入（模型固定传空数组）
        if not args.get("product_ids"):
            args = dict(args, product_ids=state.get("passed_product_ids") or [])
        return generate_goods_mateial(**args)
    if name == "simulate_goods_publish":
        return simulate_goods_publish(**args)

    return {"success": False, "error": f"未知工具: {name}"}


def _exec_safe(name: str, args: dict, state: dict) -> dict:
    """工具执行异常统一捕获 -> 回填 error（不中断调度循环）。"""
    try:
        return _execute_tool(name, args, state)
    except Exception as exc:  # noqa: BLE001
        print(f"[Agent][ERROR] 工具 {name} 执行异常: {exc!r}")
        return {"success": False, "error": f"{name} 执行异常: {exc!r}"}


# ---------------------------------------------------------------------------
# 跨轮共享状态（会话常驻：每进程创建一次，多轮任务原样传入保留）
# ---------------------------------------------------------------------------
def _new_state() -> dict:
    """执行器状态对象：crawl_runs/candidates/excel_path/json_path 等跨轮保留。"""
    return {
        "candidates": [],          # 初筛候选（执行器持有，不经模型回传）
        "select_condition": {},    # 初筛条件（归档给 ai_select_product）
        "crawl_runs": [],          # 每次 run_crawl 的返回
        "run_id": None,            # 最近一次采集批次 run_id（query 兜底注入源）
        "run_ids": [],             # 本轮任务全部批次 run_id（逗号展开，每轮复位）
        "last_crawl_ts": 0.0,      # 上次采集完成时间（关键词间 180s 风控）
        "ai_results": None,
        "excel_path": None,        # 待审核 Excel（import_audit_excel 的注入源）
        "json_path": None,
        "passed_product_ids": [],  # 审核通过商品ID（generate_goods_mateial 注入源）
        "audit_pending": False,    # True=AI选品完成待人类填Excel；导入后置 False
        "terminated": False,       # count=0 提前终止开关（每轮任务复位）
        "terminate_reason": "",
    }


# ---------------------------------------------------------------------------
# Agent 主循环（单轮任务；messages/state 由会话持有跨轮保留）
# ---------------------------------------------------------------------------
def run_agent(instruction: str, messages: list[dict], state: dict) -> dict:
    """执行一条自然语言指令，返回 ``{"final_text", "state"}``。

    * ``messages`` 由调用方（交互式会话）持有：首轮自动补 system 提示，
      历史上下文跨轮保留，模型能看到上一轮任务的工具结果与汇总；
    * ``state`` 同样跨轮保留：不会重复执行爬虫、不会重复 AI 选品；
    * 本函数只结束**本轮任务调度**，不退出进程。
    """
    if not messages:
        messages.append({"role": "system", "content": [{"text": SYSTEM_PROMPT}]})
    messages.append({"role": "user", "content": [{"text": instruction}]})
    # 本轮调度开关复位（业务数据 crawl_runs/candidates/excel_path 等原样保留）
    state["terminated"] = False
    state["terminate_reason"] = ""
    # 采集批次每轮任务重新累计（本轮未采集时 query 兜底注入最近一次 run_id）
    state["run_ids"] = []
    final_text = ""

    for step in range(1, MAX_STEPS + 1):
        print(f"\n[Agent] ---- 轮次 {step}/{MAX_STEPS}: 请求模型 {MODEL} ----")
        try:
            resp = _call_model(messages)
        except Exception as exc:  # noqa: BLE001 - 网络/模型异常 -> 本轮中止
            final_text = f"任务中止：{exc}"
            print(f"[Agent][ERROR] {final_text}")
            messages.append({"role": "assistant", "content": [{"text": final_text}]})
            return {"final_text": final_text, "state": state}

        try:
            tool_calls, raw_tcs, raw_content, text = _extract_message(_get(resp, "output"))
        except Exception as exc:  # noqa: BLE001 - 解析异常
            final_text = f"任务中止：模型返回解析失败（{exc}）"
            print(f"[Agent][ERROR] {final_text}")
            messages.append({"role": "assistant", "content": [{"text": final_text}]})
            return {"final_text": final_text, "state": state}

        if text:
            print(f"[Agent] 模型正文: {text[:400]}")

        if not tool_calls:
            # 兜底：正文里混入的工具调用 JSON（原生 tool_calls 缺失时）
            tool_calls = _parse_text_tool_calls(text)
            if tool_calls:
                print(f"[Agent] 正文兜底解析到 {len(tool_calls)} 个工具调用")
                messages.append({"role": "assistant", "content": [{"text": text}]})
            else:
                final_text = text or "(模型未返回内容)"
                messages.append({"role": "assistant", "content": [{"text": final_text}]})
                break
        else:
            messages.append({
                "role": "assistant",
                "content": raw_content or [],
                "tool_calls": raw_tcs,
            })

        for tc in tool_calls:
            if state["terminated"]:
                # 已终止：仍回填占位结果保持消息对齐，不再执行业务
                result = {"skipped": state["terminate_reason"]}
            else:
                parse_err = None
                try:
                    args = _parse_args(tc["arguments"])
                except ValueError as exc:
                    parse_err = str(exc)
                    args = {}
                print(f"[Agent] -> 工具调用 {tc['name']} args={json.dumps(args, ensure_ascii=False)[:200]}")
                if parse_err:
                    result = {"success": False, "error": parse_err}
                else:
                    result = _exec_safe(tc["name"], args, state)
            print(f"[Agent] <- {tc['name']} 结果: {json.dumps(result, ensure_ascii=False, default=str)[:300]}")
            content = json.dumps(result, ensure_ascii=False, default=str)
            if str(tc["id"]).startswith("call_text_"):
                # 正文兜底路径：多模态接口要求工具结果以 user 消息回填
                messages.append({
                    "role": "user",
                    "content": [{"text": f"工具 {tc['name']} 执行结果：{content}\n请继续按调度链路处理。"}],
                })
            else:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": content,
                })

        if state["terminated"]:
            final_text = state["terminate_reason"]
            print(f"[Agent][终止] {final_text}")
            messages.append({"role": "assistant", "content": [{"text": final_text}]})
            break
    else:
        final_text = f"已达最大调度轮次 {MAX_STEPS}，任务未收敛。"
        print(f"[Agent][WARN] {final_text}")
        messages.append({"role": "assistant", "content": [{"text": final_text}]})

    _print_summary(state, final_text)
    return {"final_text": final_text, "state": state}


def _print_summary(state: dict, final_text: str) -> None:
    print("\n" + "=" * 64)
    print("[任务汇总]")
    print(f"  模型回复: {final_text}")
    print(f"  成功入库数据表: spider_db.product_info"
          f"（原始备份 spider_db.api_backup，共 {len(state['crawl_runs'])} 次采集）")
    for cr in state["crawl_runs"]:
        print(f"    - {cr.get('keyword')} run_id={cr.get('run_id')}: "
              f"api_backup {cr.get('backup_count')} 行 / product_info {cr.get('count')} 行")
    if state["ai_results"] is not None:
        print(f"  AI选品结果表: spider_db.product_select_result（{len(state['ai_results'])} 条）")
        print(f"  Excel 人工审核报表: {state['excel_path'] or '未生成'}")
        print(f"  JSON 调试文件: {state['json_path'] or '未生成'}")
    elif state["terminated"]:
        print(f"  任务提前终止: {state['terminate_reason']}")
    else:
        print("  AI 选品: 未执行")
    print("=" * 64)


# ---------------------------------------------------------------------------
# 交互式入口：常驻会话多轮对话（exit 退出；state/messages 跨轮保留）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _start_agent_log("交互式常驻会话（多轮任务调度；输入 exit 退出）")
    t0 = time.time()
    messages: list[dict] = []   # 跨轮保留的对话上下文（首轮自动补 system）
    state: dict = _new_state()  # 跨轮保留的执行器状态
    exit_text = ""
    try:
        while True:
            try:
                user_input = input("\n你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[Agent] 输入流结束，准备退出。")
                break
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit"}:
                exit_text = "exit"
                break

            out = run_agent(user_input, messages, state)
            state = out["state"]

            if state.get("audit_pending") and state.get("excel_path"):
                # AI 选品完成 -> 打印人工审核提示，保持会话等待下一轮输入
                print("\n" + "-" * 56)
                print("AI 选品任务执行完成")
                print(f"待审核 Excel 路径：{state['excel_path']}")
                print("请你本地打开该 Excel，手工填写审核信息："
                      "audit_status (pass/reject/hold)、audit_operator 审核人、"
                      "audit_comment 审核备注；")
                print("reject 驳回必须填写驳回原因，填写完成保存文件。")
                print("当你填写保存完毕，请对我说：已审核完成，执行后续操作；"
                      "输入 exit 可退出整个 Agent 程序")
                print("-" * 56)
            else:
                print("\n[Agent] 本轮任务完成，等待你的下一步指令；"
                      "输入 exit 可退出整个 Agent 程序。")

        print(f"\n[Agent] 会话结束"
              + (f"（{exit_text}）" if exit_text else "")
              + f"，总耗时 {time.time() - t0:.1f}s，累计消息 {len(messages)} 条")
    except BaseException as exc:  # 未捕获异常 -> traceback 经 stderr 双写进日志
        print(f"[Agent][ERROR] 未捕获异常: {exc!r}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        _stop_agent_log()
