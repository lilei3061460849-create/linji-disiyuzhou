"""支付术语、法力标注与【再生】合并的回归护栏。"""
from pathlib import Path

from engine.daowen import DaoWenEngine, ResonanceEngine

ROOT = Path(__file__).resolve().parent.parent
MANA_DAOWEN = (
    "杀伐", "庇护", "波及", "贯穿",
    "愤怒", "寄生", "弱化", "无力", "急速", "加速", "蒙蔽",
    "变形", "定型", "超频", "坏死", "退化",
    "清算", "抢劫", "勒索", "假钞",
    "加害", "龙鳞", "裂变", "背负", "伤痕",
    "缄默", "瓦解", "冥气", "勾魂", "招魂", "净化",
)


def test_rebirth_cost_channels_and_healing_are_unified():
    mana = DaoWenEngine.calculate_zaisheng(3)
    assert mana["cost"] == 3
    assert mana["target_heal"] == 12
    assert "支付：法力3" in mana["summary"]

    attack = DaoWenEngine.calculate_zaisheng(3, cost_channel="攻击力")
    assert attack["cost_attack"] == 12
    assert attack["target_heal"] == 12
    assert "支付：攻击力12" in attack["summary"]


def test_self_feed_is_not_a_registered_daowen_and_path_points_to_rebirth():
    DaoWenEngine.register_all()
    assert "自食" not in DaoWenEngine._registry
    paths = ResonanceEngine.get_available_resonance("全力")
    assert any(
        path["resonance_type"] == "曲解" and path["target_daowen"] == "再生"
        for path in paths
    )


def test_mana_costs_are_explicit_in_the_daowen_index():
    index = (ROOT / "全道纹索引.md").read_text(encoding="utf-8")
    rows = {
        line.split("|")[1].strip(): line
        for line in index.splitlines()
        if line.startswith("| ") and line.count("|") >= 6
    }
    for name in MANA_DAOWEN:
        assert name in rows, f"索引缺少道纹：{name}"
        assert "支付：法力" in rows[name], f"{name} 的法力支付未明确标注：{rows[name]}"


def test_rule_docs_do_not_use_legacy_payment_labels():
    for filename in ("规则正文.md", "全道纹索引.md", "法术索引.md"):
        text = (ROOT / filename).read_text(encoding="utf-8")
        assert "消耗：" not in text, f"{filename} 仍含旧标签「消耗：」"
        assert "代价：" not in text, f"{filename} 仍含旧标签「代价：」"
