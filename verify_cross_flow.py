# -*- coding: utf-8 -*-
"""跨图打BOSS（图2挂机 → 图1打BOSS → 回图2）自检。

跑法：python verify_cross_flow.py
"""
import sys

import numpy as np

import dungeon_cross as dc

W, H = 200, 120
PT_FARM = (0.20, 0.30)
PT_GO = (0.55, 0.60)
PT_BOSS = (0.70, 0.40)
PT_BACK = (0.45, 0.50)

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def make_frame():
    arr = np.full((H, W, 4), 40, np.uint8)
    arr[..., 3] = 255
    return (W, H, arr.tobytes())


class FakeGame:
    prefix = "[跨图] "

    def __init__(self):
        self.t = 0.0
        self.actions = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, float(seconds))

    def grab(self):
        return make_frame()

    def snapshot(self, tag):
        pass

    def tap(self, name, hold=0.05):
        self.actions.append(("tap", name))
        return True

    def click_norm(self, point, keep_cursor=False):
        self.actions.append(("click", tuple(round(float(v), 3) for v in point)))
        return True

    def release_focus(self):
        pass

    def release_hold(self):
        pass

    def pin_user_foreground(self):
        pass

    def restore_user_foreground(self, quiet=False):
        pass

    def input_mode(self):
        return "hybrid"

    def allow_grab(self):
        return False

    def activate(self):
        return True


def build_profile():
    p = dc.default_profile()
    p["cross"]["farm_points"] = [
        {"name": "A点", "pt": list(PT_FARM), "travel_wait": 1.0},
        {"name": "B点", "pt": [0.25, 0.35], "travel_wait": 2.0},
    ]
    p["cross"]["farm_index"] = 0
    p["cross"]["go_steps"] = [
        {"type": "click", "pt": list(PT_GO), "wait": 1.0, "open_map": True},
        {"type": "key", "key": "esc", "wait": 0.5},
    ]
    p["cross"]["back_steps"] = [
        {"type": "click", "pt": list(PT_BACK), "wait": 1.0, "open_map": True},
    ]
    p["farm"]["boss_pt"] = list(PT_BOSS)
    p["farm"]["travel_wait"] = 1.0
    p["farm"]["boss_present"] = {"roi": None}
    p["farm"]["boss_hp"] = {"roi": None}
    p["farm"]["hp_roi"] = None
    p["farm"]["hp_threshold"] = 0
    p["farm"]["potion_every"] = 0.0
    p["farm"]["potion_timer_enabled"] = False
    p["loop"]["boss_seconds"] = 1.0
    p["loop"]["boss_count"] = 1
    p["loop"]["fatigue_every"] = 10.0
    p["fatigue"] = {"enabled": True, "low": -600, "high": 600,
                    "pt": [0.1, 0.9], "roi": [0.4, 0.05, 0.2, 0.05]}
    p["cross"]["auto_enter"] = False      # 这一份先只测路径本身
    p["cross"]["auto_exit"] = False
    p["retry"] = {"rounds": 1, "gap": 1.0, "max_fail": 1}
    return dc.normalize(p)


# ---- 1) 配置：默认值 / 保存读取 / 缺失检查 --------------------------
p_def = dc.normalize(dc.default_profile())
check("默认有 1 个打怪点", len(p_def["cross"]["farm_points"]) == 1,
      str(p_def["cross"]["farm_points"]))
check("默认路径为空", p_def["cross"]["go_steps"] == []
      and p_def["cross"]["back_steps"] == [])
miss = dc.missing_items(p_def)
check("空标定时列出 8 项缺失（含进本/退本 4 个点位）",
      len(miss) == 8, str(miss))
check("缺失里点名了副本入口 / 进入游戏 / 退出副本 / 确认",
      any("副本入口" in m for m in miss)
      and any("进入游戏" in m for m in miss)
      and any("退出副本" in m for m in miss)
      and any("确认" in m for m in miss), str(miss))

import os
import tempfile
_tmp = os.path.join(tempfile.gettempdir(), "cross_profile_test.json")
try:
    os.remove(_tmp)
except OSError:
    pass
p_save = build_profile()
dc.save_profile(p_save, _tmp)
p_load = dc.load_profile(_tmp)
check("保存/读取后 step 和打怪点还在",
      len(p_load["cross"]["go_steps"]) == 2
      and p_load["cross"]["go_steps"][0]["pt"] == [round(v, 6) for v in PT_GO]
      and len(p_load["cross"]["farm_points"]) == 2,
      str(p_load["cross"]["go_steps"]))
check("填好后不缺东西", dc.missing_items(p_load) == [],
      str(dc.missing_items(p_load)))
try:
    os.remove(_tmp)
except OSError:
    pass

# 坏步骤（没点位/没按键）要被过滤掉
p_bad = dc.normalize({"cross": {
    "farm_points": [{"name": "x", "pt": None}],
    "go_steps": [{"type": "click"}, {"type": "key"}, {"type": "click",
                                                      "pt": [0.1, 0.2]}],
}})
check("坏步骤被过滤掉，只留有效的那步",
      len(p_bad["cross"]["go_steps"]) == 1, str(p_bad["cross"]["go_steps"]))

# ---- 2) 一轮：挂机 → 去程 → BOSS → 回程 → 打怪点 --------------------
g = FakeGame()
p = build_profile()
logs = []
r = dc.CrossRunner(g, p, log=logs.append)
_vals = [0, -700]
_i = {"i": 0}


def _fat(**_kw):
    v = _vals[min(_i["i"], len(_vals) - 1)]
    _i["i"] += 1
    return v, 1000, ""


r.fatigue_check = _fat
ok = r.run()
check("一轮跑完成功", ok == 1, str(ok))

clicks = [a[1] for a in g.actions if a[0] == "click"]
taps = [a[1] for a in g.actions if a[0] == "tap"]
check("先去图2 打怪点（A 点）", clicks and clicks[0] == PT_FARM, str(clicks[:3]))
check("疲劳够了走「去程」点", PT_GO in clicks, str(clicks))
check("到了图1 点 BOSS 点", PT_BOSS in clicks, str(clicks))
check("打完走「回程」点", PT_BACK in clicks, str(clicks))
check("回程后自动回到打怪点",
      clicks.count(PT_FARM) == 2, str(clicks))
check("顺序：打怪点 → 去程 → BOSS → 回程 → 打怪点",
      clicks.index(PT_FARM) < clicks.index(PT_GO)
      < clicks.index(PT_BOSS) < clicks.index(PT_BACK)
      < len(clicks) - 1,
      str(clicks))
check("去程的按键步骤也执行了", "esc" in taps, str(taps[:10]))
check("开地图键按了（打怪点/去程/BOSS/回程/回打怪点）",
      taps.count("m") >= 4, str(taps.count("m")))
check("打完有拾取", taps.count("space") >= 3, str(taps.count("space")))
check("日志写了三个阶段",
      any("图2 → 图1" in l for l in logs)
      and any("进本后：去打 BOSS" in l for l in logs)
      and any("图1 → 图2" in l for l in logs),
      str([l for l in logs if "图" in l][:5]))

# 已经回到打怪点了：下一轮不该再点一次
_before = len([a for a in g.actions if a[0] == "click"])
r._switch_to = None
r.farm_loop(already_there=True)
_after_clicks = [a[1] for a in g.actions if a[0] == "click"][_before:]
check("已经在打怪点时不再重复点它", PT_FARM not in _after_clicks,
      str(_after_clicks))

# ---- 2b) 自动「进入副本 / 退出副本」 -------------------------------
def _profile_with_dungeon():
    p = build_profile()
    p["cross"]["auto_enter"] = True
    p["cross"]["auto_exit"] = True
    p["enter"].update({"dungeon_pt": [0.30, 0.55],
                       "confirm_pt": [0.50, 0.55],
                       "travel_wait": 1.0, "dialog_timeout": 3.0,
                       "load_wait": 1.0})
    p["exit"].update({"exit_pt": [0.94, 0.05], "confirm_pt": [0.55, 0.68],
                      "settle": 1.0})
    p["cross"]["go_steps"] = [{"type": "click", "pt": list(PT_GO),
                              "wait": 1.0, "open_map": True}]
    p["cross"]["back_steps"] = [{"type": "click", "pt": list(PT_BACK),
                                "wait": 1.0, "open_map": True}]
    return dc.normalize(p)


p_dg = _profile_with_dungeon()
check("进本/退本点位齐了 → 不缺东西", dc.missing_items(p_dg) == [],
      str(dc.missing_items(p_dg)))

g3 = FakeGame()
logs3 = []
r3 = dc.CrossRunner(g3, p_dg, log=logs3.append)
steps3 = []
r3.fatigue_check = lambda **_kw: (-700, 1000, "")
r3.enter_dungeon = lambda: steps3.append("enter") or True
r3.exit_dungeon = lambda: steps3.append("exit") or True
ok3 = r3.run_round()
check("自动进本/退本都调用了", steps3 == ["enter", "exit"], str(steps3))
check("顺序：进本在点 BOSS 点之前、退本在回程之前",
      [a[1] for a in g3.actions if a[0] == "click"].index(PT_GO)
      < len([a for a in g3.actions if a[0] == "click"]) - 1,
      str([a[1] for a in g3.actions if a[0] == "click"]))
check("日志写了进本/退本阶段",
      any("自动进入副本" in l for l in logs3)
      and any("自动退出副本" in l for l in logs3),
      str([l for l in logs3 if "副本" in l][:4]))

# 用步骤里的「进本 / 退本」代替自动（不该再重复调用）
p_st = _profile_with_dungeon()
p_st["cross"]["auto_enter"] = False
p_st["cross"]["auto_exit"] = False
p_st["cross"]["go_steps"] = [{"type": "enter", "wait": 0.0},
                             {"type": "click", "pt": list(PT_GO),
                              "wait": 1.0, "open_map": True}]
p_st["cross"]["back_steps"] = [{"type": "exit", "wait": 0.0},
                               {"type": "click", "pt": list(PT_BACK),
                                "wait": 1.0, "open_map": True}]
p_st = dc.normalize(p_st)
check("步骤里的 enter/exit 不会被过滤掉",
      [s.get("type") for s in p_st["cross"]["go_steps"]] == ["enter", "click"]
      and [s.get("type") for s in p_st["cross"]["back_steps"]] == ["exit", "click"],
      str(p_st["cross"]["go_steps"]))
g4 = FakeGame()
logs4 = []
r4 = dc.CrossRunner(g4, p_st, log=logs4.append)
steps4 = []
r4.fatigue_check = lambda **_kw: (-700, 1000, "")
r4.enter_dungeon = lambda: steps4.append("enter") or True
r4.exit_dungeon = lambda: steps4.append("exit") or True
r4.run_round()
check("步骤里的进本/退本按顺序执行",
      steps4 == ["enter", "exit"], str(steps4))
check("步骤版也会打 BOSS 点",
      PT_BOSS in [a[1] for a in g4.actions if a[0] == "click"],
      str([a[1] for a in g4.actions if a[0] == "click"]))

# 进本失败 → 本轮失败
g5 = FakeGame()
r5 = dc.CrossRunner(g5, _profile_with_dungeon(), log=lambda _s: None)
r5.fatigue_check = lambda **_kw: (-700, 1000, "")
r5.enter_dungeon = lambda: False
r5.exit_dungeon = lambda: True
check("进本失败 → 本轮失败", r5.run_round() is False)

# ---- 3) 没标 BOSS 点时给出明确失败 --------------------------------
g2 = FakeGame()
p2 = build_profile()
p2["farm"]["boss_pt"] = None
logs2 = []
r2 = dc.CrossRunner(g2, p2, log=logs2.append)
r2.fatigue_check = lambda **_kw: (-700, 1000, "")   # 直接低于下限 → 去图1
ok2 = r2.run_round()
check("没标 BOSS 点 → 本轮失败", ok2 is False)
check("失败日志说清楚了", any("BOSS" in l for l in logs2[-6:]),
      str(logs2[-6:]))

print()
if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("PASS: 跨图打BOSS 流程自检通过")
