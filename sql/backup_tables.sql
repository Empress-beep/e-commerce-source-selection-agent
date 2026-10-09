-- =====================================================================
-- 17qcc 采集器 · 报文备份库 DDL
-- 执行方式（任选其一）：
--   1) 代码内幂等执行： from src.common import database; database.init_schema()
--   2) 命令行：         mysql -uroot -p < sql/backup_tables.sql
-- 设计约定：
--   * 一次接口调用 = 一行报文（列表接口 product_id 为 NULL，
--     详情接口 product_id 填真实商品 ID；详情页 HTML 解析为 category=page）；
--   * raw_json  存服务端原始响应（JSON 原样/详情页 HTML 原样，不加工）；
--   * aes_key/iv 存**本行报文当时所用**的 AES 密钥与 IV —— 首页每次下发的
--     key/iv 都是一次性的（实测两次 bootstrap 不同），不存就永远解不开
--     这一行的密文；两把密钥只对本行有效，可随时弃置；
--   * decoded_json 存完整解密 JSON（page 行为 SSR 页面解析结果）；解密失败
--     或本阶段不解密时为 NULL，由 decrypt_ok / remark 说明原因；
--   * 业务字段（product_info）由 collector.etl 从本表汇总，见
--     sql/business_tables.sql。
-- =====================================================================

CREATE DATABASE IF NOT EXISTS `spider_db`
    DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;

USE `spider_db`;

CREATE TABLE IF NOT EXISTS `api_backup` (
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `run_id`        VARCHAR(32)     NOT NULL                COMMENT '运行标识，与日志文件名 spider_<run_id>.log 对应',
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
  COMMENT = '17qcc 接口报文备份(列表明文+详情密文), 一次调用一行';
