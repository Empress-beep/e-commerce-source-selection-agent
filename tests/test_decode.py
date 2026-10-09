import json
import unittest

from src.collector.bootstrap import Tokens
from src.collector.decode import decrypt_result, decrypt_result_json
from src.collector.envelope import encrypt_content, to_json

KEY = "L6bAlDPSRyCvwIn3H7z7DQRmR1jpuk78"
IV = "RFhbDgk5UA0uInX3"


def _tokens(key: str = KEY, iv: str = IV) -> Tokens:
    """构造只带 aes_key/iv 的 Tokens（解密路径只用这两个字段）。"""
    return Tokens(
        session=None,
        page_url="https://www.17qcc.com/list.html",
        qccppm=[key, iv, "TS"],
        qccrkeys=["", "KM", "", "", "", "", ""],
    )


class DecodeTests(unittest.TestCase):
    def test_decrypt_result_roundtrip(self) -> None:
        payload = {"Result": {"TotalCount": 236, "Items": [{"ProductId": 4476128}]}}
        plain = to_json(payload)
        cipher_hex = encrypt_content(plain, KEY, IV).upper()  # 响应 Result 是大写 hex
        self.assertEqual(decrypt_result(cipher_hex, _tokens()), plain)

    def test_decrypt_result_json(self) -> None:
        payload = {"Result": {"Items": []}}
        cipher_hex = encrypt_content(to_json(payload), KEY, IV)
        self.assertEqual(decrypt_result_json(cipher_hex, _tokens()), payload)

    def test_decrypt_result_wrong_key_raises(self) -> None:
        cipher_hex = encrypt_content('{"a":1}', KEY, IV)
        with self.assertRaises(Exception):
            decrypt_result(cipher_hex, _tokens(key="X" * 32))

    def test_decrypt_result_empty_input_raises(self) -> None:
        with self.assertRaises(Exception):
            decrypt_result("", _tokens())


if __name__ == "__main__":
    unittest.main()
