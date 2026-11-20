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
               boss_body=False, boss_hp=False, boss_body_alt=False):
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
    if boss_body_alt:      # 另一个角度/动作的 BOSS（第 2 张样本）
        arr[36:72, 8:60] = [200, 120, 60, 255]
        arr[40:50, 20:40] = [120, 200, 90, 255]
    if boss_hp:
        arr[7:14, 120:188] = [230, 60, 60, 255]
    return (W, H, arr.tobytes())


class FakeGame:
    def __init__(self, enter_appears=True, fill_time=2.0, fill_max=1.0,
                 body_appears=True, hp_dead_after_a=8,
                 exit_appears=True, frames_move_first=0):
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
        self.frames_move_first = int(frames_move_first)
        self.grab_count = 0

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
        self.grab_count += 1
        if self.frames_move_first and self.grab_count <= self.frames_move_first:
            # 前几帧模拟“人在走”：画面明显不同
            return make_frame(progress=None, boss_body=(self.grab_count % 2 == 0))
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
            "boss_hp": {"roi": ROI_BOSS_HP, "mode": "bright",
                        "min_fill": 0.15, "miss": 2},
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
        "loop": {"boss_seconds": 60.0, "fatigue_every": 0,
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


def clicks_near(actions, pt, radius=0.05):
    """地图点微调后，点击会落在标记点周围；用半径判断而不是精确相等。"""
    return [a[1] for a in actions if a[0] == "click"
            and abs(a[1][0] - pt[0]) <= radius
            and abs(a[1][1] - pt[1]) <= radius]


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
      len(clicks_near(game3.actions, PT_DUNGEON)) == 2,
      str(clicks_near(game3.actions, PT_DUNGEON)))

# ---- 4) BOSS 身形：没识别到就重新寻路 --------------------------------
game4 = FakeGame(body_appears=False)
logs4 = []
r4 = df.DungeonRunner(game4, build_profile(), log=logs4.append)
ok4 = r4.goto_boss_point()
check("没识别到 BOSS 身形 → 重新寻路（BOSS 点 2 次）",
      len(clicks_near(game4.actions, PT_BOSS)) == 2,
      str(clicks_near(game4.actions, PT_BOSS)))
check("始终没身形 → 判定失败", ok4 is False)
check("日志写“BOSS 身形”", any("BOSS 身形" in l for l in logs4), str(logs4[-4:]))

game5 = FakeGame()
r5 = df.DungeonRunner(game5, build_profile(), log=lambda _s: None)
check("识别到 BOSS 身形 → 可以打", r5.goto_boss_point() is True)

# ---- 5) 血条消失 = 打完；每次平A都拾取 -------------------------------
game6 = FakeGame(hp_dead_after_a=8)
game6.state["body"] = True      # 真实现场：打的时候图标/血条在，打完才消失
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
p7["farm"]["boss_hp"] = {"roi": None, "min_fill": 0.15, "miss": 3}
p7["farm"]["boss_present"] = {"roi": None}   # 血条/身形都没框 → 只能按时间停
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

# ---- 7d) 多张样本：任一张像就算命中 --------------------------------
p_multi = build_profile()
_spec = p_multi["farm"]["boss_present"]
_spec["sigs"] = [
    _spec["sig"],
    df.signature(df.crop_norm(make_frame(boss_body_alt=True), ROI_BOSS_BODY), 12),
]
game_m = FakeGame()
r_m = df.DungeonRunner(game_m, p_multi, log=lambda _s: None)


class _G2:
    def __init__(self, frame):
        self.frame = frame
    def grab(self):
        return self.frame
    def now(self):
        return 0.0


ALT_FRAME = make_frame(boss_body=False, boss_body_alt=True)
_sim, _ink = r_m.anchor_metrics(_spec, ALT_FRAME)
check("多张样本：第 2 张样本也能命中", _sim >= 0.60, f"相似度 {_sim:.2f}")

# ---- 7e) 走路时不动识别，停下才识别 --------------------------------
p_still = build_profile()
p_still["farm"]["boss_present"]["still_seconds"] = 0.4
p_still["farm"]["boss_present"]["still_timeout"] = 3.0
game_still = FakeGame(frames_move_first=6)   # 前 6 帧画面在动
logs_still = []
r_still = df.DungeonRunner(game_still, p_still, log=logs_still.append)
game_still.state["body"] = False
ok_still = r_still.wait_scene_still(0.4, 3.0, 0.015)
check("画面在动时会一直等", any("画面已静止" in l for l in logs_still)
      or any("还在动" in l for l in logs_still), str(logs_still[-3:]))

# ---- 7f) 挂机 ↔ 打BOSS 联动（按疲劳滞回切换）------------------------
p_link = build_profile()
p_link["loop"] = dict(p_link["loop"], link_fatigue=True,
                      fatigue_every=0.5, default_action="farm")
p_link["fatigue"] = {"enabled": True, "threshold": 600,
                     "pt": [0.1, 0.9], "roi": [0.4, 0.05, 0.2, 0.05]}
p_link["farm"]["farm_pt"] = [0.20, 0.30]
_seq = {"values": [800, 800, -700, -700, 700, 700, -700, -700], "i": 0}


def _next_fatigue():
    vals = _seq["values"]
    v = vals[min(_seq["i"], len(vals) - 1)]
    _seq["i"] += 1
    return v, 1000, ""


game_link = FakeGame(fill_time=1.0, hp_dead_after_a=5)
logs_link = []
r_link = df.DungeonRunner(game_link, p_link, log=logs_link.append)
r_link.hp_now = lambda: 5000
r_link.fatigue_check = _next_fatigue

# 第 1 轮：疲劳 800 > +600 → 应该去「打怪点」挂机
ok_farm = r_link.run_round()
clicks_link = [a[1] for a in game_link.actions if a[0] == "click"]
check("疲劳 >+600 → 去打了打怪点", (0.20, 0.30) in clicks_link, str(clicks_link[:4]))
check("挂机中疲劳降到 -阈值 → 自动切去打BOSS",
      r_link._mode == "boss", str(r_link._mode))
check("挂机没去副本入口", PT_DUNGEON not in clicks_link)

# 第 2 轮：疲劳 -700 < -600 → 应该切去打 BOSS
_seq["i"] = 2
game_link.actions.clear()
ok_boss = r_link.run_round()
clicks2 = [a[1] for a in game_link.actions if a[0] == "click"]
check("疲劳 < -600 → 切去打 BOSS", r_link._mode == "boss", str(r_link._mode))
check("切过去后真的进副本了", PT_DUNGEON in clicks2, str(clicks2[:5]))
check("日志有“切到打BOSS”", any("切到打BOSS" in l for l in logs_link),
      str([l for l in logs_link if "切到" in l][-2:]))

# 第 3 轮：疲劳 700 > +600 → 切回挂机
_seq["i"] = 4
game_link.actions.clear()
r_link.run_round()
clicks3 = [a[1] for a in game_link.actions if a[0] == "click"]
check("疲劳 >+600 → 切回打怪点", (0.20, 0.30) in clicks3, str(clicks3[:4]))

# 关闭联动 → 永远走 BOSS
p_nolink = build_profile()
p_nolink["loop"] = dict(p_nolink["loop"], link_fatigue=False)
g_nolink = FakeGame(fill_time=1.0, hp_dead_after_a=5)
r_nolink = df.DungeonRunner(g_nolink, p_nolink, log=lambda _s: None)
r_nolink.hp_now = lambda: 5000
r_nolink.fatigue_check = lambda: (900, 1000, "")
r_nolink.run_round()
check("关掉联动 → 直接走 BOSS 流程",
      PT_DUNGEON in [a[1] for a in g_nolink.actions if a[0] == "click"])

# ---- 7g) 自动喝药：只按 Q，血量低于阈值每 6 秒一次 ------------------
p_pot = build_profile()
p_pot["farm"]["potion_keys"] = ["q"]
p_pot["farm"]["potion_every"] = 6.0
p_pot["farm"]["hp_threshold"] = 1600
p_pot["loop"] = dict(p_pot["loop"], boss_seconds=20.0)
g_pot = FakeGame(hp_dead_after_a=100000)   # 让战斗持续满 20 秒
g_pot.state["body"] = True                  # 站在 BOSS 面前（血条在）
logs_pot = []
r_pot = df.DungeonRunner(g_pot, p_pot, log=logs_pot.append)
r_pot.hp_now = lambda: 900          # 一直低于阈值
r_pot.fight_until_finish()
taps_pot = [a[1] for a in g_pot.actions if a[0] == "tap"]
check("血量低于阈值 → 按 Q", "q" in taps_pot, str(taps_pot[:12]))
check("不再按 W", "w" not in taps_pot, str([t for t in taps_pot if t == "w"][:3]))
q_times = taps_pot.count("q")
check("20 秒内大概按了 3 次 Q（6 秒一次）", 2 <= q_times <= 4, f"Q×{q_times}")

g_pot2 = FakeGame(hp_dead_after_a=100000)
g_pot2.state["body"] = True
r_pot2 = df.DungeonRunner(g_pot2, p_pot, log=lambda _s: None)
r_pot2.hp_now = lambda: 9000        # 血量充足
r_pot2.fight_until_finish()
check("血量充足 → 不喝药",
      "q" not in [a[1] for a in g_pot2.actions if a[0] == "tap"])

# ---- 7h) 一键放入仓库：两个点位 + ESC 两次 + 0.5s 缓冲 ---------------
p_wh = build_profile()
p_wh["warehouse"] = {"enabled": True, "icon_pt": [0.92, 0.08],
                     "deposit_pt": [0.60, 0.30], "gap": 0.5,
                     "open_wait": 1.0, "esc_times": 2, "after": "exit"}
g_wh = FakeGame(fill_time=1.0, hp_dead_after_a=5)
logs_wh = []
r_wh = df.DungeonRunner(g_wh, p_wh, log=logs_wh.append)
r_wh.hp_now = lambda: 5000
ok_wh = r_wh.run_round()
clicks_wh = [a[1] for a in g_wh.actions if a[0] == "click"]
taps_wh = [a[1] for a in g_wh.actions if a[0] == "tap"]
check("一轮成功（含放仓库）", ok_wh is True)
check("点过仓库图标", (0.92, 0.08) in clicks_wh, str(clicks_wh))
check("点过一键放入", (0.60, 0.30) in clicks_wh, str(clicks_wh))
check("点仓库图标在一键放入之前",
      clicks_wh.index((0.92, 0.08)) < clicks_wh.index((0.60, 0.30)))
check("ESC 正好按 2 下", taps_wh.count("esc") == 2, str(taps_wh.count("esc")))
check("日志有“一键放入仓库”", any("一键放入仓库" in l for l in logs_wh),
      str(logs_wh[-4:]))

# 关掉仓库 → 不点仓库
p_wh_off = build_profile()
p_wh_off["warehouse"] = {"enabled": False, "icon_pt": [0.92, 0.08],
                         "deposit_pt": [0.60, 0.30]}
g_wh_off = FakeGame(fill_time=1.0, hp_dead_after_a=5)
g_wh2 = g_wh_off
r_wh2 = df.DungeonRunner(g_wh_off, p_wh_off, log=lambda _s: None)
r_wh2.hp_now = lambda: 5000
r_wh2.run_round()
check("关掉仓库 → 不点仓库图标",
      (0.92, 0.08) not in [a[1] for a in g_wh2.actions if a[0] == "click"])

# ---- 7i) 后台按键：prime + 焦点子窗口；不允许抢占时不切前台 ---------
p_bg = build_profile()
p_bg["allow_grab"] = False
g_bg = FakeGame()
r_bg = df.DungeonRunner(g_bg, p_bg, log=lambda _s: None)
win = df.WinGame.__new__(df.WinGame)          # 只测参数判断，不真的开窗口
win.profile = {"allow_grab": False, "input_mode": "hybrid"}
check("allow_grab=False 时不抢占", win.allow_grab() is False)
win.profile = {"allow_grab": True, "input_mode": "hybrid"}
check("allow_grab=True 时允许抢占", win.allow_grab() is True)
check("默认配置里 allow_grab=False",
      df.DEFAULT_PROFILE.get("allow_grab") is False)

# 连续挂机：每 fatigue_every 查疲劳，< -阈值 就结束（不再按“段”停）
p_farm = build_profile()
p_farm["loop"] = dict(p_farm["loop"], link_fatigue=True, fatigue_every=10.0,
                      default_action="farm")
p_farm["fatigue"] = {"enabled": True, "threshold": 600,
                     "pt": [0.1, 0.9], "roi": [0.4, 0.05, 0.2, 0.05]}
p_farm["farm"]["farm_pt"] = [0.20, 0.30]
p_farm["farm"]["hp_threshold"] = 0
_seq2 = {"vals": [500, 200, -700], "i": 0}


def _fat():
    v = _seq2["vals"][min(_seq2["i"], 2)]
    _seq2["i"] += 1
    return v, 1000, ""


g_farm = FakeGame()
logs_farm = []
r_farm = df.DungeonRunner(g_farm, p_farm, log=logs_farm.append)
r_farm.hp_now = lambda: 5000
r_farm.fatigue_check = _fat
r_farm._mode = "farm"
ok_farm = r_farm.farm_round()
check("挂机一直打，直到疲劳 < -阈值", ok_farm is True)
check("挂机结束时会打印“结束挂机”",
      any("结束挂机" in l for l in logs_farm), str(logs_farm[-4:]))
check("挂机期间一直在按 A（连续挂机）", 
      [a[1] for a in g_farm.actions if a[0] == "tap"].count("a") > 20,
      str([a[1] for a in g_farm.actions if a[0] == "tap"].count("a")))

# ---- 7i2) 血条判死：按开打基准自适应 + 身形兜底 ---------------------
g_hp = FakeGame()
g_hp.state["body"] = True
r_hp = df.DungeonRunner(g_hp, build_profile(), log=lambda _s: None)
base = r_hp.boss_hp_ratio()
check("血条基准能读到（活着时亮度高）", base is not None and base > 0.5,
      f"base={base}")
g_hp.a_count = 999
check("血条基准对比：血条没了 → 判死",
      r_hp.boss_hp_visible() is False,
      f"ratio={getattr(r_hp, '_last_hp_ratio', None)} "
      f"need={getattr(r_hp, '_last_hp_need', None)}")

# ---- 7i3) 血条判死三种 mode 都能跑（bar / bright / ink） -------------
for _mode in ("bar", "bright", "ink"):
    g_m = FakeGame()
    g_m.state["body"] = True
    p_m = build_profile()
    hp_spec = dict(p_m["farm"]["boss_hp"], mode=_mode)
    if _mode == "ink":
        # ink 模式靠「外圈当背景」拿参考色，ROI 要留一圈背景
        hp_spec["roi"] = [0.55, 0.02, 0.42, 0.14]
    p_m["farm"]["boss_hp"] = hp_spec
    r_m = df.DungeonRunner(g_m, p_m, log=lambda _s: None)
    alive = r_m.boss_hp_visible()
    g_m.a_count = 10 ** 9        # 血条消失
    dead = r_m.boss_hp_visible()
    check(f"血条判死 mode={_mode}：活着=True / 没了=False",
          alive is True and dead is False, f"alive={alive} dead={dead}")

# ---- 7i4) 默认判死：盯「图标/血条」还在不在（不靠颜色） -------------
check("默认判死方式 = OCR 读血量（主程序同款）",
      df.DEFAULT_PROFILE["farm"]["boss_hp"].get("mode") == "ocr",
      str(df.DEFAULT_PROFILE["farm"]["boss_hp"]))
_p_old = {"farm": {"boss_hp": {"roi": [0.6, 0.06, 0.34, 0.06],
                              "mode": "bar", "min_bar": 0.1, "miss": 3}}}
_p_new = df._normalize_profile(_p_old)
check("旧标定 mode=bar → 回退成 ocr",
      _p_new["farm"]["boss_hp"]["mode"] == "ocr",
      str(_p_new["farm"]["boss_hp"]))
_p_icon = {"farm": {"boss_hp": {"roi": [0.6, 0.06, 0.34, 0.06],
                               "mode": "icon", "use_icon": True}}}
check("写 use_icon=true 才保留 icon 模式",
      df._normalize_profile(_p_icon)["farm"]["boss_hp"]["mode"] == "icon")
check("只写 mode=icon（没 use_icon）也回退 ocr",
      df._normalize_profile(
          {"farm": {"boss_hp": {"roi": [0, 0, 1, 1], "mode": "icon"}}}
      )["farm"]["boss_hp"]["mode"] == "ocr")

p_icon4 = build_profile()
p_icon4["farm"]["boss_hp"] = dict(p_icon4["farm"]["boss_hp"],
                                 mode="icon", use_icon=True)
g_icon = FakeGame()
g_icon.state["body"] = True
r_icon = df.DungeonRunner(g_icon, p_icon4, log=lambda _s: None)
sigs_icon = r_icon.capture_boss_icon(samples=2, gap=0.0)
check("开打时拍到了图标基准", len(sigs_icon) >= 1, str(len(sigs_icon)))
check("图标还在 → 没死", r_icon.boss_hp_visible() is True)
g_icon.a_count = 10 ** 9      # BOSS 死了，图标/血条消失
dead_icon = r_icon.boss_hp_visible()
check("图标没了 → 判死", dead_icon is False,
      f"相似度={getattr(r_icon, '_last_hp_ratio', None)}")

# 打一局：图标消失后要提前结束，而不是硬打满上限
g_fs = FakeGame(hp_dead_after_a=6)
g_fs.state["body"] = True
p_fs = build_profile()
p_fs["loop"] = dict(p_fs["loop"], boss_seconds=60.0)
logs_fs = []
r_fs = df.DungeonRunner(g_fs, p_fs, log=logs_fs.append)
r_fs.hp_now = lambda: 5000
_t0 = g_fs.t
r_fs.fight_until_finish()
check("图标消失 → 提前结束（没等到 60s 上限）",
      g_fs.t - _t0 < 30.0, f"{g_fs.t - _t0:.1f}s")
check("日志里写了「判定打完」",
      any("判定打完" in l for l in logs_fs), str(logs_fs[-3:]))

# ---- 7i5) 真实数据的坑：打斗中形状在变，不能因此判死 -----------------
class IconGame(FakeGame):
    """复刻 23:53 那次的现场（参数取自程序自己存的 base/miss/dead 截图）：

    · 开打：一片暖色 224,207,177（同色 98%）；
    · 打斗中 1 秒后：颜色还在，但形状被特效/掉血改掉（同色 64% / 相似 69%）；
    · 真死：整块变成冷色 163,178,185（同色 2%）。
    """

    def __init__(self, mode="alive"):
        super().__init__()
        self.mode = mode

    def grab(self):
        arr = np.full((H, W, 4), 40, np.uint8)
        arr[..., 3] = 255
        y0, y1, x0, x1 = 7, 14, 120, 188
        if self.mode == "alive":
            arr[y0:y1, x0:x1] = [224, 207, 177, 255]
        elif self.mode == "fight":
            arr[y0:y1, x0:x1] = [224, 207, 177, 255]
            arr[y0:y1, x0:x0 + 20] = [60, 70, 90, 255]
            arr[y0 + 2:y0 + 5, x0 + 24:x1 - 4] = [190, 175, 150, 255]
        else:                      # dead
            arr[y0:y1, x0:x1] = [163, 178, 185, 255]
        return (W, H, arr.tobytes())


p_ic5 = build_profile()
p_ic5["farm"]["boss_hp"] = dict(p_ic5["farm"]["boss_hp"],
                               mode="icon", use_icon=True)
g_ic = IconGame("alive")
logs_ic = []
r_ic = df.DungeonRunner(g_ic, p_ic5, log=logs_ic.append)
r_ic.capture_boss_icon(samples=2, gap=0.0)
check("图标基准：主色记下来了", r_ic._boss_icon_color is not None)
check("开打时判活着", r_ic.boss_hp_visible() is True)
g_ic.mode = "fight"
logs_ic.clear()
check("打斗中形状变了也算活着（不误判）",
      r_ic.boss_hp_visible() is True, str(logs_ic))
g_ic.mode = "dead"
logs_ic.clear()
check("真死了才判死", r_ic.boss_hp_visible() is False, str(logs_ic))
check("判死日志写清同色/相似两个数",
      any("同色" in l and "相似" in l for l in logs_ic), str(logs_ic))

# ---- 7i6) OCR 判死（主程序 BossWatcher 同款）------------------------
p_ocr = build_profile()
p_ocr["farm"]["boss_hp"] = {"roi": ROI_BOSS_HP, "mode": "ocr", "kill_hp": 0,
                            "miss": 2, "ocr_every": 0.5, "grace": 0.0}
p_ocr["loop"] = dict(p_ocr["loop"], boss_seconds=60.0)
_texts = {"i": 0,
          "seq": (["冰牙鲨 18000/18000 10"] * 4
                  + ["冰牙鲨 16000/18000"] * 2
                  + [""] * 8)}
_real_ocr = df.app.ocr_text_from_bgra


def _fake_ocr(data, w, h, rect):
    i = min(_texts["i"], len(_texts["seq"]) - 1)
    _texts["i"] += 1
    return _texts["seq"][i]


df.app.ocr_text_from_bgra = _fake_ocr
try:
    g_ocr = FakeGame(hp_dead_after_a=10 ** 9)
    g_ocr.state["body"] = True
    logs_ocr = []
    r_ocr = df.DungeonRunner(g_ocr, p_ocr, log=logs_ocr.append)
    r_ocr.hp_now = lambda: 5000
    _t_ocr = g_ocr.t
    r_ocr.fight_until_finish()
    check("OCR 判死：读到血量时判活着",
          any("OCR 已生效：血量 18000" in l for l in logs_ocr),
          str(logs_ocr[:4]))
    check("OCR 判死：读不到血量连续 N 次 → 打完",
          g_ocr.t - _t_ocr < 30.0, f"{g_ocr.t - _t_ocr:.1f}s")
    check("OCR 判死：日志写了「判定打完」",
          any("判定打完" in l for l in logs_ocr), str(logs_ocr[-3:]))
    _texts["i"] = 0
    _texts["seq"] = ["冰牙鲨 0/18000 10"]
    r_ocr._hp_seen = False
    r_ocr._hp_ocr_at = -1.0
    check("OCR 读到 0 血 → 立刻判死", r_ocr.boss_hp_visible() is False)
finally:
    df.app.ocr_text_from_bgra = _real_ocr

# ---- 7j) 引路宏：回放录制的点击/按键 -------------------------------
p_nav = build_profile()
p_nav["nav_macro"] = {
    "enabled": True,
    "fallback": False,
    "speed": 2.0,
    "events": [
        {"type": "key_down", "t": 0.0, "name": "m"},
        {"type": "down", "t": 0.2, "button": "left",
         "nx": PT_DUNGEON[0], "ny": PT_DUNGEON[1]},
        {"type": "key_down", "t": 0.2, "name": "esc"},
    ],
}
g_nav = FakeGame()
r_nav = df.DungeonRunner(g_nav, p_nav, log=lambda _s: None)
check("引路宏已启用", r_nav.nav_macro_enabled() is True)
check("引路宏回放成功", r_nav.play_nav_macro() is True)
check("引路宏按了开图键和 Esc",
      [a[1] for a in g_nav.actions if a[0] == "tap"][:2] == ["m", "esc"],
      str(g_nav.actions[:4]))
check("引路宏点到了地图点",
      len(clicks_near(g_nav.actions, PT_DUNGEON, 0.02)) == 1,
      str(g_nav.actions[:4]))

# ---- 7k) 引路宏没到 BOSS 且不回退 → 先兜底退本 --------------------
p_navfail = build_profile()
p_navfail["nav_macro"] = {
    "enabled": True,
    "fallback": False,
    "speed": 1.0,
    "events": [{"type": "key_down", "t": 0.0, "name": "m"}],
}
g_navfail = FakeGame(body_appears=False)
logs_navfail = []
r_navfail = df.DungeonRunner(g_navfail, p_navfail, log=logs_navfail.append)
r_navfail.hp_now = lambda: 5000
ok_navfail = r_navfail.boss_round()
clicks_navfail = [a[1] for a in g_navfail.actions if a[0] == "click"]
check("引路宏没到 BOSS → 本轮算失败", ok_navfail is False)
check("引路宏没到 BOSS → 兜底点了「退出副本」",
      PT_EXIT in clicks_navfail, str(clicks_navfail))
check("兜底退本也点了「确认」", PT_EXIT_OK in clicks_navfail, str(clicks_navfail))
check("日志写清了为什么要兜底退本",
      any("兜底退出副本" in l for l in logs_navfail),
      str([l for l in logs_navfail if "兜底" in l][-2:]))

# ---- 7l) 打BOSS途中疲劳 >+阈值 → 这轮收尾（而不是硬打满上限）---------
p_mid = build_profile()
p_mid["loop"] = dict(p_mid["loop"], boss_seconds=300.0, fatigue_every=1.0,
                     link_fatigue=True)
p_mid["fatigue"] = {"enabled": True, "threshold": 600,
                    "pt": [0.1, 0.9], "roi": [0.4, 0.05, 0.2, 0.05]}
g_mid = FakeGame(hp_dead_after_a=10 ** 9)
g_mid.state["body"] = True      # 血条一直在，只能靠疲劳结束
logs_mid = []
r_mid = df.DungeonRunner(g_mid, p_mid, log=logs_mid.append)
r_mid.hp_now = lambda: 5000
r_mid.fatigue_check = lambda: (700, 1000, "")
t0 = g_mid.t
r_mid.fight_until_finish()
check("打BOSS途中疲劳 >+阈值 → 提前收尾（不到上限 300s）",
      g_mid.t - t0 < 60.0, f"{g_mid.t - t0:.1f}s")
check("日志写了「收尾」",
      any("收尾" in l for l in logs_mid), str(logs_mid[-3:]))

# 关掉联动时不在 BOSS 战里查疲劳（查了也没地方可切）
p_mid2 = build_profile()
p_mid2["loop"] = dict(p_mid2["loop"], boss_seconds=2.0, fatigue_every=0.5,
                      link_fatigue=False)
p_mid2["fatigue"] = {"enabled": True, "threshold": 600,
                     "pt": [0.1, 0.9], "roi": [0.4, 0.05, 0.2, 0.05]}
g_mid2 = FakeGame(hp_dead_after_a=10 ** 9)
g_mid2.state["body"] = True
calls = {"n": 0}
logs_mid2 = []
r_mid2 = df.DungeonRunner(g_mid2, p_mid2, log=logs_mid2.append)
r_mid2.hp_now = lambda: 5000


def _fat_count():
    calls["n"] += 1
    return 700, 1000, ""


r_mid2.fatigue_check = _fat_count
r_mid2.fight_until_finish()
check("关掉联动 → BOSS 战里不查疲劳", calls["n"] == 0, f"查了 {calls['n']} 次")

# ---- 8) 配置检查 ----------------------------------------------------
check("配置里有 BOSS 身形 / BOSS 血条",
      "boss_present" in df.DEFAULT_PROFILE["farm"]
      and "boss_hp" in df.DEFAULT_PROFILE["farm"])
check("弹窗盲点阈值在配置里",
      "blind_confirm_after" in df.DEFAULT_PROFILE["enter"])
check("喝药默认只按 Q", df.DEFAULT_PROFILE["farm"]["potion_keys"] == ["q"])
check("仓库配置在默认里",
      all(k in df.DEFAULT_PROFILE["warehouse"]
          for k in ("enabled", "icon_pt", "deposit_pt", "gap", "esc_times"))
      and df.DEFAULT_PROFILE["warehouse"]["esc_times"] == 2)
check("联动配置在默认里",
      df.DEFAULT_PROFILE["loop"].get("link_fatigue") is True
      and "farm_seconds" not in df.DEFAULT_PROFILE["loop"]
      and 5.0 <= df.DEFAULT_PROFILE["loop"]["boss_seconds"] <= 1000.0)
check("旧键已清掉（max_switch / finish）",
      "max_switch" not in df.DEFAULT_PROFILE["loop"]
      and "finish" not in df.DEFAULT_PROFILE["farm"])
check("打完 BOSS 的补拾取次数写进默认配置",
      df.DEFAULT_PROFILE["farm"].get("pickup_times") == 3)
check("疲劳上下限默认 -600 / +600",
      df.DEFAULT_PROFILE["fatigue"].get("low") == -600
      and df.DEFAULT_PROFILE["fatigue"].get("high") == 600)

# ---- 疲劳下限/上限可以分开设，互不影响 ----
p_lim = build_profile()
p_lim["fatigue"] = {"enabled": True, "low": -500, "high": 900,
                    "pt": [0.1, 0.9], "roi": [0.4, 0.05, 0.2, 0.05]}
r_lim = df.DungeonRunner(FakeGame(), p_lim, log=lambda _s: None)
check("疲劳 < 下限(-500) → 去打BOSS",
      r_lim.action_for_fatigue(-501) == "boss", r_lim.action_for_fatigue(-501))
check("疲劳在上下限之间 → 保持默认",
      r_lim.action_for_fatigue(800) == "boss"
      and r_lim.action_for_fatigue(0) == "boss")
check("疲劳 > 上限(900) → 回打怪点",
      r_lim.action_for_fatigue(901) == "farm", r_lim.action_for_fatigue(901))
check("上下限读得回来（-500 / 900）", r_lim.fatigue_limits() == (-500, 900),
      str(r_lim.fatigue_limits()))
# 旧标定只有 threshold 时按 ±threshold 折算
p_old = {"fatigue": {"enabled": True, "threshold": 700}}
check("旧标定 threshold=700 → 折算成 -700 / +700",
      df._normalize_profile(p_old)["fatigue"]["low"] == -700
      and df._normalize_profile(p_old)["fatigue"]["high"] == 700,
      str(df._normalize_profile(p_old)["fatigue"]))

# 旧存档只有 threshold：加载时要折成 low/high（别被默认 -600/600 盖掉）
import json as _json
import os as _os
import tempfile as _tempfile

_tmp_fat = _os.path.join(_tempfile.gettempdir(), "dungeon_legacy_fatigue.json")
with open(_tmp_fat, "w", encoding="utf-8") as _fh:
    _json.dump({"fatigue": {"enabled": True, "threshold": 791}}, _fh)
_p_legacy = df.load_profile(_tmp_fat)
check("旧存档 threshold=791 → 加载成 -791 / +791",
      _p_legacy["fatigue"].get("low") == -791
      and _p_legacy["fatigue"].get("high") == 791,
      str(_p_legacy["fatigue"]))
try:
    _os.remove(_tmp_fat)
except OSError:
    pass

# ---- 疲劳滞回：在打怪就一直打到 < 下限；在打BOSS就打到 > 上限 ----
def _run_combat(vals, action):
    p_h = build_profile()
    p_h["fatigue"] = {"enabled": True, "low": -800, "high": 800,
                      "pt": [0.1, 0.9], "roi": [0.4, 0.05, 0.2, 0.05]}
    p_h["loop"] = dict(p_h["loop"], fatigue_every=1.0)
    g_h = FakeGame()
    logs_h = []
    r_h = df.DungeonRunner(g_h, p_h, log=logs_h.append)
    seq_h = {"i": 0}

    def _fc():
        v = vals[min(seq_h["i"], len(vals) - 1)]
        seq_h["i"] += 1
        return v, 1000, ""

    r_h.fatigue_check = _fc
    r_h._switch_to = None
    r_h.combat(3.0, desc="打怪点挂机", action=action)
    return r_h, logs_h


r_h1, _ = _run_combat([700, 700, 700], "farm")
check("打怪中疲劳 700（没跌破下限 -800）→ 继续打怪，不切BOSS",
      r_h1._switch_to is None, str(r_h1._switch_to))
r_h2, _ = _run_combat([-900, -900], "farm")
check("打怪中疲劳 -900 < 下限 -800 → 切去打BOSS",
      r_h2._switch_to == "boss", str(r_h2._switch_to))
r_h3, _ = _run_combat([700, 700], "boss")
check("打BOSS中疲劳 700（没超过上限 800）→ 继续打，不收尾",
      r_h3._switch_to is None, str(r_h3._switch_to))
r_h4, _ = _run_combat([900, 900], "boss")
check("打BOSS中疲劳 900 > 上限 800 → 切回打怪",
      r_h4._switch_to == "farm", str(r_h4._switch_to))

# ---- 点击时屏蔽外部鼠标键盘 ----
import inspect as _inspect

check("默认开启「点击时屏蔽输入」", df.DEFAULT_PROFILE.get("block_input") is True)
check("block_user_input(False) 可安全调用（不会卡住输入）",
      df.block_user_input(False) in (True, False))
_grab_src = _inspect.getsource(df.WinGame._grab_click)
check("真实点击前后有屏蔽/恢复输入",
      "block_user_input(True)" in _grab_src
      and "block_user_input(False)" in _grab_src)

# ---- 多开不互相卡节奏：OCR 抢不到锁就跳过，不挡攻击 ----
check("能识别「多开的兄弟窗口」标题",
      df.is_game_title("CreatureCurios - 冰牙鲨鱼") is True
      and df.is_game_title("Chrome") is False
      and df.is_game_title("CreatureCurios 后台按键器") is True)

p_mo = build_profile()
p_mo["farm"]["boss_hp"] = {"roi": ROI_BOSS_HP, "mode": "ocr", "kill_hp": 0,
                           "miss": 2, "ocr_every": 0.0, "grace": 0.0}
g_mo = FakeGame()
g_mo.state["body"] = True
r_mo = df.DungeonRunner(g_mo, p_mo, log=lambda _s: None)
_calls = {"n": 0}
_real_ocr2 = df.app.ocr_text_from_bgra


def _counting_ocr(data, w, h, rect):
    _calls["n"] += 1
    return "冰牙鲨 18000/18000"


df.app.ocr_text_from_bgra = _counting_ocr
try:
    # 先正常读一次
    r_mo._hp_ocr_at = -1.0
    check("OCR 正常时能读到血量", r_mo.boss_hp_visible() is True)
    _before = _calls["n"]
    # 另一个窗口（另一个线程）占着 OCR 锁
    _held = threading.Event()
    _free = threading.Event()

    def _holder():
        df._OCR_LOCK.acquire()
        _held.set()
        _free.wait(3.0)
        df._OCR_LOCK.release()

    _ht = threading.Thread(target=_holder, daemon=True)
    _ht.start()
    _held.wait(2.0)
    try:
        r_mo._hp_ocr_at = -1.0
        _t0 = g_mo.t
        got = r_mo.boss_hp_visible()
        _dt = g_mo.t - _t0
    finally:
        _free.set()
        _ht.join(2.0)
    check("别的窗口在 OCR 时：立刻返回、不调用 OCR",
          got is True and _calls["n"] == _before, f"calls={_calls['n']}")
    check("跳过时不会卡住（耗时≈0）", _dt < 0.2, f"{_dt:.2f}s")
    check("跳过时打了标记（外层据此不记漏读）", r_mo._hp_skipped is True)
    r_mo._hp_ocr_at = -1.0
    r_mo.boss_hp_visible()
    check("正常读到后跳过标记清掉", r_mo._hp_skipped is False)
finally:
    df.app.ocr_text_from_bgra = _real_ocr2

# ---- 打怪期间完全不碰鼠标（只发后台按键）----
class _InputSpy(FakeGame):
    def __init__(self):
        super().__init__()
        self.kinds = []

    def tap(self, name):
        self.kinds.append(("tap", name))
        return super().tap(name)

    def click_norm(self, point):
        self.kinds.append(("click",))
        return super().click_norm(point)


g_spy = _InputSpy()
r_spy = df.DungeonRunner(g_spy, build_profile(), log=lambda _s: None)
r_spy.hp_now = lambda: 5000
r_spy.combat(2.0, desc="打怪验证")
check("打怪期间一次鼠标点击都没有（鼠标在哪都能打）",
      not any(k[0] == "click" for k in g_spy.kinds), str(g_spy.kinds[:6]))
check("打怪期间在发 A / 拾取空格",
      ("tap", "a") in g_spy.kinds and ("tap", "space") in g_spy.kinds,
      str(g_spy.kinds[:6]))

# ---- 打怪挂机走后台（不需要窗口在前台）----
class _FgGame(FakeGame):
    def __init__(self):
        super().__init__()
        self.pinned = 0
        self.restored = 0

    def pin_user_foreground(self):
        self.pinned += 1

    def restore_user_foreground(self, quiet=False):
        self.restored += 1


p_bg = build_profile()
p_bg["fatigue"] = {"enabled": True, "low": -600, "high": 600,
                   "pt": [0.1, 0.9], "roi": [0.4, 0.05, 0.2, 0.05]}
p_bg["loop"] = dict(p_bg["loop"], link_fatigue=True, fatigue_every=10.0,
                    default_action="farm")
p_bg["farm"]["farm_pt"] = [0.20, 0.30]
g_bg = _FgGame()
logs_bg = []
r_bg = df.DungeonRunner(g_bg, p_bg, log=logs_bg.append)
_bg_vals = [0, -700]
_bg_i = {"i": 0}


def _bg_fatigue():
    v = _bg_vals[min(_bg_i["i"], len(_bg_vals) - 1)]
    _bg_i["i"] += 1
    return v, 1000, ""


r_bg.fatigue_check = _bg_fatigue
r_bg.run_round()
check("打怪前记下你的窗口、到点后把前台还给你",
      g_bg.pinned >= 1 and g_bg.restored >= 1,
      f"pinned={g_bg.pinned} restored={g_bg.restored}")
check("打怪挂机日志写明后台发键",
      any("后台 PostMessage 发送" in l for l in logs_bg),
      str([l for l in logs_bg if "后台" in l]))
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
print("PASS: 副本引擎 v3.21（后台按键 + 连续挂机 + 抢占开关 + 喝药/仓库/联动）全部通过")
