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
    python dungeon_flow.py --run --window CreatureCurios --boss-seconds 90
    python dungeon_flow.py --probe --window CreatureCurios --seconds 5
"""

from __future__ import annotations

import argparse
import copy
import datetime
import json
import math
import os
import sys
import threading
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

# 多开时所有窗口共用：谁要切前台/借光标，都必须先拿到这把锁。
# 这样 A 窗口在点地图时，B 窗口不会同时把光标/前台抢走。
_INPUT_LOCK = threading.RLock()

DEFAULT_PROFILE: dict = {
    "name": "默认副本",
    "window_title": "CreatureCurios",
    "keys": {
        "open_map": "m",     # 开地图键（地图1/地图2 都用它）
        "pickup": "space",   # 拾取键
    },
    "allow_grab": False,      # 是否允许抢占鼠标键盘（False=纯后台，不动光标不切前台）
    "input_mode": "hybrid",   # hybrid=只在点击时借鼠标（键盘走后台）；foreground=全程占用；background=纯后台
    "prime_activate": True,   # 后台模式下补发 WM_ACTIVATE/焦点消息
    "action_gap": 1.0,        # 动作之间的缓冲（秒）
    "nav_macro": {
        "enabled": False,     # 用录制的鼠标宏引路（地图点识别不准时用）
        "events": [],         # 录制下来的点击/按键（归一化客户区坐标）
        "meta": {},           # 录制时的客户区尺寸（换分辨率也能回放）
        "speed": 1.0,         # 回放速度 0.25~3.0
        "fallback": False,    # 宏跑完没到 BOSS 时，是否回退到地图点位
    },
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
        "map_radius": 0.02,        # 重试时在副本入口周围绕圈微调（归一化半径）
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
        "boss_present": {"roi": None, "sig": [], "sigs": [], "grid": 12,
                         "sim": 0.60, "search": 1.6, "tol": 30.0,
                         "still_seconds": 0.8, "still_timeout": 12.0,
                         "still_diff": 0.015},  # BOSS 身形（多张样本 + 停下才识别）
        "boss_hp": {"roi": None, "min_fill": 0.15, "miss": 3,
                    "drop_ratio": 0.55},  # BOSS 血条（亮度占比；比基准掉到 55% 以下算消失）
        "finish": {"roi": None, "sig": [], "grid": 12, "sim": 0.90},        # 旧版，已不用
        "boss_retry": 3,           # 没识别到「打怪点BOSS」就重新寻路几次
        "map_radius": 0.02,        # 重试时在 BOSS 点周围绕圈微调（归一化半径）
        "death_by_body": False,    # 血条没消失时，BOSS 身形连续 miss 次不见也算死
        "pickup_on_a": True,       # 每次平A后都按一次拾取
        "seconds": 30.0,           # 兼容旧标定；实际 BOSS 战上限用 loop.boss_seconds
        "interval_ms": 250,
        "use_skills": True,
        "skill1": "1", "skill1_every": 12.0,
        "skill2": "2", "skill2_every": 6.0,
        "hp_roi": None,            # 血量数字区域（框了才按血量喝药）
        "hp_threshold": 1600,
        "hp_check_every": 2.0,
        "potion_keys": ["q"],      # 血量低于阈值时只按 Q
        "potion_every": 6.0,       # 喝药间隔（低于阈值时每 6 秒喝一次）
        "pickup_every": 8.0,       # 挂机期间每隔几秒按一次拾取
    },
    "loop": {
        "link_fatigue": True,      # 打怪点挂机 ↔ 打BOSS 联动（按疲劳上下限切换）
        "fatigue_every": 60.0,     # 打怪/挂机时每隔多少秒查一次疲劳（多开每个窗口独立查）
        "max_switch": 3,           # 疲劳变化导致换分支，最多切几次（防来回跳）
        "boss_seconds": 120.0,     # BOSS 战上限（血条一直不消失时兜底；最大 1000s）
        "default_action": "farm",  # 疲劳在中间区间时做哪个：boss / farm
    },
    "warehouse": {
        "enabled": True,
        "icon_pt": None,       # 仓库图标（点它打开仓库）
        "deposit_pt": None,    # 「一键放入」按钮
        "gap": 0.5,            # 每步之间缓冲
        "open_wait": 1.5,      # 点完仓库图标等界面出来
        "esc_times": 2,        # 放完按几下 ESC 回主界面（默认 2，不多不少）
        "after": "exit",       # 什么时候放：exit=退出副本后
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
        "enabled": True,           # 是否按疲劳自动切换分支
        "pt": None,                # 疲劳图标点位 [nx, ny]（点一下弹提示）
        "roi": None,               # 疲劳提示文字区域；空则用主界面已框的
        "threshold": 600,          # 疲劳上下限：< -阈值 去打BOSS，> +阈值 回打怪点
    },
    "detect": {
        "poll": 0.2,        # 轮询间隔（越小越灵敏）
        "sim": 0.80,        # 主相似度阈值
        "sim_loose": 0.60,  # 宽松阈值（配合 ink 占比一起判断）
        "ink_min": 0.05,    # 宽松判定时要求的“区域有东西”比例
    },
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
    merged = _merge(DEFAULT_PROFILE, data)
    return _normalize_profile(merged)


def _normalize_profile(p: dict) -> dict:
    """把旧版标定的“太严”阈值调松，否则弹窗明明在也识别不到。"""
    loose_defaults = {"sim": 0.80, "sim_loose": 0.60, "ink_min": 0.05}
    try:
        for group, key in (("enter", "dialog"), ("exit", "dialog"),
                           ("mount", "dialog"), ("mount", "state")):
            spec = ((p.get(group) or {}).get(key) or {})
            if not isinstance(spec, dict) or not spec.get("roi"):
                continue
            if not spec.get("sim") or float(spec.get("sim", 0)) >= 0.85:
                spec["sim"] = loose_defaults["sim"]
            spec.setdefault("sim_loose", loose_defaults["sim_loose"])
            spec.setdefault("ink_min", loose_defaults["ink_min"])
            (p.setdefault(group, {}) or {})[key] = spec
        body = ((p.get("farm") or {}).get("boss_present") or {})
        if isinstance(body, dict) and body.get("roi"):
            body["sim"] = min(float(body.get("sim") or 0.60), 0.65)
            body["search"] = max(float(body.get("search") or 1.0), 1.6)
            body["tol"] = max(float(body.get("tol") or 18.0), 30.0)
            p.setdefault("farm", {})["boss_present"] = body
    except Exception:
        pass
    return p


def save_profile(profile: dict, path: Optional[str] = None) -> str:
    path = path or profile_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, ensure_ascii=False, indent=2)
    return path


def copy_profile_for_slot(profile: dict, boss_seconds: Optional[float] = None) -> dict:
    """多开：每个窗口复制一份（不再按窗口设挂机时长）。

    保留 boss_seconds 参数只是为了兼容旧调用：它现在表示 BOSS 战上限，
    不再是「挂机多久」。
    """
    p = json.loads(json.dumps(profile))
    if boss_seconds is not None:
        try:
            p.setdefault("loop", {})["boss_seconds"] = max(
                5.0, min(1000.0, float(boss_seconds))
            )
        except (TypeError, ValueError):
            pass
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
        self._focus_saved = 0
        self._logged_target = False

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
    def allow_grab(self) -> bool:
        """是否允许抢占鼠标键盘（勾了才借鼠标/切前台）。"""
        return bool(self.profile.get("allow_grab", False))

    def _post_aux(self, hwnd: int, message: int, wparam: int, lparam: int) -> bool:
        try:
            result = win32api.PostMessage(hwnd, message, wparam, lparam)
            if result is None or int(result) != 0:
                return True
            return bool(_USER32.PostMessageW(hwnd, message, wparam, lparam))
        except Exception:
            try:
                return bool(_USER32.PostMessageW(hwnd, message, wparam, lparam))
            except Exception:
                return False

    def _cursor_inside(self) -> bool:
        try:
            x, y = win32api.GetCursorPos()
            cx, cy = win32gui.ScreenToClient(self.hwnd, (x, y))
            left, top, right, bottom = win32gui.GetClientRect(self.hwnd)
            return left <= cx < right and top <= cy < bottom
        except Exception:
            return True

    def prime_background_input(self) -> None:
        """让游戏以为自己是激活/有焦点的（不切前台、不动光标）。

        Unity 类游戏失去前台后会丢弃 WM_KEYDOWN，必须补这几条激活/焦点消息；
        鼠标不在窗口里时再补一条 WM_MOUSEMOVE，让“鼠标在窗口内”的状态恢复。
        """
        if not win32gui.IsWindow(self.hwnd):
            return
        try:
            fg = int(win32gui.GetForegroundWindow() or 0)
        except Exception:
            fg = 0
        if fg != self.hwnd:
            self._post_aux(self.hwnd, 0x001C, 1, 0)      # WM_ACTIVATEAPP
            self._post_aux(self.hwnd, 0x0086, 1, 0)      # WM_NCACTIVATE
            self._post_aux(self.hwnd, 0x0006, 1, fg)     # WM_ACTIVATE / WA_ACTIVE
            self._post_aux(self.hwnd, 0x0007, fg, 0)     # WM_SETFOCUS
        if not self._cursor_inside():
            try:
                _l, _t, cw, ch = win32gui.GetClientRect(self.hwnd)
                x, y = int(cw) // 2, int(ch) // 2
                self._post_aux(self.hwnd, 0x0200, 0,
                               (x & 0xFFFF) | ((y & 0xFFFF) << 16))
            except Exception:
                pass

    def message_target(self) -> int:
        """真正接收键盘消息的窗口（Unity 常把焦点放在子窗口上）。"""
        root = self.hwnd
        try:
            pid = wt.DWORD()
            tid = int(_USER32.GetWindowThreadProcessId(root, ctypes.byref(pid)))
            if tid:
                info = app.GUITHREADINFO()
                info.cbSize = ctypes.sizeof(app.GUITHREADINFO)
                if _USER32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndFocus:
                    focus = int(info.hwndFocus)
                    if focus == root or _USER32.IsChild(root, focus):
                        return focus
        except Exception:
            pass
        return root

    def input_mode(self) -> str:
        mode = str(self.profile.get("input_mode", "hybrid") or "hybrid").lower()
        return mode if mode in ("hybrid", "foreground", "background") else "hybrid"

    def foreground_mode(self) -> bool:
        """真正需要切前台/动鼠标的两种情况（hybrid 只在点击时借）。"""
        return self.input_mode() in ("foreground", "hybrid")

    def hybrid_mode(self) -> bool:
        return self.input_mode() == "hybrid"

    def release_focus(self, quiet: bool = False) -> None:
        """把前台还给抢占之前那个窗口（混合模式点完立刻调用）。"""
        if not self.hybrid_mode():
            return
        with _INPUT_LOCK:
            saved = getattr(self, "_focus_saved", 0)
            self._focus_saved = 0
            if not saved:
                return
            try:
                if saved == self.hwnd or not win32gui.IsWindow(saved):
                    return
                if int(win32gui.GetForegroundWindow() or 0) != self.hwnd:
                    return
                app._force_foreground_window(saved)
                if not quiet:
                    self.log("已把前台/光标还给原来的窗口（键盘继续后台发送）")
            except Exception:
                pass

    def activate(self) -> bool:
        """把游戏窗口切到前台（只有允许抢占时才真的切）。"""
        if not self.foreground_mode() or self.dry:
            return True
        if not self.allow_grab():
            return True   # 纯后台模式：不切前台，只发消息
        with _INPUT_LOCK:
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
        if self.foreground_mode() and self.allow_grab():
            return self._grab_click(x, y)
        if self.profile.get("prime_activate", True):
            app.prime_background_input(self.hwnd, activate=True)
        ok = app.post_click_client(self.hwnd, x, y)
        if not ok:
            self.last_error = "后台点击发送失败"
        return ok

    def _grab_click(self, x: int, y: int) -> bool:
        """真实鼠标点击：整段拿全局输入锁，点完光标/前台都还原。

        多开时 4 个窗口共用 _INPUT_LOCK，所以不会出现 A 窗口把光标移到
        地图上、B 窗口又把光标还原到半路的情况。
        """
        hybrid = self.hybrid_mode()
        with _INPUT_LOCK:
            saved_cursor = None
            if hybrid:
                try:
                    saved_cursor = win32api.GetCursorPos()
                except Exception:
                    saved_cursor = None
            try:
                fg = int(win32gui.GetForegroundWindow() or 0)
            except Exception:
                fg = 0
            self._focus_saved = fg if fg and fg != self.hwnd else 0
            try:
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
            finally:
                if hybrid and saved_cursor is not None:
                    try:
                        win32api.SetCursorPos(saved_cursor)
                    except Exception:
                        pass
                # 用户要求：抢完鼠标键盘要回到原来的界面。
                if hybrid:
                    self.release_focus(quiet=True)

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
        self.prime_background_input()
        target = self.message_target()
        if target != self.hwnd and not getattr(self, "_logged_target", False):
            self._logged_target = True
            self.log(f"按键投递到焦点子窗口 0x{target:X}（Unity 常见）")
        down_lp = 1 | (scan << 16)
        up_lp = down_lp | 0xC0000000
        ok = self._post_aux(target, win32con.WM_KEYDOWN, vk, down_lp)
        if hold > 0:
            self.sleep(hold)
        ok = self._post_aux(target, win32con.WM_KEYUP, vk, up_lp) and ok
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
        self._mode: Optional[str] = None   # 联动模式：farm / boss

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

    def frame_diff(self, a, b) -> float:
        """两张截图不一样的程度（0=完全一样，1=差很多）。用于判断“人停下来了没”。"""
        try:
            w = min(a[0], b[0])
            h = min(a[1], b[1])
            pa = frame_array(a)[:h, :w, :3].astype(np.int16)
            pb = frame_array(b)[:h, :w, :3].astype(np.int16)
            step_y = max(1, h // 32)
            step_x = max(1, w // 32)
            pa = pa[::step_y, ::step_x]
            pb = pb[::step_y, ::step_x]
            return float(np.abs(pa - pb).mean() / 255.0)
        except Exception:
            return 1.0

    def wait_scene_still(self, seconds: float = 0.8, timeout: float = 12.0,
                         diff_thresh: float = 0.015) -> bool:
        """等人停下来：连续 seconds 秒画面几乎不变就算静止。"""
        poll = max(0.05, float((self.p.get("detect") or {}).get("poll", 0.2)))
        start = self.game.now()
        still_since = None
        prev = self.grab()
        while not self._stopping() and self.game.now() - start < timeout:
            self.game.sleep(poll)
            cur = self.grab()
            if prev is None or cur is None:
                prev = cur
                continue
            diff = self.frame_diff(prev, cur)
            prev = cur
            if diff <= diff_thresh:
                if still_since is None:
                    still_since = self.game.now()
                if self.game.now() - still_since >= seconds:
                    self.log(f"画面已静止 {seconds:.1f}s（差异 {diff:.3f}）→ 到位")
                    return True
            else:
                still_since = None
        self.log(f"等了 {timeout:.0f}s 画面还在动（可能在走路/打架）")
        return False

    def _detect_cfg(self, key: str, default: float) -> float:
        return float((self.p.get("detect") or {}).get(key, default))

    def anchor_metrics(self, spec: dict, frame) -> tuple[float, float]:
        """返回 (相似度, 非背景占比)。没 sig 时相似度返回 -1。"""
        roi = (spec or {}).get("roi")
        if not roi or frame is None:
            return -1.0, 0.0
        patch = crop_norm(frame, roi)
        ink = ink_ratio(patch)
        sig = spec.get("sig")
        if not sig:
            return -1.0, ink
        grid = int(spec.get("grid", 12))
        tol = float(spec.get("tol", 18.0))
        cur = signature(patch, grid)
        templates = [t for t in (spec.get("sigs") or []) if t]
        if sig:
            templates.insert(0, sig)
        if not templates:
            return -1.0, ink
        best = max(similarity(t, cur, tol) for t in templates)
        search = float(spec.get("search", 1.0) or 1.0)
        if search > 1.02:
            best = max(best, self._search_similarity(spec, frame, search))
        return best, ink

    def _search_similarity(self, spec: dict, frame, search: float) -> float:
        """在放大后的区域里滑动搜索，取所有样本里最像的。"""
        """在大一圈的区域里滑动找最像的位置（BOSS 会走会晃，不能只比原位）。"""
        try:
            rx, ry, rw, rh = (float(v) for v in spec["roi"])
            dx = rw * (search - 1.0) / 2.0
            dy = rh * (search - 1.0) / 2.0
            bx = max(0.0, rx - dx)
            by = max(0.0, ry - dy)
            big = [bx, by, min(1.0 - bx, rw + 2 * dx), min(1.0 - by, rh + 2 * dy)]
            arr = crop_norm(frame, big)
            if arr.size == 0:
                return 0.0
            templates = [t for t in (spec.get("sigs") or []) if t]
            if spec.get("sig"):
                templates.insert(0, spec["sig"])
            if not templates:
                return 0.0
            grid = int(spec.get("grid", 12))
            tol = float(spec.get("tol", 30.0))
            h, w = arr.shape[:2]
            cell = max(3, min(h, w) // grid)
            span = cell * grid
            if span > h or span > w:
                whole = signature(arr, grid)
                return max(similarity(t, whole, tol) for t in templates)
            step = max(1, cell // 2)
            best = 0.0
            y = 0
            while y + span <= h:
                x = 0
                while x + span <= w:
                    patch = arr[y:y + span, x:x + span]
                    win = signature(patch, grid)
                    for t in templates:
                        sim = similarity(t, win, tol)
                        if sim > best:
                            best = sim
                            if best >= 0.99:
                                return best
                    x += step
                y += step
            whole = signature(arr, grid)
            for t in templates:
                best = max(best, similarity(t, whole, tol))
            return best
        except Exception:
            return 0.0

    def anchor_visible(self, spec: dict, frame) -> bool:
        """锚点是否“和标定时一样”。

        判定用两条路（任一条满足即可，解决“明明在、就是差一点”的问题）：
          1) 相似度 ≥ sim（默认 0.80）
          2) 相似度 ≥ sim_loose（默认 0.60）且区域内容占比 ≥ ink_min（默认 0.05）
        """
        if not isinstance(spec, dict):
            return False
        sim_th = float(spec.get("sim") or self._detect_cfg("sim", 0.80))
        loose = float(spec.get("sim_loose")
                      or self._detect_cfg("sim_loose", 0.60))
        ink_min = float(spec.get("ink_min")
                        or self._detect_cfg("ink_min", 0.05))
        sim, ink = self.anchor_metrics(spec, frame)
        if sim < 0:
            return ink > max(0.15, ink_min)
        return sim >= sim_th or (sim >= loose and ink >= ink_min)

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
        checks = (
            ("进本弹窗", self.enter_dialog_spec()),
            ("退出弹窗", self.exit_dialog_spec()),
            ("BOSS身形", self._anchor_of("boss_present")),
        )
        for name, spec in checks:
            if not (spec or {}).get("roi"):
                out[name] = "未标定"
                continue
            sim, ink = self.anchor_metrics(spec, frame)
            th = float((spec or {}).get("sim")
                       or self._detect_cfg("sim", 0.80))
            hit = self.anchor_visible(spec, frame)
            out[name] = (f"{'命中' if hit else '未命中'}"
                         f"(相似度 {sim:.2f}/{th:.2f} 占比 {ink:.2f})")
        hp_spec = self._anchor_of("boss_hp")
        if (hp_spec or {}).get("roi"):
            patch = crop_norm(frame, hp_spec["roi"])
            ratio = self.boss_hp_ratio()
            ink = ink_ratio(patch)
            out["BOSS血条"] = (
                (f"亮度 {ratio * 100:.0f}%" if ratio is not None else "亮度读取失败")
                + f" / 内容 {ink * 100:.0f}%"
            )
        prog = self._progress_spec()
        if (prog or {}).get("roi"):
            out["加载进度条"] = f"{self.progress_fill() * 100:.0f}%"
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
        poll = max(0.05, float((self.p.get("detect") or {}).get("poll", 0.2)))
        start = self.game.now()
        hits = 0
        best_sim = -2.0
        best_ink = 0.0
        next_log = 0.0
        while not self._stopping() and self.game.now() - start < timeout:
            frame = self.grab()
            if frame is not None:
                sim, ink = self.anchor_metrics(spec, frame)
                best_sim = max(best_sim, sim)
                best_ink = max(best_ink, ink)
                if self.anchor_visible(spec, frame):
                    hits += 1
                    if hits >= max(1, int(need)):
                        self.log(f"识别到{desc}（{self.game.now() - start:.1f}s，"
                                 f"相似度 {sim:.2f}）")
                        return True
                else:
                    hits = 0
                    elapsed = self.game.now() - start
                    if elapsed >= next_log:
                        next_log = elapsed + 1.0
                        self.log(f"等{desc}… 相似度 {sim:.2f}"
                                 + (f" / 占比 {ink:.2f}" if ink else ""))
            self.game.sleep(poll)
        self.log(f"没等到{desc}（最高相似度 {best_sim:.2f}，最高占比 {best_ink:.2f}）")
        return False

    def _offset_point(self, point, index: int, radius: float = 0.02):
        """V3.2 的做法：地图点第一次没点中时，在它周围绕圈微调。

        index=0 返回原点；1..8 是半径 radius 的 8 个方向；
        9..16 再转 22.5° 再来一圈（和旧版 5% 半径绕圈一致）。
        坐标是窗口客户区归一化坐标。
        """
        if not point:
            return point
        try:
            bx, by = float(point[0]), float(point[1])
            r = max(0.002, float(radius or 0.0))
            if index <= 0:
                return (bx, by)
            k = (index - 1) % 8
            loop = (index - 1) // 8
            angle = 2.0 * math.pi * k / 8.0 + loop * math.pi / 8.0
            nx = max(0.0, min(1.0, bx + r * math.cos(angle)))
            ny = max(0.0, min(1.0, by + r * math.sin(angle)))
            return (nx, ny)
        except (TypeError, ValueError):
            return point

    def _goto(self, point, wait: float, desc: str,
              arrived_spec: Optional[dict] = None,
              watch_spec: Optional[dict] = None,
              watch_desc: Optional[str] = None,
              sleep_after: bool = True) -> bool:
        """点地图点位 → 边走边看（可传要盯的锚点）→ 最多等 wait 秒。

        watch_spec：路上要盯的东西（比如 BOSS 身形 / 进入弹窗），
                   一出现就立刻返回 True，不用等走完。
        sleep_after：没盯到东西时，要不要把剩余时间等完（进本那边不等）。
        """
        if not point:
            self.log(f"「{desc}」没有标定，跳过")
            return False
        self.ensure_mount()
        self.log(f"点「{desc}」({float(point[0]):.3f}, {float(point[1]):.3f})"
                 f" → 边走边看（最多 {wait:.0f}s）")
        self.open_map()
        if not self.game.click_norm(point):
            self.log(f"点击「{desc}」失败")
            return False
        self.release_focus()

        watch = watch_spec if (watch_spec or {}).get("roi") else arrived_spec
        name = watch_desc or (f"{desc}到达标记" if watch is arrived_spec else f"{desc}目标")
        start = self.game.now()
        if (watch or {}).get("roi"):
            if self.wait_until(watch, wait, name):
                return True
        if sleep_after:
            left = wait - (self.game.now() - start)
            if left > 0:
                self.game.sleep(left)
        return True

    def _arrived_spec(self) -> dict:
        return (self._farm().get("arrived") or {})

    def _loaded_spec(self) -> dict:
        return ((self.p.get("enter") or {}).get("loaded") or {})

    # ---- 引路宏（鼠标宏）：播放录制的点击/按键 ----
    def nav_macro(self) -> dict:
        return self.p.get("nav_macro") or {}

    def nav_macro_enabled(self) -> bool:
        cfg = self.nav_macro()
        return bool(cfg.get("enabled")) and bool(cfg.get("events"))

    def play_nav_macro(self) -> bool:
        """回放录制的鼠标宏（点击 + 按键），用归一化客户区坐标。

        点击走 WinGame.click_norm：允许抢占时是真实鼠标（点完还原光标/前台），
        不允许抢占时是后台 PostMessage。多开时整段拿 _INPUT_LOCK，
        不会和别的窗口抢鼠标。
        """
        cfg = self.nav_macro()
        events = [e for e in (cfg.get("events") or []) if isinstance(e, dict)]
        if not events:
            self.log("引路宏是空的")
            return False
        try:
            speed = max(0.25, min(3.0, float(cfg.get("speed", 1.0) or 1.0)))
        except (TypeError, ValueError):
            speed = 1.0
        clicks = sum(1 for e in events if e.get("type") == "down"
                     and str(e.get("button", "left")) == "left")
        keys = sum(1 for e in events if e.get("type") == "key_down")
        self.log(f"—— 引路宏：{len(events)} 个事件（{clicks} 次点击 / {keys} 个按键，"
                 f"速度 {speed * 100:.0f}%）——")
        start = self.game.now()
        last_t = 0.0
        try:
            base_t = min(float(e.get("t", 0.0)) for e in events)
        except (TypeError, ValueError):
            base_t = 0.0
        for ev in events:
            if self._stopping():
                return False
            try:
                target_t = max(0.0, (float(ev.get("t", 0.0)) - base_t) / speed)
            except (TypeError, ValueError):
                target_t = last_t
            wait = target_t - (self.game.now() - start)
            if wait > 0:
                self.game.sleep(wait)
            last_t = max(last_t, target_t)
            typ = str(ev.get("type", ""))
            if typ == "down" and str(ev.get("button", "left")) == "left":
                nx, ny = ev.get("nx"), ev.get("ny")
                if nx is None or ny is None:
                    continue
                if not self.game.click_norm((float(nx), float(ny))):
                    self.log("引路宏点击失败，停止回放")
                    return False
            elif typ == "key_down":
                name = str(ev.get("name", "")).strip()
                if name:
                    self.game.tap(name)
        self.gap("引路宏结束缓冲")
        self.release_focus()
        self.log("引路宏回放完成")
        return True

    def _progress_spec(self) -> dict:
        return ((self.p.get("enter") or {}).get("progress") or {})

    # ---- 动作缓冲 ----
    def release_focus(self) -> None:
        """把前台还给用户（混合模式下纯键盘阶段用）。"""
        fn = getattr(self.game, "release_focus", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass

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
        watch = min(travel, timeout) if timeout > 0 else travel
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
            radius = float(enter.get("map_radius", 0.02) or 0.02)
            pt = self._offset_point(enter.get("dungeon_pt"), attempt - 1, radius)
            if attempt > 1:
                self.log(f"—— 进本第 {attempt}/{attempts} 次：地图点微调 "
                         f"({pt[0]:.3f}, {pt[1]:.3f}) ——")
            else:
                self.log(f"—— 进本第 {attempt}/{attempts} 次：点副本入口 ——")
            # 点完就开始盯弹窗：寻路过程中就识别，弹窗一出现马上点
            if not self._goto(pt, watch, "副本入口",
                              watch_spec=spec, watch_desc="进入弹窗",
                              sleep_after=False):
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
                        self.release_focus()
                        break
                # 识别不到就早点盲点一次（不傻等 24s）
                if (not blind_done
                        and self.game.now() - start >= blind_after):
                    self.log(f"{blind_after:.0f}s 没识别到弹窗 → 先盲点一次「进入游戏」")
                    self.game.click_norm(confirm_pt)
                    self.release_focus()
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
                self.release_focus()
                return True
            loaded = self._loaded_spec()
            if (loaded or {}).get("roi") and self.wait_until(loaded, 3.0, "副本内特征"):
                self.gap("进本完成后缓冲")
                self.release_focus()
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

    def fatigue_value(self):
        """读一次疲劳值，返回当前值（读不到返回 None）。"""
        cfg = self.p.get("fatigue") or {}
        if not cfg.get("enabled", True):
            return None
        current, maximum, msg = self.fatigue_check()
        if current is None:
            self.log(f"疲劳读不到（{msg}）")
            return None
        self.log(f"当前疲劳 {current}/{maximum}")
        return current

    def resolve_mode(self) -> str:
        """联动：现在的模式该是「farm」还是「boss」。

        滞回切换（不会来回跳）：
          · 挂机中：疲劳 < -阈值  → 去打 BOSS
          · 打BOSS中：疲劳 > +阈值 → 回打怪点挂机
        """
        loop = self.p.get("loop") or {}
        cfg = self.p.get("fatigue") or {}
        default = str(loop.get("default_action") or "farm")
        threshold = abs(int(cfg.get("threshold", 600) or 0))
        if self._mode is None:
            self._mode = default
        current = self.fatigue_value()
        if current is None or not threshold:
            self.log(f"按当前模式继续：{self._mode}")
            return self._mode
        if self._mode == "farm" and current < -threshold:
            self._mode = "boss"
            self.log(f"疲劳 {current} < -{threshold} → 切到打BOSS")
            return "boss"
        if self._mode == "boss" and current > threshold:
            self._mode = "farm"
            self.log(f"疲劳 {current} > +{threshold} → 切回打怪点挂机")
            return "farm"
        self.log(f"疲劳 {current}（±{threshold}）→ 继续 {self._mode}")
        return self._mode

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

    def boss_hp_ratio(self) -> Optional[float]:
        """BOSS 血条区域的亮度占比（0~1）；没框/截图失败返回 None。"""
        spec = self._anchor_of("boss_hp")
        roi = (spec or {}).get("roi")
        if not roi:
            return None
        frame = self.grab()
        if frame is None:
            return None
        try:
            patch = crop_norm(frame, roi).astype(np.float32)
            bright = patch.max(axis=2)
            return float((bright > 70).mean())
        except Exception:
            return None

    def boss_hp_visible(self, baseline: Optional[float] = None) -> bool:
        """BOSS 血条还在不在（在=没死）。

        baseline 是开打时血条区域的亮度占比。此时用「相对基准下降到 drop_ratio」
        判死，比固定 0.15 更抗背景亮度/血条粗细变化；没有 baseline 时退回 min_fill。
        """
        spec = self._anchor_of("boss_hp")
        if not (spec or {}).get("roi"):
            return True  # 没框血条 → 用时间上限/身形判死
        ratio = self.boss_hp_ratio()
        if ratio is None:
            return True
        try:
            drop = max(0.10, min(0.95, float(spec.get("drop_ratio", 0.55) or 0.55)))
        except (TypeError, ValueError):
            drop = 0.55
        if baseline is not None and baseline > 0:
            need = max(0.01, float(baseline) * drop)
        else:
            need = max(0.01, float(spec.get("min_fill",
                                             spec.get("min_ink", 0.15)) or 0.15))
        self._last_hp_ratio = ratio
        self._last_hp_need = need
        return ratio >= need

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
            radius = float(farm.get("map_radius", 0.02) or 0.02)
            pt = self._offset_point(farm.get("boss_pt"), attempt - 1, radius)
            self.log(f"前往 BOSS 点（第 {attempt}/{tries} 次"
                     + (f"，地图点微调 {pt[0]:.3f},{pt[1]:.3f}" if attempt > 1 else "")
                     + "）")
            # 走路时画面一直在动，先不识别；等停下来再看 BOSS
            if not self._goto(pt, travel, "BOSS 点", sleep_after=False):
                return False
            if not (spec or {}).get("roi"):
                self.game.sleep(travel)
                return True
            still_s = float(spec.get("still_seconds", 0.8) or 0.8)
            still_t = float(spec.get("still_timeout", 12.0) or 12.0)
            still_d = float(spec.get("still_diff", 0.015) or 0.015)
            self.release_focus()
            self.log("等角色停下（画面不动了再识别 BOSS）")
            self.wait_scene_still(still_s, still_t, still_d)
            self.gap("到 BOSS 点后缓冲")
            if self.wait_until(spec, 6.0, "BOSS 身形"):
                return True
            self.log("没识别到「BOSS 身形」→ 回到上一步重新寻路")
        self.log(f"BOSS 点确认失败：{tries} 次都没识别到 BOSS 身形")
        return False

    def fight_until_finish(self) -> bool:
        """③ 挂机打怪：每次平A都拾取；BOSS 血条消失（连续 N 次看不到）= 打完。"""
        farm = self._farm()
        loop = self.p.get("loop") or {}
        seconds = min(1000.0,
                      float(loop.get("boss_seconds", 120.0) or 120.0))
        interval = max(0.05, float(farm.get("interval_ms", 250)) / 1000.0)
        pickup_key = str(farm.get("pickup_key") or self._key("pickup", "space"))
        pickup_on_a = bool(farm.get("pickup_on_a", True))
        pickup_every = float(farm.get("pickup_every", 8.0) or 0.0)
        potion_every = float(farm.get("potion_every", 6.0) or 0.0)
        potion_keys = [str(k) for k in (farm.get("potion_keys") or ["q"]) if str(k)]
        hp_threshold = int(farm.get("hp_threshold", 0) or 0)
        hp_every = float(farm.get("hp_check_every", 2.0) or 2.0)
        use_skills = bool(farm.get("use_skills", True))
        hp_spec = self._anchor_of("boss_hp")
        body_spec = self._anchor_of("boss_present")
        has_hp = bool((hp_spec or {}).get("roi"))
        has_body = bool((body_spec or {}).get("roi"))
        use_body = bool(farm.get("death_by_body", False))
        if not has_hp and has_body:
            use_body = True
            self.log("没框 BOSS 血条 → 用「BOSS 身形消失」判死")
        if not has_hp and not has_body:
            self.log("没框血条也没框身形 → 只能按「打BOSS上限」停，识别不了死亡")
        need_miss = max(1, int((hp_spec or {}).get("miss", 3) or 3))
        poll = max(0.2, float((self.p.get("detect") or {}).get("poll", 0.5)))
        loop_fat = float(loop.get("fatigue_every", 60.0) or 0.0)
        watch_fatigue = bool((self.p.get("fatigue") or {}).get("enabled", True)) and loop_fat > 0
        hp_baseline = self.boss_hp_ratio() if has_hp else None
        if has_hp:
            if hp_baseline is None:
                self.log("BOSS 血条基准读取失败：这次按固定阈值判死")
            elif hp_baseline < 0.01:
                self.log("BOSS 血条基准亮度接近 0（可能没框准）→ 按固定阈值判死")
            else:
                drop = float((hp_spec or {}).get("drop_ratio", 0.55) or 0.55)
                self.log(f"BOSS 血条基准亮度 {hp_baseline * 100:.0f}%"
                         f"（降到 {hp_baseline * drop * 100:.0f}% 以下算消失）")

        self.release_focus()
        if has_hp:
            how = "血条消失" + ("/BOSS 身形消失" if use_body and has_body else "")
        elif has_body and use_body:
            how = "BOSS 身形消失"
        else:
            how = "按时间停"
        self.log(f"开始打怪（上限 {seconds:.0f}s；{how}就算打完）")
        start = self.game.now()
        next_s1 = next_s2 = next_potion = next_pickup = next_hp = 0.0
        next_fatigue = next_pickup = next_check = 0.0
        next_body_check = 0.0
        next_hp_log = 0.0
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

            if (has_hp or (use_body and has_body)) and elapsed >= next_check:
                next_check = elapsed + (poll if has_hp else 1.0)
                visible = self.boss_hp_visible(hp_baseline) if has_hp else True
                ratio = getattr(self, "_last_hp_ratio", None)
                need = getattr(self, "_last_hp_need", None)
                body_gone = False
                if use_body and has_body and elapsed >= next_body_check:
                    next_body_check = elapsed + 1.0
                    # 血条还在时，再用身形确认一次（血条已经没了就不重复截图）
                    if not has_hp or visible:
                        body_gone = not self.boss_present()
                if visible and not body_gone:
                    misses = 0
                else:
                    misses += 1
                    why = []
                    if not visible and ratio is not None and need is not None:
                        why.append(f"血条亮度 {ratio * 100:.0f}% < {need * 100:.0f}%")
                    elif not visible:
                        why.append("血条不见了")
                    if body_gone:
                        why.append("BOSS 身形不见")
                    self.log(f"BOSS 可能死了（{misses}/{need_miss}）："
                             + "；".join(why))
                    if misses >= need_miss:
                        self.log(f"连续 {misses} 次不见 → 判定打完")
                        break
                if has_hp and elapsed >= next_hp_log:
                    next_hp_log = elapsed + 5.0
                    self.log(f"血条亮度 {ratio * 100:.0f}%"
                             f"（判死 {need * 100:.0f}%）"
                             + ("；身形在" if not body_gone else "；身形不见"))

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

    def farm_round(self) -> bool:
        """挂机：在「打怪点」一直打，每 fatigue_every 秒查一次疲劳。

        · 不设挂机总时长；疲劳 < -阈值 才结束挂机（下一轮自动去打 BOSS）
        · 疲劳在中间区间就一直打；> +阈值 也继续打（本来就在打小怪）
        """
        farm = self._farm()
        loop = self.p.get("loop") or {}
        every = max(10.0, float(loop.get("fatigue_every", 60.0) or 60.0))
        if not farm.get("farm_pt"):
            self.log("没标「打怪点」，没法挂机（请到「副本流程」页标一下）")
            return False
        self.log(f"—— 打怪点挂机（不设时长；每 {every:.0f}s 查一次疲劳，"
                 f"疲劳 <-阈值 去打BOSS）——")
        if not self._goto(farm.get("farm_pt"),
                          float(farm.get("travel_wait", 30.0)), "打怪点"):
            return False
        self.gap("到打怪点后缓冲")
        self.release_focus()
        while not self._stopping():
            self.combat(every, desc="打怪点挂机", action="farm")
            if self._stopping():
                return True
            if self._switch_to == "boss":
                self._switch_to = None
                self._mode = "boss"
                self.log("疲劳 < -阈值 → 切到打BOSS（结束挂机）")
                return True
        return True

    # -- 兼容旧接口：固定时长打 BOSS 点 --
    def farm_at_boss(self, seconds: Optional[float] = None) -> bool:
        farm = self._farm()
        loop = self.p.get("loop") or {}
        if not self._goto(farm.get("boss_pt"),
                          float(farm.get("travel_wait", 30.0)), "BOSS 点"):
            return False
        self.gap("到 BOSS 点后缓冲")
        return self.combat(
            min(1000.0, seconds if seconds is not None
                else float(loop.get("boss_seconds", 120.0) or 120.0)),
            desc="BOSS 点挂机",
        )

    def combat(self, seconds: float, desc: str = "挂机",
               action: Optional[str] = None) -> bool:
        """通用挂机：平A + 技能 + 血量低喝药 + 定时拾取。

        action 不为空时，每 loop.fatigue_every 秒查一次疲劳，
        发现该换分支（打BOSS <-> 打怪）就提前结束本次挂机。
        """
        self._switch_to = None
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
        potion_keys = [str(k) for k in (farm.get("potion_keys") or ["q"]) if str(k)]
        hp_threshold = int(farm.get("hp_threshold", 0) or 0)
        hp_every = float(farm.get("hp_check_every", 2.0) or 2.0)
        use_skills = bool(farm.get("use_skills", True))

        self.release_focus()
        self.log(f"{desc}：{seconds:.0f}s（喝药 {potion_every:.0f}s / "
                 f"拾取 {pickup_every:.0f}s）")
        start = self.game.now()
        next_s1 = next_s2 = 0.0
        next_potion = 0.0
        next_pickup = pickup_every
        next_hp_check = 0.0
        hp_low = False

        pickup_on_a = bool(farm.get("pickup_on_a", True))
        bg_warned = False
        while not self._stopping():
            elapsed = self.game.now() - start
            # 先查疲劳：farm_round 里 seconds 可能正好等于 fatigue_every，
            # 如果先判断「到点结束」就永远查不到疲劳，会死在挂机循环里。
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
            if elapsed >= seconds:
                break
            if not self.game.tap("a") and not bg_warned:
                bg_warned = True
                self.log("后台平A发送失败：点「测试后台平A」看目标窗口；"
                         "游戏要用管理员运行，窗口别最小化")
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
        """退出副本后读一次疲劳，只更新下一次该走哪个分支，不再定时挂机。"""
        cfg = self.p.get("fatigue") or {}
        if not cfg.get("enabled", True):
            return
        current, maximum, msg = self.fatigue_check()
        if current is None:
            self.log(f"疲劳检测失败（{msg}），按当前分支继续")
            return
        threshold = abs(int(cfg.get("threshold", 600) or 0))
        self._mode = self.action_for_fatigue(current)
        self.log(f"当前疲劳 {current}/{maximum}（±{threshold}）→ "
                 f"下一轮走 {'打BOSS' if self._mode == 'boss' else '打怪点挂机'}")

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

    def deposit_to_warehouse(self) -> bool:
        """一键放入仓库：点仓库图标 → 点「一键放入」→ 按 2 下 ESC 回主界面。"""
        cfg = self.p.get("warehouse") or {}
        if not cfg.get("enabled", True):
            return True
        icon = cfg.get("icon_pt")
        deposit = cfg.get("deposit_pt")
        if not icon or not deposit:
            self.log("没标「仓库图标 / 一键放入」点位，跳过放仓库")
            return True
        gap = max(0.0, float(cfg.get("gap", 0.5) or 0.0))
        esc_times = max(0, int(cfg.get("esc_times", 2) or 0))
        open_wait = max(0.0, float(cfg.get("open_wait", 1.5) or 0.0))

        self.log("—— 一键放入仓库 ——")
        self.log("点「仓库图标」打开仓库")
        if not self.game.click_norm(icon):
            self.log("点仓库图标失败，跳过放仓库")
            return False
        self.game.sleep(gap + open_wait)
        self.log("点「一键放入」")
        if not self.game.click_norm(deposit):
            self.log("点「一键放入」失败，跳过")
            return False
        self.game.sleep(gap)
        for i in range(esc_times):
            self.log(f"按 ESC 回主界面（{i + 1}/{esc_times}）")
            self.game.tap("esc")
            self.game.sleep(gap)
        self.release_focus()
        return True

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
                self.release_focus()
                self.game.sleep(confirm_wait)
                self.game.sleep(settle)
                return True
            if self.wait_anchor(spec, timeout, poll, "退出确认弹窗"):
                self.log("点「确认」→ 等退出加载")
                self.game.click_norm(confirm_pt)
                self.release_focus()
                self.game.sleep(confirm_wait)
                self.game.sleep(settle)
                return True
            self.log(f"没识别到退出确认弹窗（等了 {timeout:.0f}s），再点一次「退出副本」")
        self.log(f"退出失败：{attempts} 次都没识别到退出确认弹窗")
        return False

    # -- 一轮 --
    def run_round(self, seconds: Optional[float] = None) -> bool:
        """一轮：按疲劳决定【打怪点挂机】还是【打BOSS副本】。"""
        self.log("=== 开始一轮 ===")
        # 只有「前台模式 + 允许抢占」才需要一开始就切前台；
        # 混合模式只在真正点击时借一下鼠标，点完马上还回去。
        try:
            mode_name = self.game.input_mode()
        except Exception:
            mode_name = "hybrid"
        if mode_name == "foreground" and getattr(self.game, "allow_grab", lambda: False)():
            self.log("前台模式：整个流程占用鼠标键盘")
            try:
                self.game.activate()
            except Exception:
                pass
        loop = self.p.get("loop") or {}
        if bool(loop.get("link_fatigue", True)):
            mode = self.resolve_mode()
        else:
            mode = "boss"
        if mode == "farm":
            self._mode = "farm"
            if not self.farm_round():
                return self._failed("打怪点挂机失败")
            return True
        self._mode = "boss"
        return self.boss_round(seconds)

    def boss_round(self, seconds: Optional[float] = None) -> bool:
        """一次 BOSS 副本：进本 → BOSS 点 → 打怪 → 拾取 → 退出。

        勾了「引路宏」时，进本 + 跑到 BOSS 这段改播放录制的鼠标宏，
        不再依赖地图点位；宏跑完若没看到 BOSS 身形，可选择回退到地图点位。
        """
        if self.nav_macro_enabled():
            self.log("用引路宏导航（鼠标宏）：进本/跑向 BOSS 不再点地图")
            if not self.play_nav_macro():
                return self._failed("引路宏回放失败")
            spec = self._anchor_of("boss_present")
            if (spec or {}).get("roi"):
                still_s = float(spec.get("still_seconds", 0.8) or 0.8)
                still_t = float(spec.get("still_timeout", 12.0) or 12.0)
                still_d = float(spec.get("still_diff", 0.015) or 0.015)
                self.release_focus()
                self.log("引路宏跑完，等角色停下再识别 BOSS")
                self.wait_scene_still(still_s, still_t, still_d)
                self.gap("到 BOSS 点后缓冲")
                if self.wait_until(spec, 6.0, "BOSS 身形"):
                    if not self.fight_until_finish():
                        return self._failed("BOSS 点打怪失败")
                elif self.nav_macro().get("fallback", False):
                    self.log("引路宏没识别到 BOSS 身形 → 回退到地图点位")
                    if not self.enter_dungeon():
                        return self._failed("进副本失败")
                    if not self.run_loops():
                        return self._failed("BOSS 点打怪失败")
                else:
                    return self._failed(
                        "引路宏没到 BOSS（可勾「宏失败回退地图点位」）"
                    )
            else:
                self.log("没框 BOSS 身形 → 引路宏跑完直接开打")
                if not self.fight_until_finish():
                    return self._failed("BOSS 点打怪失败")
        else:
            if not self.enter_dungeon():
                return self._failed("进副本失败")
            if not self.run_loops():
                return self._failed("BOSS 点打怪失败")
        self.loot_before_exit()
        if not self.exit_dungeon():
            return self._failed("退出副本失败")
        self.gap("退出后缓冲")
        self.release_focus()
        wh = self.p.get("warehouse") or {}
        if wh.get("enabled", True) and str(wh.get("after", "exit")) == "exit":
            if not self.deposit_to_warehouse():
                return self._failed("放入仓库失败")
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
    """列出候选游戏窗口。

    会排除：本程序自己的窗口（标题含“后台按键器”、或属于本进程）、
    没有标题的窗口、不可见窗口。
    """
    import os as _os

    my_pid = _os.getpid()
    found: list[tuple[int, str]] = []

    def cb(hwnd, _param):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        if "后台按键器" in title:
            return True
        try:
            pid = wt.DWORD()
            _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) == my_pid:
                return True
        except Exception:
            pass
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
    parser.add_argument("--farm-seconds", "--boss-seconds", dest="boss_seconds",
                        type=float, default=None,
                        help="覆盖 BOSS 战上限（秒，最大 1000；不是挂机时长）")
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
            runner.run(args.rounds, args.boss_seconds)
        except KeyboardInterrupt:
            print("\n已中断")
            return 130
        return 0 if runner.stats["fail"] == 0 else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
