# -*- coding: utf-8 -*-
"""验证副本引擎 v3.12 流程：

    打怪点 → 副本入口 → 进入弹窗 → 进入 → 进度条(≥90%) → 副本内 BOSS 点
    → 识别「打怪点BOSS标志」→ 挂机（每次平A都拾取）→ 识别「打怪完成标志」
    → 拾取 + 缓冲 → 退出副本 → 缓冲

跑法：python verify_dungeon_flow.py
"""
import sys
import threading

import numpy as np

import creature_curios_keyer as app
import dungeon_flow as df

W, H = 200, 120
PT_FARM = (0.20, 0.30)      # 打怪点（地图1）
PT_DUNGEON = (0.30, 0.55)   # 副本入口（地图1）
PT_CONFIRM = (0.50, 0.55)   # 弹窗里的「进入游戏」
PT_BOSS = (0.70, 0.40)      # 副本内 BOSS 点
PT_EXIT = (0.94, 0.05)
PT_EXIT_OK = (0.55, 0.68)

ROI_ENTER = [0.32, 0.20, 0.36, 0.16]     # 进入弹窗：x 64..136, y 24..43
ROI_EXIT = [0.32, 0.60, 0.36, 0.16]      # 退出弹窗：x 64..136, y 72..91
ROI_PROGRESS = [0.30, 0.50, 0.40, 0.08]  # 进度条：x 60..140, y 60..69
ROI_BOSSFLAG = [0.04, 0.04, 0.24, 0.14]  # 打怪点BOSS标志：x 8..56, y 5..21
ROI_FINISH = [0.70, 0.04, 0.24, 0.14]    # 打怪完成标志：x 140..188, y 5..21


def make_frame(enter_dialog=False, progress=None, boss_flag=False,
               finish=False, exit_dialog=False):
    arr = np.full((H, W, 4), 40, np.uint8)
    arr[..., 3] = 255
    if enter_dialog:
        arr[26:41, 70:130] = [60, 120, 220, 255]
    if exit_dialog:
        arr[74:89, 70:130] = [220, 90, 60, 255]
    if progress:                      # 进度条：按比例画亮色
        filled = int(round(80 * float(progress)))
        if filled > 0:
            arr[61:69, 60:60 + filled] = [70, 230, 120, 255]
    if boss_flag:
        arr[6:20, 10:54] = [250, 200, 60, 255]
    if finish:
        arr[6:20, 142:186] = [90, 160, 250, 255]
    return (W, H, arr.tobytes())


class FakeGame:
    def __init__(self, enter_appears=True, fill_time=2.0, fill_max=1.0,
                 boss_flag_delay=0.5, boss_flag_fails=False,
                 finish_after_a=0, exit_appears=True):
        self.t = 0.0
        self.actions = []
        self.last_error = ""
        self.enter_appears = enter_appears
        self.fill_time = float(fill_time)
        self.fill_max = float(fill_max)
        self.boss_flag_delay = float(boss_flag_delay)
        self.boss_flag_fails = boss_flag_fails
        self.finish_after_a = int(finish_after_a)
        self.exit_appears = exit_appears
        self.state = {"enter": False, "exit": False}
        self.load_start = None
        self.boss_flag_due = None
        self.a_count = 0
        self.pickup_count = 0

    # -- 时钟 --
    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, float(seconds))

    # -- 画面 --
    def progress_value(self):
        if self.load_start is None:
            return 0.0
        if self.fill_time <= 0:
            return self.fill_max
        return min(self.fill_max, (self.t - self.load_start) / self.fill_time)

    def grab(self):
        boss_flag = (self.boss_flag_due is not None
                     and not self.boss_flag_fails
                     and self.t >= self.boss_flag_due)
        finish = bool(self.finish_after_a and self.a_count >= self.finish_after_a)
        return make_frame(
            enter_dialog=self.state["enter"],
            progress=self.progress_value(),
            boss_flag=boss_flag,
            finish=finish,
            exit_dialog=self.state["exit"],
        )

    def snapshot(self, tag):
        self.actions.append(("snap", tag))

    # -- 输入 --
    def click_norm(self, point):
        pt = tuple(round(float(v), 3) for v in point)
        self.actions.append(("click", pt))
        if pt == PT_ENTRY_TUPLE if False else False:
            pass
        if pt == PT_DUNGEON and self.enter_appears:
            self.state["enter"] = True
        elif pt == PT_CONFIRM:
            self.state["enter"] = False
            if self.load_start is None:
                self.load_start = self.t
        elif pt == PT_BOSS:
            self.boss_flag_due = self.t + self.boss_flag_delay
        elif pt == PT_EXIT and self.exit_appears:
            self.state["exit"] = True
        elif pt == PT_EXIT_OK:
            self.state["exit"] = False
        return True

    def tap(self, name):
        self.actions.append(("tap", name))
        if name == "a":
            self.a_count += 1
        elif name == "space":
            self.pickup_count += 1
        return True


PT_ENTRY_TUPLE = tuple(PT_FARM)


def build_profile(**over):
    enter_sig = df.signature(df.crop_norm(make_frame(enter_dialog=True), ROI_ENTER), 12)
    exit_sig = df.signature(df.crop_norm(make_frame(exit_dialog=True), ROI_EXIT), 12)
    boss_sig = df.signature(df.crop_norm(make_frame(boss_flag=True), ROI_BOSSFLAG), 12)
    finish_sig = df.signature(df.crop_norm(make_frame(finish=True), ROI_FINISH), 12)
    p = df._merge(df.DEFAULT_PROFILE, {
        "action_gap": 1.0,
        "keys": {"open_map": "m", "pickup": "space"},
        "mount": {"enabled": False},
        "enter": {
            "dungeon_pt": list(PT_DUNGEON),
            "travel_wait": 1.0,
            "dialog": {"roi": ROI_ENTER, "sig": enter_sig, "grid": 12, "sim": 0.90},
            "confirm_pt": list(PT_CONFIRM),
            "dialog_timeout": 6.0,
            "load_wait": 10.0,
            "retry": 2,
            "progress": {"roi": ROI_PROGRESS, "min_fill": 0.90,
                         "samples": 2, "interval": 0.1},
        },
        "farm": {
            "farm_pt": list(PT_FARM),
            "boss_pt": list(PT_BOSS),
            "travel_wait": 1.0,
            "seconds": 60.0,
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
            "pickup_on_a": True,
            "pickup_times": 3,
            "boss_retry": 2,
            "arrived": {"roi": None, "sig": [], "grid": 12, "sim": 0.9},
            "boss_present": {"roi": ROI_BOSSFLAG, "sig": boss_sig,
                             "grid": 12, "sim": 0.90},
            "finish": {"roi": ROI_FINISH, "sig": finish_sig,
                       "grid": 12, "sim": 0.90},
        },
        "exit": {
            "exit_pt": list(PT_EXIT),
            "confirm_pt": list(PT_EXIT_OK),
            "dialog": {"roi": ROI_EXIT, "sig": exit_sig, "grid": 12, "sim": 0.90},
            "dialog_timeout": 3.0,
            "retry": 2,
            "exit_wait": 0.5,
            "confirm_wait": 0.5,
            "settle": 2.0,
        },
        "fatigue": {"enabled": False, "threshold": 800,
                    "pt": [0.10, 0.90], "roi": [0.4, 0.05, 0.2, 0.05]},
        "loop": {"boss_seconds": 60.0, "boss_rounds": 1, "trash_seconds": 30.0,
                 "trash_rounds": 1, "fatigue_every": 0, "max_switch": 0,
                 "default_action": "boss"},
        "detect": {"poll": 0.5},
        "retry": {"rounds": 1, "gap": 1.0, "max_fail": 2},
    })
    return df._merge(p, over)


fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


# ---- 1) 进度条测量（按列亮起来的比例，截半截也能量准）-----------------
class _FrameGame:
    def __init__(self, progress):
        self.progress = progress
    def grab(self):
        return make_frame(progress=self.progress)
    def now(self):
        return 0.0


_probe_profile = build_profile()
_runner = df.DungeonRunner(_FrameGame(0.0), _probe_profile, log=lambda _s: None)
for ratio in (0.0, 0.3, 0.5, 0.75, 1.0):
    _runner.game = _FrameGame(ratio)
    got = _runner.progress_fill()
    check(f"进度条 {ratio * 100:.0f}% 量得准", got is not None and abs(got - ratio) <= 0.12,
          f"量到 {got}")

# ---- 2) 进本：进度条满 90% 才走 ----
game = FakeGame(fill_time=2.0)
logs = []
r = df.DungeonRunner(game, build_profile(), log=logs.append)
ok = r.enter_dungeon()
check("进度条涨到 90% 以上 → 进本成功", ok is True)
check("先进了打怪点（地图1）",
      PT_FARM in [a[1] for a in game.actions if a[0] == "click"])
check("日志有进度条百分比", any("进度条" in l for l in logs), str(logs[-6:]))
check("动作之间有 1 秒缓冲", any("缓冲" in l for l in logs), str(logs[-6:]))

# ---- 3) 进度条卡在一半 → 不算进本，重新来 ----
game2 = FakeGame(fill_time=100.0, fill_max=0.5)
logs2 = []
r2 = df.DungeonRunner(game2, build_profile(enter={"retry": 2, "load_wait": 4.0}),
                      log=logs2.append)
ok2 = r2.enter_dungeon()
check("进度条只有一半 → 判没进副本", ok2 is False)
check("进度条没满 → 有明确日志",
      any("没到 90%" in l or "没有进入加载界面" in l for l in logs2), str(logs2[-4:]))
check("进度条没满 → 重新寻路（副本入口点了 2 次）",
      [a[1] for a in game2.actions if a[0] == "click"].count(PT_DUNGEON) == 2)

# ---- 4) 没出现进入弹窗 → 重新寻路 ----
game3 = FakeGame(enter_appears=False)
logs3 = []
r3 = df.DungeonRunner(game3, build_profile(enter={"retry": 2, "dialog_timeout": 3.0}),
                      log=logs3.append)
ok3 = r3.enter_dungeon()
check("没弹窗 → 重新寻路（副本入口 2 次）",
      [a[1] for a in game3.actions if a[0] == "click"].count(PT_DUNGEON) == 2)
check("没弹窗 → 最终不算进本", ok3 is False)

# ---- 5) BOSS 标志：没识别到就重新寻路 ----
p5 = build_profile()
game5 = FakeGame(boss_flag_fails=True)
logs5 = []
r5 = df.DungeonRunner(game5, p5, log=logs5.append)
ok5 = r5.goto_boss_point()
check("没识别到打怪点BOSS标志 → 重新寻路（BOSS 点 2 次）",
      [a[1] for a in game5.actions if a[0] == "click"].count(PT_BOSS) == 2)
check("始终没标志 → 判定失败", ok5 is False)
check("日志写清“重新寻路”", any("重新寻路" in l for l in logs5), str(logs5[-4:]))

# 识别到标志 → 通过
game6 = FakeGame(boss_flag_delay=0.5)
r6 = df.DungeonRunner(game6, build_profile(), log=lambda _s: None)
check("识别到打怪点BOSS标志 → 可以打", r6.goto_boss_point() is True)

# ---- 6) 打怪：每次平A都拾取；识别到完成标志就停 ----
p7 = build_profile()
p7["loop"] = dict(p7["loop"], boss_seconds=60.0)
game7 = FakeGame(finish_after_a=10)
logs7 = []
r7 = df.DungeonRunner(game7, p7, log=logs7.append)
r7.hp_now = lambda: 5000
ok7 = r7.fight_until_finish()
check("识别到打怪完成 → 停手", ok7 is True and
      any("打怪完成" in l for l in logs7), str(logs7[-3:]))
check("打怪时间很短（完成标志一到就停）", game7.t < 15.0, f"t={game7.t:.1f}")
check("每次平A都拾取", game7.pickup_count >= game7.a_count - 1,
      f"a={game7.a_count} pickup={game7.pickup_count}")
check("平A次数不多（完成标志后不再打）", game7.a_count <= 15, str(game7.a_count))

# 没标完成标志 → 按时间停
p8 = build_profile()
p8["farm"]["finish"] = {"roi": None, "sig": [], "grid": 12, "sim": 0.9}
p8["loop"] = dict(p8["loop"], boss_seconds=3.0)
game8 = FakeGame()
logs8 = []
r8 = df.DungeonRunner(game8, p8, log=logs8.append)
r8.hp_now = lambda: 5000
r8.fight_until_finish()
check("没标完成标志 → 按时间停（3s）", 3.0 <= game8.t <= 6.0, f"t={game8.t:.1f}")

# ---- 7) 一轮完整流程 ------------------------------------------------
game9 = FakeGame(fill_time=2.0, boss_flag_delay=0.5, finish_after_a=6)
logs9 = []
r9 = df.DungeonRunner(game9, build_profile(), log=logs9.append)
r9.hp_now = lambda: 5000
ok9 = r9.run_round()
clicks9 = [a[1] for a in game9.actions if a[0] == "click"]
check("一轮跑完成功", ok9 is True)
check("顺序：打怪点→副本入口→进入→BOSS点→退出→确认",
      clicks9.index(PT_FARM) < clicks9.index(PT_DUNGEON)
      < clicks9.index(PT_CONFIRM) < clicks9.index(PT_BOSS)
      < clicks9.index(PT_EXIT) < clicks9.index(PT_EXIT_OK), str(clicks9))
check("打完有再拾取（次数量 ≥ 平A前的最后拾取）",
      game9.pickup_count >= 6, str(game9.pickup_count))
check("退出后也有缓冲", any("退出后缓冲" in l for l in logs9), str(logs9[-4:]))

# ---- 8) 配置里不再有“副本门口”点位 ---------------------------------
check("配置里已去掉「副本门口」标定点",
      "entry_pt" not in df.DEFAULT_PROFILE["enter"],
      str(list(df.DEFAULT_PROFILE["enter"].keys())))
check("新增进度条/结束标志/BOSS 标志配置",
      all(k in df.DEFAULT_PROFILE["enter"] for k in ("progress",))
      and all(k in df.DEFAULT_PROFILE["farm"]
              for k in ("boss_present", "finish", "pickup_on_a")))

# ---- 9) 停止事件 ----------------------------------------------------
stop = threading.Event()
stop.set()
g10 = FakeGame()
r10 = df.DungeonRunner(g10, build_profile(), log=lambda _s: None, stop_event=stop)
r10.run_round()
check("停止事件立即生效", g10.t < 10.0, f"t={g10.t:.1f}")

print()
if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("PASS: 副本引擎 v3.12（进度条/平A拾取/完成标志/缓冲）全部通过")
