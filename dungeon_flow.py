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
    "enter": {
        "dungeon_pt": None,        # 地图1 上的副本入口 [nx, ny]
        "travel_wait": 40.0,       # 走路/寻路到副本口要多久
        "dialog": {"roi": None, "sig": [], "grid": 12, "sim": 0.90},
        "entry_pt": None,          # 可选：走到副本口后要点的「门口/传送门/NPC」位置
        "confirm_pt": None,        # 弹窗里「进入游戏」按钮 [nx, ny]
        "dialog_timeout": 20.0,    # 等弹窗最多多久
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
        "travel_wait": 30.0,       # 点完后等自动寻路到 BOSS 附近
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

    def _goto(self, point, wait: float, desc: str) -> bool:
        if not point:
            self.log(f"「{desc}」没有标定，跳过")
            return False
        self.ensure_mount()
        self.log(f"点「{desc}」({float(point[0]):.3f}, {float(point[1]):.3f})"
                 f" → 等自动寻路 {wait:.0f}s")
        self.open_map()
        if not self.game.click_norm(point):
            self.log(f"点击「{desc}」失败")
            return False
        self.game.sleep(wait)
        return True

    # -- ① 进本 --
    def _wait_dialog(self, timeout: float, poll: float,
                     sim: Optional[float] = None,
                     desc: str = "进本弹窗") -> bool:
        """等进本弹窗出现（可传更宽松的相似度做二次判定）。"""
        spec = dict(self.enter_dialog_spec() or {})
        if sim is not None:
            spec["sim"] = float(sim)
        return self.wait_anchor(spec, timeout, poll, desc)

    def _try_enter_popup(self, timeout: float, poll: float,
                         confirm_pt) -> bool:
        """识别弹窗并点「进入游戏」；识别不到就放宽阈值再试，最后兜底点一次。"""
        spec = self.enter_dialog_spec() or {}
        load_wait = float((self.p.get("enter") or {}).get("load_wait", 10.0))

        if self._wait_dialog(timeout, poll, desc="进本弹窗"):
            self.log("识别到进本弹窗 → 点「进入游戏」")
            self.game.click_norm(confirm_pt)
            self.game.sleep(load_wait)
            return True

        if spec.get("roi"):
            base = float(spec.get("sim", 0.90))
            relaxed = max(0.55, base - 0.20)
            self.log(f"严格阈值没匹配到，用放宽阈值 {relaxed:.2f} 再看 6 秒…")
            if self._wait_dialog(6.0, poll, sim=relaxed, desc="进本弹窗(放宽)"):
                self.log("放宽阈值识别到弹窗 → 点「进入游戏」")
                self.game.click_norm(confirm_pt)
                self.game.sleep(load_wait)
                return True

        # 兜底：很多游戏弹窗其实已经出来了，只是没匹配上 → 盲点一次
        self.log("没识别到弹窗 → 兜底点一次「进入游戏」（日志会存一张截图）")
        try:
            self.game.snapshot("enter_dialog_miss")
        except Exception:
            pass
        ok_click = bool(self.game.click_norm(confirm_pt))
        self.game.sleep(load_wait)
        return ok_click

    def enter_dungeon(self) -> bool:
        """① 进本：点副本入口 → 等自动寻路 →（可选点门口）→ 识别弹窗 → 点「进入游戏」。"""
        enter = self.p.get("enter") or {}
        confirm_pt = enter.get("confirm_pt")
        entry_pt = enter.get("entry_pt")
        travel = float(enter.get("travel_wait", 40.0))
        timeout = float(enter.get("dialog_timeout", 20.0))
        attempts = max(1, int(enter.get("retry", 3) or 1))
        poll = max(0.2, float((self.p.get("detect") or {}).get("poll", 0.5)))

        if not enter.get("dungeon_pt"):
            self.log("没有标定「副本入口」，跳过进本这一步")
            return True
        if not confirm_pt:
            self.log("没有标定「进入游戏」按钮，无法确认进本")
            return False

        for attempt in range(1, attempts + 1):
            if self._stopping():
                return False
            self.log(f"进本第 {attempt}/{attempts} 次：点副本入口")
            if not self._goto(enter.get("dungeon_pt"), travel, "副本入口"):
                return False
            if entry_pt:
                # 有的游戏到门口还要点一下传送门/NPC 才弹窗
                self.log(f"到了 → 点一次「副本门口/NPC」"
                         f"({float(entry_pt[0]):.3f}, {float(entry_pt[1]):.3f})")
                self.game.click_norm(entry_pt)
                self.game.sleep(1.2)
            if self._try_enter_popup(timeout, poll, confirm_pt):
                return True
            self.log(f"这次没进去，重新开地图再点一次（{attempt}/{attempts}）")
        self.log(f"进本失败：{attempts} 次都没能进去")
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

    def run_loops(self) -> bool:
        """按疲劳分流跑循环；挂机途中疲劳变了会自动换分支（最多 max_switch 次）。"""
        farm = self._farm()
        loop = self.p.get("loop") or {}
        action = self.decide_action()
        max_switch = max(0, int(loop.get("max_switch", 3) or 0))
        switched = 0
        while True:
            self._switch_to = None
            if not self._run_branch(action):
                return False
            want = self._switch_to
            if not want or want == action or switched >= max_switch:
                return True
            switched += 1
            self.log(f"疲劳变化 → 从「{action}」切到「{want}」（第 {switched} 次）")
            action = want

    def _run_branch(self, action: str) -> bool:
        farm = self._farm()
        loop = self.p.get("loop") or {}
        if action == "boss":
            rounds = max(1, int(loop.get("boss_rounds", 1) or 1))
            seconds = float(loop.get("boss_seconds", farm.get("seconds", 30.0)) or 30.0)
            for idx in range(1, rounds + 1):
                if self._stopping():
                    return False
                if not self._goto(farm.get("boss_pt"),
                                  float(farm.get("travel_wait", 30.0)),
                                  f"BOSS 点({idx}/{rounds})"):
                    return False
                if not self.combat(seconds, desc=f"打BOSS{idx}/{rounds}",
                                   action=action):
                    return False
        else:
            rounds = max(1, int(loop.get("trash_rounds", 1) or 1))
            seconds = float(loop.get("trash_seconds", 60.0) or 60.0)
            if not self._goto(farm.get("farm_pt"),
                              float(farm.get("travel_wait", 30.0)), "打怪点"):
                self.log("没标「打怪点」，就在原地打")
            for idx in range(1, rounds + 1):
                if self._stopping():
                    return False
                if not self.combat(seconds, desc=f"打怪{idx}/{rounds}",
                                   action=action):
                    return False
        return True

    # -- ② BOSS 点挂机 --
    def farm_at_boss(self, seconds: Optional[float] = None) -> bool:
        farm = self._farm()
        if not self._goto(farm.get("boss_pt"),
                          float(farm.get("travel_wait", 30.0)), "BOSS 点"):
            return False
        total = float(seconds if seconds is not None else farm.get("seconds", 30.0))
        return self.combat(total, desc="BOSS 点挂机")

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

        while not self._stopping():
            elapsed = self.game.now() - start
            if elapsed >= seconds:
                break
            self.game.tap("a")
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
        self.log("=== 开始一轮 ===")
        if getattr(self.game, "foreground_mode", None) and self.game.foreground_mode():
            self.log("切到游戏窗口（前台模式：会用真实鼠标键盘）")
            try:
                self.game.activate()
            except Exception:
                pass
        if not self.enter_dungeon():
            return self._failed("进本失败")
        farm = self._farm()
        # 先到「打怪点」，在那里读疲劳决定做什么
        if farm.get("farm_pt"):
            if not self._goto(farm.get("farm_pt"),
                              float(farm.get("travel_wait", 30.0)), "打怪点"):
                return self._failed("没走到打怪点")
        if seconds is not None:
            # 命令行 --farm-seconds：直接按固定时长打 BOSS 点（老行为）
            if not self.farm_at_boss(seconds):
                return self._failed("没走到 BOSS 点")
        elif not self.run_loops():
            return self._failed("打怪/BOSS 循环失败")
        if not self.exit_dungeon():
            return self._failed("退出副本失败")
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
