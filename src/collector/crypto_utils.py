from __future__ import annotations

import base64
import hashlib


def chunk_bytes(data: bytes, size: int) -> list[bytes]:
    return [data[index : index + size] for index in range(0, len(data), size)]


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def b64encode_text(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")
