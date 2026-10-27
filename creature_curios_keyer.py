#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CreatureCurios 后台按键器 v2.

功能：
    * 枚举标题包含 "CreatureCurios" 的可见窗口并绑定 HWND；
    * 在独立 QThread 中通过 win32api.PostMessage 后台投递 A（可选空格拾取）；
    * 可选技能 1 / 2（12s / 6s）和自动喝药 Q（默认 60s）；
    * 疲劳检测：用户框选右下角的疲劳提示区域，每 30 秒短暂点击一下，
      OCR 读取“当前疲劳值:xxx/xxx”，超过 ±可配置阈值时弹出不抢焦点的提醒；
    * 定时提醒：支持倒计时和指定时刻；
    * 支持每轮间隔设置、轮次统计、Ctrl+Alt+F8 全局热键；
    * 最小化后驻留系统托盘，双击托盘图标恢复。

安装依赖：
    pip install -r requirements.txt

运行：
    python creature_curios_keyer.py

说明：
    本程序只使用 PostMessage 后台投递，不会调用 SendInput / keyboard.press_and_release，
    因此不会抢占键盘、鼠标或窗口焦点。若目标游戏以管理员权限运行，请同样以管理员
    权限运行本程序，否则 Windows 的 UIPI 机制可能拦截消息。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import datetime
import json
import os
import random
import re
import sys
import threading
import time
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

from PySide6.QtCore import QEvent, QRect, QSettings, QTime, Qt, QThread, QTimer, Signal
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
    QLabel,
    QLineEdit,
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

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

APP_NAME = "CreatureCurios 后台按键器 v2"
WINDOW_TITLE_KEYWORD = "CreatureCurios"

VK_A = 0x41
VK_SPACE = 0x20
VK_1 = 0x31
VK_2 = 0x32
VK_Q = 0x51

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
FATIGUE_BAND_MARGIN = 20

HOTKEY_ID = "ctrl+alt+f8"
HOTKEY_TEXT = "Ctrl + Alt + F8"
HOTKEY_QT = "Ctrl+Alt+F8"


def _fatigue_config_path() -> str:
    base = os.getenv("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "CreatureCuriosKeyer")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    return os.path.join(folder, "fatigue_roi.json")


FATIGUE_CONFIG_PATH = _fatigue_config_path()
MACRO_CONFIG_PATH = os.path.join(
    os.path.dirname(FATIGUE_CONFIG_PATH), "macros.json"
)
MACRO_STEPS = ("to_boss", "boss_fight", "to_farm")
MACRO_TITLES = {
    "to_boss": "打怪点 → BOSS点",
    "boss_fight": "BOSS 全过程",
    "to_farm": "BOSS点 → 打怪点",
}
ONE_KEY_STAGE_TITLES = {
    "return_farm": "回归打怪点",
    "to_boss": "前往BOSS点",
    "boss_fight": "BOSS全过程",
    "return_boss": "回归BOSS点",
    "to_farm": "返回打怪点",
}


def default_macro_config() -> dict:
    return {
        "recordings": {
            "to_boss": {"events": [], "duration": 0.0, "meta": {}},
            "boss_fight": {"events": [], "duration": 0.0, "meta": {}},
            "to_farm": {"events": [], "duration": 0.0, "meta": {}},
        },
        "points": {
            "farm": None,
            "boss": None,
            "enter_click": None,
            "exit_click": None,
        },
        "map_key": "m",
        "close_map_key": "esc",
    }


def load_macro_config() -> dict:
    cfg = default_macro_config()
    try:
        with open(MACRO_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return cfg
        recs = data.get("recordings", {})
        if not isinstance(recs, dict):
            recs = {}
        for step in MACRO_STEPS:
            item = recs.get(step, {})
            if not isinstance(item, dict):
                continue
            events = item.get("events", [])
            if isinstance(events, list):
                cfg["recordings"][step]["events"] = events
                try:
                    cfg["recordings"][step]["duration"] = float(
                        item.get("duration", 0.0) or 0.0
                    )
                except (TypeError, ValueError):
                    cfg["recordings"][step]["duration"] = 0.0
                meta = item.get("meta")
                if isinstance(meta, dict):
                    cfg["recordings"][step]["meta"] = dict(meta)
        points = data.get("points", {})
        if not isinstance(points, dict):
            points = {}
        for key in ("farm", "boss", "enter_click", "exit_click"):
            value = points.get(key)
            try:
                if (
                    isinstance(value, (list, tuple))
                    and len(value) == 2
                    and 0.0 <= float(value[0]) <= 1.0
                    and 0.0 <= float(value[1]) <= 1.0
                ):
                    cfg["points"][key] = (float(value[0]), float(value[1]))
            except (TypeError, ValueError):
                continue
        for key in ("map_key", "close_map_key"):
            value = data.get(key, "")
            if isinstance(value, str):
                value = value.strip()
                if value:
                    cfg[key] = value[:30]
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return cfg


def save_macro_config(cfg: dict) -> bool:
    """原子写入，避免程序中途退出时把已有的录制文件截断。"""
    tmp_path = MACRO_CONFIG_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                cfg,
                f,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, MACRO_CONFIG_PATH)
        return True
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


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


# ---- 疲劳提示识别（用户在界面上框选“悬停/点击触发区”，OCR 读取提示文本） ----

_OCR_LOCK = threading.Lock()
_OCR_ENGINE = None


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

        global _OCR_ENGINE
        with _OCR_LOCK:
            if _OCR_ENGINE is None:
                from rapidocr_onnxruntime import RapidOCR

                _OCR_ENGINE = RapidOCR()
            result, _elapse = _OCR_ENGINE(ocr_input)

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


# ---------------------------------------------------------------------------
# 全局样式
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
    color: #8f959e;
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
    color: #8f959e;
}
QLabel#hint {
    color: #7f858e;
    font-size: 12px;
    padding: 2px 2px 6px 2px;
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
    background: transparent;
}
QScrollArea > QWidget > QWidget {
    background: transparent;
}
QTabWidget::pane {
    border: 1px solid #303338;
    border-radius: 8px;
    top: -1px;
    background: #1e1f22;
}
QTabBar::tab {
    background: #24262a;
    color: #aeb4bd;
    padding: 8px 18px;
    border: 1px solid #303338;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 3px;
    font-weight: 600;
}
QTabBar::tab:selected {
    background: #2f3b4d;
    color: #8ab4f8;
}
QTabBar::tab:hover {
    background: #33373d;
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
        potion_immediate: bool = False,
        use_pickup: bool = True,
        use_click: bool = False,
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
        self.use_click = bool(use_click)
        self._potion_immediate = bool(potion_immediate)
        self._potion_interval_s = max(5.0, float(potion_interval_ms) / 1000.0)
        self._next_skill1 = 0.0
        self._next_skill2 = 0.0
        self._next_potion = 0.0

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

    def set_use_click(self, enabled: bool) -> None:
        """运行中切换是否在 A 后随机左键点击。"""
        self.use_click = bool(enabled)

    def set_potion_interval(self, seconds: int) -> None:
        self._potion_interval_s = max(5.0, float(seconds))
        if self.use_potion:
            self._next_potion = min(
                self._next_potion or (time.monotonic() + self._potion_interval_s),
                time.monotonic() + self._potion_interval_s,
            )

    # -- 线程主体 ---------------------------------------------------------
    def run(self) -> None:  # noqa: C901 - 逻辑本身很短，保持集中便于阅读
        try:
            now = time.monotonic()
            self._next_skill1 = now
            self._next_skill2 = now
            self._next_potion = (
                now if self._potion_immediate else now + self._potion_interval_s
            )

            while self.is_running and not self._stop_event.is_set():
                if not self._window_alive():
                    self._last_error = "目标窗口已关闭（句柄失效），发送已停止。"
                    break

                if not self.attack_enabled:
                    # 仅定时喝药/技能，不发送 A / 空格（BOSS 录制回放时用）
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

                # 宏动作：A 按下 50ms -> 松开
                # -> 可选随机鼠标左键（加快攻速）
                # -> 可选空格拾取
                if not self._tap(VK_A):
                    break
                if self.use_click:
                    if self._stop_event.is_set():
                        break
                    self._wait(0.02)
                    if not self._click_random():
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
        if not (self.use_skills or self.use_potion):
            return True
        if not self._window_alive():
            self._last_error = "目标窗口已关闭（句柄失效），发送已停止。"
            return False

        now = time.monotonic()
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
        """可被 stop() 立即唤醒的 sleep。

        正常运行时等价于 time.sleep(seconds)；点击“停止”时能马上退出等待，
        不会让线程多睡一个完整间隔，同时仍保证按键会被松开。
        """
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

    def _random_click_point(self) -> Optional[tuple[int, int]]:
        """在客户区中间附近取一个随机的安全点击位置。"""
        try:
            _l, _t, w, h = win32gui.GetClientRect(self.hwnd)
            w, h = int(w), int(h)
        except Exception:
            return None
        if w < 20 or h < 20:
            return None
        cx, cy = w // 2, h // 2
        span_x = max(1, min(w // 5, w // 2 - max(8, w // 10)))
        span_y = max(1, min(h // 5, h // 2 - max(8, h // 10)))
        x = random.randint(max(1, cx - span_x), max(1, cx + span_x))
        y = random.randint(max(1, cy - span_y), max(1, cy + span_y))
        return (min(w - 2, x), min(h - 2, y))

    def _click_random(self) -> bool:
        """后台发送一次随机左键点击，不移动真实鼠标。"""
        if not self._window_alive():
            self._last_error = "目标窗口已关闭（句柄失效），发送已停止。"
            return False
        point = self._random_click_point()
        if point is None:
            return True
        x, y = point
        lparam = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
        self._post_aux(self.hwnd, WM_MOUSEMOVE, 0, lparam)
        if not self._post(self.hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam):
            return False
        self._wait(0.03)
        # 即使已请求停止也要补上抬起，避免目标窗口卡住左键。
        if not self._post(self.hwnd, win32con.WM_LBUTTONUP, 0, lparam):
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


# ---------------------------------------------------------------------------
# 疲劳检测线程 / 提醒弹窗
# ---------------------------------------------------------------------------


def _force_foreground_window(hwnd: int) -> None:
    """把目标窗口切到前台（检测时点击游戏需要它可见/在最上层）。"""
    try:
        current = int(win32gui.GetForegroundWindow() or 0)
        if current == hwnd:
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
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                _USER32.SwitchToThisWindow(hwnd, True)
        finally:
            if attached:
                try:
                    _USER32.AttachThreadInput(my_tid, fg_tid, False)
                except Exception:
                    pass
    except Exception:
        pass


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
# 宏录制 / 回放 / 点位捕获
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


_USER32.SendInput.argtypes = [
    wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int
]
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
_MOUSEEVENTF_HWHEEL = 0x1000
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_VIRTUALDESK = 0x4000


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


_SPECIAL_VK = {
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "page up": 0x21,
    "page down": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "shift": 0x10,
    "left shift": 0xA0,
    "right shift": 0xA1,
    "ctrl": 0x11,
    "control": 0x11,
    "left ctrl": 0xA2,
    "right ctrl": 0xA3,
    "alt": 0x12,
    "left alt": 0xA4,
    "right alt": 0xA5,
    "caps lock": 0x14,
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
    """用 keybd_event 发键；录制钩子能捕获这种按键。"""
    vk = _vk_from_key_name(name)
    if vk is None:
        # 未知按键退回 keyboard 库（不影响正常回放，只是录制钩子可能捕不到）
        try:
            if down:
                keyboard.press(name)
            else:
                keyboard.release(name)
            return True
        except Exception:
            return False
    scan = int(win32api.MapVirtualKey(vk, 0)) & 0xFF
    flags = 0 if down else win32con.KEYEVENTF_KEYUP
    try:
        win32api.keybd_event(vk, scan, flags, 0)
        return True
    except Exception:
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
        except Exception:
            self._client_rect = [0, 0, 0, 0]
            self._client_size = [0, 0]
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

    def stop(self) -> list[dict]:
        self._active = False
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
        return events

    def meta(self) -> dict:
        return {
            "screen": [int(self._screen_size[0]), int(self._screen_size[1])],
            "client_rect": [int(v) for v in self._client_rect],
            "client_size": [int(v) for v in self._client_size],
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

    def _on_mouse(self, event) -> None:
        if not self._active or not _foreground_is_window(self.game_hwnd):
            return
        try:
            if isinstance(event, mouse.MoveEvent):
                x, y = int(event.x), int(event.y)
                if not self._inside_game(x, y):
                    return
                now = float(event.time)
                if self._last_move_time >= 0 and now - self._last_move_time < 0.012:
                    return
                if self._last_move_pos is not None:
                    dx = abs(x - self._last_move_pos[0])
                    dy = abs(y - self._last_move_pos[1])
                    if dx + dy < 2:
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
                event_type = str(event.event_type)
                if event_type not in ("down", "up", "double"):
                    return
                x, y = mouse.get_position()
                if not self._inside_game(int(x), int(y)):
                    return
                button = str(event.button)

                if event_type in ("down", "double"):
                    # mouse 库会把“双击/连点”的第二个按下改写成 double。
                    # 如果直接忽略 double，BOSS 战里的快速连点就会只录到 up，
                    # 回放时大量攻击不会被触发。这里必须把 double 当成 down。
                    if button in self._pressed_buttons:
                        return
                    self._pressed_buttons.add(button)
                    recorded_type = "down"
                else:
                    # 录制开始前就已经按下的键，松开时没有对应的 down。
                    # 忽略这种孤立 up，避免回放时凭空松开鼠标键。
                    if button not in self._pressed_buttons:
                        return
                    self._pressed_buttons.discard(button)
                    recorded_type = "up"

                norm = self._norm_point(int(x), int(y))
                ev = {
                    "type": recorded_type,
                    "x": int(x),
                    "y": int(y),
                    "button": button,
                }
                if norm is not None:
                    ev["nx"], ev["ny"] = norm
                self._record(ev, float(event.time))
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
                self._record(ev, float(event.time))
        except Exception:
            pass

    def _on_key(self, event) -> None:
        if not self._active or not _foreground_is_window(self.game_hwnd):
            return
        try:
            if event.event_type not in ("down", "up"):
                return
            name = str(event.name)
            if not name:
                return
            self._record(
                {
                    "type": "key_down"
                    if event.event_type == "down"
                    else "key_up",
                    "name": name,
                },
                float(event.time),
            )
        except Exception:
            pass


class PointCapture:
    """捕获用户在游戏地图上的第一次左键点击，保存归一化客户区坐标。"""

    def __init__(self, game_hwnd: int, kind: str, callback):
        self.game_hwnd = int(game_hwnd)
        self.kind = kind
        self._callback = callback
        self._hook = None
        self._done = False
        self._origin = (0, 0)
        self._cw = 0
        self._ch = 0

    def start(self) -> None:
        if mouse is None:
            raise RuntimeError(
                f"缺少 mouse 库：{MOUSE_IMPORT_ERROR or '未安装'}"
            )
        self._done = False
        self._origin = win32gui.ClientToScreen(self.game_hwnd, (0, 0))
        left, top, right, bottom = win32gui.GetClientRect(self.game_hwnd)
        self._cw = max(1, int(right - left))
        self._ch = max(1, int(bottom - top))
        self._hook = mouse.hook(self._on_mouse)

    def cancel(self) -> None:
        self._done = True
        try:
            if self._hook is not None:
                mouse.unhook(self._hook)
        except Exception:
            pass
        self._hook = None

    def _on_mouse(self, event) -> None:
        if self._done or not isinstance(event, mouse.ButtonEvent):
            return
        if event.event_type != "down" or event.button != "left":
            return
        if not _foreground_is_window(self.game_hwnd):
            return
        try:
            x, y = mouse.get_position()
            ox, oy = self._origin
            nx = (int(x) - int(ox)) / self._cw
            ny = (int(y) - int(oy)) / self._ch
            if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
                return
            try:
                hwnd_under = int(win32gui.WindowFromPoint((int(x), int(y))) or 0)
                root_under = (
                    int(win32gui.GetAncestor(hwnd_under, win32con.GA_ROOT))
                    if hwnd_under
                    else 0
                )
                if root_under != self.game_hwnd:
                    return
            except Exception:
                return
            self._done = True
            self.cancel()
            self._callback(self.kind, (float(nx), float(ny)))
        except Exception:
            pass


class MacroPlayer(QThread):
    """按录制的时间线回放鼠标/键盘。"""

    done = Signal(str)
    error = Signal(str, str)

    def __init__(
        self,
        game_hwnd: int,
        stage: str,
        events: list[dict],
        restore_foreground: bool = True,
        restore_cursor: bool = True,
        capture_friendly_keys: bool = False,
        meta: Optional[dict] = None,
        scale_coords: bool = True,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.game_hwnd = int(game_hwnd)
        self.stage = stage
        self.events = list(events)
        self.restore_foreground = bool(restore_foreground)
        self.restore_cursor = bool(restore_cursor)
        self.capture_friendly_keys = bool(capture_friendly_keys)
        self.meta = dict(meta) if isinstance(meta, dict) else None
        self.scale_coords = bool(scale_coords)
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def _make_point_mapper(self):
        """把录制时的屏幕坐标映射到当前游戏客户区。"""
        if not self.scale_coords:
            return lambda x, y: (int(round(x)), int(round(y)))
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
            if (
                isinstance(rec_rect, (list, tuple))
                and len(rec_rect) == 4
            ):
                rec_l, rec_t = float(rec_rect[0]), float(rec_rect[1])
            if (
                isinstance(rec_size, (list, tuple))
                and len(rec_size) == 2
            ):
                rec_w, rec_h = float(rec_size[0]), float(rec_size[1])
            elif (
                isinstance(rec_rect, (list, tuple))
                and len(rec_rect) == 4
            ):
                rec_w = float(rec_rect[2]) - float(rec_rect[0])
                rec_h = float(rec_rect[3]) - float(rec_rect[1])
        except (TypeError, ValueError):
            rec_w = rec_h = 0

        if rec_w <= 0 or rec_h <= 0:
            # 旧录制没有保存窗口信息：按“当时全屏、当前屏幕尺寸”兼容。
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
        """优先使用新录制保存的归一化坐标；旧录制回退到屏幕坐标换算。"""
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

    def run(self) -> None:
        error_msg = ""
        prev_cursor = None
        prev_fg = 0
        pressed_keys: set[str] = set()
        pressed_buttons: set[str] = set()
        try:
            if not self.events:
                raise RuntimeError("没有录制内容。")
            if mouse is None:
                raise RuntimeError(
                    f"缺少 mouse 库：{MOUSE_IMPORT_ERROR or '未安装'}"
                )
            try:
                prev_cursor = win32api.GetCursorPos()
                prev_fg = int(win32gui.GetForegroundWindow() or 0)
            except Exception:
                pass

            _force_foreground_window(self.game_hwnd)
            time.sleep(0.15)
            if not _foreground_is_window(self.game_hwnd):
                raise RuntimeError("无法把游戏窗口切到前台，回放已取消。")

            map_event = self._make_event_mapper()

            # 先无副作用地把光标放到录制起点，避免第一帧产生巨大的 raw input 位移
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
                target = max(0.0, float(event.get("t", 0.0)))
                while True:
                    remaining = target - (time.perf_counter() - start)
                    if remaining <= 0:
                        break
                    if self._stop_event.wait(min(remaining, 0.02)):
                        break
                if self._stop_event.is_set():
                    break

                typ = str(event.get("type", ""))
                if typ == "move":
                    px, py = map_event(event)
                    _send_mouse_move_to(px, py)
                elif typ in ("down", "up"):
                    button = str(event.get("button", "left")).lower()
                    px, py = map_event(event)
                    _send_mouse_move_to(px, py)
                    _send_mouse_button(button, typ == "down")
                    if typ == "down":
                        pressed_buttons.add(button)
                    else:
                        pressed_buttons.discard(button)
                elif typ == "wheel":
                    px, py = map_event(event)
                    _send_mouse_move_to(px, py)
                    _send_mouse_wheel(int(event.get("delta", 0)))
                elif typ in ("key_down", "key_up"):
                    name = str(event.get("name", ""))
                    if not name:
                        continue
                    if typ == "key_down":
                        if self.capture_friendly_keys:
                            _send_key_name(name, True)
                        else:
                            keyboard.press(name)
                        pressed_keys.add(name)
                    else:
                        if self.capture_friendly_keys:
                            _send_key_name(name, False)
                        else:
                            keyboard.release(name)
                        pressed_keys.discard(name)

            if self._stop_event.is_set():
                # 停止时也视为正常结束，但不再继续后续阶段
                pass
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
        finally:
            # 释放所有仍按下的键/鼠标键，避免卡键
            for key in list(pressed_keys):
                try:
                    if self.capture_friendly_keys:
                        _send_key_name(key, False)
                    else:
                        keyboard.release(key)
                except Exception:
                    pass
            for button in list(pressed_buttons):
                try:
                    _send_mouse_button(button, False)
                except Exception:
                    pass

            if self.restore_foreground:
                _restore_foreground_window(prev_fg, self.game_hwnd)
            if self.restore_cursor and prev_cursor is not None:
                try:
                    time.sleep(0.02)
                    win32api.SetCursorPos(prev_cursor)
                except Exception:
                    pass

            if error_msg:
                self.error.emit(self.stage, error_msg)
            else:
                self.done.emit(self.stage)


class FatigueMonitor(QThread):
    """每 30 秒点击一次用户框选的疲劳提示区域，读取弹出的文本。

    检测流程：保存鼠标/前台窗口 → 移动到框选区域 → 短暂左键点击 →
    等待提示框出现 → 后台截图 → 恢复鼠标/前台窗口 → OCR 读取
    “当前疲劳值:xxx/xxx”。
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
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.hwnd = int(hwnd)
        self.roi_norm = roi_norm
        self.alert_below = alert_below
        self.alert_above = alert_above
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

        # 用户正在按鼠标时不要抢鼠标
        try:
            if (
                win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000
                or win32api.GetAsyncKeyState(win32con.VK_RBUTTON) & 0x8000
            ):
                self.result.emit(None, None, "鼠标按键按下中，跳过本次疲劳检测。")
                return
        except Exception:
            pass

        try:
            left, top, right, bottom = win32gui.GetClientRect(self.hwnd)
            cw, ch = int(right - left), int(bottom - top)
            if cw <= 0 or ch <= 0:
                self.result.emit(None, None, "客户区尺寸无效，跳过疲劳检测。")
                return
            roi = roi_from_norm(self.roi_norm, cw, ch)
            roi_left, roi_top = win32gui.ClientToScreen(self.hwnd, (roi[0], roi[1]))
            roi_right, roi_bottom = win32gui.ClientToScreen(
                self.hwnd, (roi[0] + roi[2], roi[1] + roi[3])
            )
        except Exception as exc:
            self.result.emit(None, None, f"疲劳区域无效：{type(exc).__name__}: {exc}")
            return

        saved_pos = win32api.GetCursorPos()
        prev_fg = 0
        try:
            prev_fg = int(win32gui.GetForegroundWindow() or 0)
        except Exception:
            pass

        cap = None
        cap_err = ""
        anchor_pos = None
        user_moved = False
        moved_to_anchor = False
        try:
            # 先把游戏切到前台，避免游戏被别的窗口挡住时点到别的窗口
            _force_foreground_window(self.hwnd)
            time.sleep(0.06)

            # 点击用户保存的区域中心，让游戏弹出疲劳提示
            client_x = roi[0] + roi[2] // 2
            client_y = roi[1] + roi[3] // 2
            client_x = max(1, min(cw - 2, client_x))
            client_y = max(1, min(ch - 2, client_y))
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
                self.result.emit(
                    None,
                    None,
                    "疲劳提示区域被其他窗口遮挡，已跳过本次点击。",
                )
                return

            win32api.SetCursorPos(anchor_pos)
            moved_to_anchor = True

            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

            # 等提示框完全显示；停止时也能立即退出
            if self._stop_event.wait(1.0):
                return

            cap, cap_err = capture_client_bgra(self.hwnd)
        except Exception as exc:
            cap_err = f"疲劳检测异常：{type(exc).__name__}: {exc}"
        finally:
            if moved_to_anchor:
                try:
                    cur = win32api.GetCursorPos()
                    # 游戏可能把鼠标轻微吸附到图标中心，所以用“是否还在框选区域附近”
                    # 来判断用户是否自己接管了鼠标，而不是用几像素的误差。
                    margin = 30
                    user_moved = not (
                        roi_left - margin <= cur[0] <= roi_right + margin
                        and roi_top - margin <= cur[1] <= roi_bottom + margin
                    )
                except Exception:
                    pass
            else:
                user_moved = False

            # 检测只有约 1 秒，结束后始终把前台窗口和鼠标还给用户。
            # （游戏可能自己吸附/移动光标，所以不能因为光标偏离就放弃恢复。）
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

        if self._stop_event.is_set():
            return
        if cap is None:
            self.result.emit(None, None, cap_err or "疲劳截图失败")
            return

        w, h, data = cap
        try:
            roi = roi_from_norm(self.roi_norm, w, h)
            current, maximum, msg = ocr_fatigue_from_bgra(data, w, h, roi)
        except Exception as exc:
            self.result.emit(None, None, f"疲劳识别异常：{type(exc).__name__}: {exc}")
            return

        if current is None or maximum is None:
            self.result.emit(None, None, msg)
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
            "不需要框文字或疲劳条；程序之后每 30 秒会在该区域内短暂点击一下，"
            "再读取弹出来的提示。"
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
        title.setStyleSheet("font-size: 15px; font-weight: 700; color: #f1f3f4;")
        layout.addWidget(title)

        info = QLabel(info_text)
        info.setWordWrap(True)
        info.setStyleSheet("color: #aeb4bd;")
        layout.addWidget(info)

        self.canvas = _RegionCanvas(image, initial_roi, self)
        self.canvas.selection_changed.connect(self._on_selection)
        layout.addWidget(self.canvas, 0, Qt.AlignmentFlag.AlignCenter)

        self.lbl_selection = QLabel("区域: 尚未框选")
        self.lbl_selection.setStyleSheet("color: #8ab4f8;")
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
# 主窗口
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    """UI 主窗口。工作线程 KeySender 与界面逻辑分离。"""

    # keyboard 库的回调在它自己的线程中执行，通过 Qt Signal 安全地切回 UI 线程。
    hotkey_triggered = Signal()
    # 点位捕获回调来自 mouse 监听线程，必须通过信号切回 UI 线程
    point_captured = Signal(str, object)

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
        self._fatigue_roi_norm: Optional[tuple[float, float, float, float]] = (
            load_fatigue_roi_norm()
        )
        self._settings = QSettings("CreatureCurios", "CreatureCuriosKeyer")
        try:
            threshold = int(
                self._settings.value(
                    "fatigue_threshold", DEFAULT_FATIGUE_THRESHOLD
                )
            )
        except Exception:
            threshold = DEFAULT_FATIGUE_THRESHOLD
        self._fatigue_threshold = max(50, min(1000, threshold))
        self._timer_next_mono: Optional[float] = None
        self._timer_next_dt: Optional[datetime.datetime] = None

        # BOOS 挂机 / 宏
        self._macro_cfg = load_macro_config()
        self._recorder: Optional[MacroRecorder] = None
        self._recording_step: Optional[str] = None
        self._recording_backup: Optional[tuple[str, dict]] = None
        self._rec_start_timer: Optional[QTimer] = None
        self._rec_update_timer: Optional[QTimer] = None
        self._point_capture: Optional[PointCapture] = None
        self._point_kind: Optional[str] = None
        self._point_timeout_timer: Optional[QTimer] = None
        self._single_player: Optional[MacroPlayer] = None
        self._single_stage: str = ""
        self._record_return_player: Optional[MacroPlayer] = None
        self._one_key_running = False
        self._one_key_state = "idle"
        self._one_key_stage = ""
        self._one_key_worker: Optional[KeySender] = None
        self._one_key_fatigue: Optional[FatigueMonitor] = None
        self._one_key_initial_monitor: Optional[FatigueMonitor] = None
        self._one_key_post_boss_monitor: Optional[FatigueMonitor] = None
        self._one_key_post_boss_retry = 0
        self._one_key_potion_worker: Optional[KeySender] = None
        self._one_key_player: Optional[MacroPlayer] = None
        self._one_key_prev_fg = 0
        self._one_key_prev_cursor = None
        self._one_key_farm_base = 0
        self._one_key_total_farm_rounds = 0
        # 稳定 BOSS（不录制）
        self._stable_runner = None
        self._stable_test_running = False
        self._one_key_stable = False
        self._route_editor = None

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(self._make_icon())
        self.setMinimumSize(860, 750)
        self.resize(920, 800)

        self._build_ui()
        self._refresh_fatigue_roi_label()
        self.point_captured.connect(self._on_point_captured)
        self._rec_update_timer = QTimer(self)
        self._rec_update_timer.timeout.connect(self._update_recording_status)
        self._refresh_macro_labels()
        self._refresh_point_labels()
        self._refresh_threshold_labels()
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
        subtitle = QLabel("后台 PostMessage 投递 · 不抢焦点 · 全局热键控制")
        subtitle.setObjectName("subtitle")
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header.addLayout(title_col)
        header.addStretch(1)

        badge = QLabel("v2")
        badge.setObjectName("versionBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(38, 24)
        header.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)

        root.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self.tab_basic_container = QWidget()
        basic_root = QVBoxLayout(self.tab_basic_container)
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

        click_row = QHBoxLayout()
        click_row.setSpacing(8)
        self.chk_attack_click = QCheckBox("A 后随机点击鼠标（加快攻速）")
        self.chk_attack_click.setChecked(True)
        self.chk_attack_click.setToolTip(
            "每轮 A 之后，在游戏客户区中间附近随机左键点击一次，"
            "用后台消息发送，不移动真实鼠标。"
            "如果游戏是点击移动，角色可能会被点得走位，可取消勾选。"
        )
        self.chk_attack_click.toggled.connect(self._on_attack_click_toggled)
        click_row.addWidget(self.chk_attack_click)
        click_row.addStretch(1)
        params_layout.addLayout(click_row)
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
            "开始发送后每 30 秒检测一次；检测时会在你框选的区域短暂点击一下，"
            "读取弹出的“当前疲劳值:xxx/xxx”，随后自动恢复鼠标和前台窗口"
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
        self.btn_fatigue_test.setToolTip("立即执行一次疲劳检测（会短暂移动鼠标）")
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
            "疲劳阈值，例如 800 / 600 / 500。"
            "基础页按 ±阈值 弹窗；一键打BOSS按阈值±20切换。"
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
        aux.addWidget(self.chk_timer, 4, 0)
        aux.addWidget(timer_wrap, 4, 1, 1, 3)

        aux_note = QLabel(
            "技能 / 喝药在“开始发送”后生效；疲劳检测会在你框选的区域短暂点击并读取提示；"
            "定时提醒独立生效，最小化到托盘也会弹窗。"
        )
        aux_note.setObjectName("muted")
        aux_note.setWordWrap(True)
        aux.addWidget(aux_note, 5, 0, 1, 4)

        basic_root.addWidget(box_aux)
        basic_root.addStretch(1)

        self.tab_basic = QScrollArea()
        self.tab_basic.setWidgetResizable(True)
        self.tab_basic.setFrameShape(QFrame.Shape.NoFrame)
        self.tab_basic.setWidget(self.tab_basic_container)
        self.tabs.addTab(self.tab_basic, "基础挂机")

        self.tab_boss = self._build_boss_tab()
        self.tabs.addTab(self.tab_boss, "BOOS挂机")
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

    def _build_boss_tab(self) -> QWidget:
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # ---- 疲劳阈值（放在最上面，保证一眼能看到） ----
        box_threshold = QGroupBox("疲劳阈值")
        tg = QHBoxLayout(box_threshold)
        tg.setSpacing(8)
        tg.addWidget(QLabel("疲劳阈值 ±"))
        self.spin_fatigue_threshold_boss = QSpinBox()
        self.spin_fatigue_threshold_boss.setRange(50, 1000)
        self.spin_fatigue_threshold_boss.setSingleStep(10)
        self.spin_fatigue_threshold_boss.setValue(int(self._fatigue_threshold))
        self.spin_fatigue_threshold_boss.setSuffix(" 点")
        self.spin_fatigue_threshold_boss.setFixedWidth(110)
        self.spin_fatigue_threshold_boss.setToolTip(
            "疲劳阈值，例如 800 / 600 / 500。"
            "一键打BOSS按 阈值-20 切换，基础页按 ±阈值 弹窗。"
        )
        self.spin_fatigue_threshold_boss.valueChanged.connect(
            self._on_threshold_changed
        )
        tg.addWidget(self.spin_fatigue_threshold_boss)
        self.lbl_one_key_threshold = QLabel("")
        self.lbl_one_key_threshold.setObjectName("muted")
        self.lbl_one_key_threshold.setWordWrap(True)
        tg.addWidget(self.lbl_one_key_threshold, 1)
        root.addWidget(box_threshold)

        # ---- 点位 ----
        box_points = QGroupBox("1 · 点位设置（同地图）")
        pg = QGridLayout(box_points)
        pg.setHorizontalSpacing(10)
        pg.setVerticalSpacing(8)
        pg.setColumnStretch(2, 1)

        self.lbl_point_farm = QLabel("打怪点: 未设置")
        self.btn_set_farm = QPushButton("设置打怪点")
        self.btn_set_farm.setFixedWidth(110)
        self.btn_set_farm.clicked.connect(
            lambda _=False: self._start_point_capture("farm")
        )

        self.lbl_point_boss = QLabel("BOSS点: 未设置")
        self.btn_set_boss = QPushButton("设置BOSS点")
        self.btn_set_boss.setFixedWidth(110)
        self.btn_set_boss.clicked.connect(
            lambda _=False: self._start_point_capture("boss")
        )

        self.edit_map_key = QLineEdit(str(self._macro_cfg.get("map_key", "m")))
        self.edit_map_key.setMaxLength(20)
        self.edit_map_key.setFixedWidth(80)
        self.edit_map_key.setToolTip("打开地图的按键名，例如 m / tab")
        self.edit_map_key.editingFinished.connect(self._save_macro_settings)
        self.edit_close_key = QLineEdit(
            str(self._macro_cfg.get("close_map_key", "esc"))
        )
        self.edit_close_key.setMaxLength(20)
        self.edit_close_key.setFixedWidth(80)
        self.edit_close_key.setToolTip("关闭地图的按键名，例如 esc / m")
        self.edit_close_key.editingFinished.connect(self._save_macro_settings)

        self.btn_return_farm = QPushButton("回归打怪点")
        self.btn_return_farm.setFixedWidth(120)
        self.btn_return_farm.clicked.connect(
            lambda _=False: self._return_to_point("farm")
        )
        self.btn_return_boss = QPushButton("回归BOSS点")
        self.btn_return_boss.setFixedWidth(120)
        self.btn_return_boss.clicked.connect(
            lambda _=False: self._return_to_point("boss")
        )

        pg.addWidget(self.lbl_point_farm, 0, 0, 1, 3)
        pg.addWidget(self.btn_set_farm, 0, 3)
        pg.addWidget(self.lbl_point_boss, 1, 0, 1, 3)
        pg.addWidget(self.btn_set_boss, 1, 3)
        pg.addWidget(QLabel("地图键"), 2, 0)
        pg.addWidget(self.edit_map_key, 2, 1)
        pg.addWidget(QLabel("关闭键"), 2, 2)
        pg.addWidget(self.edit_close_key, 2, 3)
        pg.addWidget(self.btn_return_farm, 3, 0)
        pg.addWidget(self.btn_return_boss, 3, 1)
        point_hint = QLabel(
            "同地图使用：地图键打开 → 点击设置的点位 → 关闭键关闭。"
            "设置点位时，3 秒后在地图上左键点击一次即可。"
        )
        point_hint.setObjectName("muted")
        point_hint.setWordWrap(True)
        pg.addWidget(point_hint, 3, 2, 1, 2)
        self.btn_route_editor = QPushButton("录制 / 管理打怪路线（窗口相对坐标）")
        self.btn_route_editor.setMinimumHeight(32)
        self.btn_route_editor.setToolTip(
            "在同一个 exe 里打开路线编辑器：记录地图入口、路径点、等待和说明。"
            "所有坐标以游戏窗口客户区左上角为基准。"
        )
        self.btn_route_editor.clicked.connect(self._open_route_editor)
        pg.addWidget(self.btn_route_editor, 4, 0, 1, 4)
        root.addWidget(box_points)

        # ---- 录制 ----
        box_rec = QGroupBox("2 · 流程录制 / 回放")
        rg = QGridLayout(box_rec)
        rg.setHorizontalSpacing(8)
        rg.setVerticalSpacing(8)
        rg.setColumnStretch(1, 1)
        self._rec_status: dict[str, QLabel] = {}
        self._rec_buttons: dict[str, tuple[QPushButton, QPushButton, QPushButton]] = {}

        for row, step in enumerate(MACRO_STEPS):
            name = QLabel(MACRO_TITLES[step])
            name.setMinimumWidth(130)
            status = QLabel("未录制")
            status.setObjectName("muted")
            btn_rec = QPushButton("录制")
            btn_play = QPushButton("开始")
            btn_rerec = QPushButton("重新录制")
            for b in (btn_rec, btn_play):
                b.setFixedWidth(72)
            btn_rerec.setFixedWidth(88)
            btn_rec.clicked.connect(
                lambda _=False, s=step: self._start_recording(s, clear=False)
            )
            btn_play.clicked.connect(lambda _=False, s=step: self._play_recording(s))
            btn_rerec.clicked.connect(
                lambda _=False, s=step: self._start_recording(s, clear=True)
            )
            rg.addWidget(name, row, 0)
            rg.addWidget(status, row, 1)
            rg.addWidget(btn_rec, row, 2)
            rg.addWidget(btn_play, row, 3)
            rg.addWidget(btn_rerec, row, 4)
            self._rec_status[step] = status
            self._rec_buttons[step] = (btn_rec, btn_play, btn_rerec)

        rec_hint = QLabel(
            "点“录制”后 3 秒开始，请切到游戏操作；录完回到本窗口点“停止录制”。"
            "“重新录制”只有在新录制录到动作后才覆盖旧录制；新录制为空或失败会保留旧录制。"
            "录制会保存游戏窗口尺寸，回放自动适配当前窗口；旧录制按屏幕比例兼容。"
            "录制会保存到本地，下次启动自动加载。"
        )
        rec_hint.setObjectName("muted")
        rec_hint.setWordWrap(True)
        rg.addWidget(rec_hint, len(MACRO_STEPS), 0, 1, 5)
        root.addWidget(box_rec)

        # ---- 一键打BOSS ----
        box_one = QGroupBox("3 · 一键打BOSS")
        og = QGridLayout(box_one)
        og.setHorizontalSpacing(10)
        og.setVerticalSpacing(8)
        og.setColumnStretch(1, 1)

        self.chk_farm_potion = QCheckBox("打怪时喝药 Q")
        self.chk_farm_potion.setToolTip(
            "刷怪阶段是否喝药；BOSS 阶段会强制喝药。喝药间隔沿用基础挂机页的设置。"
        )
        farm_hint = QLabel("BOSS战强制喝药；喝药间隔/技能沿用“基础挂机”页")
        farm_hint.setObjectName("muted")

        self.btn_one_key = QPushButton("一键打BOSS 开始")
        self.btn_one_key.setObjectName("oneKeyButton")
        self.btn_one_key.setMinimumHeight(44)
        self.btn_one_key.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_one_key.clicked.connect(self._toggle_one_key)
        self._apply_one_key_button_style(False)

        self.lbl_one_key_state = QLabel("未运行")
        self.lbl_one_key_state.setObjectName("muted")
        self.lbl_boss_fatigue = QLabel("疲劳: --/--")
        self.lbl_boss_fatigue.setObjectName("muted")
        self.lbl_boss_rounds = QLabel("刷怪: 0 轮")
        self.lbl_boss_rounds.setObjectName("muted")

        one_note = QLabel(
            "逻辑：每 30 秒检测疲劳；达到阈值时在刷怪和BOSS之间切换。"
            "去BOSS点前先回归打怪点；每次从BOSS副本出来先回归BOSS点，"
            "再决定继续打BOSS还是返回打怪点。BOSS 过程中按设定间隔强制喝药 Q。"
        )
        one_note.setObjectName("muted")
        one_note.setWordWrap(True)

        og.addWidget(self.chk_farm_potion, 0, 0)
        og.addWidget(farm_hint, 0, 1, 1, 3)
        og.addWidget(self.btn_one_key, 1, 0, 1, 4)
        og.addWidget(self.lbl_one_key_state, 2, 0, 1, 2)
        og.addWidget(self.lbl_boss_fatigue, 2, 2)
        og.addWidget(self.lbl_boss_rounds, 2, 3)
        og.addWidget(one_note, 3, 0, 1, 4)
        root.addWidget(box_one)

        # ---- 稳定 BOSS（不录制） ----
        box_stable = QGroupBox("4 · 稳定BOSS（不录制）")
        sg = QGridLayout(box_stable)
        sg.setHorizontalSpacing(8)
        sg.setVerticalSpacing(8)
        sg.setColumnStretch(1, 1)
        sg.setColumnStretch(4, 1)

        self.chk_stable_mode = QCheckBox("使用稳定模式（不需要三个录制）")
        self.chk_stable_mode.setChecked(False)
        self.chk_stable_mode.setToolTip(
            "勾选后，上面的「一键打BOSS」会使用地图自动寻路 + 弹窗识别 + 固定战斗循环，"
            "不再依赖 to_boss / boss_fight / to_farm 录制。"
        )
        stable_hint = QLabel(
            "地图自动寻路 → 入口弹窗 → BOSS 战斗循环（强制喝药）→ 退出弹窗。"
            "需要设置打怪点和BOSS点；BOSS点=地图上的副本入口。"
        )
        stable_hint.setObjectName("muted")
        stable_hint.setWordWrap(True)

        self.spin_stable_path_wait = QSpinBox()
        self.spin_stable_path_wait.setRange(5, 180)
        self.spin_stable_path_wait.setValue(30)
        self.spin_stable_path_wait.setSuffix(" 秒")
        self.spin_stable_path_wait.setFixedWidth(100)
        self.spin_stable_path_wait.setToolTip("关闭地图后，等待自动寻路并识别入口弹窗的最长时间")

        self.spin_stable_fight = QSpinBox()
        self.spin_stable_fight.setRange(2, 120)
        self.spin_stable_fight.setValue(6)
        self.spin_stable_fight.setSuffix(" 秒")
        self.spin_stable_fight.setFixedWidth(100)
        self.spin_stable_fight.setToolTip("进入副本后，自动打 BOSS 的时长；只打能秒的怪时 5-8 秒通常够")

        self.spin_stable_popup_wait = QSpinBox()
        self.spin_stable_popup_wait.setRange(3, 60)
        self.spin_stable_popup_wait.setValue(12)
        self.spin_stable_popup_wait.setSuffix(" 秒")
        self.spin_stable_popup_wait.setFixedWidth(100)
        self.spin_stable_popup_wait.setToolTip("BOSS 打完后，等待退出弹窗的最长时间")

        self.spin_stable_load_wait = QSpinBox()
        self.spin_stable_load_wait.setRange(2, 60)
        self.spin_stable_load_wait.setValue(4)
        self.spin_stable_load_wait.setSuffix(" 秒")
        self.spin_stable_load_wait.setFixedWidth(100)
        self.spin_stable_load_wait.setToolTip("进入副本后/退出副本后的加载等待")

        self.spin_stable_travel_wait = QSpinBox()
        self.spin_stable_travel_wait.setRange(3, 180)
        self.spin_stable_travel_wait.setValue(15)
        self.spin_stable_travel_wait.setSuffix(" 秒")
        self.spin_stable_travel_wait.setFixedWidth(100)
        self.spin_stable_travel_wait.setToolTip("从副本回到地图后，点打怪点并等待自动寻路的时间")

        self.edit_stable_enter_kw = QLineEdit("进入,挑战,确认")
        self.edit_stable_enter_kw.setToolTip("OCR 在入口弹窗里找这些关键词，找到后点击文字中心")
        self.edit_stable_exit_kw = QLineEdit("离开,退出,确认")
        self.edit_stable_exit_kw.setToolTip("OCR 在退出弹窗里找这些关键词，找到后点击文字中心")

        self.lbl_enter_click = QLabel("入口按钮: 未设置（仅 OCR）")
        self.lbl_enter_click.setObjectName("muted")
        self.btn_set_enter_click = QPushButton("设置入口按钮")
        self.btn_set_enter_click.setFixedWidth(110)
        self.btn_set_enter_click.clicked.connect(
            lambda _=False: self._select_click_point("enter_click")
        )
        self.lbl_exit_click = QLabel("退出按钮: 未设置（仅 OCR）")
        self.lbl_exit_click.setObjectName("muted")
        self.btn_set_exit_click = QPushButton("设置退出按钮")
        self.btn_set_exit_click.setFixedWidth(110)
        self.btn_set_exit_click.clicked.connect(
            lambda _=False: self._select_click_point("exit_click")
        )

        self.btn_stable_test = QPushButton("稳定模式 单次测试")
        self.btn_stable_test.setMinimumHeight(36)
        self.btn_stable_test.clicked.connect(self._toggle_stable_test)
        self.lbl_stable_state = QLabel("未运行")
        self.lbl_stable_state.setObjectName("muted")

        stable_note = QLabel(
            "单次测试会执行：地图→BOSS点→自动寻路→点入口→战斗固定秒数→点退出。"
            "建议先设置入口/退出按钮点（打开弹窗后点“设置”截图框选），OCR 失败时用它兜底。"
        )
        stable_note.setObjectName("muted")
        stable_note.setWordWrap(True)

        sg.addWidget(self.chk_stable_mode, 0, 0, 1, 2)
        sg.addWidget(stable_hint, 0, 2, 1, 4)
        sg.addWidget(QLabel("寻路/入口等待"), 1, 0)
        sg.addWidget(self.spin_stable_path_wait, 1, 1)
        sg.addWidget(QLabel("BOSS战"), 1, 2)
        sg.addWidget(self.spin_stable_fight, 1, 3)
        sg.addWidget(QLabel("退出弹窗等待"), 1, 4)
        sg.addWidget(self.spin_stable_popup_wait, 1, 5)
        sg.addWidget(QLabel("加载等待"), 2, 0)
        sg.addWidget(self.spin_stable_load_wait, 2, 1)
        sg.addWidget(QLabel("回打怪点等待"), 2, 2)
        sg.addWidget(self.spin_stable_travel_wait, 2, 3)
        sg.addWidget(QLabel("入口关键词"), 3, 0)
        sg.addWidget(self.edit_stable_enter_kw, 3, 1, 1, 2)
        sg.addWidget(QLabel("退出关键词"), 3, 3)
        sg.addWidget(self.edit_stable_exit_kw, 3, 4, 1, 2)
        sg.addWidget(self.lbl_enter_click, 4, 0, 1, 2)
        sg.addWidget(self.btn_set_enter_click, 4, 2)
        sg.addWidget(self.lbl_exit_click, 4, 3, 1, 2)
        sg.addWidget(self.btn_set_exit_click, 4, 5)
        sg.addWidget(self.btn_stable_test, 5, 0, 1, 3)
        sg.addWidget(self.lbl_stable_state, 5, 3, 1, 3)
        sg.addWidget(stable_note, 6, 0, 1, 6)
        root.addWidget(box_stable)

        root.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(tab)
        return scroll

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

    def _clear_binding(self, update_status: bool = True) -> None:
        self.target_hwnd = None
        self.target_title = ""
        self._set_bound_label("尚未绑定窗口", "error")
        if update_status:
            self._set_status("绑定已清除，请重新选择目标窗口。", "warn")
        self._update_bind_button_state()

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
        if self._worker is not None or self._quitting:
            return
        if (
            self._one_key_running
            or self._single_player is not None
            or self._stable_runner is not None
            or self._route_editor is not None
        ):
            self._notify(
                "当前正在回放/一键打BOSS/路线编辑",
                "请先停止当前的回放、稳定测试或一键打BOSS，再启动基础挂机。",
            )
            return
        if self._recording_step is not None:
            self._notify("正在录制", "请先停止录制。")
            return
        if self._point_capture is not None or self._point_kind is not None:
            self._notify("正在设置点位", "请等待点位设置结束。")
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

        self._last_failure = None
        self._last_rounds = 0
        self.rounds_label.setText("已发送 0 轮")

        worker = KeySender(
            hwnd,
            self.spin_interval.value(),
            use_skills=self.chk_skills.isChecked(),
            use_potion=self.chk_potion.isChecked(),
            potion_interval_ms=self.spin_potion.value() * 1000,
            use_pickup=self.chk_pickup.isChecked(),
            use_click=self.chk_attack_click.isChecked(),
            parent=self,
        )
        worker.cycle_done.connect(self._on_cycle_done)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        self._stopping = False
        worker.start()

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
        self._stop_fatigue_monitor()

        self.btn_start.setText("正在停止…")
        self.btn_start.setEnabled(False)
        self.btn_start.setStyleSheet(START_BUTTON_STYLES["stopping"])
        self._set_status("正在停止，请稍候…", "warn")
        self._update_toggle_action()

    def _toggle_running(self) -> None:
        if self._quitting:
            return
        if self._one_key_running:
            self._stop_one_key()
            return
        if self._single_player is not None:
            self._stop_single_player()
            return
        if self._recording_step is not None:
            self._stop_recording()
            return
        if self._point_capture is not None or self._point_kind is not None:
            return
        if self._stable_runner is not None and not self._one_key_running:
            self._toggle_stable_test()
            return
        if self._stopping:
            return
        if self._worker is not None:
            self._stop_sending()
        else:
            self._start_sending()

    def _on_worker_finished(self) -> None:
        worker = self._worker
        self._worker = None

        rounds = worker.rounds if worker is not None else self._last_rounds
        self._last_rounds = rounds

        if worker is not None:
            worker.deleteLater()

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
        if self._quitting:
            return
        self._last_rounds = int(rounds)
        self.rounds_label.setText(f"已发送 {int(rounds)} 轮")
        if self._tray is not None and int(rounds) % 10 == 0:
            self._update_tray_tooltip(True)

    def _on_worker_failed(self, message: str) -> None:
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
        for spin_name in (
            "spin_fatigue_threshold",
            "spin_fatigue_threshold_boss",
        ):
            spin = getattr(self, spin_name, None)
            if spin is not None and spin.value() != value:
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)
        try:
            self._settings.setValue(
                "fatigue_threshold", self._fatigue_threshold
            )
        except Exception:
            pass
        self._refresh_threshold_labels()
        # 基础页正在做疲劳检测时，用新阈值重启监控
        if self._fatigue_monitor is not None:
            self._stop_fatigue_monitor()
            self._start_fatigue_monitor()

    def _refresh_threshold_labels(self) -> None:
        threshold = int(self._fatigue_threshold)
        low_trigger = -threshold + FATIGUE_BAND_MARGIN
        high_trigger = threshold - FATIGUE_BAND_MARGIN
        if hasattr(self, "lbl_threshold_hint"):
            self.lbl_threshold_hint.setText(
                f"低于 -{threshold} / 高于 +{threshold} 弹窗；"
                f"一键打BOSS：BOSS ≤ {low_trigger:.0f}，"
                f"刷怪 ≥ {high_trigger:.0f}"
            )
        if hasattr(self, "lbl_one_key_threshold"):
            self.lbl_one_key_threshold.setText(
                f"触发: BOSS ≤ {low_trigger:.0f}  /  刷怪 ≥ {high_trigger:.0f}"
            )

    # -- 辅助功能：技能 / 喝药 -------------------------------------------
    def _on_pickup_toggled(self, checked: bool) -> None:
        if self._worker is not None:
            self._worker.set_use_pickup(bool(checked))
        if self._one_key_worker is not None:
            self._one_key_worker.set_use_pickup(bool(checked))
        self._refresh_action_text()

    def _on_attack_click_toggled(self, checked: bool) -> None:
        if self._worker is not None:
            self._worker.set_use_click(bool(checked))
        if self._one_key_worker is not None:
            self._one_key_worker.set_use_click(bool(checked))
        self._refresh_action_text()

    def _refresh_action_text(self) -> None:
        if not hasattr(self, "lbl_action_text") or not hasattr(self, "chk_pickup"):
            return
        click = bool(getattr(self, "chk_attack_click", None) and self.chk_attack_click.isChecked())
        pickup = self.chk_pickup.isChecked()
        text = "动作：A 按下 50ms → 松开"
        if click:
            text += " → 随机鼠标左键"
        if pickup:
            text += " → 空格按下 50ms → 松开"
        if not click and not pickup:
            text += "（不点击、不拾取）"
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

    def _capture_game_image(self) -> Optional[QImage]:
        """只截取游戏客户区，不移动鼠标。"""
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return None
        cap, err = capture_client_bgra(self.target_hwnd)
        if cap is None:
            self._notify("截图失败", err or "无法截取游戏画面。")
            return None
        w, h, data = cap
        return QImage(bytes(data), w, h, w * 4, QImage.Format.Format_RGB32).copy()

    def _select_click_point(self, kind: str) -> None:
        """在游戏截图上框选一个固定点击点（入口/退出按钮）。"""
        if kind not in ("enter_click", "exit_click"):
            return
        if self._macro_busy():
            self._notify("当前正忙", "请先停止录制/回放/一键打BOSS。")
            return
        image = self._capture_game_image()
        if image is None or image.isNull():
            return
        name = "入口按钮" if kind == "enter_click" else "退出按钮"
        dialog = RegionSelectDialog(
            image,
            None,
            self._capture_game_image,
            self,
            title_text=f"框选{name}位置",
            info_text=(
                f"在游戏截图上框住{name}的点击区域。"
                "稳定模式会优先 OCR 找文字；OCR 找不到时会点击这个位置。"
                "请先把弹窗打开，再点本按钮截图。"
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
        nx = (roi[0] + roi[2] / 2.0) / cw
        ny = (roi[1] + roi[3] / 2.0) / ch
        self._macro_cfg["points"][kind] = (float(nx), float(ny))
        saved = self._save_macro_config()
        self._refresh_point_labels()
        self._set_status(
            f"{name}位置已保存：{nx * 100:.1f}%, {ny * 100:.1f}%"
            if saved
            else f"{name}位置已生效（配置写入失败，本次运行有效）。",
            "ok" if saved else "warn",
        )

    def _open_route_editor(self) -> None:
        if self._quitting:
            return
        if self._macro_busy():
            self._notify("当前正忙", "请先停止录制/回放/一键打BOSS/稳定测试。")
            return
        if self._worker is not None or self._test_monitor is not None:
            self._notify("当前正忙", "请先停止基础挂机/疲劳测试。")
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return
        if self._route_editor is not None:
            self._route_editor.show()
            self._route_editor.raise_()
            self._route_editor.activateWindow()
            return
        try:
            from route_editor import RouteEditor
        except Exception as exc:
            self._notify("路线编辑器不可用", f"加载失败：{exc}")
            return
        editor = RouteEditor(game_hwnd=self.target_hwnd, parent=self)
        editor.destroyed.connect(self._on_route_editor_destroyed)
        self._route_editor = editor
        editor.show()
        editor.raise_()
        editor.activateWindow()
        self._update_macro_button_states()

    def _on_route_editor_destroyed(self, *_args) -> None:
        self._route_editor = None
        self._update_macro_button_states()

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

    def _on_fatigue_alert(self, message: str) -> None:
        if self._quitting or getattr(self, "_popup", None) is None:
            return
        self._popup.show_message("疲劳值提醒", message, seconds=20)
        self.lbl_fatigue.setStyleSheet("color: #ff6b6b; padding: 0;")

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
                "正在执行疲劳检测（会在框选区域短暂点击并读取提示）…", "warn"
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

    # =====================================================================
    # BOOS 挂机 / 宏录制 / 点位 / 一键打BOSS
    # =====================================================================
    def _apply_one_key_button_style(self, running: bool) -> None:
        if not hasattr(self, "btn_one_key"):
            return
        if running:
            style = (
                "QPushButton#oneKeyButton {"
                " background:#8e2b20; color:#ffffff; border:1px solid #a9382c;"
                " border-radius:8px; font-size:15px; font-weight:700; }"
                "QPushButton#oneKeyButton:hover { background:#a3352a; }"
            )
        else:
            style = (
                "QPushButton#oneKeyButton {"
                " background:#c0392b; color:#ffffff; border:1px solid #d14a3b;"
                " border-radius:8px; font-size:15px; font-weight:700; }"
                "QPushButton#oneKeyButton:hover { background:#d34a3a; }"
            )
        self.btn_one_key.setStyleSheet(style)

    def _macro_busy(self) -> bool:
        return bool(
            self._one_key_running
            or self._single_player is not None
            or self._recording_step is not None
            or self._point_capture is not None
            or self._point_kind is not None
            or self._record_return_player is not None
            or self._stable_runner is not None
            or self._route_editor is not None
        )

    def _save_macro_settings(self) -> None:
        if not hasattr(self, "edit_map_key"):
            return
        map_key = self.edit_map_key.text().strip().lower() or "m"
        close_key = self.edit_close_key.text().strip().lower() or "esc"
        self.edit_map_key.setText(map_key)
        self.edit_close_key.setText(close_key)
        self._macro_cfg["map_key"] = map_key
        self._macro_cfg["close_map_key"] = close_key
        save_macro_config(self._macro_cfg)

    def _save_macro_config(self) -> bool:
        return save_macro_config(self._macro_cfg)

    def _refresh_point_labels(self) -> None:
        if not hasattr(self, "lbl_point_farm"):
            return
        for kind, label in (
            ("farm", self.lbl_point_farm),
            ("boss", self.lbl_point_boss),
        ):
            point = self._macro_cfg.get("points", {}).get(kind)
            if point:
                label.setText(
                    f"{'打怪点' if kind == 'farm' else 'BOSS点'}: "
                    f"已设置 ({point[0] * 100:.1f}%, {point[1] * 100:.1f}%)"
                )
            else:
                label.setText(
                    f"{'打怪点' if kind == 'farm' else 'BOSS点'}: 未设置"
                )
        busy = self._macro_busy()
        self.btn_set_farm.setEnabled(not busy)
        self.btn_set_boss.setEnabled(not busy)
        if hasattr(self, "lbl_enter_click"):
            for kind, label, btn in (
                ("enter_click", self.lbl_enter_click, self.btn_set_enter_click),
                ("exit_click", self.lbl_exit_click, self.btn_set_exit_click),
            ):
                point = self._macro_cfg.get("points", {}).get(kind)
                name = "入口按钮" if kind == "enter_click" else "退出按钮"
                if point:
                    label.setText(
                        f"{name}: 已设置 ({point[0] * 100:.1f}%, {point[1] * 100:.1f}%)"
                    )
                else:
                    label.setText(f"{name}: 未设置（仅 OCR）")
                btn.setEnabled(not busy)
        recording_waiting = (
            self._recording_step is not None and self._recorder is None
        )
        return_blocked = (
            self._one_key_running
            or self._single_player is not None
            or self._stable_runner is not None
            or self._point_capture is not None
            or self._point_kind is not None
            or self._record_return_player is not None
            or recording_waiting
        )
        target_ok = (
            self.target_hwnd is not None and self._window_alive(self.target_hwnd)
        )
        allow_return = (not return_blocked) and target_ok
        self.btn_return_farm.setEnabled(
            allow_return and self._macro_cfg["points"].get("farm") is not None
        )
        self.btn_return_boss.setEnabled(
            allow_return and self._macro_cfg["points"].get("boss") is not None
        )
        if hasattr(self, "edit_map_key"):
            self.edit_map_key.setEnabled(not busy)
            self.edit_close_key.setEnabled(not busy)

    def _refresh_macro_labels(self) -> None:
        if not hasattr(self, "_rec_status"):
            return
        for step in MACRO_STEPS:
            item = self._macro_cfg.get("recordings", {}).get(step, {})
            events = item.get("events", [])
            duration = float(item.get("duration", 0.0) or 0.0)
            if events:
                downs = sum(
                    1
                    for e in events
                    if isinstance(e, dict) and e.get("type") == "down"
                )
                ups = sum(
                    1
                    for e in events
                    if isinstance(e, dict) and e.get("type") == "up"
                )
                text = (
                    f"已录制 {len(events)} 个动作 / {duration:.1f} 秒 / "
                    f"点击 {downs} 次"
                )
                missing_clicks = ups - downs
                if missing_clicks > 0:
                    text += f" / 疑似丢点击 {missing_clicks} 次"
                meta = item.get("meta") if isinstance(item, dict) else None
                size = meta.get("client_size") if isinstance(meta, dict) else None
                has_norm = any(
                    isinstance(e, dict) and "nx" in e and "ny" in e
                    for e in events
                )
                if (
                    isinstance(size, (list, tuple))
                    and len(size) == 2
                    and int(size[0]) > 0
                    and int(size[1]) > 0
                ):
                    text += f" / 录制窗口 {int(size[0])}×{int(size[1])}"
                elif has_norm:
                    text += " / 标准客户区坐标"
                else:
                    text += " / 旧录制·按屏幕自适应"
                self._rec_status[step].setText(text)
            else:
                self._rec_status[step].setText("未录制")
        self._update_macro_button_states()
        self._refresh_point_labels()

    def _update_macro_button_states(self) -> None:
        if not hasattr(self, "_rec_buttons") or not hasattr(self, "btn_one_key"):
            return
        recording = self._recording_step
        playing = self._single_player is not None
        one_key = self._one_key_running
        point_busy = self._point_capture is not None or self._point_kind is not None
        stable_running = self._stable_runner is not None
        stable_test = stable_running and not one_key
        target_ok = (
            self.target_hwnd is not None and self._window_alive(self.target_hwnd)
        )
        other_busy = playing or one_key or point_busy or stable_running

        for step in MACRO_STEPS:
            events = (
                self._macro_cfg.get("recordings", {}).get(step, {}).get("events", [])
            )
            btn_rec, btn_play, btn_rerec = self._rec_buttons[step]
            if recording == step:
                btn_rec.setText("停止录制")
                btn_rec.setEnabled(True)
                btn_play.setText("开始")
                btn_play.setEnabled(False)
                btn_rerec.setEnabled(False)
            else:
                btn_rec.setText("录制")
                btn_rec.setEnabled(not recording and not other_busy and target_ok)
                btn_play.setText(
                    "停止"
                    if playing and self._single_stage == step
                    else "开始"
                )
                btn_play.setEnabled(
                    (playing and self._single_stage == step)
                    or (
                        not recording
                        and not one_key
                        and not point_busy
                        and not playing
                        and bool(events)
                    )
                )
                btn_rerec.setEnabled(not recording and not other_busy and target_ok)

        self.btn_one_key.setEnabled(not recording and not point_busy and not stable_test)
        self.chk_farm_potion.setEnabled(
            not one_key and not recording and not playing and not stable_test
        )
        if hasattr(self, "btn_stable_test"):
            self.btn_stable_test.setText(
                "停止稳定测试" if stable_test else "稳定模式 单次测试"
            )
            boss_point_ok = self._macro_cfg.get("points", {}).get("boss") is not None
            self.btn_stable_test.setEnabled(
                stable_test
                or (
                    not recording
                    and not one_key
                    and not point_busy
                    and not playing
                    and target_ok
                    and boss_point_ok
                )
            )
            stable_controls_enabled = not self._macro_busy()
            for widget in (
                self.chk_stable_mode,
                self.spin_stable_path_wait,
                self.spin_stable_fight,
                self.spin_stable_popup_wait,
                self.spin_stable_load_wait,
                self.spin_stable_travel_wait,
                self.edit_stable_enter_kw,
                self.edit_stable_exit_kw,
                self.btn_set_enter_click,
                self.btn_set_exit_click,
            ):
                widget.setEnabled(stable_controls_enabled)
        if hasattr(self, "btn_route_editor"):
            self.btn_route_editor.setEnabled(not self._macro_busy())
        if hasattr(self, "spin_fatigue_threshold_boss"):
            self.spin_fatigue_threshold_boss.setEnabled(not self._macro_busy())
        self.btn_set_farm.setEnabled(not self._macro_busy())
        self.btn_set_boss.setEnabled(not self._macro_busy())
        recording_waiting = (
            recording is not None and self._recorder is None
        )
        return_blocked = (
            playing
            or one_key
            or stable_running
            or point_busy
            or self._record_return_player is not None
            or recording_waiting
        )
        target_ok_return = (
            self.target_hwnd is not None and self._window_alive(self.target_hwnd)
        )
        allow_return = (not return_blocked) and target_ok_return
        self.btn_return_farm.setEnabled(
            allow_return and self._macro_cfg["points"].get("farm") is not None
        )
        self.btn_return_boss.setEnabled(
            allow_return and self._macro_cfg["points"].get("boss") is not None
        )
        self._update_bind_button_state()

    def _set_one_key_ui_running(self, running: bool) -> None:
        self.tab_basic.setEnabled(not running)
        if hasattr(self, "btn_one_key"):
            self.btn_one_key.setText(
                "停止一键打BOSS" if running else "一键打BOSS 开始"
            )
            self.btn_one_key.setEnabled(True)
            self._apply_one_key_button_style(running)
        self._update_macro_button_states()
        self._refresh_point_labels()
        self._update_toggle_action()

    def _one_key_set_state(self, text: str, level: str = "normal") -> None:
        if hasattr(self, "lbl_one_key_state"):
            self.lbl_one_key_state.setText(text)
        self._set_status(text, level)

    # ---------------------------------------------------------------- 录制
    def _start_recording(self, step: str, clear: bool = False) -> None:
        if step not in MACRO_STEPS:
            return
        if self._one_key_running:
            self._notify("正在一键打BOSS", "请先停止一键打BOSS，再录制流程。")
            return
        if self._worker is not None:
            self._notify("基础挂机运行中", "请先停止基础挂机，再录制流程。")
            return
        if (
            self._single_player is not None
            or self._test_monitor is not None
            or self._stable_runner is not None
            or self._route_editor is not None
        ):
            self._notify("正在回放/路线编辑", "请先结束当前回放/稳定测试/路线编辑。")
            return
        if self._point_capture is not None or self._point_kind is not None:
            self._notify("正在设置点位", "请等待点位设置结束，再录制流程。")
            return
        if self._recording_step is not None:
            if self._recording_step == step:
                self._stop_recording()
            else:
                self._notify(
                    "正在录制",
                    f"当前正在录制“{MACRO_TITLES[self._recording_step]}”，请先停止。",
                )
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return

        old_item = (
            self._macro_cfg.get("recordings", {}).get(step, {})
        )
        if not isinstance(old_item, dict):
            old_item = {}
        old_events = old_item.get("events", [])
        if not isinstance(old_events, list):
            old_events = []
        if (not clear) and old_events:
            answer = QMessageBox.question(
                self,
                "覆盖录制",
                f"“{MACRO_TITLES[step]}”已有录制，是否重新录制并覆盖？\n\n"
                "如果新录制为空或失败，会保留原录制。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        # 不立即清空旧录制：只有新录制真正录到动作后才覆盖；
        # 录制为空/失败时自动恢复这份备份，避免反复“重新录制”把旧数据弄丢。
        self._recording_backup = (
            (step, dict(old_item)) if old_events else None
        )

        self._recording_step = step
        self._update_macro_button_states()
        self._rec_status[step].setText(
            "3 秒后开始录制…（成功后覆盖）" if old_events else "3 秒后开始录制…"
        )
        self._set_status(
            f"准备录制：{MACRO_TITLES[step]}，3 秒后开始，请切到游戏。", "warn"
        )
        self._rec_start_timer = QTimer(self)
        self._rec_start_timer.setSingleShot(True)
        self._rec_start_timer.timeout.connect(
            lambda s=step: self._begin_recording(s)
        )
        self._rec_start_timer.start(3000)

    def _begin_recording(self, step: str) -> None:
        self._rec_start_timer = None
        if self._recording_step != step:
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._recording_step = None
            self._recording_backup = None
            self._refresh_macro_labels()
            self._notify("录制失败", "目标窗口已关闭。")
            return
        try:
            recorder = MacroRecorder(self.target_hwnd)
            recorder.start()
        except Exception as exc:
            self._recording_step = None
            self._recording_backup = None
            self._refresh_macro_labels()
            self._notify("录制失败", f"{type(exc).__name__}: {exc}")
            return
        self._recorder = recorder
        self._update_macro_button_states()
        self._refresh_point_labels()
        self._rec_status[step].setText("录制中… 0 个动作（等待游戏前台）")
        if self._rec_update_timer is not None:
            self._rec_update_timer.start(400)
        self._set_status(
            f"正在录制：{MACRO_TITLES[step]}。请在游戏里操作，完成后回本窗口点“停止录制”。",
            "ok",
        )

    def _stop_recording(self) -> None:
        step = self._recording_step
        if step is None:
            return

        record_return = self._record_return_player
        self._record_return_player = None
        if record_return is not None:
            record_return.stop()
            if not record_return.wait(2500):
                record_return.terminate()
                record_return.wait(500)
            record_return.deleteLater()

        if self._rec_start_timer is not None:
            self._rec_start_timer.stop()
            self._rec_start_timer = None
            self._recording_step = None
            self._recording_backup = None
            self._refresh_macro_labels()
            self._set_status("已取消录制。", "normal")
            return

        recorder = self._recorder
        self._recorder = None
        if self._rec_update_timer is not None:
            self._rec_update_timer.stop()

        backup = self._recording_backup
        self._recording_backup = None
        meta = recorder.meta() if recorder is not None else {}
        events = recorder.stop() if recorder is not None else []
        duration = float(events[-1]["t"]) if events else 0.0
        if events:
            self._macro_cfg["recordings"][step] = {
                "events": events,
                "duration": duration,
                "meta": meta,
            }
            self._save_macro_config()
            self._recording_step = None
            self._refresh_macro_labels()
            click_count = sum(
                1 for e in events if isinstance(e, dict) and e.get("type") == "down"
            )
            self._set_status(
                f"录制完成：{MACRO_TITLES[step]}，{len(events)} 个动作 / "
                f"点击 {click_count} 次 / {duration:.1f} 秒。",
                "ok",
            )
            return

        # 录制为空：不要把旧录制覆盖掉。
        if backup is not None and backup[0] == step and backup[1].get("events"):
            self._macro_cfg["recordings"][step] = backup[1]
            self._save_macro_config()
            self._recording_step = None
            self._refresh_macro_labels()
            self._set_status(
                "录制为空：没有捕获到鼠标/键盘操作，已保留原录制。",
                "warn",
            )
            return

        self._macro_cfg["recordings"][step] = {
            "events": [],
            "duration": 0.0,
            "meta": {},
        }
        self._save_macro_config()
        self._recording_step = None
        self._refresh_macro_labels()
        self._set_status("录制为空：没有捕获到游戏内的鼠标/键盘操作。", "warn")

    def _update_recording_status(self) -> None:
        step = self._recording_step
        if step is None or self._recorder is None:
            if self._rec_update_timer is not None:
                self._rec_update_timer.stop()
            return
        text = (
            f"录制中… {self._recorder.event_count()} 个动作 / "
            f"点击 {self._recorder.click_count()} 次 / "
            f"{self._recorder.duration():.1f} 秒"
        )
        if self.target_hwnd is None or not _foreground_is_window(self.target_hwnd):
            text += "（等待游戏前台）"
        self._rec_status[step].setText(text)

    # ---------------------------------------------------------------- 回放
    def _play_recording(self, step: str) -> None:
        if step not in MACRO_STEPS:
            return
        if self._single_player is not None:
            if self._single_stage == step:
                self._stop_single_player()
            else:
                self._notify("正在回放", "请等待当前回放结束。")
            return
        if self._one_key_running:
            self._notify("正在一键打BOSS", "请先停止一键打BOSS。")
            return
        if self._stable_runner is not None:
            self._notify("正在稳定模式测试", "请先停止稳定模式测试。")
            return
        if self._route_editor is not None:
            self._notify("正在路线编辑", "请先关闭路线编辑器。")
            return
        if self._worker is not None:
            self._notify("基础挂机运行中", "请先停止基础挂机。")
            return
        if self._recording_step is not None:
            self._notify("正在录制", "请先停止录制。")
            return
        if self._point_capture is not None:
            self._notify("正在设置点位", "请等待点位设置结束。")
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return
        item = self._macro_cfg.get("recordings", {}).get(step, {})
        events = item.get("events", [])
        if not events:
            self._notify("没有录制", f"“{MACRO_TITLES[step]}”还没有录制。")
            return

        player = MacroPlayer(
            self.target_hwnd,
            step,
            events,
            restore_foreground=True,
            restore_cursor=True,
            meta=item.get("meta"),
            scale_coords=True,
            parent=self,
        )
        player.done.connect(self._on_single_player_done)
        player.error.connect(self._on_single_player_error)
        self._single_player = player
        self._single_stage = step
        self._update_macro_button_states()
        self._set_status(f"正在回放：{MACRO_TITLES[step]}…", "ok")
        player.start()

    def _stop_single_player(self) -> None:
        player = self._single_player
        self._single_player = None
        self._single_stage = ""
        if player is not None:
            player.stop()
            if not player.wait(2500):
                player.terminate()
                player.wait(500)
            player.deleteLater()
        if not self._one_key_running:
            self._refresh_macro_labels()
            self._set_status("回放已停止。", "warn")

    def _on_single_player_done(self, stage: str) -> None:
        player = self.sender()
        if player is not self._single_player:
            return
        self._single_player = None
        self._single_stage = ""
        if isinstance(player, MacroPlayer):
            player.deleteLater()
        self._refresh_macro_labels()
        title = MACRO_TITLES.get(stage, "点位回归")
        self._set_status(f"回放完成：{title}。", "ok")

    def _on_single_player_error(self, stage: str, message: str) -> None:
        player = self.sender()
        if player is self._single_player:
            self._single_player = None
            self._single_stage = ""
            if isinstance(player, MacroPlayer):
                player.deleteLater()
        self._refresh_macro_labels()
        self._set_status(f"回放失败：{message}", "error")
        try:
            self._popup.show_message("回放失败", message, seconds=12)
        except Exception:
            pass

    # ---------------------------------------------------------------- 点位
    def _start_point_capture(self, kind: str) -> None:
        if kind not in ("farm", "boss"):
            return
        if self._macro_busy():
            self._notify("当前正忙", "请先停止录制/回放/一键打BOSS。")
            return
        if self._worker is not None:
            self._notify("基础挂机运行中", "请先停止基础挂机。")
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return
        self._point_kind = kind
        self._update_macro_button_states()
        self._refresh_point_labels()
        self._set_status(
            f"准备设置{'打怪点' if kind == 'farm' else 'BOSS点'}："
            "3 秒后请在地图上左键点击点位。",
            "warn",
        )
        QTimer.singleShot(3000, lambda k=kind: self._begin_point_capture(k))

    def _begin_point_capture(self, kind: str) -> None:
        if self._point_kind != kind:
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._cancel_point_capture()
            return
        try:
            capture = PointCapture(
                self.target_hwnd,
                kind,
                lambda k, point: self.point_captured.emit(k, point),
            )
            capture.start()
        except Exception as exc:
            self._cancel_point_capture()
            self._notify("点位捕获失败", f"{type(exc).__name__}: {exc}")
            return
        self._point_capture = capture
        self._set_status("捕获中：请在游戏地图上左键点击点位（10 秒内）。", "warn")
        self._point_timeout_timer = QTimer(self)
        self._point_timeout_timer.setSingleShot(True)
        self._point_timeout_timer.timeout.connect(
            lambda: self._cancel_point_capture(timeout=True)
        )
        self._point_timeout_timer.start(10000)

    def _cancel_point_capture(self, timeout: bool = False) -> None:
        capture = self._point_capture
        self._point_capture = None
        if capture is not None:
            capture.cancel()
        if self._point_timeout_timer is not None:
            self._point_timeout_timer.stop()
            self._point_timeout_timer = None
        self._point_kind = None
        self._update_macro_button_states()
        self._refresh_point_labels()
        if timeout:
            self._set_status("未捕获到地图点击，点位设置已取消。", "warn")

    def _on_point_captured(self, kind: str, point) -> None:
        if kind not in ("farm", "boss"):
            return
        try:
            nx, ny = float(point[0]), float(point[1])
        except Exception:
            return
        if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
            return
        self._macro_cfg["points"][kind] = (nx, ny)
        self._save_macro_config()
        self._point_capture = None
        if self._point_timeout_timer is not None:
            self._point_timeout_timer.stop()
            self._point_timeout_timer = None
        self._point_kind = None
        self._update_macro_button_states()
        self._refresh_point_labels()
        name = "打怪点" if kind == "farm" else "BOSS点"
        self._set_status(f"{name}已设置：{nx * 100:.1f}%, {ny * 100:.1f}%", "ok")

    def _build_return_events(self, kind: str) -> Optional[list[dict]]:
        """生成“打开地图 → 点击点位 → 关闭地图”的时间线。"""
        if kind not in ("farm", "boss"):
            return None
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            return None
        point = self._macro_cfg.get("points", {}).get(kind)
        if point is None:
            return None
        map_key = self._macro_cfg.get("map_key", "m")
        close_key = self._macro_cfg.get("close_map_key", "esc")
        try:
            left, top, right, bottom = win32gui.GetClientRect(self.target_hwnd)
            cw, ch = int(right - left), int(bottom - top)
            if cw <= 0 or ch <= 0:
                return None
            client_x = int(round(float(point[0]) * cw))
            client_y = int(round(float(point[1]) * ch))
            sx, sy = win32gui.ClientToScreen(
                self.target_hwnd, (client_x, client_y)
            )
        except Exception:
            return None
        return [
            {"t": 0.0, "type": "key_down", "name": map_key},
            {"t": 0.04, "type": "key_up", "name": map_key},
            {"t": 1.10, "type": "move", "x": int(sx), "y": int(sy)},
            {"t": 1.16, "type": "down", "x": int(sx), "y": int(sy), "button": "left"},
            {"t": 1.24, "type": "up", "x": int(sx), "y": int(sy), "button": "left"},
            {"t": 1.95, "type": "key_down", "name": close_key},
            {"t": 1.99, "type": "key_up", "name": close_key},
        ]

    def _return_to_point_while_recording(self, kind: str) -> None:
        """录制过程中执行一次回归点位，并让录制钩子把动作录进去。"""
        if self._record_return_player is not None:
            self._notify("正在回归点位", "请等待当前回归动作结束。")
            return
        if self._recorder is None:
            self._notify(
                "录制还没开始",
                "请等 3 秒倒计时结束、录制真正开始后，再点回归按钮。",
            )
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return
        point = self._macro_cfg.get("points", {}).get(kind)
        if point is None:
            self._notify("未设置点位", "请先设置对应的打怪点/BOSS点。")
            return
        events = self._build_return_events(kind)
        if events is None:
            self._notify(
                "回归失败",
                "请检查点位、地图键和关闭键。",
            )
            return

        stage = "rec_return_farm" if kind == "farm" else "rec_return_boss"
        player = MacroPlayer(
            self.target_hwnd,
            stage,
            events,
            restore_foreground=False,
            restore_cursor=False,
            capture_friendly_keys=True,
            scale_coords=False,
            parent=self,
        )
        player.done.connect(self._on_record_return_done)
        player.error.connect(self._on_record_return_error)
        self._record_return_player = player
        self._update_macro_button_states()
        self._refresh_point_labels()
        name = "打怪点" if kind == "farm" else "BOSS点"
        self._set_status(
            f"录制中：正在回归{name}（这次回归会录入当前流程）…", "warn"
        )
        player.start()

    def _on_record_return_done(self, stage: str) -> None:
        player = self.sender()
        if player is not self._record_return_player:
            return
        self._record_return_player = None
        if isinstance(player, MacroPlayer):
            player.deleteLater()
        self._update_macro_button_states()
        self._refresh_point_labels()
        if self._recording_step is not None:
            self._set_status(
                "回归动作已完成并录入，继续录制后续操作。", "ok"
            )

    def _on_record_return_error(self, stage: str, message: str) -> None:
        player = self.sender()
        if player is self._record_return_player:
            self._record_return_player = None
            if isinstance(player, MacroPlayer):
                player.deleteLater()
        self._update_macro_button_states()
        self._refresh_point_labels()
        if self._recording_step is not None:
            self._set_status(f"录制中回归失败：{message}", "error")

    def _return_to_point(self, kind: str) -> None:
        if self._recording_step is not None:
            self._return_to_point_while_recording(kind)
            return
        if self._macro_busy():
            self._notify("当前正忙", "请先停止录制/回放/一键打BOSS。")
            return
        if self._worker is not None:
            self._notify("基础挂机运行中", "请先停止基础挂机。")
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return
        point = self._macro_cfg.get("points", {}).get(kind)
        if point is None:
            self._notify("未设置点位", "请先点击对应的“设置点位”按钮。")
            return
        self._save_macro_settings()
        events = self._build_return_events(kind)
        if events is None:
            self._notify(
                "点位无效",
                "请先设置该点位，并确认地图键/关闭键可用。",
            )
            return
        player = MacroPlayer(
            self.target_hwnd,
            "return_farm" if kind == "farm" else "return_boss",
            events,
            restore_foreground=True,
            restore_cursor=True,
            scale_coords=False,
            parent=self,
        )
        player.done.connect(self._on_single_player_done)
        player.error.connect(self._on_single_player_error)
        self._single_player = player
        self._single_stage = "return_farm" if kind == "farm" else "return_boss"
        self._update_macro_button_states()
        name = "打怪点" if kind == "farm" else "BOSS点"
        self._set_status(f"正在回归{name}：打开地图 → 点击点位 → 关闭地图…", "ok")
        player.start()

    # ------------------------------------------------------------ 一键打BOSS
    def _fatigue_trigger_bounds(self) -> tuple[float, float]:
        threshold = float(self._fatigue_threshold)
        return (
            -threshold + FATIGUE_BAND_MARGIN,
            threshold - FATIGUE_BAND_MARGIN,
        )

    def _toggle_one_key(self) -> None:
        if self._one_key_running:
            self._stop_one_key()
        else:
            self._start_one_key()

    def _start_one_key(self) -> None:
        if self._one_key_running or self._quitting:
            return
        if self._worker is not None:
            self._notify("基础挂机运行中", "请先停止基础挂机，再启动一键打BOSS。")
            return
        if self._single_player is not None or self._recording_step is not None:
            self._notify("当前正忙", "请先停止回放/录制。")
            return
        if (
            self._stable_runner is not None
            or self._stable_test_running
            or self._route_editor is not None
        ):
            self._notify("当前正忙", "请先停止稳定模式测试/路线编辑。")
            return
        if self._point_capture is not None or self._point_kind is not None:
            self._notify("当前正忙", "请等待点位设置结束。")
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return
        if self._fatigue_roi_norm is None:
            self._notify(
                "尚未框选疲劳区域",
                "请先在“基础挂机”页点击“框选区域”，框住右下角会弹出疲劳提示的图标/按钮。",
            )
            return

        self._one_key_stable = bool(self.chk_stable_mode.isChecked())
        if not self._one_key_stable:
            missing = [
                MACRO_TITLES[step]
                for step in MACRO_STEPS
                if not self._macro_cfg.get("recordings", {})
                .get(step, {})
                .get("events", [])
            ]
            if missing:
                self._notify(
                    "录制不完整",
                    "一键打BOSS需要先录制：\n\n" + "\n".join(f"· {m}" for m in missing),
                )
                return

        missing_points = [
            name
            for kind, name in (("farm", "打怪点"), ("boss", "BOSS点"))
            if self._macro_cfg.get("points", {}).get(kind) is None
        ]
        if missing_points:
            self._notify(
                "点位未设置",
                "一键打BOSS需要先设置：\n\n"
                + "\n".join(f"· {name}" for name in missing_points),
            )
            return

        self._save_macro_settings()
        self._one_key_running = True
        self._one_key_farm_base = 0
        self._one_key_total_farm_rounds = 0
        self._one_key_prev_fg = 0
        self._one_key_prev_cursor = None
        self._one_key_post_boss_retry = 0
        self.lbl_boss_rounds.setText("刷怪: 0 轮")
        self.lbl_boss_fatigue.setText("疲劳: --/--")
        self.lbl_stable_state.setText(
            "稳定模式：准备中…" if self._one_key_stable else "未运行"
        )
        self._set_one_key_ui_running(True)
        self._one_key_set_state("检测初始疲劳…", "warn")

        monitor = FatigueMonitor(
            self.target_hwnd,
            roi_norm=self._fatigue_roi_norm,
            interval_seconds=FATIGUE_CHECK_SECONDS,
            one_shot=True,
            parent=self,
        )
        monitor.result.connect(self._on_one_key_initial_result)
        monitor.finished.connect(self._on_one_key_initial_monitor_finished)
        self._one_key_initial_monitor = monitor
        monitor.start()

    def _on_one_key_initial_monitor_finished(self) -> None:
        monitor = self.sender()
        if monitor is self._one_key_initial_monitor:
            self._one_key_initial_monitor = None
            if isinstance(monitor, FatigueMonitor):
                monitor.deleteLater()

    def _on_one_key_initial_result(self, current, maximum, message: str) -> None:
        if not self._one_key_running:
            return
        if current is None:
            self._one_key_set_state(f"初始疲劳检测失败，先刷怪：{message}", "warn")
            self._start_farming_phase()
            return
        self.lbl_boss_fatigue.setText(f"疲劳: {int(current)}/{int(maximum)}")
        low_trigger, high_trigger = self._fatigue_trigger_bounds()
        if current <= low_trigger:
            self._one_key_set_state(
                f"初始疲劳 {current}/{maximum} ≤ {low_trigger:.0f}，进入BOSS流程。",
                "warn",
            )
            if self._one_key_stable:
                self._begin_stable_boss_sequence()
            else:
                self._begin_boss_sequence()
        else:
            self._one_key_set_state(
                f"初始疲劳 {current}/{maximum}，开始刷怪。", "ok"
            )
            self._start_farming_phase()

    def _start_farming_phase(self) -> None:
        if not self._one_key_running:
            return
        self._stop_farming_phase()
        self._one_key_state = "farming"
        self._one_key_set_state("刷怪中（每 30 秒检测疲劳）", "ok")
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._stop_one_key("目标窗口已关闭。")
            return
        worker = KeySender(
            self.target_hwnd,
            self.spin_interval.value(),
            use_skills=self.chk_skills.isChecked(),
            use_potion=self.chk_farm_potion.isChecked(),
            potion_interval_ms=self.spin_potion.value() * 1000,
            attack_enabled=True,
            potion_immediate=False,
            use_pickup=self.chk_pickup.isChecked(),
            use_click=self.chk_attack_click.isChecked(),
            parent=self,
        )
        worker.cycle_done.connect(self._on_one_key_farm_cycle)
        worker.failed.connect(self._on_one_key_farm_failed)
        self._one_key_farm_base = self._one_key_total_farm_rounds
        self._one_key_worker = worker
        worker.start()

        monitor = FatigueMonitor(
            self.target_hwnd,
            roi_norm=self._fatigue_roi_norm,
            interval_seconds=FATIGUE_CHECK_SECONDS,
            one_shot=False,
            parent=self,
        )
        monitor.result.connect(self._on_one_key_fatigue_result)
        monitor.finished.connect(self._on_one_key_fatigue_finished)
        self._one_key_fatigue = monitor
        monitor.start()

    def _stop_farming_phase(self) -> None:
        monitor = self._one_key_fatigue
        self._one_key_fatigue = None
        if monitor is not None:
            monitor.stop()
            if not monitor.wait(2500):
                monitor.terminate()
                monitor.wait(500)
            monitor.deleteLater()

        worker = self._one_key_worker
        self._one_key_worker = None
        if worker is not None:
            worker.stop()
            if not worker.wait(2500):
                worker.terminate()
                worker.wait(500)
            worker.deleteLater()

    def _on_one_key_fatigue_finished(self) -> None:
        monitor = self.sender()
        if monitor is self._one_key_fatigue:
            self._one_key_fatigue = None
            if isinstance(monitor, FatigueMonitor):
                monitor.deleteLater()

    def _on_one_key_farm_cycle(self, rounds: int) -> None:
        if not self._one_key_running or self._one_key_state != "farming":
            return
        total = self._one_key_farm_base + int(rounds)
        self._one_key_total_farm_rounds = total
        self.lbl_boss_rounds.setText(f"刷怪: {total} 轮")

    def _on_one_key_farm_failed(self, message: str) -> None:
        if self._one_key_running:
            self._stop_one_key(f"刷怪线程停止：{message}")

    def _on_one_key_fatigue_result(self, current, maximum, message: str) -> None:
        if not self._one_key_running or self._one_key_state != "farming":
            return
        if current is None:
            self._one_key_set_state(f"疲劳检测失败，继续刷怪：{message}", "warn")
            return
        self.lbl_boss_fatigue.setText(f"疲劳: {int(current)}/{int(maximum)}")
        low_trigger, high_trigger = self._fatigue_trigger_bounds()
        if current <= low_trigger:
            self._one_key_set_state(
                f"疲劳 {current}/{maximum} ≤ {low_trigger:.0f}，进入BOSS流程。",
                "warn",
            )
            if self._one_key_stable:
                self._begin_stable_boss_sequence()
            else:
                self._begin_boss_sequence()
        elif current >= high_trigger:
            self._one_key_set_state(
                f"疲劳 {current}/{maximum} ≥ +{high_trigger:.0f}，继续刷怪。",
                "ok",
            )
        else:
            self._one_key_set_state(
                f"刷怪中（疲劳 {current}/{maximum}，每 30 秒检测）", "ok"
            )

    # ------------------------------------------------------------ 稳定 BOSS（不录制）
    @staticmethod
    def _split_keywords(text: str, defaults: list[str]) -> list[str]:
        parts = [p.strip() for p in re.split(r"[,，;；\s]+", text or "") if p.strip()]
        return parts or list(defaults)

    def _stable_cfg(self) -> dict:
        points = self._macro_cfg.get("points", {})
        return {
            "points": points,
            "map_key": self._macro_cfg.get("map_key", "m"),
            "close_key": self._macro_cfg.get("close_map_key", "esc"),
            "path_wait": float(self.spin_stable_path_wait.value()),
            "fight_seconds": float(self.spin_stable_fight.value()),
            "popup_wait": float(self.spin_stable_popup_wait.value()),
            "load_wait": float(self.spin_stable_load_wait.value()),
            "travel_wait": float(self.spin_stable_travel_wait.value()),
            "post_boss_wait": 1.0,
            "enter_keywords": self._split_keywords(
                self.edit_stable_enter_kw.text(), ["进入", "挑战", "确认"]
            ),
            "exit_keywords": self._split_keywords(
                self.edit_stable_exit_kw.text(), ["离开", "退出", "确认"]
            ),
            "attack_interval_ms": int(self.spin_interval.value()),
            "potion_interval_ms": int(self.spin_potion.value()) * 1000,
            "use_skills": bool(self.chk_skills.isChecked()),
            "use_pickup": bool(self.chk_pickup.isChecked()),
        }

    def _toggle_stable_test(self) -> None:
        if self._stable_runner is not None:
            if self._one_key_running:
                return
            self._stop_stable_runner()
            self._stable_test_running = False
            self.lbl_stable_state.setText("已停止")
            self._set_status("稳定模式单次测试已停止。", "warn")
            self._update_macro_button_states()
            return
        self._start_stable_test()

    def _start_stable_test(self) -> None:
        if self._quitting or self._one_key_running or self._stable_runner is not None:
            return
        if self._route_editor is not None:
            self._notify("正在路线编辑", "请先关闭路线编辑器。")
            return
        if self._worker is not None:
            self._notify("基础挂机运行中", "请先停止基础挂机，再测试稳定模式。")
            return
        if (
            self._single_player is not None
            or self._recording_step is not None
            or self._recorder is not None
            or self._test_monitor is not None
            or self._point_capture is not None
            or self._point_kind is not None
        ):
            self._notify("当前正忙", "请先停止录制/回放/点位设置/疲劳测试。")
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return
        if self._macro_cfg.get("points", {}).get("boss") is None:
            self._notify("未设置BOSS点", "请先在点位设置里设置 BOSS 点（地图上的副本入口）。")
            return

        try:
            from stable_boss import StableBossRunner
        except Exception as exc:
            self._notify("稳定模式不可用", f"加载 stable_boss 失败：{exc}")
            return

        self._save_macro_settings()
        self._stable_test_running = True
        runner = StableBossRunner(
            self.target_hwnd,
            self._stable_cfg(),
            mode="boss",
            parent=self,
        )
        runner.status.connect(self._on_stable_status)
        runner.done.connect(self._on_stable_done)
        runner.failed.connect(self._on_stable_failed)
        self._stable_runner = runner
        self.lbl_stable_state.setText("单次测试：准备中…")
        self._set_status("稳定模式单次测试开始。", "warn")
        self._update_macro_button_states()
        runner.start()

    def _begin_stable_boss_sequence(self) -> None:
        if not self._one_key_running or self._stable_runner is not None:
            return
        if self._macro_cfg.get("points", {}).get("boss") is None:
            self._stop_one_key("未设置BOSS点（地图上的副本入口）。")
            return

        self._stop_farming_phase()
        if not self._one_key_prev_fg:
            try:
                self._one_key_prev_fg = int(win32gui.GetForegroundWindow() or 0)
                self._one_key_prev_cursor = win32api.GetCursorPos()
            except Exception:
                self._one_key_prev_fg = 0
                self._one_key_prev_cursor = None
        self._one_key_post_boss_retry = 0
        self._one_key_state = "stable_boss"
        self._one_key_set_state("稳定模式：前往BOSS点并识别入口…", "warn")
        self.lbl_stable_state.setText("稳定模式：前往BOSS点…")

        try:
            from stable_boss import StableBossRunner
        except Exception as exc:
            self._stop_one_key(f"加载 stable_boss 失败：{exc}")
            return

        runner = StableBossRunner(
            self.target_hwnd,
            self._stable_cfg(),
            mode="boss",
            parent=self,
        )
        runner.status.connect(self._on_stable_status)
        runner.done.connect(self._on_stable_done)
        runner.failed.connect(self._on_stable_failed)
        self._stable_runner = runner
        self._update_macro_button_states()
        runner.start()

    def _start_stable_travel(self, kind: str = "farm") -> None:
        if not self._one_key_running or self._stable_runner is not None:
            return
        if self._macro_cfg.get("points", {}).get(kind) is None:
            self._stop_one_key("未设置打怪点。")
            return
        try:
            from stable_boss import StableBossRunner
        except Exception as exc:
            self._stop_one_key(f"加载 stable_boss 失败：{exc}")
            return
        self._one_key_state = "stable_travel"
        self._one_key_set_state("稳定模式：返回打怪点…", "warn")
        self.lbl_stable_state.setText("稳定模式：返回打怪点…")
        runner = StableBossRunner(
            self.target_hwnd,
            self._stable_cfg(),
            mode="travel",
            point_key=kind,
            parent=self,
        )
        runner.status.connect(self._on_stable_status)
        runner.done.connect(self._on_stable_done)
        runner.failed.connect(self._on_stable_failed)
        self._stable_runner = runner
        self._update_macro_button_states()
        runner.start()

    def _on_stable_status(self, text: str, level: str) -> None:
        if hasattr(self, "lbl_stable_state"):
            self.lbl_stable_state.setText(text)
        if self._one_key_running:
            self._one_key_set_state(text, level)
        else:
            self._set_status(text, level)

    def _on_stable_done(self, mode: str) -> None:
        runner = self.sender()
        if runner is self._stable_runner:
            self._stable_runner = None
            if isinstance(runner, QThread):
                runner.wait(500)
                runner.deleteLater()

        if self._stable_test_running:
            self._stable_test_running = False
            self.lbl_stable_state.setText("单次测试完成（当前在副本外）")
            self._set_status("稳定模式单次测试完成。", "ok")
            self._update_macro_button_states()
            return

        if not self._one_key_running:
            return
        if mode == "boss":
            self._start_one_key_post_boss_check()
        elif mode == "travel":
            self._restore_one_key_focus()
            self._one_key_set_state("已回到打怪点，继续刷怪。", "ok")
            self.lbl_stable_state.setText("稳定模式：刷怪中")
            self._start_farming_phase()

    def _on_stable_failed(self, message: str) -> None:
        runner = self.sender()
        if runner is self._stable_runner:
            self._stable_runner = None
            if isinstance(runner, QThread):
                runner.wait(500)
                runner.deleteLater()

        stopped = str(message).strip() == "已停止。"
        if self._stable_test_running:
            self._stable_test_running = False
            if stopped:
                self.lbl_stable_state.setText("已停止")
                self._set_status("稳定模式单次测试已停止。", "warn")
            else:
                self.lbl_stable_state.setText("单次测试失败")
                self._set_status(f"稳定模式测试失败：{message}", "error")
                try:
                    self._popup.show_message(
                        "稳定模式测试失败", message, seconds=15
                    )
                except Exception:
                    pass
            self._update_macro_button_states()
            return

        if self._one_key_running:
            if stopped:
                self._stop_one_key()
            else:
                self._stop_one_key(f"稳定模式 BOSS 失败：{message}")

    def _stop_stable_runner(self) -> None:
        runner = self._stable_runner
        self._stable_runner = None
        if runner is not None:
            runner.stop()
            if not runner.wait(3500):
                runner.terminate()
                runner.wait(500)
            runner.deleteLater()

    def _begin_boss_sequence(self) -> None:
        if not self._one_key_running:
            return
        missing = [
            MACRO_TITLES[step]
            for step in MACRO_STEPS
            if not self._macro_cfg.get("recordings", {})
            .get(step, {})
            .get("events", [])
        ]
        if missing:
            self._stop_one_key("录制不完整：" + "、".join(missing))
            return
        missing_points = [
            name
            for kind, name in (("farm", "打怪点"), ("boss", "BOSS点"))
            if self._macro_cfg.get("points", {}).get(kind) is None
        ]
        if missing_points:
            self._stop_one_key("点位未设置：" + "、".join(missing_points))
            return

        self._stop_farming_phase()
        try:
            self._one_key_prev_fg = int(win32gui.GetForegroundWindow() or 0)
            self._one_key_prev_cursor = win32api.GetCursorPos()
        except Exception:
            self._one_key_prev_fg = 0
            self._one_key_prev_cursor = None
        self._one_key_post_boss_retry = 0

        # 先去打怪点，再走“打怪点 → BOSS点”的录制路线
        self._play_one_key_return("farm")

    def _play_one_key_return(self, kind: str) -> None:
        if not self._one_key_running:
            return
        stage = "return_farm" if kind == "farm" else "return_boss"
        events = self._build_return_events(kind)
        if events is None:
            self._stop_one_key(
                f"无法生成“{ONE_KEY_STAGE_TITLES[stage]}”动作，"
                "请检查点位、地图键和关闭键。"
            )
            return
        self._one_key_state = stage
        self._one_key_stage = stage
        self._one_key_set_state(f"{ONE_KEY_STAGE_TITLES[stage]}…", "warn")
        player = MacroPlayer(
            self.target_hwnd,
            stage,
            events,
            restore_foreground=False,
            restore_cursor=False,
            scale_coords=False,
            parent=self,
        )
        player.done.connect(self._on_one_key_macro_done)
        player.error.connect(self._on_one_key_macro_error)
        self._one_key_player = player
        player.start()

    def _play_one_key_macro(self, stage: str) -> None:
        if not self._one_key_running:
            return
        item = self._macro_cfg.get("recordings", {}).get(stage, {})
        events = item.get("events", [])
        if not events:
            self._stop_one_key(f"“{MACRO_TITLES[stage]}”没有录制内容。")
            return
        player = MacroPlayer(
            self.target_hwnd,
            stage,
            events,
            restore_foreground=False,
            restore_cursor=False,
            meta=item.get("meta"),
            scale_coords=True,
            parent=self,
        )
        player.done.connect(self._on_one_key_macro_done)
        player.error.connect(self._on_one_key_macro_error)
        self._one_key_player = player
        self._one_key_stage = stage
        player.start()

    def _start_boss_fight_phase(self) -> None:
        if not self._one_key_running:
            return
        self._one_key_state = "boss_fight"
        self._one_key_set_state("BOSS战斗中（强制喝药 Q）", "warn")
        self._start_one_key_potion()
        self._play_one_key_macro("boss_fight")

    def _return_to_farm_from_boss(self) -> None:
        if not self._one_key_running:
            return
        if self._one_key_stable:
            self._start_stable_travel("farm")
            return
        self._one_key_state = "to_farm"
        self._one_key_stage = "to_farm"
        self._one_key_set_state("返回打怪点…", "warn")
        self._play_one_key_macro("to_farm")

    def _stop_one_key_post_boss_monitor(self) -> None:
        monitor = self._one_key_post_boss_monitor
        self._one_key_post_boss_monitor = None
        if monitor is not None:
            monitor.stop()
            if not monitor.wait(2500):
                monitor.terminate()
                monitor.wait(500)
            monitor.deleteLater()

    def _start_one_key_post_boss_check(self) -> None:
        if not self._one_key_running:
            return
        self._stop_one_key_post_boss_monitor()
        self._one_key_state = "post_boss_check"
        self._one_key_set_state(
            "检测BOSS后疲劳…" if self._one_key_stable else "回归BOSS点完成，检测疲劳…",
            "warn",
        )
        monitor = FatigueMonitor(
            self.target_hwnd,
            roi_norm=self._fatigue_roi_norm,
            interval_seconds=FATIGUE_CHECK_SECONDS,
            one_shot=True,
            parent=self,
        )
        monitor.result.connect(self._on_one_key_post_boss_result)
        monitor.finished.connect(self._on_one_key_post_boss_finished)
        self._one_key_post_boss_monitor = monitor
        monitor.start()

    def _on_one_key_post_boss_finished(self) -> None:
        monitor = self.sender()
        if monitor is self._one_key_post_boss_monitor:
            self._one_key_post_boss_monitor = None
            if isinstance(monitor, FatigueMonitor):
                monitor.deleteLater()

    def _on_one_key_post_boss_result(self, current, maximum, message: str) -> None:
        if not self._one_key_running or self._one_key_state != "post_boss_check":
            return
        if current is None:
            if self._one_key_post_boss_retry < 1:
                self._one_key_post_boss_retry += 1
                self._one_key_set_state(
                    f"BOSS后疲劳检测失败，重试：{message}", "warn"
                )
                QTimer.singleShot(500, self._start_one_key_post_boss_check)
            else:
                self._one_key_post_boss_retry = 0
                self._one_key_set_state(
                    "BOSS后疲劳检测失败，先返回打怪点。", "warn"
                )
                self._return_to_farm_from_boss()
            return

        self._one_key_post_boss_retry = 0
        self.lbl_boss_fatigue.setText(f"疲劳: {int(current)}/{int(maximum)}")
        low_trigger, high_trigger = self._fatigue_trigger_bounds()
        if current >= high_trigger:
            self._one_key_set_state(
                f"疲劳 {current}/{maximum} ≥ +{high_trigger:.0f}，返回打怪点。",
                "warn",
            )
            self._return_to_farm_from_boss()
        else:
            self._one_key_set_state(
                f"疲劳 {current}/{maximum} 仍低，继续打BOSS。",
                "warn",
            )
            if self._one_key_stable:
                self._begin_stable_boss_sequence()
            else:
                self._start_boss_fight_phase()

    def _on_one_key_macro_done(self, stage: str) -> None:
        player = self.sender()
        if player is self._one_key_player:
            self._one_key_player = None
            if isinstance(player, MacroPlayer):
                player.deleteLater()
        if not self._one_key_running or stage != self._one_key_stage:
            return
        if stage == "return_farm":
            self._one_key_state = "to_boss"
            self._one_key_stage = "to_boss"
            self._one_key_set_state("前往BOSS点…", "warn")
            self._play_one_key_macro("to_boss")
        elif stage == "to_boss":
            self._start_boss_fight_phase()
        elif stage == "boss_fight":
            self._stop_one_key_potion()
            # 出了 BOSS 副本先回归 BOSS 点，再决定继续打还是回打怪点
            self._play_one_key_return("boss")
        elif stage == "return_boss":
            self._start_one_key_post_boss_check()
        elif stage == "to_farm":
            self._one_key_state = "farming"
            self._one_key_stage = ""
            self._restore_one_key_focus()
            self._one_key_set_state("已回到打怪点，继续刷怪。", "ok")
            self._start_farming_phase()

    def _on_one_key_macro_error(self, stage: str, message: str) -> None:
        player = self.sender()
        if player is self._one_key_player:
            self._one_key_player = None
            if isinstance(player, MacroPlayer):
                player.deleteLater()
        if self._one_key_running:
            title = ONE_KEY_STAGE_TITLES.get(
                stage, MACRO_TITLES.get(stage, stage)
            )
            self._stop_one_key(f"宏回放失败（{title}）：{message}")

    def _start_one_key_potion(self) -> None:
        self._stop_one_key_potion()
        if not self._one_key_running:
            return
        worker = KeySender(
            self.target_hwnd,
            interval_ms=1000,
            use_skills=False,
            use_potion=True,
            potion_interval_ms=self.spin_potion.value() * 1000,
            attack_enabled=False,
            potion_immediate=True,
            parent=self,
        )
        worker.failed.connect(self._on_one_key_potion_failed)
        self._one_key_potion_worker = worker
        worker.start()

    def _stop_one_key_potion(self) -> None:
        worker = self._one_key_potion_worker
        self._one_key_potion_worker = None
        if worker is not None:
            worker.stop()
            if not worker.wait(2500):
                worker.terminate()
                worker.wait(500)
            worker.deleteLater()

    def _on_one_key_potion_failed(self, message: str) -> None:
        if self._one_key_running:
            self._stop_one_key(f"BOSS喝药线程停止：{message}")

    def _restore_one_key_focus(self) -> None:
        prev_fg = self._one_key_prev_fg
        prev_cursor = self._one_key_prev_cursor
        self._one_key_prev_fg = 0
        self._one_key_prev_cursor = None
        self._one_key_farm_base = 0
        self._one_key_total_farm_rounds = 0
        if prev_fg:
            _restore_foreground_window(prev_fg, self.target_hwnd or 0)
        if prev_cursor is not None:
            try:
                time.sleep(0.03)
                win32api.SetCursorPos(prev_cursor)
            except Exception:
                pass

    def _stop_one_key(self, error: str = "") -> None:
        was_running = self._one_key_running
        self._one_key_running = False
        self._one_key_state = "idle"
        self._one_key_stage = ""

        self._stop_farming_phase()
        self._stop_one_key_potion()
        self._stop_one_key_post_boss_monitor()
        self._stop_stable_runner()
        self._one_key_post_boss_retry = 0
        self._one_key_stable = False

        initial = self._one_key_initial_monitor
        self._one_key_initial_monitor = None
        if initial is not None:
            initial.stop()
            if not initial.wait(2500):
                initial.terminate()
                initial.wait(500)
            initial.deleteLater()

        player = self._one_key_player
        self._one_key_player = None
        if player is not None:
            player.stop()
            if not player.wait(2500):
                player.terminate()
                player.wait(500)
            player.deleteLater()

        self._restore_one_key_focus()
        self._set_one_key_ui_running(False)
        self.lbl_boss_fatigue.setText("疲劳: --/--")
        if hasattr(self, "lbl_stable_state"):
            self.lbl_stable_state.setText("已停止")
        if error:
            self.lbl_one_key_state.setText("已停止（异常）")
            self._set_status(error, "error")
            try:
                self._popup.show_message("一键打BOSS已停止", error, seconds=15)
            except Exception:
                pass
        elif was_running:
            self.lbl_one_key_state.setText("已停止")
            self._set_status("一键打BOSS已停止。", "normal")

    def _set_running_controls(self, running: bool) -> None:
        self.combo_windows.setEnabled(not running)
        self.btn_refresh.setEnabled(not running)
        self._update_bind_button_state()
        self._update_toggle_action()

    def _update_bind_button_state(self) -> None:
        running = self._worker is not None or self._macro_busy()
        has_selection = self.combo_windows.currentData() is not None
        self.btn_bind.setEnabled((not running) and has_selection)

    def _update_toggle_action(self) -> None:
        if self._act_toggle is None:
            return

        if self._stopping:
            self._act_toggle.setText("正在停止…")
            self._act_toggle.setEnabled(False)
        elif self._one_key_running:
            self._act_toggle.setText("停止一键BOSS")
            self._act_toggle.setEnabled(True)
        elif self._single_player is not None:
            self._act_toggle.setText("停止回放")
            self._act_toggle.setEnabled(True)
        elif self._recording_step is not None:
            self._act_toggle.setText("停止录制")
            self._act_toggle.setEnabled(True)
        elif self._stable_runner is not None:
            self._act_toggle.setText("停止稳定测试")
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

        self._one_key_running = False
        self._one_key_post_boss_retry = 0
        self._recording_step = None
        self._recording_backup = None
        for timer_attr in (
            "_rec_start_timer",
            "_rec_update_timer",
            "_point_timeout_timer",
        ):
            timer = getattr(self, timer_attr, None)
            if timer is not None:
                try:
                    timer.stop()
                except Exception:
                    pass
                setattr(self, timer_attr, None)

        recorder = getattr(self, "_recorder", None)
        self._recorder = None
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:
                pass

        capture = getattr(self, "_point_capture", None)
        self._point_capture = None
        if capture is not None:
            try:
                capture.cancel()
            except Exception:
                pass
        self._point_kind = None

        try:
            self._stop_stable_runner()
        except Exception:
            pass
        self._stable_test_running = False
        self._one_key_stable = False

        route_editor = self._route_editor
        self._route_editor = None
        if route_editor is not None:
            try:
                route_editor.close()
            except Exception:
                pass

        for player_attr in (
            "_single_player",
            "_record_return_player",
            "_one_key_player",
        ):
            player = getattr(self, player_attr, None)
            setattr(self, player_attr, None)
            if player is not None:
                player.stop()
                if not player.wait(2500):
                    player.terminate()
                    player.wait(500)

        for worker_attr in ("_one_key_worker", "_one_key_potion_worker"):
            worker = getattr(self, worker_attr, None)
            setattr(self, worker_attr, None)
            if worker is not None:
                worker.stop()
                if not worker.wait(2500):
                    worker.terminate()
                    worker.wait(500)

        for monitor_attr in (
            "_one_key_initial_monitor",
            "_one_key_post_boss_monitor",
            "_one_key_fatigue",
        ):
            monitor = getattr(self, monitor_attr, None)
            setattr(self, monitor_attr, None)
            if monitor is not None:
                monitor.stop()
                if not monitor.wait(2500):
                    monitor.terminate()
                    monitor.wait(500)

        try:
            self._restore_one_key_focus()
        except Exception:
            pass

        for monitor_attr in ("_fatigue_monitor", "_test_monitor"):
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
        result = "mouse_ready={}{}current={}{}maximum={}{}message={}{}".format(
            mouse is not None,
            nl,
            current,
            nl,
            maximum,
            nl,
            msg,
            nl,
        )
    except Exception as exc:
        result = "mouse_ready={}{}ERROR {}: {}{}".format(
            mouse is not None, nl, type(exc).__name__, exc, nl
        )
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(result)
    except Exception:
        pass
    return 0


def main() -> int:
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
