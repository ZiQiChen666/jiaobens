#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""录制/回放回环测试。

用法：
    python loopback_test.py

它会启动一个独立的 Win32 测试窗口，录制一段已知键鼠序列，
然后用「前台绝对 / 前台相对 / 后台」三种方式回放，比较测试窗口
实际收到的消息，并把详细日志写到 %APPDATA%\CreatureCuriosKeyer。

注意：测试会短暂移动真实鼠标/键盘，运行前请保存工作。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import subprocess
import sys
import time
from collections import Counter

import win32api
import win32con
import win32gui
import mouse

import creature_curios_keyer as m
from raw_input_recorder import RawMouseRecorder

TARGET_TITLE = "CCLoopbackTarget"

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_PAINT = 0x000F
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM
)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.UpdateWindow.argtypes = [wt.HWND]
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT
]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.ValidateRect.argtypes = [wt.HWND, wt.LPVOID]
user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.DestroyWindow.argtypes = [wt.HWND]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


def _signed16(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def run_target(log_path: str) -> int:
    log_file = open(log_path, "w", encoding="utf-8")

    def write_raw(dx: int, dy: int) -> None:
        log_file.write(
            json.dumps(
                {"t": time.time(), "raw": True, "dx": int(dx), "dy": int(dy)},
                ensure_ascii=False,
            )
            + chr(10)
        )
        log_file.flush()

    def write_event(msg: int, wparam: int, lparam: int, x=None, y=None):
        line = {
            "t": time.time(),
            "msg": int(msg),
            "wp": int(wparam),
            "lp": int(lparam),
            "x": x,
            "y": y,
            "vk": int(wparam) if msg in (WM_KEYDOWN, WM_KEYUP) else None,
        }
        log_file.write(json.dumps(line, ensure_ascii=False) + "\n")
        log_file.flush()

    def wnd_proc(hwnd, msg, wparam, lparam):
        try:
            if msg in (
                WM_MOUSEMOVE,
                WM_LBUTTONDOWN,
                WM_LBUTTONUP,
                WM_RBUTTONDOWN,
                WM_RBUTTONUP,
                WM_MBUTTONDOWN,
                WM_MBUTTONUP,
            ):
                write_event(
                    msg,
                    wparam,
                    lparam,
                    _signed16(int(lparam)),
                    _signed16(int(lparam) >> 16),
                )
                return 0
            if msg == WM_MOUSEWHEEL:
                pt = wt.POINT(_signed16(int(lparam)), _signed16(int(lparam) >> 16))
                user32.ScreenToClient(hwnd, ctypes.byref(pt))
                write_event(
                    msg, wparam, lparam, int(pt.x), int(pt.y)
                )
                return 0
            if msg in (WM_KEYDOWN, WM_KEYUP):
                write_event(msg, wparam, lparam)
                return 0
            if msg == WM_PAINT:
                user32.ValidateRect(hwnd, None)
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(wnd_proc)
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.style = 0
    wc.lpfnWndProc = proc
    wc.cbClsExtra = 0
    wc.cbWndExtra = 0
    wc.hInstance = hinst
    wc.hIcon = None
    wc.hCursor = None
    wc.hbrBackground = wt.HBRUSH(6)  # COLOR_WINDOW + 1
    wc.lpszMenuName = None
    wc.lpszClassName = TARGET_TITLE
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(
        0,
        TARGET_TITLE,
        TARGET_TITLE,
        win32con.WS_OVERLAPPEDWINDOW | win32con.WS_VISIBLE,
        100,
        100,
        800,
        600,
        None,
        None,
        hinst,
        None,
    )
    if not hwnd:
        return 2
    user32.ShowWindow(hwnd, win32con.SW_SHOW)
    user32.UpdateWindow(hwnd)
    # 关闭输入法上下文，避免 WM_KEYDOWN 被转成 VK_PROCESSKEY(229)
    try:
        imm32 = ctypes.WinDLL("imm32")
        imm32.ImmAssociateContext.argtypes = [wt.HWND, wt.HANDLE]
        imm32.ImmAssociateContext.restype = wt.HANDLE
        imm32.ImmAssociateContext(hwnd, 0)
    except Exception:
        pass
    raw_recorder = RawMouseRecorder(write_raw)
    raw_recorder.start()
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    raw_recorder.stop()
    log_file.close()
    return 0


def _clear_log(path: str) -> None:
    with open(path, "w", encoding="utf-8"):
        pass
    time.sleep(0.05)


def _read_log(path: str) -> list[dict]:
    time.sleep(0.15)
    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return entries


def _target_summary(entries: list[dict]) -> Counter:
    return Counter(int(e.get("msg", 0)) for e in entries)


def _recorded_summary(events: list[dict]) -> Counter:
    c: Counter = Counter()
    for e in events:
        typ = e.get("type")
        if typ == "move":
            c[WM_MOUSEMOVE] += 1
        elif typ == "key_down":
            c[WM_KEYDOWN] += 1
        elif typ == "key_up":
            c[WM_KEYUP] += 1
        elif typ == "down":
            c[WM_LBUTTONDOWN if e.get("button", "left") == "left" else WM_RBUTTONDOWN] += 1
        elif typ == "up":
            c[WM_LBUTTONUP if e.get("button", "left") == "left" else WM_RBUTTONUP] += 1
    return c


def _nearest_error(
    points_a: list[tuple[float, float]],
    points_b: list[tuple[float, float]],
) -> tuple[float, float]:
    if not points_a or not points_b:
        return (float("nan"), float("nan"))
    errors = []
    for ax, ay in points_a[:800]:
        best = min(
            math.hypot(ax - bx, ay - by) for bx, by in points_b[:800]
        )
        errors.append(best)
    return (sum(errors) / len(errors), max(errors))


def _replay(
    hwnd: int,
    events: list[dict],
    meta: dict,
    label: str,
    background: bool,
    relative: bool,
) -> None:
    player = m.MacroPlayer(
        hwnd,
        label,
        events,
        meta=meta,
        background=background,
        relative_mouse=relative,
    )
    player.start()
    player.wait(300000)


def main() -> int:
    log_path = os.path.join(
        os.getcwd(), "loopback_target_events.jsonl"
    )
    child = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--target", log_path]
    )
    try:
        hwnd = 0
        for _ in range(60):
            time.sleep(0.2)
            hwnd = win32gui.FindWindow(None, TARGET_TITLE)
            if hwnd:
                break
        if not hwnd:
            print("目标窗口没有启动")
            return 2

        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_TOP,
            100,
            100,
            800,
            600,
            win32con.SWP_SHOWWINDOW,
        )
        time.sleep(0.3)
        m._force_foreground_window(hwnd)
        time.sleep(0.4)
        cl, ct = win32gui.ClientToScreen(hwnd, (0, 0))
        _l, _t, cw, ch = win32gui.GetClientRect(hwnd)
        cw, ch = int(cw), int(ch)
        print("target hwnd", hex(hwnd), "client", cw, ch)

        def move_to(cx: int, cy: int, wait: float = 0.08) -> None:
            # 用相对 mouse_event 生成低层鼠标移动事件，记录器才能录到
            tx, ty = cl + int(cx), ct + int(cy)
            sx, sy = win32api.GetCursorPos()
            dx, dy = tx - sx, ty - sy
            steps = max(1, int(max(abs(dx), abs(dy)) / 5))
            for i in range(steps):
                step_x = int(round(dx * (i + 1) / steps)) - int(round(dx * i / steps))
                step_y = int(round(dy * (i + 1) / steps)) - int(round(dy * i / steps))
                win32api.mouse_event(
                    win32con.MOUSEEVENTF_MOVE, step_x, step_y, 0, 0
                )
                time.sleep(0.005)
            time.sleep(wait)

        def click(button: str = "left") -> None:
            down = (
                win32con.MOUSEEVENTF_LEFTDOWN
                if button == "left"
                else win32con.MOUSEEVENTF_RIGHTDOWN
            )
            up = (
                win32con.MOUSEEVENTF_LEFTUP
                if button == "left"
                else win32con.MOUSEEVENTF_RIGHTUP
            )
            win32api.mouse_event(down, 0, 0, 0, 0)
            time.sleep(0.05)
            win32api.mouse_event(up, 0, 0, 0, 0)
            time.sleep(0.15)

        def key(vk: int, hold: float = 0.2) -> None:
            scan = int(win32api.MapVirtualKey(vk, 0)) & 0xFF
            win32api.keybd_event(vk, scan, 0, 0)
            time.sleep(hold)
            win32api.keybd_event(vk, scan, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.12)

        _clear_log(log_path)
        m._force_foreground_window(hwnd)
        time.sleep(0.3)
        # 先真实点一下测试窗口，确保焦点确实在它上面
        win32api.SetCursorPos((cl + 10, ct + 10))
        time.sleep(0.1)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.2)
        print(
            "fg before record:",
            hex(int(win32gui.GetForegroundWindow() or 0)),
            "target?",
            m._foreground_is_window(hwnd),
        )
        recorder = m.MacroRecorder(hwnd)
        recorder.start()
        time.sleep(0.2)
        print(
            "fg after record start:",
            hex(int(win32gui.GetForegroundWindow() or 0)),
            "target?",
            m._foreground_is_window(hwnd),
        )
        move_to(100, 100)
        move_to(160, 120)
        move_to(220, 150)
        click("left")
        key(ord("A"), 0.20)
        move_to(320, 220)
        key(ord("D"), 0.25)
        move_to(420, 280)
        click("right")
        time.sleep(0.3)
        events = recorder.stop()
        meta = recorder.meta()
        recorded = _recorded_summary(events)
        print("recorded:", len(events), dict(recorded))
        print("record meta:", meta)

        modes = [
            ("foreground-absolute", False, False),
            ("foreground-relative", False, True),
            ("background", True, False),
        ]
        for label, background, relative in modes:
            _clear_log(log_path)
            _replay(
                hwnd, events, meta, label, background, relative
            )
            target = _read_log(log_path)
            target_summary = _target_summary(target)
            print("\n==", label, "==")
            print("target:", len(target), dict(target_summary))
            print("recorded:", len(events), dict(recorded))

            # 关键序列比较
            rec_keys = [
                int(m._vk_from_key_name(e.get("name", "")) or 0)
                for e in events
                if e.get("type") == "key_down"
            ]
            tgt_keys = [
                int(e.get("vk") or 0)
                for e in target
                if int(e.get("msg", 0)) == WM_KEYDOWN
            ]
            print(
                "key seq rec/tgt:", rec_keys[:20], tgt_keys[:20],
                "match:", rec_keys == tgt_keys,
            )

            # 点击位置比较
            for button, msg in (
                ("left", WM_LBUTTONDOWN),
                ("right", WM_RBUTTONDOWN),
            ):
                rec_clicks = [
                    (
                        float(e.get("nx", 0)) * cw,
                        float(e.get("ny", 0)) * ch,
                    )
                    for e in events
                    if e.get("type") == "down"
                    and e.get("button", "left") == button
                ]
                tgt_clicks = [
                    (float(e.get("x", 0)), float(e.get("y", 0)))
                    for e in target
                    if int(e.get("msg", 0)) == msg
                ]
                print(
                    f"{button} click count rec/tgt:",
                    len(rec_clicks),
                    len(tgt_clicks),
                )
                if rec_clicks and tgt_clicks:
                    errs = [
                        math.hypot(a[0] - b[0], a[1] - b[1])
                        for a, b in zip(rec_clicks, tgt_clicks)
                    ]
                    print(
                        f"{button} click pos err avg/max:",
                        round(sum(errs) / len(errs), 2),
                        round(max(errs), 2),
                    )

            # 鼠标移动路径比较
            rec_moves = [
                (
                    float(e.get("nx", 0)) * cw,
                    float(e.get("ny", 0)) * ch,
                )
                for e in events
                if e.get("type") == "move"
            ]
            tgt_moves = [
                (float(e.get("x", 0)), float(e.get("y", 0)))
                for e in target
                if int(e.get("msg", 0)) == WM_MOUSEMOVE
            ]
            print("move count rec/tgt:", len(rec_moves), len(tgt_moves))
            if rec_moves and tgt_moves:
                a = _nearest_error(tgt_moves, rec_moves)
                b = _nearest_error(rec_moves, tgt_moves)
                print(
                    "move err tgt->rec avg/max:",
                    round(a[0], 2), round(a[1], 2),
                )
                print(
                    "move err rec->tgt avg/max:",
                    round(b[0], 2), round(b[1], 2),
                )

            rec_raw = [
                (int(e.get("dx", 0)), int(e.get("dy", 0)))
                for e in events
                if e.get("type") == "raw_move"
            ]
            tgt_raw = [
                (int(e.get("dx", 0)), int(e.get("dy", 0)))
                for e in target
                if e.get("raw")
            ]
            print(
                "raw count rec/tgt:", len(rec_raw), len(tgt_raw)
            )
            if rec_raw and tgt_raw:
                n = min(len(rec_raw), len(tgt_raw))
                same = sum(
                    1 for a, b in zip(rec_raw[:n], tgt_raw[:n]) if a == b
                )
                print(
                    "raw exact prefix:", same, "/", n,
                    "first rec/tgt:",
                    rec_raw[:6], tgt_raw[:6],
                )
                # 序列长度可能因采样不同而不一致，比较总和
                print(
                    "raw sum rec/tgt:",
                    (sum(x for x, _ in rec_raw), sum(y for _, y in rec_raw)),
                    (sum(x for x, _ in tgt_raw), sum(y for _, y in tgt_raw)),
                )

            # 时间偏差（点击）
            rec_times = [
                float(e.get("t", 0))
                for e in events
                if e.get("type") == "down"
            ]
            tgt_times = [
                float(e.get("t", 0))
                for e in target
                if int(e.get("msg", 0)) == WM_LBUTTONDOWN
            ]
            if rec_times and tgt_times:
                base = tgt_times[0]
                tgt_rel = [t - base for t in tgt_times]
                diffs = [
                    abs(a - b)
                    for a, b in zip(rec_times, tgt_rel)
                ]
                print(
                    "click time err avg/max:",
                    round(sum(diffs) / len(diffs), 3),
                    round(max(diffs), 3),
                )

        print("\n日志：")
        print(" ", m.RECORD_DEBUG_PATH)
        print(" ", m.PLAYER_DEBUG_PATH)
        print(" ", log_path)
        return 0
    finally:
        try:
            child.terminate()
            child.wait(timeout=3)
        except Exception:
            pass


if __name__ == "__main__":
    if "--target" in sys.argv:
        idx = sys.argv.index("--target")
        sys.exit(run_target(sys.argv[idx + 1]))
    sys.exit(main())
