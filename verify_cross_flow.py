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
    p["cross"]["close_map_after_click"] = True
    p["cross"]["close_key"] = "esc"
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
check("空标定时列出 9~10 项缺失（本机若主界面框过疲劳区域会少一项）",
      len(miss) in (9, 10), str(miss))
check("缺失里点名了疲劳图标", any("疲劳图标" in m for m in miss), str(miss))

# 本机没框过疲劳区域时，也要提醒去框
_real_roi_loader = dc.df.app.load_fatigue_roi_norm
dc.df.app.load_fatigue_roi_norm = lambda: None
try:
    _miss_roi = dc.missing_items(p_def)
finally:
    dc.df.app.load_fatigue_roi_norm = _real_roi_loader
check("没有疲劳区域时会提醒去框",
      any("疲劳提示区域" in m for m in _miss_roi), str(_miss_roi))
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

# ---- 2c) 点完地图点位要关地图 -------------------------------------
g6 = FakeGame()
p6 = build_profile()
r6 = dc.CrossRunner(g6, p6, log=lambda _s: None)
r6.run_steps([{"type": "click", "pt": [0.3, 0.4], "wait": 0.0,
               "open_map": True, "close_map": True}], "去程")
_taps6 = [a[1] for a in g6.actions if a[0] == "tap"]
check("点完点位会按关图键（esc）", _taps6 == ["m", "esc"], str(_taps6))

g7 = FakeGame()
p7 = build_profile()
p7["cross"]["close_map_after_click"] = False
r7 = dc.CrossRunner(g7, p7, log=lambda _s: None)
r7.run_steps([{"type": "click", "pt": [0.3, 0.4], "wait": 0.0,
               "open_map": True, "close_map": True}], "去程")
check("关掉「点完关地图」后不再按 esc",
      [a[1] for a in g7.actions if a[0] == "tap"] == ["m"],
      str([a[1] for a in g7.actions if a[0] == "tap"]))

# 走打怪点 / 打 BOSS 点（基类 _goto）也会关地图
g8 = FakeGame()
r8 = dc.CrossRunner(g8, build_profile(), log=lambda _s: None)
r8._goto([0.3, 0.4], 0.2, "测试点")
_taps8 = [a[1] for a in g8.actions if a[0] == "tap"]
check("走地图点位（_goto）后也会关地图", _taps8 == ["m", "esc"], str(_taps8))

# ---- 2d) 去程 / 回程 试运行 ----------------------------------------
p_tg = _profile_with_dungeon()
g_tg = FakeGame()
logs_tg = []
r_tg = dc.CrossRunner(g_tg, p_tg, log=logs_tg.append)
steps_tg = []
r_tg.enter_dungeon = lambda: steps_tg.append("enter") or True
r_tg.fatigue_check = lambda **_kw: (-700, 1000, "")
ok_tg = r_tg.test_path("go")
_clicks_tg = [a[1] for a in g_tg.actions if a[0] == "click"]
check("试跑去程成功", ok_tg is True)
check("试跑去程：走了去程点 + 自动进本，但没打 BOSS",
      PT_GO in _clicks_tg and PT_BOSS not in _clicks_tg
      and steps_tg == ["enter"], str(_clicks_tg))
check("试跑去程日志写明不打怪",
      any("不自动打怪" in l for l in logs_tg), str(logs_tg[-3:]))

g_tb = FakeGame()
logs_tb = []
r_tb = dc.CrossRunner(g_tb, p_tg, log=logs_tb.append)
steps_tb = []
r_tb.exit_dungeon = lambda: steps_tb.append("exit") or True
ok_tb = r_tb.test_path("back")
_clicks_tb = [a[1] for a in g_tb.actions if a[0] == "click"]
check("试跑回程成功", ok_tb is True)
check("试跑回程：先自动退本，再走回程点，最后回打怪点",
      steps_tb == ["exit"] and PT_BACK in _clicks_tb
      and PT_FARM in _clicks_tb, str(_clicks_tb))
check("试跑回程日志写明回到打怪点",
      any("试跑回程完成" in l for l in logs_tb), str(logs_tb[-3:]))

# ---- 2e) 鼠标宏：真实鼠标回放 + 优先于步骤 --------------------------
p_mac = build_profile()
p_mac["cross"]["go_macro"] = {"speed": 1.0, "meta": {}, "events": [
    {"type": "key_down", "t": 0.0, "name": "m"},
    {"type": "down", "t": 0.2, "nx": 0.33, "ny": 0.44},
]}
p_mac["cross"]["back_macro"] = {"speed": 1.0, "meta": {}, "events": [
    {"type": "down", "t": 0.0, "nx": 0.66, "ny": 0.77},
]}
p_mac = dc.normalize(p_mac)
check("宏事件被 normalize 保留",
      len(p_mac["cross"]["go_macro"]["events"]) == 2
      and len(p_mac["cross"]["back_macro"]["events"]) == 1,
      str(p_mac["cross"]["go_macro"]))
check("录了宏就不算缺去程/回程路径",
      not any("去程路径" in m for m in dc.missing_items(p_mac))
      and not any("回程路径" in m for m in dc.missing_items(p_mac)),
      str(dc.missing_items(p_mac)))

g9 = FakeGame()
logs9 = []
r9 = dc.CrossRunner(g9, p_mac, log=logs9.append)
forc = {"during": None}
_orig_click = g9.click_norm


def _spy_click(point, keep_cursor=False):
    forc["during"] = getattr(g9, "force_grab", None)
    return _orig_click(point, keep_cursor)


g9.click_norm = _spy_click
ok9 = r9.play_macro("go")
check("宏回放成功", ok9 is True)
check("宏回放时强制真实鼠标（force_grab=True）", forc["during"] is True,
      str(forc["during"]))
check("宏回放完恢复 force_grab", bool(getattr(g9, "force_grab", False)) is False)
check("宏里的点击/按键都执行了",
      ("tap", "m") in g9.actions
      and ("click", (0.33, 0.44)) in g9.actions, str(g9.actions[:6]))

# 有宏时一轮不再走步骤点位
g10 = FakeGame()
r10 = dc.CrossRunner(g10, p_mac, log=lambda _s: None)
r10.fatigue_check = lambda **_kw: (-700, 1000, "")
r10.run_round()
_clicks10 = [a[1] for a in g10.actions if a[0] == "click"]
check("有宏时去程走宏点（不点步骤里的 PT_GO）",
      (0.33, 0.44) in _clicks10 and PT_GO not in _clicks10, str(_clicks10))

# ---- 2f) 骑乘：去程/回程前都会检查 --------------------------------
g11 = FakeGame()
r11 = dc.CrossRunner(g11, _profile_with_dungeon(), log=lambda _s: None)
r11.fatigue_check = lambda **_kw: (-700, 1000, "")
mo = []
r11.ensure_mount = lambda: mo.append("mount")
r11.enter_dungeon = lambda: True
r11.exit_dungeon = lambda: True
r11.run_round()
check("一轮里至少检查两次骑乘（去程前 + 回程前）", len(mo) >= 2, str(len(mo)))

# 骑乘/疲劳标定自动沿用副本流程那份
_real_load = dc.df.load_profile
dc.df.load_profile = lambda *a, **k: {
    "mount": {"enabled": True, "key": "s",
              "dialog": {"roi": [0.1, 0.1, 0.2, 0.2], "sig": [1] * 144},
              "text_roi": [0.1, 0.1, 0.2, 0.2],
              "mount_pt": [0.3, 0.3]},
    "fatigue": {"pt": [0.5, 0.9], "roi": [0.4, 0.05, 0.2, 0.05]},
}
try:
    p_in = dc._inherit_from_dungeon(dc.default_profile())
finally:
    dc.df.load_profile = _real_load
check("骑乘标定没标时沿用副本流程的",
      bool((p_in.get("mount") or {}).get("mount_pt"))
      and bool((p_in.get("mount") or {}).get("text_roi")),
      str(p_in.get("mount")))
check("疲劳图标/区域也沿用",
      (p_in.get("fatigue") or {}).get("pt") == [0.5, 0.9]
      and bool((p_in.get("fatigue") or {}).get("roi")),
      str(p_in.get("fatigue")))

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
