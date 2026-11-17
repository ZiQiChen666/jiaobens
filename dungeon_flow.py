#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""副本流程引擎：地图1 进本 → 地图2 BOSS 点挂机 → 退出副本

一轮流程（就 3 步，没有乱七八糟的分支）：

    ① 进本：开地图1 → 点「副本入口」点位 → 等自动寻路 →
       检测「进本弹窗」出现 → 点「进入游戏」按钮
    ② 挂机：开地图2 → 点「BOSS 点」点位 → 等自动寻路 →
       挂机 N 秒（可每个多开单独设）：平A + 技能 + 血量低喝药(默认 6s 间隔) + 定时拾取
    ③ 退出：点「退出副本」→ 点「确认」→ 回地图1

特点：
    * 全程 PostMessage 后台点击/按键，不移动真实鼠标、不抢前台；
    * 弹窗识别用「锚点相似度」，不需要 OCR、不吃 CPU，多开也不卡；
    * 血量用 OCR（只在配了血量区域时启用）；
    * 失败/超时会存截图快照，便于回溯。

用法：
    python dungeon_flow.py --list
    python dungeon_flow.py --calibrate --window CreatureCurios
    python dungeon_flow.py --run --window CreatureCurios --rounds 3
    python dungeon_flow.py --run --window CreatureCurios --farm-seconds 90
    python dungeon_flow.py --probe --window CreatureCurios --seconds 5
"""

from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import sys
import time
from typing import Callable, Optional

import numpy as np

import creature_curios_keyer as app

try:
    import win32api
    import win32con
    import win32gui
except ImportError:  # pragma: no cover
    raise SystemExit("缺少 pywin32：pip install pywin32")


PROFILE_NAME = "dungeon_profile.json"
SNAPSHOT_DIR_NAME = "runs"
DUNGEON_DIR_NAME = "dungeons"
ACTIVE_NAME_FILE = "dungeon_active.txt"


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULT_PROFILE: dict = {
    "name": "默认副本",
    "window_title": "CreatureCurios",
    "keys": {
        "open_map": "m",     # 开地图键（地图1/地图2 都用它）
        "pickup": "space",   # 拾取键
    },
    "input_mode": "foreground",  # foreground=切到游戏窗口用真实鼠标键盘（宏）；background=后台消息
    "prime_activate": True,   # 后台模式下补发 WM_ACTIVATE/焦点消息
    "action_gap": 1.0,        # 动作之间的缓冲（秒）
    "enter": {
        "dungeon_pt": None,        # 地图1 上的副本入口 [nx, ny]
        "travel_wait": 40.0,       # 走路/寻路到副本口要多久
        "dialog": {"roi": None, "sig": [], "grid": 12, "sim": 0.90},
        "confirm_pt": None,        # 弹窗里「进入游戏」按钮 [nx, ny]
        "dialog_timeout": 24.0,    # 点完副本入口后，这么久还没弹窗就重新寻路
        "loaded": {"roi": None, "sig": [], "grid": 12, "sim": 0.90},  # 可选：进本后特征
        "progress": {"roi": None, "min_fill": 0.90, "samples": 3,
                     "interval": 0.25},  # 加载进度条区域 + 满多少算加载完
        "blind_confirm_after": 3.0,  # 这么久还没识别到弹窗 → 先盲点一次确认
        "load_wait": 10.0,         # 点完确认后的加载时间
        "retry": 3,                # 没识别到弹窗 → 重新开图重点，最多几次
    },
    "mount": {
        "enabled": True,           # 每次寻路前按一下 S，检查宠物是否骑乘
        "key": "s",
        "dialog": {"roi": None, "sig": [], "grid": 12, "sim": 0.90},  # 骑乘弹窗
        "text_roi": None,          # 宠物文本区域（OCR：未骑乘写「骑乘」，已骑乘写「休息」）
        "mount_text": "骑乘",      # 文本里出现它 = 还没骑，需要点
        "rest_text": "休息",       # 文本里出现它 = 已经骑了，不用点
        "state": {"roi": None, "sig": [], "grid": 12, "sim": 0.90},   # 旧的：图形标记（可不标）
        "mount_pt": None,          # 未骑乘时点这个「骑乘」按钮
        "wait": 4.0,               # 等骑乘弹窗出现
        "settle": 1.2,             # 点完骑乘等多久
        "close_key": "",           # 关弹窗的键（空 = 再按一次 key）
    },
    "farm": {
        "farm_pt": None,           # 地图2 上的「打怪点」[nx, ny]（在这里判断疲劳）
        "boss_pt": None,           # 地图2 上的 BOSS 点 [nx, ny]
        "travel_wait": 30.0,       # 点完后等自动寻路（有「到达标记」就提前结束）
        "arrived": {"roi": None, "sig": [], "grid": 12, "sim": 0.90},  # 可选：到达标记
        "boss_present": {"roi": None, "sig": [], "grid": 12, "sim": 0.88},  # BOSS 身形
        "boss_hp": {"roi": None, "min_ink": 0.02, "miss": 3},               # BOSS 血条
        "finish": {"roi": None, "sig": [], "grid": 12, "sim": 0.90},        # 旧版，已不用
        "boss_retry": 3,           # 没识别到「打怪点BOSS」就重新寻路几次
        "pickup_on_a": True,       # 每次平A后都按一次拾取
        "seconds": 30.0,           # 挂机多久（多开时每个窗口可不同）
        "interval_ms": 250,
        "use_skills": True,
        "skill1": "1", "skill1_every": 12.0,
        "skill2": "2", "skill2_every": 6.0,
        "hp_roi": None,            # 血量数字区域（框了才按血量喝药）
        "hp_threshold": 1600,
        "hp_check_every": 2.0,
        "potion_keys": ["q", "w"],
        "potion_every": 6.0,       # 喝药间隔（低于阈值时每 6 秒喝一次）
        "pickup_every": 8.0,       # 挂机期间每隔几秒按一次拾取
    },
    "loop": {
        "fatigue_every": 60.0,     # 打怪/打BOSS 期间每隔几秒查一次疲劳（0=不查）
        "max_switch": 3,           # 疲劳变化导致换分支，最多切几次（防来回跳）
        "trash_seconds": 60.0,     # 打怪循环：打多久
        "trash_rounds": 1,         # 打怪循环：打几轮
        "boss_seconds": 30.0,      # BOSS 循环：每只打多久
        "boss_rounds": 1,          # BOSS 循环：打几只
        "default_action": "boss",  # 疲劳在中间区间时做哪个：boss / farm
    },
    "exit": {
        "exit_pt": None,           # 「退出副本」按钮（第一下）[nx, ny]
        "confirm_pt": None,        # 二次确认弹窗的「确认」按钮（第二下）[nx, ny]
        # 退出确认弹窗的识别区域（和进本弹窗分开，各框各的）
        "dialog": {"roi": None, "sig": [], "grid": 12, "sim": 0.90},
        "dialog_timeout": 10.0,    # 等退出确认弹窗最多多久
        "retry": 3,                # 没识别到 → 再点一次「退出副本」
        "exit_wait": 1.0,          # 点完第一下等弹窗出现
        "confirm_wait": 1.0,       # 点完确认等
        "settle": 10.0,            # 退出副本的加载时间
    },
    "fatigue": {
        "enabled": True,           # 每轮退出副本后检测一次疲劳
        "pt": None,                # 疲劳图标点位 [nx, ny]（点一下弹提示）
        "roi": None,               # 疲劳提示文字区域；空则用主界面已框的
        "threshold": 800,          # 当前疲劳 < 这个值就算「疲劳不足」
        "farm_seconds": 300.0,     # 疲劳不足时普通挂机打怪多久
    },
    "detect": {"poll": 0.5},
    "retry": {"rounds": 0, "gap": 8.0, "max_fail": 3},
}


def _base_dir() -> str:
    """标定数据根目录（测试里可以替换）。"""
    return app._data_folder()


def profile_path() -> str:
    return os.path.join(_base_dir(), PROFILE_NAME)


# ---------------------------------------------------------------------------
# 副本方案：命名保存 / 切换 / 删除
# ---------------------------------------------------------------------------


def dungeons_dir() -> str:
    path = os.path.join(_base_dir(), DUNGEON_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _safe_name(name: str) -> str:
    clean = "".join(
        ch for ch in str(name or "").strip()
        if ch not in '\\/:*?"<>|' and ch not in "\r\n\t"
    ).strip()
    return clean[:40]


def dungeon_file(name: str) -> str:
    return os.path.join(dungeons_dir(), f"{_safe_name(name)}.json")


def list_dungeons() -> list[str]:
    try:
        names = [
            os.path.splitext(f)[0]
            for f in os.listdir(dungeons_dir())
            if f.lower().endswith(".json")
        ]
    except Exception:
        return []
    return sorted(names)


def active_name() -> str:
    try:
        with open(os.path.join(_base_dir(), ACTIVE_NAME_FILE),
                  "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def set_active(name: str) -> None:
    try:
        with open(os.path.join(_base_dir(), ACTIVE_NAME_FILE),
                  "w", encoding="utf-8") as fh:
            fh.write(str(name or "").strip())
    except Exception:
        pass


def save_dungeon(name: str, profile: dict) -> str:
    name = _safe_name(name)
    if not name:
        raise ValueError("名字不能为空")
    path = dungeon_file(name)
    save_profile(profile, path)
    set_active(name)
    return path


def load_dungeon(name: str) -> Optional[dict]:
    name = _safe_name(name)
    if not name:
        return None
    path = dungeon_file(name)
    if not os.path.exists(path):
        return None
    return load_profile(path)


def delete_dungeon(name: str) -> bool:
    name = _safe_name(name)
    if not name:
        return False
    path = dungeon_file(name)
    try:
        os.remove(path)
    except OSError:
        return False
    if active_name() == name:
        set_active("")
    return True


def _merge(base: dict, over: dict) -> dict:
    """深拷贝后再合并：避免改到结果里的嵌套字典时污染默认配置。"""
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_profile(path: Optional[str] = None) -> dict:
    if not path:
        name = active_name()
        if name:
            candidate = dungeon_file(name)
            if os.path.exists(candidate):
                path = candidate
    path = path or profile_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return json.loads(json.dumps(DEFAULT_PROFILE))
    except Exception as exc:
        print(f"[警告] 读取标定失败：{type(exc).__name__}: {exc}")
        return json.loads(json.dumps(DEFAULT_PROFILE))
    return _merge(DEFAULT_PROFILE, data)


def save_profile(profile: dict, path: Optional[str] = None) -> str:
    path = path or profile_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, ensure_ascii=False, indent=2)
    return path


def copy_profile_for_slot(profile: dict, farm_seconds: float) -> dict:
    """多开：每个窗口复制一份，只改挂机时长。"""
    p = json.loads(json.dumps(profile))
    p.setdefault("farm", {})["seconds"] = float(farm_seconds)
    return p


# ---------------------------------------------------------------------------
# 判据（纯计算，方便单测）
# ---------------------------------------------------------------------------


def frame_array(frame: tuple[int, int, bytes]) -> np.ndarray:
    w, h, data = frame
    return np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)


def crop_norm(frame: tuple[int, int, bytes], roi_norm) -> np.ndarray:
    w, h, _data = frame
    x, y, rw, rh = app.roi_from_norm(
        (float(roi_norm[0]), float(roi_norm[1]),
         float(roi_norm[2]), float(roi_norm[3])), w, h,
    )
    return frame_array(frame)[y:y + rh, x:x + rw, :3]


def ink_ratio(patch: np.ndarray, tol: int = 36) -> float:
    """区域里「不像背景」的像素占比（外圈像素当背景参考）。"""
    if patch.size == 0:
        return 0.0
    border = np.concatenate([
        patch[0, :, :].reshape(-1, 3),
        patch[-1, :, :].reshape(-1, 3),
        patch[:, 0, :].reshape(-1, 3),
        patch[:, -1, :].reshape(-1, 3),
    ]).astype(np.int16)
    bg = np.median(border, axis=0)
    flat = patch.reshape(-1, 3).astype(np.int16)
    dev = np.abs(flat - bg).max(axis=1)
    return float((dev > tol).mean())


def signature(patch: np.ndarray, grid: int = 12) -> list[float]:
    """把区域降采样成 grid×grid 灰度特征，用于锚点比对。"""
    if patch.size == 0:
        return []
    gray = patch.astype(np.float32).mean(axis=2)
    h, w = gray.shape
    gh = max(1, min(int(grid), h))
    gw = max(1, min(int(grid), w))
    ys = np.linspace(0, h, gh + 1).astype(int)
    xs = np.linspace(0, w, gw + 1).astype(int)
    out: list[float] = []
    for i in range(gh):
        y0, y1 = ys[i], max(ys[i] + 1, ys[i + 1])
        for j in range(gw):
            x0, x1 = xs[j], max(xs[j] + 1, xs[j + 1])
            out.append(round(float(gray[y0:y1, x0:x1].mean()), 2))
    return out


def similarity(a, b, tol: float = 18.0) -> float:
    """1.0 = 完全一样；按「变化格子比例」算，一小块图标出现/消失也能看出来。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    diff = np.abs(np.asarray(a, np.float32) - np.asarray(b, np.float32))
    return float(1.0 - (diff > tol).mean())


# ---------------------------------------------------------------------------
# 游戏窗口（后台 PostMessage）
# ---------------------------------------------------------------------------


class WinGame:
    """真实游戏窗口：后台截图 + 后台点击/按键，不动鼠标、不抢前台。"""

    def __init__(self, hwnd: int, profile: dict,
                 log: Callable[[str], None] = print, dry: bool = False,
                 snapshot_dir: Optional[str] = None, prefix: str = "",
                 stop_event=None):
        self.hwnd = int(hwnd)
        self.profile = profile
        self.log = log
        self.dry = bool(dry)
        self.snapshot_dir = snapshot_dir
        self.prefix = prefix
        self.stop_event = stop_event
        self.last_error = ""

    # -- 基础 --
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """可被 stop 立刻打断的 sleep（100ms 粒度）。"""
        remain = max(0.0, float(seconds))
        if remain <= 0:
            return
        while remain > 0:
            if self.stop_event is not None and self.stop_event.is_set():
                return
            step = min(0.1, remain)
            time.sleep(step)
            remain -= step

    def alive(self) -> bool:
        return bool(win32gui.IsWindow(self.hwnd))

    def minimized(self) -> bool:
        try:
            return bool(win32gui.IsIconic(self.hwnd))
        except Exception:
            return False

    # -- 截图 --
    def grab(self):
        cap, err = app.capture_client_bgra(self.hwnd)
        self.last_error = err or ""
        return cap

    def snapshot(self, tag: str) -> None:
        if not self.snapshot_dir:
            return
        cap = self.grab()
        if cap is None:
            return
        try:
            from PIL import Image
            w, h, data = cap
            img = Image.frombuffer(
                "RGBA", (w, h), bytes(data), "raw", "BGRA", 0, 1
            ).convert("RGB")
            os.makedirs(self.snapshot_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%H%M%S")
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)[:40]
            img.save(os.path.join(self.snapshot_dir, f"{stamp}_{safe}.png"))
        except Exception:
            pass

    # -- 前台模式 --
    def foreground_mode(self) -> bool:
        return str(self.profile.get("input_mode", "foreground")) != "background"

    def activate(self) -> bool:
        """把游戏窗口切到前台（前台模式必须）。"""
        if not self.foreground_mode() or self.dry:
            return True
        try:
            if int(win32gui.GetForegroundWindow() or 0) == self.hwnd:
                return True
        except Exception:
            pass
        try:
            if win32gui.IsIconic(self.hwnd):
                win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
                self.sleep(0.25)
        except Exception:
            pass
        ok = bool(app._force_foreground_window(self.hwnd))
        if not ok:
            self.sleep(0.2)
            ok = bool(app._force_foreground_window(self.hwnd))
        if not ok:
            self.log("警告：没能把游戏切到前台，本次不点击（避免点到别的窗口）")
        return ok

    def _client_to_screen(self, client_x: int, client_y: int):
        try:
            sx, sy = win32gui.ClientToScreen(self.hwnd, (int(client_x), int(client_y)))
            return int(sx), int(sy)
        except Exception:
            return 0, 0

    # -- 输入 --
    def _post(self, message: int, wparam: int, lparam: int) -> bool:
        try:
            win32api.PostMessage(self.hwnd, message, wparam, lparam)
            return True
        except Exception as exc:
            self.last_error = f"PostMessage 失败：{exc}"
            return False

    def click_norm(self, point) -> bool:
        if not point:
            return False
        try:
            _l, _t, cw, ch = win32gui.GetClientRect(self.hwnd)
        except Exception as exc:
            self.last_error = f"取客户区失败：{exc}"
            return False
        cw, ch = int(cw), int(ch)
        if cw <= 0 or ch <= 0:
            return False
        x = max(1, min(cw - 2, int(round(float(point[0]) * cw))))
        y = max(1, min(ch - 2, int(round(float(point[1]) * ch))))
        if self.dry:
            self.log(f"[DRY] click ({x},{y})")
            return True
        if self.foreground_mode():
            # 真实鼠标（宏方式）：切到游戏 → 移动光标 → 左键按下/松开
            if not self.activate():
                self.last_error = "游戏窗口不在前台，已跳过这次点击"
                return False
            sx, sy = self._client_to_screen(x, y)
            try:
                win32api.SetCursorPos((sx, sy))
                self.sleep(0.05)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                self.sleep(0.04)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                self.sleep(0.08)
                return True
            except Exception as exc:
                self.last_error = f"真实鼠标点击失败：{exc}"
                self.log(self.last_error)
                return False
        if self.profile.get("prime_activate", True):
            app.prime_background_input(self.hwnd, activate=True)
        ok = app.post_click_client(self.hwnd, x, y)
        if not ok:
            self.last_error = "后台点击发送失败"
        return ok

    def tap(self, name: str, hold: float = 0.05) -> bool:
        """后台投递按键（前台模式下也用投递，避免输入法把按键吃掉）。"""
        if not name:
            return False
        vk = app._vk_from_key_name(str(name))
        if vk is None:
            self.log(f"[警告] 不认识的按键名：{name}")
            return False
        if self.dry:
            return True
        try:
            scan = int(win32api.MapVirtualKey(vk, 0)) & 0xFF
        except Exception:
            scan = 0
        # 按键一律走 PostMessage：真实 keybd_event 会被中文输入法截成
        # VK_PROCESSKEY(229)，游戏就收不到 A / M 了；点击才需要真实鼠标。
        if self.profile.get("prime_activate", True):
            app.prime_background_input(self.hwnd, activate=True)
        down_lp = 1 | (scan << 16)
        up_lp = down_lp | 0xC0000000
        ok = self._post(win32con.WM_KEYDOWN, vk, down_lp)
        if hold > 0:
            self.sleep(hold)
        ok = self._post(win32con.WM_KEYUP, vk, up_lp) and ok
        return ok


# ---------------------------------------------------------------------------
# 流程
# ---------------------------------------------------------------------------


class DungeonRunner:
    """三步流程：进本 → BOSS 点挂机 → 退出。"""

    def __init__(self, game, profile: dict,
                 log: Optional[Callable[[str], None]] = None,
                 stop_event=None, verbose: bool = False):
        self.game = game
        self.p = profile
        self._log = log or print
        self.stop_event = stop_event
        self.verbose = bool(verbose)
        self.stats = {"ok": 0, "fail": 0, "failed_streak": 0}
        self.stopped = False
        self._last_frame = None
        self._switch_to: Optional[str] = None

    # -- 日志 / 工具 --
    def log(self, text: str) -> None:
        prefix = getattr(self.game, "prefix", "") or ""
        self._log(f"{prefix}[副本] {text}")

    def _stopping(self) -> bool:
        if self.stop_event is not None and self.stop_event.is_set():
            self.stopped = True
        return self.stopped

    def _sec(self, group: str, key: str, default: float) -> float:
        return float((self.p.get(group) or {}).get(key, default))

    def _key(self, name: str, default: str = "") -> str:
        return str((self.p.get("keys") or {}).get(name, default) or default)

    def _farm(self) -> dict:
        return self.p.get("farm") or {}

    # -- 画面 --
    def grab(self):
        frame = self.game.grab()
        if frame is None:
            self.log(f"截图失败：{getattr(self.game, 'last_error', '')}")
            return None
        self._last_frame = frame
        return frame

    def anchor_visible(self, spec: dict, frame) -> bool:
        """通用：某个锚点区域现在是不是“和标定时一样”（相似度/占比）。"""
        if not isinstance(spec, dict):
            return False
        roi = spec.get("roi")
        if not roi or frame is None:
            return False
        patch = crop_norm(frame, roi)
        sig = spec.get("sig")
        if sig:
            return similarity(
                sig, signature(patch, int(spec.get("grid", 12)))
            ) >= float(spec.get("sim", 0.90))
        return ink_ratio(patch) > 0.15

    def enter_dialog_spec(self) -> dict:
        return ((self.p.get("enter") or {}).get("dialog") or {})

    def exit_dialog_spec(self) -> dict:
        return ((self.p.get("exit") or {}).get("dialog") or {})

    def dialog_visible(self, frame) -> bool:
        """进本弹窗是否出现（只认「进本」那份区域）。"""
        return self.anchor_visible(self.enter_dialog_spec(), frame)

    def exit_dialog_visible(self, frame) -> bool:
        """退出副本的确认弹窗是否出现（只认「退出」那份区域）。"""
        return self.anchor_visible(self.exit_dialog_spec(), frame)

    def wait_anchor(self, spec: dict, timeout: float, poll: float,
                    desc: str) -> bool:
        """等某个锚点出现（连续 2 帧命中就算）。"""
        if not (spec or {}).get("roi"):
            return False
        start = self.game.now()
        hits = 0
        while not self._stopping() and self.game.now() - start < timeout:
            frame = self.grab()
            if self.anchor_visible(spec, frame):
                hits += 1
                if hits >= 2:
                    self.log(f"识别到{desc}（{self.game.now() - start:.1f}s）")
                    return True
            else:
                hits = 0
            self.game.sleep(poll)
        return False

    def hp_now(self):
        """OCR 当前血量；没配区域返回 None。"""
        frame = self._last_frame or self.grab()
        roi = self._farm().get("hp_roi")
        if frame is None or not roi:
            return None
        w, h, data = frame
        try:
            rect = app.roi_from_norm(tuple(float(v) for v in roi), w, h)
            current, _maximum, _text = app.ocr_hp_from_bgra(data, w, h, rect)
            return current
        except Exception as exc:
            self.log(f"血量识别异常：{type(exc).__name__}: {exc}")
            return None

    def probe(self) -> dict:
        """给界面用：进本弹窗 / 退出弹窗 / 血量 分别报告。"""
        frame = self.grab()
        if frame is None:
            return {}
        out = {}
        for name, spec in (("enter", self.enter_dialog_spec()),
                           ("exit", self.exit_dialog_spec())):
            roi = (spec or {}).get("roi")
            if not roi:
                out[f"{name}_popup"] = "未标定"
                continue
            patch = crop_norm(frame, roi)
            visible = self.anchor_visible(spec, frame)
            sig = spec.get("sig")
            detail = f"ink={ink_ratio(patch):.3f}"
            if sig:
                detail += (f" sim={similarity(sig, signature(patch, int(spec.get('grid', 12)))):.3f}")
            out[f"{name}_popup"] = f"{'在' if visible else '不在'}({detail})"
        hp = self.hp_now()
        if hp is not None:
            out["hp"] = hp
        return out

    # -- 骑乘检查 --
    def mount_text_now(self) -> str:
        """OCR 宠物文本区域，返回识别到的文字（没标区域 / 失败返回空）。"""
        cfg = self.p.get("mount") or {}
        roi = cfg.get("text_roi")
        if not roi:
            return ""
        frame = self._last_frame or self.grab()
        if frame is None:
            return ""
        w, h, data = frame
        try:
            rect = app.roi_from_norm(tuple(float(v) for v in roi), w, h)
            return (app.ocr_text_from_bgra(data, w, h, rect) or "").strip()
        except Exception as exc:
            self.log(f"骑乘文本识别异常：{type(exc).__name__}: {exc}")
            return ""

    def mount_state(self):
        """返回 'mounted' / 'need_mount' / 'unknown'。

        规则（按你说的）：宠物文本上写「骑乘」= 还没骑，要点；
        写「休息」= 已经在骑，不用点。
        """
        cfg = self.p.get("mount") or {}
        need = str(cfg.get("mount_text") or "骑乘")
        rest = str(cfg.get("rest_text") or "休息")
        text = self.mount_text_now()
        if text:
            compact = "".join(text.split())
            self.log(f"宠物文本：{compact}")
            if rest and rest in compact:
                return "mounted"
            if need and need in compact:
                return "need_mount"
            self.log(f"文本「{compact}」里既没有「{need}」也没有「{rest}」，按未骑乘处理")
            return "need_mount"
        # 没框文本区域：退回旧的图形标记判断
        spec = cfg.get("state") or {}
        if (spec or {}).get("roi"):
            frame = self._last_frame or self.grab()
            if frame is not None and self.anchor_visible(spec, frame):
                return "mounted"
            return "need_mount"
        return "unknown"

    def mount_ok(self) -> bool:
        """True = 已经骑乘（文本是「休息」）。"""
        return self.mount_state() == "mounted"

    def ensure_mount(self) -> None:
        """每次寻路前按一下 S，看宠物文本：写「骑乘」就点一下，写「休息」就不动。"""
        cfg = self.p.get("mount") or {}
        if not cfg.get("enabled", True):
            return
        has_text = bool(cfg.get("text_roi"))
        has_anchor = bool((cfg.get("state") or {}).get("roi")
                          or (cfg.get("dialog") or {}).get("roi"))
        if not (has_text or has_anchor):
            self.log("没标骑乘文本区域，跳过骑乘检查")
            return

        key = str(cfg.get("key") or "s")
        close_key = str(cfg.get("close_key") or key)
        dialog = cfg.get("dialog") or {}
        poll = max(0.2, float((self.p.get("detect") or {}).get("poll", 0.5)))
        mount_pt = cfg.get("mount_pt")
        settle = float(cfg.get("settle", 1.2))

        self.log(f"按「{key}」打开骑乘面板，看宠物文本")
        self.game.tap(key)
        if (dialog or {}).get("roi"):
            if not self.wait_anchor(dialog, float(cfg.get("wait", 4.0)), poll,
                                    "骑乘弹窗"):
                self.log("没识别到骑乘弹窗，跳过骑乘检查")
                return
        else:
            self.game.sleep(1.0)

        state = self.mount_state()
        if state == "mounted":
            self.log("宠物文本是「休息」→ 已经骑乘，不用点")
        elif state == "need_mount":
            if mount_pt:
                self.log("宠物文本是「骑乘」→ 点一下骑乘")
                if not self.game.click_norm(mount_pt):
                    self.log("点「骑乘」失败（窗口可能不在前台）")
                self.game.sleep(settle)
                self.log("骑乘后："
                         + ("宠物文本是「休息」✓" if self.mount_ok()
                            else "文本还不是「休息」，下一轮再查"))
            else:
                self.log("文档是「骑乘」但没标「骑乘」按钮位置，无法自动骑")
        else:
            self.log("没标骑乘文本区域，跳过")

        self.game.tap(close_key)
        self.game.sleep(0.6)

    # -- 动作 --
    # -- 动作 --
    def open_map(self) -> None:
        key = self._key("open_map", "m")
        self.log(f"按「{key}」开地图")
        self.game.tap(key)
        self.game.sleep(0.8)

    def wait_until(self, spec: Optional[dict], timeout: float, desc: str,
                   need: int = 2) -> bool:
        """边等边看：区域内连续 need 次命中就立刻返回 True；超时返回 False。"""
        if not (spec or {}).get("roi"):
            return False
        poll = max(0.2, float((self.p.get("detect") or {}).get("poll", 0.5)))
        start = self.game.now()
        hits = 0
        while not self._stopping() and self.game.now() - start < timeout:
            frame = self.grab()
            if frame is not None and self.anchor_visible(spec, frame):
                hits += 1
                if hits >= max(1, int(need)):
                    self.log(f"识别到{desc}（{self.game.now() - start:.1f}s）")
                    return True
            else:
                hits = 0
            self.game.sleep(poll)
        return False

    def _goto(self, point, wait: float, desc: str,
              arrived_spec: Optional[dict] = None) -> bool:
        """点地图点位 → 等自动寻路。有「到达标记」就一到就走，不等满。"""
        if not point:
            self.log(f"「{desc}」没有标定，跳过")
            return False
        self.ensure_mount()
        self.log(f"点「{desc}」({float(point[0]):.3f}, {float(point[1]):.3f})"
                 f" → 等自动寻路（最多 {wait:.0f}s）")
        self.open_map()
        if not self.game.click_norm(point):
            self.log(f"点击「{desc}」失败")
            return False
        if (arrived_spec or {}).get("roi"):
            if self.wait_until(arrived_spec, wait, f"{desc}到达标记"):
                return True
            self.log(f"没识别到「{desc}」到达标记，按时间继续")
        else:
            self.game.sleep(wait)
        return True

    def _arrived_spec(self) -> dict:
        return (self._farm().get("arrived") or {})

    def _loaded_spec(self) -> dict:
        return ((self.p.get("enter") or {}).get("loaded") or {})

    def _progress_spec(self) -> dict:
        return ((self.p.get("enter") or {}).get("progress") or {})

    # ---- 动作缓冲 ----
    def gap(self, name: str = "动作缓冲") -> None:
        seconds = float(self.p.get("action_gap", 1.0) or 0.0)
        if seconds > 0:
            self.log(f"{name} {seconds:.1f}s")
            self.game.sleep(seconds)

    # ---- 加载进度条判定 ----
    def progress_fill(self) -> Optional[float]:
        """量一次进度条填充比例（0~1）。

        做法：把框住的条子按列扫一遍，某一列里超过 30% 的像素和背景色不同
        就算“这一列是亮的”，亮列占比 = 进度（横向进度条）。这样即使截图
        只截到半截、条子正好铺满整个框，也能量出 0.9x 这种数值。
        """
        spec = self._progress_spec()
        roi = spec.get("roi")
        if not roi:
            return None
        frame = self.grab()
        if frame is None:
            return 0.0
        try:
            patch = crop_norm(frame, roi)
            if patch.size == 0:
                return 0.0
            bg = patch[0, 0].astype(np.int16)
            dev = np.abs(patch.astype(np.int16) - bg).max(axis=2)  # H×W
            col_lit = (dev > 36).mean(axis=0) > 0.30               # 每列是否亮
            return float(col_lit.mean())
        except Exception:
            return 0.0

    def wait_progress(self, timeout: float) -> bool:
        """等进度条到 min_fill（默认 90%）；多采几帧取最大值，避免截图半截。"""
        spec = self._progress_spec()
        if not (spec or {}).get("roi"):
            return False
        min_fill = float(spec.get("min_fill", 0.90) or 0.90)
        samples = max(1, int(spec.get("samples", 3) or 1))
        interval = max(0.05, float(spec.get("interval", 0.25) or 0.25))
        poll = max(0.2, float((self.p.get("detect") or {}).get("poll", 0.5)))
        start = self.game.now()
        seen_bar = False
        best = 0.0
        while not self._stopping() and self.game.now() - start < timeout:
            fill = self.progress_fill()
            if fill is not None:
                if fill > 0.05:
                    seen_bar = True
                # 连采几帧取最大，避免刚好截到半截进度条
                for _ in range(samples - 1):
                    self.game.sleep(interval)
                    more = self.progress_fill()
                    if more is not None:
                        fill = max(fill, more)
                best = max(best, fill)
                if fill >= min_fill:
                    self.log(f"进度条 {fill * 100:.0f}% ≥ {min_fill * 100:.0f}% → 加载完成")
                    return True
                self.log(f"进度条 {fill * 100:.0f}%…（等加载）")
            self.game.sleep(poll)
        if not seen_bar:
            self.log("没看到加载进度条 → 认为没有进入副本")
        else:
            self.log(f"进度条最高只有 {best * 100:.0f}%，没到 "
                     f"{min_fill * 100:.0f}% → 也不算加载完成")
        return False

    def enter_dungeon(self) -> bool:
        """① 点副本入口 → 立刻盯弹窗（出现就点）→ 进度条 ≥90% 才算进本。

        识别不到弹窗时不会傻等：过了 blind_confirm_after 秒就先盲点一次
        「进入游戏」，再看有没有进度条；进度条出来了就说明确实进本了。
        """
        enter = self.p.get("enter") or {}
        farm = self._farm()
        confirm_pt = enter.get("confirm_pt")
        travel = float(enter.get("travel_wait", 40.0))
        timeout = float(enter.get("dialog_timeout", 24.0))
        load_wait = float(enter.get("load_wait", 15.0))
        blind_after = float(enter.get("blind_confirm_after", 3.0))
        attempts = max(1, int(enter.get("retry", 3) or 1))
        spec = self.enter_dialog_spec() or {}
        poll = max(0.2, float((self.p.get("detect") or {}).get("poll", 0.4)))

        if not enter.get("dungeon_pt"):
            self.log("没有标定「副本入口」，跳过进本这一步")
            return True
        if not confirm_pt:
            self.log("没有标定「进入游戏」按钮，无法确认进本")
            return False

        for attempt in range(1, attempts + 1):
            if self._stopping():
                return False
            self.log(f"—— 进本第 {attempt}/{attempts} 次：点副本入口 ——")
            if not self._goto(enter.get("dungeon_pt"), travel, "副本入口"):
                return False

            blind_done = False
            start = self.game.now()
            while not self._stopping() and self.game.now() - start < timeout:
                # 弹窗一出现就点
                if (spec or {}).get("roi"):
                    frame = self.grab()
                    if frame is not None and self.anchor_visible(spec, frame):
                        self.log(f"识别到进入弹窗（{self.game.now() - start:.1f}s）→ 点「进入游戏」")
                        self.game.click_norm(confirm_pt)
                        break
                # 识别不到就早点盲点一次（不傻等 24s）
                if (not blind_done
                        and self.game.now() - start >= blind_after):
                    self.log(f"{blind_after:.0f}s 没识别到弹窗 → 先盲点一次「进入游戏」")
                    self.game.click_norm(confirm_pt)
                    blind_done = True
                elif blind_done:
                    # 盲点后只要看到进度条出现，就说明弹窗点对了、确实进本了
                    fill = self.progress_fill()
                    if fill is not None and fill > 0.02:
                        self.log(f"盲点后看到加载进度条（{fill * 100:.0f}%）→ 确认进本")
                        break
                self.game.sleep(poll)
            else:
                self.log(f"{timeout:.0f}s 没进本 → 重新寻路（{attempt}/{attempts}）")
                continue

            # 用进度条验证到底进没进
            self.gap("点进入后缓冲")
            if self.wait_progress(load_wait):
                self.gap("进本完成后缓冲")
                return True
            loaded = self._loaded_spec()
            if (loaded or {}).get("roi") and self.wait_until(loaded, 3.0, "副本内特征"):
                self.gap("进本完成后缓冲")
                return True
            self.log("没看到加载进度条 → 认为没进副本，重新寻路")
        self.log(f"进本失败：{attempts} 次都没能进副本")
        return False

    # -- ② 打怪点：看疲劳决定去 BOSS 还是打怪 --
    def action_for_fatigue(self, current) -> str:
        """疲劳值 → 该做哪个：< -阈值 打BOSS；> +阈值 打怪；其余用默认。"""
        cfg = self.p.get("fatigue") or {}
        loop = self.p.get("loop") or {}
        default = str(loop.get("default_action") or "boss")
        threshold = abs(int(cfg.get("threshold", 800) or 0))
        if current is None or not threshold:
            return default
        if current < -threshold:
            return "boss"
        if current > threshold:
            return "farm"
        return default

    def decide_action(self) -> str:
        """在打怪点读疲劳：< -阈值 打BOSS；> +阈值 打怪；中间用默认。"""
        cfg = self.p.get("fatigue") or {}
        if not cfg.get("enabled", True):
            return str((self.p.get("loop") or {}).get("default_action") or "boss")
        current, maximum, msg = self.fatigue_check()
        if current is None:
            default = str((self.p.get("loop") or {}).get("default_action") or "boss")
            self.log(f"疲劳读不到（{msg}），按默认做：{default}")
            return default
        threshold = abs(int(cfg.get("threshold", 800) or 0))
        action = self.action_for_fatigue(current)
        self.log(f"当前疲劳 {current}/{maximum}（阈值 ±{threshold}）→ "
                 + ("循环打 BOSS" if action == "boss" else "循环打怪"))
        return action

    def _anchor_of(self, key: str) -> dict:
        return (self._farm().get(key) or {})

    def boss_hp_visible(self) -> bool:
        """BOSS 血条还在不在（在=没死）。"""
        spec = self._anchor_of("boss_hp")
        roi = (spec or {}).get("roi")
        if not roi:
            return True  # 没框血条 → 用时间上限
        frame = self.grab()
        if frame is None:
            return True
        try:
            fill = ink_ratio(crop_norm(frame, roi))
        except Exception:
            return True
        return fill > float(spec.get("min_ink", 0.02) or 0.02)

    def boss_present(self) -> bool:
        """「BOSS 身形」在不在（确认真的站到 BOSS 面前了）。"""
        spec = self._anchor_of("boss_present")
        if not (spec or {}).get("roi"):
            return True  # 没标就默认到了
        frame = self.grab()
        return bool(frame is not None and self.anchor_visible(spec, frame))

    def finish_visible(self) -> bool:
        """「打怪完成」标志出现没有。"""
        spec = self._anchor_of("finish")
        if not (spec or {}).get("roi"):
            return False
        frame = self.grab()
        return bool(frame is not None and self.anchor_visible(spec, frame))

    def goto_boss_point(self) -> bool:
        """② 走到副本内 BOSS 点；没识别到「打怪点BOSS」就重新寻路。"""
        farm = self._farm()
        tries = max(1, int(farm.get("boss_retry", 3) or 1))
        travel = float(farm.get("travel_wait", 30.0))
        spec = self._anchor_of("boss_present")
        for attempt in range(1, tries + 1):
            if self._stopping():
                return False
            self.log(f"前往 BOSS 点（第 {attempt}/{tries} 次）")
            if not self._goto(farm.get("boss_pt"), travel, "BOSS 点"):
                return False
            self.gap("到 BOSS 点后缓冲")
            if not (spec or {}).get("roi"):
                return True
            if self.wait_until(spec, 6.0, "BOSS 身形"):
                return True
            self.log("没识别到「BOSS 身形」→ 回到上一步重新寻路")
        self.log(f"BOSS 点确认失败：{tries} 次都没识别到 BOSS 身形")
        return False

    def fight_until_finish(self) -> bool:
        """③ 挂机打怪：每次平A都拾取；BOSS 血条消失（连续 N 次看不到）= 打完。"""
        farm = self._farm()
        loop = self.p.get("loop") or {}
        seconds = float(loop.get("boss_seconds", farm.get("seconds", 60.0)) or 60.0)
        interval = max(0.05, float(farm.get("interval_ms", 250)) / 1000.0)
        pickup_key = str(farm.get("pickup_key") or self._key("pickup", "space"))
        pickup_on_a = bool(farm.get("pickup_on_a", True))
        pickup_every = float(farm.get("pickup_every", 8.0) or 0.0)
        potion_every = float(farm.get("potion_every", 6.0) or 0.0)
        potion_keys = [str(k) for k in (farm.get("potion_keys") or ["q", "w"]) if str(k)]
        hp_threshold = int(farm.get("hp_threshold", 0) or 0)
        hp_every = float(farm.get("hp_check_every", 2.0) or 2.0)
        use_skills = bool(farm.get("use_skills", True))
        hp_spec = self._anchor_of("boss_hp")
        need_miss = max(1, int((hp_spec or {}).get("miss", 3) or 3))
        poll = max(0.2, float((self.p.get("detect") or {}).get("poll", 0.5)))
        loop_fat = float(loop.get("fatigue_every", 60.0) or 0.0)
        watch_fatigue = bool((self.p.get("fatigue") or {}).get("enabled", True)) and loop_fat > 0

        self.log(f"开始打怪（上限 {seconds:.0f}s；"
                 + ("BOSS 血条消失就算打完）" if (hp_spec or {}).get("roi")
                    else "没框血条，按时间停）"))
        start = self.game.now()
        next_s1 = next_s2 = next_potion = next_pickup = next_hp = 0.0
        next_fatigue = next_pickup = next_check = 0.0
        next_pickup = pickup_every
        hp_low = False
        misses = 0
        while not self._stopping():
            elapsed = self.game.now() - start
            if elapsed >= seconds:
                self.log(f"打怪到上限 {seconds:.0f}s（血条一直没消失）")
                break
            self.game.tap("a")
            if pickup_on_a:
                self.game.tap(pickup_key)
            if use_skills:
                if elapsed >= next_s1:
                    self.game.tap(str(farm.get("skill1", "1")))
                    next_s1 = elapsed + float(farm.get("skill1_every", 12.0))
                if elapsed >= next_s2:
                    self.game.tap(str(farm.get("skill2", "2")))
                    next_s2 = elapsed + float(farm.get("skill2_every", 6.0))

            if hp_threshold > 0 and farm.get("hp_roi"):
                if elapsed >= next_hp:
                    current = self.hp_now()
                    next_hp = elapsed + hp_every
                    if current is not None:
                        hp_low = current < hp_threshold
            else:
                hp_low = True
            if hp_low and potion_every > 0 and elapsed >= next_potion:
                for key in potion_keys:
                    self.game.tap(key)
                next_potion = elapsed + potion_every

            if not pickup_on_a and pickup_every > 0 and elapsed >= next_pickup:
                self.game.tap(pickup_key)
                next_pickup = elapsed + pickup_every

            if (hp_spec or {}).get("roi") and elapsed >= next_check:
                next_check = elapsed + poll
                if self.boss_hp_visible():
                    misses = 0
                else:
                    misses += 1
                    self.log(f"BOSS 血条不见了（{misses}/{need_miss}）")
                    if misses >= need_miss:
                        self.log(f"BOSS 血条连续 {misses} 次不见 → 判定打完")
                        break

            if watch_fatigue and elapsed >= next_fatigue:
                next_fatigue = elapsed + loop_fat
                current, maximum, _msg = self.fatigue_check()
                if current is not None:
                    self.log(f"打怪中查疲劳 {current}/{maximum}")

            self.game.sleep(interval)
        return True

    def run_loops(self) -> bool:
        """②③ 走到 BOSS 点 → 打怪到完成。"""
        if not self.goto_boss_point():
            return False
        return self.fight_until_finish()

    # -- 兼容旧接口：固定时长打 BOSS 点 --
    def farm_at_boss(self, seconds: Optional[float] = None) -> bool:
        farm = self._farm()
        if not self._goto(farm.get("boss_pt"),
                          float(farm.get("travel_wait", 30.0)), "BOSS 点"):
            return False
        self.gap("到 BOSS 点后缓冲")
        return self.combat(seconds if seconds is not None
                           else float(farm.get("seconds", 30.0)),
                           desc="BOSS 点挂机")

    def combat(self, seconds: float, desc: str = "挂机",
               action: Optional[str] = None) -> bool:
        """通用挂机：平A + 技能 + 血量低喝药 + 定时拾取。

        action 不为空时，每 loop.fatigue_every 秒查一次疲劳，
        发现该换分支（打BOSS <-> 打怪）就提前结束本次挂机。
        """
        loop = self.p.get("loop") or {}
        fatigue_every = float(loop.get("fatigue_every", 60.0) or 0.0)
        fatigue_cfg = self.p.get("fatigue") or {}
        watch = bool(
            action and fatigue_every > 0 and fatigue_cfg.get("enabled", True)
        )
        next_fatigue = fatigue_every
        farm = self._farm()
        interval = max(0.05, float(farm.get("interval_ms", 250)) / 1000.0)
        pickup_key = str(farm.get("pickup_key") or self._key("pickup", "space"))
        pickup_every = float(farm.get("pickup_every", 8.0) or 0.0)
        potion_every = float(farm.get("potion_every", 6.0) or 0.0)
        potion_keys = [str(k) for k in (farm.get("potion_keys") or ["q", "w"]) if str(k)]
        hp_threshold = int(farm.get("hp_threshold", 0) or 0)
        hp_every = float(farm.get("hp_check_every", 2.0) or 2.0)
        use_skills = bool(farm.get("use_skills", True))

        self.log(f"{desc}：{seconds:.0f}s（喝药 {potion_every:.0f}s / "
                 f"拾取 {pickup_every:.0f}s）")
        start = self.game.now()
        next_s1 = next_s2 = 0.0
        next_potion = 0.0
        next_pickup = pickup_every
        next_hp_check = 0.0
        hp_low = False

        pickup_on_a = bool(farm.get("pickup_on_a", True))
        while not self._stopping():
            elapsed = self.game.now() - start
            if elapsed >= seconds:
                break
            self.game.tap("a")
            if pickup_on_a:
                self.game.tap(pickup_key)
            if use_skills:
                if elapsed >= next_s1:
                    self.game.tap(str(farm.get("skill1", "1")))
                    next_s1 = elapsed + float(farm.get("skill1_every", 12.0))
                if elapsed >= next_s2:
                    self.game.tap(str(farm.get("skill2", "2")))
                    next_s2 = elapsed + float(farm.get("skill2_every", 6.0))

            if hp_threshold > 0 and farm.get("hp_roi"):
                if elapsed >= next_hp_check:
                    current = self.hp_now()
                    next_hp_check = elapsed + hp_every
                    if current is not None:
                        hp_low = current < hp_threshold
                        if self.verbose and hp_low:
                            self.log(f"血量 {current} < {hp_threshold}，喝药")
            else:
                hp_low = True  # 没框血量就按固定间隔喝

            if hp_low and potion_every > 0 and elapsed >= next_potion:
                for key in potion_keys:
                    self.game.tap(key)
                next_potion = elapsed + potion_every

            if pickup_every > 0 and elapsed >= next_pickup:
                self.game.tap(pickup_key)
                next_pickup = elapsed + pickup_every

            if watch and elapsed >= next_fatigue:
                next_fatigue = elapsed + fatigue_every
                current, maximum, msg = self.fatigue_check()
                if current is None:
                    self.log(f"疲劳读不到（{msg}），继续{desc}")
                else:
                    want = self.action_for_fatigue(current)
                    threshold = abs(int(fatigue_cfg.get("threshold", 800) or 0))
                    self.log(f"{desc}中查疲劳 {current}/{maximum}（阈值 ±{threshold}）"
                             + ("" if want == action else
                                " → 该换分支了：" + ("打BOSS" if want == "boss" else "打怪")))
                    if want != action:
                        self._switch_to = want
                        break

            self.game.sleep(interval)

        self.log(f"{desc}：结束（{self.game.now() - start:.0f}s）")
        return True

    # -- 疲劳检测（退出副本后） --
    def fatigue_roi(self):
        cfg = self.p.get("fatigue") or {}
        roi = cfg.get("roi")
        if roi:
            return roi
        try:
            return app.load_fatigue_roi_norm()
        except Exception:
            return None

    def fatigue_check(self):
        """点一下疲劳图标 → OCR 读「当前疲劳值」；返回 (当前, 最大, 说明)。"""
        roi = self.fatigue_roi()
        if not roi:
            return None, None, "没有疲劳区域（主界面也没框过）"
        cfg = self.p.get("fatigue") or {}
        pt = cfg.get("pt")
        if not pt:
            pt = [
                float(roi[0]) + float(roi[2]) / 2.0,
                float(roi[1]) + float(roi[3]) / 2.0,
            ]
        self.game.click_norm(pt)
        self.game.sleep(1.0)
        frame = self.grab()
        if frame is None:
            return None, None, "截图失败"
        w, h, data = frame
        try:
            rect = app.roi_from_norm(tuple(float(v) for v in roi), w, h)
            return app.ocr_fatigue_from_bgra(data, w, h, rect)
        except Exception as exc:
            return None, None, f"疲劳识别异常：{type(exc).__name__}: {exc}"

    def after_exit_fatigue(self) -> None:
        """退出副本后检测一次疲劳；低于阈值就先普通挂机打怪一会儿。"""
        cfg = self.p.get("fatigue") or {}
        if not cfg.get("enabled", True):
            return
        current, maximum, msg = self.fatigue_check()
        if current is None:
            self.log(f"疲劳检测失败（{msg}），跳过普通打怪")
            return
        threshold = int(cfg.get("threshold", 800) or 0)
        self.log(f"当前疲劳 {current}/{maximum}（阈值 {threshold}）")
        if current < threshold:
            seconds = float(cfg.get("farm_seconds", 300.0) or 0.0)
            if seconds > 0:
                self.combat(seconds, desc=f"疲劳不足，普通挂机打怪 {seconds:.0f}s")
        else:
            self.log("疲劳充足，继续下一轮副本")

    # -- ③ 退出副本 --
    def loot_before_exit(self) -> None:
        """打完 BOSS 再捡一轮，然后缓冲 1~2 秒（按 action_gap）。"""
        farm = self._farm()
        key = str(farm.get("pickup_key") or self._key("pickup", "space"))
        times = max(0, int(farm.get("pickup_times", 3) or 0))
        if times:
            self.log(f"打完再拾取 {times} 次")
            for _ in range(times):
                if self._stopping():
                    return
                self.game.tap(key)
                self.game.sleep(0.4)
        self.gap("拾取后缓冲")

    def exit_dungeon(self) -> bool:
        """③ 退出：点「退出副本」→ 识别独立的「退出确认弹窗」→ 点「确认」。"""
        exit_cfg = self.p.get("exit") or {}
        exit_pt = exit_cfg.get("exit_pt")
        confirm_pt = exit_cfg.get("confirm_pt")
        spec = self.exit_dialog_spec()
        timeout = float(exit_cfg.get("dialog_timeout", 10.0))
        attempts = max(1, int(exit_cfg.get("retry", 3) or 1))
        exit_wait = float(exit_cfg.get("exit_wait", 1.0))
        confirm_wait = float(exit_cfg.get("confirm_wait", 1.0))
        settle = float(exit_cfg.get("settle", 10.0))
        poll = max(0.2, float((self.p.get("detect") or {}).get("poll", 0.5)))

        if not exit_pt:
            self.log("没有标定「退出副本」按钮，跳过退出")
            return False
        if not confirm_pt:
            self.log("没有标定「确认」按钮，退出可能卡在弹窗上")
            return False

        for attempt in range(1, attempts + 1):
            if self._stopping():
                return False
            self.log(f"退出副本第 {attempt}/{attempts} 次：点「退出副本」")
            self.game.click_norm(exit_pt)
            self.game.sleep(exit_wait)
            if not spec.get("roi"):
                # 没框退出确认弹窗区域：直接盲点确认（兼容老标定）
                self.log("没标定「退出确认弹窗区域」→ 直接点「确认」（盲点）")
                self.game.click_norm(confirm_pt)
                self.game.sleep(confirm_wait)
                self.game.sleep(settle)
                return True
            if self.wait_anchor(spec, timeout, poll, "退出确认弹窗"):
                self.log("点「确认」→ 等退出加载")
                self.game.click_norm(confirm_pt)
                self.game.sleep(confirm_wait)
                self.game.sleep(settle)
                return True
            self.log(f"没识别到退出确认弹窗（等了 {timeout:.0f}s），再点一次「退出副本」")
        self.log(f"退出失败：{attempts} 次都没识别到退出确认弹窗")
        return False

    # -- 一轮 --
    def run_round(self, seconds: Optional[float] = None) -> bool:
        """一轮：打怪点 → 副本入口 → 弹窗进本 → 进度条 → BOSS 点 → 打怪完成 → 退出。"""
        self.log("=== 开始一轮 ===")
        if getattr(self.game, "foreground_mode", None) and self.game.foreground_mode():
            self.log("切到游戏窗口（前台模式：会用真实鼠标键盘）")
            try:
                self.game.activate()
            except Exception:
                pass
        if not self.enter_dungeon():
            return self._failed("进副本失败")
        if not self.run_loops():
            return self._failed("BOSS 点打怪失败")
        self.loot_before_exit()
        if not self.exit_dungeon():
            return self._failed("退出副本失败")
        self.gap("退出后缓冲")
        return True

    def _failed(self, reason: str) -> bool:
        self.log(f"本轮失败：{reason}")
        try:
            self.game.snapshot(f"fail_{reason}")
        except Exception:
            pass
        return False

    def run(self, rounds: Optional[int] = None,
            seconds: Optional[float] = None) -> int:
        retry = self.p.get("retry") or {}
        total = int(rounds if rounds is not None else retry.get("rounds", 0))
        gap = float(retry.get("gap", 8.0))
        max_fail = max(1, int(retry.get("max_fail", 3)))
        done = 0
        self.log(f"开始循环：轮数={total or '无限'} 间隔={gap:.0f}s")
        while not self._stopping():
            if total and done >= total:
                break
            ok = self.run_round(seconds)
            done += 1
            if ok:
                self.stats["ok"] += 1
                self.stats["failed_streak"] = 0
                self.log(f"第 {done} 轮完成 ✓")
            else:
                self.stats["fail"] += 1
                self.stats["failed_streak"] += 1
                if self.stats["failed_streak"] >= max_fail:
                    self.log("连续失败，已停止")
                    break
            if total and done >= total:
                break
            if self._sleep_with_stop(gap):
                break
        self.log(f"结束：成功 {self.stats['ok']} 轮 / 失败 {self.stats['fail']} 轮")
        return self.stats["ok"]

    def _sleep_with_stop(self, seconds: float) -> bool:
        end = self.game.now() + max(0.0, seconds)
        while self.game.now() < end:
            if self._stopping():
                return True
            self.game.sleep(min(0.5, max(0.05, end - self.game.now())))
        return self._stopping()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def list_windows(keyword: str = "") -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []

    def cb(hwnd, _param):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        if keyword and keyword.lower() not in title.lower():
            return True
        found.append((int(hwnd), title))
        return True

    win32gui.EnumWindows(cb, None)
    return found


def find_window(keyword: str) -> Optional[int]:
    wins = list_windows(keyword)
    if not wins:
        return None
    wins.sort(key=lambda item: len(item[1]))
    return wins[0][0]


def snapshot_dir() -> str:
    folder = os.path.join(app._data_folder(), SNAPSHOT_DIR_NAME)
    os.makedirs(folder, exist_ok=True)
    return folder


def probe(hwnd: int, profile: dict, seconds: float = 0.0) -> int:
    game = WinGame(hwnd, profile, dry=True)
    runner = DungeonRunner(game, profile, verbose=True)
    cap = game.grab()
    if cap is None:
        print(f"后台截图失败：{game.last_error}")
        return 1
    print(f"后台截图正常：{cap[0]}x{cap[1]}")
    start = time.monotonic()
    try:
        while True:
            info = runner.probe()
            print(" ".join(f"{k}={v}" for k, v in info.items()) or "(还没标定判定区域)")
            if seconds and time.monotonic() - start >= seconds:
                break
            time.sleep(max(0.2, float((profile.get("detect") or {}).get("poll", 0.5))))
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="副本流程：地图1 进本 → 地图2 BOSS 点挂机 → 退出"
    )
    parser.add_argument("--list", action="store_true", help="列出窗口")
    parser.add_argument("--window", default="", help="窗口标题关键字")
    parser.add_argument("--hwnd", type=lambda v: int(v, 0), default=0)
    parser.add_argument("--profile", default="", help="标定文件路径")
    parser.add_argument("--run", action="store_true", help="开始跑")
    parser.add_argument("--probe", action="store_true", help="只看判定数值（不发按键）")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="--probe 时打印多少秒")
    parser.add_argument("--rounds", type=int, default=None, help="跑几轮")
    parser.add_argument("--farm-seconds", type=float, default=None,
                        help="覆盖挂机时长（秒）")
    parser.add_argument("--dry", action="store_true", help="模拟跑：不发真实按键")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        wins = list_windows(args.window)
        if not wins:
            print("没有找到窗口")
            return 1
        for hwnd, title in wins:
            print(f"0x{hwnd:08X}  {title}")
        return 0

    hwnd = int(args.hwnd or 0)
    if not hwnd:
        keyword = args.window or ""
        if not keyword:
            print("请用 --window 指定窗口标题关键字，或 --hwnd 指定句柄")
            return 2
        hwnd = find_window(keyword) or 0
        if not hwnd:
            print(f"没有找到标题包含「{keyword}」的窗口")
            return 1
    if not win32gui.IsWindow(hwnd):
        print("窗口句柄无效")
        return 1

    path = args.profile or profile_path()
    profile = load_profile(path)
    print(f"窗口：0x{hwnd:X} {win32gui.GetWindowText(hwnd)}")
    print(f"标定文件：{path}")

    if args.probe:
        return probe(hwnd, profile, args.seconds)

    if args.run:
        game = WinGame(hwnd, profile, dry=args.dry, snapshot_dir=snapshot_dir())
        runner = DungeonRunner(game, profile, verbose=args.verbose)
        try:
            runner.run(args.rounds, args.farm_seconds)
        except KeyboardInterrupt:
            print("\n已中断")
            return 130
        return 0 if runner.stats["fail"] == 0 else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
