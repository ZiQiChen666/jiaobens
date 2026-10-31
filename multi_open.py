#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多开槽位执行器：一个 SlotRunner 管理一个游戏窗口。

每个槽位独立运行：
    打怪 → 定时检测疲劳 → 疲劳 < -阈值 → 回放 BOSS 录制 → 播完恢复打怪

不直接依赖主程序模块，运行时从 sys.modules 取主程序里的
KeySender / FatigueMonitor / MacroPlayer，避免循环导入。
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

try:
    import win32gui
except Exception:  # pragma: no cover
    win32gui = None  # type: ignore[assignment]


def _app():
    return sys.modules.get("creature_curios_keyer") or sys.modules.get("__main__")


# 所有多开槽位共用一把“前台操作锁”：
# 录制回放、疲劳检测点击都需要独占前台鼠标键盘，必须一个一个来。
FOREGROUND_LOCK = threading.Lock()


class SlotRunner(QObject):
    """一个窗口的“挂机 ↔ 录制”循环。"""

    status = Signal(str, str)   # text, level
    finished = Signal()

    def __init__(self, hwnd: int, config: dict, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.hwnd = int(hwnd)
        self.config = dict(config)
        self.is_running = False
        self.state = "idle"
        self._worker = None
        self._monitor = None
        self._player = None
        self._rounds = 0
        self._last_fatigue = ""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._check_fatigue)
        self._lock_timer = QTimer(self)
        self._lock_timer.setSingleShot(True)
        self._lock_timer.timeout.connect(self._retry_foreground_action)
        self._lock_held = False
        self._pending_action = ""
        self._pending_play = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self.is_running:
            return
        app = _app()
        if app is None:
            self.status.emit("主程序模块不可用", "error")
            return
        if win32gui is None or not win32gui.IsWindow(self.hwnd):
            self.status.emit("游戏窗口已关闭", "error")
            return
        if not self.config.get("events"):
            self.status.emit("没有可回放的 BOSS 录制", "error")
            return
        self.is_running = True
        self.state = "farming"
        self._rounds = 0
        self._last_fatigue = ""
        self.status.emit("开始挂机，稍后检测疲劳…", "ok")
        self._start_farming()
        self._schedule(2.0)

    def stop(self, message: str = "已停止", level: str = "warn") -> None:
        had_work = (
            self.is_running
            or self._worker is not None
            or self._monitor is not None
            or self._player is not None
        )
        self.is_running = False
        self.state = "idle"
        self._timer.stop()
        self._lock_timer.stop()
        self._pending_action = ""
        self._pending_play = None
        self._stop_monitor()
        self._stop_farming()
        self._stop_player()
        self._release_foreground()
        if had_work:
            self.status.emit(message, level)
        self.finished.emit()

    # ------------------------------------------------------------------
    def _schedule(self, delay: float) -> None:
        if not self.is_running:
            return
        self._timer.start(max(0, int(delay * 1000)))

    def _start_farming(self) -> None:
        if not self.is_running or self._worker is not None:
            return
        app = _app()
        if app is None:
            self.stop("主程序模块不可用", "error")
            return
        if win32gui is None or not win32gui.IsWindow(self.hwnd):
            self.stop("游戏窗口已关闭", "error")
            return
        try:
            worker = app.KeySender(
                self.hwnd,
                int(self.config.get("interval_ms", 250)),
                use_skills=bool(self.config.get("use_skills", False)),
                use_potion=bool(self.config.get("use_potion", False)),
                potion_interval_ms=int(
                    self.config.get("potion_interval_ms", 60000)
                ),
                use_pickup=bool(self.config.get("use_pickup", True)),
                parent=self,
            )
        except Exception as exc:
            self.stop(f"打怪线程启动失败：{exc}", "error")
            return
        worker.cycle_done.connect(self._on_farm_cycle)
        worker.failed.connect(self._on_farm_failed)
        self.state = "farming"
        self._worker = worker
        worker.start()

    def _stop_farming(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            try:
                worker.stop()
                if not worker.wait(2500):
                    worker.terminate()
                    worker.wait(500)
            except Exception:
                pass
            worker.deleteLater()

    def _on_farm_cycle(self, rounds: int) -> None:
        if not self.is_running or self.state != "farming":
            return
        self._rounds = int(rounds)
        if self._pending_action:
            self.status.emit(
                "打怪中… 等待其他窗口完成前台操作…", "warn"
            )
            return
        suffix = f" / {self._last_fatigue}" if self._last_fatigue else ""
        self.status.emit(f"打怪中… 已 {self._rounds} 轮{suffix}", "normal")

    def _on_farm_failed(self, message: str) -> None:
        if not self.is_running:
            return
        if self.state != "farming":
            return
        self.stop(f"打怪线程停止：{message}", "error")

    # ------------------------------------------------------------------
    def _request_foreground(self, action: str) -> bool:
        """申请前台操作锁；拿不到就定时重试，不阻塞界面。"""
        if self._lock_held:
            return True
        if FOREGROUND_LOCK.acquire(blocking=False):
            self._lock_held = True
            return True
        if not self._pending_action:
            self.status.emit("等待其他窗口完成前台操作…", "warn")
        self._pending_action = action
        self._lock_timer.start(500)
        return False

    def _retry_foreground_action(self) -> None:
        if not self.is_running or not self._pending_action:
            self._pending_action = ""
            self._release_foreground()
            return
        if not FOREGROUND_LOCK.acquire(blocking=False):
            self._lock_timer.start(500)
            return
        self._lock_held = True
        action = self._pending_action
        self._pending_action = ""
        if action == "monitor":
            self._do_check_fatigue()
        elif action == "play":
            self._do_play_boss_recording()
        else:
            self._release_foreground()

    def _release_foreground(self) -> None:
        if not self._lock_held:
            return
        self._lock_held = False
        try:
            FOREGROUND_LOCK.release()
        except RuntimeError:
            pass

    def _check_fatigue(self) -> None:
        if not self.is_running or self.state != "farming":
            return
        if self._monitor is not None:
            return
        if not self._request_foreground("monitor"):
            return
        self._do_check_fatigue()

    def _do_check_fatigue(self) -> None:
        if not self.is_running or self.state != "farming":
            self._release_foreground()
            return
        app = _app()
        if app is None:
            self._release_foreground()
            self.stop("主程序模块不可用", "error")
            return
        roi_norm = self.config.get("roi_norm")
        if roi_norm is None:
            self._release_foreground()
            self.stop("尚未框选疲劳提示区域", "error")
            return
        try:
            monitor = app.FatigueMonitor(
                self.hwnd,
                roi_norm,
                one_shot=True,
                parent=self,
            )
        except Exception as exc:
            self._release_foreground()
            self.stop(f"疲劳检测启动失败：{exc}", "error")
            return
        monitor.result.connect(self._on_fatigue_result)
        self._monitor = monitor
        self.status.emit("正在检测疲劳…", "warn")
        monitor.start()

    def _stop_monitor(self) -> None:
        monitor = self._monitor
        self._monitor = None
        if monitor is not None:
            try:
                monitor.stop()
                if not monitor.wait(3000):
                    monitor.terminate()
                    monitor.wait(500)
            except Exception:
                pass
            monitor.deleteLater()
        self._release_foreground()

    def _on_fatigue_result(self, current, maximum, message: str) -> None:
        monitor = self.sender()
        if monitor is not None:
            try:
                monitor.wait(2000)
            except Exception:
                pass
            try:
                monitor.deleteLater()
            except Exception:
                pass
        self._release_foreground()
        if monitor is not self._monitor:
            return
        self._monitor = None
        if not self.is_running or self.state != "farming":
            return

        threshold = int(self.config.get("threshold", 800))
        interval = max(5.0, float(self.config.get("interval", 30)))
        if current is None or maximum is None:
            self.status.emit(f"疲劳检测失败：{message}（继续打怪）", "warn")
            self._start_farming()
            self._schedule(interval)
            return

        current = int(current)
        maximum = int(maximum)
        self._last_fatigue = f"上次疲劳 {current}/{maximum}"
        self.status.emit(
            f"当前疲劳 {current}/{maximum}（阈值 ±{threshold}）", "ok"
        )
        if current < -threshold:
            self._play_boss_recording(current, maximum, threshold)
        else:
            self._start_farming()
            self._schedule(interval)

    # ------------------------------------------------------------------
    def _play_boss_recording(
        self, current: int, maximum: int, threshold: int
    ) -> None:
        if not self.is_running:
            return
        self._timer.stop()
        self._pending_play = (current, maximum, threshold)
        if not self._request_foreground("play"):
            return
        self._do_play_boss_recording()

    def _do_play_boss_recording(self) -> None:
        if not self.is_running or self._pending_play is None:
            self._release_foreground()
            return
        current, maximum, threshold = self._pending_play
        self._pending_play = None
        self._stop_farming()
        self.state = "boss"
        app = _app()
        if app is None:
            self._release_foreground()
            self.stop("主程序模块不可用", "error")
            return
        events = list(self.config.get("events", []))
        meta = self.config.get("meta")
        try:
            player = app.MacroPlayer(
                self.hwnd,
                "多开BOSS",
                events,
                meta=meta if isinstance(meta, dict) else None,
                speed=float(self.config.get("speed", 1.0) or 1.0),
                pause_on_user_activity=bool(
                    self.config.get("pause_on_user_activity", True)
                ),
                parent=self,
            )
        except Exception as exc:
            self._release_foreground()
            self.stop(f"录制回放启动失败：{exc}", "error")
            return
        player.done.connect(self._on_player_done)
        player.error.connect(self._on_player_error)
        player.paused.connect(self._on_player_paused)
        self._player = player
        self.status.emit(
            f"疲劳 {current}/{maximum} < -{threshold}，回放 BOSS 录制…",
            "warn",
        )
        player.start()

    def _stop_player(self) -> None:
        player = self._player
        self._player = None
        if player is not None:
            try:
                player.stop()
                if not player.wait(3000):
                    player.terminate()
                    player.wait(500)
            except Exception:
                pass
            player.deleteLater()
        self._release_foreground()

    def _on_player_paused(self, text: str) -> None:
        self.status.emit(text, "warn")

    def _on_player_done(self, stage: str) -> None:
        player = self.sender()
        if player is not None:
            try:
                player.wait(500)
            except Exception:
                pass
            try:
                player.deleteLater()
            except Exception:
                pass
        self._release_foreground()
        if player is not self._player:
            return
        self._player = None
        if not self.is_running:
            return
        self.state = "farming"
        interval = max(5.0, float(self.config.get("interval", 30)))
        self.status.emit("BOSS 录制完成，恢复打怪…", "ok")
        self._start_farming()
        self._schedule(interval)

    def _on_player_error(self, stage: str, message: str) -> None:
        player = self.sender()
        if player is not None:
            try:
                player.wait(500)
            except Exception:
                pass
            try:
                player.deleteLater()
            except Exception:
                pass
        self._release_foreground()
        if player is not self._player:
            return
        self._player = None
        if not self.is_running:
            return
        self.stop(f"BOSS 录制回放失败：{message}", "error")
