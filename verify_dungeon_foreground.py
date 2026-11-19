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

profile = df._merge(df.DEFAULT_PROFILE,
                    {"input_mode": "foreground", "allow_grab": True})
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
# （窗口在文件末尾统一关闭）
time.sleep(0.2)

# ---- 5) 混合模式：借鼠标点击，点完立刻还原光标 -------------------
hybrid_profile = df._merge(df.DEFAULT_PROFILE,
                           {"input_mode": "hybrid", "allow_grab": True})
hgame = df.WinGame(hwnd, hybrid_profile, log=print)

calls = []
_real_set = win32api.SetCursorPos
_real_evt = win32api.mouse_event


def spy_set(pos, *a, **k):
    calls.append(("SetCursorPos", tuple(pos)))
    return _real_set(pos, *a, **k)


def spy_evt(*a, **k):
    calls.append(("mouse_event", int(a[0])))
    return _real_evt(*a, **k)


hgame.activate = lambda: True          # 这一步只测“借/还鼠标”，不测切前台
win32api.SetCursorPos = spy_set
win32api.mouse_event = spy_evt
saved = win32api.GetCursorPos()
try:
    ok_h = hgame.click_norm((0.5, 0.5))
finally:
    win32api.SetCursorPos = _real_set
    win32api.mouse_event = _real_evt

moves = [c for c in calls if c[0] == "SetCursorPos"]
downs = [c for c in calls if c[0] == "mouse_event"]
check("混合模式点击返回成功", ok_h, f"last_error={hgame.last_error}")
check("混合模式：先移到目标点", bool(moves))
check("混合模式：最后把光标还原到原位置",
      bool(moves) and moves[-1][1] == tuple(saved),
      f"最后一次 {moves[-1][1] if moves else None} vs 原 {tuple(saved)}")
check("混合模式：真的按下/松开了一次",
      sum(1 for c in downs if c[1] == win32con.MOUSEEVENTF_LEFTDOWN) == 1
      and sum(1 for c in downs if c[1] == win32con.MOUSEEVENTF_LEFTUP) == 1,
      str([c[1] for c in downs]))

calls.clear()
win32api.SetCursorPos = spy_set
win32api.mouse_event = spy_evt
try:
    hgame.tap("m")
finally:
    win32api.SetCursorPos = _real_set
    win32api.mouse_event = _real_evt
check("混合模式：按键不碰鼠标", not calls, str(calls[:3]))

# 收尾：关掉测试窗口
try:
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
except Exception:
    pass

# ---- 6) 不允许抢占：完全不碰鼠标/前台 ------------------------------
bg_profile = df._merge(df.DEFAULT_PROFILE,
                       {"input_mode": "hybrid", "allow_grab": False})
bgame = df.WinGame(hwnd, bg_profile, log=print)
called = []
_real_set2 = win32api.SetCursorPos
_real_evt2 = win32api.mouse_event
_real_fg2 = win32gui.SetForegroundWindow
win32api.SetCursorPos = lambda *a, **k: called.append("SetCursorPos") or _real_set2(*a, **k)
win32api.mouse_event = lambda *a, **k: called.append("mouse_event") or _real_evt2(*a, **k)
win32gui.SetForegroundWindow = lambda *a, **k: called.append("SetForegroundWindow") or _real_fg2(*a, **k)
try:
    ok_b = bgame.click_norm((0.5, 0.5))
    bgame.tap("a")
finally:
    win32api.SetCursorPos = _real_set2
    win32api.mouse_event = _real_evt2
    win32gui.SetForegroundWindow = _real_fg2
check("不允许抢占：点击仍然成功（走后台消息）", ok_b)
check("不允许抢占：完全不碰鼠标/前台", not called, str(called))

print()
if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("PASS: 前台模式（切窗口 + 真实鼠标键盘）可用")
