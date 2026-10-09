"""17qcc 纯协议采集器：无浏览器依赖。

用法：
    python -m src.collector.main --keyword 春季男鞋 --page 1          # 默认不限类目
    python -m src.collector.main --keyword 春季男鞋 --cateid 50011740  # 限定男鞋类目
"""

__all__ = ["bootstrap", "envelope", "decode", "main"]
