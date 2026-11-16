# -*- coding: utf-8 -*-
"""验证：相对回放期间临时关掉鼠标加速/把指针速度调到 1:1，结束后恢复。

为什么：Windows 的“提高指针精确度”+ 指针速度会把 SendInput 的相对位移
放大（这台机器实测 3.2 倍），而录制抓的是 Raw Input 原始位移，
两边不一致 → 回放镜头/走位就偏。

跑法：python verify_mouse_accel.py
（会短暂移动光标，测试完恢复原位置/原设置）
"""
import statistics
import sys
import time

import win32api

import creature_curios_keyer as m

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def sample_ratio(dx=100, want=3):
    """多次采样取中位数；期间光标自己动了（有人在用）就丢弃重来。"""
    vals = []
    for _ in range(want * 4):
        if len(vals) >= want:
            break
        win32api.SetCursorPos((600, 600))
        time.sleep(0.12)
        if abs(win32api.GetCursorPos()[0] - 600) > 3:
            continue          # 有人动鼠标，跳过
        before = 600
        m._send_mouse_input(m._MOUSEEVENTF_MOVE, dx, 0)
        time.sleep(0.12)
        after = win32api.GetCursorPos()[0]
        ratio = (after - before) / float(dx)
        if -0.2 <= ratio <= 6:   # 明显被干扰的样本丢掉
            vals.append(ratio)
        time.sleep(0.05)
    return statistics.median(vals) if vals else None


saved_cursor = win32api.GetCursorPos()
saved_accel = m._get_mouse_accel()
saved_speed = m._get_mouse_speed()
print(f"当前设置：加速={saved_accel} 指针速度={saved_speed}（第3位=1 表示“提高指针精确度”开着）")

try:
    # 1) 原始倍率（加速开着时会被放大）
    raw = sample_ratio(100)
    if raw is not None:
        print(f"不处理时：发 100px → 实际 {raw * 100:.0f}px（{raw:.2f} 倍）")
        if saved_accel and saved_accel[2] == 1:
            check("加速开着时位移确实被放大（这就是回放不准的根因）", raw > 1.4,
                  f"{raw:.2f}x")

    # 2) guard 期间：系统设置必须变成“无加速 + 速度10”，并尽量实测 1:1
    guard = m.MouseAccelGuard(debug=lambda t: print("  [guard]", t))
    guard.disable()
    try:
        accel_now = m._get_mouse_accel()
        speed_now = m._get_mouse_speed()
        print(f"回放期间设置：加速={accel_now} 指针速度={speed_now}")
        check("回放期间加速已关闭", bool(accel_now) and accel_now[2] == 0,
              str(accel_now))
        check("回放期间指针速度已调到 1:1(10)", speed_now == 10, str(speed_now))
        for dx in (20, 100):
            got = sample_ratio(dx, want=2)
            if got is None:
                print(f"SKIP 实测 dx={dx}：你正在用鼠标（采样全被干扰），"
                      "以系统设置断言为准")
                continue
            print(f"回放期间实测 dx={dx}: {got:.3f} 倍")
            check(f"回放期间 dx={dx} 是 1:1（不被放大）",
                  abs(got - 1.0) <= 0.15, f"{got:.3f}x")
    finally:
        guard.restore()

    # 3) 结束后恢复原设置
    check("加速参数已恢复", m._get_mouse_accel() == saved_accel,
          f"{saved_accel} -> {m._get_mouse_accel()}")
    check("指针速度已恢复", m._get_mouse_speed() == saved_speed,
          f"{saved_speed} -> {m._get_mouse_speed()}")
finally:
    try:
        win32api.SetCursorPos(saved_cursor)
    except Exception:
        pass

print()
if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("PASS: 回放期间相对位移 1:1，结束自动恢复系统设置")
