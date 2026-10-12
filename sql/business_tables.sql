-- =====================================================================
-- 17qcc 采集器 · AI选品业务表 DDL（product_info）
-- 执行方式（任选其一）：
--   1) ETL 自动执行： from src.collector import etl; etl.run_etl(log, run_id)
--   2) 代码内幂等执行： from src.common import database; database.run_sql_file("sql/business_tables.sql")
--   3) 命令行：         mysql -uroot -p < sql/business_tables.sql
-- 设计约定：
--   * 数据来源 = api_backup 汇总（ETL，不重复采集）：
--       list   行 -> 卡片指标(销量/曝光/点击) + 卡片价/店铺名等快照；
--       page   行 -> 详情页SSR解析(批发价/阶梯价/颜色尺码/库存/标题)；
--       detail 行 -> content(类目/货号/投诉/正文) / shop(店铺39字段) /
--                    grade(7日趋势) / trade(成交记录前2页)；
--   * 唯一键 (run_id, keyword, product_id)：同一轮采集同关键词同商品一行，
--     ETL 重跑为 UPSERT（新值覆盖旧值）；
--   * 比率类字段统一 0~1 小数（源为 "11.40%" 时除以 100）；
--     rate_trade_score 为 0~5 评分，不在此列；
--   * JSON 列存结构化明细（阶梯价/颜色尺码/资质/趋势/投诉/正文属性），
--     标量列供 AI 直接筛选排序；
--   * 可追溯：run_id 回链 api_backup / 日志文件；request_url + http_status
--     记录主请求地址与状态码；api_trace 记录该商品逐接口调用明细。
-- =====================================================================

CREATE DATABASE IF NOT EXISTS `spider_db`
    DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;

USE `spider_db`;

CREATE TABLE IF NOT EXISTS `product_info` (
    `run_id`               VARCHAR(32)     NOT NULL                COMMENT '运行标识，与 api_backup.run_id 对应',
    `keyword`              VARCHAR(128)    NOT NULL DEFAULT ''     COMMENT '列表搜索关键词',
    `page_no`              INT             NULL                    COMMENT '列表页码',
    `product_id`           BIGINT          NOT NULL                COMMENT '商品 ID',

    -- ---- 商品基础信息（当前商品详情 = 详情页SSR；卡片兜底） ----
    `product_name`         VARCHAR(500)    NULL                    COMMENT '商品标题(SSR <title> 或卡片)',
    `product_no`           VARCHAR(64)     NULL                    COMMENT '货号(content.ProductNo)',
    `product_url`          VARCHAR(512)    NULL                    COMMENT '详情页URL(列表Url)',
    `cate_name`            VARCHAR(128)    NULL                    COMMENT '类目名(content.ProductCateName)',
    `cover_photo`          VARCHAR(512)    NULL                    COMMENT '主图(列表卡片Photo)',
    `listing_time`         VARCHAR(32)     NULL                    COMMENT '商品上架时间(卡片CreationTime,日级)',

    -- ---- 价格毛利 ----
    `wholesale_price`      DECIMAL(12,2)   NULL                    COMMENT '批发起批价(SSR pri_de_price)',
    `retail_price`         DECIMAL(12,2)   NULL                    COMMENT '零售指导价(pageParam.RetailPrice)',
    `card_price`           DECIMAL(12,2)   NULL                    COMMENT '列表卡片价快照(Price)',
    `tier_prices`          JSON            NULL                    COMMENT '阶梯价 [{num,money}](SSR data-mixprice)',

    -- ---- SKU / 库存（详情页SSR独有） ----
    `colors`               JSON            NULL                    COMMENT '颜色分类 [{vid,name}]',
    `sku_stock`            JSON            NULL                    COMMENT '每色尺码 {name,price,stock}',
    `stock_total`          INT             NULL                    COMMENT '各码可售库存合计(SSR)',

    -- ---- 销量流量（列表卡片独有，详情页无） ----
    `buy_count`            INT             NULL                    COMMENT '销力/销量(卡片BuyCount)',
    `year_sale_count`      INT             NULL                    COMMENT '年销量(卡片YearSaleCount)',
    `exposure_count`       INT             NULL                    COMMENT '曝光数(卡片ExposureCount)',
    `click_count`          INT             NULL                    COMMENT '点击数(卡片ClickCount)',

    -- ---- 供应商 & 档口实力（卡片 + getshopandsupplier） ----
    `shop_name`            VARCHAR(128)    NULL                    COMMENT '店铺/档口名(卡片ShopName)',
    `shop_number`          VARCHAR(64)     NULL                    COMMENT '档口号(卡片ShopNumber)',
    `supplier_id`          BIGINT          NULL                    COMMENT '供应商ID(QualificationList.SupplierId)',
    `market_name`          VARCHAR(128)    NULL                    COMMENT '市场名(卡片MarketName)',
    `market_area_name`     VARCHAR(128)    NULL                    COMMENT '区域/楼层(卡片MarketAreaName)',
    `shop_age`             INT             NULL                    COMMENT '店龄年(卡片/店铺ShopAge)',
    `rate_trade_score`     DECIMAL(4,2)    NULL                    COMMENT '交易评分0-5(店铺RateTradeScore)',
    `pin_rate`             DECIMAL(8,6)    NULL                    COMMENT '拼货率0-1(店铺PinRate %/100)',
    `stock_out_rate`       DECIMAL(8,6)    NULL                    COMMENT '缺货率0-1(店铺StockOutRate %/100)',
    `return_goods_rate`    DECIMAL(8,6)    NULL                    COMMENT '退货率0-1(店铺ReturnGoodsRate %/100)',
    `bond_balance`         DECIMAL(12,2)   NULL                    COMMENT '保证金(店铺BondBalance)',
    `h12_delivery_rate`    DECIMAL(6,4)    NULL                    COMMENT '12h发货率0-1(店铺H12DeliveryRate)',
    `h24_delivery_rate`    DECIMAL(6,4)    NULL                    COMMENT '24h发货率0-1(店铺H24DeliveryRate)',
    `total_product_count`  INT             NULL                    COMMENT '在售商品数(店铺TotalProductCount)',
    `new_product_count`    INT             NULL                    COMMENT '新品数(店铺NewProductCount)',
    `qualifications`       JSON            NULL                    COMMENT '资质列表(店铺QualificationList)',

    -- ---- 履约风险（grade + content） ----
    `grade_out_of_stock_max` DECIMAL(10,6) NULL                    COMMENT '7日缺货率峰值(grade)',
    `grade_return_rate_max`  DECIMAL(10,6) NULL                    COMMENT '7日退货率峰值(grade)',
    `grade_trend`          JSON            NULL                    COMMENT '7日趋势序列(grade 三个Json字段)',
    `complaint_types`      JSON            NULL                    COMMENT '投诉类型(content.ProductComplaintType)',

    -- ---- 交易佐证（trade 前2页汇总） ----
    `trade_total_items`    INT             NULL                    COMMENT '成交记录总条数(页1 TotalItems)',
    `trade_sample_count`   INT             NULL                    COMMENT '已采集成交记录条数(2页合计)',
    `trade_qty_sum`        INT             NULL                    COMMENT '采集记录 Quantity 合计',
    `trade_avg_quality`    DECIMAL(6,2)    NULL                    COMMENT '采集记录质量分均值(ProductQualityScore)',

    -- ---- 追溯（企业审计：商品id/请求地址/状态码/接口调用明细） ----
    `request_url`          VARCHAR(512)    NULL                    COMMENT '主请求地址(详情页GET api_backup.api_url; 无详情时=卡片Url)',
    `http_status`          INT             NULL                    COMMENT '主请求HTTP状态码(详情页GET api_backup.http_status; 未采详情为NULL)',
    `api_trace`            JSON            NULL                    COMMENT '逐接口调用追溯 [{api_name,api_url,http_status,decrypt_ok}](page+detail行按明文去重)',
    `data_source`          VARCHAR(64)     NULL                    COMMENT '实际来源逗号表: list,page,content,shop,grade,trade',
    `created_at`           DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '首次写入时间',
    `updated_at`           DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最近更新时间',
    PRIMARY KEY (`run_id`, `keyword`, `product_id`),
    KEY `idx_product_id` (`product_id`),
    KEY `idx_shop_name` (`shop_name`(64))
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci
  COMMENT = 'AI选品业务表: api_backup ETL汇总(列表卡片+详情SSR+四详情接口)';

-- =====================================================================
-- P1 素材(文案)生成表 product_material
-- 说明：
--   * 一行一商品（UNIQUE product_id）；多版本候选文案收在 content_json 内；
--   * gen_status = 机器生成生命周期（断点续跑依据）：
--       pending -> generating -> success / failed；
--       重跑幂等：跳过 success，补 failed 与未生成的；
--   * audit_status = 人工审核（pass/hold/reject，NULL=待审），约定：
--       pass  = 素材可用，进入上架环节；
--       hold  = 待观望（可改可再审，非终态）；
--       reject= 打回重新生成（作废本版素材，商品保留）；
--   * 拒绝/通过只允许写 audit_status/audit_operator/audit_comment/audit_time
--     四个字段（由 material_audit 状态机保证），AI 生成字段只读；
--   * content_json 结构：{titles:[{version,title,strategy}x3], short_points:[x5],
--       detail_outline:{pain_point,scene,specs[],trust}, sku_renames:[],
--       brief:{scene,style,palette,selling_point}, risk_flags:[]}。
-- =====================================================================

CREATE TABLE IF NOT EXISTS `product_material` (
    `id`                BIGINT          NOT NULL AUTO_INCREMENT,
    `product_id`        BIGINT          NOT NULL                COMMENT '商品ID(product_info.product_id)',
    `gen_status`        VARCHAR(16)     NOT NULL DEFAULT 'pending' COMMENT '机器生成状态: pending/generating/success/failed',
    `gen_error`         VARCHAR(500)    NULL                    COMMENT '生成失败原因(gen_status=failed)',
    `content_json`      JSON            NULL                    COMMENT '千问生成的文案JSON(titles/short_points/detail_outline/sku_renames/brief/risk_flags)',
    `model`             VARCHAR(64)     NULL                    COMMENT '生成所用模型',
    `prompt_version`    VARCHAR(32)     NULL                    COMMENT '提示词版本(调优钩子, 如 PV-20261011-1)',
    `machine_check`     VARCHAR(500)    NULL                    COMMENT '机器校验结论: pass 或 fail:原因摘要',
    `risk_flags_json`   JSON            NULL                    COMMENT '模型自报存疑点JSON数组(供人工优先复核)',
    `audit_status`      VARCHAR(16)     NULL                    COMMENT '人工审核状态: NULL=待审 pass/hold/reject',
    `audit_operator`    VARCHAR(64)     NULL                    COMMENT '人工审核人',
    `audit_comment`     TEXT            NULL                    COMMENT '人工审核备注(reject必填原因; pass可标注选用候选版)',
    `audit_time`        DATETIME        NULL                    COMMENT '人工审核时间',
    `created_at`        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
    `updated_at`        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最近更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_product_id` (`product_id`),
    KEY `idx_gen_status` (`gen_status`),
    KEY `idx_audit_status` (`audit_status`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci
  COMMENT = 'AI素材(文案)生成表: 选品pass商品 -> 千问文案; 双状态维度(gen_status机器/audit_status人工)';
