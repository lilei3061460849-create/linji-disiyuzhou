# 引擎直测：封印(移出)/再生灌敌(癌变)/逼债(还债) 三种非伤害手段的可发动性
import sys
sys.path.insert(0, ".")
from tests.setup_support import *  # noqa
from tests.test_final_duel import _new_candidate  # noqa
from engine.models import DaoWen, DaoWenInstance, Entity  # noqa


def grant(player, *names):
    for nm in names:
        player.dao_wen[nm] = DaoWenInstance(
            DaoWen(name=nm, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def fresh_battle(db):
    e = _new_candidate(db, "/tmp/dbg_sealed.json", name="测试者")
    e.state.current_battle = 1
    e.state.phase = "in_combat"
    e.state.enemies.clear()
    e.state.enemies.append(Entity(name="杂兵A", entity_type="怪物", blood_limit=40,
                                  current_hp=40, attack_power=5, attack_count=2,
                                  speed_limit=6, current_speed=6))
    e.state.enemies.append(Entity(name="杂兵B", entity_type="怪物", blood_limit=40,
                                  current_hp=40, attack_power=5, attack_count=2,
                                  speed_limit=6, current_speed=6))
    return e


# 1) 封印
e = fresh_battle("dbg_seal")
grant(e.state.player, "封印")
r = e.execute_action("use_daowen", {"daowen_name": "封印", "x": 1, "target": "杂兵A"})
print("封印X1 →", r.get("success"), r.get("error") or r.get("result", {}))
print("  敌存活:", [x.name for x in e.state.enemies if x.is_alive], "玩家异变:", getattr(e.state.player, "mutation", "?"))

# 2) 再生灌敌（癌变前置）
e = fresh_battle("dbg_feed")
grant(e.state.player, "再生")
for i in range(6):
    r = e.execute_action("use_daowen", {"daowen_name": "再生", "x": 9, "target": "杂兵A"})
    if not r.get("success"):
        print(f"再生灌敌 第{i+1}次 → 失败: {r.get('error')}")
        break
else:
    pass
en = e.state.enemies[0]
print("再生灌敌 → success", r.get("success"), "｜目标 total_healed:", en.total_healed,
      "癌变阈值:", 2 * en.blood_limit, "｜存活:", en.is_alive, "｜player is_alive:", e.state.player.is_alive)
print("  （若引擎结算癌变，应看到 is_alive=False 或被吸收）")

# 3) 逼债（还债前置：负债≥10）
e = fresh_battle("dbg_debt")
grant(e.state.player, "逼债")
r = e.execute_action("use_daowen", {"daowen_name": "逼债", "x": 12, "target": "杂兵A"})
en = e.state.enemies[0]
print("逼债X12 →", r.get("success"), r.get("error") or "", "｜目标负债:", getattr(en, "debt", "?"), "｜存活:", en.is_alive)
print("  场上敌:", [(x.name, x.is_alive) for x in e.state.enemies], "员工:", [x.name for x in e.state.employees])
