"""登录态凭证：全部从环境变量读取（``config/.env``），**禁止硬编码**。

导出步骤（改密码 / 退出登录 / 身份 Cookie 过期后需重新导出）
------------------------------------------------------------
1. 打开登录页完成登录；
2. 打开列表页（``list.html`` 任一搜索页）；
3. 控制台执行 ``document.cookie`` 取整串 Cookie，并 decodeURIComponent
   其中的 ``token``；httpOnly 的 ``.AspNetCore.Identity.Application``
   从登录请求 ``POST /exteral/userlogin`` 的响应 ``Set-Cookie`` 头获取；
4. 写入 ``config/.env``（参考 ``config/.env.example``），键名：
   ``QCC_LOGIN_USER`` / ``QCC_AUTH_TOKEN`` / ``QCC_LOGIN_COOKIE``
   （``QCC_USER_AGENT`` 可选，不填用下方默认 UA）。

注意
----
* ``.AspNetCore.Identity.Application`` 是详情页 SSR 渲染当前商品
  价格/SKU/库存的前提；缺失时服务端只回游客占位（￥0.00、无SKU）。
* 身份 Cookie 有效期约 12h，过期需按上述步骤重新导出。
* ``config/.env`` 已被 ``.gitignore`` 排除，不会提交到仓库。
"""
from __future__ import annotations

import os

from src.settings import load_env

load_env()  # 幂等：把 config/.env 中缺失的键注入 os.environ（不覆盖已有值）

LOGIN_USER = os.environ.get("QCC_LOGIN_USER", "")
AUTH_TOKEN = os.environ.get("QCC_AUTH_TOKEN", "")
LOGIN_COOKIE = os.environ.get("QCC_LOGIN_COOKIE", "")
USER_AGENT = os.environ.get(
    "QCC_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0",
)
