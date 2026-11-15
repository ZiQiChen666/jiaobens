#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Raw Input 鼠标位移采集器。

用一个隐藏窗口接收 WM_INPUT，读取 RAWMOUSE.lLastX/lLastY（相对位移），
不受 Windows 鼠标加速影响；用于录制游戏镜头/走位。

只读取原始输入，不注入、不读取游戏内存。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
from typing import Callable, Optional

WM_INPUT = 0x00FF
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
MOUSE_MOVE_ABSOLUTE = 0x01
HWND_MESSAGE = -3

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wt.USHORT),
        ("usUsage", wt.USHORT),
        ("dwFlags", wt.DWORD),
        ("hwndTarget", wt.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wt.DWORD),
        ("dwSize", wt.DWORD),
        ("hDevice", wt.HANDLE),
        ("wParam", wt.WPARAM),
    ]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [
        ("usFlags", wt.USHORT),
        ("ulButtons", wt.ULONG),
        ("usButtonFlags", wt.USHORT),
        ("usButtonData", wt.USHORT),
        ("ulRawButtons", wt.ULONG),
        ("lLastX", wt.LONG),
        ("lLastY", wt.LONG),
        ("ulExtraInformation", wt.ULONG),
    ]


class RAWINPUT(ctypes.Structure):
    _fields_ = [
        ("header", RAWINPUTHEADER),
        ("mouse", RAWMOUSE),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM
)


class _WNDCLASSW(ctypes.Structure):
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


user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT
]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.RegisterRawInputDevices.argtypes = [
    ctypes.POINTER(RAWINPUTDEVICE), wt.UINT, wt.UINT
]
user32.RegisterRawInputDevices.restype = wt.BOOL
user32.GetRawInputData.argtypes = [
    ctypes.c_void_p, wt.UINT, ctypes.c_void_p,
    ctypes.POINTER(wt.UINT), wt.UINT,
]
user32.GetRawInputData.restype = wt.UINT

_ACTIVE: Optional["RawMouseRecorder"] = None
_WNDPROC_KEEP = None


def _wnd_proc(hwnd, msg, wparam, lparam):
    active = _ACTIVE
    try:
        if msg == WM_INPUT and active is not None:
            active._handle_input(lparam)
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


class RawMouseRecorder:
    """启动一个隐藏窗口线程，持续回调 (dx, dy)。"""

    CLASS_NAME = "CCRawInputSink"

    def __init__(self, callback: Callable[[int, int], None]) -> None:
        self.callback = callback
        self.hwnd = 0
        self.is_active = False
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        if self._thread is not None:
            return self.is_active
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="RawMouseRecorder", daemon=True
        )
        self._thread.start()
        self._ready.wait(2.0)
        return self.is_active

    def stop(self) -> None:
        thread = self._thread
        self._thread = None
        global _ACTIVE
        if self.hwnd:
            try:
                user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        if thread is not None:
            try:
                thread.join(1.5)
            except Exception:
                pass
        _ACTIVE = None
        self.is_active = False

    def _run(self) -> None:
        global _ACTIVE, _WNDPROC_KEEP
        try:
            hinst = kernel32.GetModuleHandleW(None)
            _WNDPROC_KEEP = WNDPROC(_wnd_proc)
            wc = _WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = _WNDPROC_KEEP
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = hinst
            wc.hIcon = None
            wc.hCursor = None
            wc.hbrBackground = None
            wc.lpszMenuName = None
            wc.lpszClassName = self.CLASS_NAME
            user32.RegisterClassW(ctypes.byref(wc))
            hwnd = user32.CreateWindowExW(
                0,
                self.CLASS_NAME,
                self.CLASS_NAME,
                0x80000000,  # WS_POPUP，隐藏
                0,
                0,
                1,
                1,
                None,
                None,
                hinst,
                None,
            )
            if not hwnd:
                self._ready.set()
                return
            self.hwnd = int(hwnd)
            user32.ShowWindow(hwnd, 0)  # SW_HIDE
            rid = RAWINPUTDEVICE()
            rid.usUsagePage = 0x01
            rid.usUsage = 0x02  # mouse
            rid.dwFlags = RIDEV_INPUTSINK
            rid.hwndTarget = hwnd
            ok = user32.RegisterRawInputDevices(
                ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)
            )
            self.is_active = bool(ok)
            _ACTIVE = self
        except Exception:
            self.is_active = False
        finally:
            self._ready.set()
        if not self.is_active or not self.hwnd:
            return
        msg = wt.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass
        finally:
            self.is_active = False
            _ACTIVE = None

    def _handle_input(self, lparam: int) -> None:
        try:
            size = wt.UINT(0)
            header_size = ctypes.sizeof(RAWINPUTHEADER)
            user32.GetRawInputData(
                ctypes.c_void_p(int(lparam)),
                RID_INPUT,
                None,
                ctypes.byref(size),
                header_size,
            )
            if size.value == 0:
                return
            buf = ctypes.create_string_buffer(size.value)
            got = user32.GetRawInputData(
                ctypes.c_void_p(int(lparam)),
                RID_INPUT,
                buf,
                ctypes.byref(size),
                header_size,
            )
            if got == 0 or got == 0xFFFFFFFF:
                return
            raw = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
            if int(raw.header.dwType) != RIM_TYPEMOUSE:
                return
            if int(raw.mouse.usFlags) & MOUSE_MOVE_ABSOLUTE:
                return
            dx = int(raw.mouse.lLastX)
            dy = int(raw.mouse.lLastY)
            if dx == 0 and dy == 0:
                return
            self.callback(dx, dy)
        except Exception:
            pass
