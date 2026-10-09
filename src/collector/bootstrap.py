"""首页引导：GET list.html 解析 qccppm / qccrkeys 并还原 RSA 密钥对。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import requests

HOMEPAGE = (
    "https://www.17qcc.com/list.html"
    "?keyword={keyword}&cateId={cateid}&spm={spm}"
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_QCCPPM_RE = re.compile(r"var\s+qccppm\s*=\s*\"(.*?)\";", re.S)
_QCCRKEYS_MARK = re.compile(r"var\s+qccrkeys\s*=")

# _0x1d50df / _0x23e60c 的字符旋转表（实测：以 10 为周期，内容与长度无关）
ROT_PATTERN = [2, 2, 3, 4, 6, 9, 14, 22, 35, 6]


def _rot_char(ch: str, shift: int) -> str:
    """ASCII 32..126 上的循环位移（对应 _0x374cae）。"""
    code = ord(ch)
    if code < 32 or code > 126:
        return ch
    shift %= 95
    new = code + shift
    if new > 126:
        new -= 95
    if new < 32:
        new += 95
    return chr(new)


def decode_blob(blob: str) -> str:
    """qccrkeys[5]/[6] -> PEM 主体（对应 _0x23e60c，负向旋转）。"""
    return "".join(_rot_char(c, -ROT_PATTERN[i % 10]) for i, c in enumerate(blob))


def private_pem(blob: str) -> str:
    return "-----BEGIN PRIVATE KEY-----\n" + decode_blob(blob) + "\n-----END PRIVATE KEY-----"


def public_pem(blob: str) -> str:
    return "-----BEGIN PUBLIC KEY-----\n" + decode_blob(blob) + "\n-----END PUBLIC KEY-----"


@dataclass
class Tokens:
    session: requests.Session
    page_url: str
    qccppm: list = field(default_factory=list)
    qccrkeys: list = field(default_factory=list)

    @property
    def aes_key(self) -> str:
        return self.qccppm[0]

    @property
    def iv(self) -> str:
        return self.qccppm[1]

    @property
    def timestamp(self) -> str:
        return self.qccppm[2]

    @property
    def km(self) -> str:
        return self.qccrkeys[1]

    @property
    def ver(self) -> str:
        return "1"

    @property
    def private_key_pem(self) -> str:
        return private_pem(self.qccrkeys[5])

    @property
    def public_key_pem(self) -> str:
        return public_pem(self.qccrkeys[6])

    def as_dict(self) -> dict:
        return {
            "page_url": self.page_url,
            "qccppm": self.qccppm,
            "km": self.km,
            "ver": self.ver,
            "aes_key_len": len(self.aes_key),
            "iv": self.iv,
            "times_tamp": self.timestamp,
            "qccrkeys_lens": [len(x) for x in self.qccrkeys],
        }


def _extract_js_array(text: str, start: int) -> str:
    """从 '[' 起扫描，忽略字符串内部的 '];'（密钥串里可能出现）。"""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                while end < len(text) and text[end] in " \t\r\n":
                    end += 1
                if end < len(text) and text[end] == ";":
                    end += 1
                return text[start:end]
    raise RuntimeError("qccrkeys 数组未闭合")


def parse_homepage(html: str) -> tuple[list, list]:
    ppm = _QCCPPM_RE.search(html)
    mark = _QCCRKEYS_MARK.search(html)
    if not ppm or not mark:
        raise RuntimeError("首页未找到 qccppm/qccrkeys（可能被风控或页面结构变化）")
    qccppm = ppm.group(1).split("|")
    if len(qccppm) < 3:
        raise RuntimeError("qccppm 少于 3 个参数")
    lb = html.find("[", mark.end())
    if lb < 0:
        raise RuntimeError("qccrkeys 数组起始括号缺失")
    arr_src = _extract_js_array(html, lb)
    try:
        qccrkeys = json.loads(arr_src[: arr_src.rfind("]") + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"qccrkeys 解析失败: {exc}") from exc
    if len(qccrkeys) < 7:
        raise RuntimeError(f"qccrkeys 元素不足: {len(qccrkeys)}")
    return qccppm, qccrkeys


def bootstrap(
    keyword: str = "春季男鞋",
    cateid: str = "0",
    spm: str = "1.5.4-4.4",
    session: requests.Session | None = None,
    timeout: float = 20.0,
) -> Tokens:
    """GET 首页拿一次性密钥材料（key/iv/RSA 密钥均来自这里）。"""
    sess = session or requests.Session()
    url = HOMEPAGE.format(keyword=keyword, cateid=cateid, spm=spm)
    resp = sess.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    resp.raise_for_status()
    qccppm, qccrkeys = parse_homepage(resp.text)
    return Tokens(session=sess, page_url=url, qccppm=qccppm, qccrkeys=qccrkeys)
