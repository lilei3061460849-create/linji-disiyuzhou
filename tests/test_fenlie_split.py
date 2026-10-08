"""【分裂】X/Y 双参数重做（2026-09-17 用户令）。

旧版：代价冷却X；[命零]时创造X个本体血限20%的复制体。
2026-09-17 新版：**即时**创造 X 个 10Y 血限的自身复制体，代价【衰老】= X×10Y = 总血限。
2026-10-08 用户令：代价由【衰老】改为【流血】（数值不变，付当前生命而非[血限]）。

新版要点（逐条对照旧版废止的三处）：
1. 触发时机：[命零] → 即时发动（不再需要本体先死）
2. 复制体血限：本体20%（随本体浮动，越壮越赚）→ 固定 10Y（Y 自定）
3. 代价：冷却X（与产出无关，可白嫖）→ 流血X×10Y（造多少血流多少血）
4. 旧版 `entity_type != "怪物"` 过滤导致怪物永远不分裂，已取消，各类型一视同仁

运行方式：
    python -m pytest tests/test_fenlie_split.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.daowen import DaoWenEngine
from engine.models import DaoWen, DaoWenInstance, Entity


def _engine(actor_is_monster: bool = False):
    e = GameEngine()
    p = Entity("轮回者", "轮回者", blood_limit=200, current_hp=200,
               mana_limit=99, current_mana=99, speed_limit=9, current_speed=9,
               attack_count=9, attack_power=99)
    m = Entity("怪", "怪物", blood_limit=300, current_hp=300, attack_count=2,
               attack_power=10, mana_limit=10, current_mana=10,
               speed_limit=2, current_speed=2)
    e.state.player = p
    e.state.friends = []
    e.state.employees = []
    e.state.enemies = [m]
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.current_round = 2
    actor = m if actor_is_monster else p
    actor.dao_wen["分裂"] = DaoWenInstance(
        DaoWen(name="分裂", formula="", cost_type="流血",
               cost_formula="X*10Y", effect_formula=""),
        x_value=0, x_free=True)
    return e, p, m, actor


def _cast(e, actor, *, x=3, y=2, aref=""):
    kw = {"actor_ref": aref} if aref else {}
    return e.execute_action("use_daowen", {
        **kw, "daowen_name": "分裂", "x": x, "y": y,
        "target_ref": "enemy:0" if aref else "player:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}})


# ---------------------------------------------------------------- 公式

def test_cost_equals_total_clone_blood_limit():
    """代价流血 = X×10Y，恰好等于造出的总血限。"""
    for x, y in ((1, 1), (3, 2), (2, 5), (4, 3)):
        c = DaoWenEngine.resolve("分裂", x, y=y)
        assert c["cost_type"] == "流血"
        assert c["cost_hp"] == x * 10 * y
        assert c["split_clones"] == x
        assert c["clone_hp"] == 10 * y
        assert c["cost_hp"] == c["split_clones"] * c["clone_hp"]


def test_y_defaults_to_one():
    """不传 y 时默认 1，单参数调用不受影响。"""
    c = DaoWenEngine.resolve("分裂", 3)
    assert c["clone_hp"] == 10 and c["cost_hp"] == 30


# ---------------------------------------------------------------- 结算

def test_casting_pays_bleed_and_spawns_clones_immediately():
    """正常路径：即时付流血、即时创造复制体（不必等命零）。"""
    e, p, m, actor = _engine()
    r = _cast(e, actor, x=3, y=2)
    assert r["success"] is True, r
    assert p.current_hp == 200 - 60, "流血 = 3×10×2 = 60，付当前生命"
    assert p.blood_limit == 200, "流血不扣[血限]"
    assert len(e.state.temp_friends) == 3, "应即时创造 3 个复制体"
    for c in e.state.temp_friends:
        assert c.blood_limit == 20 and c.current_hp == 20, "每个复制体 10Y = 20 血限"


def test_clones_inherit_daowen_but_not_fenlie():
    """复制体继承本体道纹，但不继承【分裂】——否则可无限套娃。"""
    e, p, m, actor = _engine()
    actor.dao_wen["庇护"] = DaoWenInstance(
        DaoWen(name="庇护", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=0, x_free=True)
    r = _cast(e, actor, x=2, y=1)
    assert r["success"] is True, r
    assert len(e.state.temp_friends) == 2
    for c in e.state.temp_friends:
        assert "庇护" in c.dao_wen, "应继承本体的其他道纹"
        assert "分裂" not in c.dao_wen, "复制体不得持有【分裂】"


def test_monster_can_split_into_enemies():
    """旧版 entity_type != "怪物" 导致怪物永不分裂；现应正常，且复制体进敌方。"""
    e, p, m, actor = _engine(actor_is_monster=True)
    enemies_before = len(e.state.enemies)
    # 怪物侧通过 prepare/resolve 驱动较复杂，这里直接验证即时创生的分支归属
    clones = e.combat._spawn_fenlie_clones(actor, 2, 30)
    assert len(clones) == 2
    assert len(e.state.enemies) == enemies_before + 2, "怪物复制体应加入敌方"
    assert all(c.entity_type == "怪物" for c in clones)
    assert all(c.blood_limit == 30 for c in clones)


def test_no_clone_spawned_on_death_anymore():
    """旧行为废止：[命零]不再凭空产生复制体。"""
    e, p, m, actor = _engine()
    e.state._pending_split_clones = 0      # 已无人设置该字段
    actor.current_hp = 0
    actor.is_alive = False
    e.combat._on_entity_death(actor, ctx={})
    assert len(e.state.temp_friends) == 0, "命零不应再创造复制体"


# ---------------------------------------------------------------- 流血代价（2026-10-08）

def test_bleed_is_refused_when_current_hp_below_cost():
    """边界：付不起（当前生命 < 流血X×10Y）不能发动，不扣血、不创造复制体。

    规则正文：道纹发动必须一次性付清全部代价，付不起不能发动（禁止降X发动）。
    """
    e, p, m, actor = _engine()
    p.current_hp = 59          # 需付 3×10×2 = 60
    r = _cast(e, actor, x=3, y=2)
    assert r["success"] is False, r
    assert "流血" in r.get("error", ""), f"错误信息应指出代价不足：{r.get('error')}"
    assert p.current_hp == 59, "被拒时不得扣血"
    assert len(e.state.temp_friends) == 0, "被拒时不得创造复制体"


def test_bleed_affordability_follows_current_hp_not_blood_limit():
    """边界：可支付性看**当前生命**，不看[血限]——血限高但血见底同样发动不了。"""
    e, p, m, actor = _engine()
    p.blood_limit = 999        # 血限充足
    p.current_hp = 10          # 但只剩 10 点血
    r = _cast(e, actor, x=1, y=1)     # 只需付 10
    assert r["success"] is True, r
    assert p.current_hp == 0, "恰好付得起：60→…；此处 10 付 10 应归零"
    assert p.blood_limit == 999, "流血不扣[血限]"


def test_paying_the_last_hp_kills_caster_and_clones_still_spawn():
    """边界：把最后一点血流光 → 施法者[命零]，但复制体已在代价之后即时生成。

    这是代价从【衰老】改为【流血】的直接后果：衰老只削[血限]（不一定致死），
    流血付的是**当前生命**，付到 0 就是[命零]。顺序＝先付代价、后出效果。
    """
    e, p, m, actor = _engine()
    p.current_hp = 60          # 恰好等于代价 3×10×2
    r = _cast(e, actor, x=3, y=2)
    assert r["success"] is True, r
    assert p.current_hp == 0 and p.is_alive is False, "付光生命应[命零]"
    assert len(e.state.temp_friends) == 3, "代价已付，复制体仍应生成"
