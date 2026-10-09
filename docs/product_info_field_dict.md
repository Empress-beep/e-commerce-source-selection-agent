# product_info 业务表 · 字段字典（通俗版）

> 表：`spider_db.product_info`，共 60 列。DDL 见 `sql/business_tables.sql`。
> 本文用大白话解释每个字段：**是什么、怎么读、空值代表什么**，
> 不再依赖 "SSR / pri_de_price / 卡片" 这类内部黑话。

---

## 一、这行数据是什么（主键）

一行 = **一次采集(run) × 一个搜索词(keyword) × 一个商品(product_id)**。
同一个商品被两个关键词搜到，会占两行（各自记录当时的卡片数据）。

| 字段 | 含义 | 说明 |
|---|---|---|
| `run_id` | 采集轮次编号 | 形如 `20261007_153132`（日期_时间）。拿它能去 `api_backup` 原始表和 `logs/spider_<run_id>.log` 里回查这轮的全部原始报文 |
| `keyword` | 搜索关键词 | 这行是用哪个词搜出来的，如"春季板鞋男" |
| `page_no` | 搜索结果页码 | 该商品出现在第几页结果 |
| `product_id` | 商品 ID | 就是详情页 URL `www.17qcc.com/item/后面那串数字` |
| `created_at` / `updated_at` | 写库时间 | 首次写入 / 最近一次覆盖更新 |

**通用约定**：
- 空值 `NULL` = 这个来源没采到或该商品本来没有（不是 0），比如没采详情页时价格为空；
- 比率类字段一律 **0~1 小数**：`0.114` 表示 11.4%；
- `JSON` 列是结构化明细，标量列供直接筛选排序；
- 每行有 `data_source` 标明这行由哪几类数据合成：
  `list`(搜索卡片) / `page`(详情页) / `content`(商品正文) / `shop`(店铺) / `grade`(7日趋势) / `trade`(成交记录)。

---

## 二、商品基础信息

| 字段 | 含义 | 示例 / 备注 |
|---|---|---|
| `product_name` | 商品标题 | "2024春季新款男士休闲板鞋…" |
| `product_no` | 货号 | 档口自己的款号，如 `XY-8821` |
| `product_url` | 详情页链接 | `https://www.17qcc.com/item/4476128.html` |
| `cate_name` | 平台类目 | 如 `男鞋 > 休闲鞋` |
| `cover_photo` | 主图链接 | 搜索列表里显示的那张图 |
| `listing_time` | 上架日期 | `2024-09-24`，**越新=越可能是新款**（对应站内"上架时间"排序） |

## 三、价格 / 毛利空间

| 字段 | 含义 | 示例 / 备注 |
|---|---|---|
| `wholesale_price` | **拿货价（起批价）** | `48.00` = 1 件拿 48 元，选品成本基准 |
| `retail_price` | 零售指导价 | `168.00`，可对照算加价空间 |
| `card_price` | 搜索列表卡片显示价 | 卡片快照，与拿货价可能不同 |
| `tier_prices` | 阶梯价 | JSON：`[{"num":2,"money":46}]` = 2 件起每件 46 元 |

## 四、颜色尺码 / 库存（只有采了详情页才有）

| 字段 | 含义 | 示例 / 备注 |
|---|---|---|
| `colors` | 有哪些颜色 | JSON：`[{"vid":"28320","name":"白色"}]` |
| `sku_stock` | 每个颜色×尺码的价格和库存 | JSON：`[{"name":"39","price":48,"stock":997}, …]` |
| `stock_total` | 全部尺码可售库存合计 | `17906`，判断供货是否充足 |

## 五、销量 / 流量（只在搜索卡片上，详情页没有）

| 字段 | 含义 | 备注 |
|---|---|---|
| `buy_count` | 销量 | 卡片"已售 N 件"，**市场需求信号** |
| `year_sale_count` | 年销量 | 近一年口径 |
| `exposure_count` | 曝光数 | 推广数据：有多少人看到过 |
| `click_count` | 点击数 | 配合曝光算点击率 |

## 六、供应商 / 档口实力

| 字段 | 含义 | 备注 |
|---|---|---|
| `shop_name` | 店铺 / 档口名 | |
| `shop_number` | 档口号 | 档口在市场里的编号 |
| `supplier_id` | 供应商 ID | 取自店铺资质（卡片上是 0 所以用接口值） |
| `market_name` | 所属市场 | 如 `广州十三行` |
| `market_area_name` | 区域 / 楼层 | 如 `A栋3楼` |
| `shop_age` | 店龄（年） | 越大越稳定 |
| `rate_trade_score` | 交易评分 | **0~5 分**（注意：不是 0~1），商家综合交易评价 |
| `pin_rate` | 拼货率 | 0~1，`0.114`=11.4% |
| `stock_out_rate` | 缺货率 | 0~1，**越低越好** |
| `return_goods_rate` | 退货率 | 0~1，**越低越好** |
| `bond_balance` | 保证金 | 平台缴纳金额，实力/信用参考 |
| `h12_delivery_rate` | 12 小时发货率 | 0~1，发货速度 |
| `h24_delivery_rate` | 24 小时发货率 | 0~1 |
| `total_product_count` | 在售商品数 | 店铺经营规模 |
| `new_product_count` | 新品数 | 上新活跃度 |
| `qualifications` | 资质列表 | JSON：营业执照等 |

## 七、履约风险（近 7 天）

| 字段 | 含义 | 备注 |
|---|---|---|
| `grade_out_of_stock_max` | 7 日缺货率**峰值** | 0~1，7 天里最差那天的缺货率，**尖峰高=供货不稳** |
| `grade_return_rate_max` | 7 日退货率**峰值** | 0~1，同上 |
| `grade_trend` | 7 日趋势曲线 | JSON，缺货率/退货率等逐日序列，可看是在恶化还是好转 |
| `complaint_types` | 投诉分类字典 | JSON。**注意：这是平台固定分类表**（品质投诉/描述投诉…各带状态），不是"本商品被投诉记录"，全站商品都一样，**不能当个案风险用** |

## 八、成交记录佐证（自动拉了成交记录前 2 页）

| 字段 | 含义 | 备注 |
|---|---|---|
| `trade_total_items` | 成交记录总条数 | 该商品历史上多少条成交记录 |
| `trade_sample_count` | 我们实际抓到的条数 | 前 2 页合计；`0`=没有成交记录或为空 |
| `trade_qty_sum` | 抓到的记录购买件数合计 | 样本内一共买了多少件 |
| `trade_avg_quality` | 样本买家"质量分"均值 | 平台内部的买家优质度评分（0~5），辅助判断客群 |

> 没有 7 天/30 天销量字段——平台不提供，`buy_count` 是唯一销量口径。

## 九、请求追溯（企业审计）

| 字段 | 含义 | 备注 |
|---|---|---|
| `request_url` | 主请求地址 | 详情页 URL；没采详情的商品兜底为列表卡片里的链接 |
| `http_status` | 主请求 HTTP 状态码 | `200`=成功；`NULL`=该商品只采了卡片、没进详情 |
| `api_trace` | 这个商品调过的每个接口 | JSON 数组，每项 `{api_name, api_url, http_status, decrypt_ok}`，能逐条回放：调了谁、地址、状态、解密是否成功 |
| `data_source` | 这行由哪些源合成 | 如 `list,page,content,shop,grade,trade`=全量数据；只有 `list`=仅卡片数据 |

---

## 十、常用查询示例

```sql
-- 选品初筛：有价格、销量>500、退货率<5% 的新款
SELECT product_id, product_name, wholesale_price, buy_count, listing_time, shop_name
FROM product_info
WHERE run_id='20261007_153132'
  AND wholesale_price IS NOT NULL
  AND buy_count > 500 AND return_goods_rate < 0.05
ORDER BY buy_count DESC;

-- 数据完整度 / 审计：哪些行有完整追溯
SELECT data_source, COUNT(*), SUM(api_trace IS NOT NULL) AS traced
FROM product_info WHERE run_id='20261007_153132' GROUP BY data_source;

-- 回放某商品的原始报文（用追溯三列 + run_id）
SELECT api_name, api_url, http_status, decrypt_ok
FROM product_info, JSON_TABLE(api_trace, '$[*]'
  COLUMNS(api_name VARCHAR(128) PATH '$.api_name',
          api_url  VARCHAR(512) PATH '$.api_url',
          http_status INT      PATH '$.http_status',
          decrypt_ok  INT      PATH '$.decrypt_ok')) t
WHERE run_id='20261007_153132' AND product_id=4476128;
```
