# 商品详情接口逆向分析报告（17qcc）

- **版本**：v1.2（2026-10-07）
- **状态**：**已确认并落实** —— 确认项 1/2/4/6 已进入代码；采集范围判定见 §6（确认项 3 由我按你的五大类字段需求给出）
- **对应阶段交付**：`collector/detail.py`（四接口 + SSR 解析 + 动态 TmpVer + 采集即解密）+ `collector/main.py` 第 4 步 + `spider_db.api_backup` 的 `category='detail'`/`'page'` 行（`decoded_json` 已回填）+ `collector/etl.py` → `product_info` 业务表

> **v1.2 修订（2026-10-07，用户指令后口径变更）**
> 1. **`getproductseelist` 已从采集范围移除**（"我不需要这个"）。§2.1 的解密取证与 §6 的
>    判定表作为历史证据保留，但其中"商品信息**正式来源**"表述全部作废。
> 2. **商品信息来源改为三通道合并**：① 登录态详情页 **SSR HTML 解析**
>    （`parse_product_detail`：批发价/阶梯价/颜色尺码/库存/标题——当前商品**无**对应接口，
>    接口清单里价格/SKU 类全为 0 个，实测确认）；② 列表卡片（`GetPageList` 整行入库，
>    提供 BuyCount/Exposure/Click/价格卡面/店名档口）；③ 详情四接口（content/shop/grade/trade）。
> 3. **SSR 前置条件（实测三组对照）**：Cookie 必须同时含登录态
>    `.AspNetCore.Identity.Application`，否则返回游客占位（￥0.00、无 SKU、`guest_render=True`）。
> 4. **trade 翻页口径**：第 2 页 `TotalItems` = 第 1 页实际条数（发 0 回空，实测）；默认采 2 页。
> 5. 落地：`--detail-apis` 取值变为 `content/shop/grade/trade`；ETL 合并入
>    `sql/business_tables.sql` 的 `product_info`（`sql/backup_tables.sql` 注释同步）。

---

## 0. 结论摘要

| 项 | 结论 | 状态 |
|---|---|---|
| 详情主接口 | `POST /api/services/app/ProductFactory/GetProductContent` | ✅ 已确认 |
| 请求加密 | 与列表接口**同一套信封**（AES-CBC Content + RSA-SHA1 Sign + RSA 传 key，首页一次性下发） | ✅ 离线验证通过 |
| 响应加密 | `Result` 大写 hex，AES 同 key/iv 解密 | ✅ 离线验证通过 |
| 明文参数 | `{ProductId, TmpVer, Access}` 三个字段 | ✅ 已确认（bundle 源码 + 实抓） |
| `ProductId` 来源 | 列表接口 `Result.Items[].ProductId` | ✅ 已实现 |
| `TmpVer` 来源 | 动态提取：详情页 `var moudles` → `combo` bundle → 正则 `tmpVer:"..."`（**不硬编码**，进程内缓存） | ✅ 确认项2 已落实 |
| `Access` 来源 | **详情页 HTML 服务端渲染** `var accessC="..."`，逐页不同 → **每商品重新 GET HTML** | ✅ 确认项1 已落实 |
| 登录态 | `mustLogin:true`，需 Bearer token；实抓响应 `App-Is-Login: 1` | ✅ 已确认 |
| `getproductseelist` 明文 | 请求 `{"CateId":0,"SupplierId":8206,"Num":30}` → 响应**同供应商 30 条商品卡片**（与列表接口 48 字段完全同构） | ✅ 已解密取证（§2.1）；**v1.2 起不再采集** |
| 搜索接口定位 | `GetPageList` **只做目标发现**：`ProductId`/`Url` + 整行卡片入库（销量/曝光/点击/价格卡面/店名） | ✅ 已落实（v1.2：商品信息不再指 seelist，见页首修订） |
| 当前商品详情（价/SKU/库存） | **无对应接口**——SSR 直出在 `/item/<id>.html`，解析器 `parse_product_detail`；**必须带 `.AspNetCore.Identity.Application` Cookie**（三组实测：缺它 → ￥0.00 游客占位） | ✅ 已实现（v1.2 新增） |
| key/iv 时效 | **每次 bootstrap 一次性**（实测两次取值不同）→ 必须随报文行保存 | ✅ 已加 `aes_key`/`iv` 列（确认项6 认可） |
| 落地结构 | 采集即用当次 key/iv 解密，明文**直接回填详情行** `decoded_json`，不拆业务表 | ✅ 确认项4 已落实 |
| 采集范围 | 按 AI 选品五大类字段判定：**采 4 个接口**（正文/店铺/分级/成交）+ SSR HTML 解析 + 列表卡片，不采 7 个（含 seelist，v1.2 按用户指令移除；判定表见 §6） | ✅ 判定完成，已进代码 |

---

## 1. 调查范围与证据来源

**目标页面**：`https://www.17qcc.com/item/4476128.html`（青创网商品详情页）

**证据来源**：

1. Edge 实抓网络请求（CDP Network，`reqid=88` = GetProductContent 请求/响应全量头与体）；
2. 详情页 JS bundle 源码：`https://j.17qcc.com/combo?ajax-20&imglazy&pager-5&template-6&rollload&ajaxmethord-5&magnifier&detailproduct-97`（186,756 字符，接口调用点与明文参数全在这里）；
3. 详情页 HTML 内联脚本（`var accessC`、`var pageParam`）；
4. 离线复现脚本（临时目录，未进入交付代码）：`bootstrap -> HTML 取 Access -> build_body -> POST -> decrypt_hex`。

**固化证据**：`analysis/request_samples/detail_getproductcontent.json`（请求信封 + 原始响应密文 + 随行 key/iv 解密验证结果）。

---

## 2. 详情页接口清单

以下接口全部位于 `newopenapiweb.17qcc.com`，全部使用同一套加密封装（`$.qccAjax` 统一注入）。明文参数取自 bundle 源码。

| # | 接口 | 明文参数 | 触发时机 | 登录必需 | 本阶段处理 |
|---|---|---|---|---|---|
| 1 | `ProductFactory/GetProductContent` | `{ProductId, TmpVer, Access}` | 滚动到详情区 / 登录回调 `productDetail.GetProductContent()`，代码显式 `{mustLogin:true}` | **是** | ✅ **采**（商品基础信息）——采集即解密回填 |
| 2 | `shopfactory/getshopandsupplier` | `{SupplierId, PostageType, TmpVer, Access}` | 页面加载 | 是 | ✅ **采**（供应商 & 档口实力核心）——39 字段已解密入库 |
| 3 | `productdetail/getadverts` | `{MarketId: pageParam.MarketId==7?7:0}` | 页面加载 | 否 | ❌ 不采（广告位，无选品价值） |
| 4 | `productfactory/getproductseelist` | `{CateId, SupplierId, Num}` ← 实抓解密所得 | 页面加载 | 否 | ❌ **不采（v1.2 按用户指令移除）**：与列表卡片同构、冗余；商品信息走 SSR解析+卡片+四接口三通道（§2.1 取证保留） |
| 5 | `ProductFactory/GetProductGrade` | `{ProductId}` | 点击"评分/缺货率"页签 | 否 | ✅ **采**（履约风险 7 日趋势）——已解密入库 |
| 6 | `ProductFactory/GetProductDefaultSizeAdvice` | `{ProductId}` | 尺码建议页签 | 否 | ❌ 不采（尺码建议，无选品价值） |
| 7 | `TradeFactory/TradeRecordGetPageList` | `{ProductId, Page, TotalItems}` | 成交记录翻页 | 否 | ✅ **采**（销量/口碑佐证，先取第 1 页） |
| 8 | `SearchFactory/GetSimplePageList` / `ProductFactory/GetEliteProducts` | `para`（榜单/精选参数） | 页面加载 | 否 | ❌ 不采（推荐位，与列表接口重复） |
| 9 | `productfactory/increasebrowsinghistory` | `{ProductId, GuestId}` | 首次 mousemove/touchmove | 否 | ❌ 不采（埋点上报） |
| 10 | `productfactory/batchincreaseproductcc` | `{formtype:0, ids:"1,2,3"}` | 4.5s 定时批量曝光 | 否 | ❌ 不采（埋点上报） |
| 11 | `productfactory/collectionproduct` / `uncollectionproduct` | `{Id}` + `{mustLogin:true}` | 点击收藏 | 是 | ❌ 不采（交互动作） |

> **注**：商品详情页正文数据（标题/价格/图集等）是 **SSR 直出在 HTML** 里的，不经接口；
> 接口侧补的是属性/图文、店铺画像、趋势与成交四类增量数据。判定依据见 **§6 采集范围判定**。

### 2.1 `getproductseelist` 解密取证（回应"这个是不是详情页数据接口"）

> ⚠️ **v1.2：该接口已按用户指令不再采集**；本节保留为历史取证（明文结构结论仍有效）。

**结论先说**：它确实是详情页发出的接口，明文是"**同供应商的 30 条商品卡片列表**"
（结构与搜索列表接口完全一致），**不是当前商品的详情正文**（正文由
`GetProductContent` + SSR HTML 提供）。

**v1.1 口径调整（用户指定）**：搜索接口 `GetPageList` 只做目标发现（仅取
`ProductId`/`Url`），**商品信息一律以本接口为准** —— 已纳入采集，按
`pageParam.suppliereId` 请求后在 30 条里回查当前商品（命中写入日志与 remark，
未命中记 WARN）。

**解密链路**（reqid=39，页面 `/item/4476128.html`）：

1. 密钥来源：**详情页 HTML 内联** `var qccppm = "UjTlys0TImVEYKweWesUHPvuTBMu5mNe|FDmU6nDH5drGvLjk|3B1755884D7C4A425DBE727E623A4732"`（与该页所有接口共用）；
2. 校验：请求信封 `IV=FDmU6nDH5drGvLjk`、`TimesTamp=3B1755884D7C4A425DBE727E623A4732` 与页面 key **逐字一致** → 密钥对齐无误；
3. `decrypt_hex(Content, key, iv)` → 请求明文；`decrypt_hex(Result, key, iv)` → 响应明文。

**请求明文**：

```json
{"CateId": 0, "SupplierId": 8206, "Num": 30}
```

`SupplierId=8206` = 详情页 `pageParam.suppliereId`（当前商品所属供应商），`Num=30` 为取 30 条。

**响应明文**（数组 30 条，节选首条）：

```json
{"ProductId": 5748566, "ProductName": "2026秋季新款超轻防滑飞织祙子鞋户外慢跑中老年健步鞋...",
 "ProductNo": "2623", "Price": "46.00", "RetailPrice": 158.0, "BuyCount": 23,
 "YearSaleCount": 28, "Url": "https://www.17qcc.com/item/5748566.html",
 "Photo": "https://pics.17qcc.com/imgextra/...", "VideoUrl": "https://v.17qcc.com/video/...",
 "H12DeliveryRate": 0.684, "H24DeliveryRate": 0.789, "ShopName": null, "ShopUrl": null, ...}
```

**与列表接口的结构比对**（同一份 DTO）：

| 项 | getproductseelist | SearchFactory/GetPageList |
|---|---|---|
| 首条字段数 | 48 | 48 |
| 字段集合 | **完全相同（差集双向为空）** | 完全相同 |
| 条数 | 30（`Num` 决定） | 83（`PageSize` 决定） |
| 数据范围 | 同一供应商的商品（含当前商品 4476128 自身） | 关键词搜索结果 |
| 与列表页 83 条的 ID 交集 | 仅 1 条（就是当前商品） | — |
| `ShopName`/条内 `SupplierId` | 全 null / 全 0（精简卡片） | 有值 |

页面挂载点：bundle 内 `productRecommend.right` → `#rightProductsTab`（详情页右侧推荐位），
与 `SearchFactory/GetSimplePageList`、`ProductFactory/GetEliteProducts` 同属推荐位数据。

**取证文件**：
- `analysis/request_samples/getproductseelist_request.network-request`（请求信封原文）
- `analysis/request_samples/getproductseelist_response.network-response`（响应密文原文）
- `analysis/request_samples/getproductseelist_decrypted.json`（解密后的 30 条明文）

---

## 3. 主接口 GetProductContent 详解

### 3.1 请求

```
POST https://newopenapiweb.17qcc.com/api/services/app/ProductFactory/GetProductContent
Content-Type: application/json
```

**实抓请求头**（reqid=88，与列表接口的差异点已标 ★）：

| Header | 值 |
|---|---|
| ★ `Qcc-Pc-Referer` | `https://www.17qcc.com/item/4476128.html` |
| ★ `Accept` | `application/json, text/javascript, */*; q=0.01` |
| `Authorization` | `Bearer <token>`（登录态，与列表接口同一个 token） |
| `App-GuestId` | `A35FDA945E7E7791A4BA1CA572D6D82A` |
| `Origin` / `Referer` | `https://www.17qcc.com` / `https://www.17qcc.com/` |
| `Data-Label` / `Data-Version` / `App-Version` | `2026062401` / `1` / `0` |

**明文业务参数**（字段顺序与前端一致）：

```json
{"ProductId": 4476128, "TmpVer": "2023042202", "Access": "<448位十六进制>"}
```

| 参数 | 来源 | 说明 |
|---|---|---|
| `ProductId` | 列表接口 `Result.Items[].ProductId` | 采集链路已自动提取 |
| `TmpVer` | **动态提取**：详情页 `var moudles=[...]` 组出 `https://j.17qcc.com/combo?<模块>` → 正则 `productDetail={tmpVer:"..."}` → 进程内缓存（确认项2） | 实测提取到 `2023042202`，站点发版自动跟随，无硬编码 |
| `Access` | **商品详情页 HTML 内联**：`var accessC = "<hex>";` | 逐页不同（实测 416 / 448 位），**每商品重新 GET HTML**（确认项1） |

**信封字段**（与列表接口完全同构）：

```json
{"KM": "...", "Ver": "1", "Content": "<AES hex>", "Sign": "<transform(transform(base64(RSA-SHA1(Content))))>",
 "RsaPubAes": "<RSA(aesKey)>", "IV": "<16B>", "TimesTamp": "<hex>"}
```

信封材料全部来自**同一次** `bootstrap()`（首页 `qccppm` / `qccrkeys`），详情与列表可共用同一批 tokens。

### 3.2 响应

```
HTTP/1.1 200
app-is-login: 1
access-control-expose-headers: App-Is-Login, App-Must-Login, Data-Correlation-Id
content-type: application/json; charset=utf-8
```

```json
{"Result": "<大写 HEX 密文>", "TargetUrl": null, "Success": true,
 "Error": null, "UnAuthorizedRequest": false, "__abp": true}
```

- `Result` 用**本行报文的 aes_key/iv** 走 `envelope.decrypt_hex` 解密（与列表接口同一个函数）；
- 解密后结构：外层同上 6 个键，业务数据在 `Result` 内层。

### 3.3 解密验证（离线，分析用脚本）

```
bootstrap() -> GET 详情页 HTML -> 正则提取 accessC
-> build_body({ProductId,TmpVer,Access}) -> POST
-> HTTP 200 / App-Is-Login=1 / Result 长度 15232
-> decrypt_hex(Result, aes_key, iv)  -> 成功
```

内层明文字段（5 个）：

```json
{"Result": {"ProductCateName": "休闲板鞋", "ProductNo": "L860",
            "ProductComplaintType": [ ... ], "Content": "..." , "PropertyStr": "..."},
 "TargetUrl": ..., "Success": true, "Error": null, "UnAuthorizedRequest": false, "__abp": true}
```

> 完整样本（请求体 + 密文 + 解密字段清单 + 明文前 800 字符）：
> `analysis/request_samples/detail_getproductcontent.json`

---

## 4. 关键风险：key/iv 是一次性的

实测连续两次 `bootstrap()`：

```
k1 = 85L82e9CJmuZ4SYffTHPcYFC6b2v7Wk7
k2 = XlwtQ7Hp149XqLDHAZlmxvMax7xQdyXN   (不同)
iv1 != iv2   (不同)   KM 相同
```

**推论**：`raw_json` 里的密文**离开当次会话就解不开**，除非把当时的 `aes_key`/`iv` 一起存下来。

**处置（已实现）**：`api_backup` 增加 `aes_key`、`iv` 两列，每行报文随行保存；两把值只对该行有效，可随时弃置。解密公式 = `decrypt_hex(raw_json.Result, 本行.aes_key, 本行.iv)`。

---

## 5. 已交付实现（采集即解密回填）

```
搜索接口明文（GetPageList = 目标发现）
  └─ _extract_targets(): **只取 ProductId / Url 两个字段**（商品信息不从这里取）
       └─ 每商品 GET <Url> 一次 HTML               (collector/detail.fetch_detail_page)
            ├─ 正则 var accessC = "..."             -> Access
            ├─ 正则 var pageParam = {...}           -> SupplierId / PostageType
            └─ var moudles=[...] 组 combo URL        -> fetch_tmp_ver() 动态取 TmpVer（缓存）
                  ├─ POST getproductseelist  {CateId,SupplierId,Num}         -> 商品信息 30 卡片(48字段)【v1.2 已停采】
                 │      └─ 回查 ProductId==当前商品 -> 命中卡片写日志+remark，未命中 WARN
                 ├─ POST GetProductContent  {ProductId,TmpVer,Access}      -> 正文 5 字段
                 ├─ POST getshopandsupplier  {SupplierId,PostageType,TmpVer,Access} -> 店铺 39 字段
                 ├─ POST GetProductGrade     {ProductId}                   -> 分级 7 字段
                 └─ POST TradeRecordGetPageList {ProductId,Page,TotalItems} -> 成交记录
                      └─ 逐接口：decrypt_hex(Result, 本行 aes_key/iv)
                           └─ raw_json + decoded_json 双份入库 category='detail'
                                decrypt_ok=1（失败则 decoded_json=NULL, decrypt_ok=0）
                                整块明文只进日志文件（控制台仅一行进度）
```

实测一次运行（`run_id=20261007_125647`，`--detail-limit 1`）：

| id | category | api_name | product_id | decrypt_ok | 明文长度 | 备注 |
|---|---|---|---|---|---|---|
| 9 | list | SearchFactory/GetPageList | NULL | 1 | 142,129 B | 目标发现：仅取 ProductId/Url |
| 10 | detail | productfactory/getproductseelist | 4476128 | 1 | 39,591 B | 30 卡片，**卡片命中当前商品** |
| 11 | detail | ProductFactory/GetProductContent | 4476128 | 1 | 7,610 B | 内层 5 字段 |
| 12 | detail | shopfactory/getshopandsupplier | 4476128 | 1 | 1,925 B | 内层 39 字段 |
| 13 | detail | ProductFactory/GetProductGrade | 4476128 | 1 | 399 B | 7 日趋势 |
| 14 | detail | TradeFactory/TradeRecordGetPageList | 4476128 | 1 | 15,817 B | TotalItems=40 / 本页 19 条 |

控制台输出（仅进度，无任何明文）：

```
[1/4] 运行开始 run_id=20261007_125647
[2/4] GET 首页取 key/iv keyword=春季男鞋 ...
[3/4] POST SearchFactory/GetPageList page=1
      HTTP=200 登录=1 解密=成功 条目=83 备份行=9
[4/4] 详情采集(解密回填): 目标 1 条 x 5 接口 [seelist,content,shop,grade,trade]   ← v1.1 历史日志（v1.2 起为 4 接口）
      TmpVer 动态提取=2023042202（源=bundle combo，进程内缓存）
      4476128 商品信息 解密=成功 明文=37815B 备份行=10 卡片=命中
      4476128 正文 解密=成功 明文=7136B 备份行=11
      4476128 店铺 解密=成功 明文=1775B 备份行=12
      4476128 分级 解密=成功 明文=399B 备份行=13
      4476128 成交 解密=成功 明文=15117B 备份行=14
[完成] 列表报文 1 条 + 详情 5 条(商品信息1 + 正文1 + 店铺1 + 分级1 + 成交1) -> spider_db.api_backup
```

**异常处理**：详情页取不到 `accessC`（商品下架/风控，如实测 `/item/100.html` 短页）、
`TmpVer` 提取失败、单接口非 200 → 记 WARN 跳过该接口/商品，不中断整批；
入库失败降级为仅写日志。

---

## 6. 采集范围判定（按你的五大类字段需求）

**输入**：字段分五类，用途是 AI 选品打分、风险判断、毛利测算、供应商评估 ——
商品基础信息 / 价格毛利 / 销量流量指标 / 供应商 & 档口实力 / 履约风险指标。

**方法**：把每个接口的**已解密实证字段**（非源码猜测）映射到五类：
缺什么补什么，与已有来源重复的不采。

### 6.1 五大类 × 数据源覆盖矩阵

| 你要的类 | 关键字段 | 已有覆盖 | 缺口 | 判定（v1.2 修订） |
|---|---|---|---|---|
| 1 商品基础信息 | 标题/货号/类目/主图/视频/标签/属性/图文 | **列表卡片（`GetPageList` 整行 48 字段）**：`ProductName, ProductNo, Photo, VideoUrl, Url, CreationTime, ProductTagBinds`；**标题真值另由 SSR 解析**（`parse_product_detail.product_name`） | 类目名、属性串、图文正文**只在正文接口** | ✅ 采 `GetProductContent` + SSR 解析（seelist 移除） |
| 2 价格毛利 | 进货价/零售价/活动价/VIP价 | **SSR 解析**：`wholesale_price/tier_prices/retail_price`（**必须登录态 Cookie**）；卡片：`Price, RetailPrice, ActivityPrice/Name, PlusVipPriceInfo, ProductSupplyType` | 无（毛利 = RetailPrice − 进货价自算） | 已够，**不新增接口** |
| 3 销量流量指标 | 销量/年销/曝光/点击/成交明细 | 列表卡片：`BuyCount, YearSaleCount, ExposureCount, ClickCount` | 成交时间分布、评价质量分、SKU 热度、购买用户 | ✅ 采 `TradeRecordGetPageList`（实证 40 条记录，默认 2 页） |
| 4 供应商 & 档口实力 | 店名/档口号/市场/店龄/评分/保证金/资质/在售规模 | 列表卡片：`ShopName, ShopNumber, MarketName, MarketAreaName, ShopAge, SupplierQualificationExDtos`（条内 `SupplierId=0`，**资质里 SupplierId=8206**） | 交易评分、店铺级缺货率/退货率、保证金、资质、总在售数、上新数、经营模式 | ✅ **采 `getshopandsupplier`（本类核心，实证 39 字段）** |
| 5 履约风险指标 | 12/24h 发货率/退货率/缺货率/发货时效/投诉 | 列表卡片：`H12DeliveryRate, H24DeliveryRate, MailTimeliness, ReturnRate, OutOfStockRate, CanBan, IsLightning` | **趋势序列**（近 7 日）；商品投诉类型字典 | ✅ 采 `GetProductGrade`（7 日序列）+ 正文接口 `ProductComplaintType` |

### 6.2 判定结果（v1.2：详情域 4 采 / 7 不采；商品信息 = SSR解析 + 列表卡片）

| 接口 | 判定 | 理由 |
|---|---|---|
| `SearchFactory/GetPageList` | **目标发现 + 卡片整行入库** | `ProductId`/`Url` 进详情阶段；整行卡片（价格/销量/曝光/点击/店名档口）入 `api_backup` 供 ETL |
| `productfactory/getproductseelist` | **不采（v1.2 按用户指令移除）** | 与列表卡片字段完全同构、冗余（§2.1 取证保留）；商品信息走 SSR解析+卡片+四接口三通道 |
| 详情页 SSR HTML（`parse_product_detail`） | **新增·当前商品详情唯一来源** | 价格/阶梯价/颜色尺码/库存**无接口**（清单全查无价格/SKU类），SSR 直出；需 `.AspNetCore.Identity.Application` Cookie |
| `ProductFactory/GetProductContent` | 采（已有） | 类 1 属性/图文 + 类 5 投诉类型 |
| `shopfactory/getshopandsupplier` | 采（已有） | 类 4 唯一来源（39 字段） |
| `ProductFactory/GetProductGrade` | 采（已有） | 类 5 唯一趋势来源（7 日缺货/退货率序列） |
| `TradeFactory/TradeRecordGetPageList` | 采（已有） | 类 3 明细补充（成交时间、质量分、SKU）；默认 2 页，第2页 `TotalItems`=第1页条数 |
| `productdetail/getadverts` | 不采 | 广告位，无选品价值 |
| `GetProductDefaultSizeAdvice` | 不采 | 尺码建议（尺码/库存走 SSR 解析） |
| `GetSimplePageList` / `GetEliteProducts` | 不采 | 榜单/精选推荐位，与商品卡片信息重复 |
| `increasebrowsinghistory` / `batchincreaseproductcc` / `collectionproduct` | 不采 | 埋点上报 / 收藏交互 |

**成本变化**：每商品 1 次 HTML + ≤4 次 POST（trade 默认 2 次）。
**开关**：`--detail-apis content,shop,grade,trade` 可按需裁剪（默认全开）。

### 6.3 本轮新解密接口的明文证据

| 接口 | 明文字段数 | 关键字段（节选） | 取证文件 |
|---|---|---|---|
| `getshopandsupplier` | 39 | `ShopId, ShopName, ShopNumber, MarketName, AreaName, RateTradeScore, StockOutRate, ReturnGoodsRate, ShopAge, BondBalance, QualificationList, TotalProductCount, NewProductCount, CollectionCount, BusinessType, RegType, H12/H24DeliveryRate` | `analysis/request_samples/detail_supplier_decrypted.json` |
| `GetProductGrade` | 7 | `OutOfStockRateJson, ReturnRateJson, CreationTimeJson, Max/MinOutOfStockRate, Max/MinReturnRate` | `analysis/request_samples/detail_grade_decrypted.json` |
| `TradeRecordGetPageList` | `{Items, Pages, TotalItems}`（TotalItems=40） | `Items[].ProductRate.{BuyTime, ProductQualityScore, RateContent, UserName, SkuPropertyName}`, `TradeId` | `analysis/request_samples/detail_trade_decrypted.json` |

---

## 7. 待确认问题（需要你拍板）

| # | 问题 | 影响 | 结论 |
|---|---|---|---|
| Q1 | `accessC` 的生成规则与有效期：是否绑定 GuestId/会话/商品？过期后能否复用历史值 | 详情采集的请求成本与稳定性 | ✅ **确认项1：每个商品都重新 GET HTML 提取**（最稳，已实现） |
| Q2 | `TmpVer` 更新机制（是否仅站点发版变更） | 参数过期会导致接口报错 | ✅ **确认项2：去掉硬编码，改为动态提取**（moudles→bundle→正则+缓存，已实现） |
| Q3 | 采集范围：是否追加 #2/#5/#7 等接口 | 字段丰富度 | ✅ **确认项3：详情域采 4 / 不采 7（seelist 按用户指令移除）；商品信息 = 登录态SSR解析 + 列表卡片 + 四接口三通道合并**（判定表见 §6，v1.2 已实现） |
| Q4 | 解密后的落地结构：回填 `decoded_json` 还是拆 `product_info` 业务表 | 下一阶段工作量 | ✅ **确认项4：不拆业务表，直接更新详情行**（采集即回填，已实现） |
| Q5 | 表结构变更流程：改列需手工 `ALTER`/`DROP`，是否要 `sql/migrations/` 迁移目录 | 后续迭代效率 | ⏳ 待定（当前：直接改 `backup_tables.sql` 并重建，开发库仅测试数据） |
| Q6 | `api_backup` 明文存 `aes_key` 是否可接受（本机开发库） | 安全策略 | ✅ **确认项5：可接受**（已实现，随行保存；将来要抹可加"解密后清 key"开关） |

---

## 8. 确认清单（v1.1 回填）

- [x] **报告结论确认**（接口、参数、加密、accessC 来源均无误）
- [x] **解密动作确认**：确认项 1 —— 每个商品都重新 GET HTML 提取 `accessC`
- [x] **TmpVer 确认**：确认项 2 —— 不硬编码，改为动态提取（bundle 正则 + 进程内缓存）
- [x] **落地结构确认**：确认项 4 —— 不拆业务表，解密明文直接回填详情行 `decoded_json`
- [x] **密钥留存确认**：确认项 5 —— `api_backup` 明文存 `aes_key` 可接受
- [x] **采集范围确认**：确认项 3 —— 初判 **采 5 / 不采 6**；**v1.2 修订为采 4 / 不采 7**
      （seelist 移除），`GetPageList` = 目标发现 + 卡片整行入库（§6，已进代码）
- [x] **v1.2 新增**：SSR 详情解析（`parse_product_detail` + 登录态 Cookie）→ `page` 行入库；
      trade 默认 2 页（第2页 `TotalItems`=第1页条数）；`etl.py` 合并三通道 → `product_info`
- [ ] **Q5 迁移目录**：是否要 `sql/migrations/`（唯一遗留项，不影响当前功能）

---

## 9. 附录：交付文件索引

| 文件 | 说明 |
|---|---|
| `collector/detail.py` | 详情域四接口（`accessC`/`pageParam` 提取 + **动态 TmpVer** + 组参 + POST）+ `parse_product_detail` SSR 解析（价格/SKU/库存，登录态） |
| `collector/etl.py` | `api_backup` → `product_info` 业务表（卡片+SSR+四接口合并清洗，v1.2 新增） |
| `collector/envelope.py` | 信封构建 + `PostResult`（带回真实发出的请求体） |
| `collector/main.py` | 编排：列表解密入库（目标 + 卡片整行）→ 详情 SSR 解析 + 四接口**采集即解密回填**（`--detail-apis` 可裁剪）→ 批量关键词/风控节奏 → `[5/5]` ETL |
| `logger.py` / `database.py` | 运行日志 / MySQL 备份封装（详情、AI 模块可直接复用） |
| `sql/backup_tables.sql` | `spider_db.api_backup` DDL（含 `aes_key`/`iv`） |
| `tests/test_detail.py` | 详情页解析 / 动态 TmpVer / 参数组装单测（含"无硬编码"断言） |
| `analysis/request_samples/detail_getproductcontent.json` | 详情正文接口取证样本 |
| `analysis/request_samples/detail_{supplier,grade,trade}_decrypted.json` | 本轮新增三接口的解密明文（§6.3） |
| `analysis/request_samples/detail_api_probe_summary.json` | 三接口探测汇总（HTTP/登录态/字段数） |
| `analysis/request_samples/getproductseelist_{request,response}` | getproductseelist 请求/响应原文（reqid=39 抓包导出） |
| `analysis/request_samples/getproductseelist_decrypted.json` | getproductseelist 解密后的 30 条商品卡片明文 |
| `analysis/reverse_analysis_report.md` | 列表接口逆向报告 v2（已确认） |
| `logs/spider_<run_id>.log` | 每次运行的全量报文日志 |
