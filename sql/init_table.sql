-- =====================================================================
-- 电商货源 AI 选品 Agent · MySQL 初始化脚本（四张表一次建齐）
--
-- 执行方式（任选其一）：
--   1) 命令行：mysql -uroot -p < sql/init_table.sql
--   2) 代码内幂等执行：
--        from src.common import database
--        database.init_schema("sql/init_table.sql")
--
-- 表清单：
--   api_backup            原始备份表：一次接口调用一行（原始响应 + 解密明文）
--   product_info          清洗后业务表：ETL 汇总（AI 初筛 / 选品的数据源）
--   product_select_result 选品结果表：AI 打分 + 人工审核状态（只增不改 AI 字段）
--   product_material      素材生成表：审核通过商品 → 千问文案（gen_status + audit_status 双状态）
--
-- 设计约定：
--   * run_id 任务批次隔离：每次采集生成一个 run_id，写进日志文件名
--     （logs/spider_<run_id>.log）与三张业务表，做到任务间互不污染；
--   * raw_json 存服务端原始响应（不加工）；aes_key/iv 存本行报文当时所用
--     的一次性密钥，供该行密文后续解密；
--   * 比率类字段统一 0~1 小数（源为 "11.40%" 时除以 100）；
--   * 审核只允许改 audit_status / audit_operator / audit_comment / audit_time
--     四列，AI 原始评估字段（composite_score 等）只读。
-- =====================================================================

CREATE DATABASE IF NOT EXISTS `spider_db`
    DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;

USE `spider_db`;

-- =====================================================================
-- 1) api_backup：接口原始备份表（一次接口调用 = 一行报文）
--    列表接口 product_id 为 NULL；详情接口 product_id 填真实商品 ID；
--    详情页 HTML 解析结果记为 category=page。
-- =====================================================================
CREATE TABLE IF NOT EXISTS `api_backup` (
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `run_id`        VARCHAR(32)     NOT NULL                COMMENT '任务批次标识，与日志文件名 spider_<run_id>.log 对应（批次隔离）',
    `category`      VARCHAR(16)     NOT NULL DEFAULT 'list' COMMENT '报文类别: list=列表接口 / detail=详情接口 / page=详情页HTML解析',
    `api_name`      VARCHAR(128)    NOT NULL                COMMENT '接口短名, 如 SearchFactory/GetPageList',
    `api_url`       VARCHAR(255)    NOT NULL                COMMENT '完整请求 URL',
    `keyword`       VARCHAR(128)    NULL                    COMMENT '列表请求关键词',
    `page_no`       INT             NULL                    COMMENT '列表请求页码',
    `item_count`    INT             NULL                    COMMENT '本页返回条目数',
    `product_id`    BIGINT          NULL                    COMMENT '商品 ID；列表行为 NULL, 详情行为真实 ID',
    `detail_url`    VARCHAR(512)    NULL                    COMMENT '商品详情页 URL',
    `guest_id`      VARCHAR(64)     NULL                    COMMENT 'App-GuestId 请求头',
    `login_flag`    VARCHAR(8)      NULL                    COMMENT '响应头 App-Is-Login (登录态证据)',
    `http_status`   INT             NULL                    COMMENT 'HTTP 状态码',
    `aes_key`       VARCHAR(64)     NOT NULL                COMMENT '本行报文所用AES密钥(首页一次性下发), 供本行密文后续解密',
    `iv`            VARCHAR(32)     NOT NULL                COMMENT '本行报文所用AES IV(首页一次性下发)',
    `req_body`      LONGTEXT        NOT NULL                COMMENT '请求信封 JSON 原文 (KM/Ver/Content/Sign/...)',
    `raw_json`      LONGTEXT        NOT NULL                COMMENT '服务端原始响应(JSON/详情页HTML), 原样保存不加工',
    `decoded_json`  LONGTEXT        NULL                    COMMENT '完整解密 JSON; 未解密/解密失败为 NULL',
    `decrypt_ok`    TINYINT         NOT NULL DEFAULT 0      COMMENT '1=decoded_json 解密成功 0=未解密或失败',
    `error_msg`     VARCHAR(500)    NULL                    COMMENT '解密/入库等异常摘要',
    `remark`        VARCHAR(500)    NULL                    COMMENT '备注(如 detail: 密文留存待报告确认)',
    `created_at`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    PRIMARY KEY (`id`),
    KEY `idx_api_created` (`api_name`, `created_at`),
    KEY `idx_run_id`      (`run_id`),
    KEY `idx_category`    (`category`),
    KEY `idx_product_id`  (`product_id`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci
  COMMENT = '原始备份表: 接口报文一次调用一行(原始响应+解密明文+一次性密钥)';

-- =====================================================================
-- 2) product_info：清洗后业务表（api_backup 经 ETL 汇总，AI 初筛数据源）
--    数据来源三通道：
--      list   行 -> 卡片指标(销量/曝光/点击) + 卡片价/店铺名等快照
--      page   行 -> 详情页SSR解析(批发价/阶梯价/颜色尺码/库存/标题)
--      detail 行 -> content(类目/货号/投诉) / shop(店铺39字段) /
--                   grade(7日趋势) / trade(成交记录前2页)
--    唯一键 (run_id, keyword, product_id)：同批次同关键词同商品一行，
--    ETL 重跑为 UPSERT（新值覆盖旧值）。
-- =====================================================================
CREATE TABLE IF NOT EXISTS `product_info` (
    `run_id`               VARCHAR(32)     NOT NULL                COMMENT '任务批次标识，与 api_backup.run_id 对应（批次隔离）',
    `keyword`              VARCHAR(128)    NOT NULL DEFAULT ''     COMMENT '列表搜索关键词',
    `page_no`              INT             NULL                    COMMENT '列表页码',
    `product_id`           BIGINT          NOT NULL                COMMENT '商品 ID',

    -- ---- 商品基础信息（当前商品详情 = 详情页SSR；卡片兜底） ----
    `product_name`         VARCHAR(500)    NULL                    COMMENT '商品标题(SSR <title> 或卡片)',
    `product_no`           VARCHAR(64)     NULL                    COMMENT '货号(content.ProductNo)',
    `product_url`          VARCHAR(512)    NULL                    COMMENT '详情页URL(列表Url)',
    `cate_name`            VARCHAR(128)    NULL                    COMMENT '类目名(content.ProductCateName)，初筛 LIKE 匹配字段',
    `cover_photo`          VARCHAR(512)    NULL                    COMMENT '主图(列表卡片Photo)',
    `listing_time`         VARCHAR(32)     NULL                    COMMENT '商品上架时间(卡片CreationTime,日级)',

    -- ---- 价格毛利 ----
    `wholesale_price`      DECIMAL(12,2)   NULL                    COMMENT '批发起批价(SSR pri_de_price)，初筛拿货价上限字段',
    `retail_price`         DECIMAL(12,2)   NULL                    COMMENT '零售指导价(pageParam.RetailPrice)',
    `card_price`           DECIMAL(12,2)   NULL                    COMMENT '列表卡片价快照(Price)',
    `tier_prices`          JSON            NULL                    COMMENT '阶梯价 [{num,money}](SSR data-mixprice)',

    -- ---- SKU / 库存（详情页SSR独有） ----
    `colors`               JSON            NULL                    COMMENT '颜色分类 [{vid,name}]',
    `sku_stock`            JSON            NULL                    COMMENT '每色尺码 {name,price,stock}',
    `stock_total`          INT             NULL                    COMMENT '各码可售库存合计(SSR)',

    -- ---- 销量流量（列表卡片独有，详情页无） ----
    `buy_count`            INT             NULL                    COMMENT '销力/销量(卡片BuyCount)，初筛销量下限字段',
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
    `return_goods_rate`    DECIMAL(8,6)    NULL                    COMMENT '退货率0-1(店铺ReturnGoodsRate %/100)，初筛退货率上限字段',
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

    -- ---- 追溯（商品id/请求地址/状态码/接口调用明细） ----
    `request_url`          VARCHAR(512)    NULL                    COMMENT '主请求地址(详情页GET api_backup.api_url; 无详情时=卡片Url)',
    `http_status`          INT             NULL                    COMMENT '主请求HTTP状态码(详情页GET api_backup.http_status; 未采详情为NULL)',
    `api_trace`            JSON            NULL                    COMMENT '逐接口调用追溯 [{api_name,api_url,http_status,decrypt_ok}]',
    `data_source`          VARCHAR(64)     NULL                    COMMENT '实际来源逗号表: list,page,content,shop,grade,trade',
    `created_at`           DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '首次写入时间',
    `updated_at`           DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最近更新时间',
    PRIMARY KEY (`run_id`, `keyword`, `product_id`),
    KEY `idx_product_id` (`product_id`),
    KEY `idx_shop_name` (`shop_name`(64))
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci
  COMMENT = '清洗后业务表: api_backup ETL汇总(列表卡片+详情SSR+四详情接口)，初筛/AI选品数据源';

-- =====================================================================
-- 3) product_select_result：AI 选品结果表（只增不改 AI 字段 + 人工审核闭环）
--    审核状态机：pending -> pass/reject/hold；hold 可复审；
--    pass/reject 为终态禁止再次修改；reject 必填 audit_comment 驳回原因。
--    唯一允许 UPDATE 的列：audit_status / audit_operator / audit_comment / audit_time。
-- =====================================================================
CREATE TABLE IF NOT EXISTS `product_select_result` (
    `id`                   BIGINT          NOT NULL AUTO_INCREMENT COMMENT '自增主键，待审核Excel首列id的回写匹配键',
    `product_id`           BIGINT          NOT NULL                COMMENT '商品ID(product_info.product_id)',
    `product_name`         VARCHAR(500)    NULL                    COMMENT '商品标题',
    `composite_score`      DECIMAL(5,2)    NULL                    COMMENT 'AI综合得分0-100',
    `recommend_level`      VARCHAR(16)     NULL                    COMMENT 'AI推荐等级: 强烈推荐/可考虑/不建议',
    `risk_list_json`       JSON            NULL                    COMMENT 'AI风险清单JSON数组(risk_list转json字符串)',
    `reason`               TEXT            NULL                    COMMENT 'AI选品理由',
    `select_condition_json` JSON           NULL                    COMMENT '本次筛选条件JSON(select_condition归档)',
    `audit_status`         VARCHAR(16)     NOT NULL DEFAULT 'pending' COMMENT '人工审核状态: pending/pass/reject/hold',
    `audit_operator`       VARCHAR(64)     NULL                    COMMENT '人工审核人(Excel audit_operator列回写)',
    `audit_comment`        TEXT            NULL                    COMMENT '人工审核备注(reject必填驳回原因)',
    `audit_time`           DATETIME        NULL                    COMMENT '人工审核时间',
    `created_at`           DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'AI选品结果写入时间',
    PRIMARY KEY (`id`),
    KEY `idx_product_id` (`product_id`),
    KEY `idx_created_at` (`created_at`),
    KEY `idx_audit_status` (`audit_status`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci
  COMMENT = 'AI选品结果表: AI评分只增不改 + 人工审核状态闭环(Excel导入回写)';

-- =====================================================================
-- 4) product_material：AI 素材(文案)生成表
--    一行一商品（UNIQUE product_id）；多版本候选文案收在 content_json 内。
--    双状态维度：
--      gen_status  = 机器生成生命周期（断点续跑依据）：
--                    pending -> generating -> success / failed；
--                    重跑幂等：跳过 success，补 failed 与未生成的；
--      audit_status = 人工审核（pass/hold/reject，NULL=待审）：
--                    pass=可用进上架；hold=待观望；reject=打回重生成。
--    拒绝/通过只允许写 audit_status/audit_operator/audit_comment/audit_time
--    四个字段（由 material_audit 状态机保证），AI 生成字段只读。
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
