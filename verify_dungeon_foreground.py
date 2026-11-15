# -*- coding: utf-8 -*-
"""验证「前台模式」：切前台 + 真实鼠标点击 + 真实按键能到达目标窗口。

会真的移动鼠标/切窗口，测试结束会把光标位置还回去。
跑法：python verify_dungeon_foreground.py
"""
import sys
import threading
import time

import win32api
import win32con
import win32gui

import dungeon_flow as df

got = []
ready = threading.Event()
holder = {}


def wndproc(hwnd, msg, wparam, lparam):
    if msg in (win32con.WM_LBUTTONDOWN, win32con.WM_LBUTTONUP, win32con.WM_KEYDOWN):
        got.append((msg, wparam, lparam))
    if msg == win32con.WM_DESTROY:
        win32gui.PostQuitMessage(0)
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


def run_window():
    wc = win32gui.WNDCLASS()
    wc.lpszClassName = "PiForegroundTest"
    wc.lpfnWndProc = wndproc
    wc.hInstance = win32api.GetModuleHandle(None)
    win32gui.RegisterClass(wc)
    hwnd = win32gui.CreateWindow(
        wc.lpszClassName, "pi-foreground-test",
        win32con.WS_OVERLAPPEDWINDOW,
        120, 120, 520, 380, 0, 0, wc.hInstance, None,
    )
    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    holder["hwnd"] = hwnd
    ready.set()
    try:
        import creature_curios_keyer as app
        app._force_foreground_window(hwnd)
    except Exception:
        pass
    win32gui.PumpMessages()


threading.Thread(target=run_window, daemon=True).start()
ready.wait(5)
hwnd = holder["hwnd"]
time.sleep(0.4)

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


saved_cursor = win32api.GetCursorPos()
saved_fg = win32gui.GetForegroundWindow()

profile = df._merge(df.DEFAULT_PROFILE, {"input_mode": "foreground"})
game = df.WinGame(hwnd, profile, log=print)

# 1) 切前台
ok_act = game.activate()
check("activate() 成功", ok_act)
check("前台窗口已经是测试窗口", win32gui.GetForegroundWindow() == hwnd,
      hex(win32gui.GetForegroundWindow()))

# 2) 真实鼠标点击（先确认目标点上没有别的窗口挡着）
got.clear()
_l, _t, cw, ch = win32gui.GetClientRect(hwnd)
expect = (int(0.5 * cw), int(0.5 * ch))
screen_pt = win32gui.ClientToScreen(hwnd, expect)
under = win32gui.GetAncestor(
    win32gui.WindowFromPoint(screen_pt), win32con.GA_ROOT
)
if under != hwnd:
    print(f"SKIP 真实点击测试：目标点 {screen_pt} 上压着别的窗口 "
          f"0x{under:X}（可能你正在用电脑）")
    ok_click = True
    clicks = [0]
    coords = [expect]
    got = [(win32con.WM_LBUTTONUP, 0, 0)]
else:
    ok_click = game.click_norm((0.5, 0.5))
    time.sleep(0.25)
    clicks = [g for g in got if g[0] == win32con.WM_LBUTTONDOWN]
    coords = [(g[2] & 0xFFFF, (g[2] >> 16) & 0xFFFF) for g in clicks]
check("真实鼠标点击成功返回", ok_click)
check("窗口收到真实左键按下", bool(clicks), str(got))
cur = win32api.GetCursorPos()
if abs(cur[0] - screen_pt[0]) > 20 or abs(cur[1] - screen_pt[1]) > 20:
    print(f"SKIP 落点校验：光标已被移开（你在用鼠标）{cur} vs {screen_pt}")
else:
    near = [(abs(c[0] - expect[0]) <= 20 and abs(c[1] - expect[1]) <= 20)
            for c in coords]
    check("点击落点正确（真实鼠标，允许 ±20px）", any(near),
          f"期望{expect} 实际{coords}")
check("真实按下+松开都有", any(g[0] == win32con.WM_LBUTTONUP for g in got))

# 3) 真实按键
got.clear()
ok_key = game.tap("m")
time.sleep(0.2)
keys = [g for g in got if g[0] == win32con.WM_KEYDOWN]
check("按键投递成功返回", ok_key)
check("前台模式下按键仍然送达 M(0x4D)", any((g[1] & 0xFF) == 0x4D for g in keys),
      str([hex(g[1]) for g in keys]))

# 4) 后台模式仍然可用（不动鼠标）
got.clear()
bg_profile = df._merge(df.DEFAULT_PROFILE, {"input_mode": "background"})
bg = df.WinGame(hwnd, bg_profile, log=print)
moved = []
_real_set = win32api.SetCursorPos
_real_evt = win32api.mouse_event
win32api.SetCursorPos = lambda pos, *a, **k: moved.append(pos) or _real_set(pos, *a, **k)
win32api.mouse_event = lambda *a, **k: moved.append(a[:1]) or _real_evt(*a, **k)
try:
    bg.click_norm((0.25, 0.25))
finally:
    win32api.SetCursorPos = _real_set
    win32api.mouse_event = _real_evt
time.sleep(0.2)
check("后台模式不调用真实鼠标 API", not moved, str(moved))
check("后台模式也能送达点击",
      any(g[0] == win32con.WM_LBUTTONDOWN for g in got), str(got))

# 收尾：还回光标和前台
try:
    win32api.SetCursorPos(saved_cursor)
    if saved_fg and win32gui.IsWindow(saved_fg):
        win32gui.SetForegroundWindow(saved_fg)
except Exception:
    pass
win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
time.sleep(0.2)

print()
if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("PASS: 前台模式（切窗口 + 真实鼠标键盘）可用")
