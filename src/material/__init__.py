"""AI 素材生成包：P1 文案生成（标题/卖点/详情骨架/brief）。

* ``generator``      —— 千问文案生成主流程（事实卡组装 + 三层校验 + gen_status 断点）
* ``material_audit`` —— 素材人工审核状态机（pass/hold/reject 唯一校验入口）
* ``excel_material_import`` —— 素材审核 Excel 批量导入回写脚本
"""
