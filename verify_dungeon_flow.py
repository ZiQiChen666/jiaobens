# -*- coding: utf-8 -*-
"""验证副本引擎 v3.13：

    点副本入口 → 盯「进入弹窗」(3s 没识别到就先盲点) → 进度条≥90% 才算进本
    → 开地图点 BOSS 点 → 识别「BOSS 身形」→ 打怪（每次平A都拾取）
    → BOSS 血条连续几次不见 = 打完 → 拾取 3 次 → 退出副本 → 缓冲

跑法：python verify_dungeon_flow.py
"""
import sys
import threading

import numpy as np

import dungeon_flow as df

W, H = 200, 120
PT_DUNGEON = (0.30, 0.55)   # 副本入口（地图1）
PT_CONFIRM = (0.50, 0.55)   # 弹窗「进入游戏」
PT_BOSS = (0.70, 0.40)      # 副本内 BOSS 点
PT_EXIT = (0.94, 0.05)
PT_EXIT_OK = (0.55, 0.68)

ROI_ENTER = [0.32, 0.20, 0.36, 0.16]     # 进入弹窗：x 64..136, y 24..43
ROI_EXIT = [0.32, 0.60, 0.36, 0.16]      # 退出弹窗：x 64..136, y 72..91
ROI_PROGRESS = [0.30, 0.50, 0.40, 0.08]  # 进度条：x 60..140, y 60..69
ROI_BOSS_BODY = [0.04, 0.30, 0.26, 0.30]  # BOSS 身形：x 8..60, y 36..72
ROI_BOSS_HP = [0.60, 0.06, 0.34, 0.06]   # BOSS 血条：x 120..188, y 7..14


def make_frame(enter_dialog=False, exit_dialog=False, progress=None,
               boss_body=False, boss_hp=False):
    arr = np.full((H, W, 4), 40, np.uint8)
    arr[..., 3] = 255
    if enter_dialog:
        arr[26:41, 70:130] = [60, 120, 220, 255]
    if exit_dialog:
        arr[74:89, 70:130] = [220, 90, 60, 255]
    if progress:
        filled = int(round(80 * float(progress)))
        if filled > 0:
            arr[61:69, 60:60 + filled] = [70, 230, 120, 255]
    if boss_body:
        arr[36:72, 8:60] = [180, 90, 200, 255]
    if boss_hp:
        arr[7:14, 120:188] = [230, 60, 60, 255]
    return (W, H, arr.tobytes())


class FakeGame:
    def __init__(self, enter_appears=True, fill_time=2.0, fill_max=1.0,
                 body_appears=True, hp_dead_after_a=8,
                 exit_appears=True):
        self.t = 0.0
        self.actions = []
        self.enter_appears = enter_appears
        self.fill_time = float(fill_time)
        self.fill_max = float(fill_max)
        self.body_appears = body_appears
        self.hp_dead_after_a = int(hp_dead_after_a)
        self.exit_appears = exit_appears
        self.state = {"enter": False, "exit": False, "body": False}
        self.load_start = None
        self.a_count = 0
        self.pickup_count = 0
        self.confirm_count = 0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, float(seconds))

    def progress_value(self):
        if self.load_start is None:
            return 0.0
        if self.fill_time <= 0:
            return self.fill_max
        return min(self.fill_max, (self.t - self.load_start) / self.fill_time)

    def hp_alive(self):
        return self.a_count < self.hp_dead_after_a

    def grab(self):
        return make_frame(
            enter_dialog=self.state["enter"],
            exit_dialog=self.state["exit"],
            progress=self.progress_value(),
            boss_body=self.state["body"],
            boss_hp=self.state["body"] and self.hp_alive(),
        )

    def snapshot(self, tag):
        self.actions.append(("snap", tag))

    def click_norm(self, point):
        pt = tuple(round(float(v), 3) for v in point)
        self.actions.append(("click", pt))
        if pt == PT_DUNGEON and self.enter_appears:
            self.state["enter"] = True
        elif pt == PT_CONFIRM:
            self.state["enter"] = False
            self.confirm_count += 1
            if self.load_start is None:
                self.load_start = self.t
        elif pt == PT_BOSS and self.body_appears:
            self.state["body"] = True
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


def build_profile(**over):
    enter_sig = df.signature(df.crop_norm(make_frame(enter_dialog=True), ROI_ENTER), 12)
    exit_sig = df.signature(df.crop_norm(make_frame(exit_dialog=True), ROI_EXIT), 12)
    body_sig = df.signature(df.crop_norm(make_frame(boss_body=True), ROI_BOSS_BODY), 12)
    p = df._merge(df.DEFAULT_PROFILE, {
        "action_gap": 1.0,
        "keys": {"open_map": "m", "pickup": "space"},
        "mount": {"enabled": False},
        "enter": {
            "dungeon_pt": list(PT_DUNGEON),
            "travel_wait": 1.0,
            "dialog": {"roi": ROI_ENTER, "sig": enter_sig, "grid": 12, "sim": 0.90},
            "confirm_pt": list(PT_CONFIRM),
            "dialog_timeout": 8.0,
            "blind_confirm_after": 1.0,
            "load_wait": 10.0,
            "retry": 2,
            "progress": {"roi": ROI_PROGRESS, "min_fill": 0.90,
                         "samples": 2, "interval": 0.1},
        },
        "farm": {
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
            "boss_present": {"roi": ROI_BOSS_BODY, "sig": body_sig,
                             "grid": 12, "sim": 0.88},
            "boss_hp": {"roi": ROI_BOSS_HP, "min_ink": 0.02, "miss": 2},
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
        "detect": {"poll": 0.3},
        "retry": {"rounds": 1, "gap": 1.0, "max_fail": 2},
    })
    return df._merge(p, over)


fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


# ---- 1) 弹窗立刻识别 → 立刻进本 --------------------------------------
game = FakeGame(fill_time=2.0)
logs = []
r = df.DungeonRunner(game, build_profile(), log=logs.append)
ok = r.enter_dungeon()
check("弹窗识别到 → 进本成功", ok is True)
check("进本很快（没傻等）", game.t < 12.0, f"t={game.t:.1f}")
check("日志有进度条百分比", any("进度条" in l for l in logs), str(logs[-5:]))
check("动作之间有缓冲", any("缓冲" in l for l in logs), str(logs[-5:]))
check("不再有“打怪点”这一步",
      not any("打怪点" in l for l in logs), str([l for l in logs if "打怪点" in l][:3]))

# ---- 2) 弹窗识别不到 → 1s 后盲点确认 → 进度条验证 ---------------------
p2 = build_profile()
p2["enter"]["dialog"] = {"roi": ROI_ENTER, "sig": [9.0] * 144, "grid": 12, "sim": 0.99}
p2["enter"]["blind_confirm_after"] = 1.0
game2 = FakeGame(fill_time=2.0)
logs2 = []
r2 = df.DungeonRunner(game2, p2, log=logs2.append)
ok2 = r2.enter_dungeon()
check("识别不到弹窗也会早点盲点（不阻塞 24s）", ok2 is True and game2.t < 12.0,
      f"ok={ok2} t={game2.t:.1f}")
check("日志说明是“先盲点一次”", any("先盲点一次" in l for l in logs2), str(logs2[-5:]))
check("盲点后进度条验证通过", any("进度条" in l and "≥" in l for l in logs2),
      str(logs2[-4:]))

# ---- 3) 进度条不满 90% → 判没进副本 → 重新寻路 ------------------------
game3 = FakeGame(fill_time=100.0, fill_max=0.5)
logs3 = []
r3 = df.DungeonRunner(game3, build_profile(enter={"retry": 2, "load_wait": 4.0}),
                      log=logs3.append)
ok3 = r3.enter_dungeon()
check("进度条只有一半 → 不算进本", ok3 is False)
check("进度条不到 → 重新寻路（副本入口 2 次）",
      [a[1] for a in game3.actions if a[0] == "click"].count(PT_DUNGEON) == 2)

# ---- 4) BOSS 身形：没识别到就重新寻路 --------------------------------
game4 = FakeGame(body_appears=False)
logs4 = []
r4 = df.DungeonRunner(game4, build_profile(), log=logs4.append)
ok4 = r4.goto_boss_point()
check("没识别到 BOSS 身形 → 重新寻路（BOSS 点 2 次）",
      [a[1] for a in game4.actions if a[0] == "click"].count(PT_BOSS) == 2)
check("始终没身形 → 判定失败", ok4 is False)
check("日志写“BOSS 身形”", any("BOSS 身形" in l for l in logs4), str(logs4[-4:]))

game5 = FakeGame()
r5 = df.DungeonRunner(game5, build_profile(), log=lambda _s: None)
check("识别到 BOSS 身形 → 可以打", r5.goto_boss_point() is True)

# ---- 5) 血条消失 = 打完；每次平A都拾取 -------------------------------
game6 = FakeGame(hp_dead_after_a=8)
logs6 = []
r6 = df.DungeonRunner(game6, build_profile(), log=logs6.append)
r6.hp_now = lambda: 5000
ok6 = r6.fight_until_finish()
check("血条消失 → 判定打完", ok6 is True and
      any("判定打完" in l for l in logs6), str(logs6[-4:]))
check("打完时间短（血条没了就停）", game6.t < 15.0, f"t={game6.t:.1f}")
check("每次平A都拾取", game6.pickup_count >= game6.a_count - 1,
      f"a={game6.a_count} pickup={game6.pickup_count}")

# ---- 6) 没框血条 → 按时间上限停 --------------------------------------
p7 = build_profile()
p7["farm"]["boss_hp"] = {"roi": None, "min_ink": 0.02, "miss": 3}
p7["loop"] = dict(p7["loop"], boss_seconds=3.0)
game7 = FakeGame()
logs7 = []
r7 = df.DungeonRunner(game7, p7, log=logs7.append)
r7.hp_now = lambda: 5000
r7.fight_until_finish()
check("没框血条 → 按时间停（3s）", 3.0 <= game7.t <= 6.0, f"t={game7.t:.1f}")

# ---- 7) 一整轮：点序 + 打完拾取 3 次 + 退出 --------------------------
game8 = FakeGame(fill_time=2.0, hp_dead_after_a=6)
logs8 = []
r8 = df.DungeonRunner(game8, build_profile(), log=logs8.append)
r8.hp_now = lambda: 5000
ok8 = r8.run_round()
clicks8 = [a[1] for a in game8.actions if a[0] == "click"]
check("一轮跑完成功", ok8 is True)
check("点序：副本入口→进入→BOSS点→退出→确认",
      clicks8.index(PT_DUNGEON) < clicks8.index(PT_CONFIRM)
      < clicks8.index(PT_BOSS) < clicks8.index(PT_EXIT)
      < clicks8.index(PT_EXIT_OK), str(clicks8))
check("打完又拾取了 3 次",
      game8.pickup_count >= game8.a_count + 3 - 1, str(game8.pickup_count))
check("有“打完再拾取”日志", any("打完再拾取" in l for l in logs8), str(logs8[-4:]))

# ---- 7b) 识别优化：旧标定的严格阈值会被自动放宽 --------------------
_old_profile = df._merge(df.DEFAULT_PROFILE, {
    "enter": {"dialog": {"roi": [0.1, 0.1, 0.2, 0.1], "sig": [1.0] * 144,
                         "grid": 12, "sim": 0.90}},
    "farm": {"boss_present": {"roi": [0.2, 0.2, 0.2, 0.2], "sig": [1.0] * 144,
                              "grid": 12, "sim": 0.90}},
})
_norm = df._normalize_profile(_old_profile)
check("旧标定的弹窗阈值被放宽到 0.80",
      abs(float(_norm["enter"]["dialog"]["sim"]) - 0.80) < 1e-6,
      str(_norm["enter"]["dialog"]["sim"]))
check("弹窗补上了宽松阈值/占比阈值",
      "sim_loose" in _norm["enter"]["dialog"]
      and "ink_min" in _norm["enter"]["dialog"])
check("BOSS 身形补上滑动搜索参数",
      float(_norm["farm"]["boss_present"].get("search", 1.0)) >= 1.6
      and float(_norm["farm"]["boss_present"].get("tol", 18)) >= 30)

# BOSS 身形：小范围晃动/带特效也能命中；没 BOSS 不误判
_body_spec = {"roi": ROI_BOSS_BODY, "grid": 12,
              "sig": df.signature(df.crop_norm(make_frame(boss_body=True), ROI_BOSS_BODY), 12),
              "sim": 0.60, "search": 1.6, "tol": 30.0}


class _G:
    def __init__(self, frame):
        self.frame = frame
    def grab(self):
        return self.frame
    def now(self):
        return 0.0


_r = df.DungeonRunner(_G(make_frame(boss_body=True)), build_profile(), log=lambda _s: None)
check("BOSS 身形：原位能命中", _r.anchor_visible(_body_spec, make_frame(boss_body=True)))
_r.game = _G(make_frame())
check("BOSS 身形：没 BOSS 不误判",
      not _r.anchor_visible(_body_spec, make_frame()))

# ---- 7c) 边跑边看：弹窗/身形在“路上”出现也要立刻命中 ----------------
# (a) 弹窗在寻路的第 2 秒就出现 → 不应该等满 travel_wait(10s)
p_edge = build_profile(enter={"travel_wait": 10.0, "dialog_timeout": 10.0})
game_edge = FakeGame(fill_time=1.0)
logs_edge = []
r_edge = df.DungeonRunner(game_edge, p_edge, log=logs_edge.append)
ok_edge = r_edge.enter_dungeon()
check("弹窗在路上出现 → 立刻进本（不等满寻路 10s）",
      ok_edge is True and game_edge.t < 12.0, f"t={game_edge.t:.1f}")

# (b) BOSS 身形在路上出现 → 立刻开打（不等满 travel_wait 10s）
p_boss = build_profile(farm={"travel_wait": 10.0})
game_boss = FakeGame()
logs_boss = []
r_boss = df.DungeonRunner(game_boss, p_boss, log=logs_boss.append)
ok_boss = r_boss.goto_boss_point()
check("BOSS 身形在路上出现 → 立刻确认",
      ok_boss is True and game_boss.t < 5.0, f"t={game_boss.t:.1f}")
check("寻路日志改成“边走边看”",
      any("边走边看" in l for l in logs_boss), str(logs_boss[:3]))

# ---- 8) 配置检查 ----------------------------------------------------
check("配置里有 BOSS 身形 / BOSS 血条",
      "boss_present" in df.DEFAULT_PROFILE["farm"]
      and "boss_hp" in df.DEFAULT_PROFILE["farm"])
check("弹窗盲点阈值在配置里",
      "blind_confirm_after" in df.DEFAULT_PROFILE["enter"])
check("识别阈值可配（sim / sim_loose / poll）",
      all(k in df.DEFAULT_PROFILE["detect"]
          for k in ("sim", "sim_loose", "ink_min", "poll")))

# ---- 9) 停止事件 ----------------------------------------------------
stop = threading.Event()
stop.set()
g9 = FakeGame()
r9 = df.DungeonRunner(g9, build_profile(), log=lambda _s: None, stop_event=stop)
r9.run_round()
check("停止事件立即生效", g9.t < 10.0, f"t={g9.t:.1f}")

print()
if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("PASS: 副本引擎 v3.13（只跑BOSS流程 + 进度条 + BOSS身形 + 血条判死）全部通过")
