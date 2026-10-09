import unittest

from src.collector.envelope import decrypt_hex, encrypt_content, to_json, transform


class EnvelopeTests(unittest.TestCase):
    """信封层纯函数单测（不发网络、不依赖真实 RSA 样本）。

    与 test_fixed_vectors.py 的分工：那边用浏览器真实样本校验
    Content/Sign/密钥还原，这边只锁基础行为契约。
    """

    FIXED_KEY = "L6bAlDPSRyCvwIn3H7z7DQRmR1jpuk78"  # 32 字节 AES-256
    FIXED_IV = "RFhbDgk5UA0uInX3"  # 16 字节

    def test_to_json_is_compact(self) -> None:
        self.assertEqual(to_json({"a": 1, "b": [1, 2]}), '{"a":1,"b":[1,2]}')
        self.assertNotIn(" ", to_json({"a": 1}))
        self.assertEqual(to_json({"名": "鞋"}), '{"名":"鞋"}')

    def test_encrypt_decrypt_roundtrip(self) -> None:
        plain = to_json({"ProductId": 4476128, "Access": "ABC"})
        hex_cipher = encrypt_content(plain, self.FIXED_KEY, self.FIXED_IV)
        self.assertEqual(hex_cipher, hex_cipher.lower())  # 请求 Content = 小写 hex
        self.assertEqual(len(hex_cipher) % 32, 0)  # 整块 16 字节
        self.assertEqual(decrypt_hex(hex_cipher, self.FIXED_KEY, self.FIXED_IV), plain)

    def test_decrypt_hex_rejects_garbage(self) -> None:
        with self.assertRaises(Exception):
            decrypt_hex("00" * 16, self.FIXED_KEY, self.FIXED_IV)

    def test_transform_deterministic(self) -> None:
        s = "abcXYZ012!@# 中文不变"
        self.assertEqual(transform(s), transform(s))
        # ASCII 32..126 之外的字符（如中文）不参与旋转
        self.assertEqual(transform("中"), "中")


if __name__ == "__main__":
    unittest.main()
