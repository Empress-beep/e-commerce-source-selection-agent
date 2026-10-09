# 17qcc 搜索接口逆向分析报告（v1，已确认）

日期：2026-10-07
状态：**v1 分析已由用户确认；逆向代码已完成并端到端验证（见文末第 10 节）**
目标接口（用户指定，严禁更改）：
`https://newopenapiweb.17qcc.com/api/services/app/SearchFactory/GetPageList`
首页（key/iv 来源）：
`https://www.17qcc.com/list.html?keyword=春季男鞋&cateId=50011740&spm=1.5.4-4.4`

---

## 1. 启动门（Startup Gate）

- 环境：Python 3.10.9 / node / curl 可用（`check_reverse_env.py` 通过）
- 浏览器档位：`js-reverse-mcp` 普通 Chrome 基线，**未升级 CloakBrowser**（无指纹/反爬证据，页面 200 正常返回）
- 项目目录：`e-rpa-agent-spider-proto-17qcc/`（含 `analysis/ collector/ js_reverse_cache/ tests/`）
- 家族分类：**signer-gated + decode-gated**（请求体加密签名 + 响应体加密）
- 交付意向：纯 Python 采集器；JS 仅在必要时作为本地参数还原 helper（无浏览器依赖）
- 观察者效应：未装任何 hook/断点，网络样本为干净基线

## 2. 证据清单（全部在 `js_reverse_cache/`）

| 文件 | 内容 |
|---|---|
| `home.html` | 首页源码，含 `qccppm`、`qccrkeys` 密钥材料 |
| `combo.js` | `j.17qcc.com/combo?...encrypt...`（CryptoJS + JSEncrypt + rsautils） |
| `ajax_bundle.js` | `j.17qcc.com/combo?ajax-20&...&searchproduct-39`（jsjiami.com.v7 混淆，签名/加密封装本体） |
| `home_cookies.txt` | 首页 Set-Cookie：`_vid` |
| `api_probe_raw.txt` | 裸 POST 探测返回：`403 签名验证失败-0` |
| 浏览器基线（本次会话） | 真实 GetPageList 请求 reqid=45，200 成功 |

## 3. 真实请求长什么样（浏览器干净基线，reqid=45）

URL：POST `https://newopenapiweb.17qcc.com/api/services/app/SearchFactory/GetPageList`
Content-Type: `application/json`

请求头（业务相关）：

```
App-GuestId: A35FDA945E7E7791A4BA1CA572D6D82A
App-Version: 0
Data-Label: 2026062401
Data-Version: 1
Origin: https://www.17qcc.com
Qcc-Pc-Referer: https://www.17qcc.com/list.html
Referer: https://www.17qcc.com/
```

请求体（外层信封，非明文业务参数）：

```json
{
  "KM":       "CA37105E3825A5A62697100C835DAAB1",
  "Ver":      "1",
  "Content":  "2e2180755b0b7244...（小写 hex 密文）",
  "Sign":     "OR{S>DLw+x6N^<'J&~>$M4{?ahNGR=...（ASCII 特殊字符组成的签名串）",
  "RsaPubAes":"Qvg9AR70g0Bp6V7jPuAKhKWkEqsZa+dnBZ/...（Base64）",
  "IV":       "PpIIKhVlE6mYdYEN",
  "TimesTamp":"3BDD2E62F6E3378606A15A48D11E69E6"
}
```

响应：HTTP 200，`Result` 字段为**大写 hex 密文**（146KB），不是明文 JSON —— 属于 decode-gated。

裸请求（无签名）直接 403：`签名验证失败-0`。

## 4. key / iv 来源（用户判断正确，已定位）

首页内联脚本（每次页面加载生成，非硬编码固定值）：

```js
var qccppm = "2Pnsd4Xi9nu1kUlvfh19Xi55Eu7FXHay|3Hsm0IR9CdKwrd9J|67631D7531A811D6BEF87BEBC745E6F8";
var qccrkeys = ["0", "CA37105E3825A5A62697100C835DAAB1", "4", "123456", "0", "<一大段混淆字符串>", "1"];
window.environmentType = 2;
```

`ajax_bundle.js` 中的用法（已静态确认）：

```js
var parts = qccppm.split("|");        // 必须 3 段，否则报 "qccppm少于3个参数"
var _0x528173 = parts[0];             // AES 密钥（32 字节 UTF8/Hex parse）
var _0x33afe2 = parts[1];             // AES IV（16 字节）
var _0x23f8ec = parts[2];             // 32 hex，作为 TimesTamp 种子
...
qccrkeys[1] == 请求体里的 KM（"CA37105E..." 与首页 qccrkeys[1] 完全一致）
qccrkeys[5]/[6] + parseInt(qccrkeys[2/3/4]) 经函数 _0x23e60c() 运算 —— 疑似还原 RSA 公钥/签名参数
```

另有反调试壳：`eval("var _0x3e3ec9 = _0x28f994;...")`、`new JSEncrypt()` 被拆成数组 `["n","e","w"," ","J","S","E","n","c","r","y","p","t","()"]` 拼接 eval。

## 5. 加解密逻辑（静态已定位，待固定输入验证）

加密（请求 Content），`ajax_bundle.js` 内 `_0x5c446b`：

```
key  = CryptoJS.enc.Utf8(或Hex).parse(qccppm[0])
iv   = CryptoJS.enc.Utf8(或Hex).parse(qccppm[1])
密文 = CryptoJS.AES.encrypt(明文, key, {iv, mode: CBC, padding: Pkcs7})
输出 = .toString()  → 落到 Content（hex 形态，待验证 encode 步骤）
```

解密（响应 Result），`_0x2a5b01`（控制流平坦化 "4|5|1|0|2|3"）：

```
cipherBytes = Hex.parse(Result)          // case1: enc.Hex → Base64 → CipherParams
key  = parse(qccppm[0])                  // case4
iv   = enc.Utf8.parse(qccppm[1])         // case5，IV 直接取首页 qccppm 第 2 段
明文 = AES.decrypt(cipher, key, {iv, mode: CBC, padding: Pkcs7}).toString(enc.Utf8)   // case2/3
```

签名/信封构造：`_0x44cbd2 = {KM, Ver, Content, Sign, RsaPubAes, IV, TimesTamp}`（两处构造路径）
- `RsaPubAes`：`eval("new JSEncrypt()")` 后 RSA 加密 AES 密钥 → Base64
- `Sign`：特殊字符集串，疑似 RSA 私钥运算结果经自定义字符映射，**公式未还原**
- `TimesTamp`：32 位大写 hex，疑似由 qccppm[2] 种子 + 时间派生，**公式未还原**

## 6. 真实端点与业务参数

- 页面可见分页逻辑（`searchproduct` 模块）：`$.qccpager.init({postUrl:"/api/services/app/SearchFactory/GetPageList", postData: paginationData, ...})`
- 业务参数种子（首页内联 `paginationData`）：

```json
{"keyword":"春季男鞋","cateid":"50011740","spm":"1.5.4-4.4","IsGetShopOviewInfo":"1",
 "ProductSearchType":2,"IsGetVipPrice":0,"BanSupplier":"1","PageSize":"83"}
```

- 接口地址以 `window.QccJsWebPostBaseUrl = "https://newopenapiweb.17qcc.com"` 为准（ajax_bundle 顶部硬配置）
- 注意：`www.17qcc.com` 与 `newopenapiweb.17qcc.com` 跨域，响应 `access-control-allow-origin` 仅回显 `https://www.17qcc.com`（Python 直连需带 Origin/Referer，否则可能被 CORS/风控逻辑拒）

## 7. 尚未还原的部分（= 下一阶段要做的事）

1. `Content` 明文的确切 JSON 结构与序列化顺序（紧凑 JSON？字段顺序？）
2. `Sign` 的计算公式（对什么、什么算法、什么字符映射）
3. `TimesTamp` 的生成规则（与服务器时间/种子关系，是否有时效）
4. `qccppm[0]` 是按 Utf8 还是 Hex 解析（影响 key 推导）
5. RSA 公钥的完整来源（qccrkeys[5/6] 还原 or 页面其它位置）
6. 响应解密的固定输入验证（用 reqid=45 的真实密文离线解出明文）
7. `KM` / `App-GuestId` / `Data-Label` 等头是否参与校验、是否需随会话刷新
8. 分页翻页时参数变化（Page 变量）与重复成功率验证

## 8. 拟定交付形态（待你确认后再动工）

```
proto_17qcc/
├── analysis/          # 本报告、envelope_map、bootstrap_contract
├── collector/
│   ├── bootstrap.py   # GET 首页 → 解析 qccppm / qccrkeys / cookies
│   ├── envelope.py    # Content 加密 + Sign + RsaPubAes + TimesTamp 组装
│   ├── decode.py      # 响应 Result 的 AES-CBC hex 解密
│   └── main.py        # HTTP 直连、分页、输出
├── js_reverse_cache/  # 首页/JS/样本（已就位）
├── tests/             # 固定向量自检
└── requirements.txt
```

- 运行时：纯 Python（requests + pycryptodome），**无浏览器、无 Playwright/Selenium/CDP**
- 仅当 Sign 无法安全移植时，才保留一个本地 Node/JS 签名 helper（无 window/document 依赖）
- 首个里程碑：用 reqid=45 样本做到「离线解密响应成功 + 单次请求重放成功」，然后再谈分页/并发

## 9. 风险与合规提示

- 接口返回的是加密商品列表数据，属公开列表页数据，不含账号私有数据
- 当前样本为匿名会话（`App-Is-Login: 0`），未使用任何登录态 cookie
- 后续若需翻页/并发，会在你确认后按小流量节奏进行

---

**等待确认**：以上分析是否准确？确认后我再进入写代码逆向阶段（先做固定输入验证，再做单请求重放）。

---

## 10. 逆向实现结果（v2，已全部验证）

断点捕获：`set_breakpoint_on_text("_0x1c0044={KM:")` 命中真实信封构造点 `_0x45d93a`，
一次性导出明文对象 / Content / Sign / IV / TimesTamp / RsaPubAes / 两把 RSA 密钥 →
`js_reverse_cache/state/envelope_sample.json`（固定向量）。

### 10.1 逐项结论（原第 7 节问题全部关闭）

| # | 问题 | 结论（已验证） |
|---|---|---|
| 1 | Content 明文结构 | `JSON.stringify(业务对象)`，字段顺序即页面 `paginationData` + `page`；离线解密逐字节相等 |
| 2 | Sign 公式 | `transform(transform(base64(RSA-SHA1签(Content))))`，私钥 = 解码后的 `qccrkeys[5]` |
| 3 | TimesTamp | 就是 `qccppm[2]` 原样透传，无时间派生、无时效计算 |
| 4 | key 解析方式 | `CryptoJS.enc.Utf8.parse(qccppm[0])` / `iv = Utf8.parse(qccppm[1])`，**不是 Hex** |
| 5 | RSA 公钥来源 | `qccrkeys[5]`→私钥（签名用）、`qccrkeys[6]`→公钥（`RsaPubAes` 加密用），二者是**不同密钥对** |
| 6 | 响应解密固定输入 | `Result` 大写 hex → AES-CBC(key=qccppm[0], iv=qccppm[1], PKCS7) → UTF-8，已用真实响应验证 |
| 7 | KM / App-GuestId / Data-Label | `KM=qccrkeys[1]`（固定 `CA37105E...`）；`App-GuestId` **服务端不校验**（随机 GUID 也能通）；`Data-Label/Data-Version` 按抓包常量透传 |
| 8 | 翻页 | 仅业务参数 `page` 变化，其余信封逻辑不变；page 1/2 均复现成功 |

### 10.2 完整算法（离线可复现）

```
1) GET list.html            → 解析 qccppm、qccrkeys（8 元素）
   qccppm = aesKey | iv | timesTamp
   qccrkeys = [0, KM, "4", "123456", "0", privBlob(848), pubBlob(216), "1"]

2) 解码密钥（_0x23e60c，负向旋转）
   P = [2,2,3,4,6,9,14,22,35,6]         # 10 为周期，与内容/长度无关
   rot(ch, s): ASCII 32..126 上循环位移
   decode(blob) = rot(blob[i], -P[i%10]) 全体字符
   privPEM = "-----BEGIN PRIVATE KEY-----\n" + decode(qccrkeys[5]) + "\n-----END..."
   pubPEM  = "-----BEGIN PUBLIC KEY-----\n"  + decode(qccrkeys[6]) + "\n-----END..."

3) 请求信封
   plain    = JSON.stringify(businessParams)
   Content  = hex( AES-CBC(Utf8(aesKey), Utf8(iv), PKCS7, plain) )      # 小写 hex
   Sign     = tf( tf( base64( RSA-SHA1(privPEM, Content) ) ) )           # tf 施加两次
   RsaPubAes= base64( RSA-PKCS1v15-encrypt(pubPEM, aesKey) )
   body     = {KM:qccrkeys[1], Ver:"1", Content, Sign, RsaPubAes, IV:qccppm[1], TimesTamp:qccppm[2]}

4) 响应
   Result(大写 hex) → AES-CBC(同 key/iv, PKCS7) → UTF-8 JSON
```

`tf(s) = "".join(rot(s[i], P[i%10]))` —— `_0x1d50df(str,4,123456,0)` 的实测输出，
内容无关、长度无关（50/100/200/500 长度样本周期一致）。

### 10.3 验证结果

- `tests/test_fixed_vectors.py`：4 项固定向量全 PASS（密钥还原 / Content 往返 / Sign 一致 / 周期表）
- 真实端到端（纯 Python，无浏览器）：
  - `--keyword 春季男鞋 --page 1` → Success，`TotalCount=236`，83 条商品明文打印到终端
  - `--keyword 春季男鞋 --page 2` → Success，Page=2、`HasNextPage=true`
  - `--guest-id 00000000DEADBEEF...`（随机）→ 仍成功，证明该头不参与校验
- 裸请求（无签名）依旧 403 `签名验证失败-0`（对照组）

### 10.4 交付文件

```
collector/bootstrap.py   # GET 首页 → qccppm/qccrkeys 解析（字符串感知扫描，防密钥串内 '];' 截断）
                          # + qccrkeys[5/6] → PEM 还原
collector/envelope.py    # Content 加密、Sign、RsaPubAes、信封组装、POST
collector/decode.py      # 响应 Result 解密
collector/main.py        # CLI，解密后明文打印到终端
tests/test_fixed_vectors.py
```

运行：`python -m collector.main --keyword 春季男鞋 --page 1 [--out result.json]`
