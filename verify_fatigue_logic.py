# -*- coding: utf-8 -*-
"""验证 FatigueMonitor._check_once 的决策逻辑（不真实移动鼠标）。

场景：
  A. 默认（borrow_mouse=False）+ OCR 成功 -> 直接用后台结果，不借用鼠标；
  B. 默认 + OCR 失败 -> 只报告失败，不借用鼠标（不抢鼠标）；
  C. borrow_mouse=True + 后台 OCR 失败 -> 才走“借用鼠标”兜底。

用法：python _verify_fatigue_logic.py
"""
import sys
import threading
import time

import win32api
import win32con
import win32gui
from PySide6.QtCore import QCoreApplication

import creature_curios_keyer as app

ready = threading.Event()
holder = {}
ready_win = threading.Event()


def wndproc(hwnd, msg, wparam, lparam):
    if msg == win32con.WM_DESTROY:
        win32gui.PostQuitMessage(0)
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


def run_window():
    wc = win32gui.WNDCLASS()
    wc.lpszClassName = "PiFatigueLogic"
    wc.lpfnWndProc = wndproc
    wc.hInstance = win32api.GetModuleHandle(None)
    win32gui.RegisterClass(wc)
    hwnd = win32gui.CreateWindow(
        wc.lpszClassName, "pi-fatigue-logic",
        win32con.WS_OVERLAPPEDWINDOW,
        60, 60, 500, 400, 0, 0, wc.hInstance, None,
    )
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    holder["hwnd"] = hwnd
    ready_win.set()
    win32gui.PumpMessages()


QCoreApplication([])
threading.Thread(target=run_window, daemon=True).start()
ready_win.wait(5)
hwnd = holder["hwnd"]
time.sleep(0.3)

results = []
fails = []


def fake_capture():
    return (40, 20, b"\x00" * (40 * 20 * 4)), ""


def make_monitor(borrow, ocr):
    m = app.FatigueMonitor(
        hwnd, roi_norm=(0.1, 0.1, 0.2, 0.2), one_shot=True, borrow_mouse=borrow
    )
    m._capture_with_background_click = lambda *a, **k: fake_capture()
    app.ocr_fatigue_from_bgra = ocr
    m.result.connect(lambda cur, mx, msg: results.append((cur, mx, msg)))
    return m


# ---- A：后台 OCR 成功，不借用鼠标 --------------------------------------
borrow_called = []


def ocr_ok(data, w, h, roi):
    return 100, 200, "当前疲劳值 100/200"


m = make_monitor(False, ocr_ok)
m._capture_with_borrowed_mouse = lambda: borrow_called.append("A") or (None, "")
results.clear()
m._check_once()
print("[A] 结果:", results, "| 借用鼠标:", borrow_called)
if results != [(100, 200, "当前疲劳值 100/200")]:
    fails.append(f"A 结果不对：{results}")
if borrow_called:
    fails.append("A 后台成功时仍然借用了鼠标")


# ---- B：后台 OCR 失败 + 默认不借用 -------------------------------------
def ocr_fail(data, w, h, roi):
    return None, None, "未识别到疲劳文本"


m = make_monitor(False, ocr_fail)
m._capture_with_borrowed_mouse = lambda: borrow_called.append("B") or (None, "")
results.clear()
borrow_called.clear()
m._check_once()
print("[B] 结果:", results, "| 借用鼠标:", borrow_called)
if len(results) != 1 or results[0][0] is not None:
    fails.append(f"B 应只报告失败：{results}")
if borrow_called:
    fails.append("B 默认模式下 OCR 失败仍然借用了鼠标（会抢鼠标！）")


# ---- C：borrow_mouse=True 时后台失败才兜底 ----------------------------
m = make_monitor(True, ocr_fail)
m._capture_with_borrowed_mouse = lambda: borrow_called.append("C") or (None, "借用失败")
results.clear()
borrow_called.clear()
m._check_once()
print("[C] 结果:", results, "| 借用鼠标:", borrow_called)
if borrow_called != ["C"]:
    fails.append(f"C 开启兜底后应借用一次鼠标：{borrow_called}")
if not results or results[-1][0] is not None:
    fails.append(f"C 兜底失败应报告失败：{results}")

win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
time.sleep(0.2)

if fails:
    print("\nFAIL:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("\nPASS: 默认只用后台点击；只有显式开启兜底才会借用鼠标")
