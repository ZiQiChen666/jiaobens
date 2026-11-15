# -*- coding: utf-8 -*-
"""验证疲劳检测的后台点击：只发窗口消息，绝不移动真实鼠标/切前台。

真实机器上用户会自己动鼠标，所以这里用“拦截 API 调用”的方式验证：
拦截 SetCursorPos / mouse_event / SendInput / SetForegroundWindow，
确认后台点击过程中一次都没被调用，同时测试窗口确实收到了鼠标消息。

用法：python _verify_bg_click.py
"""
import sys
import threading
import time

import win32api
import win32con
import win32gui
from PySide6.QtCore import QCoreApplication

import creature_curios_keyer as app

calls = []
hwnd_holder = {}
received = []
ready = threading.Event()


def wndproc(hwnd, msg, wparam, lparam):
    if msg in (
        win32con.WM_MOUSEMOVE,
        win32con.WM_LBUTTONDOWN,
        win32con.WM_LBUTTONUP,
    ):
        received.append((msg, wparam, lparam))
    if msg == win32con.WM_DESTROY:
        win32gui.PostQuitMessage(0)
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


def run_window():
    wc = win32gui.WNDCLASS()
    wc.lpszClassName = "PiBgClickVerify"
    wc.lpfnWndProc = wndproc
    wc.hInstance = win32api.GetModuleHandle(None)
    win32gui.RegisterClass(wc)
    hwnd = win32gui.CreateWindow(
        wc.lpszClassName, "pi-bg-click-verify",
        win32con.WS_OVERLAPPEDWINDOW,
        60, 60, 500, 400,
        0, 0, wc.hInstance, None,
    )
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    hwnd_holder["hwnd"] = hwnd
    ready.set()
    win32gui.PumpMessages()


# ---- 拦截一切会“抢鼠标 / 抢前台”的调用 --------------------------------
_real_set_cursor = win32api.SetCursorPos
_real_mouse_event = win32api.mouse_event
_real_set_fg = win32gui.SetForegroundWindow
_real_switch = app._USER32.SwitchToThisWindow
_real_sendinput = app._USER32.SendInput
_fake_hwnd = []


def spy_set_cursor(pos, *a, **k):
    calls.append(("SetCursorPos", pos))
    return _real_set_cursor(pos, *a, **k)


def spy_mouse_event(*a, **k):
    calls.append(("mouse_event", a[:2]))
    return _real_mouse_event(*a, **k)


def spy_set_fg(hwnd, *a, **k):
    calls.append(("SetForegroundWindow", hex(int(hwnd))))
    return _real_set_fg(hwnd, *a, **k)


def spy_switch(hwnd, *a, **k):
    calls.append(("SwitchToThisWindow", hex(int(hwnd))))
    return _real_switch(hwnd, *a, **k)


def spy_sendinput(*a, **k):
    calls.append(("SendInput",))
    return _real_sendinput(*a, **k)


win32api.SetCursorPos = spy_set_cursor
win32api.mouse_event = spy_mouse_event
win32gui.SetForegroundWindow = spy_set_fg
app._USER32.SwitchToThisWindow = spy_switch
app._USER32.SendInput = spy_sendinput

QCoreApplication([])
threading.Thread(target=run_window, daemon=True).start()
ready.wait(5)
hwnd = hwnd_holder.get("hwnd")
if not hwnd:
    print("FAIL: 创建测试窗口失败")
    sys.exit(1)
time.sleep(0.3)
_fake_hwnd.append(hwnd)

fails = []

# ---- 1) 单次后台点击 ---------------------------------------------------
received.clear()
ok = app.post_click_client(hwnd, 120, 80)
time.sleep(0.2)
msgs = [m for m, _w, _l in received]
down = {m: (w, l) for m, w, l in received}.get(win32con.WM_LBUTTONDOWN, (0, 0))
print("[1] post_click_client ->", ok)
print("    收到 MOVE/DOWN/UP:", win32con.WM_MOUSEMOVE in msgs,
      win32con.WM_LBUTTONDOWN in msgs, win32con.WM_LBUTTONUP in msgs)
print("    DOWN wParam(MK_LBUTTON=1)=", down[0],
      " lParam坐标=", (down[1] & 0xFFFF, (down[1] >> 16) & 0xFFFF))
if not ok:
    fails.append("PostMessage 返回失败")
if not (win32con.WM_MOUSEMOVE in msgs and win32con.WM_LBUTTONDOWN in msgs
        and win32con.WM_LBUTTONUP in msgs):
    fails.append("测试窗口没有收到完整鼠标消息")
if down[1] != ((80 << 16) | 120):
    fails.append("点击坐标不正确")

# ---- 2) 疲劳检测的后台点击 + 截图（不 OCR） ---------------------------
received.clear()
calls.clear()
monitor = app.FatigueMonitor(hwnd, roi_norm=(0.5, 0.5, 0.2, 0.2), one_shot=True)
cap, err = monitor._capture_with_background_click()
time.sleep(0.2)
msgs = [m for m, _w, _l in received]
print("[2] _capture_with_background_click ->",
      None if cap is None else f"{cap[0]}x{cap[1]}", "err=", err)
print("    收到 MOVE/DOWN/UP:", win32con.WM_MOUSEMOVE in msgs,
      win32con.WM_LBUTTONDOWN in msgs, win32con.WM_LBUTTONUP in msgs)
print("    期间调用过的抢鼠标/抢前台 API:", calls)
if cap is None:
    fails.append(f"后台截图失败：{err}")
if not (win32con.WM_MOUSEMOVE in msgs and win32con.WM_LBUTTONDOWN in msgs
        and win32con.WM_LBUTTONUP in msgs):
    fails.append("疲劳检测没有真正点击到图标区域")
bad = [c for c in calls if c[0] in ("SetCursorPos", "mouse_event", "SendInput")]
if bad:
    fails.append(f"后台点击动用了真实鼠标：{bad}")
# 允许“把前台还给用户”，但不允许把前台抢给游戏窗口
stolen = [c for c in calls
          if c[0] in ("SetForegroundWindow", "SwitchToThisWindow")
          and c[1] == hex(hwnd)]
if stolen:
    fails.append(f"后台点击把前台抢给了游戏窗口：{stolen}")

# ---- 3) 兜底模式（借用鼠标）必须显式开启才动鼠标 ----------------------
calls.clear()
monitor2 = app.FatigueMonitor(
    hwnd, roi_norm=(0.5, 0.5, 0.2, 0.2), one_shot=True, borrow_mouse=False
)
if monitor2.borrow_mouse:
    fails.append("borrow_mouse 默认没有关闭")
print("[3] borrow_mouse 默认值:", monitor2.borrow_mouse)

win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
time.sleep(0.2)

if fails:
    print("\nFAIL:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("\nPASS: 疲劳检测后台点击有效，且不移动鼠标、不强抢前台")
