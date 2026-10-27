#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""稳定 BOSS 流程（不录制鼠标轨迹）.

核心思路：
    * 地图点位只点击一次，游戏自动寻路；
    * 副本入口/退出弹窗用 OCR 关键词找按钮，找不到时用用户预设的按钮点；
    * BOSS 战复用后台按键循环（A / 空格 / 技能 / 强制喝药），不录鼠标走位；
    * 全部坐标使用客户区归一化坐标，窗口大小/位置变化不影响。

这个模块由主程序懒加载，避免和主程序循环导入。
"""

from __future__ import annotations

import datetime
import os
import re
import sys
import threading
import time
from typing import Optional

import numpy as np
from PIL import Image
from PySide6.QtCore import QThread, Signal

try:
    import win32gui
except Exception:  # pragma: no cover
    win32gui = None  # type: ignore[assignment]

try:
    from rapidocr_onnxruntime import RapidOCR
except Exception:  # pragma: no cover
    RapidOCR = None  # type: ignore[assignment]


APP_FOLDER = os.path.join(
    os.getenv("APPDATA") or os.path.expanduser("~"),
    "CreatureCuriosKeyer",
)


def _ensure_folder() -> None:
    try:
        os.makedirs(APP_FOLDER, exist_ok=True)
    except Exception:
        pass


_ensure_folder()
STABLE_LOG_PATH = os.path.join(APP_FOLDER, "stable_boss.log")


class StableBossError(RuntimeError):
    pass


def _app():
    """取主程序模块，兼容直接运行 .py（__main__）和打包后运行。"""
    return sys.modules.get("creature_curios_keyer") or sys.modules.get("__main__")


class StableBossRunner(QThread):
    """跑一次稳定 BOSS 流程，或只跑一次地图寻路。"""

    status = Signal(str, str)   # text, level
    done = Signal(str)          # mode: "boss" / "travel"
    failed = Signal(str)

    _OCR_LOCK = threading.Lock()
    _OCR_ENGINE = None

    def __init__(
        self,
        hwnd: int,
        cfg: dict,
        mode: str = "boss",
        point_key: str = "boss",
        parent=None,
    ):
        super().__init__(parent)
        self.hwnd = int(hwnd)
        self.cfg = dict(cfg)
        self.mode = mode
        self.point_key = point_key
        self._stop_event = threading.Event()
        self._last_image: Optional[Image.Image] = None

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop_event.set()

    def _stopping(self) -> bool:
        return self._stop_event.is_set()

    def _sleep(self, seconds: float) -> bool:
        """可被 stop() 打断的等待。返回 False 表示已请求停止。"""
        if seconds <= 0:
            return not self._stopping()
        return not self._stop_event.wait(seconds)

    # ------------------------------------------------------------------
    def log(self, text: str) -> None:
        line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {text}"
        try:
            with open(STABLE_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        print(line)
        try:
            self.status.emit(text, "warn")
        except Exception:
            pass

    def _save_failure_shot(self, label: str) -> str:
        if self._last_image is None:
            return ""
        name = (
            f"stable_fail_{label}_"
            f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        )
        path = os.path.join(APP_FOLDER, name)
        try:
            self._last_image.save(path)
            return path
        except Exception:
            return ""

    # ------------------------------------------------------------------
    @classmethod
    def _ocr_engine(cls):
        if RapidOCR is None:
            raise StableBossError("缺少 rapidocr_onnxruntime，无法识别弹窗文字。")
        with cls._OCR_LOCK:
            if cls._OCR_ENGINE is None:
                cls._OCR_ENGINE = RapidOCR()
            return cls._OCR_ENGINE

    def _ocr_image(self, image: Image.Image) -> list[dict]:
        engine = self._ocr_engine()
        arr = np.array(image)[:, :, ::-1]
        result, _elapse = engine(arr)
        items: list[dict] = []
        for item in (result or []):
            if len(item) < 3:
                continue
            box, text, score = item[0], item[1], item[2]
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            x0, x1 = float(min(xs)), float(max(xs))
            y0, y1 = float(min(ys)), float(max(ys))
            items.append(
                {
                    "text": str(text),
                    "score": float(score),
                    "x0": x0,
                    "y0": y0,
                    "x1": x1,
                    "y1": y1,
                    "cx": (x0 + x1) / 2.0,
                    "cy": (y0 + y1) / 2.0,
                }
            )
        return items

    def _capture(self) -> Optional[Image.Image]:
        app = _app()
        if app is None or not hasattr(app, "capture_client_bgra"):
            return None
        cap, err = app.capture_client_bgra(self.hwnd)
        if cap is None:
            return None
        w, h, data = cap
        image = Image.frombytes("RGBA", (w, h), data, "raw", "BGRA").convert("RGB")
        self._last_image = image
        return image

    # ------------------------------------------------------------------
    def _focus(self) -> None:
        app = _app()
        if app is not None:
            app._force_foreground_window(self.hwnd)
        time.sleep(0.10)

    def _tap_key(self, name: str) -> None:
        app = _app()
        if app is None:
            raise StableBossError("主程序模块不可用。")
        self._focus()
        app._send_key_name(name, True)
        time.sleep(0.05)
        app._send_key_name(name, False)

    def _click_norm(self, nx: float, ny: float) -> None:
        app = _app()
        if app is None:
            raise StableBossError("主程序模块不可用。")
        if win32gui is None:
            raise StableBossError("win32gui 不可用。")
        left, top, right, bottom = win32gui.GetClientRect(self.hwnd)
        cw, ch = int(right - left), int(bottom - top)
        if cw <= 0 or ch <= 0:
            raise StableBossError("游戏客户区尺寸无效。")
        cx = max(1, min(cw - 2, int(round(float(nx) * cw))))
        cy = max(1, min(ch - 2, int(round(float(ny) * ch))))
        sx, sy = win32gui.ClientToScreen(self.hwnd, (cx, cy))
        self._focus()
        app._send_mouse_move_to(int(sx), int(sy))
        time.sleep(0.06)
        app._send_mouse_button("left", True)
        time.sleep(0.07)
        app._send_mouse_button("left", False)
        time.sleep(0.12)

    # ------------------------------------------------------------------
    def _travel_to(self, point_key: str) -> None:
        points = self.cfg.get("points", {})
        point = points.get(point_key)
        if not point:
            raise StableBossError(f"没有设置点位：{point_key}")
        map_key = str(self.cfg.get("map_key", "m"))
        close_key = str(self.cfg.get("close_key", "esc"))
        self.log(f"打开地图，前往点位 {point_key} …")
        self._tap_key(map_key)
        if not self._sleep(1.2):
            raise StableBossError("已停止。")
        self._click_norm(float(point[0]), float(point[1]))
        if not self._sleep(0.8):
            raise StableBossError("已停止。")
        self._tap_key(close_key)
        self.log("地图已关闭，等待游戏自动寻路。")

    def _choose_match(self, items: list[dict], keywords: list[str]) -> Optional[dict]:
        candidates: list[dict] = []
        for item in items:
            text = item["text"]
            if not any(k and k in text for k in keywords):
                continue
            if item["score"] < 0.30:
                continue
            candidates.append(item)
        if not candidates:
            return None
        # 优先选屏幕中部、识别分数高的文字，避免把聊天栏里的“进入”当成按钮
        w = float(self._last_image.width) if self._last_image else 1.0
        h = float(self._last_image.height) if self._last_image else 1.0
        for item in candidates:
            item["_center_score"] = (
                item["score"]
                - 0.30 * abs(item["cx"] / w - 0.5)
                - 0.20 * abs(item["cy"] / h - 0.5)
            )
        return max(candidates, key=lambda item: item["_center_score"])

    def _wait_and_click_popup(
        self,
        label: str,
        keywords: list[str],
        timeout: float,
        fallback: Optional[tuple[float, float]],
        fallback_after: float = 5.0,
    ) -> None:
        deadline = time.monotonic() + max(1.0, float(timeout))
        fallback_clicked = False
        last_log = 0.0
        started = time.monotonic()
        self.log(f"等待{label}弹窗（关键词：{', '.join(keywords)}）…")
        while time.monotonic() < deadline:
            if self._stopping():
                raise StableBossError("已停止。")
            image = self._capture()
            if image is not None:
                try:
                    items = self._ocr_image(image)
                except Exception as exc:
                    items = []
                    self.log(f"{label} OCR 失败：{type(exc).__name__}: {exc}")
                match = self._choose_match(items, keywords)
                if match is not None:
                    w = max(1, image.width)
                    h = max(1, image.height)
                    nx = match["cx"] / w
                    ny = match["cy"] / h
                    self.log(
                        f"{label}识别到「{match['text']}」，点击 "
                        f"({nx * 100:.1f}%, {ny * 100:.1f}%)"
                    )
                    self._click_norm(nx, ny)
                    if not self._sleep(0.8):
                        raise StableBossError("已停止。")
                    return
                if time.monotonic() - last_log >= 3.0:
                    last_log = time.monotonic()
                    joined = " / ".join(item["text"] for item in items[:20])
                    self.log(f"{label}暂未识别到按钮；当前文字：{joined[:220]}")

            if (
                fallback is not None
                and not fallback_clicked
                and time.monotonic() - started >= fallback_after
            ):
                fallback_clicked = True
                self.log(
                    f"{label} OCR 未找到按钮，尝试预设按钮点 "
                    f"({fallback[0] * 100:.1f}%, {fallback[1] * 100:.1f}%)"
                )
                self._click_norm(float(fallback[0]), float(fallback[1]))
                if not self._sleep(0.8):
                    raise StableBossError("已停止。")
                # 预设点是用户框选的实际按钮，点击后直接视为本步完成；
                # 如果点歪了，后续战斗/退出会在日志里暴露出来。
                return
            if not self._sleep(0.7):
                raise StableBossError("已停止。")

        path = self._save_failure_shot(label)
        hint = "；已保存截图：" + path if path else ""
        raise StableBossError(f"等待{label}弹窗超时{hint}")

    # ------------------------------------------------------------------
    def _fight(self) -> None:
        app = _app()
        if app is None or not hasattr(app, "KeySender"):
            raise StableBossError("主程序模块不可用。")
        KeySender = app.KeySender
        seconds = max(1.0, float(self.cfg.get("fight_seconds", 6.0)))
        self.log(f"BOSS 战斗中（{seconds:.0f} 秒，强制喝药）…")
        failures: list[str] = []
        worker = KeySender(
            self.hwnd,
            int(self.cfg.get("attack_interval_ms", 250)),
            use_skills=bool(self.cfg.get("use_skills", True)),
            use_potion=True,
            potion_interval_ms=int(self.cfg.get("potion_interval_ms", 60000)),
            attack_enabled=True,
            potion_immediate=True,
            use_pickup=bool(self.cfg.get("use_pickup", True)),
        )
        worker.failed.connect(failures.append)
        worker.start()
        try:
            if not self._sleep(seconds):
                raise StableBossError("已停止。")
            if failures:
                raise StableBossError(f"战斗线程停止：{failures[0]}")
        finally:
            worker.stop()
            if not worker.wait(2500):
                worker.terminate()
                worker.wait(500)
        self.log("BOSS 战斗循环结束。")

    # ------------------------------------------------------------------
    def run(self) -> None:
        try:
            if self.mode == "travel":
                self._travel_to(self.point_key)
                wait_s = float(self.cfg.get("travel_wait", 15.0))
                self.log(f"等待自动寻路 {wait_s:.0f} 秒 …")
                if not self._sleep(wait_s):
                    raise StableBossError("已停止。")
                self.done.emit("travel")
                return

            points = self.cfg.get("points", {})
            enter_kw = list(self.cfg.get("enter_keywords", ["进入", "挑战", "确认"]))
            exit_kw = list(self.cfg.get("exit_keywords", ["离开", "退出", "确认"]))
            enter_click = points.get("enter_click")
            exit_click = points.get("exit_click")

            self._travel_to("boss")
            path_wait = float(self.cfg.get("path_wait", 30.0))
            self._wait_and_click_popup(
                "副本入口",
                enter_kw,
                path_wait,
                tuple(enter_click) if enter_click else None,
                fallback_after=max(5.0, path_wait - 2.0),
            )
            if not self._sleep(float(self.cfg.get("load_wait", 4.0))):
                raise StableBossError("已停止。")

            self._fight()

            if not self._sleep(float(self.cfg.get("post_boss_wait", 1.0))):
                raise StableBossError("已停止。")
            popup_wait = float(self.cfg.get("popup_wait", 12.0))
            self._wait_and_click_popup(
                "副本退出",
                exit_kw,
                popup_wait,
                tuple(exit_click) if exit_click else None,
                fallback_after=max(4.0, popup_wait - 1.0),
            )
            if not self._sleep(float(self.cfg.get("load_wait", 4.0))):
                raise StableBossError("已停止。")
            self.done.emit("boss")
        except StableBossError as exc:
            msg = str(exc)
            if msg != "已停止。":
                self.log(f"失败：{msg}")
                self.failed.emit(msg)
            else:
                self.failed.emit("已停止。")
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            self.log(f"失败：{msg}")
            path = self._save_failure_shot("exception")
            if path:
                self.log(f"失败截图：{path}")
            self.failed.emit(msg)
