#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回放期间锁定用户的物理鼠标/键盘。

只拦截非注入事件，脚本自己用 SendInput 发出的键鼠事件仍然可以通过。
保留 Ctrl / Alt / F8，方便用全局热键 Ctrl+Alt+F8 停止回放。
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_QUIT = 0x0012
HC_ACTION = 0
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01

VK_CONTROL = 0x11
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_MENU = 0x12
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_F8 = 0x77
ALLOWED_KEYS = {
    VK_CONTROL,
    VK_LCONTROL,
    VK_RCONTROL,
    VK_MENU,
    VK_LMENU,
    VK_RMENU,
    VK_F8,
}

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(
    LRESULT, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t
)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int,
    HOOKPROC,
    wintypes.HINSTANCE,
    wintypes.DWORD,
]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [
    wintypes.HHOOK,
    ctypes.c_int,
    ctypes.c_size_t,
    ctypes.c_ssize_t,
]
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


class InputLock:
    def __init__(self) -> None:
        self._thread = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._active = False
        self._kb_hook = None
        self._ms_hook = None
        self._kb_proc = None
        self._ms_proc = None

    def start(self, timeout: float = 2.0) -> bool:
        if self._active:
            return True
        self._thread = threading.Thread(
            target=self._run, name="InputLock", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout)
        return bool(self._active)

    def stop(self) -> None:
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        try:
            if self._thread_id:
                user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        except Exception:
            pass
        try:
            thread.join(2.0)
        except Exception:
            pass
        self._active = False

    # ------------------------------------------------------------------
    def _run(self) -> None:
        self._thread_id = int(kernel32.GetCurrentThreadId())
        try:
            self._kb_proc = HOOKPROC(self._kb_callback)
            self._ms_proc = HOOKPROC(self._ms_callback)
            module = kernel32.GetModuleHandleW(None)
            self._kb_hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._kb_proc, module, 0
            )
            self._ms_hook = user32.SetWindowsHookExW(
                WH_MOUSE_LL, self._ms_proc, module, 0
            )
            self._active = bool(self._kb_hook and self._ms_hook)
        except Exception:
            self._active = False
        finally:
            self._ready.set()
        if not self._active:
            self._unhook()
            return
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass
        finally:
            self._active = False
            self._unhook()

    def _unhook(self) -> None:
        for attr in ("_kb_hook", "_ms_hook"):
            hook = getattr(self, attr)
            setattr(self, attr, None)
            if hook:
                try:
                    user32.UnhookWindowsHookEx(hook)
                except Exception:
                    pass

    def _kb_callback(self, n_code, w_param, l_param):
        if n_code == HC_ACTION:
            try:
                info = ctypes.cast(
                    l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)
                ).contents
                if info.flags & LLKHF_INJECTED:
                    return user32.CallNextHookEx(
                        None, n_code, w_param, l_param
                    )
                if int(info.vkCode) in ALLOWED_KEYS:
                    return user32.CallNextHookEx(
                        None, n_code, w_param, l_param
                    )
                return 1
            except Exception:
                return 1
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _ms_callback(self, n_code, w_param, l_param):
        if n_code == HC_ACTION:
            try:
                info = ctypes.cast(
                    l_param, ctypes.POINTER(MSLLHOOKSTRUCT)
                ).contents
                if info.flags & LLMHF_INJECTED:
                    return user32.CallNextHookEx(
                        None, n_code, w_param, l_param
                    )
                return 1
            except Exception:
                return 1
        return user32.CallNextHookEx(None, n_code, w_param, l_param)
