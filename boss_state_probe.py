#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BOSS 状态采集器（只截图 + OCR，不注入任何输入）.

用途：
    手动打一次 BOSS，让本工具记录每个阶段游戏窗口里的文字和截图，
    之后据此设计“状态驱动”的稳定版 BOSS 流程。

热键：
    F9  开始 / 暂停采集（有提示音）
    F10 立即抓一张（有提示音）

输出：
    boss_states/<时间戳>/
        ocr.jsonl      每次采集的完整 OCR 结果
        summary.md     人可读的时间线
        shots/*.png    关键截图
        session.json   会话信息
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
from typing import Optional

try:
    import keyboard
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        f"缺少 keyboard 库：{exc}\n请先执行：pip install keyboard"
    ) from exc

try:
    import numpy as np
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        f"缺少 OCR 依赖：{exc}\n请先执行：pip install rapidocr_onnxruntime Pillow numpy"
    ) from exc

import win32api
import win32gui

# 复用主程序的 PrintWindow 客户区截图（不启动 Qt 界面）
from creature_curios_keyer import capture_client_bgra

try:
    import winsound
except Exception:  # pragma: no cover
    winsound = None


GAME_TITLE_KEYWORD = "CreatureCurios"
CAPTURE_INTERVAL = 2.5       # 秒
AUTO_SAVE_INTERVAL = 12.0    # 没有文字变化时，至少多久存一张图
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_ROOT = os.path.join(ROOT_DIR, "boss_states")


def beep(freq: int = 900, ms: int = 90) -> None:
    if winsound is None:
        return
    try:
        winsound.Beep(freq, ms)
    except Exception:
        pass


def now_text() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def find_game_window() -> Optional[int]:
    """只找 Unity 游戏窗口，排除本项目自己的 Qt 窗口。"""
    found: list[tuple[int, str, str]] = []

    def callback(hwnd: int, _lparam) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd).strip()
            if GAME_TITLE_KEYWORD.lower() not in title.lower():
                return True
            cls = win32gui.GetClassName(hwnd)
            found.append((int(hwnd), title, cls))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        return None

    # Unity 窗口优先
    for hwnd, _title, cls in found:
        if "unity" in cls.lower():
            return hwnd
    return found[0][0] if found else None


def client_rect_on_screen(hwnd: int) -> tuple[int, int, int, int]:
    """返回客户区在屏幕上的 rect: left, top, right, bottom."""
    left, top = win32gui.ClientToScreen(hwnd, (0, 0))
    _l, _t, r, b = win32gui.GetClientRect(hwnd)
    return (int(left), int(top), int(left + r), int(top + b))


def ocr_image(image: Image.Image) -> list[dict]:
    arr = np.array(image)[:, :, ::-1]  # RGB -> BGR
    ocr = ocr_image.engine
    result, _elapse = ocr(arr)
    items: list[dict] = []
    for item in (result or []):
        if len(item) < 3:
            continue
        box, text, score = item[0], item[1], item[2]
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append(
            {
                "x0": int(min(xs)),
                "y0": int(min(ys)),
                "x1": int(max(xs)),
                "y1": int(max(ys)),
                "text": str(text),
                "score": float(score),
            }
        )
    return items


ocr_image.engine = None  # type: ignore[attr-defined]


def main() -> int:
    os.makedirs(STATE_ROOT, exist_ok=True)
    session_name = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = os.path.join(STATE_ROOT, session_name)
    shots_dir = os.path.join(session_dir, "shots")
    os.makedirs(shots_dir, exist_ok=True)
    ocr_path = os.path.join(session_dir, "ocr.jsonl")
    summary_path = os.path.join(session_dir, "summary.md")

    with open(os.path.join(ROOT_DIR, "last_probe_session.txt"), "w", encoding="utf-8") as f:
        f.write(session_dir)

    print("=" * 60)
    print("BOSS 状态采集器")
    print("  F9  开始 / 暂停采集")
    print("  F10 立即抓取一张")
    print("  只截图 + OCR，不会点击游戏、不会发送按键。")
    print("=" * 60)
    print(f"输出目录：{session_dir}")
    print("正在查找游戏窗口 …")

    hwnd: Optional[int] = None
    while hwnd is None or not win32gui.IsWindow(hwnd):
        hwnd = find_game_window()
        if hwnd is not None:
            break
        time.sleep(1.0)

    title = win32gui.GetWindowText(hwnd).strip()
    class_name = win32gui.GetClassName(hwnd)
    print(f"已找到游戏窗口：{title!r} class={class_name!r} hwnd=0x{hwnd:X}")
    print("首次加载 OCR 模型需要几秒 …")
    ocr_image.engine = RapidOCR()  # type: ignore[attr-defined]
    print("OCR 就绪。3 秒后自动开始采集，请切到游戏。")
    beep(800, 80)
    time.sleep(3.0)

    state = {
        "active": True,
        "stop": False,
        "manual": 0,
        "last_save": 0.0,
        "last_text": "",
        "count": 0,
        "saved": 0,
    }
    start_wall = now_text()

    def toggle_active() -> None:
        state["active"] = not state["active"]
        print(f"[{now_text()}] 采集{'开始' if state['active'] else '暂停'}")
        beep(1000 if state["active"] else 500, 100)

    def manual_capture() -> None:
        state["manual"] += 1
        print(f"[{now_text()}] 手动抓取")
        beep(1200, 90)

    hotkey_handles = []
    try:
        hotkey_handles.append(keyboard.add_hotkey("f9", toggle_active, suppress=False))
        hotkey_handles.append(keyboard.add_hotkey("f10", manual_capture, suppress=False))
    except Exception as exc:
        print(f"[警告] 全局热键注册失败：{exc}")
        print("       仍会自动采集；可以用 Ctrl+C 结束。")

    summary_lines = [
        "# BOSS 状态采集",
        "",
        f"- 开始：{start_wall}",
        f"- 游戏窗口：`{title}` / `{class_name}` / `0x{hwnd:X}`",
        f"- 截图间隔：{CAPTURE_INTERVAL}s",
        "",
        "| 时间 | 类型 | 文字 | 截图 |",
        "| --- | --- | --- | --- |",
    ]

    def append_jsonl(record: dict) -> None:
        with open(ocr_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    try:
        while not state["stop"]:
            if not state["active"]:
                time.sleep(0.2)
                continue
            if not win32gui.IsWindow(hwnd):
                print("[停止] 游戏窗口已关闭。")
                break

            loop_start = time.monotonic()
            manual_now = int(state["manual"])
            if manual_now:
                state["manual"] = 0

            if win32gui.IsIconic(hwnd):
                time.sleep(1.0)
                continue

            cap, err = capture_client_bgra(hwnd)
            if cap is None:
                print(f"[{now_text()}] 截图失败：{err}")
                time.sleep(1.0)
                continue
            w, h, data = cap
            image = Image.frombytes("RGBA", (w, h), data, "raw", "BGRA").convert("RGB")
            state["count"] += 1

            try:
                items = ocr_image(image)
            except Exception as exc:
                items = []
                print(f"[{now_text()}] OCR 失败：{type(exc).__name__}: {exc}")

            texts = [item["text"] for item in items]
            joined = " | ".join(texts)
            compact = re.sub(r"\s+", "", joined)
            foreground = False
            try:
                fg = int(win32gui.GetForegroundWindow() or 0)
                foreground = int(win32gui.GetAncestor(fg, 2)) == hwnd
            except Exception:
                pass

            rect = client_rect_on_screen(hwnd)
            ts = datetime.datetime.now().strftime("%H%M%S")
            save_shot = bool(manual_now)
            if not save_shot and compact and compact != state["last_text"]:
                save_shot = True
            if not save_shot and time.monotonic() - state["last_save"] >= AUTO_SAVE_INTERVAL:
                save_shot = True

            shot_rel = ""
            if save_shot:
                suffix = "manual" if manual_now else "change"
                shot_name = f"{ts}_{state['count']:05d}_{suffix}.png"
                shot_rel = os.path.join("shots", shot_name)
                try:
                    image.save(os.path.join(session_dir, shot_rel))
                    state["saved"] += 1
                    state["last_save"] = time.monotonic()
                except Exception as exc:
                    print(f"[{now_text()}] 保存截图失败：{exc}")
                    shot_rel = ""

            if compact != state["last_text"]:
                state["last_text"] = compact

            record = {
                "wall": now_text(),
                "t": round(time.monotonic(), 3),
                "client_size": [w, h],
                "client_rect": list(rect),
                "foreground": foreground,
                "manual": bool(manual_now),
                "shot": shot_rel,
                "texts": texts,
                "items": items,
            }
            append_jsonl(record)

            preview = joined[:160].replace("|", "/")
            print(
                f"[{now_text()}] #{state['count']} "
                f"{'前台' if foreground else '后台'} "
                f"{'截图 ' + shot_rel if shot_rel else '仅OCR'} | {preview}"
            )
            if shot_rel:
                summary_lines.append(
                    f"| {now_text()} | {'手动' if manual_now else '变化'} | "
                    f"{preview.replace('|', '/')} | `{shot_rel}` |"
                )
                with open(summary_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(summary_lines) + "\n")

            elapsed = time.monotonic() - loop_start
            time.sleep(max(0.2, CAPTURE_INTERVAL - elapsed))
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，结束采集。")
    finally:
        for handle in hotkey_handles:
            try:
                keyboard.remove_hotkey(handle)
            except Exception:
                pass
        session_info = {
            "started_at": start_wall,
            "ended_at": now_text(),
            "hwnd": int(hwnd),
            "title": title,
            "class_name": class_name,
            "count": state["count"],
            "saved_shots": state["saved"],
            "interval": CAPTURE_INTERVAL,
        }
        try:
            with open(os.path.join(session_dir, "session.json"), "w", encoding="utf-8") as f:
                json.dump(session_info, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        summary_lines.extend(
            [
                "",
                f"- 结束：{now_text()}",
                f"- 采集次数：{state['count']}",
                f"- 保存截图：{state['saved']}",
            ]
        )
        try:
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write("\n".join(summary_lines) + "\n")
        except Exception:
            pass
        print(f"\n采集结束。输出目录：{session_dir}")
        print("把 last_probe_session.txt 或上面的目录发给我即可。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
