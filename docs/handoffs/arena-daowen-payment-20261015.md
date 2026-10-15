# 会话交接：道纹支付术语与再生合并

- 日期：2026-10-15
- 目标：删除独立【自食】道纹并将其并入【再生】；统一规则标签「消耗：」「代价：」为「支付：」；所有法力支付明确标注「法力X」；【再生X】支付法力X或攻击力4X，为目标回复4X生命。
- 实际分支：`arena-daowen-payment-20261015`
- PR：https://github.com/lilei3061460849-create/linji-disiyuzhou/pull/62
- 已完成：更新《规则正文》《法术索引》《全道纹索引》与 `engine/daowen.py` 的支付标注；更新 `sim/gen_daowen_index.py`，确保再生成索引时保留「支付：法力X」；新增 `tests/test_payment_annotations.py` 覆盖再生双支付通道、4X回复、自食注册移除、全力曲解路径与旧标签。
- 合并状态：未合并 `main`，保留普通 PR 供手动审核。
- 测试状态：尚未运行本地 pytest 或 GitHub Actions；不得视为已验证。
- 待办：核对生成器 `--check` 与专项测试；检查 CI 结果；若生成器输出与索引存在差异，修正生成器/派生索引后再次提交。
