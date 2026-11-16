#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CreatureCurios 后台按键器（基础挂机 · 单开）

功能：
    * 绑定标题包含 "CreatureCurios" 的可见窗口；
    * 在独立 QThread 中通过 win32api.PostMessage 后台投递 A
      （可选空格拾取）；
    * 可选技能 1 / 2（12s / 6s）和自动喝药 Q（默认 60s）；
    * 疲劳检测：框选游戏右下的疲劳提示区域，每 30 秒用 PostMessage
      后台点击一次（不移动鼠标、不抢前台），OCR 读取“当前疲劳值:xxx/xxx”，
      超过 ±阈值时弹出不抢焦点的提醒；
    * 定时提醒：支持倒计时和指定时刻；
    * 每轮间隔设置、轮次统计、Ctrl+Alt+F8 全局热键；
    * 最小化后驻留系统托盘，双击托盘图标恢复。

安装依赖：
    pip install -r requirements.txt

运行：
    python creature_curios_keyer.py

说明：
    本程序只使用 PostMessage 后台投递，不会抢占键盘、鼠标或窗口焦点。
    若目标游戏以管理员权限运行，请同样以管理员权限运行本程序，
    否则 Windows 的 UIPI 机制可能拦截消息。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import datetime
import faulthandler
import json
import os
import re
import sys
import threading
import time
import traceback
from typing import Optional

try:
    import win32api
    import win32con
    import win32gui
except ImportError as exc:  # pragma: no cover - 仅用于给出更友好的报错
    raise SystemExit(
        "缺少 pywin32。请先执行：pip install pywin32\n"
        f"原始错误：{exc}"
    ) from exc

try:
    import keyboard
except Exception as exc:  # keyboard 可能在部分环境下导入失败
    keyboard = None  # type: ignore[assignment]
    KEYBOARD_IMPORT_ERROR: Optional[Exception] = exc
else:
    KEYBOARD_IMPORT_ERROR = None

try:
    import mouse  # 录制/回放鼠标
except Exception as exc:
    mouse = None  # type: ignore[assignment]
    MOUSE_IMPORT_ERROR: Optional[Exception] = exc
else:
    MOUSE_IMPORT_ERROR = None

from PySide6.QtCore import (
    QEvent,
    QObject,
    QRect,
    QSettings,
    QTime,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStatusBar,
    QSystemTrayIcon,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from multi_open import SlotRunner
from raw_input_recorder import RawMouseRecorder

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

APP_NAME = "CreatureCurios 后台按键器"
WINDOW_TITLE_KEYWORD = "CreatureCurios"

VK_A = 0x41
VK_SPACE = 0x20
VK_1 = 0x31
VK_2 = 0x32
VK_Q = 0x51
VK_W = 0x57

WM_KEYDOWN = win32con.WM_KEYDOWN  # 0x0100
WM_KEYUP = win32con.WM_KEYUP      # 0x0101

# Unity 类游戏在失去焦点后会直接丢弃按键消息。
# 这几个消息用于“伪装焦点”，不会真的把游戏切到前台。
WM_ACTIVATE = 0x0006
WM_SETFOCUS = 0x0007
WM_ACTIVATEAPP = 0x001C
WM_NCACTIVATE = 0x0086
WM_MOUSEMOVE = 0x0200
WA_ACTIVE = 1

PRESS_HOLD_SECONDS = 0.05         # A / 空格按下保持 50ms
DEFAULT_INTERVAL_MS = 250
MIN_INTERVAL_MS = 50

SKILL1_COOLDOWN_SECONDS = 12.0
SKILL2_COOLDOWN_SECONDS = 6.0
DEFAULT_POTION_SECONDS = 60
FATIGUE_CHECK_SECONDS = 30.0
DEFAULT_FATIGUE_THRESHOLD = 800

# 多开血量保护：回血红线最低值
HP_RED_LINE = 300

HOTKEY_ID = "ctrl+alt+f8"
HOTKEY_TEXT = "Ctrl + Alt + F8"
HOTKEY_QT = "Ctrl+Alt+F8"


def _data_folder() -> str:
    base = os.getenv("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "CreatureCuriosKeyer")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    return folder


FATIGUE_CONFIG_PATH = os.path.join(_data_folder(), "fatigue_roi.json")
RECORDS_PATH = os.path.join(_data_folder(), "boss_records.json")
HEAL_RECORDS_PATH = os.path.join(_data_folder(), "heal_records.json")
BOSS_LOG_PATH = os.path.join(_data_folder(), "boss_replay.log")
RECORD_DEBUG_PATH = os.path.join(_data_folder(), "record_debug.log")
PLAYER_DEBUG_PATH = os.path.join(_data_folder(), "player_debug.log")


def append_debug_log(path: str, lines: list[str]) -> None:
    if not lines:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(chr(10).join(lines) + chr(10))
    except Exception:
        pass


def load_fatigue_roi_norm() -> Optional[tuple[float, float, float, float]]:
    """读取用户框选的归一化疲劳区域 (rx, ry, rw, rh)。"""
    try:
        with open(FATIGUE_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        values = data.get("roi_norm")
        if not isinstance(values, (list, tuple)) or len(values) != 4:
            return None
        rx, ry, rw, rh = (float(v) for v in values)
        if not (0.0 <= rx <= 1.0 and 0.0 <= ry <= 1.0):
            return None
        if rw <= 0.0 or rh <= 0.0 or rx + rw > 1.002 or ry + rh > 1.002:
            return None
        return (rx, ry, rw, rh)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def save_fatigue_roi_norm(roi_norm: tuple[float, float, float, float]) -> bool:
    try:
        with open(FATIGUE_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {"roi_norm": [round(float(v), 6) for v in roi_norm]},
                f,
                ensure_ascii=False,
                indent=2,
            )
        return True
    except Exception:
        return False



def _load_records(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        records: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            events = item.get("events")
            if not isinstance(events, list) or not events:
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            meta = item.get("meta")
            records.append(
                {
                    "name": name,
                    "created": str(item.get("created") or ""),
                    "events": events,
                    "meta": meta if isinstance(meta, dict) else None,
                }
            )
        return records
    except FileNotFoundError:
        return []
    except Exception:
        return []


def _save_records(path: str, records: list[dict]) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def load_boss_records() -> list[dict]:
    return _load_records(RECORDS_PATH)


def save_boss_records(records: list[dict]) -> bool:
    return _save_records(RECORDS_PATH, records)


def load_heal_records() -> list[dict]:
    return _load_records(HEAL_RECORDS_PATH)


def save_heal_records(records: list[dict]) -> bool:
    return _save_records(HEAL_RECORDS_PATH, records)


def strip_hotkey_tail(events: list[dict]) -> list[dict]:
    """去掉录制末尾用于停止录制的 Ctrl+Alt+F8。"""
    names = {
        "ctrl", "left ctrl", "right ctrl",
        "alt", "left alt", "right alt", "f8",
    }
    start = len(events)
    saw_f8 = False
    while start > 0:
        event = events[start - 1]
        if event.get("type") not in ("key_down", "key_up"):
            break
        name = str(event.get("name", "")).lower()
        if name not in names:
            break
        if name == "f8":
            saw_f8 = True
        start -= 1
    return events[:start] if saw_f8 else events


def roi_from_norm(
    norm: tuple[float, float, float, float], cw: int, ch: int
) -> tuple[int, int, int, int]:
    """把归一化区域换算成当前客户区像素坐标。"""
    rx, ry, rw, rh = norm
    x = int(round(rx * cw))
    y = int(round(ry * ch))
    w = max(2, int(round(rw * cw)))
    h = max(2, int(round(rh * ch)))
    x = max(0, min(x, cw - 1))
    y = max(0, min(y, ch - 1))
    w = max(1, min(w, cw - x))
    h = max(1, min(h, ch - y))
    return (x, y, w, h)


class GUITHREADINFO(ctypes.Structure):
    """GetGUIThreadInfo 返回的线程焦点信息。"""

    _fields_ = [
        ("cbSize", wt.DWORD),
        ("flags", wt.DWORD),
        ("hwndActive", wt.HWND),
        ("hwndFocus", wt.HWND),
        ("hwndCapture", wt.HWND),
        ("hwndMenuOwner", wt.HWND),
        ("hwndMoveSize", wt.HWND),
        ("hwndCaret", wt.HWND),
        ("rcCaret", wt.RECT),
    ]


# 直接的 Win32 调用兜底：个别 pywin32 版本会把 lParam 当作有符号 32 位 LONG，
# 传 0xC0000000 | ... 时可能抛出 OverflowError。
_USER32 = ctypes.WinDLL("user32", use_last_error=True)
_USER32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_USER32.PostMessageW.restype = wt.BOOL
_USER32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
_USER32.MapVirtualKeyW.restype = wt.UINT
_USER32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_USER32.GetWindowThreadProcessId.restype = wt.DWORD
_USER32.GetGUIThreadInfo.argtypes = [wt.DWORD, ctypes.POINTER(GUITHREADINFO)]
_USER32.GetGUIThreadInfo.restype = wt.BOOL
_USER32.IsChild.argtypes = [wt.HWND, wt.HWND]
_USER32.IsChild.restype = wt.BOOL
_USER32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
_USER32.AttachThreadInput.restype = wt.BOOL
_USER32.SwitchToThisWindow.argtypes = [wt.HWND, wt.BOOL]
_USER32.SwitchToThisWindow.restype = None

# ---- 后台截图（疲劳检测用） ------------------------------------------------
_USER32.GetWindowDC.argtypes = [wt.HWND]
_USER32.GetWindowDC.restype = wt.HDC
_USER32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
_USER32.ReleaseDC.restype = ctypes.c_int
_USER32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
_USER32.PrintWindow.restype = wt.BOOL

_GDI32 = ctypes.WinDLL("gdi32", use_last_error=True)
_GDI32.CreateCompatibleDC.argtypes = [wt.HDC]
_GDI32.CreateCompatibleDC.restype = wt.HDC
_GDI32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
_GDI32.CreateCompatibleBitmap.restype = wt.HBITMAP
_GDI32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
_GDI32.SelectObject.restype = wt.HGDIOBJ
_GDI32.BitBlt.argtypes = [
    wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD,
]
_GDI32.BitBlt.restype = wt.BOOL
_GDI32.DeleteObject.argtypes = [wt.HGDIOBJ]
_GDI32.DeleteObject.restype = wt.BOOL
_GDI32.DeleteDC.argtypes = [wt.HDC]
_GDI32.DeleteDC.restype = wt.BOOL


class _BMIH(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


_GDI32.GetDIBits.argtypes = [
    wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p,
    ctypes.POINTER(_BMIH), wt.UINT,
]
_GDI32.GetDIBits.restype = ctypes.c_int


def capture_client_bgra(hwnd: int) -> tuple[Optional[tuple[int, int, bytes]], str]:
    """用 PrintWindow 后台抓取目标窗口客户区，返回 (w, h, BGRA bytes)。

    Unity 的 D3D 窗口一般需要 PW_RENDERFULLCONTENT(2)；失败时退回 BitBlt。
    """
    if not win32gui.IsWindow(hwnd):
        return None, "目标窗口已关闭"
    if win32gui.IsIconic(hwnd):
        return None, "目标窗口已最小化，无法截图"

    hdc = mdc = bmp = old = None
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        win_w, win_h = int(right - left), int(bottom - top)
        if win_w <= 0 or win_h <= 0:
            return None, "目标窗口尺寸无效"

        hdc = _USER32.GetWindowDC(hwnd)
        if not hdc:
            return None, "GetWindowDC 失败"
        mdc = _GDI32.CreateCompatibleDC(hdc)
        bmp = _GDI32.CreateCompatibleBitmap(hdc, win_w, win_h)
        if not mdc or not bmp:
            return None, "创建截图缓冲区失败"
        old = _GDI32.SelectObject(mdc, bmp)

        ok = _USER32.PrintWindow(hwnd, mdc, 2)  # PW_RENDERFULLCONTENT
        if not ok:
            ok = _GDI32.BitBlt(mdc, 0, 0, win_w, win_h, hdc, 0, 0, 0x00CC0020)
        if not ok:
            return None, "PrintWindow / BitBlt 截图失败"

        bi = _BMIH()
        bi.biSize = ctypes.sizeof(_BMIH)
        bi.biWidth = win_w
        bi.biHeight = -win_h  # 负数：自上而下
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0
        buf = ctypes.create_string_buffer(win_w * win_h * 4)
        if not _GDI32.GetDIBits(
            mdc, bmp, 0, win_h, ctypes.cast(buf, ctypes.c_void_p),
            ctypes.byref(bi), 0,
        ):
            return None, "GetDIBits 失败"
        raw = buf.raw

        # PrintWindow 抓的是整个窗口，裁出客户区
        cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
        ox, oy = int(cx - left), int(cy - top)
        cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
        cw, ch = int(cr - cl), int(cb - ct)
        if cw <= 0 or ch <= 0:
            return None, "客户区尺寸无效"
        ox = max(0, min(ox, win_w - 1))
        oy = max(0, min(oy, win_h - 1))
        cw = max(1, min(cw, win_w - ox))
        ch = max(1, min(ch, win_h - oy))

        stride = win_w * 4
        out = bytearray(cw * ch * 4)
        for row in range(ch):
            src = (oy + row) * stride + ox * 4
            out[row * cw * 4:(row + 1) * cw * 4] = raw[src:src + cw * 4]
        return (cw, ch, bytes(out)), ""
    except Exception as exc:
        return None, f"截图异常：{type(exc).__name__}: {exc}"
    finally:
        try:
            if mdc and old:
                _GDI32.SelectObject(mdc, old)
        except Exception:
            pass
        try:
            if bmp:
                _GDI32.DeleteObject(bmp)
        except Exception:
            pass
        try:
            if mdc:
                _GDI32.DeleteDC(mdc)
        except Exception:
            pass
        try:
            if hdc:
                _USER32.ReleaseDC(hwnd, hdc)
        except Exception:
            pass


# ---- 后台鼠标点击（不移动真实光标、不抢前台） -----------------------------

def _post_message(hwnd: int, message: int, wparam: int, lparam: int) -> bool:
    """PostMessage 包装：失败时退回 user32 原生调用，不抛异常。"""
    try:
        result = win32api.PostMessage(hwnd, message, wparam, lparam)
        if result is None:
            return True
        if int(result) == 0:
            return bool(_USER32.PostMessageW(hwnd, message, wparam, lparam))
        return True
    except Exception:
        try:
            return bool(_USER32.PostMessageW(hwnd, message, wparam, lparam))
        except Exception:
            return False


def prime_background_input(hwnd: int, activate: bool = False) -> None:
    """让游戏以为窗口处于激活/有焦点状态，但不真正切换前台。

    只 PostMessage 补发消息，不调用 SetForegroundWindow，所以不会抢走
    用户正在使用的窗口，也不会移动真实光标。

    activate=False（默认，疲劳检测用）：只发 WM_ACTIVATEAPP / WM_NCACTIVATE，
    这两条不会让窗口真的被激活。
    activate=True：额外发 WM_ACTIVATE / WM_SETFOCUS；部分游戏的默认
    窗口过程会因此把窗口拉到前台（抢焦点），所以默认不开。
    """
    if not win32gui.IsWindow(hwnd):
        return
    try:
        foreground = int(win32gui.GetForegroundWindow() or 0)
    except Exception:
        foreground = 0
    if foreground == hwnd:
        return
    _post_message(hwnd, WM_ACTIVATEAPP, 1, 0)
    _post_message(hwnd, WM_NCACTIVATE, 1, 0)
    if activate:
        _post_message(hwnd, WM_ACTIVATE, WA_ACTIVE, foreground)
        _post_message(hwnd, WM_SETFOCUS, foreground, 0)


def _mouse_lparam(client_x: int, client_y: int) -> int:
    return ((int(client_x) & 0xFFFF) | ((int(client_y) & 0xFFFF) << 16))


def post_mouse_move_client(hwnd: int, client_x: int, client_y: int) -> bool:
    """向客户区坐标补发一条 WM_MOUSEMOVE（不动真实光标）。"""
    return _post_message(hwnd, WM_MOUSEMOVE, 0, _mouse_lparam(client_x, client_y))


def post_click_client(
    hwnd: int, client_x: int, client_y: int, hold_seconds: float = 0.04
) -> bool:
    """向窗口客户区坐标发一次后台左键点击（纯消息，不动真实鼠标）。

    顺序：WM_MOUSEMOVE → WM_LBUTTONDOWN → WM_LBUTTONUP → WM_MOUSEMOVE。
    不需要 SetCursorPos，也不会把游戏切到前台，所以用户正在用的
    窗口不会被打断（不抢鼠标、不抢焦点）。
    """
    if not win32gui.IsWindow(hwnd):
        return False
    lparam = _mouse_lparam(client_x, client_y)
    prime_background_input(hwnd)
    ok = _post_message(hwnd, WM_MOUSEMOVE, 0, lparam)
    ok = (
        _post_message(
            hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam
        )
        or ok
    )
    if hold_seconds > 0:
        time.sleep(hold_seconds)
    ok = _post_message(hwnd, win32con.WM_LBUTTONUP, 0, lparam) or ok
    _post_message(hwnd, WM_MOUSEMOVE, 0, lparam)
    return ok


# ---- 疲劳提示识别（用户在界面上框选“悬停/点击触发区”，OCR 读取提示文本） ----

_OCR_LOCK = threading.Lock()
_OCR_ENGINE = None


def _get_ocr_engine():
    global _OCR_ENGINE
    with _OCR_LOCK:
        if _OCR_ENGINE is None:
            from rapidocr_onnxruntime import RapidOCR

            _OCR_ENGINE = RapidOCR()
        return _OCR_ENGINE


def ocr_fatigue_from_bgra(
    data: bytes, w: int, h: int, roi: tuple[int, int, int, int],
) -> tuple[Optional[int], Optional[int], str]:
    """从截图里裁出疲劳提示区域，用 RapidOCR 读取“当前疲劳值:xxx/xxx”。

    返回 (当前值, 最大值, 说明)。识别失败时返回 (None, None, 原因)。
    """
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:
        return None, None, f"缺少 OCR 依赖：{type(exc).__name__}: {exc}"

    try:
        x, y, ww, hh = roi
        if ww < 4 or hh < 4:
            return None, None, "框选的触发区域太小。"

        # BGRA -> RGB
        arr = np.frombuffer(data, dtype=np.uint8).reshape((h, w, 4))
        rgb = arr[:, :, [2, 1, 0]].copy()
        image = Image.fromarray(rgb, "RGB")

        # 提示框通常出现在触发区域的上方/附近，裁一块足够大的区域。
        pad_x = max(90, ww)
        top_pad = max(180, hh * 5)
        x0 = max(0, x - pad_x)
        x1 = min(w, x + ww + pad_x)
        y0 = max(0, y - top_pad)
        y1 = min(h, y + hh + 24)
        crop = image.crop((x0, y0, x1, y1))
        if crop.width < 12 or crop.height < 12:
            return None, None, "提示区域裁剪失败。"

        # 游戏字体较小，放大后再 OCR，识别率会高很多。
        scale = 4 if crop.width <= 420 else 2
        crop = crop.resize(
            (crop.width * scale, crop.height * scale),
            Image.Resampling.LANCZOS,
        )
        # RapidOCR/OpenCV 使用 BGR
        ocr_input = np.array(crop)[:, :, ::-1]

        engine = _get_ocr_engine()
        with _OCR_LOCK:
            result, _elapse = engine(ocr_input)

        texts = [str(item[1]) for item in (result or []) if len(item) >= 3]
        joined = " ".join(texts)
        compact = re.sub(r"\s+", "", joined)
        compact = (
            compact.replace("：", ":")
            .replace("－", "-")
            .replace("—", "-")
            .replace("−", "-")
            .replace("／", "/")
            .replace("﹣", "-")
        )

        # OCR 有时会把中间的 “/” 识别成 “-”，所以分隔符同时接受 / - | l I。
        # 必须同时匹配到“当前…值”，避免把其它 UI 上的 x/y 数字误当成疲劳值。
        match = re.search(
            r"当前[^0-9:：]{0,8}值:?([-一]?\d{1,5})[-/|lI]([-一]?\d{1,5})",
            compact,
        )
        if match is None:
            hint = joined[:120] if joined else "没有识别到文字"
            return None, None, f"未识别到疲劳文本（OCR：{hint}）"

        def to_int(text: str) -> int:
            return int(text.replace("一", "-"))

        current = to_int(match.group(1))
        maximum = to_int(match.group(2))
        return current, maximum, f"当前疲劳值 {current}/{maximum}"
    except Exception as exc:
        return None, None, f"OCR 失败：{type(exc).__name__}: {exc}"


def ocr_text_from_bgra(
    data: bytes,
    w: int,
    h: int,
    roi: tuple[int, int, int, int],
) -> str:
    """对截图里的指定区域做 OCR，返回识别到的文字。"""
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:
        raise RuntimeError(f"缺少 OCR 依赖：{type(exc).__name__}: {exc}")

    x, y, ww, hh = roi
    if ww < 4 or hh < 4:
        return ""
    arr = np.frombuffer(data, dtype=np.uint8).reshape((h, w, 4))
    rgb = arr[:, :, [2, 1, 0]].copy()
    image = Image.fromarray(rgb, "RGB")
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(w, x + ww)
    y1 = min(h, y + hh)
    crop = image.crop((x0, y0, x1, y1))
    if crop.width < 8 or crop.height < 8:
        return ""
    if crop.width <= 260:
        scale = 4
    elif crop.width <= 640:
        scale = 2
    else:
        scale = 1
    if scale > 1:
        crop = crop.resize(
            (crop.width * scale, crop.height * scale),
            Image.Resampling.LANCZOS,
        )
    engine = _get_ocr_engine()
    with _OCR_LOCK:
        result, _elapse = engine(np.array(crop)[:, :, ::-1])
    texts = [
        str(item[1]).strip()
        for item in (result or [])
        if len(item) >= 3
    ]
    return " ".join(texts)


def parse_hp_text(text: str) -> tuple[Optional[int], Optional[int]]:
    """从 OCR 文本里解析“当前/最大”数值。"""
    compact = re.sub(r"\s+", "", str(text or ""))
    compact = (
        compact.replace("：", ":")
        .replace("／", "/")
        .replace("|", "/")
        .replace("l", "/")
        .replace("I", "/")
        .replace("，", ",")
    )
    match = re.search(r"(\d{1,9})\s*/\s*(\d{1,9})", compact)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"\d{1,9}", compact)
    if match:
        return int(match.group(0)), None
    return None, None


def ocr_hp_from_bgra(
    data: bytes,
    w: int,
    h: int,
    roi_norm: tuple[float, float, float, float],
) -> tuple[Optional[int], Optional[int], str]:
    """从血量区域读取“当前/最大”血量。"""
    roi = roi_from_norm(roi_norm, w, h)
    try:
        text = ocr_text_from_bgra(data, w, h, roi)
    except Exception as exc:
        return None, None, f"血量识别异常：{type(exc).__name__}: {exc}"
    current, maximum = parse_hp_text(text)
    if current is None:
        return None, None, f"未识别到血量数字（OCR：{text[:80] or '空'}）"
    return current, maximum, text


# ---------------------------------------------------------------------------
# 后台按键线程
# ---------------------------------------------------------------------------


class KeySender(QThread):
    """独立工作线程：通过 PostMessage 向目标窗口后台投递按键。

    PostMessage 不会切换前台窗口，也不会占用真实键盘。
    """

    cycle_done = Signal(int)       # 每完成一轮，携带累计轮数
    failed = Signal(str)           # 发送失败 / 目标窗口关闭

    def __init__(
        self,
        hwnd: int,
        interval_ms: int,
        use_skills: bool = False,
        use_potion: bool = False,
        potion_interval_ms: int = DEFAULT_POTION_SECONDS * 1000,
        attack_enabled: bool = True,
        use_pickup: bool = True,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.hwnd = int(hwnd)
        self._interval_ms = max(MIN_INTERVAL_MS, int(interval_ms))
        self.rounds = 0

        # is_running 是布尔标志位；_stop_event 用于立即唤醒等待中的线程。
        self.is_running = True
        self._stop_event = threading.Event()

        self._scan_cache: dict[int, int] = {}
        self._last_error = ""

        # 技能 / 喝药调度
        self.use_skills = bool(use_skills)
        self.use_potion = bool(use_potion)
        self.attack_enabled = bool(attack_enabled)
        self.use_pickup = bool(use_pickup)
        self._potion_interval_s = max(5.0, float(potion_interval_ms) / 1000.0)
        self._next_skill1 = 0.0
        self._next_skill2 = 0.0
        self._next_potion = 0.0
        self._next_hp_potion = 0.0
        self._potion_request = threading.Event()

    # -- 外部控制 ---------------------------------------------------------
    def stop(self) -> None:
        """请求停止。线程会在当前消息发送完毕后安全退出。"""
        self.is_running = False
        self._stop_event.set()

    def set_interval(self, interval_ms: int) -> None:
        """运行中也可以动态修改每轮间隔（由 UI 的 valueChanged 调用）。"""
        self._interval_ms = max(MIN_INTERVAL_MS, int(interval_ms))

    def set_use_skills(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled and not self.use_skills:
            now = time.monotonic()
            self._next_skill1 = now
            self._next_skill2 = now
        self.use_skills = enabled

    def set_use_potion(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled and not self.use_potion:
            self._next_potion = time.monotonic() + self._potion_interval_s
        self.use_potion = enabled

    def set_use_pickup(self, enabled: bool) -> None:
        """运行中切换是否在 A 后发送空格拾取。"""
        self.use_pickup = bool(enabled)

    def set_attack_enabled(self, enabled: bool) -> None:
        """运行中暂停/恢复发平A；技能和喝药不受影响。"""
        self.attack_enabled = bool(enabled)

    def set_potion_interval(self, seconds: int) -> None:
        self._potion_interval_s = max(5.0, float(seconds))
        if self.use_potion:
            self._next_potion = time.monotonic() + self._potion_interval_s

    def trigger_potion(self) -> None:
        """请求立即喝药（Q + W）；实际由工作线程按冷却执行。"""
        self._potion_request.set()

    # -- 线程主体 ---------------------------------------------------------
    def run(self) -> None:
        try:
            now = time.monotonic()
            self._next_skill1 = now
            self._next_skill2 = now
            self._next_potion = now + self._potion_interval_s

            while self.is_running and not self._stop_event.is_set():
                if not self._window_alive():
                    self._last_error = "目标窗口已关闭（句柄失效），发送已停止。"
                    break

                if not self.attack_enabled:
                    # 仅定时喝药/技能，不发送 A / 空格
                    if not self._run_scheduled_actions():
                        break
                    if self._stop_event.is_set():
                        break
                    self._wait(0.2)
                    continue

                # 先处理技能 / 喝药，再执行 A（可选空格拾取）
                if not self._run_scheduled_actions():
                    break
                if self._stop_event.is_set():
                    break

                # 动作：A 按下 50ms -> 松开 -> 可选空格拾取
                if not self._tap(VK_A):
                    break
                if self.use_pickup:
                    if self._stop_event.is_set():
                        break
                    self._wait(0.05)
                    if self._stop_event.is_set():
                        break
                    if not self._tap(VK_SPACE):
                        break

                self.rounds += 1
                self.cycle_done.emit(self.rounds)

                if self._stop_event.is_set():
                    break
                # 每轮间隔（运行中修改 UI 会即时生效）。
                # 在间隔期间也持续检查技能/喝药，避免间隔设得较大时技能延迟。
                if not self._wait_main_interval():
                    break

        except Exception as exc:  # 防止线程异常直接吞掉
            self._last_error = f"工作线程异常：{type(exc).__name__}: {exc}"
        finally:
            self.is_running = False
            if self._last_error:
                self.failed.emit(self._last_error)

    # -- 内部工具 ---------------------------------------------------------
    def _run_scheduled_actions(self) -> bool:
        """按冷却时间发送技能 1/2 和喝药 Q。返回 False 表示应结束线程。"""
        now = time.monotonic()
        if self._potion_request.is_set():
            self._potion_request.clear()
            if now >= self._next_hp_potion:
                if not self._window_alive():
                    self._last_error = "目标窗口已关闭（句柄失效），发送已停止。"
                    return False
                if not self._tap(VK_Q):
                    return False
                if self._stop_event.is_set():
                    return False
                if not self._tap(VK_W):
                    return False
                self._next_hp_potion = (
                    time.monotonic() + self._potion_interval_s
                )
                self._next_potion = max(
                    self._next_potion, self._next_hp_potion
                )
                append_debug_log(
                    PLAYER_DEBUG_PATH,
                    [
                        f"[POTION] {datetime.datetime.now().strftime('%H:%M:%S')} "
                        f"hwnd=0x{self.hwnd:X} Q+W "
                        f"interval={self._potion_interval_s:.0f}s"
                    ],
                )

        if not (self.use_skills or self.use_potion):
            return True
        if not self._window_alive():
            self._last_error = "目标窗口已关闭（句柄失效），发送已停止。"
            return False

        if self.use_skills:
            if now >= self._next_skill1:
                if not self._tap(VK_1):
                    return False
                self._next_skill1 = time.monotonic() + SKILL1_COOLDOWN_SECONDS
            if self._stop_event.is_set():
                return False
            if time.monotonic() >= self._next_skill2:
                if not self._tap(VK_2):
                    return False
                self._next_skill2 = time.monotonic() + SKILL2_COOLDOWN_SECONDS
            if self._stop_event.is_set():
                return False

        if self.use_potion and time.monotonic() >= self._next_potion:
            if not self._tap(VK_Q):
                return False
            self._next_potion = time.monotonic() + self._potion_interval_s
        return not self._stop_event.is_set()

    def _window_alive(self) -> bool:
        try:
            return bool(win32gui.IsWindow(self.hwnd))
        except Exception:
            return False

    def _find_message_target(self) -> int:
        """找到真正接收键盘消息的窗口。

        Unity 经常把焦点放在子窗口上，把 WM_KEYDOWN 只发给顶层窗口时
        有时不会被处理。优先使用 GetGUIThreadInfo 返回的 hwndFocus。
        """
        root = self.hwnd
        try:
            pid = wt.DWORD()
            tid = int(_USER32.GetWindowThreadProcessId(root, ctypes.byref(pid)))
            if tid:
                info = GUITHREADINFO()
                info.cbSize = ctypes.sizeof(GUITHREADINFO)
                if _USER32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndFocus:
                    focus = int(info.hwndFocus)
                    if focus == root or _USER32.IsChild(root, focus):
                        return focus
        except Exception:
            pass
        return root

    def _cursor_inside(self) -> bool:
        """物理鼠标当前是否在目标窗口客户区内。"""
        try:
            x, y = win32gui.GetCursorPos()
            cx, cy = win32gui.ScreenToClient(self.hwnd, (x, y))
            left, top, right, bottom = win32gui.GetClientRect(self.hwnd)
            return left <= cx < right and top <= cy < bottom
        except Exception:
            # 无法判断时不额外发送鼠标消息，避免干扰游戏
            return True

    def _prime_background_input(self) -> None:
        """让游戏以为自己处于激活/有焦点状态（不抢占真实焦点）。

        这是 Unity 类游戏后台按键的关键：游戏失去前台后，即使收到
        WM_KEYDOWN 也可能直接丢弃。这里只 PostMessage 补发激活/焦点消息，
        不调用 SetForegroundWindow，所以不会切换窗口，也不会抢键盘。
        """
        if not self._window_alive():
            return

        foreground = 0
        try:
            foreground = int(win32gui.GetForegroundWindow() or 0)
        except Exception:
            pass

        if foreground != self.hwnd:
            self._post_aux(self.hwnd, WM_ACTIVATEAPP, 1, 0)
            self._post_aux(self.hwnd, WM_NCACTIVATE, 1, 0)
            self._post_aux(self.hwnd, WM_ACTIVATE, WA_ACTIVE, foreground)
            self._post_aux(self.hwnd, WM_SETFOCUS, foreground, 0)

        # 当物理鼠标不在游戏窗口内时，向客户区中心补一条 WM_MOUSEMOVE，
        # 让游戏内部的“鼠标仍在窗口内/悬停”状态恢复。
        # 注意：只发消息，不会移动真实的系统光标，也不影响你正在用的窗口。
        if not self._cursor_inside():
            try:
                left, top, right, bottom = win32gui.GetClientRect(self.hwnd)
                x = max(0, (right - left) // 2)
                y = max(0, (bottom - top) // 2)
                mouse_lparam = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
                self._post_aux(self.hwnd, WM_MOUSEMOVE, 0, mouse_lparam)
            except Exception:
                pass

    def _post_aux(self, hwnd: int, message: int, wparam: int, lparam: int) -> bool:
        """发送辅助消息（激活/鼠标）。失败不写入主错误，避免影响按键流程。"""
        try:
            result = win32api.PostMessage(hwnd, message, wparam, lparam)
            if result is not None and int(result) == 0:
                return False
            return True
        except Exception:
            try:
                return bool(_USER32.PostMessageW(hwnd, message, wparam, lparam))
            except Exception:
                return False

    def _scan_code(self, vk: int) -> int:
        """取虚拟键对应的硬件扫描码，并写入 WM_KEYDOWN/WM_KEYUP 的 lParam。"""
        cached = self._scan_cache.get(vk)
        if cached is not None:
            return cached
        try:
            scan = int(win32api.MapVirtualKey(vk, 0)) & 0xFF
        except Exception:
            scan = int(_USER32.MapVirtualKeyW(vk, 0)) & 0xFF
        self._scan_cache[vk] = scan
        return scan

    def _wait(self, seconds: float) -> None:
        """可被 stop() 立即唤醒的 sleep。"""
        if seconds > 0:
            self._stop_event.wait(seconds)

    def _wait_main_interval(self) -> bool:
        """等待“每轮间隔”，期间继续处理技能/喝药冷却。"""
        end = time.monotonic() + self._interval_ms / 1000.0
        while not self._stop_event.is_set():
            if not self._run_scheduled_actions():
                return False
            remaining = end - time.monotonic()
            if remaining <= 0:
                return True
            # 分片等待，保证长间隔下技能/喝药也不会迟到
            self._wait(min(remaining, 0.25))
        return False

    def _tap(self, vk: int) -> bool:
        """按下并松开一个键。返回 False 表示应结束循环。"""
        self._last_error = ""
        if not self._window_alive():
            self._last_error = "目标窗口已关闭（句柄失效），发送已停止。"
            return False

        # 关键：每发一个键之前都补齐 Unity 的后台焦点/鼠标状态。
        self._prime_background_input()
        target = self._find_message_target()

        scan = self._scan_code(vk)
        # lParam 规范：
        #   bit 0-15 : 重复次数 = 1
        #   bit 16-23: 扫描码
        #   bit 30   : 前一次按键状态（松开时为 1）
        #   bit 31   : 转换状态（松开时为 1）
        down_lparam = 1 | (scan << 16)
        up_lparam = 1 | (scan << 16) | 0xC0000000

        if not self._post(target, WM_KEYDOWN, vk, down_lparam):
            return False

        self._wait(PRESS_HOLD_SECONDS)

        # 无论是否已请求停止，都必须发出 KEYUP，避免目标窗口出现“卡键”。
        if not self._post(target, WM_KEYUP, vk, up_lparam):
            return False

        return not self._stop_event.is_set()

    def _post(self, hwnd: int, message: int, wparam: int, lparam: int) -> bool:
        """优先使用 win32api.PostMessage；必要时用 ctypes 兜底。"""
        try:
            result = win32api.PostMessage(hwnd, message, wparam, lparam)
        except (OverflowError, TypeError, ValueError):
            # 某些 pywin32 版本按有符号 32 位 LONG 解析 lParam。
            # 直接用 ctypes 调用 PostMessageW，可正确传递 0xC0000000 高位。
            try:
                if _USER32.PostMessageW(hwnd, message, wparam, lparam):
                    return True
                self._last_error = (
                    "PostMessageW 失败，GetLastError="
                    f"{ctypes.get_last_error()}（可能权限不足）"
                )
            except Exception as exc:
                self._last_error = f"PostMessageW 异常：{type(exc).__name__}: {exc}"
            return False
        except Exception as exc:
            self._last_error = f"PostMessage 异常：{type(exc).__name__}: {exc}"
            return False

        # pywin32 的 PostMessage 在成功时通常返回 None；只有明确返回 0 才视为失败。
        if result is not None:
            try:
                if int(result) == 0:
                    self._last_error = (
                        "PostMessage 返回失败（权限不足？请尝试以管理员身份运行）"
                    )
                    return False
            except (TypeError, ValueError):
                pass
        return True




class TapSender(KeySender):
    """连续发送同一个按键（用于击杀后按空格拾取）。"""

    def __init__(
        self,
        hwnd: int,
        vk: int,
        count: int,
        gap_seconds: float = 0.18,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(
            hwnd,
            MIN_INTERVAL_MS,
            use_skills=False,
            use_potion=False,
            use_pickup=False,
            parent=parent,
        )
        self.vk = int(vk)
        self.count = max(1, int(count))
        self.gap = max(0.05, float(gap_seconds))

    def run(self) -> None:
        try:
            for _ in range(self.count):
                if self._stop_event.is_set():
                    break
                if not self._tap(self.vk):
                    break
                self._wait(self.gap)
        except Exception as exc:
            self._last_error = f"连续按键异常：{type(exc).__name__}: {exc}"
        finally:
            self.is_running = False
            if self._last_error:
                self.failed.emit(self._last_error)


# ---------------------------------------------------------------------------
# 前台窗口辅助（疲劳检测点击游戏时需要）
# ---------------------------------------------------------------------------


def _is_foreground(hwnd: int) -> bool:
    try:
        return int(win32gui.GetForegroundWindow() or 0) == int(hwnd)
    except Exception:
        return False


def _tap_alt() -> None:
    """按一下 Alt 再松开：让本进程获得“可以切前台”的权限（Windows 限制）。"""
    try:
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        time.sleep(0.03)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)
    except Exception:
        pass


def _force_foreground_window(hwnd: int) -> bool:
    """把目标窗口切到前台，返回是否成功。

    依次尝试：直接设前台 → AttachThreadInput → SwitchToThisWindow /
    BringWindowToTop → “按一下 Alt 拿权限”后再设一次；每步都验证结果。
    """
    hwnd = int(hwnd)
    try:
        if not win32gui.IsWindow(hwnd):
            return False
        if win32gui.IsIconic(hwnd):
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.2)
            except Exception:
                pass
        if _is_foreground(hwnd):
            return True

        my_tid = int(win32api.GetCurrentThreadId())
        pid = wt.DWORD()
        for attempt in range(2):
            current = int(win32gui.GetForegroundWindow() or 0)
            if current == hwnd:
                return True
            fg_tid = 0
            if current:
                try:
                    fg_tid = int(
                        _USER32.GetWindowThreadProcessId(current, ctypes.byref(pid))
                    )
                except Exception:
                    fg_tid = 0
            attached = False
            if fg_tid and fg_tid != my_tid:
                try:
                    attached = bool(
                        _USER32.AttachThreadInput(my_tid, fg_tid, True)
                    )
                except Exception:
                    attached = False
            try:
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    try:
                        _USER32.SwitchToThisWindow(hwnd, True)
                    except Exception:
                        pass
                if not _is_foreground(hwnd):
                    try:
                        _USER32.BringWindowToTop(hwnd)
                    except Exception:
                        pass
            finally:
                if attached:
                    try:
                        _USER32.AttachThreadInput(my_tid, fg_tid, False)
                    except Exception:
                        pass
            if _is_foreground(hwnd):
                return True
            if attempt == 0:
                _tap_alt()
                time.sleep(0.05)
        for _ in range(6):
            if _is_foreground(hwnd):
                return True
            time.sleep(0.05)
        return _is_foreground(hwnd)
    except Exception:
        return False


def _restore_foreground_window(prev_fg: int, game_hwnd: int) -> None:
    """尽力把前台窗口恢复成点击游戏之前的那一个。"""
    try:
        if not prev_fg or prev_fg == game_hwnd:
            return
        if not win32gui.IsWindow(prev_fg):
            return
        current = int(win32gui.GetForegroundWindow() or 0)
        if current == prev_fg:
            return

        attached = False
        fg_tid = 0
        my_tid = int(win32api.GetCurrentThreadId())
        if current:
            pid = wt.DWORD()
            fg_tid = int(_USER32.GetWindowThreadProcessId(current, ctypes.byref(pid)))
        if fg_tid and fg_tid != my_tid:
            try:
                attached = bool(_USER32.AttachThreadInput(my_tid, fg_tid, True))
            except Exception:
                attached = False
        try:
            try:
                win32gui.SetForegroundWindow(prev_fg)
            except Exception:
                _USER32.SwitchToThisWindow(prev_fg, True)
        finally:
            if attached:
                try:
                    _USER32.AttachThreadInput(my_tid, fg_tid, False)
                except Exception:
                    pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 录制回放（前台）辅助
# ---------------------------------------------------------------------------

_ULONG_PTR = (
    ctypes.c_ulonglong
    if ctypes.sizeof(ctypes.c_void_p) == 8
    else ctypes.c_ulong
)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _INPUT(ctypes.Structure):
    class _I(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    _anonymous_ = ("i",)
    _fields_ = [("type", wt.DWORD), ("i", _I)]


_USER32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_USER32.SendInput.restype = wt.UINT

_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_XDOWN = 0x0080
_MOUSEEVENTF_XUP = 0x0100
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_VIRTUALDESK = 0x4000

_IGNORED_KEY_PARTS = (
    "browser", "www", "search", "mail", "calculator", "media", "volume",
    "play/pause", "next track", "previous track", "launch", "windows",
    "apps", "sleep", "power",
)

_BROWSER_CLASS_PARTS = (
    "chrome_widgetwin", "mozilla", "firefox", "cef", "webview", "edge",
    "iexplore", "steamwebhelper", "gameoverlayui",
)
_BROWSER_TITLE_PARTS = (
    "google chrome", "microsoft edge", "firefox", "browser", "浏览器",
)


def _is_ignored_key_name(name: str) -> bool:
    """过滤浏览器/多媒体键，避免回放时把浏览器打开。"""
    low = str(name or "").lower()
    return any(part in low for part in _IGNORED_KEY_PARTS)


def _is_browser_window(hwnd: int) -> bool:
    try:
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False
        cls = win32gui.GetClassName(hwnd).lower()
        title = win32gui.GetWindowText(hwnd).lower()
    except Exception:
        return False
    if any(part in cls for part in _BROWSER_CLASS_PARTS):
        return True
    return any(part in title for part in _BROWSER_TITLE_PARTS)


def _foreground_is_window(hwnd: int) -> bool:
    try:
        fg = int(win32gui.GetForegroundWindow() or 0)
        if not fg:
            return False
        root = int(win32gui.GetAncestor(fg, win32con.GA_ROOT))
        return root == int(hwnd)
    except Exception:
        return False


def _send_mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
    inp = _INPUT()
    inp.type = 0  # INPUT_MOUSE
    inp.mi.dx = int(dx)
    inp.mi.dy = int(dy)
    inp.mi.mouseData = int(data) & 0xFFFFFFFF
    inp.mi.dwFlags = int(flags)
    _USER32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _send_mouse_move_to(x: int, y: int) -> None:
    """用绝对坐标 SendInput 移动鼠标，避免系统鼠标加速导致位置偏移。"""
    try:
        x, y = int(x), int(y)
        vx = int(win32api.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
        vy = int(win32api.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
        vw = max(1, int(win32api.GetSystemMetrics(78)))  # SM_CXVIRTUALSCREEN
        vh = max(1, int(win32api.GetSystemMetrics(79)))  # SM_CYVIRTUALSCREEN
        nx = int(round((x - vx) * 65535 / max(1, vw - 1)))
        ny = int(round((y - vy) * 65535 / max(1, vh - 1)))
        _send_mouse_input(
            _MOUSEEVENTF_MOVE
            | _MOUSEEVENTF_ABSOLUTE
            | _MOUSEEVENTF_VIRTUALDESK,
            nx,
            ny,
        )
        try:
            now_x, now_y = win32api.GetCursorPos()
            if abs(now_x - x) > 2 or abs(now_y - y) > 2:
                win32api.SetCursorPos((x, y))
        except Exception:
            pass
    except Exception:
        pass


def _send_mouse_button(button: str, down: bool) -> None:
    button = (button or "left").lower()
    if button == "left":
        flags = _MOUSEEVENTF_LEFTDOWN if down else _MOUSEEVENTF_LEFTUP
        data = 0
    elif button == "right":
        flags = _MOUSEEVENTF_RIGHTDOWN if down else _MOUSEEVENTF_RIGHTUP
        data = 0
    elif button == "middle":
        flags = _MOUSEEVENTF_MIDDLEDOWN if down else _MOUSEEVENTF_MIDDLEUP
        data = 0
    elif button in ("x", "x1"):
        flags = _MOUSEEVENTF_XDOWN if down else _MOUSEEVENTF_XUP
        data = 1
    elif button == "x2":
        flags = _MOUSEEVENTF_XDOWN if down else _MOUSEEVENTF_XUP
        data = 2
    else:
        return
    _send_mouse_input(flags, data=data)


def _send_mouse_wheel(delta: int) -> None:
    _send_mouse_input(_MOUSEEVENTF_WHEEL, data=int(delta) * 120)


_SPI_GETMOUSE = 0x0003
_SPI_SETMOUSE = 0x0004
_SPI_GETMOUSESPEED = 0x0070
_SPI_SETMOUSESPEED = 0x0071
_SPIF_UPDATEINIFILE = 0x01
_SPIF_SENDCHANGE = 0x02
_USER32.SystemParametersInfoW.argtypes = [
    wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT
]
_USER32.SystemParametersInfoW.restype = wt.BOOL


def _get_mouse_accel() -> Optional[tuple[int, int, int]]:
    """读系统鼠标加速参数（第 3 个为 1 表示“提高指针精确度”开着）。"""
    try:
        arr = (ctypes.c_int * 3)()
        if _USER32.SystemParametersInfoW(_SPI_GETMOUSE, 0, ctypes.byref(arr), 0):
            return (int(arr[0]), int(arr[1]), int(arr[2]))
    except Exception:
        pass
    return None


def _get_mouse_speed() -> Optional[int]:
    """系统“指针移动速度”（1~20，10 = 1:1）。"""
    try:
        value = ctypes.c_int(0)
        if _USER32.SystemParametersInfoW(
            _SPI_GETMOUSESPEED, 0, ctypes.byref(value), 0
        ):
            return int(value.value)
    except Exception:
        pass
    return None


def _set_mouse_speed(speed: int) -> bool:
    try:
        return bool(
            _USER32.SystemParametersInfoW(
                _SPI_SETMOUSESPEED, 0, ctypes.c_void_p(int(speed)),
                _SPIF_UPDATEINIFILE | _SPIF_SENDCHANGE,
            )
        )
    except Exception:
        return False


def _set_mouse_accel(values) -> bool:
    try:
        v0, v1, v2 = (int(v) for v in values)
        arr = (ctypes.c_int * 3)(v0, v1, v2)
        return bool(
            _USER32.SystemParametersInfoW(
                _SPI_SETMOUSE, 0, ctypes.byref(arr),
                _SPIF_UPDATEINIFILE | _SPIF_SENDCHANGE,
            )
        )
    except Exception:
        return False


class MouseAccelGuard:
    """回放相对鼠标期间临时关掉指针加速，结束后恢复原设置。

    为什么必须关：Windows 的“提高指针精确度”会把 SendInput 的相对位移
    按速度放大（实测 dx=20 → 37px，dx=100 → 322px，最多 3.3 倍），
    而录制时抓的是 Raw Input 的原始位移，两边一放大镜头就转飞了。
    """

    def __init__(self, debug=None):
        self._debug = debug or (lambda _t: None)
        self._saved = None
        self._saved_speed = None
        self._disabled = False

    def disable(self) -> None:
        """临时关掉加速 + 把指针速度调到 1:1（10），回放结束恢复。"""
        self._saved = _get_mouse_accel()
        self._saved_speed = _get_mouse_speed()
        changed = False
        if self._saved and self._saved[2] != 0:
            if _set_mouse_accel((self._saved[0], self._saved[1], 0)):
                changed = True
            else:
                self._debug(f"ACCEL 关闭失败 {self._saved}")
        if self._saved_speed not in (None, 10):
            if _set_mouse_speed(10):
                changed = True
            else:
                self._debug(f"指针速度调整失败 {self._saved_speed}")
        self._disabled = changed
        self._debug(
            f"ACCEL 回放期间关加速/速度→10（原 accel={self._saved} "
            f"speed={self._saved_speed}）"
        )

    def restore(self) -> None:
        if not self._disabled:
            return
        try:
            if self._saved:
                _set_mouse_accel(self._saved)
            if self._saved_speed:
                _set_mouse_speed(self._saved_speed)
            self._debug(
                f"ACCEL 恢复 accel={self._saved} speed={self._saved_speed}"
            )
        except Exception:
            pass
        self._disabled = False


_SPECIAL_VK = {
    "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20,
    "enter": 0x0D, "return": 0x0D, "backspace": 0x08, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "page up": 0x21,
    "page down": 0x22, "up": 0x26, "down": 0x28, "left": 0x25,
    "right": 0x27, "shift": 0x10, "left shift": 0xA0,
    "right shift": 0xA1, "ctrl": 0x11, "control": 0x11,
    "left ctrl": 0xA2, "right ctrl": 0xA3, "alt": 0x12,
    "left alt": 0xA4, "right alt": 0xA5, "caps lock": 0x14,
}
for _i in range(1, 25):
    _SPECIAL_VK[f"f{_i}"] = 0x6F + _i


def _vk_from_key_name(name: str) -> Optional[int]:
    key = (name or "").strip().lower()
    if not key:
        return None
    if key in _SPECIAL_VK:
        return _SPECIAL_VK[key]
    if len(key) == 1:
        try:
            code = int(win32api.VkKeyScan(key))
            if code != -1:
                return code & 0xFF
        except Exception:
            pass
    return None


def _send_key_name(name: str, down: bool) -> bool:
    """发送一个按键：优先用显式 VK 的 keybd_event。

    不再优先用 keyboard.press：中文输入法/系统热键可能把它转成
    VK_PROCESSKEY(229)，游戏收到的就不是原来的 A/D 等按键。
    """
    vk = _vk_from_key_name(name)
    if vk is not None:
        scan = int(win32api.MapVirtualKey(vk, 0)) & 0xFF
        flags = 0 if down else win32con.KEYEVENTF_KEYUP
        try:
            win32api.keybd_event(vk, scan, flags, 0)
            return True
        except Exception:
            pass
    if keyboard is not None:
        try:
            if down:
                keyboard.press(name)
            else:
                keyboard.release(name)
            return True
        except Exception:
            pass
    return False


class MacroRecorder:
    """录制游戏窗口内的鼠标/键盘操作。

    只在游戏是前台窗口、并且鼠标位于游戏窗口内时记录，避免把点击本程序
    或别的窗口的动作录进去。
    """

    def __init__(self, game_hwnd: int):
        self.game_hwnd = int(game_hwnd)
        self._events: list[dict] = []
        self._lock = threading.Lock()
        self._start_time: Optional[float] = None
        self._mouse_hook = None
        self._key_hook = None
        self._active = False
        self._game_rect = (0, 0, 0, 0)
        self._last_move_time = -1.0
        self._last_move_pos: Optional[tuple[int, int]] = None
        # 记录当前按下的鼠标键，避免把“录制开始前已按下”的 up 录进去，
        # 也用于把 mouse 库的 double 事件还原成 down。
        self._pressed_buttons: set[str] = set()
        self._pressed_keys: set[str] = set()
        self._start_cursor_norm: Optional[tuple[float, float]] = None
        self._raw_recorder: Optional[RawMouseRecorder] = None
        self._poll_thread = None
        self._poll_stop = threading.Event()
        self._screen_size: tuple[int, int] = (0, 0)
        self._client_rect: list[int] = [0, 0, 0, 0]
        self._client_size: list[int] = [0, 0]

    def start(self) -> None:
        if mouse is None:
            raise RuntimeError(
                f"缺少 mouse 库：{MOUSE_IMPORT_ERROR or '未安装'}"
            )
        self._events = []
        self._start_time = None
        self._last_move_time = -1.0
        self._last_move_pos = None
        self._pressed_buttons = set()
        self._pressed_keys = set()
        self._start_cursor_norm = None
        self._active = True
        try:
            self._game_rect = win32gui.GetWindowRect(self.game_hwnd)
        except Exception:
            self._game_rect = (0, 0, 0, 0)
        try:
            self._screen_size = (
                int(win32api.GetSystemMetrics(0)),
                int(win32api.GetSystemMetrics(1)),
            )
        except Exception:
            self._screen_size = (0, 0)
        try:
            cl, ct = win32gui.ClientToScreen(self.game_hwnd, (0, 0))
            _l, _t, cw, ch = win32gui.GetClientRect(self.game_hwnd)
            cw, ch = int(cw), int(ch)
            self._client_rect = [int(cl), int(ct), int(cl + cw), int(ct + ch)]
            self._client_size = [cw, ch]
            try:
                mx, my = win32api.GetCursorPos()
                if self._inside_game(int(mx), int(my)):
                    nx = (float(mx) - float(cl)) / max(1, cw)
                    ny = (float(my) - float(ct)) / max(1, ch)
                    self._start_cursor_norm = (
                        max(0.0, min(1.0, nx)),
                        max(0.0, min(1.0, ny)),
                    )
                else:
                    self._start_cursor_norm = None
            except Exception:
                self._start_cursor_norm = None
        except Exception:
            self._client_rect = [0, 0, 0, 0]
            self._client_size = [0, 0]
            self._start_cursor_norm = None
        self._mouse_hook = mouse.hook(self._on_mouse)
        try:
            if keyboard is None:
                raise RuntimeError(
                    f"缺少 keyboard 库：{KEYBOARD_IMPORT_ERROR or '未安装'}"
                )
            self._key_hook = keyboard.hook(self._on_key, suppress=False)
        except Exception:
            # 避免 keyboard 安装失败时把 mouse 钩子遗留在系统里。
            self.stop()
            raise
        # 鼠标按键用高频轮询兜底：mouse 钩子偶尔会漏掉快速点击/double 事件。
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_mouse_buttons, daemon=True
        )
        self._poll_thread.start()
        # Raw Input：记录不受系统鼠标加速影响的相对位移
        try:
            self._raw_recorder = RawMouseRecorder(self._on_raw_delta)
            raw_ok = self._raw_recorder.start()
            append_debug_log(
                RECORD_DEBUG_PATH,
                [
                    f"[REC] START {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                    f"hwnd=0x{self.game_hwnd:X} raw_input={raw_ok}"
                ],
            )
        except Exception:
            self._raw_recorder = None
            append_debug_log(
                RECORD_DEBUG_PATH,
                [
                    f"[REC] START {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                    f"hwnd=0x{self.game_hwnd:X} raw_input=FAILED"
                ],
            )

    def stop(self) -> list[dict]:
        self._active = False
        raw = self._raw_recorder
        self._raw_recorder = None
        if raw is not None:
            try:
                raw.stop()
            except Exception:
                pass
        self._poll_stop.set()
        poll_thread = self._poll_thread
        self._poll_thread = None
        if poll_thread is not None:
            try:
                poll_thread.join(0.5)
            except Exception:
                pass
        try:
            if self._mouse_hook is not None:
                mouse.unhook(self._mouse_hook)
        except Exception:
            pass
        try:
            if self._key_hook is not None:
                keyboard.unhook(self._key_hook)
        except Exception:
            pass
        self._mouse_hook = None
        self._key_hook = None
        with self._lock:
            events = sorted(self._events, key=lambda e: e.get("t", 0.0))
        self._write_debug(events)
        return events

    def _write_debug(self, events: list[dict]) -> None:
        try:
            meta = self.meta()
            counts: dict[str, int] = {}
            for event in events:
                typ = str(event.get("type", ""))
                counts[typ] = counts.get(typ, 0) + 1
            keys_down = sum(
                1 for e in events if e.get("type") == "key_down"
            )
            keys_up = sum(1 for e in events if e.get("type") == "key_up")
            moves = [e for e in events if e.get("type") == "move"]
            duration = float(events[-1].get("t", 0.0)) if events else 0.0
            gaps = [
                float(b.get("t", 0.0)) - float(a.get("t", 0.0))
                for a, b in zip(events, events[1:])
            ]
            lines = [
                f"=== RECORD {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===",
                f"hwnd=0x{self.game_hwnd:X} meta={meta}",
                f"summary events={len(events)} duration={duration:.3f}s "
                f"counts={counts} key_down={keys_down} key_up={keys_up} "
                f"moves={len(moves)} raw_moves={counts.get('raw_move', 0)} "
                f"max_gap={max(gaps) if gaps else 0:.3f}",
            ]
            for event in events:
                lines.append(
                    f"[REC] t={float(event.get('t', 0.0)):.6f} "
                    f"type={event.get('type')} "
                    f"name={event.get('name', '')} "
                    f"button={event.get('button', '')} "
                    f"nx={event.get('nx', '')} ny={event.get('ny', '')} "
                    f"x={event.get('x', '')} y={event.get('y', '')} "
                    f"delta={event.get('delta', '')}"
                )
            append_debug_log(RECORD_DEBUG_PATH, lines)
        except Exception:
            pass

    def meta(self) -> dict:
        return {
            "screen": [int(self._screen_size[0]), int(self._screen_size[1])],
            "client_rect": [int(v) for v in self._client_rect],
            "client_size": [int(v) for v in self._client_size],
            "cursor0_norm": (
                [round(float(v), 6) for v in self._start_cursor_norm]
                if self._start_cursor_norm is not None
                else None
            ),
        }

    def _norm_point(self, x: int, y: int) -> Optional[tuple[float, float]]:
        """把屏幕坐标换算成当前客户区归一化坐标 (0~1)。"""
        try:
            left, top = win32gui.ClientToScreen(self.game_hwnd, (0, 0))
            _l, _t, w, h = win32gui.GetClientRect(self.game_hwnd)
            w, h = int(w), int(h)
            if w <= 0 or h <= 0:
                return None
            nx = (float(x) - float(left)) / float(w)
            ny = (float(y) - float(top)) / float(h)
            return (
                max(0.0, min(1.0, nx)),
                max(0.0, min(1.0, ny)),
            )
        except Exception:
            return None

    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    def click_count(self) -> int:
        with self._lock:
            return sum(1 for e in self._events if e.get("type") == "down")

    def duration(self) -> float:
        with self._lock:
            return float(self._events[-1]["t"]) if self._events else 0.0

    def _inside_game(self, x: int, y: int) -> bool:
        # 每次取当前窗口 rect，录制中轻微移动/改大小也不会用旧范围误判。
        try:
            left, top, right, bottom = win32gui.GetWindowRect(self.game_hwnd)
            self._game_rect = (left, top, right, bottom)
        except Exception:
            left, top, right, bottom = self._game_rect
        if not (left <= x < right and top <= y < bottom):
            return False
        try:
            hwnd_under = int(win32gui.WindowFromPoint((int(x), int(y))) or 0)
            if not hwnd_under:
                return False
            root_under = int(win32gui.GetAncestor(hwnd_under, win32con.GA_ROOT))
            return root_under == self.game_hwnd
        except Exception:
            return False

    def _record(self, event: dict, timestamp: float) -> None:
        with self._lock:
            if self._start_time is None:
                self._start_time = float(timestamp)
            event["t"] = max(0.0, float(timestamp) - self._start_time)
            self._events.append(event)

    def _on_raw_delta(self, dx: int, dy: int) -> None:
        if not self._active:
            return
        if not _foreground_is_window(self.game_hwnd):
            return
        self._record(
            {"type": "raw_move", "dx": int(dx), "dy": int(dy)},
            time.perf_counter(),
        )

    def _poll_mouse_buttons(self) -> None:
        """高频轮询鼠标按键，兜底 mouse 钩子漏掉的快速点击/double。"""
        try:
            buttons = (
                ("left", win32con.VK_LBUTTON),
                ("right", win32con.VK_RBUTTON),
                ("middle", win32con.VK_MBUTTON),
                ("x1", win32con.VK_XBUTTON1),
                ("x2", win32con.VK_XBUTTON2),
            )
        except Exception:
            return
        states = {name: False for name, _vk in buttons}
        recorded: set[str] = set()
        last_pos: dict[str, tuple[int, int]] = {}
        while not self._poll_stop.is_set():
            # 只要鼠标点在游戏窗口上就记录，不强制游戏必须已经在前台；
            # 这样“点击进入游戏”导致窗口刚获得焦点的那一下也能录到。
            can_record = self._active
            now = time.perf_counter()
            for name, vk in buttons:
                try:
                    down = bool(win32api.GetAsyncKeyState(vk) & 0x8000)
                except Exception:
                    continue
                if down == states[name]:
                    continue
                states[name] = down
                if down:
                    if not can_record:
                        continue
                    try:
                        x, y = win32api.GetCursorPos()
                    except Exception:
                        continue
                    if not self._inside_game(int(x), int(y)):
                        continue
                    norm = self._norm_point(int(x), int(y))
                    event = {
                        "type": "down",
                        "x": int(x),
                        "y": int(y),
                        "button": name,
                    }
                    if norm is not None:
                        event["nx"], event["ny"] = norm
                    self._record(event, now)
                    recorded.add(name)
                    last_pos[name] = (int(x), int(y))
                else:
                    if name not in recorded:
                        continue
                    recorded.discard(name)
                    if can_record:
                        try:
                            x, y = win32api.GetCursorPos()
                        except Exception:
                            x, y = last_pos.get(name, (0, 0))
                        if not self._inside_game(int(x), int(y)):
                            x, y = last_pos.get(name, (int(x), int(y)))
                    else:
                        x, y = last_pos.get(name, (0, 0))
                    norm = self._norm_point(int(x), int(y))
                    event = {
                        "type": "up",
                        "x": int(x),
                        "y": int(y),
                        "button": name,
                    }
                    if norm is not None:
                        event["nx"], event["ny"] = norm
                    self._record(event, now)
            time.sleep(0.002)

    def _on_mouse(self, event) -> None:
        if not self._active or not _foreground_is_window(self.game_hwnd):
            return
        try:
            if isinstance(event, mouse.MoveEvent):
                x, y = int(event.x), int(event.y)
                if not self._inside_game(x, y):
                    return
                now = time.perf_counter()
                if self._last_move_time >= 0 and now - self._last_move_time < 0.008:
                    return
                if self._last_move_pos is not None:
                    dx = abs(x - self._last_move_pos[0])
                    dy = abs(y - self._last_move_pos[1])
                    if dx + dy < 1:
                        return
                self._last_move_time = now
                self._last_move_pos = (x, y)
                norm = self._norm_point(x, y)
                ev = {"type": "move", "x": x, "y": y}
                if norm is not None:
                    ev["nx"], ev["ny"] = norm
                self._record(ev, now)
                return

            if isinstance(event, mouse.ButtonEvent):
                # 按键由 _poll_mouse_buttons 记录，避免 double 事件丢点击。
                return

            if isinstance(event, mouse.WheelEvent):
                x, y = mouse.get_position()
                if not self._inside_game(int(x), int(y)):
                    return
                delta = int(round(float(event.delta)))
                if delta == 0:
                    return
                norm = self._norm_point(int(x), int(y))
                ev = {
                    "type": "wheel",
                    "x": int(x),
                    "y": int(y),
                    "delta": delta,
                }
                if norm is not None:
                    ev["nx"], ev["ny"] = norm
                self._record(ev, time.perf_counter())
        except Exception:
            pass

    def _on_key(self, event) -> None:
        if not self._active or not _foreground_is_window(self.game_hwnd):
            return
        try:
            if event.event_type not in ("down", "up"):
                return
            name = str(event.name)
            if not name or _is_ignored_key_name(name):
                return
            if event.event_type == "down":
                if name in self._pressed_keys:
                    # 系统自动重复的 key_down，不重复录制
                    return
                self._pressed_keys.add(name)
                event_type = "key_down"
            else:
                if name not in self._pressed_keys:
                    return
                self._pressed_keys.discard(name)
                event_type = "key_up"
            self._record(
                {"type": event_type, "name": name},
                time.perf_counter(),
            )
        except Exception:
            pass






_FOREGROUND_BRIDGE = None


class ForegroundBridge(QObject):
    """把切前台动作转到 UI 线程执行。

    Windows 的 SetForegroundWindow 在工作线程里经常失败；通过信号让
    主线程执行，成功率更高。等待超时也不会阻塞 UI 线程。
    """

    request = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._event = threading.Event()
        self._pending = 0
        self.request.connect(
            self._handle, Qt.ConnectionType.QueuedConnection
        )

    @Slot(int)
    def _handle(self, hwnd: int) -> None:
        if int(hwnd) != int(self._pending):
            return
        _force_foreground_window(int(hwnd))
        self._event.set()

    def ensure(self, hwnd: int, timeout: float = 1.5) -> bool:
        hwnd = int(hwnd)
        if QThread.currentThread() is self.thread():
            _force_foreground_window(hwnd)
            return _foreground_is_window(hwnd)
        self._pending = hwnd
        self._event.clear()
        self.request.emit(hwnd)
        return self._event.wait(max(0.1, float(timeout)))


class MacroPlayer(QThread):
    """按录制的时间线回放鼠标/键盘（前台）。"""

    done = Signal(str)
    error = Signal(str, str)

    def __init__(
        self,
        game_hwnd: int,
        stage: str,
        events: list[dict],
        meta: Optional[dict] = None,
        speed: float = 1.0,
        lock_user_input: bool = False,
        background: bool = False,
        relative_mouse: bool = False,
        keyboard_only: bool = False,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.game_hwnd = int(game_hwnd)
        self.stage = stage
        self.events = list(events)
        self.meta = dict(meta) if isinstance(meta, dict) else None
        # 兼容旧调用方；当前回放固定走前台，后台/输入锁参数忽略。
        self.lock_user_input = bool(lock_user_input)
        self.keyboard_only = bool(keyboard_only)
        self.background = bool(background) or self.keyboard_only
        self.relative_mouse = bool(relative_mouse)
        self._bg_last_pos: Optional[tuple[int, int]] = None
        try:
            self.speed = max(0.2, min(3.0, float(speed)))
        except (TypeError, ValueError):
            self.speed = 1.0
        self._stop_event = threading.Event()
        self._debug_lines: list[str] = []
        self._late_values: list[float] = []

    def stop(self) -> None:
        self._stop_event.set()

    def _debug(self, text: str) -> None:
        self._debug_lines.append(text)

    def _event_debug(
        self, event: dict, target: float, actual: float
    ) -> None:
        late = actual - target
        self._late_values.append(late)
        self._debug(
            f"[PLAY] t={target:.6f} actual={actual:.6f} "
            f"late={late:+.6f} type={event.get('type')} "
            f"name={event.get('name', '')} "
            f"button={event.get('button', '')} "
            f"nx={event.get('nx', '')} ny={event.get('ny', '')} "
            f"delta={event.get('delta', '')}"
        )

    def _write_debug(self) -> None:
        try:
            late = self._late_values
            summary = (
                f"mode={'background' if self.background else 'foreground'} "
                f"relative={self.relative_mouse} events={len(self.events)} "
                f"sent={len(late)} "
                f"max_late={max(late) if late else 0:.3f} "
                f"avg_late={(sum(late)/len(late)) if late else 0:.3f}"
            )
            lines = [
                f"=== PLAYER {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                f"stage={self.stage} ===",
                f"hwnd=0x{self.game_hwnd:X} meta={self.meta} {summary}",
            ]
            lines.extend(self._debug_lines)
            append_debug_log(PLAYER_DEBUG_PATH, lines)
        except Exception:
            pass

    def _log(self, text: str) -> None:
        line = (
            f"[{datetime.datetime.now().strftime('%H:%M:%S')}] "
            f"[{self.stage}] {text}"
        )
        try:
            with open(BOSS_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    @staticmethod
    def _fg_info() -> str:
        try:
            hwnd = int(win32gui.GetForegroundWindow() or 0)
            title = win32gui.GetWindowText(hwnd)
            cls = win32gui.GetClassName(hwnd)
            return f"0x{hwnd:X} {cls} {title!r}"
        except Exception:
            return "?"

    def _make_point_mapper(self):
        """把录制时的屏幕坐标映射到当前游戏客户区。"""
        try:
            cur_l, cur_t = win32gui.ClientToScreen(self.game_hwnd, (0, 0))
            _l, _t, cur_w, cur_h = win32gui.GetClientRect(self.game_hwnd)
            cur_w, cur_h = int(cur_w), int(cur_h)
        except Exception:
            return lambda x, y: (int(round(x)), int(round(y)))
        if cur_w <= 0 or cur_h <= 0:
            return lambda x, y: (int(round(x)), int(round(y)))

        meta = self.meta or {}
        rec_rect = meta.get("client_rect")
        rec_size = meta.get("client_size")
        rec_l = rec_t = 0
        rec_w = rec_h = 0
        try:
            if isinstance(rec_rect, (list, tuple)) and len(rec_rect) == 4:
                rec_l, rec_t = float(rec_rect[0]), float(rec_rect[1])
            if isinstance(rec_size, (list, tuple)) and len(rec_size) == 2:
                rec_w, rec_h = float(rec_size[0]), float(rec_size[1])
            elif isinstance(rec_rect, (list, tuple)) and len(rec_rect) == 4:
                rec_w = float(rec_rect[2]) - float(rec_rect[0])
                rec_h = float(rec_rect[3]) - float(rec_rect[1])
        except (TypeError, ValueError):
            rec_w = rec_h = 0

        if rec_w <= 0 or rec_h <= 0:
            try:
                sw = int(win32api.GetSystemMetrics(0))
                sh = int(win32api.GetSystemMetrics(1))
            except Exception:
                sw = sh = 0
            if sw > 0 and sh > 0:
                rec_rect = [0, 0, sw, sh]
                rec_l = rec_t = 0.0
                rec_w, rec_h = float(sw), float(sh)
        if rec_w <= 0 or rec_h <= 0:
            return lambda x, y: (int(round(x)), int(round(y)))

        sx = cur_w / rec_w
        sy = cur_h / rec_h
        right = cur_l + cur_w - 1
        bottom = cur_t + cur_h - 1

        def mapper(x, y):
            nx = cur_l + (float(x) - rec_l) * sx
            ny = cur_t + (float(y) - rec_t) * sy
            nx = max(float(cur_l), min(float(right), nx))
            ny = max(float(cur_t), min(float(bottom), ny))
            return int(round(nx)), int(round(ny))

        return mapper

    def _make_event_mapper(self):
        """优先使用归一化坐标；旧录制回退到屏幕坐标换算。"""
        abs_map = self._make_point_mapper()
        try:
            cur_l, cur_t = win32gui.ClientToScreen(self.game_hwnd, (0, 0))
            _l, _t, cur_w, cur_h = win32gui.GetClientRect(self.game_hwnd)
            cur_l, cur_t = int(cur_l), int(cur_t)
            cur_w, cur_h = int(cur_w), int(cur_h)
        except Exception:
            cur_l = cur_t = 0
            cur_w = cur_h = 0
        right = cur_l + cur_w - 1
        bottom = cur_t + cur_h - 1

        def mapper(event: dict):
            if cur_w > 0 and cur_h > 0:
                try:
                    nx = event.get("nx")
                    ny = event.get("ny")
                    if nx is not None and ny is not None:
                        fnx = max(0.0, min(1.0, float(nx)))
                        fny = max(0.0, min(1.0, float(ny)))
                        px = cur_l + fnx * cur_w
                        py = cur_t + fny * cur_h
                        px = max(float(cur_l), min(float(right), px))
                        py = max(float(cur_t), min(float(bottom), py))
                        return int(round(px)), int(round(py))
                except (TypeError, ValueError):
                    pass
            return abs_map(event.get("x", 0), event.get("y", 0))

        return mapper

    def _point_ready(self, x: int, y: int, tries: int = 5) -> bool:
        """目标点是不是已经落到游戏窗口上（被别的窗口压住时多试几次）。"""
        if self._point_belongs_to_game(x, y):
            return True
        for _ in range(max(1, tries)):
            _force_foreground_window(self.game_hwnd)
            time.sleep(0.12)
            if self._point_belongs_to_game(x, y):
                return True
        return False

    def _point_belongs_to_game(self, x: int, y: int) -> bool:
        try:
            under = int(win32gui.WindowFromPoint((int(x), int(y))) or 0)
            if not under:
                return False
            root = int(win32gui.GetAncestor(under, win32con.GA_ROOT))
            return root == int(self.game_hwnd)
        except Exception:
            return False

    # ---- 后台回放：PostMessage，不抢真实鼠标键盘 ----
    def _bg_post(self, hwnd: int, message: int, wparam: int, lparam: int) -> bool:
        try:
            _USER32.PostMessageW(int(hwnd), int(message), int(wparam), int(lparam))
            return True
        except Exception:
            try:
                win32api.PostMessage(int(hwnd), int(message), int(wparam), int(lparam))
                return True
            except Exception:
                return False

    def _bg_prime(self) -> None:
        """让游戏以为自己有焦点/鼠标在窗口内，但不切换真实前台。"""
        try:
            fg = int(win32gui.GetForegroundWindow() or 0)
            if fg != self.game_hwnd:
                self._bg_post(self.game_hwnd, WM_ACTIVATEAPP, 1, 0)
                self._bg_post(self.game_hwnd, WM_NCACTIVATE, 1, 0)
                self._bg_post(self.game_hwnd, WM_ACTIVATE, WA_ACTIVE, fg)
                self._bg_post(self.game_hwnd, WM_SETFOCUS, fg, 0)
        except Exception:
            pass

    def _bg_key(self, name: str, down: bool) -> bool:
        vk = _vk_from_key_name(name)
        if vk is None:
            return False
        try:
            scan = int(win32api.MapVirtualKey(vk, 0)) & 0xFF
        except Exception:
            return False
        lparam = 1 | (scan << 16)
        if not down:
            lparam |= 0xC0000000
        return self._bg_post(
            self.game_hwnd,
            WM_KEYDOWN if down else WM_KEYUP,
            vk,
            lparam,
        )

    def _bg_client_point(self, event: dict, map_event) -> tuple[int, int]:
        _l, _t, cw, ch = win32gui.GetClientRect(self.game_hwnd)
        cw, ch = int(cw), int(ch)
        if cw <= 0 or ch <= 0:
            raise RuntimeError("客户区尺寸无效")
        nx = event.get("nx")
        ny = event.get("ny")
        if nx is not None and ny is not None:
            x = int(round(float(nx) * cw))
            y = int(round(float(ny) * ch))
        else:
            sx, sy = map_event(event)
            x, y = win32gui.ScreenToClient(
                self.game_hwnd, (int(sx), int(sy))
            )
        return max(1, min(cw - 2, x)), max(1, min(ch - 2, y))

    def _bg_mouse(self, event: dict, map_event) -> None:
        typ = str(event.get("type", ""))
        x, y = self._bg_client_point(event, map_event)
        self._bg_last_pos = (x, y)
        lparam = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
        try:
            if typ == "move":
                self._bg_post(self.game_hwnd, WM_MOUSEMOVE, 0, lparam)
            elif typ in ("down", "up"):
                button = str(event.get("button", "left")).lower()
                self._bg_post(self.game_hwnd, WM_MOUSEMOVE, 0, lparam)
                if button == "right":
                    message = (
                        win32con.WM_RBUTTONDOWN
                        if typ == "down"
                        else win32con.WM_RBUTTONUP
                    )
                    wparam = win32con.MK_RBUTTON if typ == "down" else 0
                elif button == "middle":
                    message = (
                        win32con.WM_MBUTTONDOWN
                        if typ == "down"
                        else win32con.WM_MBUTTONUP
                    )
                    wparam = win32con.MK_MBUTTON if typ == "down" else 0
                elif button in ("x", "x1", "x2"):
                    message = 0x020B if typ == "down" else 0x020C
                    button_id = 1 if button != "x2" else 2
                    wparam = button_id << 16
                else:
                    message = (
                        win32con.WM_LBUTTONDOWN
                        if typ == "down"
                        else win32con.WM_LBUTTONUP
                    )
                    wparam = win32con.MK_LBUTTON if typ == "down" else 0
                self._bg_post(self.game_hwnd, message, wparam, lparam)
            elif typ == "wheel":
                delta = int(event.get("delta", 0))
                if delta == 0:
                    return
                sx, sy = win32gui.ClientToScreen(self.game_hwnd, (x, y))
                wparam = (delta & 0xFFFF) << 16
                lp = (int(sx) & 0xFFFF) | ((int(sy) & 0xFFFF) << 16)
                self._bg_post(
                    self.game_hwnd, win32con.WM_MOUSEWHEEL, wparam, lp
                )
        except Exception:
            pass

    def _bg_release_buttons(self, buttons: set[str]) -> None:
        if not buttons or self._bg_last_pos is None:
            return
        x, y = self._bg_last_pos
        lparam = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
        for button in list(buttons):
            try:
                if button == "right":
                    message, wparam = win32con.WM_RBUTTONUP, 0
                elif button == "middle":
                    message, wparam = win32con.WM_MBUTTONUP, 0
                elif button in ("x", "x1", "x2"):
                    message = 0x020C
                    wparam = (1 if button != "x2" else 2) << 16
                else:
                    message, wparam = win32con.WM_LBUTTONUP, 0
                self._bg_post(self.game_hwnd, message, wparam, lparam)
            except Exception:
                pass

    def _send_mouse_relative(self, dx: int, dy: int) -> None:
        if dx == 0 and dy == 0:
            return
        _send_mouse_input(_MOUSEEVENTF_MOVE, int(dx), int(dy))

    def _keyboard_guard(self, prev_fg: int) -> None:
        """键盘模式防抢夺：游戏若自己抢到前台且鼠标不在游戏内，切回用户窗口。"""
        if not self.keyboard_only or not prev_fg:
            return
        try:
            if not win32gui.IsWindow(prev_fg):
                return
            if int(win32gui.GetForegroundWindow() or 0) != self.game_hwnd:
                return
            # 鼠标在游戏窗口内，可能是用户自己点进来的，不抢回去
            x, y = win32api.GetCursorPos()
            cx, cy = win32gui.ScreenToClient(self.game_hwnd, (x, y))
            _l, _t, cw, ch = win32gui.GetClientRect(self.game_hwnd)
            if 0 <= cx < int(cw) and 0 <= cy < int(ch):
                return
            bridge = _FOREGROUND_BRIDGE
            if bridge is not None:
                bridge.ensure(prev_fg, 0.3)
            else:
                _force_foreground_window(prev_fg)
            self._debug(
                f"GUARD restored foreground 0x{prev_fg:X} "
                f"from game 0x{self.game_hwnd:X}"
            )
        except Exception:
            pass

    def _run_background(self) -> None:
        """后台回放：只向游戏窗口 PostMessage，不移动真实鼠标/键盘。"""
        error_msg = ""
        pressed_keys: set[str] = set()
        pressed_buttons: set[str] = set()
        try:
            if not self.events:
                raise RuntimeError("没有录制内容。")
            if not win32gui.IsWindow(self.game_hwnd):
                raise RuntimeError("游戏窗口已关闭，回放已取消。")
            prev_fg = 0
            try:
                prev_fg = int(win32gui.GetForegroundWindow() or 0)
            except Exception:
                pass
            self._bg_prime()
            self._log(
                f"后台回放 hwnd=0x{self.game_hwnd:X} events={len(self.events)} "
                f"keyboard_only={self.keyboard_only}（不抢鼠标键盘）"
            )
            self._debug(
                f"START background hwnd=0x{self.game_hwnd:X} "
                f"events={len(self.events)} speed={self.speed} "
                f"keyboard_only={self.keyboard_only} meta={self.meta}"
            )
            map_event = self._make_event_mapper()
            start = time.perf_counter()
            for event in self.events:
                if self._stop_event.is_set():
                    break
                target = max(0.0, float(event.get("t", 0.0))) / self.speed
                last_guard = time.perf_counter()
                while True:
                    remaining = target - (time.perf_counter() - start)
                    if remaining <= 0:
                        break
                    if self._stop_event.wait(min(max(remaining, 0.0), 0.005)):
                        break
                    if self.keyboard_only:
                        now = time.perf_counter()
                        if now - last_guard >= 0.25:
                            self._keyboard_guard(prev_fg)
                            self._bg_prime()
                            last_guard = now
                if self._stop_event.is_set():
                    break

                typ = str(event.get("type", ""))
                actual = time.perf_counter() - start
                self._event_debug(event, target, actual)
                if typ in ("key_down", "key_up"):
                    name = str(event.get("name", ""))
                    if not name or _is_ignored_key_name(name):
                        continue
                    if typ == "key_down":
                        self._bg_prime()
                        if self._bg_key(name, True):
                            pressed_keys.add(name)
                    else:
                        self._bg_key(name, False)
                        pressed_keys.discard(name)
                elif typ in ("move", "down", "up", "wheel"):
                    if self.keyboard_only:
                        continue
                    self._bg_prime()
                    self._bg_mouse(event, map_event)
                    if typ == "down":
                        pressed_buttons.add(
                            str(event.get("button", "left")).lower()
                        )
                    elif typ == "up":
                        pressed_buttons.discard(
                            str(event.get("button", "left")).lower()
                        )
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            self._log(f"后台回放失败：{error_msg}")
            self._debug(f"ERROR {error_msg}")
        finally:
            self._write_debug()
            for key in list(pressed_keys):
                try:
                    self._bg_key(key, False)
                except Exception:
                    pass
            self._bg_release_buttons(pressed_buttons)
            if error_msg:
                self.error.emit(self.stage, error_msg)
            else:
                self._log("后台回放完成")
                self.done.emit(self.stage)

    def _ensure_foreground(self) -> None:
        bridge = _FOREGROUND_BRIDGE
        if bridge is not None:
            try:
                if bridge.ensure(self.game_hwnd, 1.5) and _foreground_is_window(
                    self.game_hwnd
                ):
                    return
            except Exception:
                pass
        _force_foreground_window(self.game_hwnd)
        for _ in range(20):
            if _foreground_is_window(self.game_hwnd):
                return
            time.sleep(0.05)
        raise RuntimeError(
            "游戏窗口不在前台，回放已停止，避免按键发到其它窗口。"
        )

    def run(self) -> None:
        if self.background:
            self._run_background()
            return
        error_msg = ""
        prev_cursor = None
        prev_fg = 0
        pressed_keys: set[str] = set()
        pressed_buttons: set[str] = set()
        accel_guard = MouseAccelGuard(self._debug)
        try:
            if not self.events:
                raise RuntimeError("没有录制内容。")
            if not win32gui.IsWindow(self.game_hwnd):
                raise RuntimeError("游戏窗口已关闭，回放已取消。")
            try:
                prev_cursor = win32api.GetCursorPos()
                prev_fg = int(win32gui.GetForegroundWindow() or 0)
            except Exception:
                pass

            self._ensure_foreground()
            time.sleep(0.15)
            map_event = self._make_event_mapper()
            relative = bool(self.relative_mouse)
            has_raw = any(
                e.get("type") == "raw_move" for e in self.events
            )
            rel_last: Optional[tuple[int, int]] = None
            if relative and has_raw:
                # 关掉“提高指针精确度”，否则注入的相对位移会被放大 2~3 倍
                accel_guard.disable()
                if accel_guard._disabled:
                    self._log(
                        "已临时关闭系统鼠标加速（提高指针精确度），"
                        "回放结束会自动恢复"
                    )
            if relative:
                try:
                    _l, _t, cur_w, cur_h = win32gui.GetClientRect(
                        self.game_hwnd
                    )
                    cl, ct = win32gui.ClientToScreen(self.game_hwnd, (0, 0))
                    meta = self.meta or {}
                    c0 = meta.get("cursor0_norm")
                    if isinstance(c0, (list, tuple)) and len(c0) == 2:
                        rel_last = (
                            int(round(float(cl) + float(c0[0]) * int(cur_w))),
                            int(round(float(ct) + float(c0[1]) * int(cur_h))),
                        )
                except Exception:
                    rel_last = None
            self._log(
                f"开始回放 hwnd=0x{self.game_hwnd:X} events={len(self.events)} "
                f"relative={relative} fg={self._fg_info()}"
            )
            self._debug(
                f"START foreground hwnd=0x{self.game_hwnd:X} "
                f"events={len(self.events)} speed={self.speed} "
                f"relative={relative} has_raw={has_raw} meta={self.meta}"
            )

            # 绝对坐标模式才预先把光标放到录制起点；相对模式从当前位置开始
            if not relative:
                for event in self.events:
                    if ("x" in event and "y" in event) or (
                        "nx" in event and "ny" in event
                    ):
                        try:
                            px, py = map_event(event)
                            win32api.SetCursorPos((px, py))
                        except Exception:
                            pass
                        break

            start = time.perf_counter()
            for event in self.events:
                if self._stop_event.is_set():
                    break
                target = max(0.0, float(event.get("t", 0.0))) / self.speed
                while True:
                    remaining = target - (time.perf_counter() - start)
                    if remaining <= 0:
                        break
                    if self._stop_event.wait(min(max(remaining, 0.0), 0.005)):
                        break
                if self._stop_event.is_set():
                    break

                typ = str(event.get("type", ""))
                actual = time.perf_counter() - start
                self._event_debug(event, target, actual)
                if typ in (
                    "move", "raw_move", "down", "up", "wheel",
                    "key_down", "key_up",
                ):
                    if not win32gui.IsWindow(self.game_hwnd):
                        raise RuntimeError("游戏窗口已关闭，回放已停止。")
                    if not _foreground_is_window(self.game_hwnd):
                        current_fg = int(win32gui.GetForegroundWindow() or 0)
                        if current_fg and _is_browser_window(current_fg):
                            self._log(
                                f"检测到浏览器抢占前台：{self._fg_info()}"
                            )
                            raise RuntimeError(
                                "检测到浏览器窗口，已停止回放，"
                                "避免按键发到浏览器。"
                            )
                        self._ensure_foreground()

                if typ == "raw_move":
                    if not relative:
                        continue
                    dx = int(event.get("dx", 0))
                    dy = int(event.get("dy", 0))
                    self._debug(f"  -> raw dx={dx} dy={dy}")
                    self._send_mouse_relative(dx, dy)
                    continue

                if typ == "move":
                    px, py = map_event(event)
                    if relative and has_raw:
                        # 有 Raw Input 位移时，镜头交给 raw_move；
                        # 绝对 move 只用于旧的绝对模式，避免重复移动
                        continue
                    if relative:
                        if rel_last is None:
                            rel_last = (int(px), int(py))
                            self._debug(
                                f"  -> rel init px={px} py={py}"
                            )
                        else:
                            dx = int(round(px - rel_last[0]))
                            dy = int(round(py - rel_last[1]))
                            self._debug(
                                f"  -> rel dx={dx} dy={dy} px={px} py={py}"
                            )
                            self._send_mouse_relative(dx, dy)
                            rel_last = (int(px), int(py))
                        continue
                    if not self._point_ready(px, py):
                        raise RuntimeError(
                            "鼠标目标点被其它窗口覆盖，已停止回放，"
                            "避免点到浏览器/桌面。"
                        )
                    self._debug(f"  -> abs px={px} py={py}")
                    _send_mouse_move_to(px, py)
                elif typ in ("down", "up"):
                    button = str(event.get("button", "left")).lower()
                    px, py = map_event(event)
                    if not self._point_ready(px, py):
                        raise RuntimeError(
                            "鼠标目标点被其它窗口覆盖，已停止回放，"
                            "避免点到浏览器/桌面。"
                        )
                    self._debug(
                        f"  -> click {button} {typ} px={px} py={py}"
                    )
                    _send_mouse_move_to(px, py)
                    rel_last = (int(px), int(py))
                    _send_mouse_button(button, typ == "down")
                    if typ == "down":
                        pressed_buttons.add(button)
                    else:
                        pressed_buttons.discard(button)
                elif typ == "wheel":
                    px, py = map_event(event)
                    if not self._point_ready(px, py):
                        raise RuntimeError(
                            "鼠标目标点被其它窗口覆盖，已停止回放，"
                            "避免点到浏览器/桌面。"
                        )
                    self._debug(f"  -> wheel px={px} py={py}")
                    _send_mouse_move_to(px, py)
                    rel_last = (int(px), int(py))
                    _send_mouse_wheel(int(event.get("delta", 0)))
                elif typ in ("key_down", "key_up"):
                    name = str(event.get("name", ""))
                    if not name or _is_ignored_key_name(name):
                        continue
                    _send_key_name(name, typ == "key_down")
                    if typ == "key_down":
                        pressed_keys.add(name)
                    else:
                        pressed_keys.discard(name)

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            self._log(f"回放失败：{error_msg}")
            self._debug(f"ERROR {error_msg}")
        finally:
            for key in list(pressed_keys):
                try:
                    _send_key_name(key, False)
                except Exception:
                    pass
            for button in list(pressed_buttons):
                try:
                    _send_mouse_button(button, False)
                except Exception:
                    pass
            accel_guard.restore()
            if not self._stop_event.is_set():
                _restore_foreground_window(prev_fg, self.game_hwnd)
            # 相对鼠标回放不要恢复光标：SetCursorPos 可能被游戏当成一次镜头转动
            if prev_cursor is not None and not self.relative_mouse:
                try:
                    time.sleep(0.02)
                    win32api.SetCursorPos(prev_cursor)
                except Exception:
                    pass

            self._write_debug()
            if error_msg:
                self.error.emit(self.stage, error_msg)
            else:
                self._log("回放完成")
                self.done.emit(self.stage)


class BossWatcher(QThread):
    """回放/战斗期间定时识别 BOSS 区域，判断 BOSS 是否已经死亡。"""

    update = Signal(str)
    gone = Signal(str)

    def __init__(
        self,
        hwnd: int,
        roi_norm: tuple[float, float, float, float],
        detect_mode: str = "hp",
        kill_hp: int = 0,
        miss_limit: int = 2,
        interval_seconds: float = 1.5,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.hwnd = int(hwnd)
        self.roi_norm = roi_norm
        self.detect_mode = detect_mode if detect_mode in ("hp", "text") else "hp"
        self.kill_hp = max(0, int(kill_hp))
        self.miss_limit = max(1, int(miss_limit))
        self.interval = max(0.5, float(interval_seconds))
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def _finish(self, reason: str) -> None:
        if not self._stop_event.is_set():
            self.gone.emit(reason)

    def run(self) -> None:
        if not win32gui.IsWindow(self.hwnd):
            self._finish("游戏窗口已关闭")
            return

        misses = 0
        while not self._stop_event.is_set():
            if not win32gui.IsWindow(self.hwnd):
                self._finish("游戏窗口已关闭")
                return
            if win32gui.IsIconic(self.hwnd):
                self.update.emit("窗口已最小化，暂停BOSS识别")
                misses = 0
                if self._stop_event.wait(self.interval):
                    break
                continue
            cap, err = capture_client_bgra(self.hwnd)
            if cap is None:
                misses += 1
                self.update.emit(err or "BOSS 区域截图失败")
                if misses >= self.miss_limit:
                    self._finish(f"连续截图失败：{err or '未知原因'}")
                    return
            else:
                w, h, data = cap
                roi = roi_from_norm(self.roi_norm, w, h)
                try:
                    text = ocr_text_from_bgra(data, w, h, roi)
                except Exception as exc:
                    text = ""
                    self.update.emit(
                        f"BOSS 识别异常：{type(exc).__name__}: {exc}"
                    )
                if self.detect_mode == "hp":
                    current, maximum = parse_hp_text(text)
                    if current is None:
                        misses += 1
                        self.update.emit("未识别到 BOSS 血量")
                        if misses >= self.miss_limit:
                            self._finish(
                                f"连续 {misses} 次未识别到 BOSS 血量"
                            )
                            return
                    else:
                        misses = 0
                        if maximum is not None:
                            self.update.emit(
                                f"BOSS 血量 {current}/{maximum}"
                            )
                        else:
                            self.update.emit(f"BOSS 血量 {current}")
                        if current <= self.kill_hp:
                            self._finish(
                                f"BOSS 血量 {current} ≤ {self.kill_hp}"
                            )
                            return
                else:
                    compact = "".join(
                        ch for ch in str(text) if ch.isalnum()
                        or "\u4e00" <= ch <= "\u9fff"
                    )
                    if not compact:
                        misses += 1
                        self.update.emit("未识别到 BOSS 文字")
                        if misses >= self.miss_limit:
                            self._finish(
                                f"连续 {misses} 次未识别到 BOSS"
                            )
                            return
                    else:
                        misses = 0
                        self.update.emit(f"检测到：{text[:48]}")

            if self._stop_event.wait(self.interval):
                break


# ---------------------------------------------------------------------------
# 疲劳检测线程 / 提醒弹窗
# ---------------------------------------------------------------------------


class FatigueMonitor(QThread):
    """每 30 秒后台点击一次用户框选的疲劳提示区域，读取弹出的文本。

    默认流程（不抢鼠标）：PostMessage 后台点击 → 等待提示框出现 →
    PrintWindow 后台截图 → OCR 读取“当前疲劳值:xxx/xxx”。
    全程不调用 SetCursorPos、不切前台，所以用户正在用的窗口不会被打断。

    只有用户显式传入 borrow_mouse=True 且后台点击读不到文本时，
    才会短暂“借用”真实鼠标点击一次，结束后立刻还原光标和前台窗口。
    """

    result = Signal(object, object, str)  # current, maximum, message
    alert = Signal(str)

    def __init__(
        self,
        hwnd: int,
        roi_norm: Optional[tuple[float, float, float, float]] = None,
        interval_seconds: float = FATIGUE_CHECK_SECONDS,
        one_shot: bool = False,
        alert_below: Optional[float] = None,
        alert_above: Optional[float] = None,
        background: bool = False,
        borrow_mouse: bool = False,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.hwnd = int(hwnd)
        self.roi_norm = roi_norm
        self.alert_below = alert_below
        self.alert_above = alert_above
        # 疲劳点击始终走后台 PostMessage，不再使用真实鼠标；
        # background 只保留兼容旧调用。
        self.background = bool(background)
        self.borrow_mouse = bool(borrow_mouse)
        self.interval = max(5.0, float(interval_seconds))
        self._one_shot = bool(one_shot)
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._last_alert_at = 0.0

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def request_check(self) -> None:
        self._wake_event.set()

    def run(self) -> None:
        if self._one_shot:
            if not self._stop_event.is_set():
                self._check_once()
            return

        # 第一次在 interval 秒后检测，之后每次检测完重新计时。
        while not self._stop_event.is_set():
            triggered = self._wake_event.wait(self.interval)
            if self._stop_event.is_set():
                break
            if triggered:
                self._wake_event.clear()
            self._check_once()
            self._wake_event.clear()

    def _check_once(self) -> None:
        if not win32gui.IsWindow(self.hwnd):
            self.result.emit(None, None, "目标窗口已关闭，疲劳检测停止。")
            self._stop_event.set()
            return
        if win32gui.IsIconic(self.hwnd):
            self.result.emit(None, None, "目标窗口已最小化，跳过疲劳检测。")
            return
        if self.roi_norm is None:
            self.result.emit(None, None, "尚未框选疲劳提示区域，请先点“框选区域”。")
            return
        # 1) 默认路径：纯后台 PostMessage 点击 + 后台截图，
        #    不移动真实光标、不切换前台，用户正在用的窗口不受影响。
        cap, cap_err = self._capture_with_background_click()
        current, maximum, msg = self._read_capture(cap, cap_err)
        if current is not None or not self.borrow_mouse:
            self._emit_result(current, maximum, msg)
            return

        # 2) 兜底路径：只有用户显式勾选“后台点击无效时借用鼠标”才走这里，
        #    借用真实鼠标约 1 秒，结束后立刻还原光标和前台窗口。
        cap2, cap_err2 = self._capture_with_borrowed_mouse()
        current2, maximum2, msg2 = self._read_capture(cap2, cap_err2)
        if current2 is not None:
            self._emit_result(current2, maximum2, msg2)
        else:
            self._emit_result(None, None, msg2 or msg)

    def _roi_in_client(self):
        """返回 (客户区内的疲劳区域, 客户区宽, 高, 错误文本)。"""
        try:
            left, top, right, bottom = win32gui.GetClientRect(self.hwnd)
        except Exception as exc:
            return None, 0, 0, f"疲劳区域无效：{type(exc).__name__}: {exc}"
        cw, ch = int(right - left), int(bottom - top)
        if cw <= 0 or ch <= 0:
            return None, 0, 0, "客户区尺寸无效，跳过疲劳检测。"
        try:
            roi = roi_from_norm(self.roi_norm, cw, ch)
        except Exception as exc:
            return None, cw, ch, f"疲劳区域无效：{type(exc).__name__}: {exc}"
        return roi, cw, ch, ""

    def _capture_with_background_click(self):
        """后台点击疲劳图标后截图：全程只发窗口消息，不动真实鼠标。"""
        roi, cw, ch, err = self._roi_in_client()
        if roi is None:
            return None, err
        client_x = max(1, min(cw - 2, roi[0] + roi[2] // 2))
        client_y = max(1, min(ch - 2, roi[1] + roi[3] // 2))

        prev_fg = 0
        try:
            prev_fg = int(win32gui.GetForegroundWindow() or 0)
        except Exception:
            pass
        try:
            cursor_before = win32api.GetCursorPos()
        except Exception:
            cursor_before = None

        post_click_client(self.hwnd, client_x, client_y)

        # 等提示框完全显示；期间持续补发悬停消息，让游戏一直认为
        # 鼠标停在该图标上（同样不会移动真实光标）。
        deadline = time.monotonic() + 1.0
        try:
            while time.monotonic() < deadline:
                if self._stop_event.wait(0.2):
                    return None, ""
                post_mouse_move_client(self.hwnd, client_x, client_y)
        finally:
            # 兜底：万一游戏自己抢到前台或挪了光标，立刻还给用户
            self._give_back_input(prev_fg, cursor_before)
        return capture_client_bgra(self.hwnd)

    def _give_back_input(self, prev_fg: int, cursor_before) -> None:
        """后台操作后把前台窗口（以及被游戏夹走的光标）还给用户。

        只有确认是游戏把前台抢走了才去恢复，避免用户在后台点击
        期间自己移动鼠标时被程序到处乱拽。
        """
        restored_fg = False
        try:
            if (
                prev_fg
                and prev_fg != self.hwnd
                and win32gui.IsWindow(prev_fg)
                and int(win32gui.GetForegroundWindow() or 0) == self.hwnd
            ):
                _restore_foreground_window(prev_fg, self.hwnd)
                restored_fg = True
        except Exception:
            pass
        if not restored_fg or cursor_before is None:
            return
        try:
            cur = win32api.GetCursorPos()
            if (int(cur[0]), int(cur[1])) != (
                int(cursor_before[0]),
                int(cursor_before[1]),
            ):
                win32api.SetCursorPos(cursor_before)
        except Exception:
            pass

    def _capture_with_borrowed_mouse(self):
        """兜底：短暂借用真实鼠标点击一次，随后立即还原（默认不用）。"""
        roi, cw, ch, err = self._roi_in_client()
        if roi is None:
            return None, err

        # 用户正在按鼠标时不要抢鼠标
        try:
            if (
                win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000
                or win32api.GetAsyncKeyState(win32con.VK_RBUTTON) & 0x8000
            ):
                return None, "鼠标按键按下中，跳过本次疲劳检测。"
        except Exception:
            pass

        saved_pos = win32api.GetCursorPos()
        prev_fg = 0
        try:
            prev_fg = int(win32gui.GetForegroundWindow() or 0)
        except Exception:
            pass

        cap = None
        cap_err = ""
        anchor_pos = None
        moved_to_anchor = False
        try:
            # 先把游戏切到前台，避免游戏被别的窗口挡住时点到别的窗口
            _force_foreground_window(self.hwnd)
            time.sleep(0.06)

            # 点击用户保存的区域中心，让游戏弹出疲劳提示
            client_x = max(1, min(cw - 2, roi[0] + roi[2] // 2))
            client_y = max(1, min(ch - 2, roi[1] + roi[3] // 2))
            sx, sy = win32gui.ClientToScreen(self.hwnd, (client_x, client_y))
            anchor_pos = (int(sx), int(sy))

            # 确认这个点最上层确实是游戏窗口，否则不要乱点
            hwnd_under = int(win32gui.WindowFromPoint(anchor_pos) or 0)
            root_under = (
                int(win32gui.GetAncestor(hwnd_under, win32con.GA_ROOT))
                if hwnd_under
                else 0
            )
            if root_under != self.hwnd:
                return None, "疲劳提示区域被其他窗口遮挡，已跳过本次点击。"

            win32api.SetCursorPos(anchor_pos)
            moved_to_anchor = True

            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

            # 等提示框完全显示；停止时也能立即退出
            if self._stop_event.wait(1.0):
                return None, ""
            cap, cap_err = capture_client_bgra(self.hwnd)
        except Exception as exc:
            cap_err = f"疲劳检测异常：{type(exc).__name__}: {exc}"
        finally:
            # 借用只有约 1 秒，结束后始终把前台窗口和鼠标还给用户。
            _restore_foreground_window(prev_fg, self.hwnd)
            if moved_to_anchor and saved_pos is not None:
                try:
                    time.sleep(0.03)
                except Exception:
                    pass
                try:
                    win32api.SetCursorPos(saved_pos)
                except Exception:
                    pass
        return cap, cap_err

    def _read_capture(self, cap, cap_err: str):
        """OCR 截图，返回 (current, maximum, message)；已停止时 message 为空。"""
        if self._stop_event.is_set():
            return None, None, ""
        if cap is None:
            return None, None, cap_err or "疲劳截图失败"
        w, h, data = cap
        try:
            roi = roi_from_norm(self.roi_norm, w, h)
            return ocr_fatigue_from_bgra(data, w, h, roi)
        except Exception as exc:
            return None, None, f"疲劳识别异常：{type(exc).__name__}: {exc}"

    def _emit_result(self, current, maximum, msg: str) -> None:
        if self._stop_event.is_set():
            return
        if current is None or maximum is None:
            self.result.emit(None, None, msg or "疲劳截图失败")
            return

        self.result.emit(int(current), int(maximum), msg)
        alert_message = ""
        if self.alert_below is not None and current < self.alert_below:
            alert_message = (
                f"当前疲劳值 {current}/{maximum}，"
                f"已经低于 {self.alert_below:.0f}，请回游戏处理！"
            )
        elif self.alert_above is not None and current > self.alert_above:
            alert_message = (
                f"当前疲劳值 {current}/{maximum}，"
                f"已经高于 +{self.alert_above:.0f}，请回游戏处理！"
            )
        if alert_message:
            now = time.monotonic()
            # 弹窗最多每分钟提醒一次，避免刷屏
            if now - self._last_alert_at >= 60.0:
                self._last_alert_at = now
                self.alert.emit(alert_message)


class HpMonitor(QThread):
    """定时截取血量区域并 OCR。"""

    result = Signal(object, object, str)  # current, maximum, message

    def __init__(
        self,
        hwnd: int,
        roi_norm: tuple[float, float, float, float],
        interval_seconds: float = 1.0,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.hwnd = int(hwnd)
        self.roi_norm = roi_norm
        self.interval = max(0.5, float(interval_seconds))
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            if not win32gui.IsWindow(self.hwnd):
                self.result.emit(None, None, "目标窗口已关闭。")
                break
            if win32gui.IsIconic(self.hwnd):
                self.result.emit(None, None, "窗口已最小化")
                if self._stop_event.wait(self.interval):
                    break
                continue
            cap, err = capture_client_bgra(self.hwnd)
            if cap is None:
                self.result.emit(None, None, err or "血量截图失败。")
            else:
                w, h, data = cap
                current, maximum, message = ocr_hp_from_bgra(
                    data, w, h, self.roi_norm
                )
                self.result.emit(current, maximum, message)
            if self._stop_event.wait(self.interval):
                break


class NameReader(QThread):
    """一次性读取窗口角色名区域。"""

    result = Signal(str, str)  # name, message

    def __init__(
        self,
        hwnd: int,
        roi_norm: tuple[float, float, float, float],
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.hwnd = int(hwnd)
        self.roi_norm = roi_norm
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if self._stop_event.is_set():
            return
        if not win32gui.IsWindow(self.hwnd):
            self.result.emit("", "游戏窗口已关闭")
            return
        if win32gui.IsIconic(self.hwnd):
            self.result.emit("", "窗口已最小化")
            return
        cap, err = capture_client_bgra(self.hwnd)
        if cap is None:
            self.result.emit("", err or "角色名截图失败")
            return
        w, h, data = cap
        roi = roi_from_norm(self.roi_norm, w, h)
        try:
            text = ocr_text_from_bgra(data, w, h, roi)
        except Exception as exc:
            self.result.emit(
                "",
                f"角色名识别异常：{type(exc).__name__}: {exc}",
            )
            return
        name = " ".join(str(text or "").split())
        name = name[:60]
        if not name:
            self.result.emit("", "未识别到角色名")
            return
        self.result.emit(name, "ok")


class NoticePopup(QWidget):
    """不抢焦点的右下角提醒弹窗。"""

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("提醒")
        self.setFixedWidth(440)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._frame = QFrame()
        self._frame.setObjectName("noticeFrame")
        self._frame.setStyleSheet(
            "QFrame#noticeFrame {"
            " background: #7d1420;"
            " border: 2px solid #ff6b6b;"
            " border-radius: 10px;"
            "}"
        )
        outer.addWidget(self._frame)

        lay = QVBoxLayout(self._frame)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(7)

        self._title = QLabel("⚠  提醒")
        self._title.setStyleSheet(
            "color: #ffffff; font-size: 15px; font-weight: 700; background: transparent;"
        )
        self._message = QLabel("")
        self._message.setWordWrap(True)
        self._message.setStyleSheet(
            "color: #ffe9e9; font-size: 13px; background: transparent;"
        )
        self._hint = QLabel("点击关闭 · 不会抢占当前窗口焦点")
        self._hint.setStyleSheet(
            "color: #f3b7b7; font-size: 11px; background: transparent;"
        )
        lay.addWidget(self._title)
        lay.addWidget(self._message)
        lay.addWidget(self._hint)

        # 让点击穿透到弹窗本身，任何位置都能关闭
        for widget in (self._frame, self._title, self._message, self._hint):
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, title: str, message: str, seconds: int = 15) -> None:
        self._title.setText(f"⚠  {title}")
        self._message.setText(message)
        self.adjustSize()
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            x = max(0, geo.right() - self.width() - 24)
            y = max(0, geo.bottom() - self.height() - 70)
            self.move(x, y)
        self._set_no_activate()
        self.show()
        self._set_no_activate()
        self._hide_timer.start(max(3, int(seconds)) * 1000)

    def _set_no_activate(self) -> None:
        try:
            hwnd = int(self.winId())
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(
                hwnd,
                win32con.GWL_EXSTYLE,
                style | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW,
            )
        except Exception:
            pass

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.hide()
        event.accept()


class _RegionCanvas(QWidget):
    """显示游戏截图，并允许鼠标拖拽框选一个矩形区域。"""

    selection_changed = Signal(object)

    def __init__(
        self,
        image: QImage,
        initial_roi: Optional[tuple[int, int, int, int]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._image = image
        self._scale = 1.0
        self._selection: Optional[tuple[int, int, int, int]] = None
        self._drag_start: Optional[tuple[int, int]] = None
        self._drag_now: Optional[tuple[int, int]] = None
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._apply_display_size()
        if initial_roi is not None:
            self._selection = self._clamp_rect(initial_roi)

    def _apply_display_size(self) -> None:
        iw = max(1, self._image.width())
        ih = max(1, self._image.height())
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            max_w = max(360, min(1200, avail.width() - 100))
            max_h = max(260, min(760, avail.height() - 240))
        else:
            max_w, max_h = 1100, 650
        self._scale = min(max_w / iw, max_h / ih, 1.0)
        self.setFixedSize(
            max(1, int(iw * self._scale)),
            max(1, int(ih * self._scale)),
        )

    def _map_to_image(self, pos) -> tuple[int, int]:
        iw = max(1, self._image.width())
        ih = max(1, self._image.height())
        x = int(pos.x() / max(0.0001, self._scale))
        y = int(pos.y() / max(0.0001, self._scale))
        return max(0, min(iw - 1, x)), max(0, min(ih - 1, y))

    def _clamp_rect(self, rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        iw, ih = self._image.width(), self._image.height()
        x, y, w, h = (int(v) for v in rect)
        x = max(0, min(iw - 1, x))
        y = max(0, min(ih - 1, y))
        w = max(1, min(w, iw - x))
        h = max(1, min(h, ih - y))
        return (x, y, w, h)

    @staticmethod
    def _rect_from_points(
        a: tuple[int, int], b: tuple[int, int]
    ) -> tuple[int, int, int, int]:
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        return (x0, y0, max(1, x1 - x0 + 1), max(1, y1 - y0 + 1))

    def _to_widget_rect(self, rect: tuple[int, int, int, int]) -> QRect:
        x, y, w, h = rect
        s = self._scale
        return QRect(
            int(x * s), int(y * s), max(1, int(w * s)), max(1, int(h * s))
        )

    def selection(self) -> Optional[tuple[int, int, int, int]]:
        return self._selection

    def set_image(self, image: QImage) -> None:
        self._image = image
        self._selection = None
        self._drag_start = None
        self._drag_now = None
        self._apply_display_size()
        self.update()
        self.selection_changed.emit(None)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self.rect(), self._image)

        active = self._selection
        if self._drag_start is not None and self._drag_now is not None:
            active = self._rect_from_points(self._drag_start, self._drag_now)

        if active is not None:
            rect = self._to_widget_rect(active)
            overlay = QColor(0, 0, 0, 105)
            painter.fillRect(QRect(0, 0, self.width(), rect.top()), overlay)
            painter.fillRect(
                QRect(0, rect.bottom() + 1, self.width(), self.height() - rect.bottom() - 1),
                overlay,
            )
            painter.fillRect(QRect(0, rect.top(), rect.left(), rect.height()), overlay)
            painter.fillRect(
                QRect(rect.right() + 1, rect.top(), self.width() - rect.right() - 1, rect.height()),
                overlay,
            )
            painter.setPen(QPen(QColor("#3ddc84"), 2))
            painter.drawRect(rect)
            painter.setPen(QColor("#3ddc84"))
            painter.drawText(
                rect.left() + 4,
                max(14, rect.top() - 6),
                f"{active[2]} × {active[3]}",
            )
        else:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 35))
            painter.setPen(QColor("#ffb454"))
            painter.drawText(
                14, 26, "按住鼠标左键拖动，框住右下角会弹出疲劳提示的图标/按钮区域"
            )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = self._map_to_image(event.position().toPoint())
            self._drag_now = self._drag_start
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_start is not None:
            self._drag_now = self._map_to_image(event.position().toPoint())
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._drag_start is None:
            return
        self._drag_now = self._map_to_image(event.position().toPoint())
        rect = self._rect_from_points(self._drag_start, self._drag_now)
        self._drag_start = None
        self._drag_now = None
        if rect[2] >= 5 and rect[3] >= 3:
            self._selection = rect
            self.selection_changed.emit(rect)
        self.update()


class RegionSelectDialog(QDialog):
    """疲劳提示区域框选对话框。截图来自游戏客户区。"""

    def __init__(
        self,
        image: QImage,
        initial_roi: Optional[tuple[int, int, int, int]],
        recapture,
        parent: Optional[QWidget] = None,
        title_text: Optional[str] = None,
        info_text: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        title_text = title_text or "框选疲劳提示区域"
        info_text = info_text or (
            "框住游戏右下角那个会弹出“当前疲劳值:xxx/xxx”的图标/按钮区域。"
            "不需要框文字或疲劳条；程序之后每 30 秒会在该区域内做一次后台点击"
            "（PostMessage，不移动真实鼠标、不切前台），读取弹出的疲劳值。"
            "如果截图里看不到这个区域，点“重新截图”，程序会先把鼠标移到游戏右下角再截图。"
        )
        self.setWindowTitle(title_text)
        self.setModal(True)
        self._recapture = recapture
        self._selection: Optional[tuple[int, int, int, int]] = self._validate(initial_roi)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(10)

        title = QLabel(title_text)
        title.setStyleSheet(
            "font-size: 15px; font-weight: 700; color: #1b1c1f;"
        )
        layout.addWidget(title)

        info = QLabel(info_text)
        info.setWordWrap(True)
        info.setStyleSheet("color: #4b5563;")
        layout.addWidget(info)

        self.canvas = _RegionCanvas(image, initial_roi, self)
        self.canvas.selection_changed.connect(self._on_selection)
        layout.addWidget(self.canvas, 0, Qt.AlignmentFlag.AlignCenter)

        self.lbl_selection = QLabel("区域: 尚未框选")
        self.lbl_selection.setStyleSheet("color: #1a4f8b;")
        layout.addWidget(self.lbl_selection)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.btn_recapture = QPushButton("重新截图")
        self.btn_recapture.clicked.connect(self._do_recapture)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_save = QPushButton("保存区域")
        self.btn_save.setDefault(True)
        self.btn_save.clicked.connect(self.accept)
        buttons.addWidget(self.btn_recapture)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_cancel)
        buttons.addWidget(self.btn_save)
        layout.addLayout(buttons)

        self._on_selection(self._selection)
        self.adjustSize()

    @staticmethod
    def _validate(roi: Optional[tuple[int, int, int, int]]):
        if (
            isinstance(roi, (tuple, list))
            and len(roi) == 4
            and int(roi[2]) >= 5
            and int(roi[3]) >= 3
        ):
            return (int(roi[0]), int(roi[1]), int(roi[2]), int(roi[3]))
        return None

    def _on_selection(self, rect) -> None:
        self._selection = self._validate(rect)
        if self._selection is None:
            self.lbl_selection.setText("区域: 尚未框选")
            self.btn_save.setEnabled(False)
        else:
            x, y, w, h = self._selection
            self.lbl_selection.setText(f"区域: x={x}, y={y}, 宽={w}, 高={h}")
            self.btn_save.setEnabled(True)

    def _do_recapture(self) -> None:
        if self._recapture is None:
            return
        image = self._recapture()
        if image is None or image.isNull():
            return
        self.canvas.set_image(image)
        self._on_selection(None)
        self.adjustSize()

    def selected_roi(self) -> Optional[tuple[int, int, int, int]]:
        return self._selection


# ---------------------------------------------------------------------------
# 样式
# ---------------------------------------------------------------------------

APP_STYLE = """
* {
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
}
QMainWindow, QWidget#central {
    background: #1b1c1f;
}
QWidget {
    color: #e4e6e9;
    font-size: 13px;
}
QGroupBox {
    background: #202226;
    border: 1px solid #303338;
    border-radius: 10px;
    margin-top: 14px;
    padding: 16px 14px 14px 14px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 5px;
    color: #8ab4f8;
    background: #1b1c1f;
}
QLabel#appTitle {
    font-size: 20px;
    font-weight: 700;
    color: #f1f3f4;
}
QLabel#subtitle {
    color: #b8bcc2;
    font-size: 12px;
}
QLabel#versionBadge {
    background: #2a3b55;
    color: #8ab4f8;
    border-radius: 12px;
    font-weight: 700;
    font-size: 12px;
}
QComboBox, QSpinBox, QTimeEdit, QLineEdit {
    background: #282b30;
    border: 1px solid #3d4148;
    border-radius: 6px;
    padding: 5px 8px;
    min-height: 24px;
    selection-background-color: #34527d;
}
QComboBox:hover, QSpinBox:hover, QTimeEdit:hover, QLineEdit:hover {
    border-color: #5c626b;
}
QComboBox:focus, QSpinBox:focus, QTimeEdit:focus, QLineEdit:focus {
    border-color: #8ab4f8;
}
QComboBox QAbstractItemView {
    background: #282b30;
    border: 1px solid #3d4148;
    selection-background-color: #34527d;
    outline: none;
}
QPushButton {
    background: #33373d;
    border: 1px solid #454a52;
    border-radius: 6px;
    padding: 6px 16px;
    min-height: 26px;
}
QPushButton:hover {
    background: #3e434a;
}
QPushButton:pressed {
    background: #2a2e34;
}
QPushButton:disabled {
    color: #6b717a;
    background: #26292d;
    border-color: #33373c;
}
QCheckBox {
    spacing: 8px;
    color: #dfe2e6;
    font-weight: 600;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #5a6069;
    border-radius: 4px;
    background: #282b30;
}
QCheckBox::indicator:hover {
    border-color: #8ab4f8;
}
QCheckBox::indicator:checked {
    border-color: #8ab4f8;
    background: #34527d;
}
QLabel#boundLabel {
    border: 1px solid #33373c;
    border-radius: 6px;
    padding: 7px 10px;
    color: #9aa0a8;
    background: #1d1f22;
}
QLabel#boundLabel[state="ok"] {
    color: #3ddc84;
    border-color: #2f6b4a;
    background: #17241d;
}
QLabel#boundLabel[state="error"] {
    color: #ff6b6b;
    border-color: #5c2b2b;
    background: #241818;
}
QLabel#actionText {
    color: #d7dbe0;
    background: #26292e;
    border-left: 3px solid #8ab4f8;
    border-radius: 4px;
    padding: 8px 10px;
}
QLabel#muted {
    color: #b8bcc2;
}
QLabel#hint {
    color: #a8aeb6;
    font-size: 12px;
    padding: 2px 2px 6px 2px;
}
QToolTip {
    background: #2a2e34;
    color: #f1f3f4;
    border: 1px solid #5c626b;
    padding: 5px 7px;
}
QStatusBar {
    background: #161719;
    border-top: 1px solid #2d3035;
}
QStatusBar::item {
    border: none;
}
QLabel#statusLabel {
    color: #aeb4bd;
    padding: 3px 2px;
}
QLabel#roundsLabel {
    color: #8ab4f8;
    font-weight: 700;
    padding: 3px 10px;
    background: #1f2733;
    border-radius: 8px;
}
QScrollArea {
    border: none;
    background: #1b1c1f;
}
QScrollArea > QWidget#qt_scrollarea_viewport {
    background: #1b1c1f;
}
QScrollArea > QWidget > QWidget {
    background: transparent;
}
QScrollBar:vertical,
QScrollBar:horizontal {
    background: #161719;
    border: none;
    margin: 0;
}
QScrollBar:vertical { width: 11px; }
QScrollBar:horizontal { height: 11px; }
QScrollBar::handle:vertical,
QScrollBar::handle:horizontal {
    background: #3d4148;
    border-radius: 5px;
    min-height: 24px;
    min-width: 24px;
}
QScrollBar::handle:vertical:hover,
QScrollBar::handle:horizontal:hover {
    background: #5c626b;
}
QScrollBar::add-line,
QScrollBar::sub-line,
QScrollBar::add-page,
QScrollBar::sub-page {
    background: transparent;
    border: none;
    width: 0;
    height: 0;
}
QMenu {
    background: #23262b;
    border: 1px solid #3a3e45;
    padding: 4px;
}
QMenu::item {
    padding: 6px 24px 6px 12px;
    border-radius: 4px;
}
QMenu::item:selected {
    background: #34527d;
}
QMenu::item:disabled {
    color: #6b717a;
}

/* ---- 框选区域对话框使用浅色主题 ---- */
QDialog {
    background: #f4f5f7;
}
QDialog QLabel,
QDialog QCheckBox,
QDialog QRadioButton {
    color: #1b1c1f;
}
QDialog QLabel#muted {
    color: #4b5563;
}
QDialog QLabel#hint {
    color: #4b5563;
}
QDialog QGroupBox {
    background: #ffffff;
    border: 1px solid #d0d4da;
}
QDialog QGroupBox::title {
    color: #1a4f8b;
    background: #f4f5f7;
}
QDialog QLineEdit,
QDialog QComboBox,
QDialog QSpinBox,
QDialog QTimeEdit {
    background: #ffffff;
    color: #1b1c1f;
    border: 1px solid #c3c8cf;
    selection-background-color: #bcd2f2;
    selection-color: #101318;
}
QDialog QLineEdit:focus,
QDialog QComboBox:focus,
QDialog QSpinBox:focus,
QDialog QTimeEdit:focus {
    border-color: #3b82f6;
}
QDialog QComboBox QAbstractItemView {
    background: #ffffff;
    color: #1b1c1f;
    selection-background-color: #bcd2f2;
    selection-color: #101318;
}
QDialog QPushButton {
    background: #e9ebef;
    color: #1b1c1f;
    border: 1px solid #c3c8cf;
}
QDialog QPushButton:hover {
    background: #dfe2e8;
}
QDialog QPushButton:pressed {
    background: #d3d7de;
}
QDialog QPushButton:disabled {
    color: #9aa0a8;
    background: #f0f1f3;
    border-color: #d8dce2;
}
QDialog QCheckBox::indicator {
    background: #ffffff;
    border: 1px solid #9aa0a8;
}
QDialog QCheckBox::indicator:checked {
    background: #3b82f6;
    border-color: #3b82f6;
}
QDialog QScrollArea {
    background: transparent;
}
/* 顶部页标签：白底黑字（原来深色底上灰字看不清） */
QTabWidget#mainTabs > QTabBar {
    background: #ffffff;
}
QTabWidget#mainTabs > QTabBar::tab {
    background: #ffffff;
    color: #000000;
    font-weight: 700;
    font-size: 13px;
    border: 1px solid #9aa0a8;
    border-bottom: none;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    padding: 8px 20px;
    margin-right: 4px;
    min-width: 96px;
}
QTabWidget#mainTabs > QTabBar::tab:selected {
    background: #ffffff;
    color: #000000;
    border-bottom: 3px solid #16a34a;
}
QTabWidget#mainTabs > QTabBar::tab:!selected {
    background: #eceff3;
    color: #000000;
}
QTabWidget#mainTabs > QTabBar::tab:hover {
    background: #f5f7fa;
    color: #000000;
}
"""

START_BUTTON_STYLES = {
    "idle": """
        QPushButton#startButton {
            background: #c0392b;
            color: #ffffff;
            border: 1px solid #d14a3b;
            border-radius: 8px;
            padding: 11px 16px;
            font-size: 15px;
            font-weight: 700;
        }
        QPushButton#startButton:hover { background: #d34a3a; }
        QPushButton#startButton:pressed { background: #a93226; }
    """,
    "running": """
        QPushButton#startButton {
            background: #8e2b20;
            color: #ffffff;
            border: 1px solid #a9382c;
            border-radius: 8px;
            padding: 11px 16px;
            font-size: 15px;
            font-weight: 700;
        }
        QPushButton#startButton:hover { background: #a3352a; }
        QPushButton#startButton:pressed { background: #77231a; }
    """,
    "stopping": """
        QPushButton#startButton {
            background: #4a2b28;
            color: #b9a6a3;
            border: 1px solid #5b3531;
            border-radius: 8px;
            padding: 11px 16px;
            font-size: 15px;
            font-weight: 700;
        }
    """,
}


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    """UI 主窗口。工作线程 KeySender 与界面逻辑分离。"""

    # keyboard 库的回调在它自己的线程中执行，通过 Qt Signal 安全地切回 UI 线程。
    hotkey_triggered = Signal()

    def __init__(self) -> None:
        super().__init__()

        self.target_hwnd: Optional[int] = None
        self.target_title: str = ""

        self._worker: Optional[KeySender] = None
        self._stopping = False
        self._quitting = False
        self._cleaned = False
        self._last_failure: Optional[str] = None
        self._last_rounds = 0
        self._tray_hint_shown = False

        self._hotkey_handle = None
        self._local_shortcut: Optional[QShortcut] = None
        self._tray: Optional[QSystemTrayIcon] = None
        self._act_show: Optional[QAction] = None
        self._act_toggle: Optional[QAction] = None
        self._act_quit: Optional[QAction] = None

        self._fatigue_monitor: Optional[FatigueMonitor] = None
        self._test_monitor: Optional[FatigueMonitor] = None
        self._single_hp_monitor: Optional[HpMonitor] = None
        self._fatigue_roi_norm: Optional[tuple[float, float, float, float]] = (
            load_fatigue_roi_norm()
        )
        self._settings = QSettings("CreatureCurios", "CreatureCuriosKeyer")
        global _FOREGROUND_BRIDGE
        self._foreground_bridge = ForegroundBridge(self)
        _FOREGROUND_BRIDGE = self._foreground_bridge
        try:
            threshold = int(
                self._settings.value(
                    "fatigue_threshold", DEFAULT_FATIGUE_THRESHOLD
                )
            )
        except Exception:
            threshold = DEFAULT_FATIGUE_THRESHOLD
        self._fatigue_threshold = max(50, min(1000, threshold))
        # 疲劳检测默认只用后台点击；“借用鼠标”是用户显式开启的兜底
        try:
            self._fatigue_borrow_mouse = self._bool_setting(
                self._settings.value("fatigue_borrow_mouse", False)
            )
        except Exception:
            self._fatigue_borrow_mouse = False
        self._timer_next_mono: Optional[float] = None
        self._timer_next_dt: Optional[datetime.datetime] = None

        # ---- 自动打BOSS ----
        self._boss_records = load_boss_records()
        self._boss_recorder: Optional[MacroRecorder] = None
        self._boss_record_pending = False
        self._boss_player: Optional[MacroPlayer] = None
        self._boss_fighter: Optional[KeySender] = None
        self._boss_watcher: Optional[BossWatcher] = None
        self._boss_looter: Optional[TapSender] = None
        self._boss_running = False
        self._boss_preview = False
        self._boss_preview_resume = False
        self._boss_kind = "replay"
        self._boss_done = 0
        self._boss_total = 1
        self._boss_interval = 0.0
        self._boss_resume_farm = False
        self._boss_origin = "manual"
        self._boss_stage = ""
        self._boss_detect_roi_norm: Optional[
            tuple[float, float, float, float]
        ] = None
        self._boss_last_error = ""
        self._boss_loading = False

        # ---- 多开 / 血量 / 角色名 ----
        self._heal_records = load_heal_records()
        self._hp_roi_norm: Optional[tuple[float, float, float, float]] = None
        self._name_roi_norm: Optional[tuple[float, float, float, float]] = None
        self._heal_recorder: Optional[MacroRecorder] = None
        self._heal_record_pending = False
        self._heal_record_index: Optional[int] = None
        self._heal_test_player: Optional[MacroPlayer] = None
        self._heal_test_index: Optional[int] = None
        self._name_readers: list[Optional[NameReader]] = [None, None, None, None]
        self._multi_runners: list[Optional[SlotRunner]] = [None, None, None, None]
        self._multi_state_text = "未运行"
        self._multi_loading = False
        self._multi_saved_hwnds: list[Optional[int]] = [None, None, None, None]

        self._boss_gap_timer = QTimer(self)
        self._boss_gap_timer.setSingleShot(True)
        self._boss_gap_timer.timeout.connect(self._boss_run_iteration)
        self._boss_fight_timer = QTimer(self)
        self._boss_fight_timer.setSingleShot(True)
        self._boss_fight_timer.timeout.connect(self._boss_detect_on_timeout)
        self._boss_trigger_timer = QTimer(self)
        self._boss_trigger_timer.setSingleShot(True)
        self._boss_trigger_timer.timeout.connect(self._on_boss_timer_fire)

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(self._make_icon())
        self.setMinimumSize(820, 700)
        self.resize(880, 760)

        self._build_ui()
        self._refresh_boss_records()
        self._load_boss_settings()
        self._refresh_multi_records()
        self._load_multi_settings()
        self._refresh_fatigue_roi_label()
        self._refresh_threshold_labels()
        self._update_boss_controls()
        self._schedule_boss_timer()
        self._popup = NoticePopup()
        self._setup_tray()
        self._install_hotkey()

        # 定时器提醒 / 倒计时显示，每秒检查一次（窗口隐藏到托盘后仍然有效）
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._on_ui_tick)
        self._ui_timer.start(1000)
        self._update_timer_mode_ui()
        self._reset_timer_state()

        # 启动后自动扫描一次窗口，省得用户第一件事就是点刷新。
        QTimer.singleShot(150, self.refresh_windows)

    # -- UI 构建 ----------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 10)
        root.setSpacing(12)

        # 顶部标题
        header = QHBoxLayout()
        header.setSpacing(12)

        icon_label = QLabel()
        icon_label.setPixmap(self._make_icon().pixmap(44, 44))
        icon_label.setFixedSize(44, 44)
        header.addWidget(icon_label)

        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        title = QLabel("CreatureCurios 后台按键器")
        title.setObjectName("appTitle")
        subtitle = QLabel("单开/多开挂机 · 自动打BOSS · 角色名识别 · 后台 PostMessage")
        subtitle.setObjectName("subtitle")
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header.addLayout(title_col)
        header.addStretch(1)

        badge = QLabel("v3.6")
        badge.setObjectName("versionBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(48, 24)
        header.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)

        root.addLayout(header)

        container = QWidget()
        basic_root = QVBoxLayout(container)
        basic_root.setContentsMargins(12, 12, 12, 12)
        basic_root.setSpacing(10)

        # 1. 绑定目标窗口
        box_target = QGroupBox("1 · 绑定目标窗口")
        target_layout = QVBoxLayout(box_target)
        target_layout.setSpacing(8)

        select_row = QHBoxLayout()
        select_row.setSpacing(8)

        self.combo_windows = QComboBox()
        self.combo_windows.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.combo_windows.setMinimumWidth(360)
        self.combo_windows.setToolTip(
            "列出所有标题包含 CreatureCurios 的可见窗口，格式：标题 [HWND]"
        )
        self.combo_windows.addItem("点击右侧“刷新”扫描 CreatureCurios 窗口", None)
        self.combo_windows.currentIndexChanged.connect(self._update_bind_button_state)

        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setToolTip("枚举所有可见窗口并筛选 CreatureCurios")
        self.btn_refresh.clicked.connect(self.refresh_windows)

        self.btn_bind = QPushButton("绑定")
        self.btn_bind.setToolTip("将当前选中的 HWND 设为发送目标")
        self.btn_bind.clicked.connect(self._bind_selected_window)

        select_row.addWidget(self.combo_windows, 1)
        select_row.addWidget(self.btn_refresh)
        select_row.addWidget(self.btn_bind)
        target_layout.addLayout(select_row)

        self.bound_label = QLabel("尚未绑定窗口")
        self.bound_label.setObjectName("boundLabel")
        self.bound_label.setProperty("state", "idle")
        self.bound_label.setWordWrap(True)
        target_layout.addWidget(self.bound_label)

        basic_root.addWidget(box_target)

        # 2. 动作参数
        box_params = QGroupBox("2 · 动作参数")
        params_layout = QVBoxLayout(box_params)
        params_layout.setSpacing(10)

        interval_row = QHBoxLayout()
        interval_row.setSpacing(8)
        interval_label = QLabel("每轮间隔")
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(MIN_INTERVAL_MS, 3_600_000)
        self.spin_interval.setSingleStep(50)
        self.spin_interval.setValue(DEFAULT_INTERVAL_MS)
        self.spin_interval.setSuffix(" ms")
        self.spin_interval.setFixedWidth(150)
        self.spin_interval.setToolTip("每完成一轮动作后等待的毫秒数")
        self.spin_interval.valueChanged.connect(self._on_interval_changed)

        interval_hint = QLabel("毫秒（最小 50）")
        interval_hint.setObjectName("muted")

        interval_row.addWidget(interval_label)
        interval_row.addWidget(self.spin_interval)
        interval_row.addWidget(interval_hint)
        interval_row.addStretch(1)
        params_layout.addLayout(interval_row)

        self.lbl_action_text = QLabel("")
        self.lbl_action_text.setObjectName("actionText")
        self.lbl_action_text.setWordWrap(True)
        params_layout.addWidget(self.lbl_action_text)

        pickup_row = QHBoxLayout()
        pickup_row.setSpacing(8)
        self.chk_pickup = QCheckBox("拾取物品（A 后按空格）")
        self.chk_pickup.setChecked(True)
        self.chk_pickup.setToolTip(
            "勾选：每轮 A 之后按空格拾取；取消：只按 A，不发送空格"
        )
        self.chk_pickup.toggled.connect(self._on_pickup_toggled)
        pickup_row.addWidget(self.chk_pickup)
        pickup_row.addStretch(1)
        params_layout.addLayout(pickup_row)

        self._refresh_action_text()

        self.btn_start = QPushButton("开始发送")
        self.btn_start.setObjectName("startButton")
        self.btn_start.setMinimumHeight(46)
        self.btn_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_start.setStyleSheet(START_BUTTON_STYLES["idle"])
        self.btn_start.clicked.connect(self._toggle_running)
        params_layout.addWidget(self.btn_start)

        basic_root.addWidget(box_params)

        # 3. 辅助功能
        box_aux = QGroupBox("3 · 辅助功能（可选）")
        aux = QGridLayout(box_aux)
        aux.setHorizontalSpacing(10)
        aux.setVerticalSpacing(8)
        aux.setColumnStretch(1, 1)

        self.chk_skills = QCheckBox("使用技能 1 / 2")
        self.chk_skills.setToolTip("开启后按冷却时间自动发送数字键 1 和 2")
        self.chk_skills.toggled.connect(self._on_skills_toggled)
        self.lbl_skills = QLabel("1 冷却 12 秒 · 2 冷却 6 秒")
        self.lbl_skills.setObjectName("muted")
        aux.addWidget(self.chk_skills, 0, 0)
        aux.addWidget(self.lbl_skills, 0, 1, 1, 3)

        self.chk_potion = QCheckBox("自动喝药 Q")
        self.chk_potion.setToolTip("开启后按设定间隔自动发送 Q")
        self.chk_potion.toggled.connect(self._on_potion_toggled)
        self.spin_potion = QSpinBox()
        self.spin_potion.setRange(5, 3600)
        self.spin_potion.setSingleStep(5)
        self.spin_potion.setValue(DEFAULT_POTION_SECONDS)
        self.spin_potion.setSuffix(" 秒")
        self.spin_potion.setFixedWidth(110)
        self.spin_potion.setToolTip("喝药间隔，默认 60 秒（1 分钟）")
        self.spin_potion.valueChanged.connect(self._on_potion_interval_changed)
        self.lbl_potion = QLabel("默认 60 秒（1 分钟）")
        self.lbl_potion.setObjectName("muted")
        aux.addWidget(self.chk_potion, 1, 0)
        aux.addWidget(QLabel("间隔"), 1, 1)
        aux.addWidget(self.spin_potion, 1, 2)
        aux.addWidget(self.lbl_potion, 1, 3)

        self.chk_fatigue = QCheckBox("疲劳检测（每 30 秒）")
        self.chk_fatigue.setToolTip(
            "开始发送后每 30 秒检测一次；检测时用 PostMessage 后台点击你框选的区域，"
            "不移动真实鼠标、不抢前台，读取弹出的“当前疲劳值:xxx/xxx”。"
        )
        self.chk_fatigue.toggled.connect(self._on_fatigue_toggled)
        self.lbl_fatigue = QLabel("上次: --")
        self.lbl_fatigue.setObjectName("muted")
        self.lbl_fatigue.setMinimumWidth(210)
        self.btn_fatigue_region = QPushButton("框选区域")
        self.btn_fatigue_region.setFixedWidth(92)
        self.btn_fatigue_region.setToolTip(
            "在游戏截图上框选右下角的疲劳提示按钮/图标区域；区域会保存"
        )
        self.btn_fatigue_region.clicked.connect(self._select_fatigue_region)
        self.btn_fatigue_test = QPushButton("测试检测")
        self.btn_fatigue_test.setFixedWidth(92)
        self.btn_fatigue_test.setToolTip(
            "立即执行一次疲劳检测（后台点击，不移动鼠标）"
        )
        self.btn_fatigue_test.clicked.connect(self._test_fatigue_check)
        aux.addWidget(self.chk_fatigue, 2, 0)
        aux.addWidget(self.lbl_fatigue, 2, 1, 1, 2)
        aux.addWidget(self.btn_fatigue_test, 2, 3)
        aux.addWidget(self.btn_fatigue_region, 2, 4)

        self.spin_fatigue_threshold = QSpinBox()
        self.spin_fatigue_threshold.setRange(50, 1000)
        self.spin_fatigue_threshold.setSingleStep(10)
        self.spin_fatigue_threshold.setValue(int(self._fatigue_threshold))
        self.spin_fatigue_threshold.setSuffix(" 点")
        self.spin_fatigue_threshold.setFixedWidth(110)
        self.spin_fatigue_threshold.setToolTip(
            "疲劳阈值，例如 800 / 600 / 500；低于 -阈值 或高于 +阈值 时弹窗。"
        )
        self.spin_fatigue_threshold.valueChanged.connect(
            self._on_threshold_changed
        )
        self.lbl_threshold_hint = QLabel("")
        self.lbl_threshold_hint.setObjectName("muted")
        self.lbl_threshold_hint.setWordWrap(True)
        aux.addWidget(QLabel("疲劳阈值 ±"), 3, 0)
        aux.addWidget(self.spin_fatigue_threshold, 3, 1)
        aux.addWidget(self.lbl_threshold_hint, 3, 2, 1, 3)

        self.chk_fatigue_borrow_mouse = QCheckBox(
            "后台点击读不到时短暂借用鼠标（会闪一下光标）"
        )
        self.chk_fatigue_borrow_mouse.setChecked(self._fatigue_borrow_mouse)
        self.chk_fatigue_borrow_mouse.setToolTip(
            "默认关闭：疲劳检测只用 PostMessage 后台点击，绝不移动鼠标。\n"
            "如果游戏不处理后鼠标消息、读不到疲劳值，可勾选此项：\n"
            "后台点击失败时才切前台真实点一下（约 1 秒），随后立刻还原光标和窗口。"
        )
        self.chk_fatigue_borrow_mouse.toggled.connect(
            self._on_fatigue_borrow_toggled
        )
        aux.addWidget(self.chk_fatigue_borrow_mouse, 4, 0, 1, 4)

        self.chk_timer = QCheckBox("定时提醒")
        self.chk_timer.setToolTip("开启后按倒计时或指定时刻弹出提醒")
        self.chk_timer.toggled.connect(self._reset_timer_state)
        self.combo_timer_mode = QComboBox()
        self.combo_timer_mode.addItem("倒计时", "countdown")
        self.combo_timer_mode.addItem("指定时刻", "clock")
        self.combo_timer_mode.setFixedWidth(96)
        self.combo_timer_mode.currentIndexChanged.connect(self._on_timer_mode_changed)
        self.spin_timer_min = QSpinBox()
        self.spin_timer_min.setRange(1, 1440)
        self.spin_timer_min.setValue(30)
        self.spin_timer_min.setSuffix(" 分钟")
        self.spin_timer_min.setFixedWidth(110)
        self.spin_timer_min.valueChanged.connect(self._reset_timer_state)
        self.time_timer = QTimeEdit()
        self.time_timer.setDisplayFormat("HH:mm")
        self.time_timer.setTime(QTime.currentTime().addSecs(3600))
        self.time_timer.setFixedWidth(90)
        self.time_timer.timeChanged.connect(self._reset_timer_state)
        self.lbl_timer_next = QLabel("未启用")
        self.lbl_timer_next.setObjectName("muted")
        self.lbl_timer_next.setMinimumWidth(150)
        self.btn_notice_test = QPushButton("测试提醒")
        self.btn_notice_test.setFixedWidth(92)
        self.btn_notice_test.clicked.connect(self._show_test_notice)

        timer_wrap = QWidget()
        timer_row = QHBoxLayout(timer_wrap)
        timer_row.setContentsMargins(0, 0, 0, 0)
        timer_row.setSpacing(8)
        timer_row.addWidget(self.combo_timer_mode)
        timer_row.addWidget(self.spin_timer_min)
        timer_row.addWidget(self.time_timer)
        timer_row.addWidget(self.lbl_timer_next, 1)
        timer_row.addWidget(self.btn_notice_test)
        aux.addWidget(self.chk_timer, 5, 0)
        aux.addWidget(timer_wrap, 5, 1, 1, 3)

        self.chk_background_mode = QCheckBox("后台模式（不抢鼠标键盘）")
        self.chk_background_mode.setChecked(True)
        self.chk_background_mode.setToolTip(
            "BOSS/导航/返回回放都改用 PostMessage 发送，不移动真实鼠标、不抢前台，"
            "可以边挂机边用电脑（疲劳检测始终用后台点击，不受本开关影响）。"
            "部分游戏不处理后鼠标消息，建议先“试放一次”验证；"
            "无效时取消勾选，会恢复前台真实鼠标键盘回放。"
        )
        self.chk_background_mode.toggled.connect(
            self._on_background_mode_toggled
        )
        aux.addWidget(self.chk_background_mode, 6, 0, 1, 2)

        self.chk_relative_mouse = QCheckBox("前台相对鼠标（会抢鼠标）")
        self.chk_relative_mouse.setChecked(False)
        self.chk_relative_mouse.setToolTip(
            "镜头更准，但会移动真实鼠标并占用前台，无法边用电脑边挂机。"
            "需要边学习边挂机请用后台模式；后台模式不抢鼠标，"
            "但游戏可能不处理后鼠标消息。"
        )
        self.chk_relative_mouse.toggled.connect(
            self._on_relative_mouse_toggled
        )
        aux.addWidget(self.chk_relative_mouse, 6, 2, 1, 2)

        self.chk_keyboard_only = QCheckBox(
            "静止鼠标 / 仅键盘（防抢）"
        )
        self.chk_keyboard_only.setChecked(False)
        self.chk_keyboard_only.setToolTip(
            "只回放键盘事件，完全跳过鼠标移动/点击，不移动真实鼠标、"
            "不抢前台；游戏若自己抢到前台，会尝试把前台还给用户窗口。"
            "适合不需要鼠标的录制；需要鼠标菜单/转镜头的录制请用其它模式。"
        )
        self.chk_keyboard_only.toggled.connect(
            self._on_keyboard_only_toggled
        )
        aux.addWidget(self.chk_keyboard_only, 7, 0, 1, 4)

        self.chk_single_hp_potion = QCheckBox(
            "血量低于阈值自动吃药（Q+W）"
        )
        self.chk_single_hp_potion.setToolTip(
            "启用后不再按固定间隔喝药；血量低于阈值时，"
            "按「喝药间隔」持续发送 Q 和 W，直到血量高于阈值。"
            "使用下方框选的血量区域。"
        )
        self.chk_single_hp_potion.toggled.connect(
            self._on_single_hp_potion_toggled
        )
        aux.addWidget(self.chk_single_hp_potion, 8, 0, 1, 2)
        aux.addWidget(QLabel("血量阈值"), 8, 2)
        self.spin_single_hp_potion_threshold = QSpinBox()
        self.spin_single_hp_potion_threshold.setRange(1, 100000)
        self.spin_single_hp_potion_threshold.setValue(1600)
        self.spin_single_hp_potion_threshold.setSingleStep(10)
        self.spin_single_hp_potion_threshold.setSuffix(" 点")
        self.spin_single_hp_potion_threshold.setFixedWidth(110)
        self.spin_single_hp_potion_threshold.valueChanged.connect(
            lambda _=0: self._save_boss_settings()
        )
        aux.addWidget(self.spin_single_hp_potion_threshold, 8, 3)

        self.btn_single_hp_roi = QPushButton("框选血量区域")
        self.btn_single_hp_roi.setFixedWidth(110)
        self.btn_single_hp_roi.clicked.connect(self._select_hp_roi)
        self.lbl_single_hp_roi = QLabel("未设置")
        self.lbl_single_hp_roi.setObjectName("muted")
        self.lbl_single_hp_value = QLabel("血量: --")
        self.lbl_single_hp_value.setObjectName("muted")
        aux.addWidget(self.btn_single_hp_roi, 9, 0)
        aux.addWidget(self.lbl_single_hp_roi, 9, 1)
        aux.addWidget(self.lbl_single_hp_value, 9, 2, 1, 2)

        aux_note = QLabel(
            "技能 / 喝药在“开始发送”后生效；疲劳检测只用后台 PostMessage 点击，不移动鼠标、不抢前台；"
            "定时提醒独立生效，最小化到托盘也会弹窗。"
            "游戏窗口最小化时只继续后台按键，疲劳检测会暂停。"
        )
        aux_note.setObjectName("muted")
        aux_note.setWordWrap(True)
        aux.addWidget(aux_note, 10, 0, 1, 4)

        basic_root.addWidget(box_aux)
        basic_root.addWidget(self._build_boss_group())
        basic_root.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(container)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self.tabs.addTab(scroll, "单开挂机")
        self.tabs.addTab(self._build_multi_tab(), "多开挂机")
        try:
            from dungeon_ui import DungeonTab

            self.dungeon_tab = DungeonTab(self)
            self.tabs.addTab(self.dungeon_tab, "副本流程")
            self._log_startup(
                f"副本流程页已加载（tab 序号 {self.tabs.indexOf(self.dungeon_tab)}）"
            )
        except Exception as exc:  # 副本页加载失败不影响主流程
            self.dungeon_tab = None
            import traceback

            detail = traceback.format_exc()
            self._log_startup(
                f"副本流程页加载失败：{type(exc).__name__}: {exc}"
                + chr(10) + detail
            )
            print(f"[警告] 副本流程页加载失败：{type(exc).__name__}: {exc}")
        root.addWidget(self.tabs, 1)

        self.hint_label = QLabel(
            f"快捷键: {HOTKEY_TEXT} 开始/停止；最小化后驻留系统托盘。"
        )
        self.hint_label.setObjectName("hint")
        self.hint_label.setWordWrap(True)
        root.addWidget(self.hint_label)

        # 底部状态栏
        status_bar = QStatusBar()
        status_bar.setSizeGripEnabled(False)
        self.status_label = QLabel("就绪：点击“刷新”，选择窗口并绑定。")
        self.status_label.setObjectName("statusLabel")
        self.rounds_label = QLabel("已发送 0 轮")
        self.rounds_label.setObjectName("roundsLabel")
        status_bar.addWidget(self.status_label, 1)
        status_bar.addPermanentWidget(self.rounds_label)
        self.setStatusBar(status_bar)

    # -- 自动打BOSS：界面 ------------------------------------------------
    def _build_boss_group(self) -> QWidget:
        box = QGroupBox("4 · 自动打BOSS")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)

        self.chk_boss_fatigue = QCheckBox("疲劳低于阈值时自动打BOSS")
        self.chk_boss_fatigue.setToolTip(
            "需要先勾选基础页的“疲劳检测”。疲劳低于 -阈值 时，"
            "暂停打怪并执行一次 / 一轮 BOSS 方案，完成后自动恢复打怪。"
        )
        grid.addWidget(self.chk_boss_fatigue, 0, 0, 1, 2)

        timer_wrap = QWidget()
        timer_row = QHBoxLayout(timer_wrap)
        timer_row.setContentsMargins(0, 0, 0, 0)
        timer_row.setSpacing(8)
        self.chk_boss_timer = QCheckBox("定时打BOSS")
        self.spin_boss_timer = QSpinBox()
        self.spin_boss_timer.setRange(1, 1440)
        self.spin_boss_timer.setValue(60)
        self.spin_boss_timer.setSuffix(" 分钟")
        self.spin_boss_timer.setFixedWidth(110)
        self.spin_boss_timer.setToolTip("每隔多少分钟执行一次 BOSS 方案")
        self.chk_boss_timer.toggled.connect(self._on_boss_timer_toggled)
        self.spin_boss_timer.valueChanged.connect(self._on_boss_timer_toggled)
        timer_row.addWidget(self.chk_boss_timer)
        timer_row.addWidget(self.spin_boss_timer)
        timer_row.addStretch(1)
        grid.addWidget(timer_wrap, 0, 2, 1, 2)

        # 录制管理 / 整段录制
        rec_wrap = QWidget()
        rec_row = QHBoxLayout(rec_wrap)
        rec_row.setContentsMargins(0, 0, 0, 0)
        rec_row.setSpacing(8)
        self.combo_boss_record = QComboBox()
        self.combo_boss_record.setMinimumWidth(240)
        self.combo_boss_record.setToolTip(
            "整段录制回放使用这里选中的录制；也可以试放 / 删除"
        )
        self.btn_boss_record = QPushButton("录制新脚本")
        self.btn_boss_record.setToolTip(
            "3 秒内切到游戏，完整录制一遍打BOSS流程，"
            "切回本窗口再点一次停止并保存"
        )
        self.btn_boss_record.clicked.connect(self._toggle_boss_record)
        self.btn_boss_test = QPushButton("试放")
        self.btn_boss_test.clicked.connect(self._test_boss_record)
        self.btn_boss_delete = QPushButton("删除")
        self.btn_boss_delete.clicked.connect(self._delete_boss_record)
        rec_row.addWidget(self.combo_boss_record, 1)
        rec_row.addWidget(self.btn_boss_record)
        rec_row.addWidget(self.btn_boss_test)
        rec_row.addWidget(self.btn_boss_delete)
        grid.addWidget(QLabel("录制"), 1, 0)
        grid.addWidget(rec_wrap, 1, 1, 1, 3)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        mode_row.addWidget(QLabel("打BOSS方式"))
        self.combo_boss_mode = QComboBox()
        self.combo_boss_mode.addItem("整段录制回放", "replay")
        self.combo_boss_mode.addItem("识别战斗（导航+自动打+返回）", "detect")
        self.combo_boss_mode.setMinimumWidth(240)
        self.combo_boss_mode.currentIndexChanged.connect(
            self._on_boss_mode_changed
        )
        mode_row.addWidget(self.combo_boss_mode)
        mode_row.addWidget(
            QLabel("整段回放：录一遍完整流程；识别战斗：录导航/返回，战斗自动识别"),
            1,
        )
        grid.addLayout(mode_row, 2, 0, 1, 4)

        # ---- 整段录制回放 ----
        self._boss_replay_panel = QWidget()
        rp = QGridLayout(self._boss_replay_panel)
        rp.setContentsMargins(0, 0, 0, 0)
        rp.setHorizontalSpacing(10)
        rp.setVerticalSpacing(8)
        rp.setColumnStretch(1, 1)
        rp.addWidget(QLabel("回放速度"), 0, 0)
        self.spin_boss_speed = QSpinBox()
        self.spin_boss_speed.setRange(50, 200)
        self.spin_boss_speed.setSingleStep(10)
        self.spin_boss_speed.setValue(100)
        self.spin_boss_speed.setSuffix(" %")
        self.spin_boss_speed.setFixedWidth(100)
        rp.addWidget(self.spin_boss_speed, 0, 1)
        rp.addWidget(QLabel("重复"), 0, 2)
        self.spin_boss_repeat = QSpinBox()
        self.spin_boss_repeat.setRange(0, 9999)
        self.spin_boss_repeat.setValue(1)
        self.spin_boss_repeat.setSuffix(" 次")
        self.spin_boss_repeat.setFixedWidth(100)
        self.spin_boss_repeat.setToolTip("0 = 无限循环")
        rp.addWidget(self.spin_boss_repeat, 0, 3)
        rp.addWidget(QLabel("每轮间隔"), 0, 4)
        self.spin_boss_gap = QSpinBox()
        self.spin_boss_gap.setRange(0, 3600)
        self.spin_boss_gap.setValue(5)
        self.spin_boss_gap.setSuffix(" 秒")
        self.spin_boss_gap.setFixedWidth(100)
        rp.addWidget(self.spin_boss_gap, 0, 5)
        rp.addWidget(
            QLabel(
                "回放会切到游戏前台并接管鼠标键盘；"
                "按 Ctrl+Alt+F8 或“停止BOSS”可随时停止"
            ),
            1, 0, 1, 6,
        )
        grid.addWidget(self._boss_replay_panel, 3, 0, 1, 4)

        # ---- 识别战斗 ----
        self._boss_detect_panel = QWidget()
        dp = QGridLayout(self._boss_detect_panel)
        dp.setContentsMargins(0, 0, 0, 0)
        dp.setHorizontalSpacing(10)
        dp.setVerticalSpacing(8)
        dp.setColumnStretch(1, 1)

        dp.addWidget(QLabel("前往BOSS录制"), 0, 0)
        self.combo_boss_approach = QComboBox()
        self.combo_boss_approach.setMinimumWidth(180)
        self.combo_boss_approach.setToolTip(
            "可选：从打怪点走到 BOSS 的录制；留空表示已经站在 BOSS 前"
        )
        dp.addWidget(self.combo_boss_approach, 0, 1)
        dp.addWidget(QLabel("返回打怪点录制"), 0, 2)
        self.combo_boss_return = QComboBox()
        self.combo_boss_return.setMinimumWidth(180)
        self.combo_boss_return.setToolTip(
            "可选：击杀后拾取/返回打怪点的录制；留空表示留在原地"
        )
        dp.addWidget(self.combo_boss_return, 0, 3)

        dp.addWidget(QLabel("BOSS检测区域"), 1, 0)
        roi_wrap = QWidget()
        roi_row = QHBoxLayout(roi_wrap)
        roi_row.setContentsMargins(0, 0, 0, 0)
        roi_row.setSpacing(8)
        self.btn_boss_detect_roi = QPushButton("框选区域")
        self.btn_boss_detect_roi.setFixedWidth(92)
        self.btn_boss_detect_roi.clicked.connect(
            self._select_boss_detect_region
        )
        self.lbl_boss_detect_roi = QLabel("未设置")
        self.lbl_boss_detect_roi.setObjectName("muted")
        roi_row.addWidget(self.btn_boss_detect_roi)
        roi_row.addWidget(self.lbl_boss_detect_roi, 1)
        dp.addWidget(roi_wrap, 1, 1)
        dp.addWidget(QLabel("检测方式"), 1, 2)
        self.combo_boss_detect_mode = QComboBox()
        self.combo_boss_detect_mode.addItem("血量数字", "hp")
        self.combo_boss_detect_mode.addItem("BOSS 文字/名称", "text")
        self.combo_boss_detect_mode.setFixedWidth(150)
        dp.addWidget(self.combo_boss_detect_mode, 1, 3)

        dp.addWidget(QLabel("击杀血量 ≤"), 2, 0)
        self.spin_boss_kill_hp = QSpinBox()
        self.spin_boss_kill_hp.setRange(0, 9_999_999)
        self.spin_boss_kill_hp.setValue(0)
        self.spin_boss_kill_hp.setFixedWidth(110)
        self.spin_boss_kill_hp.setToolTip(
            "血条识别时：当前血量小于等于这个值就认为 BOSS 已死亡"
        )
        dp.addWidget(self.spin_boss_kill_hp, 2, 1)
        dp.addWidget(QLabel("连续未识别"), 2, 2)
        miss_wrap = QWidget()
        miss_row = QHBoxLayout(miss_wrap)
        miss_row.setContentsMargins(0, 0, 0, 0)
        miss_row.setSpacing(6)
        self.spin_boss_miss = QSpinBox()
        self.spin_boss_miss.setRange(1, 10)
        self.spin_boss_miss.setValue(2)
        self.spin_boss_miss.setFixedWidth(60)
        self.spin_boss_miss.setToolTip(
            "连续多少次识别不到血量/文字，就认为 BOSS 已死亡"
        )
        miss_row.addWidget(self.spin_boss_miss)
        miss_row.addWidget(QLabel("次"))
        miss_row.addWidget(QLabel("战斗上限"))
        self.spin_boss_max_fight = QSpinBox()
        self.spin_boss_max_fight.setRange(10, 3600)
        self.spin_boss_max_fight.setValue(180)
        self.spin_boss_max_fight.setSuffix(" 秒")
        self.spin_boss_max_fight.setFixedWidth(100)
        miss_row.addWidget(self.spin_boss_max_fight)
        miss_row.addWidget(QLabel("拾取"))
        self.spin_boss_pickup = QSpinBox()
        self.spin_boss_pickup.setRange(0, 50)
        self.spin_boss_pickup.setValue(3)
        self.spin_boss_pickup.setSuffix(" 次")
        self.spin_boss_pickup.setFixedWidth(90)
        miss_row.addWidget(self.spin_boss_pickup)
        miss_row.addWidget(QLabel("空格"))
        miss_row.addStretch(1)
        dp.addWidget(miss_wrap, 2, 3)

        dp.addWidget(
            QLabel(
                "战斗沿用基础页的 A / 空格 / 技能 / 喝药设置；"
                "识别不到时可在状态栏查看 OCR 结果，或改用“文字/名称”方式"
            ),
            3, 0, 1, 4,
        )
        grid.addWidget(self._boss_detect_panel, 3, 0, 1, 4)

        # ---- 操作按钮 ----
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_boss_once = QPushButton("打一次BOSS")
        self.btn_boss_once.clicked.connect(self._boss_start_once)
        self.btn_boss_loop = QPushButton("开始BOSS循环")
        self.btn_boss_loop.clicked.connect(self._boss_start_loop)
        self.btn_boss_stop = QPushButton("停止BOSS")
        self.btn_boss_stop.clicked.connect(self._boss_stop_clicked)
        self.lbl_boss_state = QLabel("未运行")
        self.lbl_boss_state.setObjectName("muted")
        self.lbl_boss_state.setWordWrap(True)
        btn_row.addWidget(self.btn_boss_once)
        btn_row.addWidget(self.btn_boss_loop)
        btn_row.addWidget(self.btn_boss_stop)
        btn_row.addWidget(self.lbl_boss_state, 1)
        grid.addLayout(btn_row, 4, 0, 1, 4)

        return box

    # -- 自动打BOSS：设置与状态 -----------------------------------------
    def _boss_busy(self) -> bool:
        return bool(
            self._boss_running
            or self._boss_recorder is not None
            or self._boss_record_pending
            or self._boss_player is not None
            or self._boss_fighter is not None
            or self._boss_watcher is not None
            or self._boss_looter is not None
        )

    @staticmethod
    def _record_move_count(record: Optional[dict]) -> int:
        if not record:
            return 0
        return sum(
            1 for event in (record.get("events") or [])
            if event.get("type") == "move"
        )

    def _boss_record_by_name(self, name) -> Optional[dict]:
        if not name:
            return None
        for record in self._boss_records:
            if record.get("name") == name:
                return record
        return None

    def _combo_record(self, combo: QComboBox) -> Optional[dict]:
        return self._boss_record_by_name(combo.currentData())

    @staticmethod
    def _fill_record_combo(
        combo: QComboBox, records: list[dict], include_none: bool
    ) -> None:
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        if include_none:
            combo.addItem("（不使用）", None)
        for record in records:
            combo.addItem(str(record.get("name") or ""), record.get("name"))
        index = combo.findData(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _refresh_boss_records(self) -> None:
        self._fill_record_combo(self.combo_boss_record, self._boss_records, False)
        self._fill_record_combo(
            self.combo_boss_approach, self._boss_records, True
        )
        self._fill_record_combo(
            self.combo_boss_return, self._boss_records, True
        )
        self._update_boss_controls()
        if hasattr(self, "combo_multi_records"):
            self._refresh_multi_records()

    def _on_boss_mode_changed(self, _index: int) -> None:
        if self._boss_loading:
            return
        self._update_boss_panels()
        self._update_boss_controls()
        self._save_boss_settings()

    def _update_boss_panels(self) -> None:
        if not hasattr(self, "_boss_replay_panel"):
            return
        mode = self.combo_boss_mode.currentData()
        replay = mode != "detect"
        self._boss_replay_panel.setVisible(replay)
        self._boss_detect_panel.setVisible(not replay)
        self._boss_kind = "replay" if replay else "detect"

    def _update_boss_controls(self) -> None:
        if not hasattr(self, "btn_boss_record"):
            return
        busy = self._boss_busy() or self._multi_busy()
        recording = self._boss_recorder is not None
        pending = self._boss_record_pending
        farm_running = self._worker is not None

        self.btn_boss_record.setText(
            "停止录制"
            if recording
            else ("取消录制" if pending else "录制新脚本")
        )
        self.btn_boss_record.setEnabled(
            recording or pending or (not busy and not farm_running)
        )
        has_record = self.combo_boss_record.currentData() is not None
        self.btn_boss_test.setEnabled(has_record and not busy)
        self.btn_boss_delete.setEnabled(has_record and not busy)

        for widget in (
            self.chk_boss_fatigue,
            self.chk_boss_timer,
            self.spin_boss_timer,
            self.combo_boss_mode,
            self.combo_boss_record,
            self.combo_boss_approach,
            self.combo_boss_return,
            self.combo_boss_detect_mode,
            self.spin_boss_speed,
            self.spin_boss_repeat,
            self.spin_boss_gap,
            self.spin_boss_kill_hp,
            self.spin_boss_miss,
            self.spin_boss_max_fight,
            self.spin_boss_pickup,
            self.btn_boss_detect_roi,
        ):
            widget.setEnabled(not busy)

        self.btn_boss_once.setEnabled(not busy)
        self.btn_boss_loop.setEnabled(not busy)
        self.btn_boss_stop.setEnabled(busy)
        self._update_boss_panels()
        self._update_boss_roi_label()
        self._update_bind_button_state()

    def _update_boss_roi_label(self) -> None:
        if not hasattr(self, "lbl_boss_detect_roi"):
            return
        if self._boss_detect_roi_norm is None:
            self.lbl_boss_detect_roi.setText("未设置")
            self.lbl_boss_detect_roi.setStyleSheet(
                "color: #ffb454; padding: 0;"
            )
            self.btn_boss_detect_roi.setText("框选区域")
            return
        self.btn_boss_detect_roi.setText("重选区域")
        roi = None
        if self.target_hwnd is not None and self._window_alive(self.target_hwnd):
            try:
                _l, _t, cw, ch = win32gui.GetClientRect(self.target_hwnd)
                roi = roi_from_norm(
                    self._boss_detect_roi_norm, int(cw), int(ch)
                )
            except Exception:
                roi = None
        if roi is not None:
            self.lbl_boss_detect_roi.setText(
                f"x={roi[0]}, y={roi[1]}, 宽={roi[2]}, 高={roi[3]}"
            )
        else:
            self.lbl_boss_detect_roi.setText("已设置（绑定窗口后显示坐标）")
        self.lbl_boss_detect_roi.setStyleSheet(
            "color: #3ddc84; padding: 0;"
        )

    @staticmethod
    def _bool_setting(value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def _load_boss_settings(self) -> None:
        self._boss_loading = True
        try:
            self._load_boss_settings_inner()
        finally:
            self._boss_loading = False

    def _load_boss_settings_inner(self) -> None:
        s = self._settings
        self.chk_single_hp_potion.setChecked(
            self._bool_setting(s.value("single_hp_potion", False))
        )
        try:
            self.spin_single_hp_potion_threshold.setValue(
                max(
                    1,
                    min(
                        100000,
                        int(
                            s.value(
                                "single_hp_potion_threshold", 1600
                            )
                        ),
                    ),
                )
            )
        except (TypeError, ValueError):
            pass
        self.chk_keyboard_only.setChecked(
            self._bool_setting(s.value("keyboard_only", False))
        )
        self.chk_relative_mouse.setChecked(
            self._bool_setting(s.value("relative_mouse", False))
        )
        self.chk_background_mode.setChecked(
            self._bool_setting(s.value("background_mode", True))
        )
        if self.chk_keyboard_only.isChecked():
            self.chk_background_mode.blockSignals(True)
            self.chk_background_mode.setChecked(True)
            self.chk_background_mode.blockSignals(False)
            self.chk_relative_mouse.blockSignals(True)
            self.chk_relative_mouse.setChecked(False)
            self.chk_relative_mouse.blockSignals(False)
        elif (
            self.chk_background_mode.isChecked()
            and self.chk_relative_mouse.isChecked()
        ):
            self.chk_relative_mouse.blockSignals(True)
            self.chk_relative_mouse.setChecked(False)
            self.chk_relative_mouse.blockSignals(False)
        mode = str(s.value("boss/mode", "replay"))
        index = self.combo_boss_mode.findData(mode)
        if index >= 0:
            self.combo_boss_mode.setCurrentIndex(index)

        self.chk_boss_fatigue.setChecked(
            self._bool_setting(s.value("boss/fatigue_trigger", False))
        )
        self.chk_boss_timer.setChecked(
            self._bool_setting(s.value("boss/timer_trigger", False))
        )
        try:
            self.spin_boss_timer.setValue(
                max(1, min(1440, int(s.value("boss/timer_minutes", 60))))
            )
        except (TypeError, ValueError):
            pass

        def set_int(spin: QSpinBox, key: str, default: int) -> None:
            try:
                value = int(s.value(key, default))
            except (TypeError, ValueError):
                value = default
            spin.setValue(max(spin.minimum(), min(spin.maximum(), value)))

        set_int(self.spin_boss_speed, "boss/speed", 100)
        set_int(self.spin_boss_repeat, "boss/repeat", 1)
        set_int(self.spin_boss_gap, "boss/gap", 5)
        set_int(self.spin_boss_kill_hp, "boss/kill_hp", 0)
        set_int(self.spin_boss_miss, "boss/miss", 2)
        set_int(self.spin_boss_max_fight, "boss/max_fight", 180)
        set_int(self.spin_boss_pickup, "boss/pickup", 3)

        detect_mode = str(s.value("boss/detect_mode", "hp"))
        index = self.combo_boss_detect_mode.findData(detect_mode)
        if index >= 0:
            self.combo_boss_detect_mode.setCurrentIndex(index)

        roi = s.value("boss/detect_roi_norm")
        if isinstance(roi, str):
            roi = [
                part
                for part in roi.replace("[", "").replace("]", "").split(",")
                if part
            ]
        try:
            if isinstance(roi, (list, tuple)) and len(roi) == 4:
                values = tuple(float(v) for v in roi)
                if (
                    0.0 <= values[0] <= 1.0
                    and 0.0 <= values[1] <= 1.0
                    and values[2] > 0
                    and values[3] > 0
                ):
                    self._boss_detect_roi_norm = values  # type: ignore[assignment]
        except (TypeError, ValueError):
            pass

        for combo, key in (
            (self.combo_boss_record, "boss/record"),
            (self.combo_boss_approach, "boss/approach"),
            (self.combo_boss_return, "boss/return"),
        ):
            name = str(s.value(key, ""))
            index = combo.findData(name)
            if index >= 0:
                combo.setCurrentIndex(index)

    def _save_boss_settings(self) -> None:
        if self._boss_loading or not hasattr(self, "combo_boss_mode"):
            return
        s = self._settings
        s.setValue(
            "background_mode", self.chk_background_mode.isChecked()
        )
        s.setValue(
            "relative_mouse", self.chk_relative_mouse.isChecked()
        )
        s.setValue(
            "keyboard_only", self.chk_keyboard_only.isChecked()
        )
        s.setValue(
            "single_hp_potion", self.chk_single_hp_potion.isChecked()
        )
        s.setValue(
            "single_hp_potion_threshold",
            int(self.spin_single_hp_potion_threshold.value()),
        )
        s.setValue("boss/mode", self.combo_boss_mode.currentData() or "replay")
        s.setValue("boss/fatigue_trigger", self.chk_boss_fatigue.isChecked())
        s.setValue("boss/timer_trigger", self.chk_boss_timer.isChecked())
        s.setValue("boss/timer_minutes", int(self.spin_boss_timer.value()))
        s.setValue("boss/speed", int(self.spin_boss_speed.value()))
        s.setValue("boss/repeat", int(self.spin_boss_repeat.value()))
        s.setValue("boss/gap", int(self.spin_boss_gap.value()))
        s.setValue("boss/kill_hp", int(self.spin_boss_kill_hp.value()))
        s.setValue("boss/miss", int(self.spin_boss_miss.value()))
        s.setValue(
            "boss/max_fight", int(self.spin_boss_max_fight.value())
        )
        s.setValue("boss/pickup", int(self.spin_boss_pickup.value()))
        s.setValue(
            "boss/detect_mode",
            self.combo_boss_detect_mode.currentData() or "hp",
        )
        if self._boss_detect_roi_norm is not None:
            s.setValue(
                "boss/detect_roi_norm",
                [round(float(v), 6) for v in self._boss_detect_roi_norm],
            )
        else:
            s.remove("boss/detect_roi_norm")
        s.setValue("boss/record", self.combo_boss_record.currentData() or "")
        s.setValue("boss/approach", self.combo_boss_approach.currentData() or "")
        s.setValue("boss/return", self.combo_boss_return.currentData() or "")

    # -- 自动打BOSS：录制 ------------------------------------------------
    def _toggle_boss_record(self) -> None:
        if self._boss_recorder is not None:
            self._stop_boss_record()
            return
        if self._boss_record_pending:
            self._boss_record_pending = False
            self._set_boss_state("已取消录制", "warn")
            self._update_boss_controls()
            return
        if self._boss_busy() or self._multi_busy():
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify(
                "尚未绑定窗口",
                "请先绑定 CreatureCurios 窗口，再录制 BOSS 脚本。",
            )
            return
        self._boss_record_pending = True
        self._set_boss_state("3 秒后开始录制，请切到游戏…", "warn")
        self._update_boss_controls()
        QTimer.singleShot(3000, self._begin_boss_record)

    def _begin_boss_record(self) -> None:
        if not self._boss_record_pending or self._quitting:
            return
        self._boss_record_pending = False
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._set_boss_state("目标窗口已关闭，录制取消", "error")
            self._update_boss_controls()
            return
        try:
            recorder = MacroRecorder(self.target_hwnd)
            recorder.start()
        except Exception as exc:
            self._set_boss_state(f"录制启动失败：{exc}", "error")
            self._notify("录制启动失败", str(exc))
            self._update_boss_controls()
            return
        self._boss_recorder = recorder
        self._set_boss_state("录制中… 操作完切回本窗口点“停止录制”", "warn")
        self._update_boss_controls()

    def _stop_boss_record(self) -> None:
        recorder = self._boss_recorder
        self._boss_recorder = None
        if recorder is None:
            return
        try:
            events = strip_hotkey_tail(recorder.stop())
            meta = recorder.meta()
        except Exception as exc:
            self._set_boss_state(f"停止录制失败：{exc}", "error")
            self._update_boss_controls()
            return
        self._update_boss_controls()
        if not events:
            self._set_boss_state("没有录到任何操作，未保存", "warn")
            self._notify(
                "没有录到操作",
                "录制时游戏必须在前台，并且鼠标要位于游戏窗口内。"
                "请切到游戏后再操作，结束时切回本窗口点“停止录制”。",
            )
            return

        default_name = f"BOSS录制 {len(self._boss_records) + 1}"
        name, ok = QInputDialog.getText(
            self, "保存录制", "给这段 BOSS 录制起个名字：", text=default_name
        )
        if not ok:
            self._set_boss_state("已放弃保存", "warn")
            return
        name = str(name or "").strip() or default_name
        existing = {str(r.get("name")) for r in self._boss_records}
        base = name
        suffix = 2
        while name in existing:
            name = f"{base} ({suffix})"
            suffix += 1

        self._boss_records.append(
            {
                "name": name,
                "created": datetime.datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "events": events,
                "meta": meta,
            }
        )
        saved = save_boss_records(self._boss_records)
        self._refresh_boss_records()
        index = self.combo_boss_record.findData(name)
        if index >= 0:
            self.combo_boss_record.setCurrentIndex(index)
        self._save_boss_settings()
        move_count = sum(1 for e in events if e.get("type") == "move")
        duration = float(events[-1].get("t", 0.0)) if events else 0.0
        if move_count < 5 and duration > 3.0:
            move_note = "；鼠标移动很少，游戏可能锁定了鼠标，镜头可能无法复刻"
        else:
            move_note = f"；鼠标移动 {move_count} 次"
        self._set_boss_state(
            (
                f"已保存录制“{name}”（{len(events)} 个动作{move_note}）"
                if saved
                else f"录制“{name}”已生效，但写入文件失败（本次运行有效）{move_note}"
            ),
            "ok" if saved else "warn",
        )

    def _test_boss_record(self) -> None:
        record = self._combo_record(self.combo_boss_record)
        if record is None:
            self._notify("没有选择录制", "请先选择一段 BOSS 录制。")
            return
        if self._boss_busy() or self._multi_busy():
            return
        hwnd = self.target_hwnd
        if hwnd is None or not self._window_alive(hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return
        self._save_boss_settings()
        if (
            self.chk_background_mode.isChecked()
            and self._record_move_count(record) > 20
        ):
            self._set_status(
                "提示：该录制含大量鼠标移动，后台回放可能走偏；"
                "可关闭后台模式并勾选“相对鼠标移动”再试放",
                "warn",
            )
        was_farm = self._worker is not None
        if was_farm:
            self._halt_farm_for_boss()
        self._boss_preview = True
        self._boss_preview_resume = was_farm
        self._boss_running = True
        self._boss_kind = "replay"
        self._boss_stage = "试放"
        self._start_boss_player(
            record,
            stage="试放",
            speed=1.0,
            on_done=self._on_boss_player_done,
            on_error=self._on_boss_player_error,
        )
        self._set_boss_state(f"试放“{record.get('name')}”…", "warn")
        self._update_boss_controls()

    def _delete_boss_record(self) -> None:
        record = self._combo_record(self.combo_boss_record)
        if record is None:
            return
        if self._boss_busy():
            return
        name = str(record.get("name") or "")
        answer = QMessageBox.question(
            self,
            "删除录制",
            f"确定删除录制“{name}”吗？此操作不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._boss_records = [
            item for item in self._boss_records if item is not record
        ]
        save_boss_records(self._boss_records)
        self._refresh_boss_records()
        self._save_boss_settings()
        self._set_boss_state(f"已删除录制“{name}”", "ok")

    # -- 自动打BOSS：识别区域 --------------------------------------------
    def _capture_game_image(self) -> Optional[QImage]:
        """只截取游戏客户区，不移动鼠标。

        优先用单开页绑定的窗口；没有绑定时退回第一个有效的多开窗口，
        方便直接在多开页框选血量/角色名区域。
        """
        hwnd = self.target_hwnd
        if hwnd is None or not self._window_alive(hwnd):
            hwnd = None
            for index in range(4):
                candidate = self._multi_slot_hwnd(index)
                if candidate is not None and self._window_alive(candidate):
                    hwnd = candidate
                    break
        if hwnd is None:
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return None
        cap, err = capture_client_bgra(hwnd)
        if cap is None:
            self._notify("截图失败", err or "无法截取游戏画面。")
            return None
        w, h, data = cap
        return QImage(
            bytes(data), w, h, w * 4, QImage.Format.Format_RGB32
        ).copy()

    def _select_boss_detect_region(self) -> None:
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify(
                "尚未绑定窗口",
                "请先绑定 CreatureCurios 窗口，再框选 BOSS 检测区域。",
            )
            return
        image = self._capture_game_image()
        if image is None or image.isNull():
            return
        current = None
        if self._boss_detect_roi_norm is not None:
            try:
                current = roi_from_norm(
                    self._boss_detect_roi_norm,
                    image.width(),
                    image.height(),
                )
            except Exception:
                current = None
        dialog = RegionSelectDialog(
            image,
            current,
            self._capture_game_image,
            self,
            title_text="框选 BOSS 检测区域",
            info_text=(
                "框住能判断 BOSS 是否死亡的区域："
                "BOSS 血条上的数字（例如 12345/50000），"
                "或 BOSS 名称/血条文字。区域越小，识别越快越准。"
                "如果游戏里的血条没有数字，请把“检测方式”改成“BOSS 文字/名称”。"
            ),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        roi = dialog.selected_roi()
        if roi is None:
            return
        cw, ch = image.width(), image.height()
        if cw <= 0 or ch <= 0:
            return
        self._boss_detect_roi_norm = (
            roi[0] / cw,
            roi[1] / ch,
            roi[2] / cw,
            roi[3] / ch,
        )
        self._save_boss_settings()
        self._update_boss_roi_label()
        self._set_boss_state("BOSS 检测区域已保存", "ok")

    # -- 自动打BOSS：定时触发 --------------------------------------------
    def _on_boss_timer_toggled(self, *_args) -> None:
        if self._boss_loading:
            return
        self._save_boss_settings()
        self._schedule_boss_timer()

    def _schedule_boss_timer(self) -> None:
        if not hasattr(self, "chk_boss_timer"):
            return
        self._boss_trigger_timer.stop()
        if (
            self._quitting
            or not self.chk_boss_timer.isChecked()
            or self._boss_busy()
        ):
            return
        minutes = max(1, int(self.spin_boss_timer.value()))
        self._boss_trigger_timer.start(minutes * 60 * 1000)

    def _on_boss_timer_fire(self) -> None:
        if self._quitting:
            return
        if self._boss_busy():
            self._schedule_boss_timer()
            return
        if not self._boss_config_ready(silent=True):
            self._set_boss_state(
                "定时打BOSS：方案未配置完整（录制/检测区域）", "warn"
            )
            self._schedule_boss_timer()
            return
        self._boss_start(origin="timer", repeat_override=1)

    # -- 自动打BOSS：运行 ------------------------------------------------
    def _set_boss_state(self, text: str, level: str = "normal") -> None:
        if not hasattr(self, "lbl_boss_state"):
            return
        colors = {
            "normal": "#aeb4bd",
            "ok": "#3ddc84",
            "warn": "#ffb454",
            "error": "#ff6b6b",
        }
        self.lbl_boss_state.setText(text)
        self.lbl_boss_state.setStyleSheet(
            f"color: {colors.get(level, colors['normal'])}; padding: 0;"
        )

    def _boss_config_ready(self, silent: bool = False) -> bool:
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            if not silent:
                self._notify(
                    "尚未绑定窗口", "请先绑定 CreatureCurios 窗口。"
                )
            return False
        mode = self.combo_boss_mode.currentData()
        if mode == "replay":
            record = self._combo_record(self.combo_boss_record)
            if record is None or not record.get("events"):
                if not silent:
                    self._notify(
                        "没有整段录制",
                        "请先在“录制”里选择或录制一段完整的 BOSS 流程。",
                    )
                return False
            return True
        if self._boss_detect_roi_norm is None:
            if not silent:
                self._notify(
                    "尚未框选 BOSS 检测区域",
                    "识别战斗需要先框选 BOSS 血条数字或 BOSS 名称区域。",
                )
            return False
        return True

    def _boss_start_once(self) -> None:
        self._boss_start(origin="manual", repeat_override=1)

    def _boss_start_loop(self) -> None:
        self._boss_start(origin="manual", repeat_override=None)

    def _boss_start(
        self, origin: str = "manual", repeat_override: Optional[int] = None
    ) -> None:
        if self._quitting or self._boss_busy():
            return
        if self._multi_busy():
            self._notify(
                "多开正在运行",
                "请先停止多开挂机/回血录制，再启动自动BOSS。",
            )
            return
        if not self._boss_config_ready(silent=False):
            return
        if origin in ("fatigue", "timer"):
            hwnd = self.target_hwnd
            if (
                hwnd is not None
                and self._window_alive(hwnd)
                and win32gui.IsIconic(hwnd)
            ):
                self._set_boss_state(
                    "游戏窗口已最小化，跳过自动BOSS", "warn"
                )
                self._schedule_boss_timer()
                return
        self._save_boss_settings()
        if (
            self.combo_boss_mode.currentData() != "detect"
            and self.chk_background_mode.isChecked()
            and self._record_move_count(
                self._combo_record(self.combo_boss_record)
            ) > 20
        ):
            self._set_status(
                "提示：该录制含大量鼠标移动，后台回放可能走偏；"
                "可关闭后台模式并勾选“相对鼠标移动”",
                "warn",
            )
        was_farm = self._worker is not None
        if was_farm:
            self._halt_farm_for_boss()

        self._boss_kind = (
            "detect"
            if self.combo_boss_mode.currentData() == "detect"
            else "replay"
        )
        self._boss_origin = origin
        self._boss_resume_farm = was_farm or origin == "fatigue"
        self._boss_total = (
            int(self.spin_boss_repeat.value())
            if repeat_override is None
            else max(1, int(repeat_override))
        )
        self._boss_done = 0
        self._boss_interval = max(0.0, float(self.spin_boss_gap.value()))
        self._boss_last_error = ""
        self._boss_trigger_timer.stop()
        self._boss_running = True
        self._boss_preview = False
        self._boss_stage = ""
        self._update_boss_controls()
        self._set_boss_state("开始执行 BOSS 方案…", "ok")
        self._boss_run_iteration()

    def _boss_run_iteration(self) -> None:
        if not self._boss_running:
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._boss_finish("游戏窗口已关闭", "error")
            return
        self._boss_stage = ""
        if self._boss_kind == "detect":
            self._boss_detect_start()
        else:
            self._boss_replay_once()

    def _boss_replay_once(self) -> None:
        record = self._combo_record(self.combo_boss_record)
        if record is None or not record.get("events"):
            self._boss_finish("没有可回放的整段录制", "error")
            return
        speed = max(0.2, float(self.spin_boss_speed.value()) / 100.0)
        self._start_boss_player(
            record,
            stage="整段回放",
            speed=speed,
            on_done=self._on_boss_player_done,
            on_error=self._on_boss_player_error,
        )
        self._set_boss_state(
            f"整段回放“{record.get('name')}”…", "warn"
        )
        self._update_boss_controls()

    def _start_boss_player(
        self,
        record: dict,
        stage: str,
        speed: float,
        on_done,
        on_error,
    ) -> None:
        player = MacroPlayer(
            int(self.target_hwnd),
            stage,
            list(record.get("events") or []),
            meta=record.get("meta"),
            speed=speed,
            background=(
                self.chk_background_mode.isChecked()
                or self.chk_keyboard_only.isChecked()
            ),
            relative_mouse=self.chk_relative_mouse.isChecked(),
            keyboard_only=self.chk_keyboard_only.isChecked(),
            parent=self,
        )
        player.done.connect(on_done)
        player.error.connect(on_error)
        self._boss_player = player
        player.start()

    def _on_boss_player_done(self, _stage: str) -> None:
        player = self.sender()
        if player is not self._boss_player:
            return
        self._boss_player = None
        if player is not None:
            try:
                player.wait(500)
            except Exception:
                pass
            player.deleteLater()
        if self._boss_preview:
            self._finish_boss_preview("试放完成")
            return
        if not self._boss_running:
            return
        self._boss_after_iteration()

    def _on_boss_player_error(self, _stage: str, message: str) -> None:
        player = self.sender()
        if player is not self._boss_player:
            return
        self._boss_player = None
        if player is not None:
            try:
                player.wait(500)
            except Exception:
                pass
            player.deleteLater()
        if self._boss_preview:
            self._finish_boss_preview(f"试放失败：{message}", "error")
            return
        self._boss_finish(f"回放失败：{message}", "error")

    def _finish_boss_preview(
        self, message: str, level: str = "ok"
    ) -> None:
        self._boss_preview = False
        self._boss_running = False
        self._set_boss_state(message, level)
        resume = self._boss_preview_resume
        self._boss_preview_resume = False
        self._update_boss_controls()
        if resume and not self._quitting:
            QTimer.singleShot(300, self._resume_farm_after_boss)

    # ---- 识别战斗 ----
    def _boss_detect_start(self) -> None:
        approach = self._combo_record(self.combo_boss_approach)
        if approach is not None and approach.get("events"):
            self._boss_stage = "approach"
            self._start_boss_player(
                approach,
                stage="前往BOSS",
                speed=1.0,
                on_done=self._on_boss_approach_done,
                on_error=self._on_boss_player_error,
            )
            self._set_boss_state(
                f"前往BOSS：回放“{approach.get('name')}”…", "warn"
            )
            self._update_boss_controls()
            return
        self._boss_detect_start_fight()

    def _on_boss_approach_done(self, _stage: str) -> None:
        player = self.sender()
        if player is not self._boss_player:
            return
        self._boss_player = None
        if player is not None:
            try:
                player.wait(500)
            except Exception:
                pass
            player.deleteLater()
        if not self._boss_running:
            return
        self._boss_detect_start_fight()

    def _boss_detect_start_fight(self) -> None:
        if not self._boss_running:
            return
        if self._boss_detect_roi_norm is None:
            self._boss_finish("尚未框选 BOSS 检测区域", "error")
            return
        hwnd = self.target_hwnd
        if hwnd is None or not self._window_alive(hwnd):
            self._boss_finish("游戏窗口已关闭", "error")
            return

        self._boss_stage = "fight"
        fighter = KeySender(
            int(hwnd),
            self.spin_interval.value(),
            use_skills=self.chk_skills.isChecked(),
            use_potion=self.chk_potion.isChecked(),
            potion_interval_ms=self.spin_potion.value() * 1000,
            use_pickup=False,
            parent=self,
        )
        fighter.failed.connect(self._on_boss_fighter_failed)
        self._boss_fighter = fighter
        fighter.start()

        watcher = BossWatcher(
            int(hwnd),
            self._boss_detect_roi_norm,
            detect_mode=str(
                self.combo_boss_detect_mode.currentData() or "hp"
            ),
            kill_hp=int(self.spin_boss_kill_hp.value()),
            miss_limit=int(self.spin_boss_miss.value()),
            interval_seconds=1.5,
            parent=self,
        )
        watcher.update.connect(self._on_boss_watcher_update)
        watcher.gone.connect(self._on_boss_watcher_gone)
        self._boss_watcher = watcher
        watcher.start()

        limit = max(10, int(self.spin_boss_max_fight.value()))
        self._boss_fight_timer.start(limit * 1000)
        self._set_boss_state("BOSS 战斗中，等待识别击杀…", "warn")
        self._update_boss_controls()

    def _on_boss_watcher_update(self, text: str) -> None:
        if self._boss_running:
            self._set_boss_state(f"BOSS 战斗中：{text}", "warn")

    def _on_boss_watcher_gone(self, reason: str) -> None:
        watcher = self.sender()
        if watcher is not self._boss_watcher:
            return
        self._boss_watcher = None
        if watcher is not None:
            try:
                watcher.wait(1500)
            except Exception:
                pass
            watcher.deleteLater()
        self._boss_fight_timer.stop()
        self._stop_boss_fighter()
        if not self._boss_running:
            return
        self._boss_stage = "loot"
        pickup = int(self.spin_boss_pickup.value())
        self._set_boss_state(
            f"BOSS 已击杀（{reason}），拾取 {pickup} 次…", "ok"
        )
        if pickup <= 0:
            self._boss_detect_start_return()
            return
        looter = TapSender(
            int(self.target_hwnd),
            VK_SPACE,
            pickup,
            gap_seconds=0.18,
            parent=self,
        )
        looter.failed.connect(self._on_boss_looter_failed)
        looter.finished.connect(self._on_boss_loot_done)
        self._boss_looter = looter
        looter.start()
        self._update_boss_controls()

    def _on_boss_loot_done(self) -> None:
        looter = self.sender()
        if looter is not self._boss_looter:
            return
        self._boss_looter = None
        if looter is not None:
            try:
                looter.wait(1000)
            except Exception:
                pass
            looter.deleteLater()
        if not self._boss_running:
            return
        self._boss_detect_start_return()

    def _boss_detect_start_return(self) -> None:
        if not self._boss_running:
            return
        self._boss_stage = "return"
        ret = self._combo_record(self.combo_boss_return)
        if ret is None or not ret.get("events"):
            self._boss_after_iteration()
            return
        self._start_boss_player(
            ret,
            stage="返回打怪点",
            speed=1.0,
            on_done=self._on_boss_return_done,
            on_error=self._on_boss_player_error,
        )
        self._set_boss_state(
            f"返回打怪点：回放“{ret.get('name')}”…", "warn"
        )
        self._update_boss_controls()

    def _on_boss_return_done(self, _stage: str) -> None:
        player = self.sender()
        if player is not self._boss_player:
            return
        self._boss_player = None
        if player is not None:
            try:
                player.wait(500)
            except Exception:
                pass
            player.deleteLater()
        if not self._boss_running:
            return
        self._boss_after_iteration()

    def _boss_detect_on_timeout(self) -> None:
        if not self._boss_running or self._boss_stage != "fight":
            return
        self._stop_boss_watcher()
        self._stop_boss_fighter()
        self._set_boss_state(
            "战斗达到上限，未识别到 BOSS 死亡；继续拾取/返回",
            "warn",
        )
        self._boss_stage = "loot"
        pickup = int(self.spin_boss_pickup.value())
        if pickup <= 0:
            self._boss_detect_start_return()
            return
        looter = TapSender(
            int(self.target_hwnd),
            VK_SPACE,
            pickup,
            gap_seconds=0.18,
            parent=self,
        )
        looter.failed.connect(self._on_boss_looter_failed)
        looter.finished.connect(self._on_boss_loot_done)
        self._boss_looter = looter
        looter.start()

    def _on_boss_fighter_failed(self, message: str) -> None:
        if not self._boss_running:
            return
        self._boss_finish(f"战斗线程停止：{message}", "error")

    def _on_boss_looter_failed(self, message: str) -> None:
        if not self._boss_running:
            return
        self._boss_finish(f"拾取失败：{message}", "error")

    def _stop_boss_fighter(self) -> None:
        fighter = self._boss_fighter
        self._boss_fighter = None
        if fighter is not None:
            try:
                fighter.failed.disconnect(self._on_boss_fighter_failed)
            except Exception:
                pass
            try:
                fighter.stop()
                if not fighter.wait(2500):
                    fighter.terminate()
                    fighter.wait(500)
            except Exception:
                pass
            fighter.deleteLater()

    def _stop_boss_watcher(self) -> None:
        watcher = self._boss_watcher
        self._boss_watcher = None
        if watcher is not None:
            try:
                watcher.stop()
                if not watcher.wait(2000):
                    watcher.terminate()
                    watcher.wait(500)
            except Exception:
                pass
            watcher.deleteLater()

    def _stop_boss_looter(self) -> None:
        looter = self._boss_looter
        self._boss_looter = None
        if looter is not None:
            try:
                looter.stop()
                if not looter.wait(1500):
                    looter.terminate()
                    looter.wait(500)
            except Exception:
                pass
            looter.deleteLater()

    def _boss_after_iteration(self) -> None:
        if not self._boss_running:
            return
        self._boss_done += 1
        if self._boss_total > 0 and self._boss_done >= self._boss_total:
            self._boss_finish(
                f"BOSS 完成：共 {self._boss_done} 次", "ok"
            )
            return
        label = (
            f"{self._boss_done}/{self._boss_total}"
            if self._boss_total > 0
            else f"{self._boss_done}（无限）"
        )
        self._set_boss_state(f"BOSS 完成 {label}，等待下一轮…", "ok")
        self._boss_stage = ""
        self._boss_gap_timer.start(int(self._boss_interval * 1000))

    def _boss_stop_clicked(self) -> None:
        if self._boss_recorder is not None or self._boss_record_pending:
            self._toggle_boss_record()
            return
        self._stop_boss("已停止 BOSS", "warn")

    def _stop_boss(self, message: str, level: str = "warn") -> None:
        self._boss_running = False
        self._boss_preview = False
        self._boss_preview_resume = False
        self._boss_resume_farm = False
        self._boss_gap_timer.stop()
        self._boss_fight_timer.stop()
        self._boss_player_stop()
        self._stop_boss_watcher()
        self._stop_boss_fighter()
        self._stop_boss_looter()
        self._set_boss_state(message, level)
        self._update_boss_controls()
        self._schedule_boss_timer()

    def _boss_finish(self, message: str, level: str = "ok") -> None:
        resume = self._boss_resume_farm
        self._boss_resume_farm = False
        self._boss_stop_common()
        self._set_boss_state(message, level)
        self._update_boss_controls()
        self._schedule_boss_timer()
        if resume and not self._quitting:
            QTimer.singleShot(300, self._resume_farm_after_boss)

    def _boss_stop_common(self) -> None:
        self._boss_running = False
        self._boss_gap_timer.stop()
        self._boss_fight_timer.stop()
        self._boss_player_stop()
        self._stop_boss_watcher()
        self._stop_boss_fighter()
        self._stop_boss_looter()

    def _boss_player_stop(self) -> None:
        player = self._boss_player
        self._boss_player = None
        if player is not None:
            try:
                player.stop()
                if not player.wait(3000):
                    player.terminate()
                    player.wait(500)
            except Exception:
                pass
            player.deleteLater()

    def _halt_farm_for_boss(self) -> None:
        worker = self._worker
        if worker is None:
            self._stop_fatigue_monitor()
            return
        self._worker = None
        for signal, slot in (
            (worker.finished, self._on_worker_finished),
            (worker.failed, self._on_worker_failed),
            (worker.cycle_done, self._on_cycle_done),
        ):
            try:
                signal.disconnect(slot)
            except Exception:
                pass
        try:
            worker.stop()
            if not worker.wait(3000):
                worker.terminate()
                worker.wait(500)
        except Exception:
            pass
        worker.deleteLater()
        self._stop_fatigue_monitor()
        self._stopping = False
        self.btn_start.setEnabled(True)
        self.btn_start.setText("开始发送")
        self.btn_start.setStyleSheet(START_BUTTON_STYLES["idle"])
        self._set_running_controls(False)

    def _resume_farm_after_boss(self) -> None:
        if self._quitting or self._worker is not None:
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._set_boss_state("BOSS 已完成，但游戏窗口已关闭", "warn")
            return
        self._start_sending()
        self._set_boss_state("BOSS 完成，已恢复挂机", "ok")

    # -- 多开挂机：界面 ------------------------------------------------
    def _build_multi_tab(self) -> QWidget:
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        box_cfg = QGroupBox("多开设置")
        cg = QGridLayout(box_cfg)
        cg.setHorizontalSpacing(8)
        cg.setVerticalSpacing(8)
        cg.setColumnStretch(1, 1)
        cg.addWidget(QLabel("疲劳检查间隔"), 0, 0)
        self.spin_multi_interval = QSpinBox()
        self.spin_multi_interval.setRange(5, 600)
        self.spin_multi_interval.setValue(30)
        self.spin_multi_interval.setSuffix(" 秒")
        self.spin_multi_interval.setFixedWidth(100)
        self.spin_multi_interval.setToolTip("4 个窗口共用的疲劳检测间隔")
        cg.addWidget(self.spin_multi_interval, 0, 1)

        self.btn_multi_refresh = QPushButton("刷新窗口")
        self.btn_multi_refresh.clicked.connect(self._refresh_multi_windows)
        self.btn_multi_start_all = QPushButton("全部开始")
        self.btn_multi_start_all.clicked.connect(self._start_all_multi)
        self.btn_multi_stop_all = QPushButton("全部停止")
        self.btn_multi_stop_all.clicked.connect(self._stop_all_multi)
        self.btn_multi_read_names = QPushButton("读取全部名字")
        self.btn_multi_read_names.clicked.connect(self._read_all_names)
        cg.addWidget(self.btn_multi_refresh, 0, 2)
        cg.addWidget(self.btn_multi_start_all, 0, 3)
        cg.addWidget(self.btn_multi_stop_all, 0, 4)
        cg.addWidget(self.btn_multi_read_names, 0, 5)
        self.lbl_multi_state = QLabel("未运行")
        self.lbl_multi_state.setObjectName("muted")
        self.lbl_multi_state.setWordWrap(True)
        cg.addWidget(self.lbl_multi_state, 0, 6, 1, 2)

        self.chk_multi_background = QCheckBox(
            "后台回放（不抢鼠标键盘）"
        )
        self.chk_multi_background.setChecked(True)
        self.chk_multi_background.setToolTip(
            "多开的疲劳检测、BOSS/回血回放都改用 PostMessage 发送，"
            "不抢前台、不移动真实鼠标键盘。"
            "部分游戏不处理后鼠标消息，建议先“试放”验证。"
        )
        self.chk_multi_background.toggled.connect(
            self._on_multi_background_toggled
        )
        cg.addWidget(self.chk_multi_background, 1, 0, 1, 4)

        self.chk_multi_relative_mouse = QCheckBox(
            "前台相对鼠标（会抢鼠标）"
        )
        self.chk_multi_relative_mouse.setChecked(False)
        self.chk_multi_relative_mouse.setToolTip(
            "镜头更准，但会移动真实鼠标并占用前台，无法边用电脑边挂机。"
            "需要边学习边挂机请用后台模式。"
        )
        self.chk_multi_relative_mouse.toggled.connect(
            self._on_multi_relative_mouse_toggled
        )
        cg.addWidget(self.chk_multi_relative_mouse, 1, 4, 1, 4)

        self.chk_multi_keyboard_only = QCheckBox(
            "静止鼠标 / 仅键盘（防抢）"
        )
        self.chk_multi_keyboard_only.setChecked(False)
        self.chk_multi_keyboard_only.setToolTip(
            "只回放键盘事件，完全跳过鼠标移动/点击，不移动真实鼠标、"
            "不抢前台；游戏若自己抢到前台，会尝试把前台还给用户窗口。"
        )
        self.chk_multi_keyboard_only.toggled.connect(
            self._on_multi_keyboard_only_toggled
        )
        cg.addWidget(self.chk_multi_keyboard_only, 2, 0, 1, 8)

        multi_note = QLabel(
            "每个槽位绑定一个游戏窗口，可分别选择 BOSS 录制、回血录制、"
            "疲劳阈值、喝药间隔和优先级。打怪参数沿用「单开挂机」页的 "
            "A / 空格 / 技能 / 喝药设置。"
            "游戏最小化会暂停，后台模式请保持游戏窗口不最小化、放在其它窗口后面。"
        )
        multi_note.setObjectName("muted")
        multi_note.setWordWrap(True)
        cg.addWidget(multi_note, 3, 0, 1, 8)
        root.addWidget(box_cfg)

        box_aux = QGroupBox("血量保护 / 角色名")
        ag = QGridLayout(box_aux)
        ag.setHorizontalSpacing(8)
        ag.setVerticalSpacing(8)
        ag.setColumnStretch(1, 1)

        self.chk_hp_guard = QCheckBox("启用血量保护")
        self.chk_hp_guard.setToolTip(
            "低血时优先回血：回血红线（高）触发回血，"
            "停平A红线（低）只暂停发 A，技能/喝药继续。"
        )
        self.chk_hp_guard.toggled.connect(self._update_multi_controls)
        self.chk_hp_need_heal = QCheckBox("需要回血脚本（可选）")
        self.chk_hp_need_heal.setToolTip(
            "勾选：每个窗口必须选择/录制回血脚本；"
            "取消：低血只按停平A红线停手，不播放回血。"
        )
        self.chk_hp_need_heal.toggled.connect(self._update_multi_controls)
        ag.addWidget(self.chk_hp_guard, 0, 0)
        ag.addWidget(self.chk_hp_need_heal, 0, 1)

        ag.addWidget(QLabel("回血红线"), 0, 2)
        self.spin_hp_threshold = QSpinBox()
        self.spin_hp_threshold.setRange(HP_RED_LINE, 100000)
        self.spin_hp_threshold.setValue(600)
        self.spin_hp_threshold.setSingleStep(10)
        self.spin_hp_threshold.setFixedWidth(100)
        self.spin_hp_threshold.valueChanged.connect(self._on_hp_line_changed)
        ag.addWidget(self.spin_hp_threshold, 0, 3)

        ag.addWidget(QLabel("停平A红线"), 0, 4)
        self.spin_hp_pause_line = QSpinBox()
        self.spin_hp_pause_line.setRange(HP_RED_LINE, 100000)
        self.spin_hp_pause_line.setValue(HP_RED_LINE)
        self.spin_hp_pause_line.setSingleStep(10)
        self.spin_hp_pause_line.setFixedWidth(100)
        self.spin_hp_pause_line.valueChanged.connect(self._on_hp_line_changed)
        ag.addWidget(self.spin_hp_pause_line, 0, 5)

        ag.addWidget(QLabel("血量检测"), 0, 6)
        self.spin_hp_interval = QSpinBox()
        self.spin_hp_interval.setRange(1, 30)
        self.spin_hp_interval.setValue(1)
        self.spin_hp_interval.setSuffix(" 秒")
        self.spin_hp_interval.setFixedWidth(90)
        ag.addWidget(self.spin_hp_interval, 0, 7)

        self.chk_hp_potion = QCheckBox("血量低于阈值自动吃药（Q+W）")
        self.chk_hp_potion.setToolTip(
            "血量低于阈值时，按「喝药间隔」持续发送 Q 和 W，"
            "直到血量高于阈值；启用后不再按固定间隔自动喝药。"
            "使用下面框选的血量区域。"
        )
        self.chk_hp_potion.toggled.connect(self._update_multi_controls)
        ag.addWidget(self.chk_hp_potion, 1, 0, 1, 3)
        ag.addWidget(QLabel("吃药阈值"), 1, 3)
        self.spin_hp_potion_threshold = QSpinBox()
        self.spin_hp_potion_threshold.setRange(1, 100000)
        self.spin_hp_potion_threshold.setValue(1600)
        self.spin_hp_potion_threshold.setSingleStep(10)
        self.spin_hp_potion_threshold.setSuffix(" 点")
        self.spin_hp_potion_threshold.setFixedWidth(110)
        ag.addWidget(self.spin_hp_potion_threshold, 1, 4)
        ag.addWidget(
            QLabel("低于阈值后按 Q 间隔发送 Q + W"), 1, 5, 1, 3
        )

        self.btn_hp_roi = QPushButton("框选血量区域")
        self.btn_hp_roi.setFixedWidth(110)
        self.btn_hp_roi.clicked.connect(self._select_hp_roi)
        self.lbl_hp_roi = QLabel("未设置")
        self.lbl_hp_roi.setObjectName("muted")
        ag.addWidget(self.btn_hp_roi, 2, 0)
        ag.addWidget(self.lbl_hp_roi, 2, 1, 1, 3)

        self.btn_name_roi = QPushButton("框选角色名区域")
        self.btn_name_roi.setFixedWidth(126)
        self.btn_name_roi.clicked.connect(self._select_name_roi)
        self.lbl_name_roi = QLabel("未设置")
        self.lbl_name_roi.setObjectName("muted")
        ag.addWidget(self.btn_name_roi, 2, 4)
        ag.addWidget(self.lbl_name_roi, 2, 5, 1, 3)

        aux_note = QLabel(
            "血量区域框住头像旁的当前血量数字（如 1725/1780）；"
            "角色名区域框住窗口里显示的角色名。"
            "角色名在绑定/刷新窗口时自动读取，也可以点“读取全部名字”。"
            "窗口最小化时 OCR 会暂停，恢复窗口后自动继续。"
        )
        aux_note.setObjectName("muted")
        aux_note.setWordWrap(True)
        ag.addWidget(aux_note, 3, 0, 1, 8)
        root.addWidget(box_aux)

        self.combo_multi_windows: list[QComboBox] = []
        self.combo_multi_records: list[QComboBox] = []
        self.combo_multi_heal_records: list[QComboBox] = []
        self.lbl_multi_names: list[QLabel] = []
        self.lbl_multi_hps: list[QLabel] = []
        self.lbl_multi_heal_labels: list[QLabel] = []
        self.lbl_multi_slots: list[QLabel] = []
        self.spin_multi_thresholds: list[QSpinBox] = []
        self.spin_multi_potions: list[QSpinBox] = []
        self.spin_multi_priorities: list[QSpinBox] = []
        self.chk_multi_farm_only: list[QCheckBox] = []
        self.btn_multi_tests: list[QPushButton] = []
        self.btn_multi_starts: list[QPushButton] = []
        self.btn_multi_stops: list[QPushButton] = []
        self.btn_multi_heal_records: list[QPushButton] = []
        self.btn_multi_heal_tests: list[QPushButton] = []
        self.btn_multi_heal_deletes: list[QPushButton] = []

        for index in range(4):
            box = QGroupBox(f"窗口 {index + 1}")
            sg = QGridLayout(box)
            sg.setHorizontalSpacing(8)
            sg.setVerticalSpacing(8)
            sg.setColumnStretch(1, 1)
            sg.setColumnStretch(5, 1)

            sg.addWidget(QLabel("游戏窗口"), 0, 0)
            window_combo = QComboBox()
            window_combo.setMinimumWidth(260)
            window_combo.currentIndexChanged.connect(
                lambda _=0, i=index: self._on_multi_window_changed(i)
            )
            sg.addWidget(window_combo, 0, 1, 1, 2)

            name_label = QLabel("角色: --")
            name_label.setObjectName("muted")
            name_label.setMinimumWidth(150)
            name_label.setToolTip("自动 OCR 读取的角色名")
            sg.addWidget(name_label, 0, 3)

            sg.addWidget(QLabel("BOSS 录制"), 0, 4)
            record_combo = QComboBox()
            record_combo.setMinimumWidth(200)
            record_combo.currentIndexChanged.connect(
                lambda _=0: self._update_multi_controls()
            )
            sg.addWidget(record_combo, 0, 5, 1, 2)

            chk_farm = QCheckBox("本地挂机")
            chk_farm.setToolTip(
                "只在本窗口后台打怪，不检测疲劳、不播放录制；"
                "适合多开纯挂机。"
            )
            chk_farm.toggled.connect(
                lambda _=False: self._update_multi_controls()
            )
            sg.addWidget(chk_farm, 0, 7)

            btn_test = QPushButton("试放")
            btn_start = QPushButton("开始")
            btn_stop = QPushButton("停止")
            btn_stop.setEnabled(False)
            btn_test.clicked.connect(
                lambda _=False, i=index: self._test_multi_slot(i)
            )
            btn_start.clicked.connect(
                lambda _=False, i=index: self._start_multi_slot(i)
            )
            btn_stop.clicked.connect(
                lambda _=False, i=index: self._stop_multi_slot(i)
            )
            sg.addWidget(btn_test, 0, 8)
            sg.addWidget(btn_start, 0, 9)
            sg.addWidget(btn_stop, 0, 10)

            sg.addWidget(QLabel("疲劳阈值 ±"), 1, 0)
            spin_threshold = QSpinBox()
            spin_threshold.setRange(50, 1000)
            spin_threshold.setSingleStep(10)
            spin_threshold.setValue(int(self._fatigue_threshold))
            spin_threshold.setSuffix(" 点")
            spin_threshold.setFixedWidth(110)
            spin_threshold.setToolTip("这个窗口单独使用的疲劳阈值")
            sg.addWidget(spin_threshold, 1, 1)

            sg.addWidget(QLabel("喝药间隔"), 1, 2)
            spin_potion = QSpinBox()
            spin_potion.setRange(5, 3600)
            spin_potion.setSingleStep(5)
            spin_potion.setValue(int(self.spin_potion.value()))
            spin_potion.setSuffix(" 秒")
            spin_potion.setFixedWidth(100)
            spin_potion.setToolTip(
                "这个窗口单独使用的喝药间隔；是否喝药仍由单开页“自动喝药 Q”控制"
            )
            sg.addWidget(spin_potion, 1, 3)

            sg.addWidget(QLabel("优先级"), 1, 4)
            spin_priority = QSpinBox()
            spin_priority.setRange(1, 99)
            spin_priority.setValue(index + 1)
            spin_priority.setFixedWidth(70)
            spin_priority.setToolTip(
                "1 最高。多个窗口同时要打 BOSS 时，按优先级从小到大排队。"
            )
            sg.addWidget(spin_priority, 1, 5)

            status = QLabel("未开始")
            status.setObjectName("muted")
            status.setWordWrap(True)
            sg.addWidget(status, 1, 6, 1, 5)

            sg.addWidget(QLabel("血量"), 2, 0)
            hp_label = QLabel("血量: --")
            hp_label.setObjectName("muted")
            hp_label.setMinimumWidth(160)
            sg.addWidget(hp_label, 2, 1, 1, 2)

            heal_label = QLabel("回血录制")
            sg.addWidget(heal_label, 2, 3)
            heal_combo = QComboBox()
            heal_combo.setMinimumWidth(180)
            heal_combo.currentIndexChanged.connect(
                lambda _=0: self._update_multi_controls()
            )
            sg.addWidget(heal_combo, 2, 4, 1, 2)

            heal_btn = QPushButton("录制回血")
            heal_btn.clicked.connect(
                lambda _=False, i=index: self._toggle_multi_heal_record(i)
            )
            heal_test = QPushButton("试放")
            heal_test.clicked.connect(
                lambda _=False, i=index: self._test_multi_heal(i)
            )
            heal_delete = QPushButton("删除")
            heal_delete.clicked.connect(
                lambda _=False, i=index: self._delete_multi_heal(i)
            )
            sg.addWidget(heal_btn, 2, 6)
            sg.addWidget(heal_test, 2, 7)
            sg.addWidget(heal_delete, 2, 8)

            root.addWidget(box)
            self.combo_multi_windows.append(window_combo)
            self.combo_multi_records.append(record_combo)
            self.combo_multi_heal_records.append(heal_combo)
            self.lbl_multi_names.append(name_label)
            self.lbl_multi_hps.append(hp_label)
            self.lbl_multi_heal_labels.append(heal_label)
            self.lbl_multi_slots.append(status)
            self.spin_multi_thresholds.append(spin_threshold)
            self.spin_multi_potions.append(spin_potion)
            self.spin_multi_priorities.append(spin_priority)
            self.chk_multi_farm_only.append(chk_farm)
            self.btn_multi_tests.append(btn_test)
            self.btn_multi_starts.append(btn_start)
            self.btn_multi_stops.append(btn_stop)
            self.btn_multi_heal_records.append(heal_btn)
            self.btn_multi_heal_tests.append(heal_test)
            self.btn_multi_heal_deletes.append(heal_delete)

        root.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(tab)
        return scroll

    # -- 多开挂机：录制 / 窗口 / 角色名 ----------------------------------
    def _multi_record_data(self, index: int) -> Optional[dict]:
        if not (0 <= index < len(self.combo_multi_records)):
            return None
        return self._boss_record_by_name(
            self.combo_multi_records[index].currentData()
        )

    def _heal_record_data(self, index: int) -> Optional[dict]:
        if not (0 <= index < len(self.combo_multi_heal_records)):
            return None
        name = self.combo_multi_heal_records[index].currentData()
        if not name:
            return None
        for record in self._heal_records:
            if record.get("name") == name:
                return record
        return None

    @staticmethod
    def _fill_named_record_combo(
        combo: QComboBox, records: list[dict], placeholder: str
    ) -> None:
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(placeholder, None)
        for record in records:
            combo.addItem(str(record.get("name") or ""), record.get("name"))
        index = combo.findData(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _refresh_multi_records(self) -> None:
        if not hasattr(self, "combo_multi_records"):
            return
        for index in range(4):
            self._fill_named_record_combo(
                self.combo_multi_records[index],
                self._boss_records,
                "（请选择 BOSS 录制）",
            )
            self._fill_named_record_combo(
                self.combo_multi_heal_records[index],
                self._heal_records,
                "（不选择回血录制）",
            )
        self._update_multi_controls()

    def _enum_creature_windows(self) -> list[tuple[str, int]]:
        own_hwnd = 0
        try:
            own_hwnd = int(self.winId())
        except Exception:
            pass
        found: list[tuple[str, int]] = []

        def enum_callback(hwnd: int, _lparam) -> bool:
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                if int(hwnd) == own_hwnd:
                    return True
                title = win32gui.GetWindowText(hwnd).strip()
                if title and WINDOW_TITLE_KEYWORD.lower() in title.lower():
                    found.append((title, int(hwnd)))
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(enum_callback, None)
        except Exception:
            return []
        unique = {(title, hwnd) for title, hwnd in found}
        return sorted(unique, key=lambda item: (item[0].lower(), item[1]))

    def _refresh_multi_windows(self) -> None:
        if not hasattr(self, "combo_multi_windows"):
            return
        found = self._enum_creature_windows()
        found_hwnds = {hwnd for _title, hwnd in found}

        self._multi_loading = True
        try:
            used: set[int] = set()
            for index in range(4):
                combo = self.combo_multi_windows[index]
                current = combo.currentData()
                desired = None
                if current is not None and int(current) in found_hwnds:
                    desired = int(current)
                elif (
                    self._multi_saved_hwnds[index] is not None
                    and int(self._multi_saved_hwnds[index]) in found_hwnds
                ):
                    desired = int(self._multi_saved_hwnds[index])
                if desired is not None and desired in used:
                    desired = None
                if desired is not None:
                    used.add(desired)

                combo.blockSignals(True)
                combo.clear()
                combo.addItem("（请选择窗口）", None)
                for title, hwnd in found:
                    combo.addItem(f"{title} [0x{hwnd:X}]", hwnd)
                index_to_select = (
                    combo.findData(desired) if desired is not None else 0
                )
                if index_to_select < 0:
                    index_to_select = 0
                combo.setCurrentIndex(index_to_select)
                combo.blockSignals(False)
                self._multi_saved_hwnds[index] = combo.currentData()

            # 第二遍：把没分配到的槽位自动填上还没被占用的窗口
            used = {
                int(combo.currentData())
                for combo in self.combo_multi_windows
                if combo.currentData() is not None
            }
            free = [hwnd for _title, hwnd in found if hwnd not in used]
            for index in range(4):
                combo = self.combo_multi_windows[index]
                if combo.currentData() is not None or not free:
                    continue
                hwnd = free.pop(0)
                combo.blockSignals(True)
                combo.setCurrentIndex(combo.findData(hwnd))
                combo.blockSignals(False)
                self._multi_saved_hwnds[index] = hwnd
        finally:
            self._multi_loading = False

        self._update_multi_controls()
        for index in range(4):
            self._schedule_multi_name(index)

    def _on_multi_window_changed(self, index: int) -> None:
        if getattr(self, "_multi_loading", False):
            return
        if 0 <= index < len(self.combo_multi_windows):
            self._multi_saved_hwnds[index] = (
                self.combo_multi_windows[index].currentData()
            )
        self._schedule_multi_name(index)
        self._update_multi_controls()
        self._save_multi_settings()

    def _schedule_multi_name(self, index: int) -> None:
        if not (0 <= index < len(self.lbl_multi_names)):
            return
        label = self.lbl_multi_names[index]
        if self._name_roi_norm is None:
            label.setText("角色: 未设置区域")
            label.setStyleSheet("color: #ffb454; padding: 0;")
            label.setToolTip("请先点“框选角色名区域”")
            return
        hwnd = self._multi_slot_hwnd(index)
        if hwnd is None:
            label.setText("角色: --")
            label.setStyleSheet("color: #8f959e; padding: 0;")
            return
        if self._window_alive(hwnd) and win32gui.IsIconic(hwnd):
            label.setText("角色: 窗口已最小化")
            label.setStyleSheet("color: #8f959e; padding: 0;")
            label.setToolTip("恢复游戏窗口后会自动读取")
            return
        label.setText("角色: 读取中…")
        label.setStyleSheet("color: #8ab4f8; padding: 0;")
        QTimer.singleShot(120, lambda i=index: self._read_multi_name(i))

    def _read_multi_name(self, index: int) -> None:
        if self._quitting or self._name_roi_norm is None:
            return
        if not (0 <= index < 4):
            return
        hwnd = self._multi_slot_hwnd(index)
        if hwnd is None:
            return
        if self._window_alive(hwnd) and win32gui.IsIconic(hwnd):
            self._schedule_multi_name(index)
            return
        old = self._name_readers[index]
        if old is not None and old.isRunning():
            old.stop()
        reader = NameReader(hwnd, self._name_roi_norm, parent=self)
        self._name_readers[index] = reader
        reader.result.connect(
            lambda name, msg, i=index, h=hwnd: self._on_multi_name_result(
                i, h, name, msg
            )
        )
        reader.finished.connect(
            lambda i=index, r=reader: self._on_multi_name_finished(i, r)
        )
        reader.start()

    def _on_multi_name_result(
        self, index: int, hwnd: int, name: str, message: str
    ) -> None:
        if not (0 <= index < len(self.lbl_multi_names)):
            return
        current = self._multi_slot_hwnd(index)
        if current is None or int(current) != int(hwnd):
            return
        label = self.lbl_multi_names[index]
        if name:
            label.setText(f"角色: {name}")
            label.setStyleSheet("color: #3ddc84; padding: 0;")
            label.setToolTip(message)
        elif "最小化" in str(message):
            label.setText("角色: 窗口已最小化")
            label.setStyleSheet("color: #8f959e; padding: 0;")
            label.setToolTip("恢复游戏窗口后会自动读取")
        else:
            label.setText("角色: 识别失败")
            label.setStyleSheet("color: #ffb454; padding: 0;")
            label.setToolTip(message or "未识别到角色名，请重新框选区域")

    def _on_multi_name_finished(
        self, index: int, reader: NameReader
    ) -> None:
        if 0 <= index < len(self._name_readers):
            if self._name_readers[index] is reader:
                self._name_readers[index] = None
        try:
            reader.deleteLater()
        except Exception:
            pass

    def _read_all_names(self) -> None:
        if self._name_roi_norm is None:
            self._notify(
                "尚未框选角色名区域",
                "请先点“框选角色名区域”，框住每个窗口里显示的角色名。",
            )
            return
        for index in range(4):
            if self._multi_slot_hwnd(index) is not None:
                self._schedule_multi_name(index)

    def _stop_name_readers(self) -> None:
        for index, reader in enumerate(self._name_readers):
            if reader is not None:
                try:
                    reader.stop()
                    reader.wait(1500)
                except Exception:
                    pass
                self._name_readers[index] = None

    def _multi_slot_hwnd(self, index: int) -> Optional[int]:
        if not (0 <= index < len(self.combo_multi_windows)):
            return None
        hwnd = self.combo_multi_windows[index].currentData()
        if hwnd is None:
            return None
        return int(hwnd)

    def _multi_heal_recording(self) -> bool:
        return (
            self._heal_recorder is not None
            or self._heal_record_pending
        )

    def _any_multi_running(self) -> bool:
        return any(
            runner is not None and getattr(runner, "is_running", False)
            for runner in self._multi_runners
        )

    def _multi_busy(self) -> bool:
        return (
            self._any_multi_running()
            or self._multi_heal_recording()
            or self._heal_test_player is not None
        )

    def _single_busy_for_multi(self) -> bool:
        return bool(
            self._worker is not None
            or self._boss_busy()
            or self._heal_recorder is not None
            or self._heal_record_pending
            or self._heal_test_player is not None
        )

    def _set_multi_slot_text(self, index: int, text: str) -> None:
        if 0 <= index < len(self.lbl_multi_slots):
            self.lbl_multi_slots[index].setText(text)

    # -- 多开挂机：控件状态 ----------------------------------------------
    def _update_multi_controls(self) -> None:
        if not hasattr(self, "btn_multi_start_all"):
            return
        single_busy = self._single_busy_for_multi()
        heal_recording = self._multi_heal_recording()
        heal_testing = self._heal_test_player is not None
        any_running = self._any_multi_running()
        running_count = 0
        can_start_any = False

        for index in range(4):
            runner = self._multi_runners[index]
            running = runner is not None and getattr(
                runner, "is_running", False
            )
            if running:
                running_count += 1
            hwnd = self._multi_slot_hwnd(index)
            farm_only = self.chk_multi_farm_only[index].isChecked()
            record = self._multi_record_data(index)
            record_ok = bool(record and record.get("events"))
            heal_record = self._heal_record_data(index)
            heal_ok = bool(heal_record and heal_record.get("events"))
            hp_guard = self.chk_hp_guard.isChecked()
            need_heal = self.chk_hp_need_heal.isChecked()
            hp_potion = self.chk_hp_potion.isChecked()
            heal_enabled = bool(hp_guard and need_heal)
            hp_roi_required = bool(hp_guard or hp_potion)
            hp_roi_ok = self._hp_roi_norm is not None
            heal_recording_here = (
                heal_recording and self._heal_record_index == index
            )
            idle = (
                (not running)
                and (not single_busy)
                and (not heal_recording)
                and (not heal_testing)
            )

            self.combo_multi_windows[index].setEnabled(
                not running and not heal_recording and not single_busy
            )
            self.combo_multi_records[index].setEnabled(idle and not farm_only)
            self.combo_multi_heal_records[index].setEnabled(
                idle and heal_enabled
            )
            self.chk_multi_farm_only[index].setEnabled(not running)
            self.spin_multi_thresholds[index].setEnabled(not running)
            self.spin_multi_potions[index].setEnabled(not running)
            self.spin_multi_priorities[index].setEnabled(not running)

            heal_visible = bool(hp_guard and need_heal)
            self.lbl_multi_heal_labels[index].setVisible(heal_visible)
            self.combo_multi_heal_records[index].setVisible(heal_visible)
            self.btn_multi_heal_records[index].setVisible(heal_visible)
            self.btn_multi_heal_tests[index].setVisible(heal_visible)
            self.btn_multi_heal_deletes[index].setVisible(heal_visible)

            heal_btn = self.btn_multi_heal_records[index]
            if heal_recording_here:
                heal_btn.setText(
                    "取消录制" if self._heal_record_pending else "停止录制"
                )
            else:
                heal_btn.setText("录制回血")
            heal_btn.setEnabled(
                heal_recording_here
                or (idle and heal_enabled and hwnd is not None)
            )
            self.btn_multi_heal_tests[index].setEnabled(
                idle and heal_enabled and heal_ok and hwnd is not None
            )
            self.btn_multi_heal_deletes[index].setEnabled(
                idle and heal_enabled and heal_ok
            )

            hp_ok = (not hp_roi_required) or (
                hp_roi_ok
                and (heal_ok or not (hp_guard and need_heal))
            )
            can_start = bool(
                idle
                and hwnd is not None
                and hp_ok
                and (farm_only or (record_ok and self._fatigue_roi_norm))
            )
            missing = []
            if hwnd is None:
                missing.append("游戏窗口")
            if not farm_only and not record_ok:
                missing.append("BOSS 录制")
            if not farm_only and self._fatigue_roi_norm is None:
                missing.append("疲劳提示区域")
            if hp_roi_required and not hp_roi_ok:
                missing.append("血量区域")
            if hp_guard and need_heal and not heal_ok:
                missing.append("回血录制")
            self.btn_multi_starts[index].setToolTip(
                "还缺少：" + "、".join(missing)
                if missing
                else "开始这个窗口的挂机"
            )
            self.btn_multi_starts[index].setEnabled(can_start)
            self.btn_multi_stops[index].setEnabled(running)
            self.btn_multi_tests[index].setEnabled(
                idle and hwnd is not None and record_ok and not farm_only
            )
            if can_start:
                can_start_any = True

        self.spin_multi_interval.setEnabled(not any_running)
        self.btn_multi_refresh.setEnabled(
            not any_running and not heal_recording and not heal_testing
        )
        self.btn_multi_start_all.setEnabled(can_start_any)
        self.btn_multi_stop_all.setEnabled(any_running)
        for widget in (
            self.chk_hp_guard,
            self.chk_hp_need_heal,
            self.spin_hp_threshold,
            self.spin_hp_pause_line,
            self.spin_hp_interval,
            self.btn_hp_roi,
            self.btn_name_roi,
            self.chk_hp_potion,
            self.spin_hp_potion_threshold,
            self.chk_multi_background,
            self.chk_multi_relative_mouse,
            self.chk_multi_keyboard_only,
        ):
            widget.setEnabled(
                not any_running and not heal_recording and not heal_testing
            )
        self.chk_hp_need_heal.setEnabled(
            (not any_running)
            and (not heal_recording)
            and (not heal_testing)
            and self.chk_hp_guard.isChecked()
        )
        if any_running:
            self.lbl_multi_state.setText(f"多开运行中：{running_count}/4")
        else:
            self.lbl_multi_state.setText(
                getattr(self, "_multi_state_text", "未运行")
            )
        self._update_bind_button_state()
        self._update_toggle_action()

    # -- 多开挂机：启动 / 停止 --------------------------------------------
    def _start_multi_slot(self, index: int, silent: bool = False) -> None:
        if not (0 <= index < 4):
            return
        if self._single_busy_for_multi() or self._multi_heal_recording():
            if not silent:
                self._notify(
                    "单开自动化正在运行",
                    "请先停止单开挂机/自动BOSS/回血录制，再启动多开。",
                )
            return
        runner = self._multi_runners[index]
        if runner is not None and getattr(runner, "is_running", False):
            return
        hwnd = self._multi_slot_hwnd(index)
        if hwnd is None:
            if not silent:
                self._notify(
                    "没有选择窗口",
                    f"请给窗口 {index + 1} 选择一个游戏窗口。",
                )
            else:
                self._set_multi_slot_text(index, "未选择窗口")
            return
        if not self._window_alive(hwnd):
            if not silent:
                self._notify(
                    "窗口已关闭",
                    "请先点“刷新窗口”，重新选择。",
                )
            return
        for other_index, other in enumerate(self._multi_runners):
            if (
                other_index != index
                and other is not None
                and getattr(other, "is_running", False)
                and int(other.hwnd) == int(hwnd)
            ):
                if not silent:
                    self._notify(
                        "窗口重复",
                        f"窗口 {index + 1} 和窗口 {other_index + 1} "
                        "选了同一个游戏窗口。",
                    )
                return

        farm_only = self.chk_multi_farm_only[index].isChecked()
        record = self._multi_record_data(index)
        if not farm_only:
            if record is None or not record.get("events"):
                if not silent:
                    self._notify(
                        "没有录制",
                        f"请先给窗口 {index + 1} 选择一段有效的 BOSS 录制，"
                        "或者勾选“本地挂机”。",
                    )
                return
            if self._fatigue_roi_norm is None:
                if not silent:
                    self._notify(
                        "尚未框选疲劳区域",
                        "请先在单开页框选疲劳提示区域，"
                        "或者勾选“本地挂机”。",
                    )
                return

        hp_enabled = self.chk_hp_guard.isChecked()
        need_heal = self.chk_hp_need_heal.isChecked()
        hp_potion = self.chk_hp_potion.isChecked()
        heal_record = self._heal_record_data(index)
        if (hp_enabled or hp_potion) and self._hp_roi_norm is None:
            if not silent:
                self._notify(
                    "尚未框选血量区域",
                    "请先点“框选血量区域”，框住头像旁边的当前血量数字。",
                )
            return
        if hp_enabled:
            if need_heal and (
                heal_record is None or not heal_record.get("events")
            ):
                if not silent:
                    self._notify(
                        "没有回血录制",
                        f"请给窗口 {index + 1} 选择一段回血录制，"
                        "或者取消勾选“需要回血脚本”。",
                    )
                return

        self._save_multi_settings()
        if (
            self.chk_multi_background.isChecked()
            and self._record_move_count(record) > 20
        ):
            self._set_status(
                "提示：多开录制含大量鼠标移动，后台回放可能走偏；"
                "可关闭后台并勾选“相对鼠标移动”",
                "warn",
            )
        config = {
            "label": str(record.get("name") or "本地挂机") if record else "本地挂机",
            "farm_only": bool(farm_only),
            "events": list(record.get("events", [])) if record else [],
            "meta": record.get("meta") if record else None,
            "hp_enabled": bool(hp_enabled),
            "hp_need_heal": bool(need_heal),
            "hp_potion_enabled": bool(hp_potion),
            "hp_potion_threshold": int(
                self.spin_hp_potion_threshold.value()
            ),
            "hp_roi": (
                list(self._hp_roi_norm)
                if (hp_enabled or hp_potion)
                else None
            ),
            "hp_threshold": int(self.spin_hp_threshold.value()),
            "hp_pause_line": int(self.spin_hp_pause_line.value()),
            "hp_red_line": HP_RED_LINE,
            "hp_interval": int(self.spin_hp_interval.value()),
            "heal_events": (
                list(heal_record.get("events", []))
                if hp_enabled and need_heal and heal_record
                else []
            ),
            "heal_meta": (
                heal_record.get("meta")
                if hp_enabled and need_heal and heal_record
                else None
            ),
            "threshold": int(self.spin_multi_thresholds[index].value()),
            "priority": int(self.spin_multi_priorities[index].value()),
            "interval": float(self.spin_multi_interval.value()),
            "roi_norm": self._fatigue_roi_norm,
            "speed": 1.0,
            "interval_ms": int(self.spin_interval.value()),
            "use_skills": bool(self.chk_skills.isChecked()),
            "potion_interval_ms": int(
                self.spin_multi_potions[index].value()
            ) * 1000,
            "use_pickup": bool(self.chk_pickup.isChecked()),
            "use_potion": bool(self.chk_potion.isChecked())
            and not bool(hp_potion),
            "background": bool(
                self.chk_multi_background.isChecked()
                or self.chk_multi_keyboard_only.isChecked()
            ),
            "borrow_mouse": bool(
                self.chk_fatigue_borrow_mouse.isChecked()
            ),
            "relative_mouse": bool(
                self.chk_multi_relative_mouse.isChecked()
            ),
            "keyboard_only": bool(
                self.chk_multi_keyboard_only.isChecked()
            ),
        }
        slot = SlotRunner(int(hwnd), config, parent=self)
        slot.status.connect(
            lambda text, level, i=index: self._on_multi_slot_status(
                i, text, level
            )
        )
        slot.finished.connect(
            lambda i=index: self._on_multi_slot_finished(i)
        )
        slot.hp_changed.connect(
            lambda cur, mx, msg, i=index: self._on_multi_slot_hp(
                i, cur, mx, msg
            )
        )
        self._multi_runners[index] = slot
        self.lbl_multi_slots[index].setText("启动中…")
        slot.start()
        if not getattr(slot, "is_running", False):
            slot.deleteLater()
            self._multi_runners[index] = None
        self._schedule_multi_name(index)
        self._update_multi_controls()

    def _test_multi_slot(self, index: int) -> None:
        if not (0 <= index < 4):
            return
        if self._single_busy_for_multi() or self._multi_heal_recording():
            self._notify(
                "单开自动化正在运行",
                "请先停止单开挂机/自动BOSS/回血录制，再试放多开录制。",
            )
            return
        runner = self._multi_runners[index]
        if runner is not None and getattr(runner, "is_running", False):
            return
        if self.chk_multi_farm_only[index].isChecked():
            self._notify(
                "本地挂机不需要试放",
                "“本地挂机”只后台打怪，不播放 BOSS 录制。",
            )
            return
        hwnd = self._multi_slot_hwnd(index)
        if hwnd is None or not self._window_alive(hwnd):
            self._notify("没有有效窗口", "请先选择并刷新游戏窗口。")
            return
        record = self._multi_record_data(index)
        if record is None or not record.get("events"):
            self._notify("没有录制", "请先选择一段有效的 BOSS 录制。")
            return
        self._save_multi_settings()
        if (
            self.chk_multi_background.isChecked()
            and self._record_move_count(record) > 20
        ):
            self._set_status(
                "提示：录制含大量鼠标移动，后台试放可能走偏；"
                "可关闭后台并勾选“相对鼠标移动”再试放",
                "warn",
            )
        config = {
            "label": str(record.get("name") or "BOSS"),
            "farm_only": False,
            "events": list(record.get("events", [])),
            "meta": record.get("meta"),
            "hp_enabled": False,
            "hp_roi": None,
            "threshold": int(self.spin_multi_thresholds[index].value()),
            "priority": int(self.spin_multi_priorities[index].value()),
            "interval": float(self.spin_multi_interval.value()),
            "roi_norm": self._fatigue_roi_norm,
            "speed": 1.0,
            "interval_ms": int(self.spin_interval.value()),
            "use_skills": bool(self.chk_skills.isChecked()),
            "use_potion": bool(self.chk_potion.isChecked()),
            "potion_interval_ms": int(
                self.spin_multi_potions[index].value()
            ) * 1000,
            "use_pickup": bool(self.chk_pickup.isChecked()),
            "background": bool(
                self.chk_multi_background.isChecked()
                or self.chk_multi_keyboard_only.isChecked()
            ),
            "borrow_mouse": bool(
                self.chk_fatigue_borrow_mouse.isChecked()
            ),
            "relative_mouse": bool(
                self.chk_multi_relative_mouse.isChecked()
            ),
            "keyboard_only": bool(
                self.chk_multi_keyboard_only.isChecked()
            ),
        }
        slot = SlotRunner(int(hwnd), config, test_only=True, parent=self)
        slot.status.connect(
            lambda text, level, i=index: self._on_multi_slot_status(
                i, text, level
            )
        )
        slot.finished.connect(
            lambda i=index: self._on_multi_slot_finished(i)
        )
        self._multi_runners[index] = slot
        self.lbl_multi_slots[index].setText("试放中…")
        slot.start()
        if not getattr(slot, "is_running", False):
            slot.deleteLater()
            self._multi_runners[index] = None
        self._update_multi_controls()

    def _stop_multi_slot(self, index: int) -> None:
        if not (0 <= index < 4):
            return
        runner = self._multi_runners[index]
        if runner is not None:
            runner.stop("已停止", "warn")
        self._update_multi_controls()

    def _start_all_multi(self) -> None:
        for index in range(4):
            if self._multi_slot_hwnd(index) is not None:
                self._start_multi_slot(index, silent=True)

    def _stop_all_multi(self) -> None:
        for index in range(4):
            runner = self._multi_runners[index]
            if runner is not None:
                runner.stop("已停止", "warn")
        self._update_multi_controls()

    def _on_multi_slot_status(
        self, index: int, text: str, level: str
    ) -> None:
        if 0 <= index < len(self.lbl_multi_slots):
            self.lbl_multi_slots[index].setText(text)

    def _on_multi_slot_hp(
        self, index: int, current, maximum, message: str
    ) -> None:
        if not (0 <= index < len(self.lbl_multi_hps)):
            return
        label = self.lbl_multi_hps[index]
        if current is None:
            if "最小化" in str(message):
                label.setText("血量: 窗口已最小化")
            else:
                label.setText(f"血量: {message or '--'}")
            label.setStyleSheet("color: #8f959e; padding: 0;")
            return
        try:
            cur = int(current)
        except (TypeError, ValueError):
            return
        text = (
            f"血量: {cur}/{int(maximum)}"
            if maximum is not None
            else f"血量: {cur}"
        )
        try:
            potion_threshold = int(
                self.spin_hp_potion_threshold.value()
            )
            potion_interval = int(
                self.spin_multi_potions[index].value()
            )
        except Exception:
            potion_threshold = 1600
            potion_interval = 60
        if (
            cur < potion_threshold
            and self.chk_hp_potion.isChecked()
        ):
            text += f"（自动吃药中：每 {potion_interval} 秒）"
        label.setText(text)
        try:
            heal_line = int(self.spin_hp_threshold.value())
        except Exception:
            heal_line = 600
        try:
            pause_line = int(self.spin_hp_pause_line.value())
        except Exception:
            pause_line = HP_RED_LINE
        if cur < pause_line:
            color = "#ff6b6b"
        elif cur < heal_line:
            color = "#ffb454"
        else:
            color = "#3ddc84"
        label.setStyleSheet(f"color: {color}; padding: 0;")

    def _on_multi_slot_finished(self, index: int) -> None:
        if not (0 <= index < 4):
            return
        runner = self._multi_runners[index]
        if runner is not None:
            runner.deleteLater()
            self._multi_runners[index] = None
        self._update_multi_controls()

    # -- 多开挂机：血量 / 角色名区域 --------------------------------------
    def _set_roi_spin(self, spin: QSpinBox, value: int) -> None:
        spin.blockSignals(True)
        spin.setValue(int(value))
        spin.blockSignals(False)

    def _on_hp_line_changed(self, *_args) -> None:
        if not hasattr(self, "spin_hp_threshold"):
            return
        heal = max(HP_RED_LINE, int(self.spin_hp_threshold.value()))
        pause = max(HP_RED_LINE, int(self.spin_hp_pause_line.value()))
        if pause > heal:
            pause = heal
        if heal != self.spin_hp_threshold.value():
            self._set_roi_spin(self.spin_hp_threshold, heal)
        if pause != self.spin_hp_pause_line.value():
            self._set_roi_spin(self.spin_hp_pause_line, pause)

    def _refresh_hp_roi_label(self) -> None:
        if not hasattr(self, "lbl_hp_roi"):
            return
        if self._hp_roi_norm is None:
            self.lbl_hp_roi.setText("未设置")
            self.lbl_hp_roi.setStyleSheet("color: #ffb454; padding: 0;")
            return
        self.lbl_hp_roi.setText("已设置")
        self.lbl_hp_roi.setStyleSheet("color: #3ddc84; padding: 0;")

    def _select_hp_roi(self) -> None:
        image = self._capture_game_image()
        if image is None or image.isNull():
            return
        current = None
        if self._hp_roi_norm is not None:
            try:
                current = roi_from_norm(
                    self._hp_roi_norm, image.width(), image.height()
                )
            except Exception:
                current = None
        dialog = RegionSelectDialog(
            image,
            current,
            self._capture_game_image,
            self,
            title_text="框选血量区域",
            info_text=(
                "框住头像旁边的“当前血量”数字，例如 1725/1780 中的当前血量部分。"
                "多开时所有窗口共用这个区域，程序会按窗口客户区尺寸自动缩放。"
                "血量数字不要框进太多其它文字，否则 OCR 容易识别错。"
            ),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        roi = dialog.selected_roi()
        if roi is None:
            return
        cw, ch = image.width(), image.height()
        if cw <= 0 or ch <= 0:
            return
        self._hp_roi_norm = (
            roi[0] / cw,
            roi[1] / ch,
            roi[2] / cw,
            roi[3] / ch,
        )
        self._save_multi_settings()
        self._refresh_hp_roi_label()
        self._refresh_single_hp_roi_label()
        self._update_multi_controls()

    def _refresh_name_roi_label(self) -> None:
        if not hasattr(self, "lbl_name_roi"):
            return
        if self._name_roi_norm is None:
            self.lbl_name_roi.setText("未设置")
            self.lbl_name_roi.setStyleSheet("color: #ffb454; padding: 0;")
            return
        self.lbl_name_roi.setText("已设置")
        self.lbl_name_roi.setStyleSheet("color: #3ddc84; padding: 0;")

    def _select_name_roi(self) -> None:
        image = self._capture_game_image()
        if image is None or image.isNull():
            return
        current = None
        if self._name_roi_norm is not None:
            try:
                current = roi_from_norm(
                    self._name_roi_norm, image.width(), image.height()
                )
            except Exception:
                current = None
        dialog = RegionSelectDialog(
            image,
            current,
            self._capture_game_image,
            self,
            title_text="框选角色名区域",
            info_text=(
                "框住游戏窗口里显示角色名的位置。"
                "绑定/刷新多开窗口时会自动读取，并显示在“角色:”旁边；"
                "如果名字有多个字/带称号，尽量只框角色名本身，识别率更高。"
            ),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        roi = dialog.selected_roi()
        if roi is None:
            return
        cw, ch = image.width(), image.height()
        if cw <= 0 or ch <= 0:
            return
        self._name_roi_norm = (
            roi[0] / cw,
            roi[1] / ch,
            roi[2] / cw,
            roi[3] / ch,
        )
        self._save_multi_settings()
        self._refresh_name_roi_label()
        self._update_multi_controls()
        self._read_all_names()

    # -- 多开挂机：设置持久化 --------------------------------------------
    def _load_multi_settings(self) -> None:
        if not hasattr(self, "chk_hp_guard"):
            return
        self._multi_loading = True
        s = self._settings
        try:
            self.chk_multi_keyboard_only.setChecked(
                self._bool_setting(s.value("multi/keyboard_only", False))
            )
            self.chk_multi_relative_mouse.setChecked(
                self._bool_setting(
                    s.value("multi/relative_mouse", False)
                )
            )
            self.chk_multi_background.setChecked(
                self._bool_setting(s.value("multi/background", True))
            )
            if self.chk_multi_keyboard_only.isChecked():
                self.chk_multi_background.blockSignals(True)
                self.chk_multi_background.setChecked(True)
                self.chk_multi_background.blockSignals(False)
                self.chk_multi_relative_mouse.blockSignals(True)
                self.chk_multi_relative_mouse.setChecked(False)
                self.chk_multi_relative_mouse.blockSignals(False)
            elif (
                self.chk_multi_background.isChecked()
                and self.chk_multi_relative_mouse.isChecked()
            ):
                self.chk_multi_relative_mouse.blockSignals(True)
                self.chk_multi_relative_mouse.setChecked(False)
                self.chk_multi_relative_mouse.blockSignals(False)
            try:
                self.spin_multi_interval.setValue(
                    max(
                        5,
                        min(
                            600,
                            int(s.value("multi/fatigue_interval", 30)),
                        ),
                    )
                )
            except (TypeError, ValueError):
                pass
            self.chk_hp_guard.setChecked(
                self._bool_setting(s.value("multi/hp_enabled", False))
            )
            self.chk_hp_need_heal.setChecked(
                self._bool_setting(s.value("multi/hp_need_heal", False))
            )
            self.chk_hp_potion.setChecked(
                self._bool_setting(
                    s.value("multi/hp_potion_enabled", False)
                )
            )
            try:
                self.spin_hp_potion_threshold.setValue(
                    max(
                        1,
                        min(
                            100000,
                            int(
                                s.value(
                                    "multi/hp_potion_threshold", 1600
                                )
                            ),
                        ),
                    )
                )
            except (TypeError, ValueError):
                pass
            try:
                self.spin_hp_threshold.setValue(
                    max(
                        HP_RED_LINE,
                        int(s.value("multi/hp_threshold", 600)),
                    )
                )
            except (TypeError, ValueError):
                pass
            try:
                self.spin_hp_pause_line.setValue(
                    max(
                        HP_RED_LINE,
                        int(s.value("multi/hp_pause_line", HP_RED_LINE)),
                    )
                )
            except (TypeError, ValueError):
                pass
            try:
                self.spin_hp_interval.setValue(
                    max(1, min(30, int(s.value("multi/hp_interval", 1))))
                )
            except (TypeError, ValueError):
                pass

            self._hp_roi_norm = self._parse_roi_setting(
                s.value("multi/hp_roi_norm")
            )
            self._name_roi_norm = self._parse_roi_setting(
                s.value("multi/name_roi_norm")
            )

            for index in range(4):
                self._multi_saved_hwnds[index] = self._parse_int_setting(
                    s.value(f"multi/slot{index}/hwnd")
                )
                try:
                    self.spin_multi_thresholds[index].setValue(
                        max(
                            50,
                            min(
                                1000,
                                int(
                                    s.value(
                                        f"multi/slot{index}/threshold",
                                        self._fatigue_threshold,
                                    )
                                ),
                            ),
                        )
                    )
                    self.spin_multi_potions[index].setValue(
                        max(
                            5,
                            min(
                                3600,
                                int(
                                    s.value(
                                        f"multi/slot{index}/potion",
                                        self.spin_potion.value(),
                                    )
                                ),
                            ),
                        )
                    )
                    self.spin_multi_priorities[index].setValue(
                        max(
                            1,
                            min(
                                99,
                                int(
                                    s.value(
                                        f"multi/slot{index}/priority",
                                        index + 1,
                                    )
                                ),
                            ),
                        )
                    )
                except (TypeError, ValueError):
                    pass
                self.chk_multi_farm_only[index].setChecked(
                    self._bool_setting(
                        s.value(f"multi/slot{index}/farm_only", False)
                    )
                )
                for combo, key in (
                    (
                        self.combo_multi_records[index],
                        f"multi/slot{index}/record",
                    ),
                    (
                        self.combo_multi_heal_records[index],
                        f"multi/slot{index}/heal",
                    ),
                ):
                    name = str(s.value(key, ""))
                    pos = combo.findData(name)
                    if pos >= 0:
                        combo.setCurrentIndex(pos)
        finally:
            self._multi_loading = False
        self._on_hp_line_changed()
        self._refresh_hp_roi_label()
        self._refresh_single_hp_roi_label()
        self._refresh_name_roi_label()
        self._update_multi_controls()

    @staticmethod
    def _parse_int_setting(value) -> Optional[int]:
        try:
            if value is None or str(value).strip() == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_roi_setting(
        value,
    ) -> Optional[tuple[float, float, float, float]]:
        if isinstance(value, str):
            value = [
                part
                for part in value.replace("[", "").replace("]", "").split(",")
                if part.strip()
            ]
        try:
            if isinstance(value, (list, tuple)) and len(value) == 4:
                values = tuple(float(v) for v in value)
                if (
                    0.0 <= values[0] <= 1.0
                    and 0.0 <= values[1] <= 1.0
                    and values[2] > 0
                    and values[3] > 0
                ):
                    return values  # type: ignore[return-value]
        except (TypeError, ValueError):
            pass
        return None

    def _save_multi_settings(self) -> None:
        if getattr(self, "_multi_loading", False):
            return
        if not hasattr(self, "chk_hp_guard"):
            return
        s = self._settings
        s.setValue(
            "multi/background", self.chk_multi_background.isChecked()
        )
        s.setValue(
            "multi/relative_mouse",
            self.chk_multi_relative_mouse.isChecked(),
        )
        s.setValue(
            "multi/keyboard_only",
            self.chk_multi_keyboard_only.isChecked(),
        )
        s.setValue(
            "multi/fatigue_interval", int(self.spin_multi_interval.value())
        )
        s.setValue("multi/hp_enabled", self.chk_hp_guard.isChecked())
        s.setValue("multi/hp_need_heal", self.chk_hp_need_heal.isChecked())
        s.setValue(
            "multi/hp_potion_enabled", self.chk_hp_potion.isChecked()
        )
        s.setValue(
            "multi/hp_potion_threshold",
            int(self.spin_hp_potion_threshold.value()),
        )
        s.setValue(
            "multi/hp_threshold", int(self.spin_hp_threshold.value())
        )
        s.setValue(
            "multi/hp_pause_line", int(self.spin_hp_pause_line.value())
        )
        s.setValue("multi/hp_interval", int(self.spin_hp_interval.value()))
        if self._hp_roi_norm is not None:
            s.setValue(
                "multi/hp_roi_norm",
                [round(float(v), 6) for v in self._hp_roi_norm],
            )
        else:
            s.remove("multi/hp_roi_norm")
        if self._name_roi_norm is not None:
            s.setValue(
                "multi/name_roi_norm",
                [round(float(v), 6) for v in self._name_roi_norm],
            )
        else:
            s.remove("multi/name_roi_norm")
        for index in range(4):
            s.setValue(
                f"multi/slot{index}/hwnd",
                self._multi_slot_hwnd(index) or "",
            )
            s.setValue(
                f"multi/slot{index}/record",
                self.combo_multi_records[index].currentData() or "",
            )
            s.setValue(
                f"multi/slot{index}/heal",
                self.combo_multi_heal_records[index].currentData() or "",
            )
            s.setValue(
                f"multi/slot{index}/farm_only",
                self.chk_multi_farm_only[index].isChecked(),
            )
            s.setValue(
                f"multi/slot{index}/threshold",
                int(self.spin_multi_thresholds[index].value()),
            )
            s.setValue(
                f"multi/slot{index}/potion",
                int(self.spin_multi_potions[index].value()),
            )
            s.setValue(
                f"multi/slot{index}/priority",
                int(self.spin_multi_priorities[index].value()),
            )

    # -- 多开挂机：回血录制 ----------------------------------------------
    def _toggle_multi_heal_record(self, index: int) -> None:
        if not (0 <= index < 4):
            return
        if self._heal_recorder is not None:
            if self._heal_record_index == index:
                self._stop_multi_heal_record()
            return
        if self._heal_record_pending:
            if self._heal_record_index == index:
                self._heal_record_pending = False
                self._heal_record_index = None
                self._set_multi_slot_text(index, "已取消录制回血")
                self._update_multi_controls()
            return
        if self._any_multi_running() or self._single_busy_for_multi():
            self._notify(
                "当前正在运行自动化",
                "请先停止多开/单开挂机，再录制回血。",
            )
            return
        hwnd = self._multi_slot_hwnd(index)
        if hwnd is None or not self._window_alive(hwnd):
            self._notify("没有有效窗口", "请先选择游戏窗口。")
            return
        self._heal_record_index = index
        self._heal_record_pending = True
        self._set_multi_slot_text(index, "3 秒后开始录制回血，请切到游戏…")
        self._update_multi_controls()
        QTimer.singleShot(3000, self._begin_multi_heal_record)

    def _begin_multi_heal_record(self) -> None:
        if not self._heal_record_pending or self._quitting:
            return
        self._heal_record_pending = False
        index = self._heal_record_index
        if index is None or not (0 <= index < 4):
            return
        hwnd = self._multi_slot_hwnd(index)
        if hwnd is None or not self._window_alive(hwnd):
            self._heal_record_index = None
            self._set_multi_slot_text(index, "窗口已关闭，录制取消")
            self._update_multi_controls()
            return
        try:
            recorder = MacroRecorder(hwnd)
            recorder.start()
        except Exception as exc:
            self._heal_record_index = None
            self._set_multi_slot_text(index, f"回血录制启动失败：{exc}")
            self._notify("回血录制启动失败", str(exc))
            self._update_multi_controls()
            return
        self._heal_recorder = recorder
        self._set_multi_slot_text(
            index, "回血录制中… 操作完切回本窗口点“停止录制”"
        )
        self._update_multi_controls()

    def _stop_multi_heal_record(self) -> None:
        recorder = self._heal_recorder
        index = self._heal_record_index
        self._heal_recorder = None
        self._heal_record_index = None
        if recorder is None:
            return
        try:
            events = strip_hotkey_tail(recorder.stop())
            meta = recorder.meta()
        except Exception as exc:
            if index is not None:
                self._set_multi_slot_text(index, f"停止回血录制失败：{exc}")
            self._update_multi_controls()
            return
        self._update_multi_controls()
        if index is None:
            return
        if not events:
            self._set_multi_slot_text(index, "没有录到操作，未保存")
            self._notify(
                "没有录到操作",
                "录制时游戏必须在前台，并且鼠标要位于游戏窗口内。",
            )
            return

        default_name = f"回血录制 {len(self._heal_records) + 1}"
        name, ok = QInputDialog.getText(
            self, "保存回血录制", "给这段回血录制起个名字：", text=default_name
        )
        if not ok:
            self._set_multi_slot_text(index, "已放弃保存回血录制")
            return
        name = str(name or "").strip() or default_name
        existing = {str(r.get("name")) for r in self._heal_records}
        base = name
        suffix = 2
        while name in existing:
            name = f"{base} ({suffix})"
            suffix += 1
        self._heal_records.append(
            {
                "name": name,
                "created": datetime.datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "events": events,
                "meta": meta,
            }
        )
        saved = save_heal_records(self._heal_records)
        self._refresh_multi_records()
        pos = self.combo_multi_heal_records[index].findData(name)
        if pos >= 0:
            self.combo_multi_heal_records[index].setCurrentIndex(pos)
        self._save_multi_settings()
        self._set_multi_slot_text(
            index,
            f"已保存回血录制“{name}”（{len(events)} 个动作）"
            if saved
            else f"回血录制“{name}”已生效，但写入失败（本次有效）",
        )
        self._update_multi_controls()

    def _test_multi_heal(self, index: int) -> None:
        if not (0 <= index < 4):
            return
        if self._any_multi_running() or self._multi_heal_recording():
            self._notify(
                "多开正在运行",
                "请先停止多开，再试放回血录制。",
            )
            return
        if self._single_busy_for_multi():
            self._notify(
                "单开自动化正在运行",
                "请先停止单开挂机/自动BOSS，再试放回血录制。",
            )
            return
        record = self._heal_record_data(index)
        if record is None or not record.get("events"):
            self._notify("没有回血录制", "请先录制或选择回血录制。")
            return
        hwnd = self._multi_slot_hwnd(index)
        if hwnd is None or not self._window_alive(hwnd):
            self._notify("没有有效窗口", "请先选择游戏窗口。")
            return
        player = MacroPlayer(
            int(hwnd),
            "试放回血",
            list(record.get("events") or []),
            meta=record.get("meta"),
            speed=1.0,
            background=(
                self.chk_multi_background.isChecked()
                or self.chk_multi_keyboard_only.isChecked()
            ),
            relative_mouse=self.chk_multi_relative_mouse.isChecked(),
            keyboard_only=self.chk_multi_keyboard_only.isChecked(),
            parent=self,
        )
        player.done.connect(self._on_multi_heal_test_done)
        player.error.connect(self._on_multi_heal_test_error)
        self._heal_test_player = player
        self._heal_test_index = index
        self._set_multi_slot_text(index, "试放回血录制…")
        player.start()
        self._update_multi_controls()

    def _on_multi_heal_test_done(self, _stage: str) -> None:
        player = self.sender()
        if player is not self._heal_test_player:
            return
        self._heal_test_player = None
        index = self._heal_test_index
        self._heal_test_index = None
        if player is not None:
            try:
                player.wait(500)
            except Exception:
                pass
            player.deleteLater()
        if index is not None:
            self._set_multi_slot_text(index, "回血试放完成")
        self._update_multi_controls()

    def _on_multi_heal_test_error(self, _stage: str, message: str) -> None:
        player = self.sender()
        if player is not self._heal_test_player:
            return
        self._heal_test_player = None
        index = self._heal_test_index
        self._heal_test_index = None
        if player is not None:
            try:
                player.wait(500)
            except Exception:
                pass
            player.deleteLater()
        if index is not None:
            self._set_multi_slot_text(index, f"回血试放失败：{message}")
        self._update_multi_controls()

    def _stop_multi_heal_test(self) -> None:
        player = self._heal_test_player
        self._heal_test_player = None
        self._heal_test_index = None
        if player is not None:
            try:
                player.stop()
                if not player.wait(3000):
                    player.terminate()
                    player.wait(500)
            except Exception:
                pass
            player.deleteLater()

    def _delete_multi_heal(self, index: int) -> None:
        if not (0 <= index < 4):
            return
        record = self._heal_record_data(index)
        if record is None:
            return
        if self._any_multi_running() or self._multi_heal_recording():
            self._notify("多开正在运行", "请先停止多开，再删除回血录制。")
            return
        name = str(record.get("name") or "")
        answer = QMessageBox.question(
            self,
            "删除回血录制",
            f"确定删除回血录制“{name}”吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._heal_records = [
            item for item in self._heal_records if item is not record
        ]
        save_heal_records(self._heal_records)
        self._refresh_multi_records()
        self._save_multi_settings()
        self._set_multi_slot_text(index, f"已删除回血录制“{name}”")
        self._update_multi_controls()

    @staticmethod
    def _make_icon() -> QIcon:
        """用 QPainter 画一个图标，免去外部资源文件。"""
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#c0392b"))
        painter.drawRoundedRect(2, 2, 60, 60, 14, 14)

        # 白色键帽 + 红色 A
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(12, 21, 40, 30, 7, 7)
        painter.setPen(QColor("#c0392b"))
        font = QFont("Segoe UI", 20)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            QRect(12, 21, 40, 30),
            Qt.AlignmentFlag.AlignCenter,
            "A",
        )
        painter.end()
        return QIcon(pixmap)

    # -- 绑定窗口 ---------------------------------------------------------
    def refresh_windows(self) -> None:
        """使用 win32gui.EnumWindows 枚举可见窗口。"""
        own_hwnd = 0
        try:
            own_hwnd = int(self.winId())
        except Exception:
            pass

        found: list[tuple[str, int]] = []

        def enum_callback(hwnd: int, _lparam) -> bool:
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                if int(hwnd) == own_hwnd:
                    return True
                title = win32gui.GetWindowText(hwnd).strip()
                if title and WINDOW_TITLE_KEYWORD.lower() in title.lower():
                    found.append((title, int(hwnd)))
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(enum_callback, None)
        except Exception as exc:
            self._set_status(f"枚举窗口失败：{exc}", "error")
            return

        self.combo_windows.blockSignals(True)
        self.combo_windows.clear()

        if found:
            # 去重并排序
            unique = {(title, hwnd) for title, hwnd in found}
            found = sorted(unique, key=lambda item: (item[0].lower(), item[1]))
            for title, hwnd in found:
                self.combo_windows.addItem(f"{title} [0x{hwnd:X}]", hwnd)

            # 如果之前绑定过，刷新后尽量保持选中
            if self.target_hwnd is not None:
                index = self.combo_windows.findData(self.target_hwnd)
                if index >= 0:
                    self.combo_windows.setCurrentIndex(index)

            self._set_status(
                f"找到 {len(found)} 个 CreatureCurios 窗口，请选择后点击“绑定”。",
                "ok",
            )
        else:
            self.combo_windows.addItem(
                "（未找到标题包含 CreatureCurios 的可见窗口）", None
            )
            self._set_status(
                "未找到 CreatureCurios 窗口，请先启动游戏，再点击“刷新”。",
                "warn",
            )

        self.combo_windows.blockSignals(False)
        self._update_bind_button_state()
        if hasattr(self, "combo_multi_windows"):
            self._refresh_multi_windows()

        # 已绑定的窗口若被关闭，自动清除绑定
        if self.target_hwnd is not None and not self._window_alive(self.target_hwnd):
            self._clear_binding(update_status=False)

    def _bind_selected_window(self) -> None:
        hwnd = self.combo_windows.currentData()
        if hwnd is None:
            QMessageBox.information(
                self,
                "没有可绑定的窗口",
                "请先启动 CreatureCurios，点击“刷新”，在下拉框中选择目标窗口。",
            )
            return

        hwnd = int(hwnd)
        if not self._window_alive(hwnd):
            QMessageBox.warning(
                self,
                "窗口已失效",
                "该窗口句柄已失效，可能游戏已经关闭。请重新点击“刷新”。",
            )
            self.refresh_windows()
            return

        try:
            title = win32gui.GetWindowText(hwnd).strip()
        except Exception:
            title = ""
        if not title:
            title = self.combo_windows.currentText().split(" [0x", 1)[0]

        self.target_hwnd = hwnd
        self.target_title = title
        self._set_bound_label(f"已绑定: {title} [0x{hwnd:X}]", "ok")
        self._set_status(f"已绑定窗口：{title} [0x{hwnd:X}]", "ok")
        self._update_bind_button_state()
        self._refresh_dungeon_tab()

    def _clear_binding(self, update_status: bool = True) -> None:
        self.target_hwnd = None
        self.target_title = ""
        self._set_bound_label("尚未绑定窗口", "error")
        if update_status:
            self._set_status("绑定已清除，请重新选择目标窗口。", "warn")
        self._update_bind_button_state()
        self._refresh_dungeon_tab()

    def _refresh_dungeon_tab(self) -> None:
        tab = getattr(self, "dungeon_tab", None)
        if tab is not None:
            try:
                tab._refresh_all()
            except Exception:
                pass

    @staticmethod
    def _log_startup(text: str) -> None:
        """把启动信息写到 startup.log，方便排查“某个页/功能没出来”。"""
        try:
            path = os.path.join(_data_folder(), "startup.log")
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"[{stamp}] {text}" + chr(10))
        except Exception:
            pass

    def _set_bound_label(self, text: str, state: str) -> None:
        self.bound_label.setText(text)
        self.bound_label.setProperty("state", state)
        style = self.bound_label.style()
        style.unpolish(self.bound_label)
        style.polish(self.bound_label)
        self.bound_label.update()

    @staticmethod
    def _window_alive(hwnd: int) -> bool:
        try:
            return bool(win32gui.IsWindow(hwnd))
        except Exception:
            return False

    # -- 运行控制 ---------------------------------------------------------
    def _start_sending(self) -> None:
        if self._worker is not None or self._quitting or self._boss_busy():
            return
        if self._multi_busy():
            self._notify(
                "多开正在运行",
                "请先停止多开挂机/回血录制，再启动单开挂机。",
            )
            return

        hwnd = self.target_hwnd
        if hwnd is None:
            self._notify(
                "尚未绑定窗口",
                "请先点击“刷新”，在下拉框中选择 CreatureCurios 窗口，再点击“绑定”。",
            )
            self._set_status("尚未绑定目标窗口。", "warn")
            return

        if not self._window_alive(hwnd):
            self._clear_binding(update_status=False)
            self.refresh_windows()
            self._notify(
                "目标窗口已关闭",
                "之前绑定的窗口句柄已失效，请重新选择并绑定窗口。",
            )
            return

        if self.chk_fatigue.isChecked() and self._fatigue_roi_norm is None:
            # 没框选区域就不启动疲劳检测，但攻击/技能/喝药照常运行
            self.chk_fatigue.blockSignals(True)
            self.chk_fatigue.setChecked(False)
            self.chk_fatigue.blockSignals(False)
            self.lbl_fatigue.setText("上次: 请先框选区域")
            self.lbl_fatigue.setStyleSheet("color: #ffb454; padding: 0;")
            self._set_status(
                "疲劳检测已跳过：请先点击“框选区域”框住右下角的疲劳提示按钮。", "warn"
            )
            self._popup.show_message(
                "疲劳检测未启用",
                "还没有框选疲劳提示区域。请先点击主界面的“框选区域”，"
                "在游戏截图上框住右下角会弹出疲劳提示的图标/按钮，"
                "然后重新勾选“疲劳检测”。",
                seconds=10,
            )

        hp_potion = self.chk_single_hp_potion.isChecked()
        if hp_potion and self._hp_roi_norm is None:
            self._notify(
                "尚未框选血量区域",
                "请先点“框选血量区域”，框住头像旁边的当前血量数字，"
                "再开始挂机。",
            )
            self._set_status("血量自动吃药缺少血量区域。", "warn")
            return

        self._last_failure = None
        self._last_rounds = 0
        self.rounds_label.setText("已发送 0 轮")

        worker = KeySender(
            hwnd,
            self.spin_interval.value(),
            use_skills=self.chk_skills.isChecked(),
            use_potion=self.chk_potion.isChecked() and not hp_potion,
            potion_interval_ms=self.spin_potion.value() * 1000,
            use_pickup=self.chk_pickup.isChecked(),
            parent=self,
        )
        worker.cycle_done.connect(self._on_cycle_done)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        self._stopping = False
        worker.start()
        if hp_potion:
            self._start_single_hp_monitor()

        # 疲劳检测只在发送期间运行；首次检测在 30 秒后
        if self.chk_fatigue.isChecked():
            self._start_fatigue_monitor()

        self.btn_start.setText("停止发送")
        self.btn_start.setEnabled(True)
        self.btn_start.setStyleSheet(START_BUTTON_STYLES["running"])
        self._set_running_controls(True)
        self._set_status(
            "正在后台发送 A → 空格；你可以正常使用其他窗口。", "ok"
        )
        self._update_tray_tooltip(True)
        self._update_toggle_action()

    def _stop_sending(self) -> None:
        if self._worker is None or self._stopping:
            return
        self._stopping = True
        self._worker.stop()
        self._stop_single_hp_monitor()
        self._stop_fatigue_monitor()

        self.btn_start.setText("正在停止…")
        self.btn_start.setEnabled(False)
        self.btn_start.setStyleSheet(START_BUTTON_STYLES["stopping"])
        self._set_status("正在停止，请稍候…", "warn")
        self._update_toggle_action()

    def _toggle_running(self) -> None:
        if self._quitting:
            return
        if self._boss_recorder is not None or self._boss_record_pending:
            self._toggle_boss_record()
            return
        if self._heal_recorder is not None or self._heal_record_pending:
            self._stop_multi_heal_record()
            return
        if self._heal_test_player is not None:
            index = self._heal_test_index
            self._stop_multi_heal_test()
            if index is not None:
                self._set_multi_slot_text(index, "已停止回血试放")
            self._update_multi_controls()
            return
        if self._any_multi_running():
            self._stop_all_multi()
            return
        if self._boss_busy():
            self._stop_boss("已停止 BOSS", "warn")
            return
        if self._stopping:
            return
        if self._worker is not None:
            self._stop_sending()
        else:
            self._start_sending()

    def _on_worker_finished(self) -> None:
        worker = self.sender()
        if worker is not self._worker:
            return
        self._worker = None

        rounds = worker.rounds if worker is not None else self._last_rounds
        self._last_rounds = rounds

        if worker is not None:
            worker.deleteLater()

        self._stop_single_hp_monitor()
        self._stop_fatigue_monitor()

        if self._quitting:
            return

        self._stopping = False
        self.btn_start.setEnabled(True)
        self.btn_start.setText("开始发送")
        self.btn_start.setStyleSheet(START_BUTTON_STYLES["idle"])
        self.rounds_label.setText(f"已发送 {rounds} 轮")
        self._set_running_controls(False)

        if self._last_failure:
            self._set_status(f"已停止（异常）：{self._last_failure}", "error")
        else:
            self._set_status(f"已停止：本次共发送 {rounds} 轮。", "normal")

        self._update_tray_tooltip(False)
        self._update_toggle_action()

    def _on_cycle_done(self, rounds: int) -> None:
        if self._quitting or self.sender() is not self._worker:
            return
        self._last_rounds = int(rounds)
        self.rounds_label.setText(f"已发送 {int(rounds)} 轮")
        if self._tray is not None and int(rounds) % 10 == 0:
            self._update_tray_tooltip(True)

    def _on_worker_failed(self, message: str) -> None:
        if self.sender() is not self._worker:
            return
        self._last_failure = message
        if self._quitting:
            return
        self._set_status(message, "error")
        if self._tray is not None and self._tray.isVisible():
            self._tray.showMessage(
                "发送已停止",
                message,
                QSystemTrayIcon.MessageIcon.Warning,
                4000,
            )

    def _on_interval_changed(self, value: int) -> None:
        if self._worker is not None:
            self._worker.set_interval(int(value))

    def _on_threshold_changed(self, value: int) -> None:
        value = max(50, min(1000, int(value)))
        self._fatigue_threshold = value
        try:
            self._settings.setValue(
                "fatigue_threshold", self._fatigue_threshold
            )
        except Exception:
            pass
        self._refresh_threshold_labels()
        # 正在做疲劳检测时，用新阈值重启监控
        if self._fatigue_monitor is not None:
            self._stop_fatigue_monitor()
            self._start_fatigue_monitor()

    def _refresh_threshold_labels(self) -> None:
        threshold = int(self._fatigue_threshold)
        if hasattr(self, "lbl_threshold_hint"):
            self.lbl_threshold_hint.setText(
                f"低于 -{threshold} / 高于 +{threshold} 弹窗"
            )

    # -- 辅助功能：技能 / 喝药 -------------------------------------------
    def _on_pickup_toggled(self, checked: bool) -> None:
        if self._worker is not None:
            self._worker.set_use_pickup(bool(checked))
        self._refresh_action_text()

    def _refresh_action_text(self) -> None:
        if not hasattr(self, "lbl_action_text") or not hasattr(self, "chk_pickup"):
            return
        pickup = self.chk_pickup.isChecked()
        text = "动作：A 按下 50ms → 松开"
        if pickup:
            text += " → 空格按下 50ms → 松开"
        else:
            text += "（不拾取）"
        self.lbl_action_text.setText(text)

    def _on_skills_toggled(self, checked: bool) -> None:
        if self._worker is not None:
            self._worker.set_use_skills(bool(checked))

    def _on_potion_toggled(self, checked: bool) -> None:
        if self._worker is not None:
            self._worker.set_use_potion(bool(checked))

    def _on_potion_interval_changed(self, seconds: int) -> None:
        if self._worker is not None:
            self._worker.set_potion_interval(int(seconds))

    # -- 疲劳检测 ---------------------------------------------------------
    def _current_fatigue_roi(self) -> Optional[tuple[int, int, int, int]]:
        if self.target_hwnd is None or self._fatigue_roi_norm is None:
            return None
        if not self._window_alive(self.target_hwnd):
            return None
        try:
            left, top, right, bottom = win32gui.GetClientRect(self.target_hwnd)
            cw, ch = int(right - left), int(bottom - top)
        except Exception:
            return None
        if cw <= 0 or ch <= 0:
            return None
        return roi_from_norm(self._fatigue_roi_norm, cw, ch)

    def _refresh_fatigue_roi_label(self) -> None:
        if not hasattr(self, "btn_fatigue_region"):
            return
        if self._fatigue_roi_norm is None:
            self.lbl_fatigue.setText("上次: 未设置区域")
            self.lbl_fatigue.setStyleSheet("color: #ffb454; padding: 0;")
            self.btn_fatigue_region.setText("框选区域")
            self.btn_fatigue_region.setToolTip(
                "点击后会在游戏截图上框选右下角的疲劳提示按钮/图标，区域会保存"
            )
            return

        roi = self._current_fatigue_roi()
        self.btn_fatigue_region.setText("重选区域")
        if roi is not None:
            self.btn_fatigue_region.setToolTip(
                f"当前区域: x={roi[0]}, y={roi[1]}, 宽={roi[2]}, 高={roi[3]}"
                "（会随游戏窗口尺寸缩放）。点击可重新框选。"
            )
        else:
            self.btn_fatigue_region.setToolTip("点击可重新框选疲劳提示区域")
        if not self.lbl_fatigue.text().startswith("上次: "):
            self.lbl_fatigue.setText("上次: --")

    def _capture_calibration_image(self) -> Optional[QImage]:
        """移动鼠标到游戏右下角、截图、恢复鼠标，然后把截图转为 QImage。"""
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return None

        saved_pos = win32api.GetCursorPos()
        anchor_pos = None
        cap = None
        err = ""
        try:
            left, top, right, bottom = win32gui.GetClientRect(self.target_hwnd)
            cw, ch = int(right - left), int(bottom - top)
            if cw <= 0 or ch <= 0:
                self._notify("截图失败", "目标窗口客户区尺寸无效。")
                return None
            saved_roi = self._current_fatigue_roi()
            if saved_roi is not None:
                client_x = saved_roi[0] + saved_roi[2] // 2
                client_y = saved_roi[1] + int(saved_roi[3] * 0.80)
            else:
                client_x = int(cw * 0.94)
                client_y = int(ch * 0.94)
            client_x = max(1, min(cw - 2, client_x))
            client_y = max(1, min(ch - 2, client_y))
            sx, sy = win32gui.ClientToScreen(self.target_hwnd, (client_x, client_y))
            anchor_pos = (int(sx), int(sy))
            win32api.SetCursorPos(anchor_pos)
            time.sleep(0.40)
            cap, err = capture_client_bgra(self.target_hwnd)
        finally:
            try:
                if anchor_pos is not None:
                    cur = win32api.GetCursorPos()
                    if abs(cur[0] - anchor_pos[0]) <= 3 and abs(cur[1] - anchor_pos[1]) <= 3:
                        win32api.SetCursorPos(saved_pos)
            except Exception:
                pass

        if cap is None:
            self._notify("截图失败", err or "无法截取游戏画面。")
            return None
        w, h, data = cap
        # GetDIBits 返回 BGRA；用 RGB32 显示，忽略可能为 0 的 alpha 字节
        return QImage(bytes(data), w, h, w * 4, QImage.Format.Format_RGB32).copy()

    def _select_fatigue_region(self) -> None:
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify(
                "尚未绑定窗口",
                "请先绑定 CreatureCurios 窗口，再框选疲劳提示区域。",
            )
            return

        image = self._capture_calibration_image()
        if image is None or image.isNull():
            return

        dialog = RegionSelectDialog(
            image,
            self._current_fatigue_roi(),
            self._capture_calibration_image,
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        roi = dialog.selected_roi()
        if roi is None:
            return

        # 用截图本身的尺寸归一化，避免框选期间窗口尺寸变化导致偏移
        cw, ch = image.width(), image.height()
        if cw <= 0 or ch <= 0:
            return
        self._fatigue_roi_norm = (
            roi[0] / cw,
            roi[1] / ch,
            roi[2] / cw,
            roi[3] / ch,
        )
        saved = save_fatigue_roi_norm(self._fatigue_roi_norm)
        self._refresh_fatigue_roi_label()
        self.lbl_fatigue.setText(
            "上次: 区域已保存" if saved else "上次: 区域已生效（本次运行）"
        )
        self.lbl_fatigue.setStyleSheet("color: #3ddc84; padding: 0;")
        self._set_status(
            "疲劳提示区域已保存，可以点“测试检测”验证。"
            if saved
            else "疲劳区域已生效，但配置文件写入失败（本次运行有效）。",
            "ok" if saved else "warn",
        )

        # 如果已经在发送且勾选了疲劳检测，用新区域启动/重启检测
        if self._fatigue_monitor is not None:
            self._stop_fatigue_monitor()
            self._start_fatigue_monitor()
        elif self.chk_fatigue.isChecked() and self._worker is not None:
            self._start_fatigue_monitor()

    def _on_fatigue_toggled(self, checked: bool) -> None:
        if checked:
            if self._fatigue_roi_norm is None:
                self.lbl_fatigue.setText("上次: 请先框选区域")
                self.lbl_fatigue.setStyleSheet("color: #ffb454; padding: 0;")
                self._set_status(
                    "疲劳检测需要先点击“框选区域”框住右下角的疲劳提示按钮。", "warn"
                )
                return
            if self._worker is not None:
                self._start_fatigue_monitor()
            else:
                self.lbl_fatigue.setText("上次: --（开始发送后生效）")
                self.lbl_fatigue.setStyleSheet("color: #8f959e; padding: 0;")
        else:
            self._stop_fatigue_monitor()
            self.lbl_fatigue.setText("上次: --")
            self.lbl_fatigue.setStyleSheet("color: #8f959e; padding: 0;")

    def _on_keyboard_only_toggled(self, checked: bool) -> None:
        if getattr(self, "_boss_loading", False):
            return
        if checked:
            if not self.chk_background_mode.isChecked():
                self.chk_background_mode.blockSignals(True)
                self.chk_background_mode.setChecked(True)
                self.chk_background_mode.blockSignals(False)
            if self.chk_relative_mouse.isChecked():
                self.chk_relative_mouse.blockSignals(True)
                self.chk_relative_mouse.setChecked(False)
                self.chk_relative_mouse.blockSignals(False)
            self._settings.setValue("background_mode", True)
            self._settings.setValue("relative_mouse", False)
            self._settings.setValue("keyboard_only", True)
            self._set_status(
                "静止鼠标/仅键盘：不移动鼠标、不抢前台，只发送键盘", "ok"
            )
        else:
            self._settings.setValue("keyboard_only", False)
            self._set_status("静止鼠标/仅键盘已关闭", "normal")

    def _on_background_mode_toggled(self, checked: bool) -> None:
        if getattr(self, "_boss_loading", False):
            return
        if not checked and self.chk_keyboard_only.isChecked():
            self.chk_keyboard_only.blockSignals(True)
            self.chk_keyboard_only.setChecked(False)
            self.chk_keyboard_only.blockSignals(False)
            self._settings.setValue("keyboard_only", False)
        if checked and self.chk_relative_mouse.isChecked():
            self.chk_relative_mouse.blockSignals(True)
            self.chk_relative_mouse.setChecked(False)
            self.chk_relative_mouse.blockSignals(False)
            self._settings.setValue("relative_mouse", False)
        try:
            self._settings.setValue("background_mode", bool(checked))
        except Exception:
            pass
        if self._fatigue_monitor is not None:
            self._stop_fatigue_monitor()
            self._start_fatigue_monitor()
        self._set_status(
            "后台模式：不抢鼠标键盘" if checked else "前台模式：回放会接管鼠标键盘",
            "ok" if checked else "warn",
        )

    def _on_relative_mouse_toggled(self, checked: bool) -> None:
        if getattr(self, "_boss_loading", False):
            return
        if checked and self.chk_keyboard_only.isChecked():
            self.chk_keyboard_only.blockSignals(True)
            self.chk_keyboard_only.setChecked(False)
            self.chk_keyboard_only.blockSignals(False)
            self._settings.setValue("keyboard_only", False)
        if checked and self.chk_background_mode.isChecked():
            self.chk_background_mode.blockSignals(True)
            self.chk_background_mode.setChecked(False)
            self.chk_background_mode.blockSignals(False)
            self._on_background_mode_toggled(False)
        self._settings.setValue("relative_mouse", bool(checked))
        self._set_status(
            "前台相对鼠标已开启：会移动真实鼠标并占用前台"
            if checked
            else "相对鼠标回放已关闭",
            "ok" if checked else "normal",
        )

    def _on_multi_keyboard_only_toggled(self, checked: bool) -> None:
        if getattr(self, "_multi_loading", False):
            return
        if checked:
            if not self.chk_multi_background.isChecked():
                self.chk_multi_background.blockSignals(True)
                self.chk_multi_background.setChecked(True)
                self.chk_multi_background.blockSignals(False)
            if self.chk_multi_relative_mouse.isChecked():
                self.chk_multi_relative_mouse.blockSignals(True)
                self.chk_multi_relative_mouse.setChecked(False)
                self.chk_multi_relative_mouse.blockSignals(False)
            self._settings.setValue("multi/background", True)
            self._settings.setValue("multi/relative_mouse", False)
            self._settings.setValue("multi/keyboard_only", True)
            self._set_status(
                "多开静止鼠标/仅键盘：不移动鼠标、不抢前台，只发送键盘",
                "ok",
            )
        else:
            self._settings.setValue("multi/keyboard_only", False)
            self._set_status("多开静止鼠标/仅键盘已关闭", "normal")
        self._update_multi_controls()

    def _on_multi_background_toggled(self, checked: bool) -> None:
        if getattr(self, "_multi_loading", False):
            return
        if not checked and self.chk_multi_keyboard_only.isChecked():
            self.chk_multi_keyboard_only.blockSignals(True)
            self.chk_multi_keyboard_only.setChecked(False)
            self.chk_multi_keyboard_only.blockSignals(False)
            self._settings.setValue("multi/keyboard_only", False)
        if checked and self.chk_multi_relative_mouse.isChecked():
            self.chk_multi_relative_mouse.blockSignals(True)
            self.chk_multi_relative_mouse.setChecked(False)
            self.chk_multi_relative_mouse.blockSignals(False)
            self._settings.setValue("multi/relative_mouse", False)
        self._settings.setValue("multi/background", bool(checked))
        self._set_status(
            "多开后台回放已开启，下次开始/试放生效"
            if checked
            else "多开后台回放已关闭，下次开始/试放生效",
            "ok" if checked else "warn",
        )
        self._update_multi_controls()

    def _on_multi_relative_mouse_toggled(self, checked: bool) -> None:
        if getattr(self, "_multi_loading", False):
            return
        if checked and self.chk_multi_keyboard_only.isChecked():
            self.chk_multi_keyboard_only.blockSignals(True)
            self.chk_multi_keyboard_only.setChecked(False)
            self.chk_multi_keyboard_only.blockSignals(False)
            self._settings.setValue("multi/keyboard_only", False)
        if checked and self.chk_multi_background.isChecked():
            self.chk_multi_background.blockSignals(True)
            self.chk_multi_background.setChecked(False)
            self.chk_multi_background.blockSignals(False)
            self._on_multi_background_toggled(False)
        self._settings.setValue("multi/relative_mouse", bool(checked))
        self._set_status(
            "多开前台相对鼠标已开启：会移动真实鼠标并占用前台"
            if checked
            else "多开相对鼠标回放已关闭",
            "ok" if checked else "normal",
        )

    def _on_single_hp_potion_toggled(self, checked: bool) -> None:
        if getattr(self, "_boss_loading", False):
            return
        self._save_boss_settings()
        if checked and self._hp_roi_norm is None:
            self._set_status(
                "血量自动吃药已开启，但还没框选血量区域", "warn"
            )

    def _refresh_single_hp_roi_label(self) -> None:
        if not hasattr(self, "lbl_single_hp_roi"):
            return
        if self._hp_roi_norm is None:
            self.lbl_single_hp_roi.setText("未设置")
            self.lbl_single_hp_roi.setStyleSheet(
                "color: #ffb454; padding: 0;"
            )
        else:
            self.lbl_single_hp_roi.setText("已设置")
            self.lbl_single_hp_roi.setStyleSheet(
                "color: #3ddc84; padding: 0;"
            )

    def _start_single_hp_monitor(self) -> None:
        if not self.chk_single_hp_potion.isChecked():
            return
        if self._single_hp_monitor is not None:
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            return
        if self._hp_roi_norm is None:
            return
        monitor = HpMonitor(
            self.target_hwnd,
            self._hp_roi_norm,
            1.0,
            parent=self,
        )
        monitor.result.connect(self._on_single_hp_result)
        self._single_hp_monitor = monitor
        monitor.start()

    def _stop_single_hp_monitor(self) -> None:
        monitor = self._single_hp_monitor
        self._single_hp_monitor = None
        if monitor is not None:
            try:
                monitor.stop()
                if not monitor.wait(3000):
                    monitor.terminate()
                    monitor.wait(500)
            except Exception:
                pass
            monitor.deleteLater()

    def _on_single_hp_result(self, current, maximum, message: str) -> None:
        if not hasattr(self, "lbl_single_hp_value"):
            return
        if current is None:
            self.lbl_single_hp_value.setText(
                f"血量: {message or '--'}"
            )
            self.lbl_single_hp_value.setStyleSheet(
                "color: #8f959e; padding: 0;"
            )
            return
        try:
            cur = int(current)
        except (TypeError, ValueError):
            return
        text = (
            f"血量: {cur}/{int(maximum)}"
            if maximum is not None
            else f"血量: {cur}"
        )
        threshold = int(self.spin_single_hp_potion_threshold.value())
        if cur < threshold and self.chk_single_hp_potion.isChecked():
            text += (
                f"（自动吃药中：每 {int(self.spin_potion.value())} 秒 Q+W）"
            )
        self.lbl_single_hp_value.setText(text)
        if cur < threshold:
            self.lbl_single_hp_value.setStyleSheet(
                "color: #ff6b6b; padding: 0;"
            )
        else:
            self.lbl_single_hp_value.setStyleSheet(
                "color: #3ddc84; padding: 0;"
            )
        if cur < threshold and self._worker is not None:
            try:
                self._worker.trigger_potion()
            except Exception:
                pass

    def _start_fatigue_monitor(self) -> None:
        if self._quitting or not self.chk_fatigue.isChecked():
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            return
        if self._fatigue_roi_norm is None:
            self.lbl_fatigue.setText("上次: 请先框选区域")
            self.lbl_fatigue.setStyleSheet("color: #ffb454; padding: 0;")
            return
        if self._fatigue_monitor is not None:
            return

        monitor = FatigueMonitor(
            self.target_hwnd,
            roi_norm=self._fatigue_roi_norm,
            interval_seconds=FATIGUE_CHECK_SECONDS,
            one_shot=False,
            alert_below=-float(self._fatigue_threshold),
            alert_above=float(self._fatigue_threshold),
            background=True,
            borrow_mouse=self.chk_fatigue_borrow_mouse.isChecked(),
            parent=self,
        )
        monitor.result.connect(self._on_fatigue_result)
        monitor.alert.connect(self._on_fatigue_alert)
        monitor.finished.connect(self._on_fatigue_monitor_finished)
        self._fatigue_monitor = monitor
        self.lbl_fatigue.setText("上次: 等待检测…")
        self.lbl_fatigue.setStyleSheet("color: #8f959e; padding: 0;")
        monitor.start()

    def _stop_fatigue_monitor(self) -> None:
        monitor = self._fatigue_monitor
        self._fatigue_monitor = None
        if monitor is not None:
            monitor.stop()
            if not monitor.wait(2500):
                monitor.terminate()
                monitor.wait(500)
            monitor.deleteLater()

    def _on_fatigue_monitor_finished(self) -> None:
        monitor = self.sender()
        if monitor is self._fatigue_monitor:
            self._fatigue_monitor = None
            if isinstance(monitor, FatigueMonitor):
                monitor.deleteLater()

    def _on_fatigue_result(self, current, maximum, message: str) -> None:
        if self._quitting:
            return
        if current is None:
            skipped = any(
                word in str(message)
                for word in ("跳过", "关闭", "最小化", "尚未框选")
            )
            self.lbl_fatigue.setText("上次: 跳过" if skipped else "上次: 识别失败")
            self.lbl_fatigue.setStyleSheet("color: #ffb454; padding: 0;")
            self.lbl_fatigue.setToolTip(str(message))
            return

        threshold = float(self._fatigue_threshold)
        over_threshold = current < -threshold or current > threshold
        color = "#ff6b6b" if over_threshold else "#3ddc84"
        max_text = f"/{int(maximum)}" if maximum is not None else ""
        self.lbl_fatigue.setText(f"上次: {int(current)}{max_text}")
        self.lbl_fatigue.setStyleSheet(f"color: {color}; padding: 0;")
        self.lbl_fatigue.setToolTip(str(message))

        if (
            self.sender() is self._fatigue_monitor
            and current < -threshold
            and self.chk_boss_fatigue.isChecked()
            and not self._boss_busy()
            and self._boss_config_ready(silent=True)
        ):
            QTimer.singleShot(0, self._on_fatigue_boss_trigger)

    def _on_fatigue_boss_trigger(self) -> None:
        if self._quitting or self._boss_busy():
            return
        if not self.chk_boss_fatigue.isChecked():
            return
        if not self._boss_config_ready(silent=True):
            self._set_boss_state(
                "疲劳已到阈值，但 BOSS 方案未配置完整", "warn"
            )
            return
        self._boss_start(origin="fatigue", repeat_override=1)

    def _on_fatigue_alert(self, message: str) -> None:
        if self._quitting or getattr(self, "_popup", None) is None:
            return
        if (
            "低于" in str(message)
            and self.chk_boss_fatigue.isChecked()
            and self._boss_config_ready(silent=True)
        ):
            self._set_boss_state("疲劳已到阈值，准备自动打BOSS…", "warn")
            return
        self._popup.show_message("疲劳值提醒", message, seconds=20)
        self.lbl_fatigue.setStyleSheet("color: #ff6b6b; padding: 0;")

    def _on_fatigue_borrow_toggled(self, checked: bool) -> None:
        self._fatigue_borrow_mouse = bool(checked)
        try:
            self._settings.setValue("fatigue_borrow_mouse", bool(checked))
        except Exception:
            pass
        # 正在做疲劳检测时，用新模式重启监控
        if self._fatigue_monitor is not None:
            self._stop_fatigue_monitor()
            self._start_fatigue_monitor()
        if checked:
            self._set_status(
                "疲劳检测：后台点击读不到时会短暂借用鼠标（约 1 秒，随后还原）",
                "warn",
            )
        else:
            self._set_status(
                "疲劳检测：只用后台点击，不会移动鼠标、不抢前台", "ok"
            )

    def _test_fatigue_check(self) -> None:
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify(
                "尚未绑定窗口",
                "请先绑定 CreatureCurios 窗口，再测试疲劳检测。",
            )
            return

        if self._fatigue_roi_norm is None:
            self._notify(
                "尚未框选疲劳提示区域",
                "请先点击“框选区域”，在游戏截图上框住右下角会弹出疲劳提示的图标/按钮，再测试检测。",
            )
            return

        # 已有定时检测线程：立刻插一次检测
        if self._fatigue_monitor is not None:
            self._fatigue_monitor.request_check()
            self._set_status(
                "正在执行疲劳检测（后台点击，不移动鼠标）…", "warn"
            )
            return
        if self._test_monitor is not None:
            return

        monitor = FatigueMonitor(
            self.target_hwnd,
            roi_norm=self._fatigue_roi_norm,
            interval_seconds=FATIGUE_CHECK_SECONDS,
            one_shot=True,
            alert_below=-float(self._fatigue_threshold),
            alert_above=float(self._fatigue_threshold),
            background=True,
            borrow_mouse=self.chk_fatigue_borrow_mouse.isChecked(),
            parent=self,
        )
        monitor.result.connect(self._on_fatigue_result)
        monitor.alert.connect(self._on_fatigue_alert)
        monitor.finished.connect(self._on_test_monitor_finished)
        self._test_monitor = monitor
        self.btn_fatigue_test.setEnabled(False)
        self.btn_fatigue_test.setText("检测中…")
        self.lbl_fatigue.setText("上次: 检测中…")
        self.lbl_fatigue.setStyleSheet("color: #8f959e; padding: 0;")
        monitor.start()

    def _on_test_monitor_finished(self) -> None:
        monitor = self.sender()
        if monitor is self._test_monitor:
            self._test_monitor = None
        if isinstance(monitor, FatigueMonitor):
            monitor.deleteLater()
        self.btn_fatigue_test.setEnabled(True)
        self.btn_fatigue_test.setText("测试检测")

    # -- 定时提醒 ---------------------------------------------------------
    def _on_timer_mode_changed(self, _index: int) -> None:
        self._update_timer_mode_ui()
        self._reset_timer_state()

    def _update_timer_mode_ui(self) -> None:
        if not hasattr(self, "combo_timer_mode"):
            return
        countdown = self.combo_timer_mode.currentData() == "countdown"
        self.spin_timer_min.setVisible(countdown)
        self.time_timer.setVisible(not countdown)

    def _reset_timer_state(self, *_args) -> None:
        if not hasattr(self, "chk_timer"):
            return
        if not self.chk_timer.isChecked():
            self._timer_next_mono = None
            self._timer_next_dt = None
            self._update_timer_label()
            return

        if self.combo_timer_mode.currentData() == "countdown":
            minutes = max(1, int(self.spin_timer_min.value()))
            self._timer_next_mono = time.monotonic() + minutes * 60
            self._timer_next_dt = None
        else:
            now = datetime.datetime.now()
            qtime = self.time_timer.time()
            target = now.replace(
                hour=qtime.hour(), minute=qtime.minute(), second=0, microsecond=0
            )
            if target <= now:
                target += datetime.timedelta(days=1)
            self._timer_next_dt = target
            self._timer_next_mono = None
        self._update_timer_label()

    def _on_ui_tick(self) -> None:
        if self._boss_recorder is not None:
            try:
                self._set_boss_state(
                    f"录制中… {self._boss_recorder.event_count()} 个动作 / "
                    f"{self._boss_recorder.duration():.1f} 秒",
                    "warn",
                )
            except Exception:
                pass
        if (
            self._heal_recorder is not None
            and self._heal_record_index is not None
        ):
            try:
                self._set_multi_slot_text(
                    self._heal_record_index,
                    f"回血录制中… {self._heal_recorder.event_count()} 个动作 / "
                    f"{self._heal_recorder.duration():.1f} 秒",
                )
            except Exception:
                pass
        if self._quitting or not self.chk_timer.isChecked():
            return

        now_mono = time.monotonic()
        now_dt = datetime.datetime.now()
        if self._timer_next_mono is not None:
            if now_mono >= self._timer_next_mono:
                self._fire_timer_reminder()
                minutes = max(1, int(self.spin_timer_min.value()))
                self._timer_next_mono = now_mono + minutes * 60
            self._update_timer_label(now_mono=now_mono)
        elif self._timer_next_dt is not None:
            if now_dt >= self._timer_next_dt:
                self._fire_timer_reminder()
                self._timer_next_dt += datetime.timedelta(days=1)
            self._update_timer_label(now_dt=now_dt)

    def _fire_timer_reminder(self) -> None:
        if getattr(self, "_popup", None) is None:
            return
        if self._timer_next_mono is not None:
            message = f"倒计时 {int(self.spin_timer_min.value())} 分钟到了。"
        else:
            message = f"指定时刻 {self.time_timer.time().toString('HH:mm')} 到了。"
        self._popup.show_message("定时提醒", message, seconds=25)

    def _update_timer_label(
        self,
        now_mono: Optional[float] = None,
        now_dt: Optional[datetime.datetime] = None,
    ) -> None:
        if self._timer_next_mono is not None:
            now = time.monotonic() if now_mono is None else now_mono
            remaining = max(0, int(self._timer_next_mono - now + 0.999))
            minutes, seconds = divmod(remaining, 60)
            self.lbl_timer_next.setText(f"距下次: {minutes:02d}:{seconds:02d}")
            self.lbl_timer_next.setStyleSheet("color: #8ab4f8; padding: 0;")
        elif self._timer_next_dt is not None:
            now = datetime.datetime.now() if now_dt is None else now_dt
            prefix = "今天" if self._timer_next_dt.date() == now.date() else "明天"
            self.lbl_timer_next.setText(
                f"下次: {prefix} {self._timer_next_dt.strftime('%H:%M')}"
            )
            self.lbl_timer_next.setStyleSheet("color: #8ab4f8; padding: 0;")
        else:
            self.lbl_timer_next.setText("未启用")
            self.lbl_timer_next.setStyleSheet("color: #8f959e; padding: 0;")

    def _show_test_notice(self) -> None:
        self._popup.show_message(
            "测试提醒",
            "这是一个测试提醒。点击弹窗即可关闭，不会抢占当前窗口焦点。",
            seconds=10,
        )

    # -- 控件状态 ---------------------------------------------------------
    def _set_running_controls(self, running: bool) -> None:
        blocked = bool(running or self._boss_busy() or self._multi_busy())
        self.combo_windows.setEnabled(not blocked)
        self.btn_refresh.setEnabled(not blocked)
        for widget in (
            getattr(self, "chk_single_hp_potion", None),
            getattr(self, "spin_single_hp_potion_threshold", None),
            getattr(self, "btn_single_hp_roi", None),
        ):
            if widget is not None:
                widget.setEnabled(not running)
        self._update_bind_button_state()
        self._update_boss_controls()
        self._update_multi_controls()
        self._update_toggle_action()

    def _update_bind_button_state(self) -> None:
        running = (
            self._worker is not None
            or self._boss_busy()
            or self._multi_busy()
        )
        has_selection = self.combo_windows.currentData() is not None
        self.btn_bind.setEnabled((not running) and has_selection)
        self.combo_windows.setEnabled(not running)
        self.btn_refresh.setEnabled(not running)

    def _update_toggle_action(self) -> None:
        if self._act_toggle is None:
            return
        if self._boss_recorder is not None or self._boss_record_pending:
            self._act_toggle.setText("停止录制")
            self._act_toggle.setEnabled(True)
        elif self._heal_recorder is not None or self._heal_record_pending:
            self._act_toggle.setText("停止录制回血")
            self._act_toggle.setEnabled(True)
        elif self._any_multi_running():
            self._act_toggle.setText("停止多开")
            self._act_toggle.setEnabled(True)
        elif self._stopping:
            self._act_toggle.setText("正在停止…")
            self._act_toggle.setEnabled(False)
        elif self._boss_busy():
            self._act_toggle.setText("停止BOSS")
            self._act_toggle.setEnabled(True)
        elif self._worker is not None:
            self._act_toggle.setText("停止发送")
            self._act_toggle.setEnabled(True)
        else:
            self._act_toggle.setText("开始发送")
            self._act_toggle.setEnabled(True)

    # -- 状态栏 -----------------------------------------------------------
    def _set_status(self, text: str, level: str = "normal") -> None:
        colors = {
            "normal": "#aeb4bd",
            "ok": "#3ddc84",
            "warn": "#ffb454",
            "error": "#ff6b6b",
        }
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"color: {colors.get(level, colors['normal'])}; padding: 3px 2px;"
        )

    def _update_tray_tooltip(self, running: bool) -> None:
        if self._tray is None:
            return
        if running:
            self._tray.setToolTip(
                f"{APP_NAME}\n运行中 · 已发送 {self._last_rounds} 轮\n"
                f"热键: {HOTKEY_TEXT}"
            )
        else:
            self._tray.setToolTip(f"{APP_NAME}\n已停止\n热键: {HOTKEY_TEXT}")

    def _notify(self, title: str, text: str) -> None:
        """窗口可见时用消息框；隐藏到托盘时用托盘气泡，避免把窗口强行拉出来。"""
        if self.isVisible() and not self.isMinimized():
            QMessageBox.information(self, title, text)
        elif self._tray is not None:
            self._tray.show()
            self._tray.showMessage(
                title, text, QSystemTrayIcon.MessageIcon.Information, 4000
            )

    # -- 系统托盘 ---------------------------------------------------------
    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = None
            return

        self._tray = QSystemTrayIcon(self._make_icon(), self)
        self._tray.setToolTip(f"{APP_NAME}\n热键: {HOTKEY_TEXT}")

        menu = QMenu(self)
        self._act_show = menu.addAction("显示主界面")
        self._act_toggle = menu.addAction("开始发送")
        menu.addSeparator()
        self._act_quit = menu.addAction("退出")

        self._act_show.triggered.connect(self._restore_window)
        self._act_toggle.triggered.connect(self._toggle_running)
        self._act_quit.triggered.connect(self._quit_app)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.hide()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self._restore_window()

    def _hide_to_tray(self) -> None:
        if self._quitting or self._tray is None:
            return

        if self.isVisible():
            self.hide()
        self._tray.show()

        if not self._tray_hint_shown:
            self._tray.showMessage(
                APP_NAME,
                "已最小化到系统托盘。双击图标恢复界面，"
                f"{HOTKEY_TEXT} 可随时开始/停止。",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
            self._tray_hint_shown = True

    def _restore_window(self) -> None:
        if self._quitting:
            return
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self._tray is not None:
            self._tray.hide()

    # -- 全局热键 ---------------------------------------------------------
    def _install_hotkey(self) -> None:
        self.hotkey_triggered.connect(
            self._toggle_running, Qt.ConnectionType.QueuedConnection
        )

        error: Optional[Exception] = KEYBOARD_IMPORT_ERROR
        if keyboard is not None:
            try:
                self._hotkey_handle = keyboard.add_hotkey(
                    HOTKEY_ID,
                    self.hotkey_triggered.emit,
                    suppress=False,
                )
            except Exception as exc:
                error = exc

        if self._hotkey_handle is None:
            # 全局热键失败时，至少保证窗口在前台时快捷键可用
            self._local_shortcut = QShortcut(QKeySequence(HOTKEY_QT), self)
            self._local_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            self._local_shortcut.activated.connect(self._toggle_running)
            reason = f"：{error}" if error else ""
            self._set_status(
                f"全局热键注册失败{reason}。窗口内热键仍可用。", "warn"
            )
            self.hint_label.setText(
                f"快捷键: {HOTKEY_TEXT} 开始/停止（全局注册失败，目前仅在窗口内有效）；"
                "最小化后驻留系统托盘。"
            )
        else:
            self.hint_label.setText(
                f"快捷键: {HOTKEY_TEXT} 开始/停止；最小化后驻留系统托盘。"
            )

    # -- 退出与窗口事件 ---------------------------------------------------
    def _cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        global _FOREGROUND_BRIDGE
        _FOREGROUND_BRIDGE = None

        if self._hotkey_handle is not None and keyboard is not None:
            try:
                keyboard.remove_hotkey(self._hotkey_handle)
            except Exception:
                pass
            self._hotkey_handle = None

        if self._local_shortcut is not None:
            try:
                self._local_shortcut.setEnabled(False)
            except Exception:
                pass
            self._local_shortcut = None

        for monitor_attr in (
            "_fatigue_monitor",
            "_test_monitor",
            "_single_hp_monitor",
        ):
            monitor = getattr(self, monitor_attr, None)
            setattr(self, monitor_attr, None)
            if monitor is not None:
                monitor.stop()
                if not monitor.wait(2500):
                    monitor.terminate()
                    monitor.wait(500)

        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.stop()
            if not worker.wait(2500):
                # 极端情况下强制结束，保证进程能退出
                worker.terminate()
                worker.wait(500)

        self._boss_running = False
        self._boss_preview = False
        self._boss_gap_timer.stop()
        self._boss_fight_timer.stop()
        self._boss_trigger_timer.stop()
        self._boss_player_stop()
        self._stop_boss_watcher()
        self._stop_boss_fighter()
        self._stop_boss_looter()
        recorder = self._boss_recorder
        self._boss_recorder = None
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:
                pass

        heal_recorder = self._heal_recorder
        self._heal_recorder = None
        self._heal_record_pending = False
        self._heal_record_index = None
        if heal_recorder is not None:
            try:
                heal_recorder.stop()
            except Exception:
                pass
        self._stop_multi_heal_test()
        self._stop_name_readers()
        for index in range(4):
            runner = self._multi_runners[index]
            self._multi_runners[index] = None
            if runner is not None:
                for sig_name in ("status", "hp_changed", "finished"):
                    try:
                        getattr(runner, sig_name).disconnect()
                    except Exception:
                        pass
                try:
                    runner.stop("已停止", "warn")
                except Exception:
                    pass
                try:
                    runner.deleteLater()
                except Exception:
                    pass

        if getattr(self, "_popup", None) is not None:
            self._popup.hide()
            self._popup.close()
            self._popup = None

        if self._tray is not None:
            self._tray.hide()

    def _quit_app(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._cleanup()
        QApplication.quit()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._save_boss_settings()
        self._save_multi_settings()
        if self._quitting:
            event.accept()
            return

        if self._tray is not None:
            # 点右上角 X 只隐藏到托盘，不退出程序
            event.ignore()
            self._hide_to_tray()
            return

        # 没有系统托盘时，关闭窗口即退出
        self._quitting = True
        self._cleanup()
        event.accept()
        QApplication.quit()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if (
            event.type() == QEvent.Type.WindowStateChange
            and self.isMinimized()
            and self._tray is not None
            and not self._quitting
        ):
            # singleShot 避免在状态切换过程中直接 hide 引发的绘制/状态问题
            QTimer.singleShot(0, self._hide_to_tray)
        super().changeEvent(event)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _run_ocr_selftest(image_path: str, out_path: str) -> int:
    """隐藏的诊断入口：验证打包后的 OCR 是否可用。"""
    nl = chr(10)
    try:
        import numpy as np
        from PIL import Image

        image = Image.open(image_path).convert("RGB")
        w, h = image.size
        rgb = np.array(image)
        bgra = np.zeros((h, w, 4), dtype=np.uint8)
        bgra[:, :, 0] = rgb[:, :, 2]
        bgra[:, :, 1] = rgb[:, :, 1]
        bgra[:, :, 2] = rgb[:, :, 0]
        bgra[:, :, 3] = 255
        norm = load_fatigue_roi_norm()
        roi = roi_from_norm(norm, w, h) if norm is not None else (0, 0, w, h)
        current, maximum, msg = ocr_fatigue_from_bgra(
            bgra.tobytes(), w, h, roi
        )
        result = "current={}{}maximum={}{}message={}{}".format(
            current, nl, maximum, nl, msg, nl
        )
    except Exception as exc:
        result = "ERROR {}: {}{}".format(type(exc).__name__, exc, nl)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(result)
    except Exception:
        pass
    return 0


_CRASH_LOG_HANDLE = None


def _install_crash_logging() -> None:
    global _CRASH_LOG_HANDLE
    try:
        _CRASH_LOG_HANDLE = open(
            os.path.join(_data_folder(), "crash.log"),
            "a",
            encoding="utf-8",
            buffering=1,
        )
        faulthandler.enable(file=_CRASH_LOG_HANDLE, all_threads=True)
    except Exception:
        _CRASH_LOG_HANDLE = None

    def _write_exception(exc_type, exc_value, exc_tb, prefix="") -> None:
        try:
            with open(
                os.path.join(_data_folder(), "crash.log"),
                "a",
                encoding="utf-8",
            ) as f:
                f.write(
                    chr(10)
                    + "=== "
                    + datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    + " "
                    + prefix
                    + " ==="
                    + chr(10)
                )
                traceback.print_exception(
                    exc_type, exc_value, exc_tb, file=f
                )
        except Exception:
            pass

    def _sys_hook(exc_type, exc_value, exc_tb):
        _write_exception(exc_type, exc_value, exc_tb, "UNHANDLED")

    def _thread_hook(args):
        _write_exception(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            f"THREAD {args.thread.name if args.thread else ''}",
        )

    sys.excepthook = _sys_hook
    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass


def main() -> int:
    _install_crash_logging()
    if "--ocr-selftest" in sys.argv:
        try:
            index = sys.argv.index("--ocr-selftest")
            image_path = sys.argv[index + 1]
            out_path = (
                sys.argv[index + 2]
                if len(sys.argv) > index + 2
                else "ocr_selftest_result.txt"
            )
            return _run_ocr_selftest(image_path, out_path)
        except Exception as exc:
            try:
                with open("ocr_selftest_result.txt", "w", encoding="utf-8") as f:
                    f.write("ERROR {}: {}{}".format(type(exc).__name__, exc, chr(10)))
            except Exception:
                pass
            return 1

    # 提高定时器精度，减少回放事件的时间抖动
    try:
        ctypes.WinDLL("winmm").timeBeginPeriod(1)
    except Exception:
        pass

    # 高 DPI（Qt6 默认已开启，这里主要保证 Windows 原生窗口坐标一致）
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("CreatureCurios")
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLE)

    window = MainWindow()
    app.aboutToQuit.connect(window._cleanup)
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
