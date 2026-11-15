#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多开槽位执行器：一个 SlotRunner 管理一个游戏窗口。

每个槽位独立运行：
    打怪 → 定时检测疲劳 → 疲劳 < -阈值 → 回放 BOSS 录制 → 播完恢复打怪
    + 血量保护：低血回血 / 低血停平A

不直接依赖主程序模块，运行时从 sys.modules 取主程序里的
KeySender / FatigueMonitor / HpMonitor / MacroPlayer，避免循环导入。
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
    hp_changed = Signal(object, object, str)  # current, maximum, message
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
        self.label = str(config.get("label") or "BOSS")
        try:
            self.priority = max(1, min(99, int(config.get("priority", 1))))
        except (TypeError, ValueError):
            self.priority = 1
        # 两条线，每个窗口单独判断：
        # 1) 回血红线（高）：低于它 → 排队/开始回血
        # 2) 停平A红线（低，不低于 300）：低于它 → 只暂停发 A，
        #    技能/喝药继续；高于它才恢复打怪
        try:
            self.hp_red_line = max(1, int(config.get("hp_red_line", 300)))
        except (TypeError, ValueError):
            self.hp_red_line = 300
        try:
            self.hp_heal_line = max(
                self.hp_red_line,
                int(config.get("hp_threshold", self.hp_red_line)),
            )
        except (TypeError, ValueError):
            self.hp_heal_line = self.hp_red_line
        try:
            self.hp_pause_line = max(
                self.hp_red_line,
                min(
                    self.hp_heal_line,
                    int(
                        config.get("hp_pause_line", self.hp_heal_line)
                        or self.hp_heal_line
                    ),
                ),
            )
        except (TypeError, ValueError):
            self.hp_pause_line = self.hp_red_line
        self.hp_need_heal = bool(config.get("hp_need_heal", True))
        self.hp_potion_enabled = bool(
            config.get("hp_potion_enabled", False)
        )
        try:
            self.hp_potion_threshold = max(
                1, int(config.get("hp_potion_threshold", 1600))
            )
        except (TypeError, ValueError):
            self.hp_potion_threshold = 1600
        self._attack_paused = False
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
        self._request_priority = self.priority
        self._hp_monitor = None
        self._hp_current = None
        self._hp_maximum = None
        self._heal_pending = False
        self._playing_heal = False
        self._heal_cooldown_until = 0.0

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
            self.status.emit(f"试放{self.label}录制…", "warn")
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
        self._heal_pending = False
        self._playing_heal = False
        self._attack_paused = False
        FOREGROUND_ARBITER.cancel(self._waiter_id)
        self._stop_hp_monitor()
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

    def _is_minimized(self) -> bool:
        try:
            return bool(win32gui is not None and win32gui.IsIconic(self.hwnd))
        except Exception:
            return False

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
                use_potion=bool(self.config.get("use_potion", False))
                and not self.hp_potion_enabled,
                potion_interval_ms=int(
                    self.config.get("potion_interval_ms", 60000)
                ),
                attack_enabled=True,
                use_pickup=bool(self.config.get("use_pickup", True)),
                parent=self,
            )
        except Exception as exc:
            self.stop(f"打怪线程启动失败：{exc}", "error")
            return
        self._attack_paused = False
        worker.cycle_done.connect(self._on_farm_cycle)
        worker.failed.connect(self._on_farm_failed)
        self.state = "farming"
        self._worker = worker
        worker.start()
        self._start_hp_monitor()

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
            self.status.emit("打怪中… 等待其他窗口完成前台操作…", "warn")
            return
        if self._is_minimized():
            self.status.emit(
                f"窗口已最小化（游戏可能暂停），仍发送后台按键… 已 {self._rounds} 轮",
                "normal",
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

    # ---- 血量保护 -----------------------------------------------------
    def _start_hp_monitor(self) -> None:
        if not (
            self.config.get("hp_enabled") or self.hp_potion_enabled
        ):
            return
        if self._hp_monitor is not None:
            return
        roi = self.config.get("hp_roi")
        if not roi:
            return
        app = _app()
        if app is None or not hasattr(app, "HpMonitor"):
            return
        try:
            monitor = app.HpMonitor(
                self.hwnd,
                tuple(roi),
                float(self.config.get("hp_interval", 1)),
                parent=self,
            )
        except Exception:
            return
        monitor.result.connect(self._on_hp_result)
        self._hp_monitor = monitor
        monitor.start()

    def _stop_hp_monitor(self) -> None:
        monitor = self._hp_monitor
        self._hp_monitor = None
        if monitor is not None:
            self.hp_changed.emit(None, None, "暂停检测")
            try:
                monitor.stop()
                if not monitor.wait(3000):
                    monitor.terminate()
                    monitor.wait(500)
            except Exception:
                pass
            monitor.deleteLater()

    def _on_hp_result(self, current, maximum, message: str) -> None:
        if not self.is_running:
            return
        if current is None:
            self.hp_changed.emit(None, None, message)
            return
        try:
            self._hp_current = int(current)
            self._hp_maximum = int(maximum) if maximum is not None else None
        except (TypeError, ValueError):
            return
        self.hp_changed.emit(self._hp_current, self._hp_maximum, message)
        # 只在打怪阶段检测；BOSS/回血/排队播放期间不触发。
        if self.state != "farming":
            return
        if (
            self.hp_potion_enabled
            and self._hp_current is not None
            and self._hp_current < self.hp_potion_threshold
            and self._worker is not None
        ):
            try:
                self._worker.trigger_potion()
            except Exception:
                pass
        if not self.config.get("hp_enabled"):
            return
        if self._playing_heal:
            return
        # 第二个红线：低于它只暂停发平A，技能/喝药继续。
        self._update_attack_gate(self._hp_current)
        if (
            self.hp_need_heal
            and self._hp_current < self.hp_heal_line
            and time.monotonic() >= self._heal_cooldown_until
        ):
            self._trigger_heal(
                self._hp_current, self._hp_maximum, self.hp_heal_line
            )

    def _update_attack_gate(self, current) -> None:
        """停平A红线：低于它暂停发 A，高于它才恢复打怪。"""
        if current is None:
            return
        line = self.hp_pause_line
        if current > line:
            if self._attack_paused:
                self._attack_paused = False
                self._set_worker_attack(True)
                self.status.emit(
                    f"血量 {current} 高于停平A红线 {line}，恢复打怪…",
                    "ok",
                )
        elif current < line:
            if not self._attack_paused:
                self._attack_paused = True
                self._set_worker_attack(False)
                self.status.emit(
                    f"血量 {current} 低于停平A红线 {line}，"
                    "本窗口暂停平A（其他窗口继续，技能/喝药继续）…",
                    "warn",
                )

    def _set_worker_attack(self, enabled: bool) -> None:
        worker = self._worker
        if worker is None:
            return
        try:
            worker.set_attack_enabled(enabled)
        except Exception:
            pass

    def _trigger_heal(self, current, maximum, threshold: int) -> None:
        if not self.is_running or self._playing_heal:
            return
        if self.state != "farming":
            return
        if self._heal_pending:
            return
        self._heal_pending = True
        label = "回血红线" if threshold <= self.hp_red_line else "回血阈值"
        self.status.emit(
            f"血量 {current}/{maximum} 低于{label} {threshold}，准备回血…",
            "warn",
        )
        # 疲劳检测点击期间：等它结束，_on_fatigue_result 会接着回血。
        if self._monitor is not None:
            return
        # 正在排队（例如别的窗口在播 BOSS）：
        # 先继续打怪，回血请求改成最高优先级；
        # 低于停平A红线时由 _update_attack_gate 负责停 A。
        if self._pending_action and not self._lock_held:
            self._pending_action = "heal"
            self._request_priority = 0
            FOREGROUND_ARBITER.cancel(self._waiter_id)
            self._lock_timer.start(0)
            return
        if self._lock_held:
            self._release_foreground()
        self._start_heal()

    def _start_heal(self) -> None:
        """申请前台并开始回血；拿不到就继续打怪排队。"""
        if not self.is_running or self._playing_heal:
            return
        if not self.hp_need_heal:
            self._heal_pending = False
            return
        if self._is_minimized():
            self._heal_pending = False
            self._heal_cooldown_until = time.monotonic() + 30.0
            self.status.emit(
                "窗口已最小化，跳过回血（游戏可能暂停）", "warn"
            )
            return
        if self._monitor is not None:
            self._heal_pending = True
            return
        if self.config.get("background"):
            self._begin_heal_now()
            return
        if not self._request_foreground("heal", priority=0):
            if self._pending_action == "heal":
                hp_text = (
                    f"{self._hp_current}/{self._hp_maximum}"
                    if self._hp_maximum is not None
                    else str(self._hp_current)
                )
                self.status.emit(
                    f"血量 {hp_text} 低于回血红线，已排队回血，继续打怪"
                    f"（低于停平A红线 {self.hp_pause_line} 时暂停平A）…",
                    "warn",
                )
            return
        self._begin_heal_now()

    def _begin_heal_now(self) -> None:
        """真正开始回血：先停本窗口打怪，再播回血录制。"""
        if not self.is_running or self._playing_heal:
            return
        if not self.hp_need_heal:
            self._heal_pending = False
            return
        if self._is_minimized():
            self._heal_pending = False
            self._heal_cooldown_until = time.monotonic() + 30.0
            self._release_foreground()
            self.status.emit(
                "窗口已最小化，跳过回血（游戏可能暂停）", "warn"
            )
            return
        self._timer.stop()
        self._stop_farming()
        self.state = "healing"
        self._playing_heal = True
        self._stop_hp_monitor()
        self._heal_pending = True
        self._do_play_heal()

    def _do_play_heal(self) -> None:
        events = list(self.config.get("heal_events", []))
        if not self.is_running or not events:
            self._playing_heal = False
            self._release_foreground()
            self.stop("没有可回放的回血录制", "error")
            return
        app = _app()
        if app is None:
            self._playing_heal = False
            self._release_foreground()
            self.stop("主程序模块不可用", "error")
            return
        try:
            player = app.MacroPlayer(
                self.hwnd,
                "回城回血",
                events,
                meta=self.config.get("heal_meta"),
                speed=1.0,
                background=bool(
                    self.config.get("background")
                    or self.config.get("keyboard_only")
                ),
                relative_mouse=bool(self.config.get("relative_mouse")),
                keyboard_only=bool(self.config.get("keyboard_only")),
                parent=self,
            )
        except Exception as exc:
            self._playing_heal = False
            self._release_foreground()
            self.stop(f"回血录制启动失败：{exc}", "error")
            return
        player.done.connect(self._on_heal_done)
        player.error.connect(self._on_heal_error)
        self._player = player
        self.status.emit("回血中…", "warn")
        player.start()

    def _on_heal_done(self, stage: str) -> None:
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
        self._playing_heal = False
        self._heal_pending = False
        self._heal_cooldown_until = time.monotonic() + 3.0
        if not self.is_running:
            return
        self.state = "farming"
        interval = max(5.0, float(self.config.get("interval", 30)))
        self.status.emit("回血完成，恢复打怪…", "ok")
        self._start_farming()
        self._schedule(interval)

    def _on_heal_error(self, stage: str, message: str) -> None:
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
        self._playing_heal = False
        self._heal_pending = False
        if self.is_running:
            self.stop(f"回血录制失败：{message}", "error")

    # ---- 前台操作排队 -------------------------------------------------
    def _request_foreground(self, action: str, priority=None) -> bool:
        """按优先级申请前台操作权；拿不到就定时重试，不阻塞界面。"""
        if self._lock_held:
            return True
        self._request_priority = (
            self.priority if priority is None else int(priority)
        )
        if FOREGROUND_ARBITER.request(
            self._waiter_id, self._request_priority
        ):
            self._lock_held = True
            return True
        if not self._pending_action:
            self.status.emit(
                f"等待前台操作（优先级 {self._request_priority}）…", "warn"
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
        if not FOREGROUND_ARBITER.request(
            self._waiter_id, self._request_priority
        ):
            self._lock_timer.start(300)
            return
        self._lock_held = True
        action = self._pending_action
        self._pending_action = ""
        if action == "monitor":
            self._do_check_fatigue()
        elif action == "play":
            self._do_play_boss_recording()
        elif action == "heal":
            self._begin_heal_now()
        else:
            self._release_foreground()

    def _release_foreground(self) -> None:
        if not self._lock_held:
            return
        self._lock_held = False
        FOREGROUND_ARBITER.release(self._waiter_id)

    # ---- 疲劳检测 -----------------------------------------------------
    def _check_fatigue(self) -> None:
        if not self.is_running or self.state != "farming":
            return
        if self._monitor is not None:
            return
        if self._is_minimized():
            interval = max(5.0, float(self.config.get("interval", 30)))
            self.status.emit(
                "窗口已最小化，跳过疲劳检测（游戏可能暂停）", "normal"
            )
            self._schedule(interval)
            return
        # 回血请求排队期间不要再插前台检测，避免抢走 heal 的优先级
        if self._heal_pending or self._playing_heal:
            return
        if self.config.get("background"):
            self._do_check_fatigue()
            return
        if not self._request_foreground("monitor"):
            return
        self._do_check_fatigue()

    def _do_check_fatigue(self) -> None:
        if not self.is_running or self.state != "farming":
            self._release_foreground()
            return
        if self._is_minimized():
            self._release_foreground()
            interval = max(5.0, float(self.config.get("interval", 30)))
            self.status.emit(
                "窗口已最小化，跳过疲劳检测（游戏可能暂停）", "normal"
            )
            self._schedule(interval)
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
                background=bool(self.config.get("background")),
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
        if self._heal_pending:
            self._start_heal()
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

    # ---- BOSS 录制回放 -------------------------------------------------
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
        if not self.test_only and self._is_minimized():
            self._release_foreground()
            interval = max(5.0, float(self.config.get("interval", 30)))
            self.status.emit(
                "窗口已最小化，跳过BOSS回放（游戏可能暂停）", "warn"
            )
            self._start_farming()
            self._schedule(interval)
            return
        self._stop_farming()
        self.state = "boss"
        self._stop_hp_monitor()
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
                f"多开{self.label}",
                events,
                meta=meta if isinstance(meta, dict) else None,
                speed=float(self.config.get("speed", 1.0) or 1.0),
                background=bool(
                    self.config.get("background")
                    or self.config.get("keyboard_only")
                ),
                relative_mouse=bool(self.config.get("relative_mouse")),
                keyboard_only=bool(self.config.get("keyboard_only")),
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
            self.status.emit(f"试放{self.label}录制…", "warn")
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
