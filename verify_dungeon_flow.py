# -*- coding: utf-8 -*-
"""验证副本引擎：进本弹窗 / 退出弹窗 分开识别 + 重试 + 疲劳

用假窗口 + 虚拟时钟。跑法：python verify_dungeon_flow.py
"""
import sys
import threading

import numpy as np

import creature_curios_keyer as app
import dungeon_flow as df

W, H = 200, 120
PT_DUNGEON = (0.30, 0.55)
PT_CONFIRM = (0.50, 0.55)
PT_BOSS = (0.70, 0.40)
PT_EXIT = (0.94, 0.05)
PT_EXIT_OK = (0.55, 0.68)
PT_FATIGUE = (0.10, 0.90)
ROI_ENTER = [0.32, 0.20, 0.36, 0.16]   # 进本弹窗：x 64..136, y 24..43
ROI_EXIT = [0.32, 0.60, 0.36, 0.16]    # 退出弹窗：x 64..136, y 72..91


def make_frame(enter_dialog=False, exit_dialog=False):
    arr = np.full((H, W, 4), 40, np.uint8)
    arr[..., 3] = 255
    if enter_dialog:
        arr[26:41, 70:130] = [60, 120, 220, 255]
    if exit_dialog:
        arr[74:89, 70:130] = [220, 90, 60, 255]
    return (W, H, arr.tobytes())


class FakeGame:
    def __init__(self, enter_appears=True, exit_appears=True):
        self.t = 0.0
        self.actions = []
        self.last_error = ""
        self.state = {"enter": False, "exit": False}
        self.enter_appears = enter_appears
        self.exit_appears = exit_appears
        self.enter_popup_at = None
        self.confirm_at = None
        self.confirm_count = 0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, float(seconds))

    def grab(self):
        return make_frame(enter_dialog=self.state["enter"],
                          exit_dialog=self.state["exit"])

    def snapshot(self, tag):
        self.actions.append(("snap", tag))

    def click_norm(self, point):
        pt = tuple(round(float(v), 3) for v in point)
        self.actions.append(("click", pt))
        if pt == PT_DUNGEON and self.enter_appears:
            self.state["enter"] = True
            self.enter_popup_at = self.t
        elif pt == PT_CONFIRM or pt == PT_EXIT_OK:
            if pt == PT_CONFIRM:
                self.state["enter"] = False
            if pt == PT_EXIT_OK:
                if self.confirm_at is None:
                    self.confirm_at = self.t
                self.confirm_count += 1
                self.state["exit"] = False
        elif pt == PT_EXIT and self.exit_appears:
            self.state["exit"] = True
        return True

    def tap(self, name):
        self.actions.append(("tap", name))
        return True


def build_profile(**over):
    enter_sig = df.signature(df.crop_norm(make_frame(enter_dialog=True), ROI_ENTER), 12)
    exit_sig = df.signature(df.crop_norm(make_frame(exit_dialog=True), ROI_EXIT), 12)
    p = df._merge(df.DEFAULT_PROFILE, {
        "keys": {"open_map": "m", "pickup": "space"},
        "enter": {
            "dungeon_pt": list(PT_DUNGEON),
            "travel_wait": 1.0,
            "dialog": {"roi": ROI_ENTER, "sig": enter_sig, "grid": 12, "sim": 0.90},
            "confirm_pt": list(PT_CONFIRM),
            "dialog_timeout": 5.0,
            "load_wait": 2.0,
            "retry": 2,
        },
        "farm": {
            "boss_pt": list(PT_BOSS),
            "travel_wait": 1.0,
            "seconds": 30.0,
            "interval_ms": 100,
            "use_skills": True,
            "skill1_every": 12.0,
            "skill2_every": 6.0,
            "hp_roi": [0.02, 0.02, 0.10, 0.04],
            "hp_threshold": 1600,
            "hp_check_every": 2.0,
            "potion_keys": ["q", "w"],
            "potion_every": 6.0,
            "pickup_every": 8.0,
        },
        "exit": {
            "exit_pt": list(PT_EXIT),
            "confirm_pt": list(PT_EXIT_OK),
            "dialog": {"roi": ROI_EXIT, "sig": exit_sig, "grid": 12, "sim": 0.90},
            "dialog_timeout": 4.0,
            "retry": 2,
            "exit_wait": 0.5,
            "confirm_wait": 0.5,
            "settle": 2.0,
        },
        "fatigue": {
            "enabled": True,
            "pt": list(PT_FATIGUE),
            "roi": [0.4, 0.05, 0.2, 0.05],
            "threshold": 800,
            "farm_seconds": 12.0,
        },
        "detect": {"poll": 0.5},
        "retry": {"rounds": 1, "gap": 1.0, "max_fail": 2},
    })
    return df._merge(p, over)


fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


# ---- 1) 两个弹窗的锚点互不干扰 ----------------------------------------
p = build_profile()
game = FakeGame()
runner = df.DungeonRunner(game, p, log=lambda _s: None)
f_enter_only = make_frame(enter_dialog=True)
f_exit_only = make_frame(exit_dialog=True)
f_none = make_frame()

check("只有进本弹窗时：进本=True", runner.anchor_visible(runner.enter_dialog_spec(), f_enter_only))
check("只有进本弹窗时：退出=False", not runner.anchor_visible(runner.exit_dialog_spec(), f_enter_only))
check("只有退出弹窗时：退出=True", runner.anchor_visible(runner.exit_dialog_spec(), f_exit_only))
check("只有退出弹窗时：进本=False", not runner.anchor_visible(runner.enter_dialog_spec(), f_exit_only))
check("都没有时两个都=False",
      not runner.dialog_visible(f_none) and not runner.exit_dialog_visible(f_none))
check("两份区域不重合",
      ROI_ENTER != ROI_EXIT
      and p["enter"]["dialog"]["roi"] != p["exit"]["dialog"]["roi"])
check("两份特征不共用",
      p["enter"]["dialog"]["sig"] != p["exit"]["dialog"]["sig"])

# ---- 2) 完整一轮：进本识别 → 挂机 → 退出识别 → 确认 ------------------
game = FakeGame()
logs = []
runner = df.DungeonRunner(game, build_profile(), log=logs.append)
runner.hp_now = lambda: 1000
_real_ocr = app.ocr_fatigue_from_bgra
app.ocr_fatigue_from_bgra = lambda data, w, h, roi: (900, 1000, "当前疲劳值 900/1000")
try:
    ok = runner.run_round()
finally:
    app.ocr_fatigue_from_bgra = _real_ocr

taps = [a[1] for a in game.actions if a[0] == "tap"]
clicks = [a[1] for a in game.actions if a[0] == "click"]
check("一轮跑完=成功", ok)
check("点过副本入口 / 进入游戏", PT_DUNGEON in clicks and PT_CONFIRM in clicks)
check("点过 BOSS 点", PT_BOSS in clicks)
check("退出：先点退出按钮再点确认",
      clicks.index(PT_EXIT) < clicks.index(PT_EXIT_OK))
check("确认是在「退出确认弹窗」出现之后才点的",
      game.enter_popup_at is not None and game.confirm_at is not None
      and game.confirm_at >= game.enter_popup_at)
check("识别到退出确认弹窗的日志", any("识别到退出确认弹窗" in l for l in logs), str(logs[-6:]))
check("退出只点了一次确认", game.confirm_count == 1, str(game.confirm_count))
check("打过平A / 技能 / 拾取", "a" in taps and "1" in taps and "space" in taps)
check("血量低会喝药", "q" in taps and "w" in taps)
check("点过疲劳图标", PT_FATIGUE in clicks)

# ---- 3) 退出弹窗一直不出现 → 重点退出按钮 → 判失败 --------------------
game2 = FakeGame(exit_appears=False)
logs2 = []
runner2 = df.DungeonRunner(
    game2, build_profile(exit={"retry": 2, "dialog_timeout": 3.0}),
    log=logs2.append,
)
runner2.hp_now = lambda: 1000
runner2.fatigue_check = lambda: (900, 1000, "")
ok2 = runner2.run_round()
clicks2 = [a[1] for a in game2.actions if a[0] == "click"]
check("退出弹窗没出现：点退出按钮 2 次（重试）",
      clicks2.count(PT_EXIT) == 2, str(clicks2.count(PT_EXIT)))
check("退出弹窗没出现：不会瞎点确认", PT_EXIT_OK not in clicks2)
check("退出弹窗没出现：最终判本轮失败", ok2 is False)
check("退出弹窗没出现：有重试日志", any("再点一次「退出副本」" in l for l in logs2))

# ---- 4) 没框退出弹窗区域 → 盲点确认（兼容老标定）---------------------
p4 = build_profile()
p4["exit"]["dialog"] = {"roi": None, "sig": [], "grid": 12, "sim": 0.9}
game3 = FakeGame(exit_appears=False)
logs3 = []
runner3 = df.DungeonRunner(game3, p4, log=logs3.append)
runner3.hp_now = lambda: 1000
runner3.fatigue_check = lambda: (900, 1000, "")
ok3 = runner3.run_round()
clicks3 = [a[1] for a in game3.actions if a[0] == "click"]
check("没框退出弹窗：直接盲点确认一次",
      ok3 and clicks3.count(PT_EXIT_OK) == 1, str(clicks3))
check("没框退出弹窗：日志说明是盲点",
      any("盲点" in l for l in logs3))

# ---- 5) 没框进本弹窗区域 → 盲点进入游戏（与退出互不影响）-------------
p5 = build_profile()
p5["enter"]["dialog"] = {"roi": None, "sig": [], "grid": 12, "sim": 0.9}
game4 = FakeGame(enter_appears=False)
runner4 = df.DungeonRunner(game4, p5, log=lambda _s: None)
runner4.hp_now = lambda: 1000
runner4.fatigue_check = lambda: (900, 1000, "")
ok4 = runner4.run_round()
clicks4 = [a[1] for a in game4.actions if a[0] == "click"]
check("没框进本弹窗：进本盲点一次、退出仍然识别",
      ok4 and clicks4.count(PT_CONFIRM) == 1 and clicks4.count(PT_EXIT_OK) == 1,
      str(clicks4))

# ---- 6) 疲劳：不足时普通打怪；充足时直接下一轮 -----------------------
game5 = FakeGame()
logs5 = []
runner5 = df.DungeonRunner(game5, build_profile(), log=logs5.append)
runner5.hp_now = lambda: 5000
app.ocr_fatigue_from_bgra = lambda data, w, h, roi: (300, 1000, "当前疲劳值 300/1000")
try:
    runner5.run_round()
finally:
    app.ocr_fatigue_from_bgra = _real_ocr
taps5 = [a[1] for a in game5.actions if a[0] == "tap"]
check("疲劳不足 → 普通挂机打怪", any("疲劳不足" in l for l in logs5))
check("普通挂机真的在按 A", taps5.count("a") > 250, str(taps5.count("a")))

# ---- 7) 多开秒数独立 / 停止 ------------------------------------------
a = df.copy_profile_for_slot(build_profile(), 45)
b = df.copy_profile_for_slot(build_profile(), 300)
check("多开秒数互不影响", a["farm"]["seconds"] == 45.0 and b["farm"]["seconds"] == 300.0)

stop = threading.Event()
stop.set()
game6 = FakeGame()
runner6 = df.DungeonRunner(game6, build_profile(), log=lambda _s: None, stop_event=stop)
runner6.run_round()
check("停止事件立即生效", game6.t < 10.0, f"t={game6.t:.1f}")

# ---- 8) 停止能打断长等待（这是「点停止没反应」的根因）----
import time as _time

_g = df.WinGame(0, {"prime_activate": False}, log=lambda _s: None)
_t0 = _time.monotonic()
_g.sleep(0.3)
_took = _time.monotonic() - _t0
check("普通 sleep 时间正常", _took >= 0.25, f"{_took:.2f}s")

_ev = threading.Event()
_ev.set()
_g2 = df.WinGame(0, {"prime_activate": False}, log=lambda _s: None, stop_event=_ev)
_t0 = _time.monotonic()
_g2.sleep(5.0)
_took = _time.monotonic() - _t0
check("停止能立刻打断 5 秒等待", _took < 0.3, f"{_took:.2f}s")

check("默认开启焦点伪装（否则游戏忽略后台按键）",
      df.DEFAULT_PROFILE.get("prime_activate") is True)

# ---- 9) 副本方案：命名保存 / 切换 / 删除 ------------------------------
import os as _os
import tempfile as _tempfile

_tmp = _tempfile.mkdtemp(prefix="dungeon_profiles_")
_real_base = df._base_dir
df._base_dir = lambda: _tmp
try:
    _p1 = df._merge(df.DEFAULT_PROFILE, {"name": "甲"})
    _p1["enter"]["dungeon_pt"] = [0.11, 0.22]
    _p2 = df._merge(df.DEFAULT_PROFILE, {"name": "乙"})
    _p2["enter"]["dungeon_pt"] = [0.77, 0.88]
    df.save_dungeon("方案甲", _p1)
    df.save_dungeon("方案乙", _p2)
    check("方案列表正确", df.list_dungeons() == ["方案乙", "方案甲"] or
          sorted(df.list_dungeons()) == ["方案甲", "方案乙"], str(df.list_dungeons()))
    check("最后保存的方案成为当前方案", df.active_name() == "方案乙")
    check("load_profile 默认读当前方案",
          df.load_profile()["enter"]["dungeon_pt"] == [0.77, 0.88])
    check("按名字读方案", df.load_dungeon("方案甲")["enter"]["dungeon_pt"] == [0.11, 0.22])
    df.set_active("方案甲")
    check("切换方案生效", df.load_profile()["enter"]["dungeon_pt"] == [0.11, 0.22])
    check("名字里的非法字符被清洗",
          _os.path.basename(df.dungeon_file("a/b:c*d")) == "abcd.json",
          df.dungeon_file("a/b:c*d"))
    check("删除方案", df.delete_dungeon("方案乙") and
          df.list_dungeons() == ["方案甲"], str(df.list_dungeons()))
    check("删除不存在的方案返回 False", df.delete_dungeon("没有这个") is False)
finally:
    df._base_dir = _real_base

print()
if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("PASS: 副本引擎（进本/退出弹窗分开识别 + 重试 + 疲劳）全部通过")
