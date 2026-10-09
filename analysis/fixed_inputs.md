# Fixed inputs

## Helper vectors
- 样本文件：`js_reverse_cache/state/envelope_sample.json`（断点 `_0x1c0044={KM:` 处一次性导出）
- Input（业务明文）：
  `{"keyword":"春季男鞋","cateid":"50011740","spm":"1.5.4-4.4","IsGetShopOviewInfo":"1","ProductSearchType":2,"IsGetVipPrice":0,"BanSupplier":"1","PageSize":"83","page":"1"}`
- Expected browser output（`Content`）：`1868eb7a539c07063f61cc5864bb1ea8…`（小写 hex，见样本文件）
- Expected local output：`tests/test_fixed_vectors.py::test_content_roundtrip` 与 `::test_sign` 均 PASS

## Decode vectors
- Raw payload sample：`qccrkeys[5]`（848 字符）/ `qccrkeys[6]`（216 字符）
- Expected decoded sample：`envelope_sample.json` 的 `privKey` / `pubKey`（PEM，含头尾）
- Decoder：`collector/bootstrap.py::decode_blob`（`rot(ch, -P[i%10])`，P=[2,2,3,4,6,9,14,22,35,6]）

## Envelope vectors
- Signed payload JSON：`Sign = transform(transform(base64(RSA-SHA1(Content))))`
- Wrapped request body：
  `{KM:qccrkeys[1], Ver:"1", Content, Sign, RsaPubAes, IV:qccppm[1], TimesTamp:qccppm[2]}`
- 验证：`tests/test_fixed_vectors.py`（离线） + 真实 POST 成功（`Success:true`，236 条）

## Live replay vectors
- `python -m collector.main --keyword 春季男鞋 --page 1` → 83 items / TotalCount=236
- `python -m collector.main --keyword 春季男鞋 --page 2` → Page=2 / HasNextPage=true
- 保存样例：`analysis/request_samples/page2_sample.json`
