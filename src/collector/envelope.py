"""请求信封：Content(AES) / Sign(RSA-SHA1) / RsaPubAes / IV / TimesTamp。"""

from __future__ import annotations

import base64
import json

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from .bootstrap import ROT_PATTERN, Tokens
from .session_login import AUTH_TOKEN, LOGIN_COOKIE, USER_AGENT

API_URL = "https://newopenapiweb.17qcc.com/api/services/app/SearchFactory/GetPageList"

BASE_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.17qcc.com",
    "Referer": "https://www.17qcc.com/",
    "Qcc-Pc-Referer": "https://www.17qcc.com/list.html",
    "App-Version": "0",
    "Data-Label": "2026062401",
    "Data-Version": "1",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": USER_AGENT,
}

DEFAULT_GUEST_ID = "A35FDA945E7E7791A4BA1CA572D6D82A"


def _rot_char(ch: str, shift: int) -> str:
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


def transform(s: str) -> str:
    """_0x1d50df：按 ROT_PATTERN 旋转一次（Sign 里连用两次）。"""
    return "".join(_rot_char(c, ROT_PATTERN[i % 10]) for i, c in enumerate(s))


def to_json(obj) -> str:
    """等价于 JSON.stringify（紧凑、保留插入顺序）。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def encrypt_content(plaintext_json: str, aes_key: str, iv: str) -> str:
    """_0x5c446b：AES-CBC + PKCS7，输出小写 hex。"""
    cipher = AES.new(aes_key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
    ct = cipher.encrypt(pad(plaintext_json.encode("utf-8"), 16))
    return ct.hex()


def decrypt_hex(hex_cipher: str, aes_key: str, iv: str) -> str:
    """_0x2a5b01：hex 密文 -> 明文（请求/响应用同一把 key/iv）。"""
    from Crypto.Util.Padding import unpad

    data = bytes.fromhex(hex_cipher)
    cipher = AES.new(aes_key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
    return unpad(cipher.decrypt(data), 16).decode("utf-8")


def sign_content(content: str, private_key_pem: str) -> str:
    """_0x12a8a3：SHA1withRSA -> base64 -> transform x2。"""
    key = load_pem_private_key(
        private_key_pem.encode(), password=None, backend=default_backend()
    )
    sig = key.sign(content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA1())
    b64 = base64.b64encode(sig).decode("ascii")
    return transform(transform(b64))


def rsa_encrypt_aes_key(aes_key: str, public_key_pem: str) -> str:
    """JSEncrypt.encrypt：RSA PKCS1 v1.5 加密 AES 密钥 -> base64。"""
    key = load_pem_public_key(public_key_pem.encode(), backend=default_backend())
    ct = key.encrypt(aes_key.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(ct).decode("ascii")


def build_body(tokens: Tokens, plaintext_obj: dict) -> dict:
    content = encrypt_content(to_json(plaintext_obj), tokens.aes_key, tokens.iv)
    return {
        "KM": tokens.km,
        "Ver": tokens.ver,
        "Content": content,
        "Sign": sign_content(content, tokens.private_key_pem),
        "RsaPubAes": rsa_encrypt_aes_key(tokens.aes_key, tokens.public_key_pem),
        "IV": tokens.iv,
        "TimesTamp": tokens.timestamp,
    }


class PostResult:
    """POST 结果容器。

    ``json, headers = post(..., with_headers=True)`` 的解包写法依旧可用
    （实现了 ``__iter__``），同时额外带上 ``request_body`` —— 信封里含
    RSA 随机填充，事后重建的 body 和真实发出的不一致，必须当场带回，
    备份库 req_body 列存的必须是**真实发出**的那份。
    """

    __slots__ = ("json", "headers", "request_body", "url")

    def __init__(self, json_data: dict, headers: dict, request_body: dict, url: str):
        self.json = json_data
        self.headers = headers
        self.request_body = request_body
        self.url = url

    def __iter__(self):
        # 兼容旧写法: resp, headers = post(...)
        return iter((self.json, self.headers))

    def get(self, key, default=None):
        return self.json.get(key, default)


def business_params(
    keyword: str = "春季男鞋",
    cateid: str = "0",
    spm: str = "1.5.4-4.4",
    page: int = 1,
    page_size: str = "83",
    product_search_type: int = 2,
) -> dict:
    """业务明文（字段顺序与页面 paginationData 一致）。"""
    return {
        "keyword": keyword,
        "cateid": cateid,
        "spm": spm,
        "IsGetShopOviewInfo": "1",
        "ProductSearchType": product_search_type,
        "IsGetVipPrice": 0,
        "BanSupplier": "1",
        "PageSize": page_size,
        "page": str(page),
    }


def post(
    tokens: Tokens,
    plaintext_obj: dict,
    guest_id: str = DEFAULT_GUEST_ID,
    timeout: float = 30.0,
    login: bool = True,
    with_headers: bool = False,
    api_url: str | None = None,
    extra_headers: dict | None = None,
):
    """POST 业务接口（默认列表接口 GetPageList）。

    :param api_url:      换接口用（如详情接口 ProductFactory/GetProductContent），
                         不传则用 :data:`API_URL`。
    :param extra_headers: 覆盖/追加请求头（详情接口需要 Qcc-Pc-Referer 指向商品页）。
    login=True 时带上 session_login.py 里的 Authorization(Bearer token) 与 Cookie，
    服务端据此返回登录态（响应头 App-Is-Login: 1）。
    with_headers=True 时返回 :class:`PostResult`（可解包成 json, headers，
    另带真实发出的 request_body，供备份库 req_body 列使用）。
    """
    body = build_body(tokens, plaintext_obj)
    headers = dict(BASE_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    headers["App-GuestId"] = guest_id
    if login:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
        headers["Cookie"] = LOGIN_COOKIE
    resp = tokens.session.post(
        api_url or API_URL, json=body, headers=headers, timeout=timeout
    )
    resp.raise_for_status()
    if with_headers:
        return PostResult(resp.json(), dict(resp.headers), body, api_url or API_URL)
    return resp.json()
