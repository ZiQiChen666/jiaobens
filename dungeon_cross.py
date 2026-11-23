#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨图打BOSS：图2 挂机打怪 → 去图1 打BOSS → 回图2 打怪点

和「副本流程」是两套独立的功能，配置单独存在：
    %APPDATA%\\CreatureCuriosKeyer\\cross_profile.json

一轮 = 在图2 选中的「打怪点」挂机 → 疲劳 < 下限 就走「去程路径」到图1
      → 点图1 的 BOSS 点 → 打 BOSS（判死/喝药/拾取同副本流程）
      → 走「回程路径」回图2 → 回到选中的打怪点 → 继续挂机。
"""

from __future__ import annotations

import json
import os
from typing import Optional

import dungeon_flow as df

PROFILE_NAME = "cross_profile.json"


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def profile_path() -> str:
    return os.path.join(df.app._data_folder(), PROFILE_NAME)


def default_profile() -> dict:
    """一份「副本流程」兼容的配置 + 跨图专用的 cross 段。"""
    p = json.loads(json.dumps(df.DEFAULT_PROFILE))
    p["name"] = "跨图打BOSS"
    p["mount"]["enabled"] = False      # 跨图先不做骑乘检查（要的话可以打开）
    p["loop"]["boss_count"] = 1
    p["retry"] = {"rounds": 0, "gap": 5.0, "max_fail": 3}
    p["cross"] = {
        # 图2 上的打怪点（可以多个，界面上用下拉框切换）
        "farm_points": [
            {"name": "打怪点1", "pt": None, "travel_wait": 20.0},
        ],
        "farm_index": 0,
        # 图2 → 图1 的路径（按顺序执行）
        "go_steps": [],
        # 图1 → 图2 的路径（按顺序执行，跑完自动点选中的打怪点）
        "back_steps": [],
        "back_to_farm": True,
        "auto_enter": True,            # 到图1 后自动「进入副本」
        "auto_exit": True,             # 打完 BOSS 自动「退出副本」
        "close_map_after_click": True, # 点完地图点位顺手把地图关掉
        "close_key": "esc",            # 关地图用的键
        "map_key": "m",                # 开图键（和副本流程一致）
    }
    return p


def _norm_steps(steps) -> list:
    out = []
    for st in (steps or []):
        if not isinstance(st, dict):
            continue
        typ = str(st.get("type") or "").lower()
        try:
            wait = max(0.0, float(st.get("wait", 0.0) or 0.0))
        except (TypeError, ValueError):
            wait = 0.0
        if typ == "click":
            pt = st.get("pt")
            if not pt or len(pt) < 2:
                continue
            out.append({
                "type": "click",
                "pt": [round(float(pt[0]), 6), round(float(pt[1]), 6)],
                "wait": wait,
                "open_map": bool(st.get("open_map", True)),
                "close_map": bool(st.get("close_map", True)),
            })
        elif typ == "key":
            name = str(st.get("key") or "").strip()
            if not name:
                continue
            out.append({"type": "key", "key": name, "wait": wait})
        elif typ in ("enter", "exit"):
            out.append({"type": typ, "wait": wait})
    return out


def normalize(profile: dict) -> dict:
    """补齐/校验 cross 段，并走一遍副本流程的归一化（阈值/判死参数）。"""
    p = profile if isinstance(profile, dict) else {}
    crop = p.setdefault("cross", {})
    if not isinstance(crop, dict):
        crop = {}
        p["cross"] = crop

    points = []
    for item in (crop.get("farm_points") or []):
        if not isinstance(item, dict):
            continue
        pt = item.get("pt")
        try:
            pt = ([round(float(pt[0]), 6), round(float(pt[1]), 6)]
                  if pt and len(pt) >= 2 else None)
        except (TypeError, ValueError):
            pt = None
        try:
            travel = max(1.0, float(item.get("travel_wait", 20.0) or 20.0))
        except (TypeError, ValueError):
            travel = 20.0
        points.append({
            "name": str(item.get("name") or f"打怪点{len(points) + 1}"),
            "pt": pt,
            "travel_wait": travel,
        })
    crop["farm_points"] = points or [
        {"name": "打怪点1", "pt": None, "travel_wait": 20.0}
    ]
    try:
        idx = int(crop.get("farm_index", 0) or 0)
    except (TypeError, ValueError):
        idx = 0
    crop["farm_index"] = max(0, min(idx, len(crop["farm_points"]) - 1))
    crop["go_steps"] = _norm_steps(crop.get("go_steps"))
    crop["back_steps"] = _norm_steps(crop.get("back_steps"))
    crop["back_to_farm"] = bool(crop.get("back_to_farm", True))
    crop["auto_enter"] = bool(crop.get("auto_enter", True))
    crop["auto_exit"] = bool(crop.get("auto_exit", True))
    crop["close_map_after_click"] = bool(crop.get("close_map_after_click", True))
    crop["close_key"] = str(crop.get("close_key") or "esc").strip() or "esc"
    crop["map_key"] = str(crop.get("map_key") or "m").strip() or "m"
    return df._normalize_profile(p)


def load_profile(path: Optional[str] = None) -> dict:
    path = path or profile_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return normalize(default_profile())
    except Exception as exc:  # pragma: no cover
        print(f"[警告] 读取跨图标定失败：{type(exc).__name__}: {exc}")
        return normalize(default_profile())
    return normalize(df._merge(default_profile(), data))


def save_profile(profile: dict, path: Optional[str] = None) -> str:
    path = path or profile_path()
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, ensure_ascii=False, indent=2)
    return path


def missing_items(profile: dict) -> list[str]:
    """开跑前检查：缺什么直接告诉用户。"""
    p = profile or {}
    farm = p.get("farm") or {}
    crop = p.get("cross") or {}
    miss = []
    points = [x for x in (crop.get("farm_points") or [])
              if isinstance(x, dict) and x.get("pt")]
    if not points:
        miss.append("① 图2 打怪点（至少标一个）")
    if not crop.get("go_steps"):
        miss.append("② 去程路径（图2 → 图1）")
    if not farm.get("boss_pt"):
        miss.append("④ 图1 的 BOSS 点")
    steps = list(crop.get("go_steps") or []) + list(crop.get("back_steps") or [])
    need_enter = bool(crop.get("auto_enter", True)) or         any(s.get("type") == "enter" for s in steps)
    need_exit = bool(crop.get("auto_exit", True)) or         any(s.get("type") == "exit" for s in steps)
    enter = p.get("enter") or {}
    exit_cfg = p.get("exit") or {}
    if need_enter:
        if not enter.get("dungeon_pt"):
            miss.append("③ 图1 副本入口点")
        if not enter.get("confirm_pt"):
            miss.append("③ 进本弹窗「进入游戏」按钮")
    if need_exit:
        if not exit_cfg.get("exit_pt"):
            miss.append("③「退出副本」按钮")
        if not exit_cfg.get("confirm_pt"):
            miss.append("③「确认」按钮")
    if not crop.get("back_steps"):
        miss.append("⑤ 回程路径（图1 → 图2）")
    fatigue = p.get("fatigue") or {}
    if fatigue.get("enabled", True):
        has_roi = bool(fatigue.get("roi"))
        if not has_roi:
            try:
                has_roi = bool(df.app.load_fatigue_roi_norm())
            except Exception:
                has_roi = False
        if not has_roi:
            miss.append("⑥ 疲劳提示区域（框一下，或者主界面框过）")
        if not fatigue.get("pt"):
            miss.append("⑥ 疲劳图标位置（点一下那个图标）")
    return miss


# ---------------------------------------------------------------------------
# 流程
# ---------------------------------------------------------------------------

class CrossRunner(df.DungeonRunner):
    """图2 挂机 → 图1 打BOSS → 回图2 打怪点。其余能力全部复用副本流程。"""

    def __init__(self, game, profile: dict, log=None,
                 stop_event=None, verbose: bool = False):
        super().__init__(game, profile, log=log,
                         stop_event=stop_event, verbose=verbose)
        self._at_farm = False

    # ---- 小工具 ----
    def cross(self) -> dict:
        return self.p.get("cross") or {}

    def map_key(self) -> str:
        return str(self.cross().get("map_key") or "m")

    def farm_points(self) -> list:
        return [x for x in (self.cross().get("farm_points") or [])
                if isinstance(x, dict)]

    def current_farm_point(self) -> Optional[dict]:
        points = self.farm_points()
        if not points:
            return None
        try:
            idx = int(self.cross().get("farm_index", 0) or 0)
        except (TypeError, ValueError):
            idx = 0
        return points[max(0, min(idx, len(points) - 1))]

    def open_map(self) -> None:            # 用跨图自己的开图键
        key = self.map_key()
        self.log(f"按「{key}」开地图")
        self.game.tap(key)
        self.game.sleep(0.8)

    def close_map(self, force: bool = False) -> None:
        """点完点位把地图关掉（不然一直挂着地图，人物不走/点不到东西）。"""
        crop = self.cross()
        if not force and not bool(crop.get("close_map_after_click", True)):
            return
        key = str(crop.get("close_key") or "esc")
        self.game.tap(key)
        self.game.sleep(0.35)

    def after_map_click(self) -> None:
        self.close_map()

    # ---- 路径 ----
    def run_steps(self, steps, desc: str) -> bool:
        """按顺序执行路径：点击（可先开地图）或按键，每步后面等 wait 秒。"""
        steps = [s for s in (steps or []) if isinstance(s, dict)]
        if not steps:
            self.log(f"{desc}：没配路径，直接往下走")
            return True
        total = len(steps)
        for i, st in enumerate(steps, 1):
            if self._stopping():
                return False
            typ = str(st.get("type") or "")
            try:
                wait = max(0.0, float(st.get("wait", 0.0) or 0.0))
            except (TypeError, ValueError):
                wait = 0.0
            if typ == "click":
                pt = st.get("pt")
                if not pt:
                    self.log(f"{desc} 第 {i}/{total} 步：没点位，跳过")
                    continue
                self.log(f"{desc} 第 {i}/{total} 步：点地图点 "
                         f"({float(pt[0]):.3f}, {float(pt[1]):.3f}) → 等 {wait:.0f}s")
                if bool(st.get("open_map", True)):
                    self.open_map()
                if not self.game.click_norm(pt):
                    self.log(f"{desc} 第 {i} 步：点击失败")
                    return False
                self.release_focus()
                if bool(st.get("open_map", True)) and bool(st.get("close_map", True)):
                    self.close_map()
            elif typ == "key":
                key = str(st.get("key") or "").strip()
                if not key:
                    self.log(f"{desc} 第 {i}/{total} 步：没填按键，跳过")
                    continue
                self.log(f"{desc} 第 {i}/{total} 步：按「{key}」→ 等 {wait:.1f}s")
                self.game.tap(key)
            elif typ == "enter":
                self.log(f"{desc} 第 {i}/{total} 步：进入副本")
                if not self.enter_dungeon():
                    self.log(f"{desc} 第 {i} 步：进入副本失败")
                    return False
            elif typ == "exit":
                self.log(f"{desc} 第 {i}/{total} 步：退出副本")
                if not self.exit_dungeon():
                    self.log(f"{desc} 第 {i} 步：退出副本失败")
                    return False
            else:
                continue
            if wait > 0:
                self.game.sleep(wait)
        return True

    # ---- ① 图2 挂机 ----
    def farm_loop(self, already_there: bool = False) -> bool:
        point = self.current_farm_point()
        if not point or not point.get("pt"):
            self.log("没标「图2 打怪点」→ 没法挂机")
            return False
        name = str(point.get("name") or "打怪点")
        travel = float(point.get("travel_wait", 20.0) or 20.0)
        loop = self.p.get("loop") or {}
        every = max(10.0, float(loop.get("fatigue_every", 90.0) or 90.0))
        if already_there:
            self.log(f"—— 图2 挂机：已经在「{name}」（每 {every:.0f}s 查疲劳）——")
        else:
            self.log(f"—— 图2 挂机：去「{name}」（等寻路 {travel:.0f}s，"
                     f"每 {every:.0f}s 查疲劳）——")
            if not self._goto(point.get("pt"), travel, f"打怪点「{name}」"):
                return False
            self.gap("到打怪点后缓冲")
        self.release_focus()
        self.restore_user_foreground()
        self.log("图2 挂机：A/技能/拾取全部后台 PostMessage 发送"
                 "（窗口不用在前台，鼠标也不用在游戏里）")
        low, high = self.fatigue_limits()
        while not self._stopping():
            self.combat(every, desc=f"图2挂机（{name}）", action="farm")
            if self._stopping():
                return True
            if self._switch_to == "boss":
                self._switch_to = None
                self.log(f"疲劳 < 下限 {low} → 准备去图1打BOSS")
                return True
        return True

    # ---- ②③ 去图1 打BOSS + ④ 回图2 ----
    def trip_to_boss(self) -> bool:
        crop = self.cross()
        loop = self.p.get("loop") or {}
        kills = max(1, min(9, int(loop.get("boss_count", 1) or 1)))
        go_steps = list(crop.get("go_steps") or [])
        back_steps = list(crop.get("back_steps") or [])
        has_enter = any(str(s.get("type")) == "enter"
                        for s in go_steps + back_steps)
        has_exit = any(str(s.get("type")) == "exit"
                       for s in go_steps + back_steps)
        self.log("=== 图2 → 图1：走「去程路径」===")
        if not self.run_steps(go_steps, "去程"):
            return self._failed("去程路径失败")
        if bool(crop.get("auto_enter", True)) and not has_enter:
            self.log("=== 到图1：自动进入副本 ===")
            if not self.enter_dungeon():
                return self._failed("进入副本失败")
        self.log("=== 进本后：去打 BOSS ===")
        if not self._fight_clones(kills):
            return self._failed("图1 打BOSS失败")
        self.loot_before_exit()
        if bool(crop.get("auto_exit", True)) and not has_exit:
            self.log("=== 打完：自动退出副本 ===")
            if not self.exit_dungeon():
                return self._failed("退出副本失败")
        self.log("=== 图1 → 图2：走「回程路径」===")
        if not self.run_steps(back_steps, "回程"):
            return self._failed("回程路径失败")
        if bool(crop.get("back_to_farm", True)):
            point = self.current_farm_point()
            if point and point.get("pt"):
                name = str(point.get("name") or "打怪点")
                travel = float(point.get("travel_wait", 20.0) or 20.0)
                self.log(f"回到打怪点「{name}」（等寻路 {travel:.0f}s）")
                if not self._goto(point.get("pt"), travel,
                                  f"打怪点「{name}」"):
                    return self._failed("回打怪点失败")
                self.gap("到打怪点后缓冲")
                self._at_farm = True
        return True

    # ---- 试跑路径（只走路径，不打怪）----
    def test_path(self, which: str = "go") -> bool:
        crop = self.cross()
        which = "back" if str(which) == "back" else "go"
        if which == "go":
            self.log("=== 试跑去程（图2 → 图1）===")
            steps = crop.get("go_steps") or []
            if not self.run_steps(steps, "去程"):
                return self._failed("试跑去程失败")
            has_enter = any(str(s.get("type")) == "enter" for s in steps)
            if bool(crop.get("auto_enter", True)) and not has_enter:
                self.log("=== 自动进入副本 ===")
                if not self.enter_dungeon():
                    return self._failed("试跑：进入副本失败")
            self.log("试跑去程完成（走到图1、进本完成，不自动打怪）")
            return True
        self.log("=== 试跑回程（图1 → 图2）===")
        steps = crop.get("back_steps") or []
        has_exit = any(str(s.get("type")) == "exit" for s in steps)
        if bool(crop.get("auto_exit", True)) and not has_exit:
            self.log("=== 自动退出副本 ===")
            if not self.exit_dungeon():
                return self._failed("试跑：退出副本失败")
        if not self.run_steps(steps, "回程"):
            return self._failed("试跑回程失败")
        if bool(crop.get("back_to_farm", True)):
            point = self.current_farm_point()
            if point and point.get("pt"):
                name = str(point.get("name") or "打怪点")
                travel = float(point.get("travel_wait", 20.0) or 20.0)
                self.log(f"回到打怪点「{name}」（等寻路 {travel:.0f}s）")
                if not self._goto(point.get("pt"), travel, f"打怪点「{name}」"):
                    return self._failed("试跑：回打怪点失败")
                self.gap("到打怪点后缓冲")
        self.log("试跑回程完成（回到图2 打怪点）")
        return True

    # ---- 一轮 ----
    def run_round(self, seconds: Optional[float] = None) -> bool:  # noqa: D401
        self.log("=== 开始一轮：图2挂机 → 图1打BOSS → 回图2 ===")
        pin = getattr(self.game, "pin_user_foreground", None)
        if callable(pin):
            try:
                pin()
            except Exception:
                pass
        if not self.farm_loop(already_there=self._at_farm):
            return self._failed("图2挂机失败")
        self._at_farm = False
        if self._stopping():
            return True
        if not self.trip_to_boss():
            return self._failed("去图1打BOSS失败")
        return True
