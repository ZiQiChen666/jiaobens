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
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
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

from multi_open import SlotRunner

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
RECORDS_PATH = os.path.join(
    os.path.dirname(FATIGUE_CONFIG_PATH), "records.json"
)
COMBOS_PATH = os.path.join(
    os.path.dirname(FATIGUE_CONFIG_PATH), "combos.json"
)
REPLAY_LOG_PATH = os.path.join(
    os.path.dirname(FATIGUE_CONFIG_PATH), "macro_replay.log"
)


def default_macro_config() -> dict:
    return {
        "map_key": "m",
        "close_map_key": "esc",
        "mount_key": "f",
    }


def load_macro_config() -> dict:
    """只保留地图寻路/路径执行需要的按键配置，兼容旧版 macros.json。"""
    cfg = default_macro_config()
    try:
        with open(MACRO_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return cfg
        for key in ("map_key", "close_map_key", "mount_key"):
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
    """原子写入，避免程序中途退出时把已有的配置文件截断。"""
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


def load_records() -> list[dict]:
    """读取整段副本录制列表。"""
    try:
        with open(RECORDS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        records = data.get("records", [])
        if not isinstance(records, list):
            return []
        result = []
        for item in records:
            if not isinstance(item, dict):
                continue
            events = item.get("events", [])
            if not isinstance(events, list):
                continue
            record = dict(item)
            record["name"] = str(record.get("name", "未命名"))
            record["events"] = [e for e in events if isinstance(e, dict)]
            result.append(record)
        return result
    except FileNotFoundError:
        return []
    except Exception:
        return []


def save_records(records: list[dict]) -> bool:
    """原子写入整段副本录制。"""
    tmp_path = RECORDS_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                {"records": records},
                f,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, RECORDS_PATH)
        return True
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def load_combos() -> list[dict]:
    """读取“路径 + 副本录制”的组合列表。"""
    try:
        with open(COMBOS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        combos = data.get("combos", [])
        if not isinstance(combos, list):
            return []
        result = []
        for item in combos:
            if not isinstance(item, dict):
                continue
            combo = dict(item)
            combo["name"] = str(combo.get("name", "未命名"))
            combo["path"] = str(combo.get("path", ""))
            combo["record"] = str(combo.get("record", ""))
            try:
                combo["repeat"] = max(0, int(combo.get("repeat", 1)))
            except (TypeError, ValueError):
                combo["repeat"] = 1
            try:
                combo["interval"] = max(0.0, float(combo.get("interval", 3)))
            except (TypeError, ValueError):
                combo["interval"] = 3.0
            try:
                combo["speed"] = max(0.5, min(2.0, float(combo.get("speed", 1.0))))
            except (TypeError, ValueError):
                combo["speed"] = 1.0
            result.append(combo)
        return result
    except FileNotFoundError:
        return []
    except Exception:
        return []


def save_combos(combos: list[dict]) -> bool:
    """原子写入组合列表。"""
    tmp_path = COMBOS_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                {"combos": combos},
                f,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, COMBOS_PATH)
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


def _get_ocr_engine():
    global _OCR_ENGINE
    with _OCR_LOCK:
        if _OCR_ENGINE is None:
            from rapidocr_onnxruntime import RapidOCR

            _OCR_ENGINE = RapidOCR()
        return _OCR_ENGINE


def ocr_screen_items(data: bytes, w: int, h: int) -> list[dict]:
    """OCR 整个游戏客户区，返回文字和归一化坐标。"""
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:
        raise RuntimeError(f"缺少 OCR 依赖：{type(exc).__name__}: {exc}")
    arr = np.frombuffer(data, dtype=np.uint8).reshape((h, w, 4))
    rgb = arr[:, :, [2, 1, 0]].copy()
    image = Image.fromarray(rgb, "RGB")
    engine = _get_ocr_engine()
    with _OCR_LOCK:
        result, _elapse = engine(np.array(image)[:, :, ::-1])
    items: list[dict] = []
    for item in (result or []):
        if len(item) < 3:
            continue
        box, text, score = item[0], item[1], item[2]
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        items.append(
            {
                "text": str(text).strip(),
                "score": float(score),
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "nx": (x0 + x1) / 2.0 / max(1, w),
                "ny": (y0 + y1) / 2.0 / max(1, h),
            }
        )
    return items


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

/* ---- 弹窗（地图标注 / 路径编辑 / 框选区域）使用浅色主题 ---- */
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
QDialog QDoubleSpinBox,
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
QDialog QDoubleSpinBox:focus,
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
QDialog QListWidget,
QDialog QTableWidget,
QDialog QPlainTextEdit {
    background: #ffffff;
    color: #1b1c1f;
    border: 1px solid #c3c8cf;
    selection-background-color: #bcd2f2;
    selection-color: #101318;
}
QDialog QListWidget::item:selected,
QDialog QTableWidget::item:selected {
    background: #bcd2f2;
    color: #101318;
}
QDialog QHeaderView::section {
    background: #e9ebef;
    color: #1b1c1f;
    border: 1px solid #cfd4db;
    padding: 4px;
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

                # 动作：A 按下 50ms -> 松开
                # -> 可选空格拾取
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
        if win32gui.IsIconic(hwnd):
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.2)
            except Exception:
                pass
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
# 键鼠输入辅助（前台发送 / 前台恢复）
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


_IGNORED_KEY_PARTS = (
    "browser",
    "www",
    "search",
    "mail",
    "calculator",
    "media",
    "volume",
    "play/pause",
    "next track",
    "previous track",
    "launch",
    "windows",
    "apps",
    "sleep",
    "power",
)


def _is_ignored_key_name(name: str) -> bool:
    """过滤浏览器/多媒体键，避免回放时把浏览器打开。"""
    low = str(name or "").lower()
    return any(part in low for part in _IGNORED_KEY_PARTS)


_BROWSER_CLASS_PARTS = (
    "chrome_widgetwin",
    "mozilla",
    "firefox",
    "cef",
    "webview",
    "edge",
    "iexplore",
    "steamwebhelper",
    "gameoverlayui",
)
_BROWSER_TITLE_PARTS = (
    "google chrome",
    "microsoft edge",
    "firefox",
    "browser",
    "浏览器",
)


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
        # 鼠标按键用高频轮询兜底：mouse 钩子偶尔会漏掉快速点击/double 事件。
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_mouse_buttons, daemon=True
        )
        self._poll_thread.start()

    def stop(self) -> list[dict]:
        self._active = False
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
            now = time.time()
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
                now = float(event.time)
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
            if not name or _is_ignored_key_name(name):
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
        speed: float = 1.0,
        lock_user_input: bool = True,
        background: bool = False,
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
        try:
            self.speed = max(0.2, min(3.0, float(speed)))
        except (TypeError, ValueError):
            self.speed = 1.0
        self.lock_user_input = bool(lock_user_input)
        self.background = bool(background)
        self._bg_last_pos: Optional[tuple[int, int]] = None
        self._input_lock = None
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

    def _log(self, text: str) -> None:
        line = (
            f"[{datetime.datetime.now().strftime('%H:%M:%S')}] "
            f"[{self.stage}] {text}"
        )
        try:
            with open(REPLAY_LOG_PATH, "a", encoding="utf-8") as f:
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

    def _bg_prime(self) -> None:
        try:
            fg = int(win32gui.GetForegroundWindow() or 0)
            if fg != self.game_hwnd:
                win32api.PostMessage(self.game_hwnd, WM_ACTIVATEAPP, 1, 0)
                win32api.PostMessage(self.game_hwnd, WM_NCACTIVATE, 1, 0)
                win32api.PostMessage(self.game_hwnd, WM_ACTIVATE, WA_ACTIVE, fg)
                win32api.PostMessage(self.game_hwnd, WM_SETFOCUS, fg, 0)
        except Exception:
            pass

    def _bg_key(self, name: str, down: bool) -> bool:
        vk = _vk_from_key_name(name)
        if vk is None:
            return False
        try:
            scan = int(win32api.MapVirtualKey(vk, 0)) & 0xFF
            lparam = 1 | (scan << 16)
            if not down:
                lparam |= 0xC0000000
            win32api.PostMessage(
                self.game_hwnd,
                win32con.WM_KEYDOWN if down else win32con.WM_KEYUP,
                vk,
                lparam,
            )
            return True
        except Exception:
            return False

    def _bg_client_point(self, event: dict, map_event):
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
                win32api.PostMessage(
                    self.game_hwnd, win32con.WM_MOUSEMOVE, 0, lparam
                )
            elif typ in ("down", "up"):
                button = str(event.get("button", "left")).lower()
                win32api.PostMessage(
                    self.game_hwnd, win32con.WM_MOUSEMOVE, 0, lparam
                )
                if button == "right":
                    msg = (
                        win32con.WM_RBUTTONDOWN
                        if typ == "down"
                        else win32con.WM_RBUTTONUP
                    )
                    wp = win32con.MK_RBUTTON if typ == "down" else 0
                elif button == "middle":
                    msg = (
                        win32con.WM_MBUTTONDOWN
                        if typ == "down"
                        else win32con.WM_MBUTTONUP
                    )
                    wp = win32con.MK_MBUTTON if typ == "down" else 0
                else:
                    msg = (
                        win32con.WM_LBUTTONDOWN
                        if typ == "down"
                        else win32con.WM_LBUTTONUP
                    )
                    wp = win32con.MK_LBUTTON if typ == "down" else 0
                win32api.PostMessage(self.game_hwnd, msg, wp, lparam)
            elif typ == "wheel":
                delta = int(event.get("delta", 0))
                sx, sy = win32gui.ClientToScreen(
                    self.game_hwnd, (x, y)
                )
                wp = (delta & 0xFFFF) << 16
                lp = (int(sx) & 0xFFFF) | ((int(sy) & 0xFFFF) << 16)
                win32api.PostMessage(
                    self.game_hwnd, win32con.WM_MOUSEWHEEL, wp, lp
                )
        except Exception:
            pass

    def _bg_release_buttons(self, buttons: set[str]) -> None:
        if not buttons or self._bg_last_pos is None:
            return
        x, y = self._bg_last_pos
        lparam = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
        for button in list(buttons):
            if button == "right":
                msg, wp = win32con.WM_RBUTTONUP, 0
            elif button == "middle":
                msg, wp = win32con.WM_MBUTTONUP, 0
            else:
                msg, wp = win32con.WM_LBUTTONUP, 0
            try:
                win32api.PostMessage(self.game_hwnd, msg, wp, lparam)
            except Exception:
                pass

    def _point_belongs_to_game(self, x: int, y: int) -> bool:
        """鼠标目标点最上层是不是游戏窗口（防止点到浏览器/桌面）。"""
        try:
            under = int(win32gui.WindowFromPoint((int(x), int(y))) or 0)
            if not under:
                return False
            root = int(win32gui.GetAncestor(under, win32con.GA_ROOT))
            return root == int(self.game_hwnd)
        except Exception:
            return False

    def run(self) -> None:
        error_msg = ""
        prev_cursor = None
        prev_fg = 0
        pressed_keys: set[str] = set()
        pressed_buttons: set[str] = set()
        try:
            if not self.events:
                raise RuntimeError("没有录制内容。")
            if not win32gui.IsWindow(self.game_hwnd):
                raise RuntimeError("游戏窗口已关闭，回放已取消。")
            if mouse is None:
                raise RuntimeError(
                    f"缺少 mouse 库：{MOUSE_IMPORT_ERROR or '未安装'}"
                )
            try:
                prev_cursor = win32api.GetCursorPos()
                prev_fg = int(win32gui.GetForegroundWindow() or 0)
            except Exception:
                pass

            if self.background:
                self._bg_prime()
                self._log(
                    "后台回放模式：PostMessage，不抢真实鼠标键盘"
                )
            else:
                if self.lock_user_input:
                    try:
                        from input_lock import InputLock

                        self._input_lock = InputLock()
                        if not self._input_lock.start(2.0):
                            self._input_lock = None
                            self._log(
                                "输入锁启动失败，继续回放（可能被用户操作干扰）"
                            )
                        else:
                            self._log("已锁定用户鼠标键盘，Ctrl+Alt+F8 可停止")
                    except Exception as exc:
                        self._input_lock = None
                        self._log(f"输入锁不可用：{exc}")

                _force_foreground_window(self.game_hwnd)
                time.sleep(0.15)
                if not _foreground_is_window(self.game_hwnd):
                    raise RuntimeError("无法把游戏窗口切到前台，回放已取消。")

            map_event = self._make_event_mapper()
            self._log(
                f"开始回放 hwnd=0x{self.game_hwnd:X} events={len(self.events)} "
                f"fg={self._fg_info()}"
            )

            if not self.background:
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
                target = max(0.0, float(event.get("t", 0.0))) / self.speed
                while True:
                    remaining = target - (time.perf_counter() - start)
                    if remaining <= 0:
                        break
                    if self._stop_event.wait(min(max(remaining, 0.0), 0.02)):
                        break
                if self._stop_event.is_set():
                    break

                typ = str(event.get("type", ""))
                if typ in ("down", "up", "key_down", "key_up", "wheel"):
                    coord = ""
                    if typ in ("down", "up", "wheel"):
                        coord = (
                            f" nx={event.get('nx')} ny={event.get('ny')}"
                            f" x={event.get('x')} y={event.get('y')}"
                        )
                    self._log(
                        f"事件 {typ} t={float(event.get('t', 0)):.2f} "
                        f"key={event.get('name', '')} "
                        f"button={event.get('button', '')}{coord} "
                        f"fg={self._fg_info()}"
                    )
                if self.background:
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
                        self._bg_prime()
                        self._bg_mouse(event, map_event)
                        if typ == "down":
                            button = str(
                                event.get("button", "left")
                            ).lower()
                            pressed_buttons.add(button)
                        elif typ == "up":
                            button = str(
                                event.get("button", "left")
                            ).lower()
                            pressed_buttons.discard(button)
                    continue

                # 每个键鼠事件前确认游戏仍在前台，避免按键发到浏览器/其它窗口。
                if typ in ("move", "down", "up", "wheel", "key_down", "key_up"):
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
                        _force_foreground_window(self.game_hwnd)
                        for _ in range(20):
                            if _foreground_is_window(self.game_hwnd):
                                break
                            time.sleep(0.05)
                        if not _foreground_is_window(self.game_hwnd):
                            raise RuntimeError(
                                "游戏窗口不在前台，已停止回放，"
                                "避免按键发到其它窗口。"
                            )
                if typ == "move":
                    px, py = map_event(event)
                    if not self._point_belongs_to_game(px, py):
                        _force_foreground_window(self.game_hwnd)
                        time.sleep(0.1)
                    if not self._point_belongs_to_game(px, py):
                        raise RuntimeError(
                            "鼠标目标点被其它窗口覆盖，已停止回放，"
                            "避免点到浏览器/桌面。"
                        )
                    _send_mouse_move_to(px, py)
                elif typ in ("down", "up"):
                    button = str(event.get("button", "left")).lower()
                    px, py = map_event(event)
                    if not self._point_belongs_to_game(px, py):
                        _force_foreground_window(self.game_hwnd)
                        time.sleep(0.1)
                    if not self._point_belongs_to_game(px, py):
                        raise RuntimeError(
                            "鼠标目标点被其它窗口覆盖，已停止回放，"
                            "避免点到浏览器/桌面。"
                        )
                    _send_mouse_move_to(px, py)
                    _send_mouse_button(button, typ == "down")
                    if typ == "down":
                        pressed_buttons.add(button)
                    else:
                        pressed_buttons.discard(button)
                elif typ == "wheel":
                    px, py = map_event(event)
                    if not self._point_belongs_to_game(px, py):
                        _force_foreground_window(self.game_hwnd)
                        time.sleep(0.1)
                    if not self._point_belongs_to_game(px, py):
                        raise RuntimeError(
                            "鼠标目标点被其它窗口覆盖，已停止回放，"
                            "避免点到浏览器/桌面。"
                        )
                    _send_mouse_move_to(px, py)
                    _send_mouse_wheel(int(event.get("delta", 0)))
                elif typ in ("key_down", "key_up"):
                    name = str(event.get("name", ""))
                    if not name or _is_ignored_key_name(name):
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
            self._log(f"回放失败：{error_msg}")
        finally:
            if self.background:
                for key in list(pressed_keys):
                    try:
                        self._bg_key(key, False)
                    except Exception:
                        pass
                self._bg_release_buttons(pressed_buttons)
            else:
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

                if self._input_lock is not None:
                    try:
                        self._input_lock.stop()
                    except Exception:
                        pass
                    self._input_lock = None

            if error_msg:
                self.error.emit(self.stage, error_msg)
            else:
                self._log("回放完成")
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


class ComboEditDialog(QDialog):
    """新建 / 编辑“路径 + 副本录制”组合。"""

    def __init__(
        self,
        paths: list[dict],
        records: list[dict],
        combo: Optional[dict] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("组合挂机设置")
        self.resize(620, 340)
        self._paths = [p for p in paths if isinstance(p, dict)]
        self._records = [r for r in records if isinstance(r, dict)]
        self._original = dict(combo) if isinstance(combo, dict) else None
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        form = QFormLayout()

        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("例如：打BOSS循环")
        form.addRow("组合名称", self.edit_name)

        self.combo_path = QComboBox()
        self.combo_path.addItem("（不使用路径）", "")
        for path in self._paths:
            self.combo_path.addItem(str(path.get("name", "未命名")), str(path.get("name", "")))
        form.addRow("路径", self.combo_path)

        self.combo_record = QComboBox()
        self.combo_record.addItem("（不使用录制）", "")
        for record in self._records:
            self.combo_record.addItem(
                str(record.get("name", "未命名")), str(record.get("name", ""))
            )
        form.addRow("副本录制", self.combo_record)

        self.spin_repeat = QSpinBox()
        self.spin_repeat.setRange(0, 9999)
        self.spin_repeat.setValue(1)
        self.spin_repeat.setSpecialValueText("无限循环")
        self.spin_repeat.setSuffix(" 次")
        form.addRow("重复次数", self.spin_repeat)

        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(0, 600)
        self.spin_interval.setValue(3)
        self.spin_interval.setSuffix(" 秒")
        form.addRow("每轮间隔", self.spin_interval)

        self.spin_speed = QSpinBox()
        self.spin_speed.setRange(50, 200)
        self.spin_speed.setSingleStep(10)
        self.spin_speed.setValue(100)
        self.spin_speed.setSuffix(" %")
        form.addRow("录制回放速度", self.spin_speed)
        root.addLayout(form)

        hint = QLabel(
            "每轮：先执行选中路径（走到副本入口），再回放副本录制。"
            "路径或录制可以只选一个。重复次数 0 = 无限循环。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        root.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.btn_cancel = QPushButton("取消")
        self.btn_save = QPushButton("保存")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_save.clicked.connect(self._on_save)
        buttons.addWidget(self.btn_cancel)
        buttons.addWidget(self.btn_save)
        root.addLayout(buttons)

        if self._original is not None:
            self.edit_name.setText(str(self._original.get("name", "")))
            self._select_text(self.combo_path, str(self._original.get("path", "")))
            self._select_text(
                self.combo_record, str(self._original.get("record", ""))
            )
            try:
                self.spin_repeat.setValue(int(self._original.get("repeat", 1)))
            except (TypeError, ValueError):
                self.spin_repeat.setValue(1)
            try:
                self.spin_interval.setValue(
                    int(float(self._original.get("interval", 3)))
                )
            except (TypeError, ValueError):
                self.spin_interval.setValue(3)
            try:
                self.spin_speed.setValue(
                    int(round(float(self._original.get("speed", 1.0)) * 100))
                )
            except (TypeError, ValueError):
                self.spin_speed.setValue(100)

    @staticmethod
    def _select_text(combo: QComboBox, text: str) -> None:
        idx = combo.findData(text)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _on_save(self) -> None:
        name = self.edit_name.text().strip()
        if not name:
            QMessageBox.warning(self, "缺少名称", "请填写组合名称。")
            return
        if not self.combo_path.currentData() and not self.combo_record.currentData():
            QMessageBox.warning(
                self, "没有动作", "路径和副本录制至少要选一个。"
            )
            return
        self.accept()

    def combo_data(self) -> dict:
        return {
            "name": self.edit_name.text().strip(),
            "path": str(self.combo_path.currentData() or ""),
            "record": str(self.combo_record.currentData() or ""),
            "repeat": int(self.spin_repeat.value()),
            "interval": float(self.spin_interval.value()),
            "speed": max(
                0.5, min(2.0, float(self.spin_speed.value()) / 100.0)
            ),
        }


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

        # BOOS 挂机：地图按键 / 路线编辑 / 地图寻路 / 路径执行 / 副本整段录制
        self._macro_cfg = load_macro_config()
        self._route_editor = None
        self._map_runner = None
        self._path_runner = None
        self._dungeon_records = load_records()
        self._dungeon_recorder: Optional[MacroRecorder] = None
        self._dungeon_player: Optional[MacroPlayer] = None
        self._dungeon_recording_pending = False
        self._dungeon_running = False
        self._dungeon_done_count = 0
        self._dungeon_total = 1
        self._dungeon_speed = 1.0
        self._dungeon_interval = 0.0
        self._dungeon_pending_timer: Optional[QTimer] = None
        self._combos = load_combos()
        self._combo_running = False
        self._combo_total = 1
        self._combo_done_count = 0
        self._combo_name = ""
        self._combo_cfg: dict = {}
        self._combo_path_runner = None
        self._combo_player: Optional[MacroPlayer] = None
        self._combo_pending_timer: Optional[QTimer] = None
        self._fatigue_loop_running = False
        self._fatigue_loop_state = "idle"
        self._fatigue_loop_cfg: dict = {}
        self._fatigue_loop_worker: Optional[KeySender] = None
        self._fatigue_loop_monitor: Optional[FatigueMonitor] = None
        self._fatigue_loop_path_runner = None
        self._fatigue_loop_path_kind = ""
        self._fatigue_loop_player: Optional[MacroPlayer] = None
        self._fatigue_loop_timer: Optional[QTimer] = None
        self._fatigue_loop_rounds = 0
        self._fl_map_open = False
        self._multi_runners: list[Optional[SlotRunner]] = [None, None, None, None]
        self._multi_state_text = "未运行"

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(self._make_icon())
        self.setMinimumSize(860, 750)
        self.resize(920, 800)

        self._build_ui()
        self._refresh_fatigue_roi_label()
        self._refresh_threshold_labels()
        self._refresh_map_profiles()
        self._refresh_paths()
        self._refresh_dungeon_records()
        self._refresh_combos()
        self._refresh_fatigue_loop_options()
        self._refresh_multi_records()
        self._refresh_multi_windows()
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

        self.tab_multi = self._build_multi_tab()
        self.tabs.addTab(self.tab_multi, "多开挂机")
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

        # ---- 地图按键 / 路线编辑 ----
        box_setup = QGroupBox("1 · 地图按键 / 路线")
        sg = QGridLayout(box_setup)
        sg.setHorizontalSpacing(10)
        sg.setVerticalSpacing(8)
        sg.setColumnStretch(3, 1)

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

        self.edit_mount_key = QLineEdit(str(self._macro_cfg.get("mount_key", "f")))
        self.edit_mount_key.setMaxLength(20)
        self.edit_mount_key.setFixedWidth(80)
        self.edit_mount_key.setToolTip(
            "执行路径前按这个键骑乘；留空表示不按。默认 f"
        )
        self.edit_mount_key.editingFinished.connect(self._save_macro_settings)

        self.btn_route_editor = QPushButton("录制 / 管理打怪路线（窗口相对坐标）")
        self.btn_route_editor.setMinimumHeight(32)
        self.btn_route_editor.setToolTip(
            "在同一个 exe 里打开路线编辑器：记录地图入口、路径点、等待和说明。"
            "所有坐标以游戏窗口客户区左上角为基准。"
        )
        self.btn_route_editor.clicked.connect(self._open_route_editor)

        sg.addWidget(QLabel("地图键"), 0, 0)
        sg.addWidget(self.edit_map_key, 0, 1)
        sg.addWidget(QLabel("关闭键"), 0, 2)
        sg.addWidget(self.edit_close_key, 0, 3)
        sg.addWidget(QLabel("骑乘键"), 1, 0)
        sg.addWidget(self.edit_mount_key, 1, 1)
        sg.addWidget(self.btn_route_editor, 2, 0, 1, 4)
        setup_hint = QLabel(
            "地图键 / 关闭键用于下面的地图寻路与路径执行；骑乘键在每次执行路径前按一次；"
            "路线编辑器只记录窗口相对坐标，不发送任何输入。"
        )
        setup_hint.setObjectName("muted")
        setup_hint.setWordWrap(True)
        sg.addWidget(setup_hint, 3, 0, 1, 4)
        root.addWidget(box_setup)

        # ---- 地图标注寻路（人工坐标） ----
        box_map = QGroupBox("2 · 地图标注寻路（人工坐标）")
        mg = QGridLayout(box_map)
        mg.setHorizontalSpacing(8)
        mg.setVerticalSpacing(8)
        mg.setColumnStretch(1, 1)
        mg.addWidget(QLabel("地图"), 0, 0)
        self.combo_map_profile = QComboBox()
        self.combo_map_profile.setMinimumWidth(260)
        mg.addWidget(self.combo_map_profile, 0, 1, 1, 2)
        mg.addWidget(QLabel("目标"), 0, 3)
        self.combo_map_target = QComboBox()
        self.combo_map_target.setMinimumWidth(220)
        mg.addWidget(self.combo_map_target, 0, 4, 1, 2)
        mg.addWidget(QLabel("寻路等待"), 1, 0)
        self.spin_map_travel_wait = QSpinBox()
        self.spin_map_travel_wait.setRange(3, 180)
        self.spin_map_travel_wait.setValue(20)
        self.spin_map_travel_wait.setSuffix(" 秒")
        self.spin_map_travel_wait.setFixedWidth(100)
        self.spin_map_travel_wait.setToolTip("点击地图标注并关闭地图后，等待自动寻路的时间")
        mg.addWidget(self.spin_map_travel_wait, 1, 1)
        self.btn_map_learn = QPushButton("标注 / 编辑地图（人工）")
        self.btn_map_learn.setToolTip(
            "先打开游戏地图，再点这里截图；人工标定地图范围并点选打怪/BOSS坐标，可载入旧地图修改"
        )
        self.btn_map_learn.clicked.connect(self._open_map_learner)
        mg.addWidget(self.btn_map_learn, 1, 2)
        self.btn_map_test = QPushButton("测试前往目标")
        self.btn_map_test.setToolTip("按 M 打开地图 → OCR 找目标文字 → 点击 → Esc → 等待自动寻路")
        self.btn_map_test.clicked.connect(self._toggle_map_test)
        mg.addWidget(self.btn_map_test, 1, 3)
        self.lbl_map_state = QLabel("未运行")
        self.lbl_map_state.setObjectName("muted")
        mg.addWidget(self.lbl_map_state, 1, 4, 1, 2)
        map_note = QLabel(
            "流程：打开地图 → 点“标注/编辑地图（人工）” → 标定地图零点/范围 → "
            "手动点选打怪点/BOSS点 → 保存。可载入已保存地图修改后重新保存。"
            "运行时不依赖 OCR，直接用窗口坐标 + 地图内坐标点击。"
        )
        map_note.setObjectName("muted")
        map_note.setWordWrap(True)
        mg.addWidget(map_note, 2, 0, 1, 6)
        self.combo_map_profile.currentIndexChanged.connect(self._refresh_map_targets)
        root.addWidget(box_map)

        # ---- 路径执行 ----
        box_path = QGroupBox("3 · 路径执行（地图标注）")
        pg2 = QGridLayout(box_path)
        pg2.setHorizontalSpacing(8)
        pg2.setVerticalSpacing(8)
        pg2.setColumnStretch(1, 1)
        pg2.addWidget(QLabel("路径"), 0, 0)
        self.combo_path = QComboBox()
        self.combo_path.setMinimumWidth(300)
        pg2.addWidget(self.combo_path, 0, 1, 1, 2)
        self.btn_path_edit = QPushButton("编辑路径")
        self.btn_path_edit.clicked.connect(self._open_path_editor)
        pg2.addWidget(self.btn_path_edit, 0, 3)
        self.btn_path_run = QPushButton("执行路径")
        self.btn_path_run.clicked.connect(self._toggle_path_run)
        pg2.addWidget(self.btn_path_run, 1, 0, 1, 2)
        self.lbl_path_state = QLabel("未运行")
        self.lbl_path_state.setObjectName("muted")
        self.lbl_path_state.setWordWrap(True)
        pg2.addWidget(self.lbl_path_state, 1, 2, 1, 2)
        path_note = QLabel(
            "路径 = 地图 + 已标注入口 + 等待。运行时不 OCR 识别地图名，"
            "靠场景切换动画判断是否进入下一张地图；点到物体/没动会自动再点一次。"
        )
        path_note.setObjectName("muted")
        path_note.setWordWrap(True)
        pg2.addWidget(path_note, 2, 0, 1, 4)
        root.addWidget(box_path)

        # ---- 副本整段录制 / 回放 ----
        box_dungeon = QGroupBox("4 · 副本整段录制 / 回放")
        dg = QGridLayout(box_dungeon)
        dg.setHorizontalSpacing(8)
        dg.setVerticalSpacing(8)
        dg.setColumnStretch(1, 1)

        dg.addWidget(QLabel("录制"), 0, 0)
        self.combo_dungeon_record = QComboBox()
        self.combo_dungeon_record.setMinimumWidth(260)
        dg.addWidget(self.combo_dungeon_record, 0, 1, 1, 2)
        self.btn_dungeon_record = QPushButton("开始录制")
        self.btn_dungeon_record.clicked.connect(self._toggle_dungeon_record)
        dg.addWidget(self.btn_dungeon_record, 0, 3)
        self.btn_dungeon_rename = QPushButton("重命名")
        self.btn_dungeon_rename.clicked.connect(self._rename_dungeon_record)
        dg.addWidget(self.btn_dungeon_rename, 0, 4)
        self.btn_dungeon_delete = QPushButton("删除")
        self.btn_dungeon_delete.clicked.connect(self._delete_dungeon_record)
        dg.addWidget(self.btn_dungeon_delete, 0, 5)

        dg.addWidget(QLabel("重复"), 1, 0)
        self.spin_dungeon_repeat = QSpinBox()
        self.spin_dungeon_repeat.setRange(0, 9999)
        self.spin_dungeon_repeat.setValue(1)
        self.spin_dungeon_repeat.setSpecialValueText("无限循环")
        self.spin_dungeon_repeat.setSuffix(" 次")
        self.spin_dungeon_repeat.setFixedWidth(110)
        self.spin_dungeon_repeat.setToolTip("0 = 无限循环，直到手动停止")
        dg.addWidget(self.spin_dungeon_repeat, 1, 1)
        dg.addWidget(QLabel("间隔"), 1, 2)
        self.spin_dungeon_interval = QSpinBox()
        self.spin_dungeon_interval.setRange(0, 600)
        self.spin_dungeon_interval.setValue(3)
        self.spin_dungeon_interval.setSuffix(" 秒")
        self.spin_dungeon_interval.setFixedWidth(100)
        self.spin_dungeon_interval.setToolTip("两次回放之间的等待时间")
        dg.addWidget(self.spin_dungeon_interval, 1, 3)
        dg.addWidget(QLabel("速度"), 1, 4)
        self.spin_dungeon_speed = QSpinBox()
        self.spin_dungeon_speed.setRange(50, 200)
        self.spin_dungeon_speed.setSingleStep(10)
        self.spin_dungeon_speed.setValue(100)
        self.spin_dungeon_speed.setSuffix(" %")
        self.spin_dungeon_speed.setFixedWidth(90)
        dg.addWidget(self.spin_dungeon_speed, 1, 5)

        self.btn_dungeon_test = QPushButton("试放一次")
        self.btn_dungeon_test.clicked.connect(self._play_dungeon_once_test)
        dg.addWidget(self.btn_dungeon_test, 2, 0, 1, 2)
        self.btn_dungeon_run = QPushButton("开始副本")
        self.btn_dungeon_run.clicked.connect(self._toggle_dungeon_run)
        dg.addWidget(self.btn_dungeon_run, 2, 2, 1, 2)
        self.lbl_dungeon_state = QLabel("未运行")
        self.lbl_dungeon_state.setObjectName("muted")
        self.lbl_dungeon_state.setWordWrap(True)
        dg.addWidget(self.lbl_dungeon_state, 2, 4, 1, 2)

        self.chk_lock_input = QCheckBox(
            "回放时锁定鼠标键盘（Ctrl+Alt+F8 停止）"
        )
        self.chk_lock_input.setChecked(True)
        self.chk_lock_input.setToolTip(
            "回放期间拦截你的物理鼠标/键盘，避免点开浏览器/计算器；"
            "脚本自己发出的键鼠不受影响。停止请按 Ctrl+Alt+F8。"
        )
        dg.addWidget(self.chk_lock_input, 3, 0, 1, 6)

        self.chk_background_replay = QCheckBox(
            "后台回放（PostMessage，不抢鼠标键盘，部分游戏可能无效）"
        )
        self.chk_background_replay.setChecked(False)
        self.chk_background_replay.setToolTip(
            "用后台消息回放录制，不移动真实鼠标、不抢前台；"
            "这样可以边办公边挂机。部分游戏不处理后台鼠标消息，"
            "建议先“试放一次”验证。"
        )
        dg.addWidget(self.chk_background_replay, 4, 0, 1, 6)

        dungeon_note = QLabel(
            "把“进副本 → 打BOSS → 拾取掉落 → 退出副本”完整录成一段，回放时按时间线原样执行。"
            "录制开始前先站到固定起点；点“开始录制”后 3 秒内切到游戏操作，"
            "完成后切回本窗口点“停止录制”。重复次数 0 = 无限循环。"
        )
        dungeon_note.setObjectName("muted")
        dungeon_note.setWordWrap(True)
        dg.addWidget(dungeon_note, 5, 0, 1, 6)
        root.addWidget(box_dungeon)

        # ---- 组合挂机：路径 + 副本录制 ----
        box_combo = QGroupBox("5 · 组合挂机（路径 + 副本录制）")
        cg = QGridLayout(box_combo)
        cg.setHorizontalSpacing(8)
        cg.setVerticalSpacing(8)
        cg.setColumnStretch(1, 1)

        cg.addWidget(QLabel("组合"), 0, 0)
        self.combo_combo = QComboBox()
        self.combo_combo.setMinimumWidth(260)
        cg.addWidget(self.combo_combo, 0, 1, 1, 2)
        self.btn_combo_new = QPushButton("新建组合")
        self.btn_combo_new.clicked.connect(self._new_combo)
        cg.addWidget(self.btn_combo_new, 0, 3)
        self.btn_combo_edit = QPushButton("编辑/重命名")
        self.btn_combo_edit.clicked.connect(self._edit_combo)
        cg.addWidget(self.btn_combo_edit, 0, 4)
        self.btn_combo_delete = QPushButton("删除")
        self.btn_combo_delete.clicked.connect(self._delete_combo)
        cg.addWidget(self.btn_combo_delete, 0, 5)

        self.btn_combo_run = QPushButton("开始组合")
        self.btn_combo_run.clicked.connect(self._toggle_combo)
        cg.addWidget(self.btn_combo_run, 1, 0, 1, 2)
        self.lbl_combo_state = QLabel("未运行")
        self.lbl_combo_state.setObjectName("muted")
        self.lbl_combo_state.setWordWrap(True)
        cg.addWidget(self.lbl_combo_state, 1, 2, 1, 4)

        combo_note = QLabel(
            "组合 = 一条路径 + 一段副本录制。运行时每轮先执行路径，再回放录制；"
            "重复次数 0 = 无限循环。选择组合后点“开始组合”即可挂机。"
        )
        combo_note.setObjectName("muted")
        combo_note.setWordWrap(True)
        cg.addWidget(combo_note, 2, 0, 1, 6)
        root.addWidget(box_combo)

        # ---- 疲劳循环：打怪 ↔ BOSS ----
        box_fatigue_loop = QGroupBox("6 · 疲劳循环（挂机 ↔ 录制）")
        fg = QGridLayout(box_fatigue_loop)
        fg.setHorizontalSpacing(8)
        fg.setVerticalSpacing(8)
        fg.setColumnStretch(1, 1)

        fg.addWidget(QLabel("BOSS 录制"), 0, 0)
        self.combo_fl_record = QComboBox()
        self.combo_fl_record.setMinimumWidth(300)
        fg.addWidget(self.combo_fl_record, 0, 1, 1, 3)
        self.btn_fl_record = QPushButton("录制新脚本")
        self.btn_fl_record.setFixedWidth(100)
        self.btn_fl_record.clicked.connect(self._toggle_dungeon_record)
        fg.addWidget(self.btn_fl_record, 0, 4)
        self.btn_fl_test = QPushButton("试放")
        self.btn_fl_test.setFixedWidth(60)
        self.btn_fl_test.clicked.connect(self._play_fl_record_test)
        fg.addWidget(self.btn_fl_test, 0, 5)
        self.btn_fl_delete_record = QPushButton("删除录制")
        self.btn_fl_delete_record.setFixedWidth(92)
        self.btn_fl_delete_record.clicked.connect(self._delete_fl_record)
        fg.addWidget(self.btn_fl_delete_record, 0, 6)

        fg.addWidget(QLabel("疲劳阈值 ±"), 1, 0)
        self.spin_fl_threshold = QSpinBox()
        self.spin_fl_threshold.setRange(50, 1000)
        self.spin_fl_threshold.setSingleStep(10)
        self.spin_fl_threshold.setValue(int(self._fatigue_threshold))
        self.spin_fl_threshold.setSuffix(" 点")
        self.spin_fl_threshold.setFixedWidth(110)
        self.spin_fl_threshold.valueChanged.connect(self._on_threshold_changed)
        fg.addWidget(self.spin_fl_threshold, 1, 1)
        fg.addWidget(QLabel("检查间隔"), 1, 2)
        self.spin_fl_interval = QSpinBox()
        self.spin_fl_interval.setRange(5, 600)
        self.spin_fl_interval.setValue(30)
        self.spin_fl_interval.setSuffix(" 秒")
        self.spin_fl_interval.setFixedWidth(100)
        fg.addWidget(self.spin_fl_interval, 1, 3)

        self.combo_fl_record.currentIndexChanged.connect(
            lambda _=0: self._update_fatigue_loop_controls()
        )

        self.btn_fl_run = QPushButton("开始疲劳循环")
        self.btn_fl_run.clicked.connect(self._toggle_fatigue_loop)
        fg.addWidget(self.btn_fl_run, 2, 0, 1, 2)
        self.lbl_fl_state = QLabel("未运行")
        self.lbl_fl_state.setObjectName("muted")
        self.lbl_fl_state.setWordWrap(True)
        fg.addWidget(self.lbl_fl_state, 2, 2, 1, 2)

        fl_note = QLabel(
            "挂机打怪 → 每隔 N 秒检测疲劳 → 疲劳 < -阈值 → 回放 BOSS 整段录制 "
            "→ 播放完自动恢复打怪。录制里要包含去BOSS、进副本、打BOSS、拾取、"
            "退出、回打怪点的全部操作。"
        )
        fl_note.setObjectName("muted")
        fl_note.setWordWrap(True)
        fg.addWidget(fl_note, 3, 0, 1, 4)
        root.addWidget(box_fatigue_loop)

        root.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(tab)
        return scroll

    def _build_multi_tab(self) -> QWidget:
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        box_cfg = QGroupBox("多开设置")
        cg = QGridLayout(box_cfg)
        cg.setHorizontalSpacing(8)
        cg.setVerticalSpacing(8)
        cg.setColumnStretch(3, 1)

        cg.addWidget(QLabel("检查间隔"), 0, 0)
        self.spin_multi_interval = QSpinBox()
        self.spin_multi_interval.setRange(5, 600)
        self.spin_multi_interval.setValue(30)
        self.spin_multi_interval.setSuffix(" 秒")
        self.spin_multi_interval.setFixedWidth(100)
        cg.addWidget(self.spin_multi_interval, 0, 1)

        self.btn_multi_refresh = QPushButton("刷新窗口")
        self.btn_multi_refresh.clicked.connect(self._refresh_multi_windows)
        cg.addWidget(self.btn_multi_refresh, 1, 0)
        self.btn_multi_start_all = QPushButton("全部开始")
        self.btn_multi_start_all.clicked.connect(self._start_all_multi)
        cg.addWidget(self.btn_multi_start_all, 1, 1)
        self.btn_multi_stop_all = QPushButton("全部停止")
        self.btn_multi_stop_all.clicked.connect(self._stop_all_multi)
        cg.addWidget(self.btn_multi_stop_all, 1, 2)
        self.lbl_multi_state = QLabel("未运行")
        self.lbl_multi_state.setObjectName("muted")
        self.lbl_multi_state.setWordWrap(True)
        cg.addWidget(self.lbl_multi_state, 1, 3)

        multi_note = QLabel(
            "每个槽位绑定一个游戏窗口，并可以分别选择不同的 BOSS 录制；"
            "勾选“本地挂机”则只后台打怪，不检测疲劳、不播录制。"
            "疲劳阈值、喝药间隔每个窗口独立设置；检查间隔共用；"
            "打怪参数沿用基础挂机页的 A / 空格 / 技能 / 喝药设置。"
        )
        multi_note.setObjectName("muted")
        multi_note.setWordWrap(True)
        cg.addWidget(multi_note, 2, 0, 1, 4)
        root.addWidget(box_cfg)

        self.combo_multi_windows: list[QComboBox] = []
        self.combo_multi_records: list[QComboBox] = []
        self.lbl_multi_slots: list[QLabel] = []
        self.spin_multi_thresholds: list[QSpinBox] = []
        self.spin_multi_potions: list[QSpinBox] = []
        self.spin_multi_priorities: list[QSpinBox] = []
        self.chk_multi_farm_only: list[QCheckBox] = []
        self.btn_multi_tests: list[QPushButton] = []
        self.btn_multi_starts: list[QPushButton] = []
        self.btn_multi_stops: list[QPushButton] = []

        for index in range(4):
            box = QGroupBox(f"窗口 {index + 1}")
            sg = QGridLayout(box)
            sg.setHorizontalSpacing(8)
            sg.setVerticalSpacing(8)
            sg.setColumnStretch(1, 1)
            sg.setColumnStretch(4, 1)
            sg.addWidget(QLabel("游戏窗口"), 0, 0)
            window_combo = QComboBox()
            window_combo.setMinimumWidth(280)
            sg.addWidget(window_combo, 0, 1, 1, 2)
            sg.addWidget(QLabel("BOSS 录制"), 0, 3)
            record_combo = QComboBox()
            record_combo.setMinimumWidth(240)
            record_combo.currentIndexChanged.connect(
                lambda _=0: self._update_multi_controls()
            )
            sg.addWidget(record_combo, 0, 4, 1, 2)
            chk_farm = QCheckBox("本地挂机")
            chk_farm.setToolTip(
                "只在本窗口后台打怪，不检测疲劳、不播放录制；"
                "适合多开纯挂机。"
            )
            chk_farm.toggled.connect(
                lambda _=False: self._update_multi_controls()
            )
            sg.addWidget(chk_farm, 0, 6)
            btn_test = QPushButton("试放")
            btn_test.clicked.connect(
                lambda _=False, i=index: self._test_multi_slot(i)
            )
            btn_start = QPushButton("开始")
            btn_stop = QPushButton("停止")
            btn_stop.setEnabled(False)
            btn_start.clicked.connect(
                lambda _=False, i=index: self._start_multi_slot(i)
            )
            btn_stop.clicked.connect(
                lambda _=False, i=index: self._stop_multi_slot(i)
            )
            sg.addWidget(btn_test, 0, 7)
            sg.addWidget(btn_start, 0, 8)
            sg.addWidget(btn_stop, 0, 9)

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
            spin_potion.setFixedWidth(110)
            spin_potion.setToolTip(
                "这个窗口单独使用的喝药间隔；是否喝药仍由基础页“自动喝药 Q”控制"
            )
            sg.addWidget(spin_potion, 1, 3)
            sg.addWidget(QLabel("优先级"), 1, 4)
            spin_priority = QSpinBox()
            spin_priority.setRange(1, 4)
            spin_priority.setValue(index + 1)
            spin_priority.setFixedWidth(70)
            spin_priority.setToolTip(
                "1 最高。多个窗口同时要打 BOSS 时，按优先级从小到大排队。"
            )
            sg.addWidget(spin_priority, 1, 5)
            status = QLabel("未开始")
            status.setObjectName("muted")
            status.setWordWrap(True)
            sg.addWidget(status, 1, 6, 1, 4)
            root.addWidget(box)
            self.combo_multi_windows.append(window_combo)
            self.combo_multi_records.append(record_combo)
            self.spin_multi_thresholds.append(spin_threshold)
            self.spin_multi_potions.append(spin_potion)
            self.spin_multi_priorities.append(spin_priority)
            self.lbl_multi_slots.append(status)
            self.chk_multi_farm_only.append(chk_farm)
            self.btn_multi_tests.append(btn_test)
            self.btn_multi_starts.append(btn_start)
            self.btn_multi_stops.append(btn_stop)

        root.addStretch(1)
        wholesale = QScrollArea()
        wholesale.setWidgetResizable(True)
        wholesale.setFrameShape(QFrame.Shape.NoFrame)
        wholesale.setWidget(tab)
        return wholesale

    # ------------------------------------------------------------ 多开挂机
    def _multi_record_data(self, index: int) -> Optional[dict]:
        if not (0 <= index < len(self.combo_multi_records)):
            return None
        name = str(self.combo_multi_records[index].currentData() or "").strip()
        if not name:
            return None
        return next(
            (
                record
                for record in self._dungeon_records
                if str(record.get("name", "")) == name
            ),
            None,
        )

    def _refresh_multi_records(self) -> None:
        if not hasattr(self, "combo_multi_records"):
            return
        for combo in self.combo_multi_records:
            current = str(combo.currentData() or "")
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("（请选择 BOSS 录制）", "")
            for record in self._dungeon_records:
                name = str(record.get("name", "")).strip()
                if name:
                    combo.addItem(name, name)
            idx = combo.findData(current) if current else -1
            if idx > 0:
                combo.setCurrentIndex(idx)
            elif combo.count() > 1:
                combo.setCurrentIndex(1)
            combo.blockSignals(False)
        self._update_multi_controls()

    def _refresh_multi_windows(self) -> None:
        if not hasattr(self, "combo_multi_windows"):
            return
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
        unique = {(title, hwnd) for title, hwnd in found}
        found = sorted(unique, key=lambda item: (item[0].lower(), item[1]))
        hwnds = [hwnd for _title, hwnd in found]
        current = [combo.currentData() for combo in self.combo_multi_windows]
        used: set[int] = set()
        for runner in self._multi_runners:
            if runner is not None and getattr(runner, "is_running", False):
                used.add(int(runner.hwnd))

        for index, combo in enumerate(self.combo_multi_windows):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("（未选择）", None)
            for title, hwnd in found:
                combo.addItem(f"{title} [0x{hwnd:X}]", hwnd)
            desired = current[index]
            chosen = None
            if desired in hwnds and desired not in used:
                chosen = desired
            else:
                for hwnd in hwnds:
                    if hwnd not in used:
                        chosen = hwnd
                        break
            if chosen is not None:
                idx = combo.findData(chosen)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                used.add(int(chosen))
            else:
                combo.setCurrentIndex(0)
            combo.blockSignals(False)

        self._multi_state_text = (
            f"找到 {len(found)} 个游戏窗口" if found else "没有找到游戏窗口"
        )
        self._update_multi_controls()

        # 运行中的槽位如果窗口被关闭，自动停止。
        for index, runner in enumerate(self._multi_runners):
            if runner is None or not getattr(runner, "is_running", False):
                continue
            if not self._window_alive(runner.hwnd):
                runner.stop("游戏窗口已关闭", "error")

    def _update_multi_controls(self) -> None:
        if not hasattr(self, "btn_multi_start_all"):
            return
        roi_ok = self._fatigue_roi_norm is not None
        single_busy = self._single_window_busy()
        any_running = self._any_multi_running()
        startable = False
        running_count = 0
        for index in range(4):
            runner = self._multi_runners[index]
            running = runner is not None and getattr(runner, "is_running", False)
            if running:
                running_count += 1
            record = self._multi_record_data(index)
            record_ok = bool(record and record.get("events"))
            window_combo = self.combo_multi_windows[index]
            record_combo = self.combo_multi_records[index]
            farm_only = self.chk_multi_farm_only[index].isChecked()
            hwnd = window_combo.currentData()
            window_combo.setEnabled(not running)
            record_combo.setEnabled(not running and not farm_only)
            self.chk_multi_farm_only[index].setEnabled(not running)
            self.spin_multi_thresholds[index].setEnabled(not running)
            self.spin_multi_potions[index].setEnabled(not running)
            self.spin_multi_priorities[index].setEnabled(not running)
            can_start = (
                (not running)
                and bool(hwnd)
                and not single_busy
                and (farm_only or (record_ok and roi_ok))
            )
            self.btn_multi_starts[index].setEnabled(can_start)
            self.btn_multi_stops[index].setEnabled(running)
            self.btn_multi_tests[index].setEnabled(
                (not running)
                and bool(hwnd)
                and record_ok
                and not single_busy
                and not farm_only
            )
            if can_start:
                startable = True
        self.spin_multi_interval.setEnabled(not any_running)
        self.btn_multi_start_all.setEnabled(startable)
        self.btn_multi_stop_all.setEnabled(any_running)
        self.btn_multi_refresh.setEnabled(True)
        if any_running:
            self.lbl_multi_state.setText(f"多开运行中：{running_count}/4")
        else:
            self.lbl_multi_state.setText(
                getattr(self, "_multi_state_text", "未运行")
            )

    def _start_multi_slot(self, index: int) -> None:
        if index < 0 or index >= 4:
            return
        if self._single_window_busy():
            self._notify(
                "单窗口正在运行",
                "请先停止基础挂机/路径/副本/组合/疲劳循环，再启动多开。",
            )
            return
        runner = self._multi_runners[index]
        if runner is not None and getattr(runner, "is_running", False):
            return
        hwnd = self.combo_multi_windows[index].currentData()
        if hwnd is None:
            self._notify("没有选择窗口", f"请给窗口 {index + 1} 选择一个游戏窗口。")
            return
        if not self._window_alive(int(hwnd)):
            self._notify("窗口已关闭", "请先点“刷新窗口”，重新选择。")
            return
        for other_index, other in enumerate(self._multi_runners):
            if (
                other_index != index
                and other is not None
                and getattr(other, "is_running", False)
                and int(other.hwnd) == int(hwnd)
            ):
                self._notify(
                    "窗口重复",
                    f"窗口 {index + 1} 和窗口 {other_index + 1} 选了同一个游戏窗口。",
                )
                return
        farm_only = self.chk_multi_farm_only[index].isChecked()
        record = self._multi_record_data(index)
        if not farm_only:
            if record is None or not record.get("events"):
                self._notify(
                    "没有录制",
                    f"请先给窗口 {index + 1} 选择一段有效的 BOSS 录制，"
                    "或者勾选“本地挂机”。",
                )
                return
            if self._fatigue_roi_norm is None:
                self._notify(
                    "尚未框选疲劳区域",
                    "请先在基础挂机页框选疲劳提示区域，"
                    "或者勾选“本地挂机”。",
                )
                return
        config = {
            "farm_only": bool(farm_only),
            "events": list(record.get("events", [])) if record else [],
            "meta": record.get("meta") if record else None,
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
            "lock_user_input": bool(self.chk_lock_input.isChecked()),
            "background": bool(self.chk_background_replay.isChecked()),
        }
        slot = SlotRunner(int(hwnd), config, parent=self)
        slot.status.connect(
            lambda text, level, i=index: self._on_multi_slot_status(i, text, level)
        )
        slot.finished.connect(
            lambda i=index: self._on_multi_slot_finished(i)
        )
        self._multi_runners[index] = slot
        self.lbl_multi_slots[index].setText("启动中…")
        slot.start()
        if not getattr(slot, "is_running", False):
            slot.deleteLater()
            self._multi_runners[index] = None
        self._update_multi_controls()

    def _test_multi_slot(self, index: int) -> None:
        if index < 0 or index >= 4:
            return
        if self.chk_multi_farm_only[index].isChecked():
            self._notify(
                "本地挂机模式",
                "当前窗口勾选了“本地挂机”，没有录制可以试放。",
            )
            return
        if self._single_window_busy():
            self._notify(
                "单窗口正在运行",
                "请先停止单窗口自动化，再试放多开录制。",
            )
            return
        runner = self._multi_runners[index]
        if runner is not None and getattr(runner, "is_running", False):
            return
        hwnd = self.combo_multi_windows[index].currentData()
        if hwnd is None:
            self._notify("没有选择窗口", f"请给窗口 {index + 1} 选择一个游戏窗口。")
            return
        if not self._window_alive(int(hwnd)):
            self._notify("窗口已关闭", "请先点“刷新窗口”，重新选择。")
            return
        for other_index, other in enumerate(self._multi_runners):
            if (
                other_index != index
                and other is not None
                and getattr(other, "is_running", False)
                and int(other.hwnd) == int(hwnd)
            ):
                self._notify(
                    "窗口重复",
                    f"窗口 {index + 1} 和窗口 {other_index + 1} 选了同一个游戏窗口。",
                )
                return
        record = self._multi_record_data(index)
        if record is None or not record.get("events"):
            self._notify(
                "没有录制",
                f"请先给窗口 {index + 1} 选择一段有效的 BOSS 录制。",
            )
            return
        config = {
            "events": list(record.get("events", [])),
            "meta": record.get("meta"),
            "speed": 1.0,
            "priority": int(self.spin_multi_priorities[index].value()),
            "lock_user_input": bool(self.chk_lock_input.isChecked()),
            "background": bool(self.chk_background_replay.isChecked()),
        }
        slot = SlotRunner(int(hwnd), config, test_only=True, parent=self)
        slot.status.connect(
            lambda text, level, i=index: self._on_multi_slot_status(i, text, level)
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
        if index < 0 or index >= 4:
            return
        runner = self._multi_runners[index]
        if runner is not None:
            runner.stop("已停止", "warn")
        else:
            self._update_multi_controls()

    def _start_all_multi(self) -> None:
        for index in range(4):
            runner = self._multi_runners[index]
            if runner is not None and getattr(runner, "is_running", False):
                continue
            if self.combo_multi_windows[index].currentData() is not None:
                self._start_multi_slot(index)

    def _stop_all_multi(self) -> None:
        for index in range(4):
            runner = self._multi_runners[index]
            if runner is not None:
                runner.stop("已停止", "warn")
        self._update_multi_controls()

    def _on_multi_slot_status(self, index: int, text: str, level: str) -> None:
        if 0 <= index < len(self.lbl_multi_slots):
            self.lbl_multi_slots[index].setText(text)

    def _on_multi_slot_finished(self, index: int) -> None:
        runner = self._multi_runners[index]
        if runner is not None:
            runner.deleteLater()
            self._multi_runners[index] = None
        # 状态文字由 SlotRunner 最后的 status 信号决定（已停止/试放完成/错误）。
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
        if self._macro_busy():
            self._notify(
                "当前正在路线编辑/地图测试/路径执行/副本或组合挂机",
                "请先停止当前自动化，再启动基础挂机。",
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
        if self._any_multi_running():
            self._stop_all_multi()
            return
        if self._fatigue_loop_running:
            self._stop_fatigue_loop()
            return
        if self._combo_running:
            self._stop_combo()
            return
        if self._dungeon_recorder is not None or self._dungeon_recording_pending:
            self._stop_dungeon_record()
            return
        if self._dungeon_running:
            self._stop_dungeon_play()
            return
        if self._map_runner is not None:
            self._stop_map_runner()
            return
        if self._path_runner is not None:
            self._stop_path_runner()
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
            "spin_fl_threshold",
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


    # ------------------------------------------------------------ 地图标注寻路
    def _refresh_map_profiles(self) -> None:
        if not hasattr(self, "combo_map_profile"):
            return
        try:
            from map_labels import load_maps

            profiles = load_maps()
        except Exception:
            profiles = []
        current = self.combo_map_profile.currentData()
        current_name = str(current.get("name", "")) if isinstance(current, dict) else ""
        self.combo_map_profile.blockSignals(True)
        self.combo_map_profile.clear()
        for profile in profiles:
            self.combo_map_profile.addItem(
                str(profile.get("name", "未命名")), profile
            )
        self.combo_map_profile.blockSignals(False)
        if current_name:
            idx = next(
                (
                    i
                    for i in range(self.combo_map_profile.count())
                    if str(
                        (self.combo_map_profile.itemData(i) or {}).get("name", "")
                    )
                    == current_name
                ),
                -1,
            )
            if idx >= 0:
                self.combo_map_profile.setCurrentIndex(idx)
        self._refresh_map_targets()

    def _refresh_map_targets(self, *_args) -> None:
        if not hasattr(self, "combo_map_target"):
            return
        profile = self.combo_map_profile.currentData()
        self.combo_map_target.blockSignals(True)
        self.combo_map_target.clear()
        if isinstance(profile, dict):
            for target in profile.get("targets", []):
                if not isinstance(target, dict):
                    continue
                kind = {"farm": "打怪点", "boss": "BOSS点", "other": "其他"}.get(
                    str(target.get("kind", "")), "其他"
                )
                self.combo_map_target.addItem(
                    f"{target.get('name', '')} [{kind}]", target
                )
        self.combo_map_target.blockSignals(False)
        self._update_map_controls_state()

    def _update_map_controls_state(self) -> None:
        if not hasattr(self, "btn_map_test"):
            return
        map_running = self._map_runner is not None
        busy = self._macro_busy()
        usable = bool(self.combo_map_profile.currentData()) and bool(
            self.combo_map_target.currentData()
        )
        target_ok = (
            self.target_hwnd is not None and self._window_alive(self.target_hwnd)
        )
        self.btn_map_test.setText("停止地图测试" if map_running else "测试前往目标")
        self.btn_map_test.setEnabled(
            map_running or (usable and target_ok and not busy)
        )
        self.btn_map_learn.setEnabled(target_ok and not busy and not map_running)
        self.combo_map_profile.setEnabled(not busy and not map_running)
        self.combo_map_target.setEnabled(not busy and not map_running)
        self.spin_map_travel_wait.setEnabled(not busy and not map_running)

    # ------------------------------------------------------------ 路径执行
    def _refresh_paths(self) -> None:
        if not hasattr(self, "combo_path"):
            return
        try:
            from paths import load_paths

            paths = load_paths()
        except Exception:
            paths = []
        current = self.combo_path.currentData()
        current_name = str(current.get("name", "")) if isinstance(current, dict) else ""
        self.combo_path.blockSignals(True)
        self.combo_path.clear()
        for path in paths:
            self.combo_path.addItem(str(path.get("name", "未命名")), path)
        self.combo_path.blockSignals(False)
        if current_name:
            idx = next(
                (
                    i
                    for i in range(self.combo_path.count())
                    if str((self.combo_path.itemData(i) or {}).get("name", ""))
                    == current_name
                ),
                -1,
            )
            if idx >= 0:
                self.combo_path.setCurrentIndex(idx)
        self._update_path_controls_state()
        self._refresh_fatigue_loop_options()

    def _update_path_controls_state(self) -> None:
        if not hasattr(self, "btn_path_run"):
            return
        running = self._path_runner is not None
        busy = self._macro_busy()
        has_path = isinstance(self.combo_path.currentData(), dict)
        target_ok = (
            self.target_hwnd is not None and self._window_alive(self.target_hwnd)
        )
        self.btn_path_run.setText("停止路径" if running else "执行路径")
        self.btn_path_run.setEnabled(running or (has_path and target_ok and not busy))
        self.btn_path_edit.setEnabled(target_ok and not busy and not running)
        self.combo_path.setEnabled(not busy and not running)

    def _open_path_editor(self) -> None:
        if self._macro_busy():
            self._notify("当前正忙", "请先停止其它自动化。")
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return
        try:
            from paths import PathEditorDialog
        except Exception as exc:
            self._notify("路径编辑器不可用", f"加载失败：{exc}")
            return
        dialog = PathEditorDialog(parent=self)
        dialog.exec()
        self._refresh_paths()
        self._refresh_combos()
        self._refresh_fatigue_loop_options()
        self._set_status("路径列表已刷新。", "ok")

    def _toggle_path_run(self) -> None:
        if self._path_runner is not None:
            self._stop_path_runner()
            return
        self._start_path_run()

    def _start_path_run(self) -> None:
        if self._quitting or self._path_runner is not None:
            return
        if self._macro_busy() or self._worker is not None:
            self._notify("当前正忙", "请先停止其它自动化。")
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return
        path = self.combo_path.currentData()
        if not isinstance(path, dict):
            self._notify("没有路径", "请先点“编辑路径”创建一条路径。")
            return
        try:
            from map_labels import load_maps
            from paths import PathRunner
        except Exception as exc:
            self._notify("路径执行不可用", f"加载失败：{exc}")
            return
        profiles = load_maps()
        if not profiles:
            self._notify("没有地图标注", "请先在“地图标注寻路”里人工标注地图。")
            return
        runner = PathRunner(
            self.target_hwnd,
            path,
            profiles,
            map_key=self._macro_cfg.get("map_key", "m"),
            close_key=self._macro_cfg.get("close_map_key", "esc"),
            mount_key=self._macro_cfg.get("mount_key", "f"),
            parent=self,
        )
        runner.status.connect(self._on_path_status)
        runner.done.connect(self._on_path_done)
        runner.failed.connect(self._on_path_failed)
        self._path_runner = runner
        self.lbl_path_state.setText("路径执行：准备中…")
        self._set_status("路径执行开始。", "warn")
        self._update_macro_button_states()
        self._update_path_controls_state()
        runner.start()

    def _on_path_status(self, text: str, level: str) -> None:
        if hasattr(self, "lbl_path_state"):
            self.lbl_path_state.setText(text)
        self._set_status(text, level)

    def _on_path_done(self) -> None:
        runner = self.sender()
        if runner is self._path_runner:
            self._path_runner = None
            if isinstance(runner, QThread):
                runner.wait(500)
                runner.deleteLater()
        self.lbl_path_state.setText("路径执行完成")
        self._set_status("路径执行完成。", "ok")
        self._update_macro_button_states()
        self._update_path_controls_state()

    def _on_path_failed(self, message: str) -> None:
        runner = self.sender()
        if runner is self._path_runner:
            self._path_runner = None
            if isinstance(runner, QThread):
                runner.wait(500)
                runner.deleteLater()
        stopped = str(message).strip() == "已停止。"
        self.lbl_path_state.setText("已停止" if stopped else "路径执行失败")
        self._set_status(
            "路径执行已停止。" if stopped else f"路径执行失败：{message}",
            "warn" if stopped else "error",
        )
        if not stopped:
            try:
                self._popup.show_message("路径执行失败", message, seconds=15)
            except Exception:
                pass
        self._update_macro_button_states()
        self._update_path_controls_state()

    def _stop_path_runner(self) -> None:
        runner = self._path_runner
        self._path_runner = None
        if runner is not None:
            runner.stop()
            if not runner.wait(4000):
                runner.terminate()
                runner.wait(500)
            runner.deleteLater()
        self.lbl_path_state.setText("已停止")
        self._set_status("路径执行已停止。", "warn")
        self._update_macro_button_states()
        self._update_path_controls_state()

    # ------------------------------------------------------------ 副本整段录制 / 回放
    def _refresh_dungeon_records(self) -> None:
        if not hasattr(self, "combo_dungeon_record"):
            return
        current = self.combo_dungeon_record.currentData()
        current_name = (
            str(current.get("name", "")) if isinstance(current, dict) else ""
        )
        self.combo_dungeon_record.blockSignals(True)
        self.combo_dungeon_record.clear()
        for record in self._dungeon_records:
            duration = float(record.get("duration", 0.0) or 0.0)
            clicks = int(record.get("clicks", 0) or 0)
            if clicks == 0:
                clicks = sum(
                    1
                    for event in record.get("events", [])
                    if isinstance(event, dict)
                    and event.get("type") == "down"
                )
            self.combo_dungeon_record.addItem(
                f"{record.get('name', '未命名')}  "
                f"({duration:.1f} 秒 / 点击 {clicks} 次)",
                record,
            )
        if current_name:
            idx = next(
                (
                    i
                    for i in range(self.combo_dungeon_record.count())
                    if str(
                        (self.combo_dungeon_record.itemData(i) or {}).get(
                            "name", ""
                        )
                    )
                    == current_name
                ),
                -1,
            )
            if idx >= 0:
                self.combo_dungeon_record.setCurrentIndex(idx)
        self.combo_dungeon_record.blockSignals(False)
        self._update_dungeon_controls()
        self._refresh_fatigue_loop_options()
        self._refresh_multi_records()

    def _selected_dungeon_record(self) -> Optional[dict]:
        data = self.combo_dungeon_record.currentData()
        return data if isinstance(data, dict) else None

    def _update_dungeon_controls(self) -> None:
        if not hasattr(self, "btn_dungeon_run"):
            return
        recording = (
            self._dungeon_recorder is not None
            or self._dungeon_recording_pending
        )
        running = self._dungeon_running
        has_record = isinstance(
            self.combo_dungeon_record.currentData(), dict
        )
        target_ok = (
            self.target_hwnd is not None
            and self._window_alive(self.target_hwnd)
        )
        busy_other = bool(
            self._worker is not None
            or self._route_editor is not None
            or self._map_runner is not None
            or self._path_runner is not None
            or self._combo_running
            or self._combo_path_runner is not None
            or self._combo_player is not None
            or self._fatigue_loop_running
            or self._fatigue_loop_worker is not None
            or self._fatigue_loop_monitor is not None
            or self._fatigue_loop_path_runner is not None
            or self._fatigue_loop_player is not None
            or self._any_multi_running()
        )
        idle = (not recording) and (not running) and (not busy_other)

        self.btn_dungeon_record.setText(
            "取消录制"
            if self._dungeon_recording_pending
            else ("停止录制" if recording else "开始录制")
        )
        record_enabled = recording or (idle and target_ok)
        self.btn_dungeon_record.setEnabled(record_enabled)
        if hasattr(self, "btn_fl_record"):
            self.btn_fl_record.setText(self.btn_dungeon_record.text())
            self.btn_fl_record.setEnabled(record_enabled)
        for widget in (
            self.combo_dungeon_record,
            self.btn_dungeon_rename,
            self.btn_dungeon_delete,
        ):
            widget.setEnabled(idle and has_record)
        self.btn_dungeon_test.setEnabled(idle and has_record and target_ok)
        self.btn_dungeon_run.setText("停止副本" if running else "开始副本")
        self.btn_dungeon_run.setEnabled(
            running or (idle and has_record and target_ok)
        )
        for widget in (
            self.spin_dungeon_repeat,
            self.spin_dungeon_interval,
            self.spin_dungeon_speed,
        ):
            widget.setEnabled(not recording and not running)

    def _toggle_dungeon_record(self) -> None:
        if self._dungeon_recorder is not None or self._dungeon_recording_pending:
            self._stop_dungeon_record()
            return
        self._start_dungeon_record()

    def _start_dungeon_record(self) -> None:
        if self._quitting or self._dungeon_running:
            return
        if self._macro_busy():
            self._notify("当前正忙", "请先停止基础挂机/地图测试/路径执行。")
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return
        self._dungeon_recording_pending = True
        self.lbl_dungeon_state.setText("3 秒后开始录制，请切到游戏…")
        self._set_status(
            "3 秒后开始录制副本；请切到游戏，完整操作后回来点“停止录制”。",
            "warn",
        )
        self._update_dungeon_controls()
        self._update_toggle_action()
        QTimer.singleShot(3000, self._begin_dungeon_record)

    def _begin_dungeon_record(self) -> None:
        if not self._dungeon_recording_pending:
            return
        self._dungeon_recording_pending = False
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self.lbl_dungeon_state.setText("未运行")
            self._set_status("绑定窗口已失效，录制未开始。", "error")
            self._update_dungeon_controls()
            return
        recorder = MacroRecorder(self.target_hwnd)
        try:
            _force_foreground_window(self.target_hwnd)
            recorder.start()
        except Exception as exc:
            self.lbl_dungeon_state.setText("录制启动失败")
            self._set_status(f"录制启动失败：{exc}", "error")
            self._notify("录制启动失败", str(exc))
            self._update_dungeon_controls()
            return
        self._dungeon_recorder = recorder
        self.lbl_dungeon_state.setText("录制中… 0 个动作")
        self._set_status(
            "正在录制副本。请在游戏里操作，完成后切回本窗口点“停止录制”。",
            "ok",
        )
        self._update_dungeon_controls()
        self._update_toggle_action()

    @staticmethod
    def _strip_dungeon_hotkey_tail(events: list[dict]) -> list[dict]:
        """用 Ctrl+Alt+F8 停止录制时，把结尾这组热键事件从录制里去掉。"""
        if not events:
            return events
        try:
            last_t = float(events[-1].get("t", 0.0))
        except (TypeError, ValueError):
            last_t = 0.0
        cutoff = last_t - 1.0
        hot_keys = {"ctrl", "alt", "f8"}
        cleaned = []
        for event in events:
            try:
                t = float(event.get("t", 0.0))
            except (TypeError, ValueError):
                t = 0.0
            name = str(event.get("name", "")).lower()
            if (
                t >= cutoff
                and event.get("type") in ("key_down", "key_up")
                and name in hot_keys
            ):
                continue
            cleaned.append(event)
        return cleaned

    def _stop_dungeon_record(self) -> None:
        if self._dungeon_recording_pending:
            self._dungeon_recording_pending = False
            self.lbl_dungeon_state.setText("未运行")
            self._set_status("已取消录制。", "warn")
            self._update_dungeon_controls()
            self._update_toggle_action()
            return
        recorder = self._dungeon_recorder
        self._dungeon_recorder = None
        if recorder is None:
            self._update_dungeon_controls()
            return
        try:
            events = recorder.stop()
        except Exception:
            events = []
        events = self._strip_dungeon_hotkey_tail(events)
        meta = recorder.meta()
        clicks = sum(1 for e in events if e.get("type") == "down")
        duration = 0.0
        if events:
            try:
                duration = max(0.0, float(events[-1].get("t", 0.0)))
            except (TypeError, ValueError):
                duration = 0.0
        if not events:
            self.lbl_dungeon_state.setText("录制为空")
            self._set_status("没有录到任何操作，未保存。", "warn")
            self._notify(
                "录制为空",
                "没有录到操作。录制开始后请让游戏保持前台，并在游戏窗口内操作。",
            )
            self._update_dungeon_controls()
            self._update_toggle_action()
            return

        self._restore_window()
        default_name = "副本 " + datetime.datetime.now().strftime("%m-%d %H:%M:%S")
        name, ok = QInputDialog.getText(
            self, "保存副本录制", "录制名称：", text=default_name
        )
        name = name.strip() if ok else ""
        if not name:
            name = default_name
        existing = next(
            (
                i
                for i, rec in enumerate(self._dungeon_records)
                if str(rec.get("name", "")) == name
            ),
            None,
        )
        if existing is not None:
            answer = QMessageBox.question(
                self,
                "覆盖录制",
                f"录制「{name}」已存在，是否覆盖？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.lbl_dungeon_state.setText("已保留旧录制")
                self._update_dungeon_controls()
                self._update_toggle_action()
                return
        record = {
            "name": name,
            "events": events,
            "clicks": clicks,
            "duration": duration,
            "meta": meta,
            "updated_at": datetime.datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }
        if existing is not None:
            self._dungeon_records[existing] = record
        else:
            self._dungeon_records.append(record)
        if not save_records(self._dungeon_records):
            self._notify("保存失败", f"无法写入：{RECORDS_PATH}")
        self._refresh_dungeon_records()
        idx = next(
            (
                i
                for i in range(self.combo_dungeon_record.count())
                if str(
                    (self.combo_dungeon_record.itemData(i) or {}).get("name", "")
                )
                == name
            ),
            -1,
        )
        if idx >= 0:
            self.combo_dungeon_record.setCurrentIndex(idx)
        self.lbl_dungeon_state.setText(
            f"已保存：{name}（{len(events)} 个动作 / {duration:.1f} 秒）"
        )
        if clicks == 0:
            self._notify(
                "这段录制没有鼠标点击",
                "录制里没有录到任何鼠标点击。如果“进入游戏”是在另一个窗口点的，"
                "请先让那个窗口在前台；或者勾选“回放前识别并点击”。",
            )
        self._set_status(
            f"副本录制已保存：{name}（点击 {clicks} 次）", "ok"
        )
        self._update_dungeon_controls()
        self._update_toggle_action()

    def _rename_dungeon_record(self) -> None:
        record = self._selected_dungeon_record()
        if record is None:
            return
        old_name = str(record.get("name", ""))
        new_name, ok = QInputDialog.getText(
            self, "重命名录制", "新名称：", text=old_name
        )
        new_name = new_name.strip()
        if not ok or not new_name or new_name == old_name:
            return
        if any(
            str(rec.get("name", "")) == new_name
            for rec in self._dungeon_records
            if str(rec.get("name", "")) != old_name
        ):
            self._notify("名称重复", f"录制「{new_name}」已存在。")
            return
        actual = next(
            (
                rec
                for rec in self._dungeon_records
                if str(rec.get("name", "")) == old_name
            ),
            None,
        )
        if actual is None:
            self._notify("录制不存在", f"找不到录制「{old_name}」。")
            return
        actual["name"] = new_name
        actual["updated_at"] = datetime.datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        if not save_records(self._dungeon_records):
            self._notify("保存失败", f"无法写入：{RECORDS_PATH}")
        combos_changed = False
        for combo in self._combos:
            if str(combo.get("record", "")) == old_name:
                combo["record"] = new_name
                combos_changed = True
        if combos_changed:
            save_combos(self._combos)
            self._refresh_combos()
        self._refresh_dungeon_records()
        self._set_status(f"已重命名：{new_name}", "ok")

    def _delete_dungeon_record(self) -> None:
        record = self._selected_dungeon_record()
        if record is None:
            return
        name = str(record.get("name", ""))
        answer = QMessageBox.question(
            self,
            "删除录制",
            f"确定删除录制「{name}」？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._dungeon_records = [
            rec
            for rec in self._dungeon_records
            if str(rec.get("name", "")) != name
        ]
        if not save_records(self._dungeon_records):
            self._notify("保存失败", f"无法写入：{RECORDS_PATH}")
        combos_changed = False
        for combo in self._combos:
            if str(combo.get("record", "")) == name:
                combo["record"] = ""
                combos_changed = True
        if combos_changed:
            save_combos(self._combos)
            self._refresh_combos()
        self._refresh_dungeon_records()
        self._set_status(f"已删除录制：{name}", "warn")

    def _play_dungeon_once_test(self) -> None:
        self._start_dungeon_play(loop=False)

    def _toggle_dungeon_run(self) -> None:
        if self._dungeon_running:
            self._stop_dungeon_play("副本回放已停止。")
            return
        self._start_dungeon_play(loop=True)

    def _start_dungeon_play(self, loop: bool) -> None:
        if self._quitting or self._dungeon_running:
            return
        if self._dungeon_recorder is not None or self._dungeon_recording_pending:
            self._notify("正在录制", "请先停止录制。")
            return
        if self._macro_busy():
            self._notify("当前正忙", "请先停止基础挂机/地图测试/路径执行。")
            return
        record = self._selected_dungeon_record()
        if record is None or not record.get("events"):
            self._notify("没有录制", "请先录制一段副本流程。")
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return

        repeat = int(self.spin_dungeon_repeat.value())
        self._dungeon_total = 1 if not loop else repeat
        self._dungeon_done_count = 0
        self._dungeon_interval = float(self.spin_dungeon_interval.value())
        self._dungeon_speed = max(
            0.5, min(2.0, float(self.spin_dungeon_speed.value()) / 100.0)
        )
        self._dungeon_running = True
        total_label = "∞" if self._dungeon_total == 0 else str(self._dungeon_total)
        self.lbl_dungeon_state.setText(f"准备回放 0/{total_label}…")
        self._set_status(
            "开始副本回放：请勿操作鼠标键盘，结束后会自动恢复前台窗口。",
            "warn",
        )
        self._update_dungeon_controls()
        self._update_toggle_action()
        self._play_dungeon_once()

    def _play_dungeon_once(self) -> None:
        if not self._dungeon_running:
            return
        if self._dungeon_total > 0 and self._dungeon_done_count >= self._dungeon_total:
            self._finish_dungeon_play()
            return
        if self._dungeon_player is not None:
            return
        record = self._selected_dungeon_record()
        if record is None or not record.get("events"):
            self._stop_dungeon_play("录制不存在，已停止。", "error")
            return
        events = list(record.get("events", []))
        meta = record.get("meta")
        player = MacroPlayer(
            self.target_hwnd,
            "副本",
            events,
            meta=meta if isinstance(meta, dict) else None,
            speed=self._dungeon_speed,
            lock_user_input=bool(self.chk_lock_input.isChecked()),
            background=bool(self.chk_background_replay.isChecked()),
            parent=self,
        )
        player.done.connect(self._on_dungeon_player_done)
        player.error.connect(self._on_dungeon_player_error)
        self._dungeon_player = player
        total_label = "∞" if self._dungeon_total == 0 else str(self._dungeon_total)
        self.lbl_dungeon_state.setText(
            f"回放中 {self._dungeon_done_count + 1}/{total_label}…"
        )
        player.start()

    def _on_dungeon_player_done(self, stage: str) -> None:
        player = self.sender()
        if isinstance(player, MacroPlayer):
            player.wait(500)
            player.deleteLater()
        if player is not self._dungeon_player:
            return
        self._dungeon_player = None
        if not self._dungeon_running:
            return
        self._dungeon_done_count += 1
        total_label = "∞" if self._dungeon_total == 0 else str(self._dungeon_total)
        self.lbl_dungeon_state.setText(
            f"已完成 {self._dungeon_done_count}/{total_label}"
        )
        if (
            self._dungeon_total > 0
            and self._dungeon_done_count >= self._dungeon_total
        ):
            self._finish_dungeon_play()
            return
        if self._dungeon_pending_timer is None:
            self._dungeon_pending_timer = QTimer(self)
            self._dungeon_pending_timer.setSingleShot(True)
            self._dungeon_pending_timer.timeout.connect(self._play_dungeon_once)
        self._set_status(
            f"下一次回放将在 {self._dungeon_interval:.0f} 秒后开始…", "warn"
        )
        self._dungeon_pending_timer.start(
            max(0, int(self._dungeon_interval * 1000))
        )

    def _on_dungeon_player_error(self, stage: str, message: str) -> None:
        player = self.sender()
        if isinstance(player, MacroPlayer):
            player.wait(500)
            player.deleteLater()
        if player is not self._dungeon_player:
            return
        self._dungeon_player = None
        self._stop_dungeon_play(f"副本回放失败：{message}", "error")
        try:
            self._popup.show_message("副本回放失败", message, seconds=15)
        except Exception:
            pass

    def _finish_dungeon_play(self) -> None:
        self._dungeon_running = False
        if self._dungeon_pending_timer is not None:
            self._dungeon_pending_timer.stop()
        self.lbl_dungeon_state.setText("副本回放完成")
        self._set_status("副本回放完成。", "ok")
        self._update_dungeon_controls()
        self._update_toggle_action()

    def _stop_dungeon_play(
        self, message: str = "副本回放已停止。", level: str = "warn"
    ) -> None:
        was_running = self._dungeon_running
        self._dungeon_running = False
        if self._dungeon_pending_timer is not None:
            self._dungeon_pending_timer.stop()
        player = self._dungeon_player
        self._dungeon_player = None
        if player is not None:
            player.stop()
            if not player.wait(3000):
                player.terminate()
                player.wait(500)
            player.deleteLater()
        if was_running:
            self.lbl_dungeon_state.setText("已停止")
            self._set_status(message, level)
        self._update_dungeon_controls()
        self._update_toggle_action()

    # ------------------------------------------------------------ 组合挂机
    def _refresh_combos(self) -> None:
        if not hasattr(self, "combo_combo"):
            return
        current = self.combo_combo.currentData()
        current_name = (
            str(current.get("name", "")) if isinstance(current, dict) else ""
        )
        self.combo_combo.blockSignals(True)
        self.combo_combo.clear()
        for combo in self._combos:
            path_name = str(combo.get("path", "")).strip() or "无路径"
            record_name = str(combo.get("record", "")).strip() or "无录制"
            repeat = int(combo.get("repeat", 1))
            repeat_text = "∞" if repeat == 0 else str(repeat)
            self.combo_combo.addItem(
                f"{combo.get('name', '未命名')}  [{path_name} + {record_name}] ×{repeat_text}",
                combo,
            )
        if current_name:
            idx = next(
                (
                    i
                    for i in range(self.combo_combo.count())
                    if str(
                        (self.combo_combo.itemData(i) or {}).get("name", "")
                    )
                    == current_name
                ),
                -1,
            )
            if idx >= 0:
                self.combo_combo.setCurrentIndex(idx)
        self.combo_combo.blockSignals(False)
        self._update_combo_controls()

    def _selected_combo(self) -> Optional[dict]:
        data = self.combo_combo.currentData()
        return data if isinstance(data, dict) else None

    def _update_combo_controls(self) -> None:
        if not hasattr(self, "btn_combo_run"):
            return
        running = self._combo_running
        has_combo = isinstance(self.combo_combo.currentData(), dict)
        target_ok = (
            self.target_hwnd is not None
            and self._window_alive(self.target_hwnd)
        )
        busy_other = bool(
            self._worker is not None
            or self._route_editor is not None
            or self._map_runner is not None
            or self._path_runner is not None
            or self._dungeon_recorder is not None
            or self._dungeon_recording_pending
            or self._dungeon_running
            or self._dungeon_player is not None
            or self._combo_running
            or self._combo_path_runner is not None
            or self._combo_player is not None
            or self._any_multi_running()
        )
        idle = (not running) and (not busy_other)
        self.combo_combo.setEnabled(idle and has_combo)
        self.btn_combo_new.setEnabled(idle)
        self.btn_combo_edit.setEnabled(idle and has_combo)
        self.btn_combo_delete.setEnabled(idle and has_combo)
        self.btn_combo_run.setText("停止组合" if running else "开始组合")
        self.btn_combo_run.setEnabled(
            running or (idle and has_combo and target_ok)
        )

    def _new_combo(self) -> None:
        if self._combo_running:
            return
        try:
            from paths import load_paths

            paths = load_paths()
        except Exception:
            paths = []
        dialog = ComboEditDialog(paths, self._dungeon_records, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        data = dialog.combo_data()
        if any(
            str(combo.get("name", "")) == data["name"]
            for combo in self._combos
        ):
            self._notify("名称重复", f"组合「{data['name']}」已存在。")
            return
        self._combos.append(data)
        if not save_combos(self._combos):
            self._notify("保存失败", f"无法写入：{COMBOS_PATH}")
        self._refresh_combos()
        idx = next(
            (
                i
                for i in range(self.combo_combo.count())
                if str((self.combo_combo.itemData(i) or {}).get("name", ""))
                == data["name"]
            ),
            -1,
        )
        if idx >= 0:
            self.combo_combo.setCurrentIndex(idx)
        self._set_status(f"已新建组合：{data['name']}", "ok")

    def _edit_combo(self) -> None:
        combo = self._selected_combo()
        if combo is None or self._combo_running:
            return
        try:
            from paths import load_paths

            paths = load_paths()
        except Exception:
            paths = []
        dialog = ComboEditDialog(
            paths, self._dungeon_records, combo, parent=self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        data = dialog.combo_data()
        old_name = str(combo.get("name", ""))
        if any(
            str(other.get("name", "")) == data["name"]
            for other in self._combos
            if str(other.get("name", "")) != old_name
        ):
            self._notify("名称重复", f"组合「{data['name']}」已存在。")
            return
        actual = next(
            (
                item
                for item in self._combos
                if str(item.get("name", "")) == old_name
            ),
            None,
        )
        if actual is None:
            self._notify("组合不存在", f"找不到组合「{old_name}」。")
            return
        actual.clear()
        actual.update(data)
        if not save_combos(self._combos):
            self._notify("保存失败", f"无法写入：{COMBOS_PATH}")
        self._refresh_combos()
        idx = next(
            (
                i
                for i in range(self.combo_combo.count())
                if str((self.combo_combo.itemData(i) or {}).get("name", ""))
                == data["name"]
            ),
            -1,
        )
        if idx >= 0:
            self.combo_combo.setCurrentIndex(idx)
        self._set_status(f"组合已保存：{data['name']}", "ok")

    def _delete_combo(self) -> None:
        combo = self._selected_combo()
        if combo is None or self._combo_running:
            return
        name = str(combo.get("name", ""))
        answer = QMessageBox.question(
            self,
            "删除组合",
            f"确定删除组合「{name}」？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._combos = [
            item
            for item in self._combos
            if str(item.get("name", "")) != name
        ]
        if not save_combos(self._combos):
            self._notify("保存失败", f"无法写入：{COMBOS_PATH}")
        self._refresh_combos()
        self._set_status(f"已删除组合：{name}", "warn")

    def _toggle_combo(self) -> None:
        if self._combo_running:
            self._stop_combo("组合挂机已停止。")
            return
        self._start_combo()

    def _start_combo(self) -> None:
        if self._quitting or self._combo_running:
            return
        if self._macro_busy():
            self._notify(
                "当前正忙", "请先停止基础挂机/路径执行/副本录制或回放。"
            )
            return
        combo = self._selected_combo()
        if combo is None:
            self._notify("没有组合", "请先新建并选择一个组合。")
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return

        path_name = str(combo.get("path", "")).strip()
        record_name = str(combo.get("record", "")).strip()
        if not path_name and not record_name:
            self._notify("组合为空", "组合里路径和副本录制至少要有一个。")
            return
        if path_name:
            try:
                from paths import load_paths

                paths = load_paths()
            except Exception:
                paths = []
            if not any(
                str(path.get("name", "")) == path_name for path in paths
            ):
                self._notify(
                    "路径不存在",
                    f"组合引用的路径「{path_name}」不存在，请编辑组合重新选择。",
                )
                return
        if record_name and not any(
            str(record.get("name", "")) == record_name
            for record in self._dungeon_records
        ):
            self._notify(
                "录制不存在",
                f"组合引用的录制「{record_name}」不存在，请编辑组合重新选择。",
            )
            return

        self._combo_cfg = dict(combo)
        self._combo_name = str(combo.get("name", ""))
        self._combo_total = max(0, int(combo.get("repeat", 1)))
        self._combo_done_count = 0
        self._combo_running = True
        total_label = "∞" if self._combo_total == 0 else str(self._combo_total)
        self.lbl_combo_state.setText(f"准备 0/{total_label}…")
        self._set_status(
            f"开始组合挂机：{self._combo_name}", "ok"
        )
        self._update_combo_controls()
        self._update_macro_button_states()
        self._update_toggle_action()
        self._combo_run_path()

    def _combo_next_label(self) -> str:
        total_label = "∞" if self._combo_total == 0 else str(self._combo_total)
        return f"{self._combo_done_count + 1}/{total_label}"

    def _combo_run_path(self) -> None:
        if not self._combo_running:
            return
        if self._combo_path_runner is not None or self._combo_player is not None:
            return
        path_name = str(self._combo_cfg.get("path", "")).strip()
        if not path_name:
            self._combo_play_record()
            return
        try:
            from map_labels import load_maps
            from paths import PathRunner, load_paths
        except Exception as exc:
            self._stop_combo(f"加载路径模块失败：{exc}", "error")
            return
        path = next(
            (
                item
                for item in load_paths()
                if str(item.get("name", "")) == path_name
            ),
            None,
        )
        if path is None:
            self._stop_combo(f"路径「{path_name}」不存在。", "error")
            return
        profiles = load_maps()
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._stop_combo("目标窗口已关闭。", "error")
            return
        runner = PathRunner(
            self.target_hwnd,
            path,
            profiles,
            map_key=self._macro_cfg.get("map_key", "m"),
            close_key=self._macro_cfg.get("close_map_key", "esc"),
            mount_key=self._macro_cfg.get("mount_key", "f"),
            parent=self,
        )
        runner.status.connect(self._on_combo_path_status)
        runner.done.connect(self._on_combo_path_done)
        runner.failed.connect(self._on_combo_path_failed)
        self._combo_path_runner = runner
        self.lbl_combo_state.setText(
            f"第 {self._combo_next_label()} 轮：执行路径「{path_name}」…"
        )
        runner.start()

    def _on_combo_path_status(self, text: str, level: str) -> None:
        self.lbl_combo_state.setText(text)
        self._set_status(text, level)

    def _on_combo_path_done(self) -> None:
        runner = self.sender()
        if isinstance(runner, QThread):
            runner.wait(500)
            runner.deleteLater()
        if runner is not self._combo_path_runner:
            return
        self._combo_path_runner = None
        if not self._combo_running:
            return
        self._combo_play_record()

    def _on_combo_path_failed(self, message: str) -> None:
        runner = self.sender()
        if isinstance(runner, QThread):
            runner.wait(500)
            runner.deleteLater()
        if runner is not self._combo_path_runner:
            return
        self._combo_path_runner = None
        if not self._combo_running:
            return
        self._stop_combo(f"组合路径失败：{message}", "error")
        try:
            self._popup.show_message("组合路径失败", message, seconds=15)
        except Exception:
            pass

    def _combo_play_record(self) -> None:
        if not self._combo_running or self._combo_player is not None:
            return
        record_name = str(self._combo_cfg.get("record", "")).strip()
        if not record_name:
            self._combo_cycle_done()
            return
        record = next(
            (
                item
                for item in self._dungeon_records
                if str(item.get("name", "")) == record_name
            ),
            None,
        )
        if record is None or not record.get("events"):
            self._stop_combo(
                f"副本录制「{record_name}」不存在或为空。", "error"
            )
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._stop_combo("目标窗口已关闭。", "error")
            return
        meta = record.get("meta")
        player = MacroPlayer(
            self.target_hwnd,
            "组合",
            list(record.get("events", [])),
            meta=meta if isinstance(meta, dict) else None,
            speed=float(self._combo_cfg.get("speed", 1.0) or 1.0),
            lock_user_input=bool(self.chk_lock_input.isChecked()),
            background=bool(self.chk_background_replay.isChecked()),
            parent=self,
        )
        player.done.connect(self._on_combo_player_done)
        player.error.connect(self._on_combo_player_error)
        self._combo_player = player
        self.lbl_combo_state.setText(
            f"第 {self._combo_next_label()} 轮：回放副本录制「{record_name}」…"
        )
        player.start()

    def _on_combo_player_done(self, stage: str) -> None:
        player = self.sender()
        if isinstance(player, MacroPlayer):
            player.wait(500)
            player.deleteLater()
        if player is not self._combo_player:
            return
        self._combo_player = None
        if not self._combo_running:
            return
        self._combo_cycle_done()

    def _on_combo_player_error(self, stage: str, message: str) -> None:
        player = self.sender()
        if isinstance(player, MacroPlayer):
            player.wait(500)
            player.deleteLater()
        if player is not self._combo_player:
            return
        self._combo_player = None
        if not self._combo_running:
            return
        self._stop_combo(f"组合录制回放失败：{message}", "error")
        try:
            self._popup.show_message("组合录制回放失败", message, seconds=15)
        except Exception:
            pass

    def _combo_cycle_done(self) -> None:
        if not self._combo_running:
            return
        self._combo_done_count += 1
        total_label = "∞" if self._combo_total == 0 else str(self._combo_total)
        self.lbl_combo_state.setText(
            f"已完成 {self._combo_done_count}/{total_label}"
        )
        if (
            self._combo_total > 0
            and self._combo_done_count >= self._combo_total
        ):
            self._finish_combo()
            return
        interval = max(0.0, float(self._combo_cfg.get("interval", 0) or 0))
        if self._combo_pending_timer is None:
            self._combo_pending_timer = QTimer(self)
            self._combo_pending_timer.setSingleShot(True)
            self._combo_pending_timer.timeout.connect(self._combo_run_path)
        self._set_status(
            f"下一轮组合将在 {interval:.0f} 秒后开始…", "warn"
        )
        self._combo_pending_timer.start(max(0, int(interval * 1000)))

    def _finish_combo(self) -> None:
        self._combo_running = False
        if self._combo_pending_timer is not None:
            self._combo_pending_timer.stop()
        self.lbl_combo_state.setText("组合挂机完成")
        self._set_status(f"组合挂机完成：{self._combo_name}", "ok")
        self._update_combo_controls()
        self._update_macro_button_states()
        self._update_toggle_action()

    def _stop_combo(
        self, message: str = "组合挂机已停止。", level: str = "warn"
    ) -> None:
        was_running = self._combo_running
        self._combo_running = False
        if self._combo_pending_timer is not None:
            self._combo_pending_timer.stop()
        runner = self._combo_path_runner
        self._combo_path_runner = None
        if runner is not None:
            runner.stop()
            if not runner.wait(4000):
                runner.terminate()
                runner.wait(500)
            runner.deleteLater()
        player = self._combo_player
        self._combo_player = None
        if player is not None:
            player.stop()
            if not player.wait(3000):
                player.terminate()
                player.wait(500)
            player.deleteLater()
        if was_running:
            self.lbl_combo_state.setText("已停止")
            self._set_status(message, level)
        self._update_combo_controls()
        self._update_macro_button_states()
        self._update_toggle_action()

    # ------------------------------------------------------------ 疲劳循环（打怪 ↔ BOSS）
    def _refresh_fatigue_loop_options(self) -> None:
        if not hasattr(self, "combo_fl_record"):
            return
        combo = self.combo_fl_record
        current = str(combo.currentData() or "")
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("（请选择 BOSS 整段录制）", "")
        for record in self._dungeon_records:
            name = str(record.get("name", "")).strip()
            if name:
                combo.addItem(name, name)
        idx = combo.findData(current)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.blockSignals(False)
        self._update_fatigue_loop_controls()

    def _refresh_fl_boss_targets(self, *_args) -> None:
        if not hasattr(self, "combo_fl_boss_target"):
            return
        profile_name = str(self.combo_fl_boss_map.currentData() or "")
        try:
            from map_labels import load_maps

            profiles = load_maps()
        except Exception:
            profiles = []
        profile = None
        if profile_name:
            profile = next(
                (
                    item
                    for item in profiles
                    if str(item.get("name", "")) == profile_name
                ),
                None,
            )
            if profile is None:
                profile = next(
                    (
                        item
                        for item in profiles
                        if profile_name in str(item.get("name", ""))
                        or str(item.get("name", "")) in profile_name
                    ),
                    None,
                )
        combo = self.combo_fl_boss_target
        current = str(combo.currentData() or "")
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("（请选择 BOSS 点）", "")
        if profile is not None:
            for target in profile.get("targets", []):
                if not isinstance(target, dict):
                    continue
                name = str(target.get("name", "")).strip()
                if name:
                    kind = {"farm": "打怪点", "boss": "BOSS点", "other": "其他"}.get(
                        str(target.get("kind", "")), "其他"
                    )
                    combo.addItem(f"{name} [{kind}]", name)
        idx = combo.findData(current)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.blockSignals(False)
        self._update_fatigue_loop_controls()

    def _update_fatigue_loop_controls(self) -> None:
        if not hasattr(self, "btn_fl_run"):
            return
        running = self._fatigue_loop_running
        target_ok = (
            self.target_hwnd is not None
            and self._window_alive(self.target_hwnd)
        )
        roi_ok = self._fatigue_roi_norm is not None
        busy_other = bool(
            self._worker is not None
            or self._route_editor is not None
            or self._map_runner is not None
            or self._path_runner is not None
            or self._dungeon_recorder is not None
            or self._dungeon_recording_pending
            or self._dungeon_running
            or self._dungeon_player is not None
            or self._combo_running
            or self._combo_path_runner is not None
            or self._combo_player is not None
        )
        idle = (not running) and (not busy_other)
        self.btn_fl_run.setText("停止疲劳循环" if running else "开始疲劳循环")
        # 不因为没选满而禁用按钮，点击后由 _start_fatigue_loop 给出具体原因。
        self.btn_fl_run.setEnabled(running or idle)
        missing = []
        if not self.combo_fl_record.currentData():
            missing.append("BOSS录制")
        if not target_ok:
            missing.append("绑定窗口")
        if not roi_ok:
            missing.append("框选疲劳区域")
        if missing:
            self.btn_fl_run.setToolTip("还缺少：" + "、".join(missing))
        else:
            self.btn_fl_run.setToolTip("开始疲劳循环")
        has_record = bool(self.combo_fl_record.currentData())
        for widget in (
            self.combo_fl_record,
            self.spin_fl_threshold,
            self.spin_fl_interval,
        ):
            widget.setEnabled(idle)
        if hasattr(self, "btn_fl_delete_record"):
            self.btn_fl_delete_record.setEnabled(idle and has_record)
        if hasattr(self, "btn_fl_test"):
            self.btn_fl_test.setEnabled(idle and has_record and target_ok)
        if hasattr(self, "btn_fl_record"):
            recording = (
                self._dungeon_recorder is not None
                or self._dungeon_recording_pending
            )
            self.btn_fl_record.setText(
                "取消录制"
                if self._dungeon_recording_pending
                else ("停止录制" if recording else "录制新脚本")
            )
            self.btn_fl_record.setEnabled(recording or (idle and target_ok))

    def _fl_set_state(self, text: str, level: str = "normal") -> None:
        if hasattr(self, "lbl_fl_state"):
            self.lbl_fl_state.setText(text)
        self._set_status(text, level)

    def _play_fl_record_test(self) -> None:
        name = str(self.combo_fl_record.currentData() or "").strip()
        if not name:
            return
        for i in range(self.combo_dungeon_record.count()):
            data = self.combo_dungeon_record.itemData(i) or {}
            if isinstance(data, dict) and str(data.get("name", "")) == name:
                self.combo_dungeon_record.setCurrentIndex(i)
                break
        self._play_dungeon_once_test()

    def _delete_fl_record(self) -> None:
        name = str(self.combo_fl_record.currentData() or "").strip()
        if not name or self._fatigue_loop_running:
            return
        # 同步选中第 4 组里的同一条录制，复用统一的删除逻辑。
        target_index = -1
        for i in range(self.combo_dungeon_record.count()):
            data = self.combo_dungeon_record.itemData(i) or {}
            if isinstance(data, dict) and str(data.get("name", "")) == name:
                target_index = i
                break
        if target_index >= 0:
            self.combo_dungeon_record.setCurrentIndex(target_index)
            self._delete_dungeon_record()
            return
        # 兜底：第 4 组没有这条时，直接按名字删除。
        answer = QMessageBox.question(
            self,
            "删除录制",
            f"确定删除录制「{name}」？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._dungeon_records = [
            rec
            for rec in self._dungeon_records
            if str(rec.get("name", "")) != name
        ]
        if not save_records(self._dungeon_records):
            self._notify("保存失败", f"无法写入：{RECORDS_PATH}")
        self._refresh_dungeon_records()
        self._refresh_fatigue_loop_options()
        self._set_status(f"已删除录制：{name}", "warn")

    def _toggle_fatigue_loop(self) -> None:
        if self._fatigue_loop_running:
            self._stop_fatigue_loop("疲劳循环已停止。")
            return
        self._start_fatigue_loop()

    def _start_fatigue_loop(self) -> None:
        if self._quitting or self._fatigue_loop_running:
            return
        if self._macro_busy():
            self._notify(
                "当前正忙", "请先停止基础挂机/路径执行/副本回放/组合挂机。"
            )
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return
        if self._fatigue_roi_norm is None:
            self._notify(
                "尚未框选疲劳区域",
                "请先在基础挂机页点击“框选区域”，框住右下角的疲劳提示按钮。",
            )
            return
        record_name = str(self.combo_fl_record.currentData() or "").strip()
        if not record_name:
            self._notify("没有录制", "请先选择一段 BOSS 整段录制。")
            return
        if not any(
            str(record.get("name", "")) == record_name
            for record in self._dungeon_records
        ):
            self._notify("录制不存在", f"BOSS 录制「{record_name}」不存在。")
            return

        self._fatigue_loop_cfg = {
            "record": record_name,
            "threshold": int(self.spin_fl_threshold.value()),
            "interval": float(self.spin_fl_interval.value()),
        }
        self._fatigue_loop_rounds = 0
        self._fatigue_loop_running = True
        self._fatigue_loop_state = "farming"
        self._fl_set_state("开始疲劳循环：先打怪，随后检测疲劳…", "ok")
        self._update_fatigue_loop_controls()
        self._update_macro_button_states()
        self._update_toggle_action()
        self._fl_start_farming()
        self._fl_schedule_check(2.0)

    def _fl_start_farming(self) -> None:
        if not self._fatigue_loop_running:
            return
        if self._fatigue_loop_worker is not None:
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._stop_fatigue_loop("目标窗口已关闭。", "error")
            return
        self._fatigue_loop_state = "farming"
        worker = KeySender(
            self.target_hwnd,
            self.spin_interval.value(),
            use_skills=self.chk_skills.isChecked(),
            use_potion=self.chk_potion.isChecked(),
            potion_interval_ms=self.spin_potion.value() * 1000,
            use_pickup=self.chk_pickup.isChecked(),
            parent=self,
        )
        worker.cycle_done.connect(self._on_fl_farm_cycle)
        worker.failed.connect(self._on_fl_farm_failed)
        self._fatigue_loop_worker = worker
        worker.start()

    def _fl_stop_farming(self) -> None:
        worker = self._fatigue_loop_worker
        self._fatigue_loop_worker = None
        if worker is not None:
            worker.stop()
            if not worker.wait(2500):
                worker.terminate()
                worker.wait(500)
            worker.deleteLater()

    def _on_fl_farm_cycle(self, rounds: int) -> None:
        if not self._fatigue_loop_running or self._fatigue_loop_state != "farming":
            return
        last = getattr(self, "_fl_last_fatigue_text", "")
        suffix = f" / {last}" if last else ""
        self.lbl_fl_state.setText(f"打怪中… 已 {int(rounds)} 轮{suffix}")

    def _on_fl_farm_failed(self, message: str) -> None:
        if not self._fatigue_loop_running:
            return
        if self._fatigue_loop_state != "farming":
            return
        self._stop_fatigue_loop(f"打怪线程停止：{message}", "error")
        try:
            self._popup.show_message("打怪线程停止", message, seconds=15)
        except Exception:
            pass

    def _fl_schedule_check(self, delay: float) -> None:
        if not self._fatigue_loop_running:
            return
        if self._fatigue_loop_timer is None:
            self._fatigue_loop_timer = QTimer(self)
            self._fatigue_loop_timer.setSingleShot(True)
            self._fatigue_loop_timer.timeout.connect(self._fl_check_once)
        self._fatigue_loop_timer.start(max(0, int(delay * 1000)))

    def _fl_check_once(self) -> None:
        if not self._fatigue_loop_running:
            return
        if self._fatigue_loop_monitor is not None:
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._stop_fatigue_loop("目标窗口已关闭。", "error")
            return
        if self._fatigue_roi_norm is None:
            self._stop_fatigue_loop("尚未框选疲劳提示区域。", "error")
            return
        monitor = FatigueMonitor(
            self.target_hwnd,
            self._fatigue_roi_norm,
            one_shot=True,
            parent=self,
        )
        monitor.result.connect(self._on_fl_fatigue_result)
        self._fatigue_loop_monitor = monitor
        if self._fatigue_loop_state == "farming":
            self.lbl_fl_state.setText("打怪中… 正在检测疲劳…")
        else:
            self.lbl_fl_state.setText("正在检测疲劳…")
        monitor.start()

    def _on_fl_fatigue_result(self, current, maximum, message: str) -> None:
        monitor = self.sender()
        if isinstance(monitor, FatigueMonitor):
            monitor.wait(2000)
            monitor.deleteLater()
        if monitor is not self._fatigue_loop_monitor:
            return
        self._fatigue_loop_monitor = None
        if not self._fatigue_loop_running:
            return
        if self._fatigue_loop_state != "farming":
            return
        threshold = int(
            self._fatigue_loop_cfg.get("threshold", self._fatigue_threshold)
        )
        interval = max(5.0, float(self._fatigue_loop_cfg.get("interval", 30)))
        if current is None or maximum is None:
            self._fl_set_state(
                f"疲劳检测失败：{message}（继续当前状态）", "warn"
            )
            if self._fatigue_loop_state == "farming":
                self._fl_start_farming()
                self._fl_schedule_check(interval)
            return
        current = int(current)
        maximum = int(maximum)
        self._fl_last_fatigue_text = f"上次疲劳 {current}/{maximum}"
        self._fl_set_state(
            f"当前疲劳 {current}/{maximum}（阈值 ±{threshold}）", "ok"
        )
        if current < -threshold:
            self._fl_go_boss(current, maximum, threshold)
        else:
            self._fl_continue_farming(
                f"疲劳 {current}/{maximum}，继续打怪…", interval
            )

    def _fl_go_boss(self, current: int, maximum: int, threshold: int) -> None:
        if not self._fatigue_loop_running:
            return
        self._fatigue_loop_state = "boss"
        if self._fatigue_loop_timer is not None:
            self._fatigue_loop_timer.stop()
        self._fl_stop_farming()
        self._fl_set_state(
            f"疲劳 {current}/{maximum} < -{threshold}，回放 BOSS 录制…", "warn"
        )
        self._fl_play_record()

    def _fl_continue_farming(
        self, message: str = "继续打怪…", interval: Optional[float] = None
    ) -> None:
        if not self._fatigue_loop_running:
            return
        if self._fatigue_loop_state in ("to_boss", "boss"):
            return
        self._fatigue_loop_state = "farming"
        self._fl_start_farming()
        self._fl_set_state(message, "ok")
        if interval is None:
            interval = max(
                5.0, float(self._fatigue_loop_cfg.get("interval", 30))
            )
        self._fl_schedule_check(interval)

    def _fl_resume_farming(
        self, current: int, maximum: int, threshold: int, interval: float
    ) -> None:
        self._fl_continue_farming(
            f"疲劳 {current}/{maximum} > +{threshold}，继续打怪…",
            interval,
        )

    def _fl_run_path(self, kind: str) -> None:
        if not self._fatigue_loop_running:
            return
        if self._fatigue_loop_path_runner is not None:
            return
        path_name = str(self._fatigue_loop_cfg.get(kind, "")).strip()
        if not path_name:
            self._stop_fatigue_loop(f"没有选择{kind}路径。", "error")
            return
        try:
            from map_labels import load_maps
            from paths import PathRunner, load_paths
        except Exception as exc:
            self._stop_fatigue_loop(f"加载路径模块失败：{exc}", "error")
            return
        path = next(
            (
                item
                for item in load_paths()
                if str(item.get("name", "")) == path_name
            ),
            None,
        )
        if path is None:
            self._stop_fatigue_loop(f"路径「{path_name}」不存在。", "error")
            return
        runner = PathRunner(
            self.target_hwnd,
            path,
            load_maps(),
            map_key=self._macro_cfg.get("map_key", "m"),
            close_key=self._macro_cfg.get("close_map_key", "esc"),
            mount_key=self._macro_cfg.get("mount_key", "f"),
            parent=self,
        )
        runner.status.connect(self._on_fl_path_status)
        runner.done.connect(self._on_fl_path_done)
        runner.failed.connect(self._on_fl_path_failed)
        self._fatigue_loop_path_kind = kind
        self._fatigue_loop_path_runner = runner
        label = "进路路径" if kind == "to_boss" else "返回路径"
        self.lbl_fl_state.setText(f"执行{label}「{path_name}」…")
        runner.start()

    def _on_fl_path_status(self, text: str, level: str) -> None:
        self.lbl_fl_state.setText(text)
        self._set_status(text, level)

    def _on_fl_path_done(self) -> None:
        runner = self.sender()
        if isinstance(runner, QThread):
            runner.wait(500)
            runner.deleteLater()
        if runner is not self._fatigue_loop_path_runner:
            return
        self._fatigue_loop_path_runner = None
        if not self._fatigue_loop_running:
            return
        kind = self._fatigue_loop_path_kind
        self._fl_map_open = bool(getattr(runner, "_map_open", False))
        if kind == "to_boss":
            self._fatigue_loop_state = "boss_point"
            self._fl_click_boss_point_and_wait()
        else:
            # 返回路径结束后地图可能还开着，先关掉再打怪。
            if self._fl_map_open:
                self._fl_tap_key(self._macro_cfg.get("close_map_key", "esc"))
                self._fl_map_open = False
            self._fl_continue_farming("已返回打怪点，继续打怪…")

    def _on_fl_path_failed(self, message: str) -> None:
        runner = self.sender()
        if isinstance(runner, QThread):
            runner.wait(500)
            runner.deleteLater()
        if runner is not self._fatigue_loop_path_runner:
            return
        self._fatigue_loop_path_runner = None
        if not self._fatigue_loop_running:
            return
        self._stop_fatigue_loop(f"疲劳循环路径失败：{message}", "error")
        try:
            self._popup.show_message("疲劳循环路径失败", message, seconds=15)
        except Exception:
            pass

    def _fl_tap_key(self, name: str) -> None:
        if not name:
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            return
        _force_foreground_window(self.target_hwnd)
        time.sleep(0.1)
        _send_key_name(name, True)
        time.sleep(0.05)
        _send_key_name(name, False)
        time.sleep(0.3)

    def _fl_click_norm(self, nx: float, ny: float) -> None:
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            return
        _l, _t, cw, ch = win32gui.GetClientRect(self.target_hwnd)
        cw, ch = int(cw), int(ch)
        if cw <= 0 or ch <= 0:
            return
        x = max(1, min(cw - 2, int(round(float(nx) * cw))))
        y = max(1, min(ch - 2, int(round(float(ny) * ch))))
        sx, sy = win32gui.ClientToScreen(self.target_hwnd, (x, y))
        _force_foreground_window(self.target_hwnd)
        time.sleep(0.1)
        _send_mouse_move_to(int(sx), int(sy))
        time.sleep(0.06)
        _send_mouse_button("left", True)
        time.sleep(0.07)
        _send_mouse_button("left", False)
        time.sleep(0.2)

    def _fl_wait_enter_keyword(
        self, keyword: str, timeout: float = 12.0
    ) -> bool:
        try:
            from map_labels import compact_text

            wanted_parts = [
                compact_text(part)
                for part in re.split(r"[,，;；\s]+", keyword or "")
                if compact_text(part)
            ]
        except Exception:
            wanted_parts = [keyword.strip()] if keyword.strip() else []
        if not wanted_parts:
            return True
        self._fl_set_state(f"等待识别“{keyword}”…", "warn")
        start = time.monotonic()
        last_text = ""
        while True:
            if not self._fatigue_loop_running:
                return False
            if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
                return False
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                self._set_status(
                    f"没有识别到“{keyword}”（OCR：{last_text or '空'}）", "error"
                )
                return False
            cap, _err = capture_client_bgra(self.target_hwnd)
            if cap is not None:
                w, h, data = cap
                try:
                    items = ocr_screen_items(data, w, h)
                except Exception:
                    items = []
                texts = [str(item.get("text", "")) for item in items]
                blob = "".join(compact_text(t) for t in texts)
                for part in wanted_parts:
                    if part in blob:
                        self._set_status(
                            f"识别到“{keyword}”，开始回放 BOSS 录制。", "ok"
                        )
                        return True
                if texts:
                    last_text = " ".join(texts)[:80]
            self._fl_set_state(
                f"等待“{keyword}”…（OCR：{last_text or '空'}）", "warn"
            )
            time.sleep(0.8)
            # 轮询期间保持界面可响应，方便随时点“停止疲劳循环”。
            QApplication.processEvents()
        return False

    def _fl_click_boss_point_and_wait(self) -> None:
        if not self._fatigue_loop_running:
            return
        self._fatigue_loop_state = "boss_point"
        boss_map = str(self._fatigue_loop_cfg.get("boss_map", "")).strip()
        boss_target = str(self._fatigue_loop_cfg.get("boss_target", "")).strip()
        keyword = str(
            self._fatigue_loop_cfg.get("enter_keyword", "进入副本")
        ).strip() or "进入副本"
        try:
            from map_labels import _valid_map_xy, _valid_rect, load_maps
        except Exception as exc:
            self._stop_fatigue_loop(f"加载地图标注失败：{exc}", "error")
            return
        profiles = load_maps()
        profile = next(
            (
                item
                for item in profiles
                if str(item.get("name", "")) == boss_map
            ),
            None,
        )
        if profile is None:
            profile = next(
                (
                    item
                    for item in profiles
                    if boss_map in str(item.get("name", ""))
                    or str(item.get("name", "")) in boss_map
                ),
                None,
            )
        if profile is None:
            self._stop_fatigue_loop(f"找不到 BOSS 地图「{boss_map}」。", "error")
            return
        target = next(
            (
                item
                for item in profile.get("targets", [])
                if isinstance(item, dict)
                and str(item.get("name", "")) == boss_target
            ),
            None,
        )
        if target is None:
            self._stop_fatigue_loop(
                f"BOSS 地图「{boss_map}」里没有「{boss_target}」。", "error"
            )
            return

        if not self._fl_map_open:
            self._fl_tap_key(self._macro_cfg.get("map_key", "m"))
            time.sleep(0.9)
            self._fl_map_open = True
        rect = _valid_rect(profile.get("map_rect_norm"))
        mx, my = _valid_map_xy(target)
        if rect is not None and mx is not None and my is not None:
            x0, y0, x1, y1 = rect
            nx = x0 + mx * (x1 - x0)
            ny = y0 + my * (y1 - y0)
        else:
            nx = float(target.get("nx", 0.5))
            ny = float(target.get("ny", 0.5))
        nx = max(0.0, min(1.0, float(nx)))
        ny = max(0.0, min(1.0, float(ny)))
        self._fl_set_state(f"点击 BOSS 点「{boss_target}」…", "warn")
        self._fl_click_norm(nx, ny)
        time.sleep(0.8)
        # 点完 BOSS 点后地图通常会关闭/弹进入确认框，下一次重新打开地图。
        self._fl_map_open = False
        if not self._fl_wait_enter_keyword(keyword, timeout=12.0):
            self._stop_fatigue_loop(
                f"点击 BOSS 点后没有识别到“{keyword}”，已停止。", "error"
            )
            return
        self._fl_play_record()

    def _fl_play_record(self) -> None:
        if not self._fatigue_loop_running or self._fatigue_loop_player is not None:
            return
        record_name = str(self._fatigue_loop_cfg.get("record", "")).strip()
        record = next(
            (
                item
                for item in self._dungeon_records
                if str(item.get("name", "")) == record_name
            ),
            None,
        )
        if record is None or not record.get("events"):
            self._stop_fatigue_loop(
                f"BOSS 录制「{record_name}」不存在或为空。", "error"
            )
            return
        meta = record.get("meta")
        player = MacroPlayer(
            self.target_hwnd,
            "疲劳循环BOSS",
            list(record.get("events", [])),
            meta=meta if isinstance(meta, dict) else None,
            speed=1.0,
            lock_user_input=bool(self.chk_lock_input.isChecked()),
            background=bool(self.chk_background_replay.isChecked()),
            parent=self,
        )
        player.done.connect(self._on_fl_player_done)
        player.error.connect(self._on_fl_player_error)
        self._fatigue_loop_player = player
        self.lbl_fl_state.setText(f"回放 BOSS 录制「{record_name}」…")
        player.start()

    def _on_fl_player_done(self, stage: str) -> None:
        player = self.sender()
        if isinstance(player, MacroPlayer):
            player.wait(500)
            player.deleteLater()
        if player is not self._fatigue_loop_player:
            return
        self._fatigue_loop_player = None
        if not self._fatigue_loop_running:
            return
        self._fatigue_loop_state = "farming"
        interval = max(
            5.0, float(self._fatigue_loop_cfg.get("interval", 30))
        )
        self._fl_set_state("BOSS 录制完成，恢复打怪…", "ok")
        self._fl_start_farming()
        self._fl_schedule_check(interval)

    def _on_fl_player_error(self, stage: str, message: str) -> None:
        player = self.sender()
        if isinstance(player, MacroPlayer):
            player.wait(500)
            player.deleteLater()
        if player is not self._fatigue_loop_player:
            return
        self._fatigue_loop_player = None
        if not self._fatigue_loop_running:
            return
        self._stop_fatigue_loop(f"BOSS 录制回放失败：{message}", "error")
        try:
            self._popup.show_message("BOSS 录制回放失败", message, seconds=15)
        except Exception:
            pass

    def _stop_fatigue_loop(
        self, message: str = "疲劳循环已停止。", level: str = "warn"
    ) -> None:
        was_running = self._fatigue_loop_running
        self._fatigue_loop_running = False
        self._fatigue_loop_state = "idle"
        if self._fatigue_loop_timer is not None:
            self._fatigue_loop_timer.stop()
        monitor = self._fatigue_loop_monitor
        self._fatigue_loop_monitor = None
        if monitor is not None:
            monitor.stop()
            if not monitor.wait(3000):
                monitor.terminate()
                monitor.wait(500)
            monitor.deleteLater()
        self._fl_stop_farming()
        runner = self._fatigue_loop_path_runner
        self._fatigue_loop_path_runner = None
        if runner is not None:
            runner.stop()
            if not runner.wait(4000):
                runner.terminate()
                runner.wait(500)
            runner.deleteLater()
        player = self._fatigue_loop_player
        self._fatigue_loop_player = None
        if player is not None:
            player.stop()
            if not player.wait(3000):
                player.terminate()
                player.wait(500)
            player.deleteLater()
        self._fl_last_fatigue_text = ""
        if was_running:
            self.lbl_fl_state.setText("已停止")
            self._set_status(message, level)
        self._update_fatigue_loop_controls()
        self._update_macro_button_states()
        self._update_toggle_action()

    def _open_map_learner(self) -> None:
        if self._macro_busy():
            self._notify("当前正忙", "请先停止地图测试/路径执行。")
            return
        if self._worker is not None or self._test_monitor is not None:
            self._notify("当前正忙", "请先停止基础挂机/疲劳测试。")
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return
        try:
            from map_labels import MapLabelDialog
        except Exception as exc:
            self._notify("地图标注不可用", f"加载失败：{exc}")
            return
        dialog = MapLabelDialog(self.target_hwnd, parent=self)
        dialog.exec()
        self._refresh_map_profiles()
        if getattr(dialog, "saved", False):
            self._set_status("地图标注已保存，可以测试前往目标。", "ok")

    def _toggle_map_test(self) -> None:
        if self._map_runner is not None:
            self._stop_map_runner()
            return
        self._start_map_test()

    def _start_map_test(self) -> None:
        if self._quitting or self._map_runner is not None:
            return
        if self._macro_busy() or self._worker is not None:
            self._notify("当前正忙", "请先停止其它自动化。")
            return
        if self.target_hwnd is None or not self._window_alive(self.target_hwnd):
            self._notify("尚未绑定窗口", "请先绑定 CreatureCurios 窗口。")
            return
        profile = self.combo_map_profile.currentData()
        target = self.combo_map_target.currentData()
        if not isinstance(profile, dict) or not isinstance(target, dict):
            self._notify("没有地图目标", "请先学习地图标注，并选择目标。")
            return
        try:
            from map_labels import MapTravelRunner
        except Exception as exc:
            self._notify("地图寻路不可用", f"加载失败：{exc}")
            return
        runner = MapTravelRunner(
            self.target_hwnd,
            profile,
            target,
            map_key=self._macro_cfg.get("map_key", "m"),
            close_key=self._macro_cfg.get("close_map_key", "esc"),
            travel_wait=float(self.spin_map_travel_wait.value()),
            parent=self,
        )
        runner.status.connect(self._on_map_status)
        runner.done.connect(self._on_map_done)
        runner.failed.connect(self._on_map_failed)
        self._map_runner = runner
        self.lbl_map_state.setText("地图测试：准备中…")
        self._set_status("地图寻路测试开始：按地图键 → OCR 找标注 → 点击 → 关闭地图。", "warn")
        self._update_macro_button_states()
        self._update_map_controls_state()
        runner.start()

    def _on_map_status(self, text: str, level: str) -> None:
        if hasattr(self, "lbl_map_state"):
            self.lbl_map_state.setText(text)
        self._set_status(text, level)

    def _on_map_done(self) -> None:
        runner = self.sender()
        if runner is self._map_runner:
            self._map_runner = None
            if isinstance(runner, QThread):
                runner.wait(500)
                runner.deleteLater()
        self.lbl_map_state.setText("地图测试完成，已等待自动寻路")
        self._set_status("地图测试完成。", "ok")
        self._update_macro_button_states()
        self._update_map_controls_state()

    def _on_map_failed(self, message: str) -> None:
        runner = self.sender()
        if runner is self._map_runner:
            self._map_runner = None
            if isinstance(runner, QThread):
                runner.wait(500)
                runner.deleteLater()
        stopped = str(message).strip() == "已停止。"
        self.lbl_map_state.setText("已停止" if stopped else "地图测试失败")
        self._set_status(
            "地图测试已停止。" if stopped else f"地图测试失败：{message}",
            "warn" if stopped else "error",
        )
        if not stopped:
            try:
                self._popup.show_message("地图寻路失败", message, seconds=15)
            except Exception:
                pass
        self._update_macro_button_states()
        self._update_map_controls_state()

    def _stop_map_runner(self) -> None:
        runner = self._map_runner
        self._map_runner = None
        if runner is not None:
            runner.stop()
            if not runner.wait(4000):
                runner.terminate()
                runner.wait(500)
            runner.deleteLater()
        self.lbl_map_state.setText("已停止")
        self._set_status("地图测试已停止。", "warn")
        self._update_macro_button_states()
        self._update_map_controls_state()

    def _open_route_editor(self) -> None:
        if self._quitting:
            return
        if self._macro_busy():
            self._notify("当前正忙", "请先停止地图测试/路径执行。")
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
        self._update_fatigue_loop_controls()
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
        recorder = self._dungeon_recorder
        if recorder is not None:
            try:
                self.lbl_dungeon_state.setText(
                    f"录制中… {recorder.event_count()} 个动作 / "
                    f"{recorder.duration():.1f} 秒"
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

    # =====================================================================
    # BOOS 挂机：地图按键 / 路线编辑 / 地图寻路 / 路径执行
    # =====================================================================

    def _single_window_busy(self) -> bool:
        return bool(
            self._worker is not None
            or self._test_monitor is not None
            or self._route_editor is not None
            or self._map_runner is not None
            or self._path_runner is not None
            or self._dungeon_recorder is not None
            or self._dungeon_recording_pending
            or self._dungeon_running
            or self._dungeon_player is not None
            or self._combo_running
            or self._combo_path_runner is not None
            or self._combo_player is not None
            or self._fatigue_loop_running
            or self._fatigue_loop_worker is not None
            or self._fatigue_loop_monitor is not None
            or self._fatigue_loop_player is not None
        )

    def _any_multi_running(self) -> bool:
        return any(
            runner is not None and getattr(runner, "is_running", False)
            for runner in self._multi_runners
        )

    def _macro_busy(self) -> bool:
        return self._single_window_busy() or self._any_multi_running()

    def _save_macro_settings(self) -> None:
        if not hasattr(self, "edit_map_key"):
            return
        map_key = self.edit_map_key.text().strip().lower() or "m"
        close_key = self.edit_close_key.text().strip().lower() or "esc"
        mount_key = self.edit_mount_key.text().strip().lower()
        self.edit_map_key.setText(map_key)
        self.edit_close_key.setText(close_key)
        self.edit_mount_key.setText(mount_key)
        self._macro_cfg["map_key"] = map_key
        self._macro_cfg["close_map_key"] = close_key
        self._macro_cfg["mount_key"] = mount_key
        save_macro_config(self._macro_cfg)

    def _update_macro_button_states(self) -> None:
        """自动化运行状态变化时，刷新剩余控件的可用性。"""
        if hasattr(self, "btn_route_editor"):
            self.btn_route_editor.setEnabled(not self._macro_busy())
        self._update_map_controls_state()
        self._update_path_controls_state()
        self._update_dungeon_controls()
        self._update_combo_controls()
        self._update_fatigue_loop_controls()
        self._update_multi_controls()
        self._update_bind_button_state()

    def _set_running_controls(self, running: bool) -> None:
        self.combo_windows.setEnabled(not running)
        self.btn_refresh.setEnabled(not running)
        self._update_bind_button_state()
        self._update_toggle_action()

    def _update_bind_button_state(self) -> None:
        running = self._worker is not None or self._macro_busy()
        has_selection = self.combo_windows.currentData() is not None
        self.btn_bind.setEnabled((not running) and has_selection)
        self._update_map_controls_state()
        self._update_path_controls_state()
        self._update_dungeon_controls()
        self._update_combo_controls()
        self._update_fatigue_loop_controls()

    def _update_toggle_action(self) -> None:
        if self._act_toggle is None:
            return

        if self._stopping:
            self._act_toggle.setText("正在停止…")
            self._act_toggle.setEnabled(False)
        elif self._fatigue_loop_running:
            self._act_toggle.setText("停止疲劳循环")
            self._act_toggle.setEnabled(True)
        elif self._combo_running:
            self._act_toggle.setText("停止组合挂机")
            self._act_toggle.setEnabled(True)
        elif self._dungeon_recorder is not None or self._dungeon_recording_pending:
            self._act_toggle.setText("停止录制副本")
            self._act_toggle.setEnabled(True)
        elif self._dungeon_running:
            self._act_toggle.setText("停止副本")
            self._act_toggle.setEnabled(True)
        elif self._map_runner is not None:
            self._act_toggle.setText("停止地图测试")
            self._act_toggle.setEnabled(True)
        elif self._path_runner is not None:
            self._act_toggle.setText("停止路径执行")
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

        self._dungeon_recording_pending = False
        self._dungeon_running = False
        if self._dungeon_pending_timer is not None:
            try:
                self._dungeon_pending_timer.stop()
            except Exception:
                pass
            self._dungeon_pending_timer = None
        recorder = self._dungeon_recorder
        self._dungeon_recorder = None
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:
                pass
        player = self._dungeon_player
        self._dungeon_player = None
        if player is not None:
            try:
                player.stop()
                if not player.wait(3000):
                    player.terminate()
                    player.wait(500)
            except Exception:
                pass

        self._combo_running = False
        if self._combo_pending_timer is not None:
            try:
                self._combo_pending_timer.stop()
            except Exception:
                pass
            self._combo_pending_timer = None
        runner = self._combo_path_runner
        self._combo_path_runner = None
        if runner is not None:
            try:
                runner.stop()
                if not runner.wait(4000):
                    runner.terminate()
                    runner.wait(500)
            except Exception:
                pass
        player = self._combo_player
        self._combo_player = None
        if player is not None:
            try:
                player.stop()
                if not player.wait(3000):
                    player.terminate()
                    player.wait(500)
            except Exception:
                pass

        self._fatigue_loop_running = False
        self._fatigue_loop_state = "idle"
        if self._fatigue_loop_timer is not None:
            try:
                self._fatigue_loop_timer.stop()
            except Exception:
                pass
            self._fatigue_loop_timer = None
        monitor = self._fatigue_loop_monitor
        self._fatigue_loop_monitor = None
        if monitor is not None:
            try:
                monitor.stop()
                if not monitor.wait(3000):
                    monitor.terminate()
                    monitor.wait(500)
            except Exception:
                pass
        worker = self._fatigue_loop_worker
        self._fatigue_loop_worker = None
        if worker is not None:
            try:
                worker.stop()
                if not worker.wait(2500):
                    worker.terminate()
                    worker.wait(500)
            except Exception:
                pass
        runner = self._fatigue_loop_path_runner
        self._fatigue_loop_path_runner = None
        if runner is not None:
            try:
                runner.stop()
                if not runner.wait(4000):
                    runner.terminate()
                    runner.wait(500)
            except Exception:
                pass
        player = self._fatigue_loop_player
        self._fatigue_loop_player = None
        if player is not None:
            try:
                player.stop()
                if not player.wait(3000):
                    player.terminate()
                    player.wait(500)
            except Exception:
                pass

        for index in range(4):
            runner = self._multi_runners[index]
            self._multi_runners[index] = None
            if runner is not None:
                try:
                    runner.stop("已停止", "warn")
                except Exception:
                    pass
                try:
                    runner.deleteLater()
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
