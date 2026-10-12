"""固定向量自检：用浏览器断点捕获的真实样本验证离线算法。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.collector.bootstrap import decode_blob, private_pem, public_pem
from src.collector.envelope import decrypt_hex, encrypt_content, sign_content, to_json, transform

STATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests",
    "fixtures",
    "envelope_sample.json",
)


def test_content_roundtrip():
    d = json.load(open(STATE, encoding="utf-8"))
    plain_json = to_json(d["plaintext"])
    assert decrypt_hex(d["content"], d["qccppm"].split("|")[0], d["iv"]) == plain_json
    assert encrypt_content(plain_json, d["qccppm"].split("|")[0], d["iv"]) == d["content"]
    print("[ok] Content 加解密与浏览器样本一致")


def test_sign():
    d = json.load(open(STATE, encoding="utf-8"))
    got = sign_content(d["content"], d["privKey"])
    assert got == d["sign"], f"\ngot : {got}\nwant: {d['sign']}"
    print("[ok] Sign(RSA-SHA1 + transform x2) 与浏览器样本一致")


def test_keys():
    d = json.load(open(STATE, encoding="utf-8"))
    assert private_pem(d["qccrkeys"][5]) == d["privKey"]
    assert public_pem(d["qccrkeys"][6]) == d["pubKey"]
    assert decode_blob(d["qccrkeys"][5]) == d["privKey"].splitlines()[1]
    print("[ok] qccrkeys -> RSA 私钥/公钥还原一致")


def test_transform_period():
    s = "".join(chr(32 + (i % 95)) for i in range(172))
    t1 = transform(s)
    assert all(ord(a) - ord(b) for a, b in zip(s[:1], t1[:1]))
    P = [2, 2, 3, 4, 6, 9, 14, 22, 35, 6]
    for i, (a, b) in enumerate(zip(s, t1)):
        assert (ord(b) - ord(a)) % 95 == P[i % 10]
    print("[ok] transform 周期表 = [2,2,3,4,6,9,14,22,35,6]")


if __name__ == "__main__":
    test_keys()
    test_content_roundtrip()
    test_sign()
    test_transform_period()
    print("ALL PASS")
