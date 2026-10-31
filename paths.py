#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路径执行 / 路径编辑.

路径步骤 = 地图 + 已标注目标 + 等待。

运行时（不使用 OCR 识别地图名）：
    1. 用步骤里的地图名找到已标注地图；
    2. 按 M 打开地图 → 点击目标 → 关闭地图；
    3. 点击后检查画面：没动或提示“无法到达”时自动再点一次；
    4. 如果下一步是另一张地图，则等待黑屏/加载/硬切等场景切换动画，
       检测到切换就认为进入了下一张地图；没检测到就在入口附近绕圈重试；
    5. 等待，再执行下一步。
"""

from __future__ import annotations

import datetime
import json
import math
import os
import sys
import time
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

import win32gui

from map_labels import (
    NO_MOVE_DIFF_RATIO,
    PROMPT_CONFIRM_KEYWORDS,
    REACH_FAIL_KEYWORDS,
    SCENE_BIG_DIFF,
    SCENE_DARK_BRIGHTNESS,
    SCENE_LIGHT_BRIGHTNESS,
    SCENE_RECOVER_DIFF,
    SCENE_RECOVER_SECONDS,
    SCENE_STABLE_DIFF,
    SCENE_STABLE_FRAMES,
    TARGET_KINDS,
    _app,
    _valid_map_xy,
    _valid_rect,
    frame_diff_ratio,
    load_maps,
    mean_brightness,
    prompt_has_keyword,
)


APP_FOLDER = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"),
    "CreatureCuriosKeyer",
)
PATHS_PATH = os.path.join(APP_FOLDER, "paths.json")
PATH_LOG_PATH = os.path.join(APP_FOLDER, "path_run.log")

# 地图上的蓝色人物箭头：满足这些颜色条件的像素视为箭头候选。
BLUE_ARROW_B_MIN = 120
BLUE_ARROW_B_OVER_R = 30
BLUE_ARROW_B_OVER_G = 0
BLUE_ARROW_MIN_PIXELS = 12
BLUE_ARROW_RADIUS_NORM = 0.035


def _compact(text: str) -> str:
    return "".join(
        ch for ch in str(text or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff"
    )


def load_paths() -> list[dict]:
    try:
        with open(PATHS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        paths = data.get("paths", [])
        if isinstance(paths, list):
            return [p for p in paths if isinstance(p, dict)]
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return []


def save_paths(paths: list[dict]) -> bool:
    os.makedirs(os.path.dirname(PATHS_PATH), exist_ok=True)
    tmp = PATHS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"paths": paths}, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PATHS_PATH)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def find_target(profile: dict, target_name: str) -> Optional[dict]:
    wanted = _compact(target_name)
    for target in profile.get("targets", []):
        if not isinstance(target, dict):
            continue
        if _compact(target.get("name", "")) == wanted:
            return target
    for target in profile.get("targets", []):
        if not isinstance(target, dict):
            continue
        if wanted and wanted in _compact(target.get("name", "")):
            return target
    return None


class PathRunner(QThread):
    status = Signal(str, str)
    done = Signal()
    failed = Signal(str)

    def __init__(
        self,
        hwnd: int,
        path: dict,
        profiles: list[dict],
        map_key: str = "m",
        close_key: str = "esc",
        mount_key: str = "f",
        parent=None,
    ):
        super().__init__(parent)
        self.hwnd = int(hwnd)
        self.path = dict(path)
        self.profiles = list(profiles)
        self.map_key = str(map_key or "m")
        self.close_key = str(close_key or "esc")
        self.mount_key = str(mount_key or "")
        self._stop = False
        self._map_open = False

    def stop(self) -> None:
        self._stop = True

    def log(self, text: str) -> None:
        line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {text}"
        try:
            with open(PATH_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        print(line)
        try:
            self.status.emit(text, "warn")
        except Exception:
            pass

    def _sleep(self, seconds: float) -> bool:
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end:
            if self._stop:
                return False
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))
        return not self._stop

    def _tap_key(self, name: str) -> None:
        app = _app()
        if app is None:
            raise RuntimeError("主程序模块不可用。")
        app._force_foreground_window(self.hwnd)
        time.sleep(0.1)
        app._send_key_name(name, True)
        time.sleep(0.05)
        app._send_key_name(name, False)

    def _click_norm(self, nx: float, ny: float) -> None:
        app = _app()
        if app is None:
            raise RuntimeError("主程序模块不可用。")
        _l, _t, cw, ch = win32gui.GetClientRect(self.hwnd)
        cw, ch = int(cw), int(ch)
        if cw <= 0 or ch <= 0:
            raise RuntimeError("游戏客户区尺寸无效。")
        x = max(1, min(cw - 2, int(round(nx * cw))))
        y = max(1, min(ch - 2, int(round(ny * ch))))
        sx, sy = win32gui.ClientToScreen(self.hwnd, (x, y))
        app._force_foreground_window(self.hwnd)
        time.sleep(0.1)
        app._send_mouse_move_to(int(sx), int(sy))
        time.sleep(0.06)
        app._send_mouse_button("left", True)
        time.sleep(0.07)
        app._send_mouse_button("left", False)
        time.sleep(0.15)

    def _ensure_no_exit_menu(self) -> None:
        """如果当前已经出现“退出游戏”菜单，按一次 Esc 关掉；不额外多按。"""
        app = _app()
        if app is None or not hasattr(app, "ocr_screen_items"):
            return
        cap, _err = app.capture_client_bgra(self.hwnd)
        if cap is None:
            return
        w, h, data = cap
        try:
            items = app.ocr_screen_items(data, w, h)
        except Exception:
            return
        texts = [_compact(it.get("text", "")) for it in items]
        if any("退出游戏" in t for t in texts if t):
            self.log("检测到“退出游戏”菜单，先按一次 Esc 关闭")
            self._tap_key(self.close_key)
            time.sleep(0.5)

    def _close_map_safely(self) -> None:
        # 只按一次：地图是 Esc 关闭的；多按会打开别的菜单
        self._tap_key(self.close_key)
        time.sleep(0.55)
        app = _app()
        if app is None or not hasattr(app, "ocr_screen_items"):
            return
        cap, _err = app.capture_client_bgra(self.hwnd)
        if cap is None:
            return
        w, h, data = cap
        try:
            items = app.ocr_screen_items(data, w, h)
        except Exception:
            return
        texts = [_compact(it.get("text", "")) for it in items]
        if any(("退出游戏" in t or "退出" in t) for t in texts if t):
            self.log("检测到“退出游戏”菜单，再按一次 Esc 关闭")
            self._tap_key(self.close_key)
            time.sleep(0.5)

    def _capture(self):
        app = _app()
        if app is None or not hasattr(app, "capture_client_bgra"):
            return None
        cap, _err = app.capture_client_bgra(self.hwnd)
        return cap

    def _ocr_items(self, cap) -> list[dict]:
        app = _app()
        if app is None or cap is None or not hasattr(app, "ocr_screen_items"):
            return []
        try:
            w, h, data = cap
            return list(app.ocr_screen_items(data, w, h))
        except Exception:
            return []

    def _dismiss_prompt(self, items: list[dict]) -> None:
        clicked = False
        for item in items or []:
            if prompt_has_keyword([item], PROMPT_CONFIRM_KEYWORDS):
                try:
                    self._click_norm(float(item["nx"]), float(item["ny"]))
                    clicked = True
                    break
                except Exception:
                    pass
        if not clicked:
            self._tap_key(self.close_key)
        if not self._sleep(0.45):
            raise RuntimeError("已停止。")
        # 提示框可能盖在地图上，关掉提示后再确保地图/菜单已关闭。
        self._close_map_safely()

    def _wait_map_closed(self, map_cap) -> None:
        """等地图界面真正消失，避免把“地图→世界”的变化当成人物移动。"""
        if not map_cap:
            if not self._sleep(0.8):
                raise RuntimeError("已停止。")
            return
        end = time.monotonic() + 2.5
        closed = False
        while time.monotonic() < end:
            if not self._sleep(0.15):
                raise RuntimeError("已停止。")
            cap = self._capture()
            if cap and frame_diff_ratio(map_cap, cap) >= 0.05:
                closed = True
                break
        if not closed:
            self.log("关闭地图后画面仍像地图，再按一次关闭键")
            self._close_map_safely()
            if not self._sleep(0.6):
                raise RuntimeError("已停止。")

    def _travel_once(self, nx: float, ny: float):
        self._tap_key(self.map_key)
        if not self._sleep(1.2):
            raise RuntimeError("已停止。")
        map_cap = self._capture()
        self._click_norm(nx, ny)
        if not self._sleep(0.5):
            raise RuntimeError("已停止。")
        self._close_map_safely()
        self._wait_map_closed(map_cap)
        return map_cap

    def _travel_with_retry(
        self,
        nx: float,
        ny: float,
        label: str = "",
        check_move: bool = True,
        max_attempts: int = 2,
    ) -> None:
        """点击目标后检查是否真的开始寻路；没动或提示无法到达时再点一次。"""
        for attempt in range(1, max_attempts + 1):
            self._travel_once(nx, ny)
            if attempt >= max_attempts:
                return
            self.log("检查点击后是否开始移动…")
            # 多等一会儿，避开地图关闭/淡出的收尾动画，再取基线帧。
            if not self._sleep(1.2):
                raise RuntimeError("已停止。")
            cap_a = self._capture()
            items = self._ocr_items(cap_a)
            if prompt_has_keyword(items, REACH_FAIL_KEYWORDS):
                self.log("检测到“无法到达”提示，关闭提示后重试一次")
                self._dismiss_prompt(items)
                continue
            if not check_move:
                return
            if prompt_has_keyword(items, PROMPT_CONFIRM_KEYWORDS):
                self.log("检测到游戏弹窗，先不重试")
                return
            if not self._sleep(1.2):
                raise RuntimeError("已停止。")
            cap_b = self._capture()
            ratio = frame_diff_ratio(cap_a, cap_b)
            if ratio < NO_MOVE_DIFF_RATIO:
                # 再等一会儿确认：可能是加载画面，不要刚点完就重试。
                if not self._sleep(1.2):
                    raise RuntimeError("已停止。")
                cap_c = self._capture()
                late_items = self._ocr_items(cap_c)
                if prompt_has_keyword(late_items, REACH_FAIL_KEYWORDS):
                    self.log("检测到“无法到达”提示，关闭提示后重试一次")
                    self._dismiss_prompt(late_items)
                    continue
                if prompt_has_keyword(late_items, PROMPT_CONFIRM_KEYWORDS):
                    self.log("检测到游戏弹窗，先不重试")
                    return
                ratio2 = frame_diff_ratio(cap_b, cap_c)
                if ratio2 < NO_MOVE_DIFF_RATIO:
                    self.log(
                        f"点击后画面一直没有变化（差异 {ratio:.4f}/{ratio2:.4f}），重试一次"
                    )
                    continue
                self.log(f"已开始移动（画面差异 {ratio2:.4f}）")
                return
            self.log(f"已开始移动（画面差异 {ratio:.4f}）")
            return

    def _target_norm(self, profile: dict, target: dict) -> tuple[float, float]:
        rect = _valid_rect(profile.get("map_rect_norm"))
        mx, my = _valid_map_xy(target)
        if rect is not None and mx is not None and my is not None:
            x0, y0, x1, y1 = rect
            return (
                max(0.0, min(1.0, x0 + mx * (x1 - x0))),
                max(0.0, min(1.0, y0 + my * (y1 - y0))),
            )
        nx = float(target.get("nx", 0.5))
        ny = float(target.get("ny", 0.5))
        return max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny))

    def _profile_by_name(self, name: str) -> Optional[dict]:
        wanted = _compact(name)
        if not wanted:
            return None
        for profile in self.profiles:
            if _compact(str(profile.get("name", ""))) == wanted:
                return profile
        for profile in self.profiles:
            if self._same_map(str(profile.get("name", "")), name):
                return profile
        return None

    @staticmethod
    def _same_map(a: str, b: str) -> bool:
        ca, cb = _compact(a), _compact(b)
        if not ca or not cb:
            return False
        return ca in cb or cb in ca

    def _wait_scene_transition(self, timeout: float) -> bool:
        """不依赖 OCR：通过黑屏/白屏/硬切 + 画面重新稳定判断场景切换。"""
        if timeout <= 0:
            return False
        self.log(f"等待场景切换动画（最多 {timeout:.0f} 秒，不识别地图文字）…")
        prev = self._capture()
        if prev is None:
            return False
        start_bright = mean_brightness(prev)
        prev_bright = start_bright
        # 游戏本身可能偏暗/偏亮：黑屏/白屏阈值按初始亮度自适应。
        dark_limit = min(SCENE_DARK_BRIGHTNESS, max(8.0, start_bright * 0.45))
        light_limit = max(
            SCENE_LIGHT_BRIGHTNESS, min(250.0, start_bright * 1.7)
        )
        self.log(
            f"场景检测基准：亮度 {start_bright:.0f}，"
            f"黑屏阈值 {dark_limit:.0f}，白屏阈值 {light_limit:.0f}"
        )
        start = time.monotonic()
        signal_time: Optional[float] = None
        signal_cap = None
        recovered_at: Optional[float] = None
        stable = 0
        last_log = 0.0
        while True:
            if self._stop:
                raise RuntimeError("已停止。")
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                break
            if not self._sleep(0.15):
                raise RuntimeError("已停止。")
            cap = self._capture()
            if cap is None:
                continue
            diff = frame_diff_ratio(prev, cap)
            bright = mean_brightness(cap)
            now = time.monotonic()
            if now - last_log >= 1.0:
                last_log = now
                self.log(f"场景检测：亮度 {bright:.0f}，变化 {diff:.3f}")
            if (
                bright <= dark_limit
                or bright >= light_limit
                or diff >= SCENE_BIG_DIFF
                or abs(bright - prev_bright) >= 55.0
            ):
                if signal_time is None:
                    self.log(
                        f"检测到场景切换动画（亮度 {bright:.0f}，变化 {diff:.3f}）…"
                    )
                    signal_time = now
                    signal_cap = cap
                stable = 0
                recovered_at = None
            elif signal_time is not None:
                normal = 45.0 <= bright <= 212.0
                if normal and diff < SCENE_STABLE_DIFF:
                    stable += 1
                    if stable >= SCENE_STABLE_FRAMES:
                        self.log("场景切换完成（画面已稳定）")
                        return True
                else:
                    stable = 0
                since_signal = (
                    frame_diff_ratio(signal_cap, cap) if signal_cap else 1.0
                )
                if normal and since_signal >= SCENE_RECOVER_DIFF:
                    if recovered_at is None:
                        recovered_at = now
                    elif now - recovered_at >= SCENE_RECOVER_SECONDS:
                        self.log("场景切换完成（新场景已出现）")
                        return True
                else:
                    recovered_at = None
            prev = cap
            prev_bright = bright
        self.log(f"{timeout:.0f} 秒内没有检测到场景切换")
        return False

    def _enter_next_map(
        self,
        profile: dict,
        target: dict,
        expected_map: str,
        timeout: float,
    ) -> bool:
        """靠场景切换动画判断是否进入新地图；没切换就在入口附近重试。"""
        if self._wait_scene_transition(timeout):
            self.log(f"已检测到场景切换，认为进入「{expected_map}」")
            # 再给新地图一点加载/淡入时间。
            if not self._sleep(1.5):
                raise RuntimeError("已停止。")
            return True

        base_nx, base_ny = self._target_norm(profile, target)
        rect = _valid_rect(profile.get("map_rect_norm"))
        if rect is not None:
            x0, y0, x1, y1 = rect
            rx = 0.05 * (x1 - x0)
            ry = 0.05 * (y1 - y0)
        else:
            rx = ry = 0.05
        self.log(
            f"没检测到进入「{expected_map}」的切换动画，在入口附近 5% 半径重试…"
        )
        per_try = max(4.0, min(8.0, timeout / 2.0))
        attempts = 0
        for loop in range(2):
            for k in range(8):
                if self._stop:
                    raise RuntimeError("已停止。")
                angle = 2.0 * math.pi * k / 8.0 + loop * math.pi / 8.0
                nx = base_nx + rx * math.cos(angle)
                ny = base_ny + ry * math.sin(angle)
                if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
                    continue
                attempts += 1
                self.log(f"绕圈第 {attempts} 次：{nx*100:.1f}%, {ny*100:.1f}%")
                self._travel_once(nx, ny)
                if self._wait_scene_transition(per_try):
                    self.log(f"绕圈后检测到场景切换：{expected_map}")
                    if not self._sleep(1.5):
                        raise RuntimeError("已停止。")
                    return True
        self.log(f"绕圈 {attempts} 次仍未检测到进入「{expected_map}」")
        return False

    def _mount(self) -> None:
        if not self.mount_key:
            return
        self.log(f"按 {self.mount_key.upper()} 骑乘…")
        self._tap_key(self.mount_key)
        if not self._sleep(1.0):
            raise RuntimeError("已停止。")

    def _ensure_map_open(self) -> None:
        if self._map_open:
            return
        self.log("打开地图…")
        self._tap_key(self.map_key)
        if not self._sleep(1.2):
            raise RuntimeError("已停止。")
        self._map_open = True

    def _click_target(self, profile: dict, target: dict) -> int:
        self._ensure_map_open()
        # 点击前先记下目标点附近已有的蓝色像素（地图图标可能也是蓝的），
        # 后面要求蓝色像素明显增加，才算“箭头到了”。
        baseline = self._blue_pixels_near(self._capture(), profile, target)
        nx, ny = self._target_norm(profile, target)
        self.log(
            f"点击地图目标：{nx*100:.1f}%, {ny*100:.1f}%"
            f"（点击前附近蓝色像素 {baseline}）"
        )
        self._click_norm(nx, ny)
        if not self._sleep(0.4):
            raise RuntimeError("已停止。")
        return baseline

    def _blue_pixels_near(
        self, cap, profile: dict, target: dict
    ) -> int:
        """统计目标点附近满足“蓝色箭头”颜色的像素数量。"""
        if cap is None:
            return 0
        rect = _valid_rect(profile.get("map_rect_norm"))
        if rect is None:
            return 0
        try:
            import numpy as np
        except Exception:
            return 0
        w, h, data = cap
        try:
            arr = np.frombuffer(data, dtype=np.uint8).reshape((h, w, 4))
        except Exception:
            return 0
        nx, ny = self._target_norm(profile, target)
        x0, y0, x1, y1 = rect
        rx = BLUE_ARROW_RADIUS_NORM * max(0.0001, x1 - x0)
        ry = BLUE_ARROW_RADIUS_NORM * max(0.0001, y1 - y0)
        left = max(0, int((nx - rx) * w))
        right = min(w, int((nx + rx) * w) + 1)
        top = max(0, int((ny - ry) * h))
        bottom = min(h, int((ny + ry) * h) + 1)
        if right <= left or bottom <= top:
            return 0
        crop = arr[top:bottom, left:right]
        b = crop[:, :, 0].astype(np.int16)
        g = crop[:, :, 1].astype(np.int16)
        r = crop[:, :, 2].astype(np.int16)
        mask = (
            (b >= BLUE_ARROW_B_MIN)
            & ((b - r) >= BLUE_ARROW_B_OVER_R)
            & ((b - g) >= BLUE_ARROW_B_OVER_G)
        )
        return int(mask.sum())

    def _wait_arrival_or_transition(
        self,
        profile: dict,
        target: dict,
        timeout: float,
        baseline_blue: int = 0,
    ) -> str:
        """地图一直开着，等蓝色箭头到达目标，或检测到场景切换。"""
        start = time.monotonic()
        prev = self._capture()
        if prev is None:
            return "timeout"
        start_bright = mean_brightness(prev)
        prev_bright = start_bright
        dark_limit = min(SCENE_DARK_BRIGHTNESS, max(8.0, start_bright * 0.45))
        light_limit = max(
            SCENE_LIGHT_BRIGHTNESS, min(250.0, start_bright * 1.7)
        )
        self.log(
            f"等待蓝色箭头到达（最多 {timeout:.0f} 秒，地图保持打开）…"
        )
        signal_at: Optional[float] = None
        last_log = 0.0
        while True:
            if self._stop:
                raise RuntimeError("已停止。")
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                self.log(f"{timeout:.0f} 秒内没有检测到到达或场景切换")
                return "timeout"
            if not self._sleep(0.15):
                raise RuntimeError("已停止。")
            cap = self._capture()
            if cap is None:
                continue
            count = self._blue_pixels_near(cap, profile, target)
            need = baseline_blue + max(6, BLUE_ARROW_MIN_PIXELS // 2)
            if count >= max(BLUE_ARROW_MIN_PIXELS, need):
                self.log(
                    f"蓝色箭头已到达标定点位"
                    f"（蓝色像素 {count}，点击前 {baseline_blue}）"
                )
                return "arrived"
            diff = frame_diff_ratio(prev, cap)
            bright = mean_brightness(cap)
            now = time.monotonic()
            if now - last_log >= 1.0:
                last_log = now
                self.log(
                    f"等待到达：蓝色像素 {count}，亮度 {bright:.0f}，"
                    f"变化 {diff:.3f}"
                )
            if (
                bright <= dark_limit
                or bright >= light_limit
                or diff >= SCENE_BIG_DIFF
                or abs(bright - prev_bright) >= 55.0
            ):
                if signal_at is None:
                    self.log(
                        f"检测到场景切换（亮度 {bright:.0f}，变化 {diff:.3f}）…"
                    )
                    signal_at = now
            if signal_at is not None and now - signal_at >= 1.0:
                self._map_open = False
                self.log("场景切换完成，地图已关闭")
                return "scene"
            prev = cap
            prev_bright = bright

    def run(self) -> None:
        try:
            steps = [s for s in self.path.get("steps", []) if isinstance(s, dict)]
            if not steps:
                raise RuntimeError("路径没有步骤。")
            self._map_open = False
            self._mount()
            current_profile: Optional[dict] = None
            for index, step in enumerate(steps, 1):
                if self._stop:
                    raise RuntimeError("已停止。")
                step_map = str(step.get("map", "")).strip()
                target_name = str(step.get("target", "")).strip()
                wait = max(0.0, float(step.get("wait", 15.0) or 0.0))
                timeout = wait if wait > 0 else 15.0
                if step_map:
                    profile = self._profile_by_name(step_map)
                    if profile is None:
                        raise RuntimeError(
                            f"找不到地图「{step_map}」的标注，请先在“地图标注寻路”里保存"
                        )
                else:
                    if current_profile is None:
                        raise RuntimeError("路径第一步必须选择地图。")
                    profile = current_profile
                profile_name = str(profile.get("name", ""))
                target = find_target(profile, target_name)
                if target is None:
                    raise RuntimeError(
                        f"地图「{profile_name}」里没有目标「{target_name}」"
                    )
                self.log(
                    f"第 {index}/{len(steps)} 步：{profile_name} → 「{target_name}」"
                    f"（超时 {timeout:.0f} 秒）"
                )
                ok = False
                for attempt in range(1, 3):
                    baseline_blue = self._click_target(profile, target)
                    result = self._wait_arrival_or_transition(
                        profile, target, timeout, baseline_blue
                    )
                    if result in ("arrived", "scene"):
                        ok = True
                        break
                    if attempt == 1:
                        self.log("没有检测到到达/切换，重试点击一次")
                if not ok:
                    raise RuntimeError(f"没有检测到到达「{target_name}」")
                current_profile = (
                    self._profile_by_name(step_map) if step_map else profile
                ) or profile
            self.log("路径执行完成（地图保持打开）")
            self.done.emit()
        except Exception as exc:
            msg = str(exc)
            if msg != "已停止。":
                self.log(f"失败：{msg}")
                self.failed.emit(msg)
            else:
                self.failed.emit("已停止。")


class PathEditorDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("路径编辑（地图 + 入口 + 等待）")
        self.resize(1050, 760)
        self.paths = load_paths()
        self.profiles = load_maps()
        self.current_index = -1
        self.steps: list[dict] = []
        self._build_ui()
        self._refresh_path_list()
        if self.paths:
            self.list_paths.setCurrentRow(0)
        else:
            self.new_path()

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        left = QVBoxLayout()
        left.addWidget(QLabel("路径列表"))
        self.list_paths = QListWidget()
        left.addWidget(self.list_paths, 1)
        lr = QHBoxLayout()
        self.btn_new = QPushButton("新建路径")
        self.btn_del_path = QPushButton("删除路径")
        lr.addWidget(self.btn_new)
        lr.addWidget(self.btn_del_path)
        left.addLayout(lr)
        root.addLayout(left, 0)

        right = QVBoxLayout()
        form = QFormLayout()
        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("例如：刷怪点 → BOSS点")
        form.addRow("路径名", self.edit_name)
        right.addLayout(form)

        add_box = QGroupBox("添加步骤")
        af = QFormLayout(add_box)
        self.combo_map = QComboBox()
        for mp in self.profiles:
            self.combo_map.addItem(str(mp.get("name", "未命名")), mp)
        self.combo_target = QComboBox()
        self.spin_wait = QDoubleSpinBox()
        self.spin_wait.setRange(0, 300)
        self.spin_wait.setValue(15)
        self.spin_wait.setSuffix(" 秒")
        self.edit_note = QLineEdit()
        self.edit_note.setPlaceholderText("说明（可选）")
        self.btn_add = QPushButton("添加这个步骤")
        af.addRow("地图", self.combo_map)
        af.addRow("目标", self.combo_target)
        af.addRow("等待", self.spin_wait)
        af.addRow("说明", self.edit_note)
        af.addRow("", self.btn_add)
        right.addWidget(add_box)

        right.addWidget(QLabel("路径步骤"))
        self.list_steps = QListWidget()
        right.addWidget(self.list_steps, 1)
        br = QHBoxLayout()
        self.btn_up = QPushButton("上移")
        self.btn_down = QPushButton("下移")
        self.btn_del_step = QPushButton("删除步骤")
        br.addWidget(self.btn_up)
        br.addWidget(self.btn_down)
        br.addWidget(self.btn_del_step)
        br.addStretch(1)
        self.btn_save = QPushButton("保存路径")
        br.addWidget(self.btn_save)
        right.addLayout(br)
        root.addLayout(right, 1)

        self.btn_new.clicked.connect(self.new_path)
        self.btn_del_path.clicked.connect(self.delete_path)
        self.combo_map.currentIndexChanged.connect(self._refresh_targets)
        self.btn_add.clicked.connect(self.add_step)
        self.btn_up.clicked.connect(lambda: self.move_step(-1))
        self.btn_down.clicked.connect(lambda: self.move_step(1))
        self.btn_del_step.clicked.connect(self.delete_step)
        self.btn_save.clicked.connect(self.save_path)
        self.list_paths.currentRowChanged.connect(self.load_path_at)
        self._refresh_targets()

    def _refresh_path_list(self) -> None:
        self.list_paths.blockSignals(True)
        self.list_paths.clear()
        for path in self.paths:
            self.list_paths.addItem(str(path.get("name", "未命名")))
        self.list_paths.blockSignals(False)

    def _refresh_targets(self, *_args) -> None:
        self.combo_target.clear()
        mp = self.combo_map.currentData()
        if isinstance(mp, dict):
            for target in mp.get("targets", []):
                if isinstance(target, dict):
                    kind = TARGET_KINDS.get(str(target.get("kind")), "")
                    self.combo_target.addItem(
                        f"{target.get('name', '')} [{kind}]", target
                    )

    def _refresh_steps(self) -> None:
        self.list_steps.clear()
        for idx, step in enumerate(self.steps, 1):
            self.list_steps.addItem(
                f"{idx}. {step.get('map', '')} → {step.get('target', '')}  "
                f"等待 {float(step.get('wait', 0)):.0f}s  {step.get('note', '')}"
            )

    def new_path(self) -> None:
        self.current_index = -1
        self.list_paths.clearSelection()
        self.edit_name.setText("新路径")
        self.steps = []
        self._refresh_steps()

    def load_path_at(self, index: int) -> None:
        if index < 0 or index >= len(self.paths):
            return
        path = self.paths[index]
        self.current_index = index
        self.edit_name.setText(str(path.get("name", "")))
        self.steps = [dict(s) for s in path.get("steps", []) if isinstance(s, dict)]
        self._refresh_steps()

    def delete_path(self) -> None:
        if self.current_index < 0 or self.current_index >= len(self.paths):
            return
        del self.paths[self.current_index]
        save_paths(self.paths)
        self.current_index = -1
        self._refresh_path_list()
        if self.paths:
            self.list_paths.setCurrentRow(0)
        else:
            self.new_path()

    def add_step(self) -> None:
        mp = self.combo_map.currentData()
        target = self.combo_target.currentData()
        if not isinstance(mp, dict) or not isinstance(target, dict):
            QMessageBox.information(
                self,
                "缺少目标",
                "先选择地图和目标。\n如果地图没有目标，先去人工标注地图。",
            )
            return
        self.steps.append(
            {
                "map": str(mp.get("name", "")),
                "target": str(target.get("name", "")),
                "wait": float(self.spin_wait.value()),
                "note": self.edit_note.text().strip(),
            }
        )
        self._refresh_steps()
        self.list_steps.setCurrentRow(len(self.steps) - 1)

    def move_step(self, delta: int) -> None:
        row = self.list_steps.currentRow()
        if row < 0:
            return
        target = row + delta
        if target < 0 or target >= len(self.steps):
            return
        self.steps[row], self.steps[target] = self.steps[target], self.steps[row]
        self._refresh_steps()
        self.list_steps.setCurrentRow(target)

    def delete_step(self) -> None:
        row = self.list_steps.currentRow()
        if 0 <= row < len(self.steps):
            del self.steps[row]
            self._refresh_steps()

    def save_path(self) -> None:
        name = self.edit_name.text().strip()
        if not name:
            QMessageBox.warning(self, "缺少路径名", "请填写路径名。")
            return
        if not self.steps:
            QMessageBox.warning(self, "没有步骤", "请至少添加一个步骤。")
            return
        path = {
            "name": name,
            "steps": [dict(s) for s in self.steps],
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        existing = next(
            (i for i, p in enumerate(self.paths) if str(p.get("name", "")) == name),
            None,
        )
        if existing is not None:
            self.paths[existing] = path
            self.current_index = existing
        else:
            self.paths.append(path)
            self.current_index = len(self.paths) - 1
        if not save_paths(self.paths):
            QMessageBox.warning(self, "保存失败", f"无法写入：{PATHS_PATH}")
            return
        self._refresh_path_list()
        self.list_paths.setCurrentRow(self.current_index)
        QMessageBox.information(
            self, "已保存", f"路径「{name}」已保存。\n{PATHS_PATH}"
        )
