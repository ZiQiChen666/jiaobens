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


# 所有多开槽位共用一个“前台操作排队器”：
# 录制回放、疲劳检测点击都需要独占前台鼠标键盘，必须一个一个来；
# 多个窗口同时请求时，按每个窗口设置的优先级决定谁先执行。
class ForegroundArbiter:
    def __init__(self, coalesce_seconds: float = 0.8) -> None:
        self._lock = threading.Lock()
        self._holder = None
        self._waiters: dict[int, tuple[int, int]] = {}
        self._seq = 0
        self._first_wait = None
        self.coalesce = max(0.0, float(coalesce_seconds))

    def request(self, waiter_id: int, priority: int) -> bool:
        with self._lock:
            now = time.monotonic()
            if waiter_id not in self._waiters:
                self._seq += 1
                self._waiters[waiter_id] = (int(priority), self._seq)
            if self._holder is not None:
                return False
            if not self._waiters:
                return False
            if self._first_wait is None:
                self._first_wait = now
            # 先等一个很短的合并窗口，让同时到的其他窗口也能登记优先级。
            if now - self._first_wait < self.coalesce:
                return False
            top = min(
                self._waiters.items(),
                key=lambda item: (item[1][0], item[1][1]),
            )[0]
            if top != waiter_id:
                return False
            self._holder = waiter_id
            del self._waiters[waiter_id]
            self._first_wait = None
            return True

    def release(self, waiter_id: int) -> None:
        with self._lock:
            if self._holder == waiter_id:
                self._holder = None
                self._first_wait = None

    def cancel(self, waiter_id: int) -> None:
        with self._lock:
            self._waiters.pop(waiter_id, None)
            if self._holder == waiter_id:
                self._holder = None
                self._first_wait = None
            if not self._waiters:
                self._first_wait = None


FOREGROUND_ARBITER = ForegroundArbiter(coalesce_seconds=0.8)


class SlotRunner(QObject):
    """一个窗口的“挂机 ↔ 录制”循环。"""

    status = Signal(str, str)   # text, level
    finished = Signal()

    def __init__(
        self,
        hwnd: int,
        config: dict,
        test_only: bool = False,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.hwnd = int(hwnd)
        self.config = dict(config)
        self.test_only = bool(test_only)
        try:
            self.priority = max(1, min(99, int(config.get("priority", 1))))
        except (TypeError, ValueError):
            self.priority = 1
        self._waiter_id = id(self)
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
        farm_only = bool(self.config.get("farm_only", False))
        if not farm_only and not self.config.get("events"):
            self.status.emit("没有可回放的 BOSS 录制", "error")
            return
        self.is_running = True
        if farm_only:
            self.state = "farming"
            self.status.emit("本地挂机中…", "ok")
            self._start_farming()
            return
        if self.test_only:
            self.state = "boss"
            self._pending_play = (0, 0, 0)
            self.status.emit("试放 BOSS 录制…", "warn")
            if not self._request_foreground("play"):
                return
            self._do_play_boss_recording()
            return
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
        FOREGROUND_ARBITER.cancel(self._waiter_id)
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
        if self.config.get("farm_only"):
            self.status.emit(f"本地挂机中… 已 {self._rounds} 轮", "normal")
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
        """按优先级申请前台操作权；拿不到就定时重试，不阻塞界面。"""
        if self._lock_held:
            return True
        if FOREGROUND_ARBITER.request(self._waiter_id, self.priority):
            self._lock_held = True
            return True
        if not self._pending_action:
            self.status.emit(
                f"等待前台操作（优先级 {self.priority}）…", "warn"
            )
        self._pending_action = action
        self._lock_timer.start(300)
        return False

    def _retry_foreground_action(self) -> None:
        if not self.is_running or not self._pending_action:
            self._pending_action = ""
            FOREGROUND_ARBITER.cancel(self._waiter_id)
            self._release_foreground()
            return
        if not FOREGROUND_ARBITER.request(self._waiter_id, self.priority):
            self._lock_timer.start(300)
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
        FOREGROUND_ARBITER.release(self._waiter_id)

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
        if self.config.get("background"):
            self._do_play_boss_recording()
            return
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
                lock_user_input=bool(
                    self.config.get("lock_user_input", True)
                ),
                background=bool(self.config.get("background", False)),
                parent=self,
            )
        except Exception as exc:
            self._release_foreground()
            self.stop(f"录制回放启动失败：{exc}", "error")
            return
        player.done.connect(self._on_player_done)
        player.error.connect(self._on_player_error)
        self._player = player
        if self.test_only:
            self.status.emit("试放 BOSS 录制…", "warn")
        else:
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
        if self.test_only:
            self.stop("试放完成", "ok")
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
