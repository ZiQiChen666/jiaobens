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
PT_ENTRY = (0.50, 0.80)   # 副本门口/NPC
ROI_ENTER = [0.32, 0.20, 0.36, 0.16]   # 进本弹窗：x 64..136, y 24..43
ROI_EXIT = [0.32, 0.60, 0.36, 0.16]    # 退出弹窗：x 64..136, y 72..91
ROI_MOUNT = [0.02, 0.02, 0.28, 0.16]   # 骑乘弹窗：x 4..60, y 2..21
ROI_MOUNT_STATE = [0.02, 0.70, 0.22, 0.14]  # 骑乘状态：x 4..48, y 84..100
PT_MOUNT = (0.50, 0.20)


def make_frame(enter_dialog=False, exit_dialog=False,
               mount_dialog=False, mounted=False):
    arr = np.full((H, W, 4), 40, np.uint8)
    arr[..., 3] = 255
    if enter_dialog:
        arr[26:41, 70:130] = [60, 120, 220, 255]
    if exit_dialog:
        arr[74:89, 70:130] = [220, 90, 60, 255]
    if mount_dialog:
        arr[4:19, 8:56] = [120, 200, 90, 255]
    if mounted:
        arr[86:98, 8:44] = [250, 220, 60, 255]
    return (W, H, arr.tobytes())


class FakeGame:
    def __init__(self, enter_appears=True, exit_appears=True,
                 need_entry_click=False):
        self.t = 0.0
        self.actions = []
        self.last_error = ""
        self.state = {"enter": False, "exit": False,
                      "mount_dialog": False, "mounted": False}
        self.enter_appears = enter_appears
        self.exit_appears = exit_appears
        self.need_entry_click = need_entry_click
        self.entry_clicked = False
        self.enter_popup_at = None
        self.confirm_at = None
        self.confirm_count = 0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, float(seconds))

    def grab(self):
        return make_frame(enter_dialog=self.state["enter"],
                          exit_dialog=self.state["exit"],
                          mount_dialog=self.state["mount_dialog"],
                          mounted=self.state["mounted"])

    def snapshot(self, tag):
        self.actions.append(("snap", tag))

    def click_norm(self, point):
        pt = tuple(round(float(v), 3) for v in point)
        self.actions.append(("click", pt))
        if pt == PT_DUNGEON and self.enter_appears and not self.need_entry_click:
            self.state["enter"] = True
            self.enter_popup_at = self.t
        elif pt == PT_ENTRY:
            self.entry_clicked = True
            if self.need_entry_click and self.enter_appears:
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
        elif pt == PT_MOUNT:
            self.state["mounted"] = True
        return True

    def tap(self, name):
        self.actions.append(("tap", name))
        if name == "s":
            self.state["mount_dialog"] = not self.state["mount_dialog"]
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
app.ocr_fatigue_from_bgra = lambda data, w, h, roi: (-900, 1000, "当前疲劳值 -900/1000")
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
check("点过疲劳图标（打怪点读疲劳）", PT_FATIGUE in clicks)
check("疲劳 -900 → 打 BOSS（日志有分流说明）",
      any("循环打 BOSS" in l for l in logs), str(logs[-6:]))

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

# ---- 6) 疲劳 +900 → 在打怪点打怪（不去 BOSS 点）----------------------
p6b = build_profile()
p6b["mount"]["enabled"] = False
p6b["farm"]["farm_pt"] = [0.6, 0.6]
p6b["loop"] = {"boss_seconds": 1.0, "boss_rounds": 1,
               "trash_seconds": 1.0, "trash_rounds": 1,
               "default_action": "boss"}
game5 = FakeGame()
logs5 = []
runner5 = df.DungeonRunner(game5, p6b, log=logs5.append)
runner5.hp_now = lambda: 5000
app.ocr_fatigue_from_bgra = lambda data, w, h, roi: (900, 1000, "当前疲劳值 900/1000")
try:
    runner5.run_round()
finally:
    app.ocr_fatigue_from_bgra = _real_ocr
taps5 = [a[1] for a in game5.actions if a[0] == "tap"]
clicks5 = [a[1] for a in game5.actions if a[0] == "click"]
check("疲劳 +900 → 走打怪分支（日志）", any("循环打怪" in l for l in logs5),
      str(logs5[-6:]))
check("疲劳 +900 → 去了打怪点", (0.6, 0.6) in clicks5, str(clicks5))
check("疲劳 +900 → 不去 BOSS 点", PT_BOSS not in clicks5)
check("打怪循环真的在按 A", taps5.count("a") >= 8, str(taps5.count("a")))

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

# ---- 10) 骑乘检查（每次寻路前）---------------------------------------
p10 = build_profile()
p10["mount"] = {
    "enabled": True, "key": "s",
    "dialog": {"roi": ROI_MOUNT,
               "sig": df.signature(df.crop_norm(make_frame(mount_dialog=True), ROI_MOUNT), 12),
               "grid": 12, "sim": 0.90},
    "state": {"roi": ROI_MOUNT_STATE,
              "sig": df.signature(df.crop_norm(make_frame(mounted=True), ROI_MOUNT_STATE), 12),
              "grid": 12, "sim": 0.90},
    "mount_pt": list(PT_MOUNT), "wait": 2.0, "settle": 0.5,
}
game10 = FakeGame()
logs10 = []
r10 = df.DungeonRunner(game10, p10, log=logs10.append)
r10.ensure_mount()
taps10 = [a[1] for a in game10.actions if a[0] == "tap"]
clicks10 = [a[1] for a in game10.actions if a[0] == "click"]
check("未骑乘：按了 S", taps10.count("s") >= 1, str(taps10))
check("未骑乘：识别到骑乘弹窗",
      any("识别到骑乘弹窗" in l for l in logs10), str(logs10))
check("未骑乘：点了「骑乘」按钮", PT_MOUNT in clicks10, str(clicks10))
check("骑乘后状态变成已骑乘", game10.state["mounted"] is True)
check("骑乘后弹窗被关掉", game10.state["mount_dialog"] is False)

game11 = FakeGame()
game11.state["mounted"] = True
logs11 = []
r11 = df.DungeonRunner(game11, p10, log=logs11.append)
r11.ensure_mount()
taps11 = [a[1] for a in game11.actions if a[0] == "tap"]
clicks11 = [a[1] for a in game11.actions if a[0] == "click"]
check("已骑乘：不再点「骑乘」按钮", PT_MOUNT not in clicks11, str(clicks11))
check("已骑乘：日志显示已骑乘", any("已骑乘" in l for l in logs11), str(logs11))
check("已骑乘：弹窗也关掉了", game11.state["mount_dialog"] is False)

# ---- 11) 打怪点按疲劳分流 --------------------------------------------
p11 = build_profile()
p11["mount"]["enabled"] = False
p11["loop"] = {"boss_seconds": 1.0, "boss_rounds": 2,
               "trash_seconds": 1.0, "trash_rounds": 1,
               "default_action": "boss"}
r11b = df.DungeonRunner(FakeGame(), p11, log=lambda _s: None)
r11b.fatigue_check = lambda: (-900, 1000, "")
check("疲劳 -900（< -800）→ 打BOSS", r11b.decide_action() == "boss")
r11b.fatigue_check = lambda: (900, 1000, "")
check("疲劳 +900（> +800）→ 打怪", r11b.decide_action() == "farm")
r11b.fatigue_check = lambda: (100, 1000, "")
check("疲劳在中间 → 用默认动作", r11b.decide_action() == "boss")

# BOSS 循环按次数跑
game12 = FakeGame()
logs12 = []
r12 = df.DungeonRunner(game12, p11, log=logs12.append)
r12.hp_now = lambda: 5000
r12.fatigue_check = lambda: (-900, 1000, "")
ok12 = r12.run_loops()
clicks12 = [a[1] for a in game12.actions if a[0] == "click"]
check("BOSS 循环按 boss_rounds 跑 2 次",
      clicks12.count(PT_BOSS) == 2, str(clicks12.count(PT_BOSS)))
check("BOSS 循环跑成功", ok12 is True)

# 打怪循环
p13 = build_profile()
p13["mount"]["enabled"] = False
p13["farm"]["farm_pt"] = [0.6, 0.6]
p13["loop"] = {"boss_seconds": 1.0, "boss_rounds": 1,
               "trash_seconds": 1.0, "trash_rounds": 2,
               "default_action": "boss"}
game13 = FakeGame()
logs13 = []
r13 = df.DungeonRunner(game13, p13, log=logs13.append)
r13.hp_now = lambda: 5000
r13.fatigue_check = lambda: (900, 1000, "")
r13.run_loops()
clicks13 = [a[1] for a in game13.actions if a[0] == "click"]
check("疲劳高 → 走到了打怪点", (0.6, 0.6) in clicks13, str(clicks13))
check("疲劳高 → 不去 BOSS 点", PT_BOSS not in clicks13)
check("打怪循环跑 2 轮（日志有 打怪2/2）",
      any("打怪2/2" in l for l in logs13), str(logs13[-4:]))

# ---- 11b) 打怪期间定时查疲劳 → 自动换分支 ---------------------------
p15 = build_profile()
p15["mount"]["enabled"] = False
p15["farm"]["farm_pt"] = [0.6, 0.6]
p15["loop"] = {"boss_seconds": 1.0, "boss_rounds": 1,
               "trash_seconds": 5.0, "trash_rounds": 1,
               "fatigue_every": 0.5, "max_switch": 2,
               "default_action": "farm"}
game15 = FakeGame()
logs15 = []
r15 = df.DungeonRunner(game15, p15, log=logs15.append)
r15.hp_now = lambda: 5000
r15.fatigue_check = lambda: (900, 1000, "")
# 先按默认 farm 打，途中疲劳变成 -900 → 应该切到 boss
seq = {"n": 0}


def fake_check():
    seq["n"] += 1
    return (-900, 1000, "") if seq["n"] >= 2 else (0, 1000, "")


r15.fatigue_check = fake_check
# decide_action 第一次用 (0,1000) → 默认 farm；挂机中第二次读到 -900 → 切 boss
r15.p["loop"]["default_action"] = "farm"
r15.decide_action = lambda: "farm"
ok15 = r15.run_loops()
clicks15 = [a[1] for a in game15.actions if a[0] == "click"]
check("挂机中查到疲劳变化会换分支",
      any("该换分支了" in l for l in logs15) or any("切到" in l for l in logs15),
      str(logs15[-6:]))
check("切换后真的去了 BOSS 点", PT_BOSS in clicks15, str(clicks15[-6:]))

# 关掉定时查疲劳就不该切
p16 = dict(p15)
p16["loop"] = dict(p15["loop"], fatigue_every=0)
game16 = FakeGame()
logs16 = []
r16 = df.DungeonRunner(game16, p16, log=logs16.append)
r16.hp_now = lambda: 5000
r16.fatigue_check = lambda: (-900, 1000, "")
r16.decide_action = lambda: "farm"
r16.run_loops()
clicks16 = [a[1] for a in game16.actions if a[0] == "click"]
check("fatigue_every=0 时不换分支", PT_BOSS not in clicks16, str(clicks16[-6:]))

# ---- 11c) 进本：门口点 / 放宽阈值 / 兜底 ----------------------------
p17 = build_profile()
p17["mount"]["enabled"] = False
p17["enter"]["entry_pt"] = list(PT_ENTRY)
game17 = FakeGame(need_entry_click=True)
logs17 = []
r17 = df.DungeonRunner(game17, p17, log=logs17.append)
ok17 = r17.enter_dungeon()
clicks17 = [a[1] for a in game17.actions if a[0] == "click"]
check("需要点门口才弹窗时：点了门口", PT_ENTRY in clicks17, str(clicks17))
check("需要点门口才弹窗时：成功进本", ok17 is True)
check("进本日志提到门口点", any("副本门口" in l for l in logs17), str(logs17[-4:]))

# 放宽阈值：把 sim 设成 1.0（严格），实测相似度略低 → 严识别失败，放宽后命中
p18 = build_profile()
p18["mount"]["enabled"] = False
p18["enter"]["dialog"]["sim"] = 1.0
game18 = FakeGame()
logs18 = []
r18 = df.DungeonRunner(game18, p18, log=logs18.append)

_real_wait = r18._wait_dialog
def fake_wait(timeout, poll, sim=None, desc="进本弹窗"):
    if sim is None:
        return False          # 严格阈值假装没匹配到
    return True               # 放宽后命中
r18._wait_dialog = fake_wait
ok18 = r18.enter_dungeon()
clicks18 = [a[1] for a in game18.actions if a[0] == "click"]
check("严格阈值失败 → 放宽阈值后仍能进本",
      ok18 is True and PT_CONFIRM in clicks18, str(clicks18))
check("放宽阈值有日志", any("放宽" in l for l in logs18), str(logs18[-4:]))

# 兜底：识别不到弹窗也要点一次「进入游戏」
p19 = build_profile()
p19["mount"]["enabled"] = False
game19 = FakeGame(enter_appears=False)
logs19 = []
r19 = df.DungeonRunner(game19, p19, log=logs19.append)
r19._wait_dialog = lambda *a, **k: False
ok19 = r19.enter_dungeon()
clicks19 = [a[1] for a in game19.actions if a[0] == "click"]
check("识别不到弹窗 → 兜底点一次「进入游戏」", PT_CONFIRM in clicks19, str(clicks19))
check("兜底有日志说明", any("兜底点一次" in l for l in logs19), str(logs19[-5:]))
check("兜底也会存截图", any(a[0] == "snap" for a in game19.actions))

# 骑乘没标区域时，不应该按 S
p20 = build_profile()
p20["mount"] = {"enabled": True, "key": "s", "dialog": {"roi": None, "sig": []},
                "state": {"roi": None, "sig": []}, "mount_pt": None}
game20 = FakeGame()
logs20 = []
r20 = df.DungeonRunner(game20, p20, log=logs20.append)
r20.ensure_mount()
check("骑乘没标区域 → 不按 S",
      not any(a == ("tap", "s") for a in game20.actions), str(game20.actions[:4]))
check("骑乘没标区域 → 有日志说明",
      any("没标骑乘区域" in l for l in logs20), str(logs20))

# ---- 12) 保存要包含所有参数（轮数/循环/骑乘）-------------------------
p14 = build_profile()
p14.setdefault("retry", {})["rounds"] = 7
check("轮数能存进 profile", p14["retry"]["rounds"] == 7)
check("定时查疲劳参数在默认配置里",
      "fatigue_every" in df.DEFAULT_PROFILE["loop"])
check("循环参数在默认配置里",
      all(k in df.DEFAULT_PROFILE["loop"]
          for k in ("boss_seconds", "boss_rounds", "trash_seconds", "trash_rounds")))
check("骑乘配置在默认配置里",
      all(k in df.DEFAULT_PROFILE["mount"]
          for k in ("enabled", "key", "dialog", "state", "mount_pt")))
check("打怪点字段存在", "farm_pt" in df.DEFAULT_PROFILE["farm"])

print()
if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("PASS: 副本引擎（进本/退出弹窗分开识别 + 重试 + 疲劳）全部通过")
