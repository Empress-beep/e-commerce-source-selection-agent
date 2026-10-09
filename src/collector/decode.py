"""响应解密：Result 字段（大写 hex）-> 明文 JSON 字符串。"""

from __future__ import annotations

import json

from .bootstrap import Tokens
from .envelope import decrypt_hex


def decrypt_result(result_hex: str, tokens: Tokens) -> str:
    return decrypt_hex(result_hex, tokens.aes_key, tokens.iv)


def decrypt_result_json(result_hex: str, tokens: Tokens):
    text = decrypt_result(result_hex, tokens)
    return json.loads(text)
