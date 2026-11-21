#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""副本流程引擎：地图1 进本 → 地图2 BOSS 点挂机 → 退出副本

一轮流程（就 3 步，没有乱七八糟的分支）：

    ① 进本：开地图1 → 点「副本入口」点位 → 等自动寻路 →
       检测「进本弹窗」出现 → 点「进入游戏」按钮
    ② 挂机：开地图2 → 点「BOSS 点」点位 → 等自动寻路 →
       挂机 N 秒（可每个多开单独设）：平A + 技能 + 血量低喝药(默认 6s 间隔) + 定时拾取
    ③ 退出：点「退出副本」→ 点「确认」→ 回地图1

特点：
    * 全程 PostMessage 后台点击/按键，不移动真实鼠标、不抢前台；
    * 弹窗识别用「锚点相似度」，不需要 OCR、不吃 CPU，多开也不卡；
    * 血量用 OCR（只在配了血量区域时启用）；
    * 失败/超时会存截图快照，便于回溯。

用法：
    python dungeon_flow.py --list
    python dungeon_flow.py --calibrate --window CreatureCurios
    python dungeon_flow.py --run --window CreatureCurios --rounds 3
    python dungeon_flow.py --run --window CreatureCurios --boss-seconds 90
    python dungeon_flow.py --probe --window CreatureCurios --seconds 5
"""

from __future__ import annotations

import argparse
import copy
import datetime
import json
import math
import os
import sys
import threading
import time
from typing import Callable, Optional

import numpy as np

import creature_curios_keyer as app

try:
    import win32api
    import win32con
    import win32gui
except ImportError:  # pragma: no cover
    raise SystemExit("缺少 pywin32：pip install pywin32")


PROFILE_NAME = "dungeon_profile.json"
SNAPSHOT_DIR_NAME = "runs"
DUNGEON_DIR_NAME = "dungeons"
ACTIVE_NAME_FILE = "dungeon_active.txt"


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# 多开时所有窗口共用：谁要切前台/借光标，都必须先拿到这把锁。
# 这样 A 窗口在点地图时，B 窗口不会同时把光标/前台抢走。
_INPUT_LOCK = threading.RLock()

# 多开时几个窗口会同时想 OCR；RapidOCR 那个引擎不是多线程安全的，串起来。
_OCR_LOCK = threading.RLock()


def is_game_title(title: str, keyword: str = "CreatureCurios") -> bool:
    """标题里有没有游戏关键字（用来识别「多开的兄弟窗口」）。"""
    t = str(title or "").lower()
    k = str(keyword or "CreatureCurios").lower()
    return bool(k) and k in t


def block_user_input(block: bool) -> bool:
    """短暂屏蔽真实鼠标/键盘（需要管理员权限；失败返回 False）。

    只在「用真实鼠标点一下」的那一小段时间里用：
    防止你正好动鼠标/点键盘，把这一下点到别的窗口上去。
    """
    try:
        return bool(app._USER32.BlockInput(bool(block)))
    except Exception:
        return False

DEFAULT_PROFILE: dict = {
    "name": "默认副本",
    "window_title": "CreatureCurios",
    "keys": {
        "open_map": "m",     # 开地图键（地图1/地图2 都用它）
        "pickup": "space",   # 拾取键
    },
    "allow_grab": False,      # 是否允许抢占鼠标键盘（False=纯后台，不动光标不切前台）
    "block_input": True,      # 真实点击那一瞬间短暂屏蔽外部鼠标键盘（需管理员）
    "input_mode": "hybrid",   # hybrid=只在点击时借鼠标（键盘走后台）；foreground=全程占用；background=纯后台
    "prime_activate": True,   # 后台模式下补发 WM_ACTIVATE/焦点消息
    "action_gap": 1.0,        # 动作之间的缓冲（秒）
    "nav_macro": {
        "enabled": False,     # 用录制的鼠标宏引路（地图点识别不准时用）
        "events": [],         # 录制下来的点击/按键（归一化客户区坐标）
        "meta": {},           # 录制时的客户区尺寸（换分辨率也能回放）
        "speed": 1.0,         # 回放速度 0.25~3.0
        "fallback": False,    # 宏跑完没到 BOSS 时，是否回退到地图点位
    },
    "enter": {
        "dungeon_pt": None,        # 地图1 上的副本入口 [nx, ny]
        "travel_wait": 40.0,       # 走路/寻路到副本口要多久
        "dialog": {"roi": None, "sig": [], "grid": 12, "sim": 0.90},
        "confirm_pt": None,        # 弹窗里「进入游戏」按钮 [nx, ny]
        "dialog_timeout": 24.0,    # 点完副本入口后，这么久还没弹窗就重新寻路
        "loaded": {"roi": None, "sig": [], "grid": 12, "sim": 0.90},  # 可选：进本后特征
        "progress": {"roi": None, "min_fill": 0.90, "samples": 3,
                     "interval": 0.25},  # 加载进度条区域 + 满多少算加载完
        "blind_confirm_after": 3.0,  # 这么久还没识别到弹窗 → 先盲点一次确认
        "still_diff": 0.02,        # 相邻两帧差异小于它才算"画面静止"（静止了才比对弹窗）
        "assume_entered": True,    # 测不到进度条时，点过「进入游戏」就先当已进本
        "load_wait": 10.0,         # 点完确认后的加载时间
        "retry": 3,                # 没识别到弹窗 → 重新开图重点，最多几次
        "map_radius": 0.02,        # 重试时在副本入口周围绕圈微调（归一化半径）
    },
    "mount": {
        "enabled": True,           # 每次寻路前按一下 S，检查宠物是否骑乘
        "key": "s",
        "dialog": {"roi": None, "sig": [], "grid": 12, "sim": 0.90},  # 骑乘弹窗
        "text_roi": None,          # 宠物文本区域（OCR：未骑乘写「骑乘」，已骑乘写「休息」）
        "mount_text": "骑乘",      # 文本里出现它 = 还没骑，需要点
        "rest_text": "休息",       # 文本里出现它 = 已经骑了，不用点
        "state": {"roi": None, "sig": [], "grid": 12, "sim": 0.90},   # 旧的：图形标记（可不标）
        "mount_pt": None,          # 未骑乘时点这个「骑乘」按钮
        "wait": 4.0,               # 等骑乘弹窗出现
        "settle": 1.2,             # 点完骑乘等多久
        "close_key": "",           # 关弹窗的键（空 = 再按一次 key）
    },
    "farm": {
        "farm_pt": None,           # 地图2 上的「打怪点」[nx, ny]（在这里判断疲劳）
        "boss_pt": None,           # 地图2 上的 BOSS 点 [nx, ny]
        "travel_wait": 30.0,       # 点完后等自动寻路（有「到达标记」就提前结束）
        "arrived": {"roi": None, "sig": [], "grid": 12, "sim": 0.90},  # 可选：到达标记
        "boss_present": {"roi": None, "sig": [], "sigs": [], "grid": 12,
                         "sim": 0.60, "min_sim": 0.75, "search": 1.6,
                         "tol": 30.0, "still_seconds": 0.8,
                         "still_timeout": 12.0, "still_diff": 0.05},
        # ↑ BOSS 身形：只在「画面完全不动」时比对，相似度要 ≥ max(sim, 0.75)，
        #   防止路过的「小怪」被当成 BOSS
        "boss_hp": {"roi": None, "mode": "ocr", "kill_hp": 0, "miss": 3,
                    "ocr_every": 2.0, "grace": 6.0, "min_fill": 0.15},
        # ↑ BOSS 死亡判定（默认 mode=ocr，和主程序 BossWatcher 同一套）：
        #   对框住的区域 OCR 读「名字 + 当前/最大血量」；
        #   读到 0（或 ≤kill_hp）→ 死；连续 miss 次读不到（血条/名字消失）→ 死。
        #   mode=bright 是 v3.24 tag 的亮度占比；mode=icon 才是"开打拍基准比图标"。
        "boss_retry": 3,           # 没识别到「打怪点BOSS」就重新寻路几次
        "map_radius": 0.02,        # 重试时在 BOSS 点周围绕圈微调（归一化半径）
        "pickup_on_a": True,       # 每次平A后都按一次拾取
        "seconds": 30.0,           # 兼容旧标定；实际 BOSS 战上限用 loop.boss_seconds
        "interval_ms": 250,
        "use_skills": True,
        "skill1": "1", "skill1_every": 12.0,
        "skill2": "2", "skill2_every": 6.0,
        "hp_roi": None,            # 血量数字区域（框了才按血量喝药）
        "hp_threshold": 1600,
        "hp_check_every": 2.0,
        "potion_keys": ["q", "w", "e"],  # 低于阈值时依次按 Q、W、E
        "potion_every": 6.0,             # 低于阈值时，每 6 秒喝一次
        "potion_timer_enabled": False,   # 定时喝药（不管血量，每 N 秒按一次）
        "potion_timer_every": 60.0,      # 定时喝药间隔（默认 60 秒）
        "pickup_every": 8.0,       # 挂机期间每隔几秒按一次拾取
        "pickup_times": 3,         # 打完 BOSS、退出前再拾取几次
    },
    "loop": {
        "link_fatigue": True,      # 打怪点挂机 ↔ 打BOSS 联动（按疲劳上下限切换）
        "fatigue_every": 90.0,     # 打怪/挂机时每隔多少秒查一次疲劳（默认 90 秒 = 1 分半）
        "boss_seconds": 120.0,     # BOSS 战上限（血条一直不消失时兜底；最大 1000s）
        "default_action": "farm",  # 疲劳在中间区间时做哪个：boss / farm
        "boss_count": 1,           # 一只副本里要打几只 BOSS（分身怪要 3 只才算过）
    },
    "warehouse": {
        "enabled": True,
        "icon_pt": None,       # 仓库图标（点它打开仓库）
        "deposit_pt": None,    # 「一键放入」按钮
        "gap": 0.5,            # 每步之间缓冲
        "open_wait": 1.5,      # 点完仓库图标等界面出来
        "esc_times": 2,        # 放完按几下 ESC 回主界面（默认 2，不多不少）
        "after": "exit",       # 什么时候放：exit=退出副本后
    },
    "exit": {
        "exit_pt": None,           # 「退出副本」按钮（第一下）[nx, ny]
        "confirm_pt": None,        # 二次确认弹窗的「确认」按钮（第二下）[nx, ny]
        # 退出确认弹窗的识别区域（和进本弹窗分开，各框各的）
        "dialog": {"roi": None, "sig": [], "grid": 12, "sim": 0.90},
        "dialog_timeout": 10.0,    # 等退出确认弹窗最多多久
        "retry": 3,                # 没识别到 → 再点一次「退出副本」
        "exit_wait": 1.0,          # 点完第一下等弹窗出现
        "confirm_wait": 1.0,       # 点完确认等
        "settle": 10.0,            # 退出副本的加载时间
    },
    "fatigue": {
        "enabled": True,           # 是否按疲劳自动切换分支
        "pt": None,                # 疲劳图标点位 [nx, ny]（点一下弹提示）
        "roi": None,               # 疲劳提示文字区域；空则用主界面已框的
        "low": -600,               # 疲劳下限：< 下限 → 去打BOSS
        "high": 600,               # 疲劳上限：> 上限 → 回打怪点打小怪
        "hold_seconds": 3.0,       # 点住疲劳图标等提示框的时间（太短读不到）
        "poll": 0.3,               # 期间每隔多久截一次图
        "retry": 2,                # 读不到就再点几次
    },
    "detect": {
        "poll": 0.2,        # 轮询间隔（越小越灵敏）
        "sim": 0.80,        # 主相似度阈值
        "sim_loose": 0.60,  # 宽松阈值（配合 ink 占比一起判断）
        "ink_min": 0.05,    # 宽松判定时要求的“区域有东西”比例
    },
    "retry": {"rounds": 0, "gap": 8.0, "max_fail": 3},
}


def _base_dir() -> str:
    """标定数据根目录（测试里可以替换）。"""
    return app._data_folder()


def profile_path() -> str:
    return os.path.join(_base_dir(), PROFILE_NAME)


# ---------------------------------------------------------------------------
# 副本方案：命名保存 / 切换 / 删除
# ---------------------------------------------------------------------------


def dungeons_dir() -> str:
    path = os.path.join(_base_dir(), DUNGEON_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _safe_name(name: str) -> str:
    clean = "".join(
        ch for ch in str(name or "").strip()
        if ch not in '\\/:*?"<>|' and ch not in "\r\n\t"
    ).strip()
    return clean[:40]


def dungeon_file(name: str) -> str:
    return os.path.join(dungeons_dir(), f"{_safe_name(name)}.json")


def list_dungeons() -> list[str]:
    try:
        names = [
            os.path.splitext(f)[0]
            for f in os.listdir(dungeons_dir())
            if f.lower().endswith(".json")
        ]
    except Exception:
        return []
    return sorted(names)


def active_name() -> str:
    try:
        with open(os.path.join(_base_dir(), ACTIVE_NAME_FILE),
                  "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def set_active(name: str) -> None:
    try:
        with open(os.path.join(_base_dir(), ACTIVE_NAME_FILE),
                  "w", encoding="utf-8") as fh:
            fh.write(str(name or "").strip())
    except Exception:
        pass


def save_dungeon(name: str, profile: dict) -> str:
    name = _safe_name(name)
    if not name:
        raise ValueError("名字不能为空")
    path = dungeon_file(name)
    save_profile(profile, path)
    set_active(name)
    return path


def load_dungeon(name: str) -> Optional[dict]:
    name = _safe_name(name)
    if not name:
        return None
    path = dungeon_file(name)
    if not os.path.exists(path):
        return None
    return load_profile(path)


def delete_dungeon(name: str) -> bool:
    name = _safe_name(name)
    if not name:
        return False
    path = dungeon_file(name)
    try:
        os.remove(path)
    except OSError:
        return False
    if active_name() == name:
        set_active("")
    return True


def _merge(base: dict, over: dict) -> dict:
    """深拷贝后再合并：避免改到结果里的嵌套字典时污染默认配置。"""
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_profile(path: Optional[str] = None) -> dict:
    if not path:
        name = active_name()
        if name:
            candidate = dungeon_file(name)
            if os.path.exists(candidate):
                path = candidate
    path = path or profile_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return json.loads(json.dumps(DEFAULT_PROFILE))
    except Exception as exc:
        print(f"[警告] 读取标定失败：{type(exc).__name__}: {exc}")
        return json.loads(json.dumps(DEFAULT_PROFILE))
    # 旧版只有 fatigue.threshold（±一个数）：先折成 low/high，别被默认值盖掉
    try:
        fat = data.get("fatigue")
        if isinstance(fat, dict) and fat.get("threshold") is not None:
            th = abs(int(fat.get("threshold") or 0))
            if th:
                fat.setdefault("low", -th)
                fat.setdefault("high", th)
    except Exception:
        pass
    merged = _merge(DEFAULT_PROFILE, data)
    return _normalize_profile(merged)


def _normalize_profile(p: dict) -> dict:
    """把旧版标定的“太严”阈值调松，否则弹窗明明在也识别不到。"""
    loose_defaults = {"sim": 0.80, "sim_loose": 0.60, "ink_min": 0.05}
    try:
        for group, key in (("enter", "dialog"), ("exit", "dialog"),
                           ("mount", "dialog"), ("mount", "state")):
            spec = ((p.get(group) or {}).get(key) or {})
            if not isinstance(spec, dict) or not spec.get("roi"):
                continue
            if not spec.get("sim") or float(spec.get("sim", 0)) >= 0.85:
                spec["sim"] = loose_defaults["sim"]
            spec.setdefault("sim_loose", loose_defaults["sim_loose"])
            spec.setdefault("ink_min", loose_defaults["ink_min"])
            (p.setdefault(group, {}) or {})[key] = spec
        body = ((p.get("farm") or {}).get("boss_present") or {})
        if isinstance(body, dict) and body.get("roi"):
            body["sim"] = min(float(body.get("sim") or 0.60), 0.65)
            body["search"] = max(float(body.get("search") or 1.0), 1.6)
            body["tol"] = max(float(body.get("tol") or 18.0), 30.0)
            body.setdefault("min_sim", 0.75)      # v3.25.15 严格下限，防小怪
            body.setdefault("still_seconds", 0.8)
            if float(body.get("still_diff", 0.05) or 0.05) < 0.03:
                # 0.015 太严：站着时环境里的树叶/粒子都会超，永远等不到"静止"
                body["still_diff"] = 0.05
            p.setdefault("farm", {})["boss_present"] = body
        farm_cfg = (p.get("farm") or {})
        if isinstance(farm_cfg, dict):
            keys = farm_cfg.get("potion_keys")
            if keys == ["q"]:
                # v3.25.12：默认改成 Q/W/E（老标定升级）
                farm_cfg["potion_keys"] = ["q", "w", "e"]
            p.setdefault("farm", {}).update(farm_cfg)
        loop_cfg = (p.get("loop") or {})
        if isinstance(loop_cfg, dict):
            try:
                fe = float(loop_cfg.get("fatigue_every", 90.0) or 90.0)
            except (TypeError, ValueError):
                fe = 90.0
            if fe in (60.0, 64.0, 66.0):
                # v3.25.13：默认改成 90 秒（1 分半）查一次疲劳
                loop_cfg["fatigue_every"] = 90.0
            p.setdefault("loop", {}).update(loop_cfg)
        fat = (p.get("fatigue") or {})
        if isinstance(fat, dict):
            if fat.get("low") is None or fat.get("high") is None:
                th = abs(int(fat.get("threshold", 600) or 0))
                if fat.get("low") is None:
                    fat["low"] = -th
                if fat.get("high") is None:
                    fat["high"] = th
            p.setdefault("fatigue", {}).update(fat)
        hp = ((p.get("farm") or {}).get("boss_hp") or {})
        if isinstance(hp, dict) and hp.get("roi"):
            # v3.25.6：判死回到 v3.24.0 tag 的「亮像素占比」。
            # 只有标定里明确写 mode=icon 才用「开打拍基准比图标」那套。
            if hp.get("use_icon"):
                hp["mode"] = "icon"
            elif str(hp.get("mode", "")).lower() == "bright":
                hp["mode"] = "bright"
            else:
                # 之前自动写进去的 icon / bar / ink 统统回到 OCR（最可靠那个）
                hp["mode"] = "ocr"
            hp.setdefault("kill_hp", 0)
            hp.setdefault("ocr_every", 2.0)
            hp.setdefault("grace", 6.0)
            hp.setdefault("sim", 0.75)
            hp.setdefault("grid", 12)
            hp.setdefault("tol", 30.0)
            hp.setdefault("min_same", 0.35)
            hp.setdefault("color_tol", 40)
            p.setdefault("farm", {})["boss_hp"] = hp
    except Exception:
        pass
    return p


def save_profile(profile: dict, path: Optional[str] = None) -> str:
    path = path or profile_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, ensure_ascii=False, indent=2)
    return path


def copy_profile_for_slot(profile: dict, boss_seconds: Optional[float] = None) -> dict:
    """多开：每个窗口复制一份（不再按窗口设挂机时长）。

    保留 boss_seconds 参数只是为了兼容旧调用：它现在表示 BOSS 战上限，
    不再是「挂机多久」。
    """
    p = json.loads(json.dumps(profile))
    if boss_seconds is not None:
        try:
            p.setdefault("loop", {})["boss_seconds"] = max(
                5.0, min(1000.0, float(boss_seconds))
            )
        except (TypeError, ValueError):
            pass
    return p


# ---------------------------------------------------------------------------
# 判据（纯计算，方便单测）
# ---------------------------------------------------------------------------


def frame_array(frame: tuple[int, int, bytes]) -> np.ndarray:
    w, h, data = frame
    return np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)


def crop_norm(frame: tuple[int, int, bytes], roi_norm) -> np.ndarray:
    w, h, _data = frame
    x, y, rw, rh = app.roi_from_norm(
        (float(roi_norm[0]), float(roi_norm[1]),
         float(roi_norm[2]), float(roi_norm[3])), w, h,
    )
    return frame_array(frame)[y:y + rh, x:x + rw, :3]


def ink_ratio(patch: np.ndarray, tol: int = 36) -> float:
    """区域里「不像背景」的像素占比（外圈像素当背景参考）。"""
    if patch.size == 0:
        return 0.0
    border = np.concatenate([
        patch[0, :, :].reshape(-1, 3),
        patch[-1, :, :].reshape(-1, 3),
        patch[:, 0, :].reshape(-1, 3),
        patch[:, -1, :].reshape(-1, 3),
    ]).astype(np.int16)
    bg = np.median(border, axis=0)
    flat = patch.reshape(-1, 3).astype(np.int16)
    dev = np.abs(flat - bg).max(axis=1)
    return float((dev > tol).mean())


def color_signature(patch: np.ndarray, grid: int = 12) -> list[float]:
    """按格降采样成 RGB 特征（灰度签名对"只变颜色"不敏感，这里补上）。"""
    if patch is None or patch.size == 0:
        return []
    h, w = patch.shape[:2]
    gh = max(1, min(int(grid), h))
    gw = max(1, min(int(grid), w))
    ys = np.linspace(0, h, gh + 1).astype(int)
    xs = np.linspace(0, w, gw + 1).astype(int)
    out: list[float] = []
    for i in range(gh):
        y0, y1 = ys[i], max(ys[i] + 1, ys[i + 1])
        for j in range(gw):
            x0, x1 = xs[j], max(xs[j] + 1, xs[j + 1])
            m = patch[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)
            out.extend([round(float(m[0]), 1), round(float(m[1]), 1),
                        round(float(m[2]), 1)])
    return out


def color_similarity(a, b, tol: float = 30.0) -> float:
    """1.0 = 每格三个通道都一样；按「变色格子比例」算。"""
    if not a or not b or len(a) != len(b) or len(a) % 3:
        return 0.0
    da = np.asarray(a, np.float32).reshape(-1, 3)
    db = np.asarray(b, np.float32).reshape(-1, 3)
    return float(1.0 - (np.abs(da - db).max(axis=1) > tol).mean())


def same_color_ratio(patch: np.ndarray, ref, tol: int = 40) -> float:
    """和参考主色接近的像素占比（0~1）。

    BOSS 血量条/图标这类 HUD，形状会随掉血/特效一直变（结构相似度靠不住），
    但「整块还是不是那个颜色」很稳：BOSS 活着时一片暖色，死后变回背景色。
    """
    if patch is None or ref is None or patch.size == 0:
        return 0.0
    try:
        dev = np.abs(patch.astype(np.int16) - np.asarray(ref, np.int16)).max(axis=2)
        return float((dev <= max(1, int(tol))).mean())
    except Exception:
        return 0.0


def bar_mask(patch: np.ndarray) -> np.ndarray:
    """找出「像血条」的红/橙/黄/绿像素。

    游戏里血条通常是高饱和的红/橙/绿，背景是蓝色天空/灰色场景。
    只按亮度判断会被亮背景骗过去，所以这里按颜色找条带。
    """
    if patch.size == 0:
        return np.zeros(patch.shape[:2], dtype=bool)
    p = patch.astype(np.int16)
    r, g, b = p[:, :, 0], p[:, :, 1], p[:, :, 2]
    red = (r > 90) & (r > g + 25) & (r > b + 25)
    green = (g > 90) & (g > r + 20) & (g > b + 10)
    yellow = ((r > 120) & (g > 90) & (b < 110)
              & (r >= g - 20) & (g > b + 30))
    return red | green | yellow


def bar_row_scores(patch: np.ndarray) -> np.ndarray:
    mask = bar_mask(patch)
    if mask.size == 0:
        return np.zeros(0, dtype=float)
    return mask.mean(axis=1)


def bar_score(patch: np.ndarray, band=None) -> float:
    """血条内容分数：条带里最像血条的那一行占整行的比例。

    取「最大行」而不是整块平均：ROI 框大了也没关系，只要血条那一行有内容。
    """
    if patch.size == 0:
        return 0.0
    if band:
        try:
            y0, y1 = float(band[0]), float(band[1])
            h = patch.shape[0]
            a = max(0, min(h, int(round(y0 * h))))
            b = max(a + 1, min(h, int(round(y1 * h))))
            patch = patch[a:b]
        except (TypeError, ValueError, IndexError):
            pass
    scores = bar_row_scores(patch)
    return float(scores.max()) if scores.size else 0.0


def bar_band(patch: np.ndarray, min_score: float = 0.05):
    """从框选时的截图里找血条所在的行带，存成 ROI 内的相对坐标。"""
    scores = bar_row_scores(patch)
    if scores.size == 0 or float(scores.max()) < min_score:
        return None
    h = patch.shape[0]
    best = int(scores.argmax())
    y0 = max(0, best - 2)
    y1 = min(h, best + 3)
    return [round(y0 / max(1, h), 4), round(y1 / max(1, h), 4)]


def signature(patch: np.ndarray, grid: int = 12) -> list[float]:
    """把区域降采样成 grid×grid 灰度特征，用于锚点比对。"""
    if patch.size == 0:
        return []
    gray = patch.astype(np.float32).mean(axis=2)
    h, w = gray.shape
    gh = max(1, min(int(grid), h))
    gw = max(1, min(int(grid), w))
    ys = np.linspace(0, h, gh + 1).astype(int)
    xs = np.linspace(0, w, gw + 1).astype(int)
    out: list[float] = []
    for i in range(gh):
        y0, y1 = ys[i], max(ys[i] + 1, ys[i + 1])
        for j in range(gw):
            x0, x1 = xs[j], max(xs[j] + 1, xs[j + 1])
            out.append(round(float(gray[y0:y1, x0:x1].mean()), 2))
    return out


def similarity(a, b, tol: float = 18.0) -> float:
    """1.0 = 完全一样；按「变化格子比例」算，一小块图标出现/消失也能看出来。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    diff = np.abs(np.asarray(a, np.float32) - np.asarray(b, np.float32))
    return float(1.0 - (diff > tol).mean())


# ---------------------------------------------------------------------------
# 游戏窗口（后台 PostMessage）
# ---------------------------------------------------------------------------


class WinGame:
    """真实游戏窗口：后台截图 + 后台点击/按键，不动鼠标、不抢前台。"""

    def __init__(self, hwnd: int, profile: dict,
                 log: Callable[[str], None] = print, dry: bool = False,
                 snapshot_dir: Optional[str] = None, prefix: str = "",
                 stop_event=None):
        self.hwnd = int(hwnd)
        self.profile = profile
        self.log = log
        self.dry = bool(dry)
        self.snapshot_dir = snapshot_dir
        self.prefix = prefix
        self.stop_event = stop_event
        self.last_error = ""
        self._focus_saved = 0
        self._user_fg = 0
        self._logged_target = False
        self._block_input_ok: Optional[bool] = None   # None=还没试过
        self._hold_saved_cursor = None
        self._hold_saved_focus = 0

    # -- 基础 --
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """可被 stop 立刻打断的 sleep（100ms 粒度）。"""
        remain = max(0.0, float(seconds))
        if remain <= 0:
            return
        while remain > 0:
            if self.stop_event is not None and self.stop_event.is_set():
                return
            step = min(0.1, remain)
            time.sleep(step)
            remain -= step

    def alive(self) -> bool:
        return bool(win32gui.IsWindow(self.hwnd))

    def minimized(self) -> bool:
        try:
            return bool(win32gui.IsIconic(self.hwnd))
        except Exception:
            return False

    # -- 截图 --
    def grab(self):
        cap, err = app.capture_client_bgra(self.hwnd)
        self.last_error = err or ""
        return cap

    def snapshot(self, tag: str) -> None:
        if not self.snapshot_dir:
            return
        cap = self.grab()
        if cap is None:
            return
        try:
            from PIL import Image
            w, h, data = cap
            img = Image.frombuffer(
                "RGBA", (w, h), bytes(data), "raw", "BGRA", 0, 1
            ).convert("RGB")
            os.makedirs(self.snapshot_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%H%M%S")
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)[:40]
            img.save(os.path.join(self.snapshot_dir, f"{stamp}_{safe}.png"))
        except Exception:
            pass

    # -- 前台模式 --
    def allow_grab(self) -> bool:
        """是否允许抢占鼠标键盘（勾了才借鼠标/切前台）。"""
        return bool(self.profile.get("allow_grab", False))

    def _post_aux(self, hwnd: int, message: int, wparam: int, lparam: int) -> bool:
        try:
            result = win32api.PostMessage(hwnd, message, wparam, lparam)
            if result is None or int(result) != 0:
                return True
            return bool(_USER32.PostMessageW(hwnd, message, wparam, lparam))
        except Exception:
            try:
                return bool(_USER32.PostMessageW(hwnd, message, wparam, lparam))
            except Exception:
                return False

    def _cursor_inside(self) -> bool:
        try:
            x, y = win32api.GetCursorPos()
            cx, cy = win32gui.ScreenToClient(self.hwnd, (x, y))
            left, top, right, bottom = win32gui.GetClientRect(self.hwnd)
            return left <= cx < right and top <= cy < bottom
        except Exception:
            return True

    def prime_background_input(self) -> None:
        """让游戏以为自己是激活/有焦点的（不切前台、不动光标）。

        Unity 类游戏失去前台后会丢弃 WM_KEYDOWN，必须补这几条激活/焦点消息；
        鼠标不在窗口里时再补一条 WM_MOUSEMOVE，让“鼠标在窗口内”的状态恢复。
        """
        if not win32gui.IsWindow(self.hwnd):
            return
        try:
            fg = int(win32gui.GetForegroundWindow() or 0)
        except Exception:
            fg = 0
        if fg != self.hwnd:
            self._post_aux(self.hwnd, 0x001C, 1, 0)      # WM_ACTIVATEAPP
            self._post_aux(self.hwnd, 0x0086, 1, 0)      # WM_NCACTIVATE
            self._post_aux(self.hwnd, 0x0006, 1, fg)     # WM_ACTIVATE / WA_ACTIVE
            self._post_aux(self.hwnd, 0x0007, fg, 0)     # WM_SETFOCUS
        if not self._cursor_inside():
            try:
                _l, _t, cw, ch = win32gui.GetClientRect(self.hwnd)
                x, y = int(cw) // 2, int(ch) // 2
                self._post_aux(self.hwnd, 0x0200, 0,
                               (x & 0xFFFF) | ((y & 0xFFFF) << 16))
            except Exception:
                pass

    def message_target(self) -> int:
        """真正接收键盘消息的窗口（Unity 常把焦点放在子窗口上）。"""
        root = self.hwnd
        try:
            pid = wt.DWORD()
            tid = int(_USER32.GetWindowThreadProcessId(root, ctypes.byref(pid)))
            if tid:
                info = app.GUITHREADINFO()
                info.cbSize = ctypes.sizeof(app.GUITHREADINFO)
                if _USER32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndFocus:
                    focus = int(info.hwndFocus)
                    if focus == root or _USER32.IsChild(root, focus):
                        return focus
        except Exception:
            pass
        return root

    def input_mode(self) -> str:
        mode = str(self.profile.get("input_mode", "hybrid") or "hybrid").lower()
        return mode if mode in ("hybrid", "foreground", "background") else "hybrid"

    def foreground_mode(self) -> bool:
        """真正需要切前台/动鼠标的两种情况（hybrid 只在点击时借）。"""
        return self.input_mode() in ("foreground", "hybrid")

    def hybrid_mode(self) -> bool:
        return self.input_mode() == "hybrid"

    def release_focus(self, quiet: bool = False) -> None:
        """把前台还给抢占之前那个窗口（混合模式点完立刻调用）。"""
        if not self.hybrid_mode():
            return
        with _INPUT_LOCK:
            saved = getattr(self, "_focus_saved", 0)
            self._focus_saved = 0
            if not saved:
                return
            try:
                if saved == self.hwnd or not win32gui.IsWindow(saved):
                    return
                if int(win32gui.GetForegroundWindow() or 0) != self.hwnd:
                    return
                app._force_foreground_window(saved)
                if not quiet:
                    self.log("已把前台/光标还给原来的窗口（键盘继续后台发送）")
            except Exception:
                pass

    def pin_user_foreground(self) -> None:
        """记下「你正在用的窗口」，挂机时用它把前台还给你。

        多开时前台很可能是**另一个游戏窗口**——那个不能记，否则一会儿
        就把兄弟窗口抢到前台，打断它的打怪节奏。
        """
        try:
            fg = int(win32gui.GetForegroundWindow() or 0)
        except Exception:
            fg = 0
        if not fg or fg == self.hwnd:
            fg = 0
        elif self._is_sibling_window(fg):
            fg = 0
        self._user_fg = fg

    def _is_sibling_window(self, hwnd: int) -> bool:
        """这个窗口是不是同一个游戏（多开的兄弟窗口）。"""
        try:
            title = win32gui.GetWindowText(hwnd) or ""
        except Exception:
            return False
        return is_game_title(title, (self.profile or {}).get("window_title"))

    def restore_user_foreground(self, quiet: bool = False) -> None:
        """把前台还给你（前台/混合模式都生效）；按键本身一直是后台发送的。"""
        fg = int(getattr(self, "_user_fg", 0) or 0)
        self._user_fg = 0
        if not fg:
            return
        with _INPUT_LOCK:
            try:
                if not win32gui.IsWindow(fg):
                    return
                if int(win32gui.GetForegroundWindow() or 0) != self.hwnd:
                    return
                app._force_foreground_window(fg)
                if not quiet:
                    self.log("已把前台还给你（挂机按键继续走后台 PostMessage）")
            except Exception:
                pass

    def activate(self) -> bool:
        """把游戏窗口切到前台（只有允许抢占时才真的切）。"""
        if not self.foreground_mode() or self.dry:
            return True
        if not self.allow_grab():
            return True   # 纯后台模式：不切前台，只发消息
        with _INPUT_LOCK:
            try:
                if int(win32gui.GetForegroundWindow() or 0) == self.hwnd:
                    return True
            except Exception:
                pass
            try:
                if win32gui.IsIconic(self.hwnd):
                    win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
                    self.sleep(0.25)
            except Exception:
                pass
            ok = bool(app._force_foreground_window(self.hwnd))
            if not ok:
                self.sleep(0.2)
                ok = bool(app._force_foreground_window(self.hwnd))
            if not ok:
                self.log("警告：没能把游戏切到前台，本次不点击（避免点到别的窗口）")
            return ok

    def _client_to_screen(self, client_x: int, client_y: int):
        try:
            sx, sy = win32gui.ClientToScreen(self.hwnd, (int(client_x), int(client_y)))
            return int(sx), int(sy)
        except Exception:
            return 0, 0

    # -- 输入 --
    def _post(self, message: int, wparam: int, lparam: int) -> bool:
        try:
            win32api.PostMessage(self.hwnd, message, wparam, lparam)
            return True
        except Exception as exc:
            self.last_error = f"PostMessage 失败：{exc}"
            return False

    def click_norm(self, point, keep_cursor: bool = False) -> bool:
        if not point:
            return False
        try:
            _l, _t, cw, ch = win32gui.GetClientRect(self.hwnd)
        except Exception as exc:
            self.last_error = f"取客户区失败：{exc}"
            return False
        cw, ch = int(cw), int(ch)
        if cw <= 0 or ch <= 0:
            return False
        x = max(1, min(cw - 2, int(round(float(point[0]) * cw))))
        y = max(1, min(ch - 2, int(round(float(point[1]) * ch))))
        if self.dry:
            self.log(f"[DRY] click ({x},{y})")
            return True
        if self.foreground_mode() and self.allow_grab():
            return self._grab_click(x, y, keep_cursor=keep_cursor)
        if self.profile.get("prime_activate", True):
            app.prime_background_input(self.hwnd, activate=True)
        ok = app.post_click_client(self.hwnd, x, y)
        if not ok:
            self.last_error = "后台点击发送失败"
        return ok

    def _grab_click(self, x: int, y: int, keep_cursor: bool = False) -> bool:
        """真实鼠标点击：整段拿全局输入锁，点完光标/前台都还原。

        多开时 4 个窗口共用 _INPUT_LOCK，所以不会出现 A 窗口把光标移到
        地图上、B 窗口又把光标还原到半路的情况。
        """
        hybrid = self.hybrid_mode()
        with _INPUT_LOCK:
            saved_cursor = None
            blocked = False
            if self.profile.get("block_input", True) and self._block_input_ok is not False:
                blocked = block_user_input(True)
                if self._block_input_ok is None:
                    self._block_input_ok = blocked
                    if blocked:
                        self.log("点击时已临时屏蔽外部鼠标键盘"
                                 "（点完立刻恢复；若点击失效请取消勾选）")
                    else:
                        self.log("点击时屏蔽外部鼠标键盘失败（需要以管理员运行）"
                                 "→ 不屏蔽，继续点击")
            if hybrid or keep_cursor:
                try:
                    saved_cursor = win32api.GetCursorPos()
                except Exception:
                    saved_cursor = None
            try:
                fg = int(win32gui.GetForegroundWindow() or 0)
            except Exception:
                fg = 0
            self._focus_saved = fg if fg and fg != self.hwnd else 0
            try:
                if not self.activate():
                    self.last_error = "游戏窗口不在前台，已跳过这次点击"
                    return False
                sx, sy = self._client_to_screen(x, y)
                try:
                    win32api.SetCursorPos((sx, sy))
                    self.sleep(0.05)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                    self.sleep(0.04)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                    self.sleep(0.08)
                    if keep_cursor:
                        # 「按住不放」：光标/前台先别还原，让游戏把提示框弹出来
                        self._hold_saved_cursor = saved_cursor
                        self._hold_saved_focus = self._focus_saved
                        self._focus_saved = 0
                    return True
                except Exception as exc:
                    self.last_error = f"真实鼠标点击失败：{exc}"
                    self.log(self.last_error)
                    return False
            finally:
                if not keep_cursor:
                    if hybrid and saved_cursor is not None:
                        try:
                            win32api.SetCursorPos(saved_cursor)
                        except Exception:
                            pass
                    # 用户要求：抢完鼠标键盘要回到原来的界面。
                    if hybrid:
                        self.release_focus(quiet=True)
                if blocked:
                    block_user_input(False)

    def release_hold(self) -> None:
        """结束「按住不放」（疲劳提示那种）：把光标/前台还原。"""
        saved = getattr(self, "_hold_saved_cursor", None)
        focus = int(getattr(self, "_hold_saved_focus", 0) or 0)
        self._hold_saved_cursor = None
        self._hold_saved_focus = 0
        if saved is None and not focus:
            return
        with _INPUT_LOCK:
            if saved is not None:
                try:
                    win32api.SetCursorPos(saved)
                except Exception:
                    pass
            if focus:
                try:
                    if int(win32gui.GetForegroundWindow() or 0) == self.hwnd:
                        app._force_foreground_window(focus)
                except Exception:
                    pass

    def tap(self, name: str, hold: float = 0.05) -> bool:
        """后台投递按键（前台模式下也用投递，避免输入法把按键吃掉）。"""
        if not name:
            return False
        vk = app._vk_from_key_name(str(name))
        if vk is None:
            self.log(f"[警告] 不认识的按键名：{name}")
            return False
        if self.dry:
            return True
        try:
            scan = int(win32api.MapVirtualKey(vk, 0)) & 0xFF
        except Exception:
            scan = 0
        # 按键一律走 PostMessage：真实 keybd_event 会被中文输入法截成
        # VK_PROCESSKEY(229)，游戏就收不到 A / M 了；点击才需要真实鼠标。
        self.prime_background_input()
        target = self.message_target()
        if target != self.hwnd and not getattr(self, "_logged_target", False):
            self._logged_target = True
            self.log(f"按键投递到焦点子窗口 0x{target:X}（Unity 常见）")
        down_lp = 1 | (scan << 16)
        up_lp = down_lp | 0xC0000000
        ok = self._post_aux(target, win32con.WM_KEYDOWN, vk, down_lp)
        if hold > 0:
            self.sleep(hold)
        ok = self._post_aux(target, win32con.WM_KEYUP, vk, up_lp) and ok
        return ok


# ---------------------------------------------------------------------------
# 流程
# ---------------------------------------------------------------------------


class DungeonRunner:
    """三步流程：进本 → BOSS 点挂机 → 退出。"""

    def __init__(self, game, profile: dict,
                 log: Optional[Callable[[str], None]] = None,
                 stop_event=None, verbose: bool = False):
        self.game = game
        self.p = profile
        self._log = log or print
        self.stop_event = stop_event
        self.verbose = bool(verbose)
        self.stats = {"ok": 0, "fail": 0, "failed_streak": 0}
        self.stopped = False
        self._last_frame = None
        self._switch_to: Optional[str] = None
        self._mode: Optional[str] = None   # 联动模式：farm / boss
        # 开打时拍下来的「BOSS 血量图标」基准；图标没了就判死
        self._boss_icon_sigs: list[list[float]] = []
        self._boss_icon_color = None
        self._hp_seen = False
        self._hp_ocr_at = -1.0
        self._hp_ocr_visible = True
        self._hp_ocr_skips = 0
        self._hp_skipped = False
        self._hp_ocr_logged = False
        self._hp_ocr_busy = 0
        self._last_progress_seen = False
        self._last_hp_detail = ""
        self._icon_checks = 0
        self._icon_miss_shot = False
        self._last_hp_metric = "图标相似度"

    # -- 日志 / 工具 --
    def log(self, text: str) -> None:
        prefix = getattr(self.game, "prefix", "") or ""
        self._log(f"{prefix}[副本] {text}")

    def _stopping(self) -> bool:
        if self.stop_event is not None and self.stop_event.is_set():
            self.stopped = True
        return self.stopped

    def _sec(self, group: str, key: str, default: float) -> float:
        return float((self.p.get(group) or {}).get(key, default))

    def _key(self, name: str, default: str = "") -> str:
        return str((self.p.get("keys") or {}).get(name, default) or default)

    def _farm(self) -> dict:
        return self.p.get("farm") or {}

    # -- 画面 --
    def grab(self):
        frame = self.game.grab()
        if frame is None:
            self.log(f"截图失败：{getattr(self.game, 'last_error', '')}")
            return None
        self._last_frame = frame
        return frame

    def frame_diff(self, a, b) -> float:
        """两张截图不一样的程度（0=完全一样，1=差很多）。用于判断“人停下来了没”。"""
        try:
            w = min(a[0], b[0])
            h = min(a[1], b[1])
            pa = frame_array(a)[:h, :w, :3].astype(np.int16)
            pb = frame_array(b)[:h, :w, :3].astype(np.int16)
            step_y = max(1, h // 32)
            step_x = max(1, w // 32)
            pa = pa[::step_y, ::step_x]
            pb = pb[::step_y, ::step_x]
            return float(np.abs(pa - pb).mean() / 255.0)
        except Exception:
            return 1.0

    def wait_scene_still(self, seconds: float = 0.8, timeout: float = 12.0,
                         diff_thresh: float = 0.015) -> bool:
        """等人停下来：连续 seconds 秒画面几乎不变就算静止。"""
        poll = max(0.05, float((self.p.get("detect") or {}).get("poll", 0.2)))
        start = self.game.now()
        still_since = None
        prev = self.grab()
        while not self._stopping() and self.game.now() - start < timeout:
            self.game.sleep(poll)
            cur = self.grab()
            if prev is None or cur is None:
                prev = cur
                continue
            diff = self.frame_diff(prev, cur)
            prev = cur
            if diff <= diff_thresh:
                if still_since is None:
                    still_since = self.game.now()
                if self.game.now() - still_since >= seconds:
                    self.log(f"画面已静止 {seconds:.1f}s（差异 {diff:.3f}）→ 到位")
                    return True
            else:
                still_since = None
        self.log(f"等了 {timeout:.0f}s 画面还在动（可能在走路/打架）")
        return False

    def _detect_cfg(self, key: str, default: float) -> float:
        return float((self.p.get("detect") or {}).get(key, default))

    def anchor_metrics(self, spec: dict, frame) -> tuple[float, float]:
        """返回 (相似度, 非背景占比)。没 sig 时相似度返回 -1。"""
        roi = (spec or {}).get("roi")
        if not roi or frame is None:
            return -1.0, 0.0
        patch = crop_norm(frame, roi)
        ink = ink_ratio(patch)
        sig = spec.get("sig")
        if not sig:
            return -1.0, ink
        grid = int(spec.get("grid", 12))
        tol = float(spec.get("tol", 18.0))
        cur = signature(patch, grid)
        templates = [t for t in (spec.get("sigs") or []) if t]
        if sig:
            templates.insert(0, sig)
        if not templates:
            return -1.0, ink
        best = max(similarity(t, cur, tol) for t in templates)
        search = float(spec.get("search", 1.0) or 1.0)
        if search > 1.02:
            best = max(best, self._search_similarity(spec, frame, search))
        return best, ink

    def _search_similarity(self, spec: dict, frame, search: float) -> float:
        """在放大后的区域里滑动搜索，取所有样本里最像的。"""
        """在大一圈的区域里滑动找最像的位置（BOSS 会走会晃，不能只比原位）。"""
        try:
            rx, ry, rw, rh = (float(v) for v in spec["roi"])
            dx = rw * (search - 1.0) / 2.0
            dy = rh * (search - 1.0) / 2.0
            bx = max(0.0, rx - dx)
            by = max(0.0, ry - dy)
            big = [bx, by, min(1.0 - bx, rw + 2 * dx), min(1.0 - by, rh + 2 * dy)]
            arr = crop_norm(frame, big)
            if arr.size == 0:
                return 0.0
            templates = [t for t in (spec.get("sigs") or []) if t]
            if spec.get("sig"):
                templates.insert(0, spec["sig"])
            if not templates:
                return 0.0
            grid = int(spec.get("grid", 12))
            tol = float(spec.get("tol", 30.0))
            h, w = arr.shape[:2]
            cell = max(3, min(h, w) // grid)
            span = cell * grid
            if span > h or span > w:
                whole = signature(arr, grid)
                return max(similarity(t, whole, tol) for t in templates)
            step = max(1, cell // 2)
            best = 0.0
            y = 0
            while y + span <= h:
                x = 0
                while x + span <= w:
                    patch = arr[y:y + span, x:x + span]
                    win = signature(patch, grid)
                    for t in templates:
                        sim = similarity(t, win, tol)
                        if sim > best:
                            best = sim
                            if best >= 0.99:
                                return best
                    x += step
                y += step
            whole = signature(arr, grid)
            for t in templates:
                best = max(best, similarity(t, whole, tol))
            return best
        except Exception:
            return 0.0

    def anchor_visible(self, spec: dict, frame) -> bool:
        """锚点是否“和标定时一样”。

        判定用两条路（任一条满足即可，解决“明明在、就是差一点”的问题）：
          1) 相似度 ≥ sim（默认 0.80）
          2) 相似度 ≥ sim_loose（默认 0.60）且区域内容占比 ≥ ink_min（默认 0.05）
        """
        if not isinstance(spec, dict):
            return False
        sim_th = float(spec.get("sim") or self._detect_cfg("sim", 0.80))
        loose = float(spec.get("sim_loose")
                      or self._detect_cfg("sim_loose", 0.60))
        ink_min = float(spec.get("ink_min")
                        or self._detect_cfg("ink_min", 0.05))
        sim, ink = self.anchor_metrics(spec, frame)
        if sim < 0:
            return ink > max(0.15, ink_min)
        return sim >= sim_th or (sim >= loose and ink >= ink_min)

    def enter_dialog_spec(self) -> dict:
        return ((self.p.get("enter") or {}).get("dialog") or {})

    def exit_dialog_spec(self) -> dict:
        return ((self.p.get("exit") or {}).get("dialog") or {})

    def dialog_visible(self, frame) -> bool:
        """进本弹窗是否出现（只认「进本」那份区域）。"""
        return self.anchor_visible(self.enter_dialog_spec(), frame)

    def exit_dialog_visible(self, frame) -> bool:
        """退出副本的确认弹窗是否出现（只认「退出」那份区域）。"""
        return self.anchor_visible(self.exit_dialog_spec(), frame)

    def wait_anchor(self, spec: dict, timeout: float, poll: float,
                    desc: str) -> bool:
        """等某个锚点出现（连续 2 帧命中就算）。"""
        if not (spec or {}).get("roi"):
            return False
        start = self.game.now()
        hits = 0
        while not self._stopping() and self.game.now() - start < timeout:
            frame = self.grab()
            if self.anchor_visible(spec, frame):
                hits += 1
                if hits >= 2:
                    self.log(f"识别到{desc}（{self.game.now() - start:.1f}s）")
                    return True
            else:
                hits = 0
            self.game.sleep(poll)
        return False

    def hp_now(self):
        """OCR 当前血量；没配区域返回 None。

        两个坑（都会导致"永远不喝药"）已修：
          · 必须**现拍一张**：_last_frame 在打怪阶段还是进本前的旧图（血是满的）；
          · ocr_hp_from_bgra 要的是**归一化**区域，以前又换算成像素 rect 传进去，
            等于把 (139,96,196,54) 当成 0.139/0.096/... 去裁图 → 永远读不到数字。
        """
        roi = self._farm().get("hp_roi")
        if not roi:
            return None
        frame = self.grab()
        if frame is None:
            return None
        w, h, data = frame
        try:
            roi_norm = tuple(float(v) for v in roi)
            # 多开时 OCR 引擎是共用的：先不阻塞抢一下，抢不到再等 0.6 秒，
            # 还抢不到就这轮跳过（不让某个窗口一直霸占/一直等）。
            got_lock = _OCR_LOCK.acquire(blocking=False)
            if not got_lock:
                got_lock = _OCR_LOCK.acquire(timeout=0.6)
            if not got_lock:
                self._hp_ocr_busy += 1
                return None
            try:
                current, maximum, text = app.ocr_hp_from_bgra(
                    data, w, h, roi_norm)
            finally:
                _OCR_LOCK.release()
            if not self._hp_ocr_logged:
                self._hp_ocr_logged = True
                if current is None:
                    self.log(f"血量识别失败：{text}（区域 {roi}）")
                else:
                    self.log(f"血量识别 OK：{current}"
                             + (f"/{maximum}" if maximum else "")
                             + f"（阈值 {self._farm().get('hp_threshold') or 0}）")
            return current
        except Exception as exc:
            self.log(f"血量识别异常：{type(exc).__name__}: {exc}")
            return None

    def probe(self) -> dict:
        """给界面用：进本弹窗 / 退出弹窗 / 血量 分别报告。"""
        frame = self.grab()
        if frame is None:
            return {}
        out = {}
        checks = (
            ("进本弹窗", self.enter_dialog_spec()),
            ("退出弹窗", self.exit_dialog_spec()),
            ("BOSS身形", self._anchor_of("boss_present")),
        )
        for name, spec in checks:
            if not (spec or {}).get("roi"):
                out[name] = "未标定"
                continue
            sim, ink = self.anchor_metrics(spec, frame)
            th = float((spec or {}).get("sim")
                       or self._detect_cfg("sim", 0.80))
            hit = self.anchor_visible(spec, frame)
            out[name] = (f"{'命中' if hit else '未命中'}"
                         f"(相似度 {sim:.2f}/{th:.2f} 占比 {ink:.2f})")
        hp_spec = self._anchor_of("boss_hp")
        if (hp_spec or {}).get("roi"):
            patch = crop_norm(frame, hp_spec["roi"])
            ratio = self.boss_hp_ratio()
            ink = ink_ratio(patch)
            extra = ""
            if self._boss_icon_sigs:
                grid = int((hp_spec or {}).get("grid", 12) or 12)
                tol = float((hp_spec or {}).get("tol", 30.0) or 30.0)
                csig = color_signature(patch, grid)
                best = max(color_similarity(csig, ref, tol)
                           for ref in self._boss_icon_sigs)
                same = same_color_ratio(
                    patch, getattr(self, "_boss_icon_color", None),
                    int((hp_spec or {}).get("color_tol", 40) or 40))
                extra = (f" / 同色 {same * 100:.0f}%"
                         f" / 图标相似度 {best * 100:.0f}%")
            out["BOSS血量图标"] = (
                (f"亮度 {ratio * 100:.0f}%" if ratio is not None else "亮度读取失败")
                + f" / 内容 {ink * 100:.0f}%" + extra
            )
        prog = self._progress_spec()
        if (prog or {}).get("roi"):
            out["加载进度条"] = f"{self.progress_fill() * 100:.0f}%"
        hp = self.hp_now()
        if hp is not None:
            out["hp"] = hp
        return out

    # -- 骑乘检查 --
    def mount_text_now(self) -> str:
        """OCR 宠物文本区域，返回识别到的文字（没标区域 / 失败返回空）。"""
        cfg = self.p.get("mount") or {}
        roi = cfg.get("text_roi")
        if not roi:
            return ""
        frame = self._last_frame or self.grab()
        if frame is None:
            return ""
        w, h, data = frame
        try:
            rect = app.roi_from_norm(tuple(float(v) for v in roi), w, h)
            return (app.ocr_text_from_bgra(data, w, h, rect) or "").strip()
        except Exception as exc:
            self.log(f"骑乘文本识别异常：{type(exc).__name__}: {exc}")
            return ""

    def mount_state(self):
        """返回 'mounted' / 'need_mount' / 'unknown'。

        规则（按你说的）：宠物文本上写「骑乘」= 还没骑，要点；
        写「休息」= 已经在骑，不用点。
        """
        cfg = self.p.get("mount") or {}
        need = str(cfg.get("mount_text") or "骑乘")
        rest = str(cfg.get("rest_text") or "休息")
        text = self.mount_text_now()
        if text:
            compact = "".join(text.split())
            self.log(f"宠物文本：{compact}")
            if rest and rest in compact:
                return "mounted"
            if need and need in compact:
                return "need_mount"
            self.log(f"文本「{compact}」里既没有「{need}」也没有「{rest}」，按未骑乘处理")
            return "need_mount"
        # 没框文本区域：退回旧的图形标记判断
        spec = cfg.get("state") or {}
        if (spec or {}).get("roi"):
            frame = self._last_frame or self.grab()
            if frame is not None and self.anchor_visible(spec, frame):
                return "mounted"
            return "need_mount"
        return "unknown"

    def mount_ok(self) -> bool:
        """True = 已经骑乘（文本是「休息」）。"""
        return self.mount_state() == "mounted"

    def ensure_mount(self) -> None:
        """每次寻路前按一下 S，看宠物文本：写「骑乘」就点一下，写「休息」就不动。"""
        cfg = self.p.get("mount") or {}
        if not cfg.get("enabled", True):
            return
        has_text = bool(cfg.get("text_roi"))
        has_anchor = bool((cfg.get("state") or {}).get("roi")
                          or (cfg.get("dialog") or {}).get("roi"))
        if not (has_text or has_anchor):
            self.log("没标骑乘文本区域，跳过骑乘检查")
            return

        key = str(cfg.get("key") or "s")
        close_key = str(cfg.get("close_key") or key)
        dialog = cfg.get("dialog") or {}
        poll = max(0.2, float((self.p.get("detect") or {}).get("poll", 0.5)))
        mount_pt = cfg.get("mount_pt")
        settle = float(cfg.get("settle", 1.2))

        self.log(f"按「{key}」打开骑乘面板，看宠物文本")
        self.game.tap(key)
        if (dialog or {}).get("roi"):
            if not self.wait_anchor(dialog, float(cfg.get("wait", 4.0)), poll,
                                    "骑乘弹窗"):
                self.log("没识别到骑乘弹窗，跳过骑乘检查")
                return
        else:
            self.game.sleep(1.0)

        state = self.mount_state()
        if state == "mounted":
            self.log("宠物文本是「休息」→ 已经骑乘，不用点")
        elif state == "need_mount":
            if mount_pt:
                self.log("宠物文本是「骑乘」→ 点一下骑乘")
                if not self.game.click_norm(mount_pt):
                    self.log("点「骑乘」失败（窗口可能不在前台）")
                self.game.sleep(settle)
                self.log("骑乘后："
                         + ("宠物文本是「休息」✓" if self.mount_ok()
                            else "文本还不是「休息」，下一轮再查"))
            else:
                self.log("文档是「骑乘」但没标「骑乘」按钮位置，无法自动骑")
        else:
            self.log("没标骑乘文本区域，跳过")

        self.game.tap(close_key)
        self.game.sleep(0.6)

    # -- 动作 --
    # -- 动作 --
    def open_map(self) -> None:
        key = self._key("open_map", "m")
        self.log(f"按「{key}」开地图")
        self.game.tap(key)
        self.game.sleep(0.8)

    def recover_ui(self, reason: str = "") -> None:
        """点歪了 / 超时没等到弹窗：先把可能开着的界面关掉，让「开地图」是干净的。

        不这么做的话，上一次地图还开着时再按 M 等于**把地图关掉**，
        接着那一下点击就点到游戏世界里 → 角色乱走（这就是"走偏"）。
        """
        enter = self.p.get("enter") or {}
        esc_times = max(0, int(enter.get("recover_esc", 1) or 0))
        if reason:
            self.log(f"回收界面：{reason}")
        for _ in range(esc_times):
            if self._stopping():
                return
            self.game.tap(self._key("close_ui", "esc"))
            self.game.sleep(0.35)
        self.gap("回收界面后缓冲")

    def wait_anchor_still(self, spec, timeout: float, desc: str,
                          need: int = 2, matcher=None,
                          still_seconds: float = 0.5) -> bool:
        """先确认画面**真的没在动**，再比对锚点；在动就一直不判定。

        走路/攻击/旁边有小怪在动时每帧都不一样，那时候任何比对都不可信
        （以前会拿"最像的一帧"兜底，结果把小怪认成 BOSS）。现在：
          1) 连续 still_seconds 秒相邻帧差异都 ≤ still_diff 才算"静止"；
          2) 静止之后才开始比对，连续 need 帧命中才算识别到；
          3) 画面又动了 → 之前的命中清零；
          4) 超时都没等到静止 → 直接 False（绝不拿"动的帧"下结论）。
        matcher: 自定义判定 (spec, frame) -> bool，默认 anchor_visible。
        """
        if not (spec or {}).get("roi"):
            return False
        poll = max(0.1, float((self.p.get("detect") or {}).get("poll", 0.3)))
        still_d = float(spec.get("still_diff", 0.02) or 0.02)
        try:
            still_seconds = float(
                still_seconds if still_seconds is not None
                else spec.get("still_seconds", 0.5))
        except (TypeError, ValueError):
            still_seconds = 0.5
        still_seconds = max(0.0, still_seconds)
        match = matcher or (lambda sp, fr: self.anchor_visible(sp, fr))
        start = self.game.now()
        prev = None
        still_since = None
        hits = 0
        best = -1.0
        next_log = 0.0
        while not self._stopping() and self.game.now() - start < timeout:
            frame = self.grab()
            if frame is None:
                self.game.sleep(poll)
                continue
            diff = None if prev is None else self.frame_diff(prev, frame)
            prev = frame
            sim, ink = self.anchor_metrics(spec, frame)
            best = max(best, sim)
            elapsed = self.game.now() - start
            if diff is None or diff > still_d:
                still_since = None
                hits = 0                      # 画面在动：一律不认
            else:
                if still_since is None:
                    still_since = self.game.now()
                still_for = self.game.now() - still_since
                if still_for < still_seconds:
                    hits = 0                  # 还没站够时间，先等着
                elif match(spec, frame):
                    hits += 1
                    if hits >= max(1, int(need)):
                        self.log(f"画面静止后识别到{desc}"
                                 f"（相似度 {sim:.2f} / 占比 {ink:.2f}"
                                 f" / 画面差异 {diff:.3f}）")
                        return True
                else:
                    hits = 0
            if elapsed >= next_log:
                next_log = elapsed + 3.0
                self.log(f"等{desc}… 相似度 {sim:.2f} / 占比 {ink:.2f}"
                         + ("（画面还在动，先不判定）"
                            if still_since is None else ""))
            self.game.sleep(poll)
        if still_since is None:
            self.log(f"画面一直在动，没敢判定{desc}"
                     f"（最高相似度 {max(0.0, best):.2f}）")
            return False
        self.log(f"画面不动了但没识别到{desc}"
                 f"（最高相似度 {max(0.0, best):.2f}）")
        return False

    def wait_until(self, spec: Optional[dict], timeout: float, desc: str,
                   need: int = 2) -> bool:
        """边等边看：区域内连续 need 次命中就立刻返回 True；超时返回 False。"""
        if not (spec or {}).get("roi"):
            return False
        poll = max(0.05, float((self.p.get("detect") or {}).get("poll", 0.2)))
        start = self.game.now()
        hits = 0
        best_sim = -2.0
        best_ink = 0.0
        next_log = 0.0
        while not self._stopping() and self.game.now() - start < timeout:
            frame = self.grab()
            if frame is not None:
                sim, ink = self.anchor_metrics(spec, frame)
                best_sim = max(best_sim, sim)
                best_ink = max(best_ink, ink)
                if self.anchor_visible(spec, frame):
                    hits += 1
                    if hits >= max(1, int(need)):
                        self.log(f"识别到{desc}（{self.game.now() - start:.1f}s，"
                                 f"相似度 {sim:.2f}）")
                        return True
                else:
                    hits = 0
                    elapsed = self.game.now() - start
                    if elapsed >= next_log:
                        next_log = elapsed + 1.0
                        self.log(f"等{desc}… 相似度 {sim:.2f}"
                                 + (f" / 占比 {ink:.2f}" if ink else ""))
            self.game.sleep(poll)
        self.log(f"没等到{desc}（最高相似度 {best_sim:.2f}，最高占比 {best_ink:.2f}）")
        return False

    def _offset_point(self, point, index: int, radius: float = 0.02):
        """V3.2 的做法：地图点第一次没点中时，在它周围绕圈微调。

        index=0 返回原点；1..8 是半径 radius 的 8 个方向；
        9..16 再转 22.5° 再来一圈（和旧版 5% 半径绕圈一致）。
        坐标是窗口客户区归一化坐标。
        """
        if not point:
            return point
        try:
            bx, by = float(point[0]), float(point[1])
            r = max(0.002, float(radius or 0.0))
            if index <= 0:
                return (bx, by)
            k = (index - 1) % 8
            loop = (index - 1) // 8
            angle = 2.0 * math.pi * k / 8.0 + loop * math.pi / 8.0
            nx = max(0.0, min(1.0, bx + r * math.cos(angle)))
            ny = max(0.0, min(1.0, by + r * math.sin(angle)))
            return (nx, ny)
        except (TypeError, ValueError):
            return point

    def _goto(self, point, wait: float, desc: str,
              arrived_spec: Optional[dict] = None,
              watch_spec: Optional[dict] = None,
              watch_desc: Optional[str] = None,
              sleep_after: bool = True) -> bool:
        """点地图点位 → 边走边看（可传要盯的锚点）→ 最多等 wait 秒。

        watch_spec：路上要盯的东西（比如 BOSS 身形 / 进入弹窗），
                   一出现就立刻返回 True，不用等走完。
        sleep_after：没盯到东西时，要不要把剩余时间等完（进本那边不等）。
        """
        if not point:
            self.log(f"「{desc}」没有标定，跳过")
            return False
        self.ensure_mount()
        self.log(f"点「{desc}」({float(point[0]):.3f}, {float(point[1]):.3f})"
                 f" → 边走边看（最多 {wait:.0f}s）")
        self.open_map()
        if not self.game.click_norm(point):
            self.log(f"点击「{desc}」失败")
            return False
        self.release_focus()

        watch = watch_spec if (watch_spec or {}).get("roi") else arrived_spec
        name = watch_desc or (f"{desc}到达标记" if watch is arrived_spec else f"{desc}目标")
        start = self.game.now()
        if (watch or {}).get("roi"):
            if self.wait_anchor_still(watch, wait, name):
                return True
        if sleep_after:
            left = wait - (self.game.now() - start)
            if left > 0:
                self.game.sleep(left)
        return True

    def _arrived_spec(self) -> dict:
        return (self._farm().get("arrived") or {})

    def _loaded_spec(self) -> dict:
        return ((self.p.get("enter") or {}).get("loaded") or {})

    # ---- 引路宏（鼠标宏）：播放录制的点击/按键 ----
    def nav_macro(self) -> dict:
        return self.p.get("nav_macro") or {}

    def nav_macro_enabled(self) -> bool:
        cfg = self.nav_macro()
        return bool(cfg.get("enabled")) and bool(cfg.get("events"))

    def play_nav_macro(self) -> bool:
        """回放录制的鼠标宏（点击 + 按键），用归一化客户区坐标。

        点击走 WinGame.click_norm：允许抢占时是真实鼠标（点完还原光标/前台），
        不允许抢占时是后台 PostMessage。多开时每次点击各自拿 _INPUT_LOCK，
        不会和别的窗口同时抢鼠标（但两个窗口的宏仍会交错执行）。
        """
        cfg = self.nav_macro()
        events = [e for e in (cfg.get("events") or []) if isinstance(e, dict)]
        if not events:
            self.log("引路宏是空的")
            return False
        try:
            speed = max(0.25, min(3.0, float(cfg.get("speed", 1.0) or 1.0)))
        except (TypeError, ValueError):
            speed = 1.0
        clicks = sum(1 for e in events if e.get("type") == "down"
                     and str(e.get("button", "left")) == "left")
        keys = sum(1 for e in events if e.get("type") == "key_down")
        self.log(f"—— 引路宏：{len(events)} 个事件（{clicks} 次点击 / {keys} 个按键，"
                 f"速度 {speed * 100:.0f}%）——")
        start = self.game.now()
        last_t = 0.0
        try:
            base_t = min(float(e.get("t", 0.0)) for e in events)
        except (TypeError, ValueError):
            base_t = 0.0
        for ev in events:
            if self._stopping():
                return False
            try:
                target_t = max(0.0, (float(ev.get("t", 0.0)) - base_t) / speed)
            except (TypeError, ValueError):
                target_t = last_t
            wait = target_t - (self.game.now() - start)
            if wait > 0:
                self.game.sleep(wait)
            last_t = max(last_t, target_t)
            typ = str(ev.get("type", ""))
            if typ == "down" and str(ev.get("button", "left")) == "left":
                nx, ny = ev.get("nx"), ev.get("ny")
                if nx is None or ny is None:
                    continue
                if not self.game.click_norm((float(nx), float(ny))):
                    self.log("引路宏点击失败，停止回放")
                    return False
            elif typ == "key_down":
                name = str(ev.get("name", "")).strip()
                if name:
                    self.game.tap(name)
        self.gap("引路宏结束缓冲")
        self.release_focus()
        self.log("引路宏回放完成")
        return True

    def _progress_spec(self) -> dict:
        return ((self.p.get("enter") or {}).get("progress") or {})

    # ---- 动作缓冲 ----
    def release_focus(self) -> None:
        """把前台还给用户（混合模式下纯键盘阶段用）。"""
        fn = getattr(self.game, "release_focus", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass

    def restore_user_foreground(self) -> None:
        """把前台还给用户（挂机/打架时窗口不用留在前台）。"""
        fn = getattr(self.game, "restore_user_foreground", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass

    def gap(self, name: str = "动作缓冲") -> None:
        seconds = float(self.p.get("action_gap", 1.0) or 0.0)
        if seconds > 0:
            self.log(f"{name} {seconds:.1f}s")
            self.game.sleep(seconds)

    # ---- 加载进度条判定 ----
    def progress_fill(self) -> Optional[float]:
        """量一次进度条填充比例（0~1）。

        做法：把框住的条子按列扫一遍，某一列里超过 30% 的像素和背景色不同
        就算“这一列是亮的”，亮列占比 = 进度（横向进度条）。这样即使截图
        只截到半截、条子正好铺满整个框，也能量出 0.9x 这种数值。
        """
        spec = self._progress_spec()
        roi = spec.get("roi")
        if not roi:
            return None
        patch = self._progress_patch()
        if patch is None:
            return 0.0
        return self._fill_from_patch(patch)

    def _progress_patch(self):
        """抓一张进度条区域（像素）。"""
        roi = (self._progress_spec() or {}).get("roi")
        if not roi:
            return None
        frame = self.grab()
        if frame is None:
            return None
        try:
            patch = crop_norm(frame, roi)
            if patch.size == 0:
                return None
            return patch.astype(np.int16)
        except Exception:
            return None

    @staticmethod
    def _fill_from_patch(patch) -> float:
        """静态量法：和「四条边的中位色」比，看有多少列和背景不一样。

        （框里带一点背景/空槽时最准；如果框得正好只有条子，就得靠
        _fill_vs_ref 那种「和第一帧比」的办法。）
        """
        try:
            # 背景参考只取「上/下两行」：进度条是横条，上下通常是背景/空槽；
            # 左右两列会有一半是条子，取进来反而把背景算错。
            border = np.concatenate([patch[0], patch[-1]])
            # 背景取「出现次数最多的颜色」（众数），比中位数稳：
            # 条子占一半时中位数会落在条子和空槽中间，结果把两色都算成“有内容”。
            q = (border // 24) * 24
            uniq, counts = np.unique(q, axis=0, return_counts=True)
            bg = uniq[counts.argmax()].astype(np.int16)
            dev = np.abs(patch - bg).max(axis=2)          # H×W
            col_lit = (dev > 36).mean(axis=0) > 0.30
            fill_dev = float(col_lit.mean())
            bright_th = max(90, int(bg.max()) + 20)
            col_bright = (patch.max(axis=2) > bright_th).mean(axis=0) > 0.30
            return max(fill_dev, float(col_bright.mean()))
        except Exception:
            return 0.0

    @staticmethod
    def _fill_vs_ref(patch, ref) -> float:
        """和「加载刚开始那一帧」比：有多少列变了（框得只剩条子时的唯一办法）。"""
        try:
            if patch is None or ref is None or patch.shape != ref.shape:
                return 0.0
            dev = np.abs(patch - ref).max(axis=2)
            col = (dev > 36).mean(axis=0) > 0.30
            return float(col.mean())
        except Exception:
            return 0.0

    def wait_progress(self, timeout: float) -> bool:
        """等进度条到 min_fill（默认 90%）；多采几帧取最大值，避免截图半截。"""
        spec = self._progress_spec()
        if not (spec or {}).get("roi"):
            return False
        min_fill = float(spec.get("min_fill", 0.90) or 0.90)
        samples = max(1, int(spec.get("samples", 3) or 1))
        interval = max(0.05, float(spec.get("interval", 0.25) or 0.25))
        poll = max(0.2, float((self.p.get("detect") or {}).get("poll", 0.5)))
        start = self.game.now()
        seen_bar = False
        best = 0.0
        ref_patch = None
        while not self._stopping() and self.game.now() - start < timeout:
            patch = self._progress_patch()
            fill = 0.0
            if patch is not None:
                if ref_patch is None:
                    ref_patch = patch
                static_fill = self._fill_from_patch(patch)
                grow_fill = self._fill_vs_ref(patch, ref_patch)
                fill = max(static_fill, grow_fill)
                if fill > 0.02:
                    seen_bar = True
                # 连采几帧取最大，避免刚好截到半截进度条
                for _ in range(max(0, samples - 1)):
                    self.game.sleep(interval)
                    more_patch = self._progress_patch()
                    if more_patch is not None:
                        fill = max(fill, self._fill_from_patch(more_patch),
                                   self._fill_vs_ref(more_patch, ref_patch))
                best = max(best, fill)
                if fill >= min_fill:
                    self._last_progress_seen = True
                    self.log(f"进度条 {fill * 100:.0f}% ≥ {min_fill * 100:.0f}% → 加载完成")
                    return True
                self.log(f"进度条 {fill * 100:.0f}%…（等加载）")
            self.game.sleep(poll)
        self._last_progress_seen = seen_bar
        if not seen_bar:
            self.log("没看到加载进度条 → 认为没有进入副本")
        else:
            self.log(f"进度条最高只有 {best * 100:.0f}%，没到 "
                     f"{min_fill * 100:.0f}% → 也不算加载完成")
        return False

    def enter_dungeon(self) -> bool:
        """① 点副本入口 → 立刻盯弹窗（出现就点）→ 进度条 ≥90% 才算进本。

        识别不到弹窗时不会傻等：过了 blind_confirm_after 秒就先盲点一次
        「进入游戏」，再看有没有进度条；进度条出来了就说明确实进本了。
        """
        enter = self.p.get("enter") or {}
        farm = self._farm()
        confirm_pt = enter.get("confirm_pt")
        travel = float(enter.get("travel_wait", 40.0))
        timeout = float(enter.get("dialog_timeout", 24.0))
        watch = min(travel, timeout) if timeout > 0 else travel
        load_wait = float(enter.get("load_wait", 15.0))
        blind_after = float(enter.get("blind_confirm_after", 3.0))
        attempts = max(1, int(enter.get("retry", 3) or 1))
        spec = self.enter_dialog_spec() or {}
        poll = max(0.2, float((self.p.get("detect") or {}).get("poll", 0.4)))

        if not enter.get("dungeon_pt"):
            self.log("没有标定「副本入口」，跳过进本这一步")
            return True
        if not confirm_pt:
            self.log("没有标定「进入游戏」按钮，无法确认进本")
            return False

        for attempt in range(1, attempts + 1):
            if self._stopping():
                return False
            radius = float(enter.get("map_radius", 0.02) or 0.02)
            pt = self._offset_point(enter.get("dungeon_pt"), attempt - 1, radius)
            if attempt > 1:
                self.log(f"—— 进本第 {attempt}/{attempts} 次：地图点微调 "
                         f"({pt[0]:.3f}, {pt[1]:.3f}) ——")
            else:
                self.log(f"—— 进本第 {attempt}/{attempts} 次：点副本入口 ——")
            # 点完就开始盯弹窗：寻路过程中就识别，弹窗一出现马上点
            if not self._goto(pt, watch, "副本入口",
                              watch_spec=spec, watch_desc="进入弹窗",
                              sleep_after=False):
                return False
            blind_done = False
            start = self.game.now()
            prev = None
            hits = 0
            best = -1.0
            still_d = float(enter.get("still_diff", 0.02) or 0.02)
            next_log = 0.0
            while not self._stopping() and self.game.now() - start < timeout:
                # 弹窗一出现就点（只在画面相对不动时认，避免走路/动画帧误判）
                if (spec or {}).get("roi"):
                    frame = self.grab()
                    if frame is not None:
                        diff = None if prev is None else self.frame_diff(prev, frame)
                        prev = frame
                        sim, ink = self.anchor_metrics(spec, frame)
                        best = max(best, sim)
                        if diff is not None and diff > still_d:
                            hits = 0
                        elif self.anchor_visible(spec, frame):
                            hits += 1
                            if hits >= 2:
                                self.log(f"识别到进入弹窗（{self.game.now() - start:.1f}s"
                                         f"，相似度 {sim:.2f}）→ 点「进入游戏」")
                                self.game.click_norm(confirm_pt)
                                self.release_focus()
                                break
                        else:
                            hits = 0
                        if self.game.now() - start >= next_log:
                            next_log = self.game.now() - start + 3.0
                            self.log(f"等进入弹窗… 相似度 {sim:.2f} / 占比 {ink:.2f}")
                # 识别不到就早点盲点一次（不傻等 24s）
                if (not blind_done
                        and self.game.now() - start >= blind_after):
                    self.log(f"{blind_after:.0f}s 没识别到弹窗 → 先盲点一次「进入游戏」")
                    self.game.click_norm(confirm_pt)
                    self.release_focus()
                    blind_done = True
                elif blind_done:
                    # 盲点后只要看到进度条出现，就说明弹窗点对了、确实进本了
                    fill = self.progress_fill()
                    if fill is not None and fill > 0.02:
                        self.log(f"盲点后看到加载进度条（{fill * 100:.0f}%）→ 确认进本")
                        break
                self.game.sleep(poll)
            else:
                self.log(f"{timeout:.0f}s 没等到「进入弹窗」→ 回地图重新点副本入口"
                         f"（{attempt}/{attempts}）")
                self.recover_ui("进本超时（可能点歪了，或者地图没开）")
                continue

            # 用进度条验证到底进没进
            self.gap("点进入后缓冲")
            if self.wait_progress(load_wait):
                self.gap("进本完成后缓冲")
                self.release_focus()
                return True
            loaded = self._loaded_spec()
            if (loaded or {}).get("roi") and self.wait_until(loaded, 3.0, "副本内特征"):
                self.gap("进本完成后缓冲")
                self.release_focus()
                return True
            if (bool(enter.get("assume_entered", True))
                    and not getattr(self, "_last_progress_seen", False)):
                self.log("没测到加载进度条 → 但已经点过「进入游戏」，"
                         "先按已进本继续（到 BOSS 点再看结果）")
                return True
            self.log("没看到加载进度条 → 认为没进副本，重新寻路")
        self.log(f"进本失败：{attempts} 次都没能进副本")
        return False

    # -- ② 打怪点：看疲劳决定去 BOSS 还是打怪 --
    def fatigue_limits(self) -> tuple[int, int]:
        """疲劳下限/上限（旧标定只有 threshold 时，按 ±threshold 折算）。"""
        cfg = self.p.get("fatigue") or {}
        low, high = cfg.get("low"), cfg.get("high")
        if low is None or high is None:
            th = abs(int(cfg.get("threshold", 600) or 0))
            if low is None:
                low = -th
            if high is None:
                high = th
        try:
            return int(low), int(high)
        except (TypeError, ValueError):
            return -600, 600

    def next_action_for(self, current, action: Optional[str]) -> Optional[str]:
        """疲劳 → 该不该换分支（滞回，不来回跳）。

        · 正在打小怪：只有 疲劳 < 下限 才换去「打BOSS」，中间区间一直打怪；
        · 正在打BOSS：只有 疲劳 > 上限 才换回「打怪」，中间区间一直打BOSS；
        · 还没定分支（action 为空）：按 action_for_fatigue 选初始分支。
        返回 None = 保持当前分支。
        """
        if current is None:
            return None
        low, high = self.fatigue_limits()
        if action == "farm":
            return "boss" if current < low else None
        if action == "boss":
            return "farm" if current > high else None
        return self.action_for_fatigue(current)

    def action_for_fatigue(self, current) -> str:
        """疲劳值 → 该做哪个：< 下限 去打BOSS；> 上限 去打怪；其余用默认。"""
        loop = self.p.get("loop") or {}
        default = str(loop.get("default_action") or "boss")
        if current is None:
            return default
        low, high = self.fatigue_limits()
        if current < low:
            return "boss"
        if current > high:
            return "farm"
        return default

    def fatigue_value(self):
        """读一次疲劳值，返回当前值（读不到返回 None）。"""
        cfg = self.p.get("fatigue") or {}
        if not cfg.get("enabled", True):
            return None
        current, maximum, msg = self.fatigue_check()
        if current is None:
            self.log(f"疲劳读不到（{msg}）")
            return None
        self.log(f"当前疲劳 {current}/{maximum}")
        return current

    def resolve_mode(self) -> str:
        """联动：现在的模式该是「farm」还是「boss」。

        滞回切换（不会来回跳）：
          · 挂机中：疲劳 < 下限  → 去打 BOSS
          · 打BOSS中：疲劳 > 上限 → 回打怪点挂机
        """
        loop = self.p.get("loop") or {}
        default = str(loop.get("default_action") or "farm")
        low, high = self.fatigue_limits()
        if self._mode is None:
            self._mode = default
        current = self.fatigue_value()
        if current is None:
            self.log(f"按当前模式继续：{self._mode}")
            return self._mode
        if self._mode == "farm" and current < low:
            self._mode = "boss"
            self.log(f"疲劳 {current} < 下限 {low} → 切到打BOSS")
            return "boss"
        if self._mode == "boss" and current > high:
            self._mode = "farm"
            self.log(f"疲劳 {current} > 上限 {high} → 切回打怪点挂机")
            return "farm"
        self.log(f"疲劳 {current}（下限 {low} / 上限 {high}）→ 继续 {self._mode}")
        return self._mode

    def _anchor_of(self, key: str) -> dict:
        return (self._farm().get(key) or {})

    def boss_hp_bar_score(self) -> Optional[float]:
        """BOSS 血条区域的「条带内容」分数（0~1）；没框/截图失败返回 None。"""
        spec = self._anchor_of("boss_hp")
        roi = (spec or {}).get("roi")
        if not roi:
            return None
        frame = self.grab()
        if frame is None:
            return None
        try:
            patch = crop_norm(frame, roi)
            return float(bar_score(patch, (spec or {}).get("band")))
        except Exception:
            return None

    def boss_hp_ink(self) -> Optional[float]:
        """BOSS 血条区域的「内容占比」（和背景不同的像素比例）；没框/截图失败返回 None。

        boss_hp_visible() 里 mode=ink 会调它；之前只调不定义，
        用旧标定（mode=ink）会直接 AttributeError。
        """
        spec = self._anchor_of("boss_hp")
        roi = (spec or {}).get("roi")
        if not roi:
            return None
        frame = self.grab()
        if frame is None:
            return None
        try:
            patch = crop_norm(frame, roi)
            return float(ink_ratio(patch, int(spec.get("tol", 36) or 36)))
        except Exception:
            return None

    def boss_hp_ratio(self) -> Optional[float]:
        """BOSS 血条区域的亮度占比（0~1）；没框/截图失败返回 None。"""
        spec = self._anchor_of("boss_hp")
        roi = (spec or {}).get("roi")
        if not roi:
            return None
        frame = self.grab()
        if frame is None:
            return None
        try:
            patch = crop_norm(frame, roi).astype(np.float32)
            bright = patch.max(axis=2)
            return float((bright > 70).mean())
        except Exception:
            return None

    def _save_patch(self, patch, tag: str) -> str:
        """把一块区域存成 png（排错用：确认程序截到的到底是哪块）。"""
        folder = getattr(self.game, "snapshot_dir", "") or ""
        if not folder:
            return ""
        try:
            from PIL import Image
            os.makedirs(folder, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%H%M%S")
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)[:40]
            path = os.path.join(folder, f"{stamp}_{safe}.png")
            img = np.ascontiguousarray(patch[:, :, :3]).astype(np.uint8)
            Image.fromarray(img).save(path)
            return path
        except Exception as exc:
            self.log(f"区域截图保存失败：{exc}")
            return ""

    def capture_boss_icon(self, samples: int = 3, gap: float = 0.25) -> list:
        """开打时把「你框住的那个 BOSS 血量图标」拍几张当基准。

        存两样东西：
          · 主色（这块区域开打时的颜色）→ 判死看「同色占比」；
          · 结构签名 → 作为补充，形状没变也算还在。
        原因是血条/图标会随掉血、特效一直变，单看形状会一开打就误判。
        """
        spec = self._anchor_of("boss_hp")
        roi = (spec or {}).get("roi")
        self._boss_icon_sigs = []
        self._boss_icon_color = None
        self._icon_checks = 0
        self._icon_miss_shot = False
        self._last_hp_detail = ""
        if not roi:
            self.log("没框「BOSS 血量图标」→ 判死只能按打BOSS上限停"
                     "（到「点位」里框一下打 BOSS 时出现的血量图标）")
            return []
        mode = str((spec or {}).get("mode", "icon") or "icon")
        sim = float((spec or {}).get("sim", 0.75) or 0.75)
        miss = max(1, int((spec or {}).get("miss", 3) or 3))
        min_same = float((spec or {}).get("min_same", 0.35) or 0.35)
        grid = int((spec or {}).get("grid", 12) or 12)
        self.log(f"BOSS 血量图标已加载：x={float(roi[0]):.4f} y={float(roi[1]):.4f} "
                 f"w={float(roi[2]):.4f} h={float(roi[3]):.4f}"
                 f" / 方式 {mode} / 同色≥{min_same * 100:.0f}%"
                 f" 或 相似度≥{sim * 100:.0f}% / miss {miss}")
        sigs: list[list[float]] = []
        patches = []
        for i in range(max(1, int(samples))):
            if self._stopping():
                break
            frame = self.grab()
            if frame is not None:
                try:
                    patch = crop_norm(frame, roi)
                    if not patches:
                        self.log(f"图标区域实际像素：{patch.shape[1]}×{patch.shape[0]}")
                    patches.append(patch)
                    sig = color_signature(patch, grid)
                    if sig and all(color_similarity(sig, old, 30.0) < 0.98
                                   for old in sigs):
                        sigs.append(sig)
                except Exception as exc:
                    self.log(f"图标区域解析失败：{exc}")
            if i + 1 < samples:
                self.game.sleep(max(0.0, float(gap)))
        self._boss_icon_sigs = sigs
        if patches:
            try:
                self._boss_icon_color = np.median(
                    patches[0].reshape(-1, 3), axis=0
                ).astype(np.int16)
            except Exception:
                self._boss_icon_color = None
        if sigs and self._boss_icon_color is not None:
            color = self._boss_icon_color
            base_same = same_color_ratio(
                patches[0], color, int((spec or {}).get("color_tol", 40) or 40))
            self.log(f"BOSS 血量图标基准：主色 RGB"
                     f"({color[0]},{color[1]},{color[2]})"
                     f" / 开打同色 {base_same * 100:.0f}%"
                     f" / 结构样本 {len(sigs)} 张")
            path = self._save_patch(patches[0], "boss_icon_base")
            if path:
                self.log(f"基准截图已存：{path}")
        else:
            self._boss_icon_sigs = []
            self.log("BOSS 血量图标基准没拍到 → 回退到亮度占比判死")
        return self._boss_icon_sigs

    def boss_icon_visible(self) -> bool:
        """框住的 BOSS 血量图标还在不在（和开打时的样子比）。

        两条信号，任一成立就算「还在」：
          1) 和开打主色相同的像素占比 ≥ min_same（默认 35%）；
          2) 结构相似度 ≥ sim（默认 75%）。
        只用第 2 条会出问题：血条掉血/特效会把形状改掉，开打 1 秒就误判死亡。
        """
        spec = self._anchor_of("boss_hp")
        roi = (spec or {}).get("roi")
        sigs = self._boss_icon_sigs
        color = getattr(self, "_boss_icon_color", None)
        if not roi or (not sigs and color is None):
            return True
        frame = self.grab()
        if frame is None:
            return True
        try:
            patch = crop_norm(frame, roi)
        except Exception:
            return True
        color_tol = int((spec or {}).get("color_tol", 40) or 40)
        min_same = float((spec or {}).get("min_same", 0.35) or 0.35)
        sim = float((spec or {}).get("sim", 0.75) or 0.75)
        tol = float((spec or {}).get("tol", 30.0) or 30.0)
        grid = int((spec or {}).get("grid", 12) or 12)

        same = same_color_ratio(patch, color, color_tol)
        best = 0.0
        if sigs:
            try:
                sig = color_signature(patch, grid)
                best = max(color_similarity(sig, ref, tol) for ref in sigs)
            except Exception:
                best = 0.0
        visible = (same >= min_same) or (best >= sim)
        detail = (f"同色 {same * 100:.0f}%（<{min_same * 100:.0f}%）"
                  f" / 相似 {best * 100:.0f}%（<{sim * 100:.0f}%）")
        self._last_hp_detail = detail
        self._last_hp_ratio = max(same, best)
        self._last_hp_need = min_same
        self._last_hp_metric = "同色/相似"

        self._icon_checks = int(getattr(self, "_icon_checks", 0)) + 1
        if self._icon_checks <= 3 or not visible:
            self.log(f"图标比对 {self._icon_checks}：{detail} → "
                     + ("还在" if visible else "不见了"))
        if not visible and not getattr(self, "_icon_miss_shot", False):
            self._icon_miss_shot = True
            path = self._save_patch(patch, "boss_icon_miss")
            if path:
                self.log(f"「不像了」那一刻的截图已存：{path}")
        return visible

    def boss_hp_ocr_text(self) -> str:
        """对「BOSS 血量图标」区域做 OCR（和主程序 BossWatcher 一样）。"""
        spec = self._anchor_of("boss_hp")
        roi = (spec or {}).get("roi")
        if not roi:
            return ""
        frame = self.grab()
        if frame is None:
            return ""
        w, h, data = frame
        try:
            rect = app.roi_from_norm(tuple(float(v) for v in roi), w, h)
            return app.ocr_text_from_bgra(data, w, h, rect)
        except Exception as exc:
            self.log(f"血量 OCR 失败：{type(exc).__name__}: {exc}")
            return ""

    def boss_hp_ocr_visible(self) -> bool:
        """按 OCR 读到的 BOSS 血量判断还在不在。

        返回 False 只表示「这一次没读到」——连续 miss 次才由外层判死，
        这样偶发 OCR 失败不会直接误杀。
        """
        spec = self._anchor_of("boss_hp")
        every = max(0.5, float((spec or {}).get("ocr_every", 2.0) or 2.0))
        now = self.game.now()
        if now < self._hp_ocr_at:
            return self._hp_ocr_visible
        # 多开时 OCR 引擎是共用的：拿不到锁就跳过这一轮，绝不让别的窗口
        # 把本窗口的攻击节奏卡住（跳过不算漏读，不会误判死亡）。
        if not _OCR_LOCK.acquire(blocking=False):
            self._hp_ocr_skips += 1
            self._hp_skipped = True
            if self._hp_ocr_skips == 1:
                self.log("另一个窗口正在 OCR 血量 → 本轮跳过（不挡攻击节奏）")
            return self._hp_ocr_visible
        self._hp_skipped = False
        try:
            self._hp_ocr_at = now + every
            text = self.boss_hp_ocr_text()
        finally:
            _OCR_LOCK.release()
        current, maximum = app.parse_hp_text(text)
        if maximum is not None and (current is None
                                    or maximum < current
                                    or maximum > max(current * 20, 20)):
            maximum = None          # 把等级数字读进最大值了，丢掉
        kill_hp = max(0, int((spec or {}).get("kill_hp", 0) or 0))
        if current is None:
            self._hp_ocr_visible = False
            snippet = "".join(str(text or "").split())[:24]
            self._last_hp_detail = f"OCR 没读到血量（{snippet or '空'}）"
        else:
            first = not self._hp_seen
            self._hp_seen = True
            self._hp_ocr_visible = current > kill_hp
            self._last_hp_detail = (f"血量 {current}/{maximum}" if maximum
                                    else f"血量 {current}")
            if first:
                self.log(f"BOSS 血量 OCR 已生效：{self._last_hp_detail}")
        self._last_hp_ratio = 1.0 if self._hp_ocr_visible else 0.0
        self._last_hp_need = 1.0
        self._last_hp_metric = "血量OCR"
        return self._hp_ocr_visible

    def boss_hp_visible(self) -> bool:
        """BOSS 还在不在（在=没死）。

        默认 mode=ocr：OCR 读「名字 + 当前/最大血量」（主程序 BossWatcher 同款）。
        mode=bright：v3.24.0 tag 的亮像素占比；mode=icon：开打拍基准比图标。
        """
        spec = self._anchor_of("boss_hp")
        if not (spec or {}).get("roi"):
            return True  # 没框血条 → 用时间上限
        mode = str(spec.get("mode", "ocr") or "ocr").lower()
        if mode != "ocr":
            self._hp_skipped = False
        if mode == "ocr":
            return self.boss_hp_ocr_visible()
        if mode == "icon":
            if self._boss_icon_sigs:
                return self.boss_icon_visible()
            mode = "bright"      # 没基准 → 用 tag 的亮度占比兼底
        if mode == "bright":
            ratio = self.boss_hp_ratio()
            if ratio is None:
                return True
            need = float(spec.get("min_fill",
                                  spec.get("min_ink", 0.15)) or 0.15)
            self._last_hp_ratio = ratio
            self._last_hp_need = need
            self._last_hp_metric = "亮度"
            self._last_hp_detail = (f"亮度 {ratio * 100:.0f}%"
                                    f"（<{need * 100:.0f}%）")
            self._last_hp_bright = ratio
            return ratio >= need
        if mode == "ink":
            ink = self.boss_hp_ink()
            if ink is None:
                return True
            need = float(spec.get("min_ink", 0.02) or 0.02)
            self._last_hp_ratio = ink
            self._last_hp_need = need
            self._last_hp_metric = "内容"
            self._last_hp_detail = ""
            return ink >= need
        score = self.boss_hp_bar_score()
        if score is None:
            return True
        need = float(spec.get("min_bar", 0.10) or 0.10)
        self._last_hp_ratio = score
        self._last_hp_need = need
        self._last_hp_metric = "条带"
        self._last_hp_detail = ""
        return score >= need

    def boss_body_visible(self, spec=None, frame=None) -> bool:
        """严格版「BOSS 身形」判定（防止把小怪认成 BOSS）。

        只用相似度，不走 anchor_visible 里「相似度 ≥ 0.60 且区域有内容」的宽松兜底；
        阈值取 max(标定 sim, min_sim=0.75)。调用方还要保证画面静止。
        """
        spec = spec if spec is not None else self._anchor_of("boss_present")
        if not (spec or {}).get("roi"):
            return True
        if frame is None:
            frame = self.grab()
        if frame is None:
            return False
        sim, ink = self.anchor_metrics(spec, frame)
        if sim < 0:
            return False
        try:
            sim_th = max(float(spec.get("sim") or 0.60),
                         float(spec.get("min_sim", 0.75) or 0.75))
        except (TypeError, ValueError):
            sim_th = 0.75
        self._last_body_sim = sim
        self._last_body_ink = ink
        return sim >= sim_th

    def boss_present(self) -> bool:
        """「BOSS 身形」在不在（确认真的站到 BOSS 面前了）。"""
        spec = self._anchor_of("boss_present")
        if not (spec or {}).get("roi"):
            return True  # 没标就默认到了
        frame = self.grab()
        return bool(frame is not None and self.anchor_visible(spec, frame))

    def goto_boss_point(self) -> bool:
        """② 走到副本内 BOSS 点；没识别到「打怪点BOSS」就重新寻路。"""
        farm = self._farm()
        tries = max(1, int(farm.get("boss_retry", 3) or 1))
        travel = float(farm.get("travel_wait", 30.0))
        spec = self._anchor_of("boss_present")
        for attempt in range(1, tries + 1):
            if self._stopping():
                return False
            radius = float(farm.get("map_radius", 0.02) or 0.02)
            pt = self._offset_point(farm.get("boss_pt"), attempt - 1, radius)
            self.log(f"前往 BOSS 点（第 {attempt}/{tries} 次"
                     + (f"，地图点微调 {pt[0]:.3f},{pt[1]:.3f}" if attempt > 1 else "")
                     + "）")
            # 走路时画面一直在动，先不识别；等停下来再看 BOSS
            if not self._goto(pt, travel, "BOSS 点", sleep_after=False):
                return False
            if not (spec or {}).get("roi"):
                self.game.sleep(travel)
                return True
            self.release_focus()
            self.log("等角色停下（画面不动了再识别 BOSS 身形/小鹿）")
            if self.wait_anchor_still(
                    spec, 10.0, "BOSS 身形", need=3,
                    matcher=self.boss_body_visible,
                    still_seconds=float(spec.get("still_seconds", 0.8) or 0.8)):
                return True
            self.log("没识别到「BOSS 身形」→ 回地图重新点 BOSS 点")
            self.recover_ui("到点后没看到 BOSS（可能走歪了）")
        self.log(f"BOSS 点确认失败：{tries} 次都没识别到 BOSS 身形")
        return False

    def fight_until_finish(self) -> bool:
        """③ 挂机打怪：每次平A都拾取；BOSS 血量图标消失（连续 N 次看不到）= 打完。"""
        farm = self._farm()
        loop = self.p.get("loop") or {}
        seconds = min(1000.0,
                      float(loop.get("boss_seconds", 120.0) or 120.0))
        interval = max(0.05, float(farm.get("interval_ms", 250)) / 1000.0)
        pickup_key = str(farm.get("pickup_key") or self._key("pickup", "space"))
        pickup_on_a = bool(farm.get("pickup_on_a", True))
        pickup_every = float(farm.get("pickup_every", 8.0) or 0.0)
        potion_every = float(farm.get("potion_every", 6.0) or 0.0)
        potion_keys = [str(k) for k in (farm.get("potion_keys") or ["q"]) if str(k)]
        hp_threshold = int(farm.get("hp_threshold", 0) or 0)
        hp_every = float(farm.get("hp_check_every", 2.0) or 2.0)
        use_skills = bool(farm.get("use_skills", True))
        timer_on = bool(farm.get("potion_timer_enabled", False))
        timer_every = float(farm.get("potion_timer_every", 60.0) or 0.0)
        self._hp_ocr_logged = False
        hp_spec = self._anchor_of("boss_hp")
        need_miss = max(1, int((hp_spec or {}).get("miss", 3) or 3))
        poll = max(0.2, float((self.p.get("detect") or {}).get("poll", 0.5)))
        loop_fat = float(loop.get("fatigue_every", 90.0) or 0.0)
        # 只有开了「打怪点 ↔ 打BOSS 联动」才在 BOSS 战里查疲劳，
        # 否则查了也没地方可切，只会白白按一次疲劳图标。
        watch_fatigue = (
            bool(loop.get("link_fatigue", True))
            and loop_fat > 0
            and bool((self.p.get("fatigue") or {}).get("enabled", True))
        )

        self.release_focus()
        self.log(f"开始打怪（上限 {seconds:.0f}s；"
                 + ("BOSS 血量图标没了就算打完）" if (hp_spec or {}).get("roi")
                    else "没框 BOSS 血量图标，只能按时间停）"))
        self._hp_seen = False
        self._hp_ocr_at = -1.0
        self._hp_ocr_visible = True
        self._hp_ocr_skips = 0
        hp_mode = str((hp_spec or {}).get("mode", "ocr") or "ocr").lower()
        self._hp_mode = hp_mode
        self.restore_user_foreground()
        self.log("打BOSS：A/技能/拾取全部后台 PostMessage 发送"
                 "（窗口不用在前台）")
        if hp_threshold > 0 and farm.get("hp_roi"):
            self.log(f"自动喝药：血量 < {hp_threshold} → 每 {potion_every:.0f}s 按"
                     f"「{'、'.join(potion_keys)}」，血够了就停")
        elif hp_threshold > 0:
            self.log("自动喝药：填了「血量阈值」但没点「框血量区域」→ "
                     "不会按血量喝，只能按固定间隔")
        if timer_on and timer_every > 0:
            self.log(f"定时喝药：开打先按一次「{'、'.join(potion_keys)}」，之后每 "
                     f"{timer_every:.0f}s 一次")
        if (hp_spec or {}).get("roi"):
            if hp_mode == "icon":
                self.capture_boss_icon()
            elif hp_mode == "ocr":
                self.log(f"判死方式：OCR 读 BOSS 血量"
                         f"（每 {float((hp_spec or {}).get('ocr_every', 2.0) or 2.0):.1f}s 读一次，"
                         f"连续 {need_miss} 次读不到 = 打完）")
            else:
                self.log(f"判死方式：亮像素占比（v3.24.0 tag 同款）"
                         f" / 阈值 {float((hp_spec or {}).get('min_fill', 0.15) or 0.15) * 100:.0f}%"
                         f" / miss {need_miss}")
        start = self.game.now()
        next_s1 = next_s2 = 0.0
        next_potion = next_hp = next_fatigue = next_check = 0.0
        next_beat = 0.0
        next_pickup = pickup_every
        next_timer_potion = 0.0
        hp_low = False
        hp_low_logged = False
        hp_miss = 0
        hp_fallback_logged = False
        misses = 0
        while not self._stopping():
            elapsed = self.game.now() - start
            if elapsed >= seconds:
                self.log(f"打怪到上限 {seconds:.0f}s（血条一直没消失）")
                break
            self.game.tap("a")
            if pickup_on_a:
                self.game.tap(pickup_key)
            if use_skills:
                if elapsed >= next_s1:
                    self.game.tap(str(farm.get("skill1", "1")))
                    next_s1 = elapsed + float(farm.get("skill1_every", 12.0))
                if elapsed >= next_s2:
                    self.game.tap(str(farm.get("skill2", "2")))
                    next_s2 = elapsed + float(farm.get("skill2_every", 6.0))

            if hp_threshold > 0 and farm.get("hp_roi"):
                if elapsed >= next_hp:
                    current = self.hp_now()
                    next_hp = elapsed + hp_every
                    if current is not None:
                        hp_miss = 0
                        hp_low = current < hp_threshold
                        if hp_low and not hp_low_logged:
                            hp_low_logged = True
                            self.log(f"血量 {current} < {hp_threshold} → 喝药"
                                     f"「{'、'.join(potion_keys)}」"
                                     f"（每 {potion_every:.0f}s 一次，"
                                     "血够了就停）")
                        elif not hp_low:
                            hp_low_logged = False
                    else:
                        hp_miss += 1
                        if hp_miss >= 3:
                            hp_low = True
                            if not hp_fallback_logged:
                                hp_fallback_logged = True
                                self.log("血量连续 3 次读不到（可能别的窗口在 OCR）"
                                         "→ 先按固定间隔喝药兜底")
            else:
                hp_low = True
            if hp_low and potion_every > 0 and elapsed >= next_potion:
                for key in potion_keys:
                    self.game.tap(key)
                next_potion = elapsed + potion_every

            # 定时喝药（不管血量）：每 N 秒按一次
            if (timer_on and timer_every > 0 and potion_keys
                    and elapsed >= next_timer_potion):
                for key in potion_keys:
                    self.game.tap(key)
                next_timer_potion = elapsed + timer_every

            if not pickup_on_a and pickup_every > 0 and elapsed >= next_pickup:
                self.game.tap(pickup_key)
                next_pickup = elapsed + pickup_every

            if (hp_spec or {}).get("roi") and elapsed >= next_check:
                next_check = elapsed + poll
                grace = float((hp_spec or {}).get("grace", 6.0) or 0.0)
                if (getattr(self, "_hp_mode", "") == "ocr"
                        and not self._hp_seen and elapsed < grace):
                    # 刚开打血条可能还没显示出来，先给个宽限期，别急着判死
                    visible = True
                    self._last_hp_detail = f"等 BOSS 血量出现（宽限 {grace:.0f}s）"
                else:
                    visible = self.boss_hp_visible()
                # 每 10 秒打一行「全部指标」，BOSS 死的时候到底哪个数变了，
                # 看这一行就能判断，不用猜。
                if elapsed >= next_beat:
                    next_beat = elapsed + 10.0
                    body_spec = self._anchor_of("boss_present")
                    pres = ""
                    if (body_spec or {}).get("roi"):
                        bf = self.grab()
                        if bf is not None:
                            sim_b, _ink_b = self.anchor_metrics(body_spec, bf)
                            pres = f" / BOSS身形相似 {sim_b:.2f}"
                    self.log(f"血量监测（{elapsed:.0f}s）："
                             f"{getattr(self, '_last_hp_detail', '')}{pres}")
                if visible:
                    misses = 0
                elif getattr(self, "_hp_skipped", False):
                    # 本轮是"别的窗口在 OCR"跳过的，既不算漏读也不清零
                    pass
                else:
                    misses += 1
                    ratio = getattr(self, "_last_hp_ratio", None)
                    need = getattr(self, "_last_hp_need", None)
                    detail = getattr(self, "_last_hp_detail", "")
                    extra = ""
                    if detail:
                        extra = f"（{detail}）"
                    elif ratio is not None and need is not None:
                        metric = getattr(self, "_last_hp_metric", "亮度")
                        extra = (f"（{metric} {ratio * 100:.0f}% < "
                                 f"{need * 100:.0f}%）")
                    self.log(f"BOSS 血量图标不见了（{misses}/{need_miss}）{extra}")
                    if misses >= need_miss:
                        self.log(f"BOSS 血量图标连续 {misses} 次不见 → 判定打完")
                        try:
                            self.game.snapshot("boss_dead")
                        except Exception:
                            pass
                        break

            if watch_fatigue and elapsed >= next_fatigue:
                next_fatigue = elapsed + loop_fat
                current, maximum, _msg = self.fatigue_check(keep_attacking=True)
                if current is not None:
                    low, high = self.fatigue_limits()
                    self.log(f"打怪中查疲劳 {current}/{maximum}"
                             f"（下限 {low} / 上限 {high}）")
                    if current > high:
                        # 涨到上限：这轮 BOSS 收尾（拾取 → 退本），下一轮回打怪点
                        self.log(f"疲劳 {current} > 上限 {high} → 这轮 BOSS 收尾，"
                                 "下一轮回打怪点打小怪")
                        break

            self.game.sleep(interval)
        return True

    def run_loops(self) -> bool:
        """②③ 走到 BOSS 点 → 打怪到完成。"""
        if not self.goto_boss_point():
            return False
        return self.fight_until_finish()

    def farm_round(self) -> bool:
        """挂机：在「打怪点」一直打，每 fatigue_every 秒查一次疲劳。

        · 不设挂机总时长；疲劳 < -阈值 才结束挂机（下一轮自动去打 BOSS）
        · 疲劳在中间区间就一直打；> +阈值 也继续打（本来就在打小怪）
        """
        farm = self._farm()
        loop = self.p.get("loop") or {}
        every = max(10.0, float(loop.get("fatigue_every", 90.0) or 90.0))
        if not farm.get("farm_pt"):
            self.log("没标「打怪点」，没法挂机（请到「副本流程」页标一下）")
            return False
        low, high = self.fatigue_limits()
        self.log(f"—— 打怪点挂机（不设时长；每 {every:.0f}s 查一次疲劳，"
                 f"疲劳 < 下限 {low} 就去打BOSS）——")
        if not self._goto(farm.get("farm_pt"),
                          float(farm.get("travel_wait", 30.0)), "打怪点"):
            return False
        self.gap("到打怪点后缓冲")
        self.release_focus()
        self.restore_user_foreground()
        self.log("打怪点挂机：A/技能/拾取全部后台 PostMessage 发送"
                 "（窗口不用在前台，鼠标也不用在游戏里）")
        while not self._stopping():
            self.combat(every, desc="打怪点挂机", action="farm")
            if self._stopping():
                return True
            if self._switch_to == "boss":
                self._switch_to = None
                self._mode = "boss"
                self.log(f"疲劳 < 下限 {low} → 切到打BOSS（结束挂机）")
                return True
        return True

    def combat(self, seconds: float, desc: str = "挂机",
               action: Optional[str] = None) -> bool:
        """通用挂机：平A + 技能 + 血量低喝药 + 定时拾取。

        action 不为空时，每 loop.fatigue_every 秒查一次疲劳，
        发现该换分支（打BOSS <-> 打怪）就提前结束本次挂机。
        """
        self._switch_to = None
        loop = self.p.get("loop") or {}
        fatigue_every = float(loop.get("fatigue_every", 90.0) or 0.0)
        fatigue_cfg = self.p.get("fatigue") or {}
        watch = bool(
            action and fatigue_every > 0 and fatigue_cfg.get("enabled", True)
        )
        next_fatigue = fatigue_every
        farm = self._farm()
        interval = max(0.05, float(farm.get("interval_ms", 250)) / 1000.0)
        pickup_key = str(farm.get("pickup_key") or self._key("pickup", "space"))
        pickup_every = float(farm.get("pickup_every", 8.0) or 0.0)
        potion_every = float(farm.get("potion_every", 6.0) or 0.0)
        potion_keys = [str(k) for k in (farm.get("potion_keys") or ["q"]) if str(k)]
        hp_threshold = int(farm.get("hp_threshold", 0) or 0)
        hp_every = float(farm.get("hp_check_every", 2.0) or 2.0)
        use_skills = bool(farm.get("use_skills", True))
        timer_on = bool(farm.get("potion_timer_enabled", False))
        timer_every = float(farm.get("potion_timer_every", 60.0) or 0.0)

        self._hp_ocr_logged = False
        self.release_focus()
        self.log(f"{desc}：{seconds:.0f}s（喝药 {potion_every:.0f}s / "
                 f"拾取 {pickup_every:.0f}s）")
        if hp_threshold > 0 and farm.get("hp_roi"):
            self.log(f"自动喝药：血量 < {hp_threshold} → 每 {potion_every:.0f}s 按"
                     f"「{'、'.join(potion_keys)}」，血够了就停")
        elif hp_threshold > 0:
            self.log("自动喝药：填了「血量阈值」但没点「框血量区域」→ "
                     "不会按血量喝，只能按固定间隔")
        if timer_on and timer_every > 0:
            self.log(f"定时喝药：开打先按一次「{'、'.join(potion_keys)}」，之后每 "
                     f"{timer_every:.0f}s 一次")
        start = self.game.now()
        next_s1 = next_s2 = 0.0
        next_potion = 0.0
        next_pickup = pickup_every
        next_hp_check = 0.0
        next_timer_potion = 0.0
        hp_low = False
        hp_low_logged = False
        hp_miss = 0
        hp_fallback_logged = False

        pickup_on_a = bool(farm.get("pickup_on_a", True))
        bg_warned = False
        while not self._stopping():
            elapsed = self.game.now() - start
            # 先查疲劳：farm_round 里 seconds 可能正好等于 fatigue_every，
            # 如果先判断「到点结束」就永远查不到疲劳，会死在挂机循环里。
            if watch and elapsed >= next_fatigue:
                next_fatigue = elapsed + fatigue_every
                current, maximum, msg = self.fatigue_check(keep_attacking=True)
                if current is None:
                    self.log(f"疲劳读不到（{msg}），继续{desc}")
                else:
                    low, high = self.fatigue_limits()
                    want = self.next_action_for(current, action)
                    self.log(f"{desc}中查疲劳 {current}/{maximum}"
                             f"（下限 {low} / 上限 {high}）"
                             + (" → 该换分支了："
                                + ("打BOSS" if want == "boss" else "打怪")
                                if want else " → 继续" + desc))
                    if want:
                        self._switch_to = want
                        break
            if elapsed >= seconds:
                break
            if not self.game.tap("a") and not bg_warned:
                bg_warned = True
                self.log("后台平A发送失败：点「测试后台平A」看目标窗口；"
                         "游戏要用管理员运行，窗口别最小化")
            if pickup_on_a:
                self.game.tap(pickup_key)
            if use_skills:
                if elapsed >= next_s1:
                    self.game.tap(str(farm.get("skill1", "1")))
                    next_s1 = elapsed + float(farm.get("skill1_every", 12.0))
                if elapsed >= next_s2:
                    self.game.tap(str(farm.get("skill2", "2")))
                    next_s2 = elapsed + float(farm.get("skill2_every", 6.0))

            if hp_threshold > 0 and farm.get("hp_roi"):
                if elapsed >= next_hp_check:
                    current = self.hp_now()
                    next_hp_check = elapsed + hp_every
                    if current is not None:
                        hp_miss = 0
                        hp_low = current < hp_threshold
                        if hp_low and not hp_low_logged:
                            hp_low_logged = True
                            self.log(f"血量 {current} < {hp_threshold} → 喝药"
                                     f"「{'、'.join(potion_keys)}」"
                                     f"（每 {potion_every:.0f}s 一次，"
                                     "血够了就停）")
                        elif not hp_low:
                            hp_low_logged = False
                    else:
                        hp_miss += 1
                        if hp_miss >= 3:
                            hp_low = True      # 读不到就按固定间隔喝，别见死不救
                            if not hp_fallback_logged:
                                hp_fallback_logged = True
                                self.log("血量连续 3 次读不到（可能别的窗口在 OCR）"
                                         "→ 先按固定间隔喝药兜底")
            else:
                hp_low = True  # 没框血量就按固定间隔喝

            if hp_low and potion_every > 0 and elapsed >= next_potion:
                for key in potion_keys:
                    self.game.tap(key)
                next_potion = elapsed + potion_every

            # 定时喝药（不管血量）：每 N 秒按一次
            if (timer_on and timer_every > 0 and potion_keys
                    and elapsed >= next_timer_potion):
                for key in potion_keys:
                    self.game.tap(key)
                next_timer_potion = elapsed + timer_every

            if pickup_every > 0 and elapsed >= next_pickup:
                self.game.tap(pickup_key)
                next_pickup = elapsed + pickup_every

            self.game.sleep(interval)

        self.log(f"{desc}：结束（{self.game.now() - start:.0f}s）")
        return True

    # -- 疲劳检测（退出副本后） --
    def fatigue_roi(self):
        cfg = self.p.get("fatigue") or {}
        roi = cfg.get("roi")
        if roi:
            return roi
        try:
            return app.load_fatigue_roi_norm()
        except Exception:
            return None

    def fatigue_check(self, keep_attacking: bool = False):
        """点住疲劳图标 → 等提示框弹出来 → OCR 读「当前疲劳值」。

        返回 (当前, 最大, 说明)。要点：
          · 点完**光标停在图标上不还原**（hold_seconds，默认 3 秒），
            否则鼠标一移开游戏就把提示框收起来，OCR 永远读到空的；
          · 这 3 秒里每 poll 秒截一次图，读到就提前结束；
          · 还是读不到就再点一次（retry，默认 2 次）。
        """
        roi = self.fatigue_roi()
        if not roi:
            return None, None, "没有疲劳区域（主界面也没框过）"
        cfg = self.p.get("fatigue") or {}
        pt = cfg.get("pt")
        if not pt:
            pt = [
                float(roi[0]) + float(roi[2]) / 2.0,
                float(roi[1]) + float(roi[3]) / 2.0,
            ]
        hold = max(0.5, float(cfg.get("hold_seconds", 3.0) or 3.0))
        poll = max(0.1, float(cfg.get("poll", 0.3) or 0.3))
        tries = max(1, int(cfg.get("retry", 2) or 2))
        farm = self._farm()
        pickup_key = str(farm.get("pickup_key") or self._key("pickup", "space"))
        pickup_on_a = bool(farm.get("pickup_on_a", True))
        last_msg = "没读到疲劳"
        for attempt in range(1, tries + 1):
            if self._stopping():
                break
            try:
                clicked = self.game.click_norm(pt, keep_cursor=True)
            except TypeError:          # 兼容没有 keep_cursor 的假窗口/旧实现
                clicked = self.game.click_norm(pt)
            if not clicked:
                last_msg = "点疲劳图标失败"
            end = self.game.now() + hold
            got = None
            while not self._stopping() and self.game.now() < end:
                self.game.sleep(poll)
                # 读疲劳的这几秒别站着发呆：继续后台平A（+拾取）
                if keep_attacking:
                    self.game.tap("a")
                    if pickup_on_a:
                        self.game.tap(pickup_key)
                frame = self.grab()
                if frame is None:
                    last_msg = "截图失败"
                    continue
                w, h, data = frame
                try:
                    rect = app.roi_from_norm(tuple(float(v) for v in roi), w, h)
                    if _OCR_LOCK.acquire(timeout=2.0):
                        try:
                            cur, mx, msg = app.ocr_fatigue_from_bgra(
                                data, w, h, rect)
                        finally:
                            _OCR_LOCK.release()
                    else:
                        cur, mx, msg = None, None, "OCR 正忙（别的窗口）"
                except Exception as exc:
                    cur, mx, msg = (None, None,
                                    f"疲劳识别异常：{type(exc).__name__}: {exc}")
                last_msg = msg
                if cur is not None:
                    got = (cur, mx, msg)
                    break
            # 松开：光标/前台还给你
            fn = getattr(self.game, "release_hold", None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
            if got:
                if attempt > 1 or self.verbose:
                    self.log(f"疲劳：第 {attempt} 次点击读到了 {got[0]}/{got[1]}")
                return got
            if attempt < tries:
                self.log(f"疲劳：点住 {hold:.0f}s 没读到（{last_msg}）→ 再点一次")
                self.game.sleep(0.4)
        return None, None, last_msg

    # -- ③ 退出副本 --
    def loot_before_exit(self) -> None:
        """打完 BOSS 再捡一轮，然后缓冲 1~2 秒（按 action_gap）。"""
        farm = self._farm()
        key = str(farm.get("pickup_key") or self._key("pickup", "space"))
        times = max(0, int(farm.get("pickup_times", 3) or 0))
        if times:
            self.log(f"打完再拾取 {times} 次")
            for _ in range(times):
                if self._stopping():
                    return
                self.game.tap(key)
                self.game.sleep(0.4)
        self.gap("拾取后缓冲")

    def deposit_to_warehouse(self) -> bool:
        """一键放入仓库：点仓库图标 → 点「一键放入」→ 按 2 下 ESC 回主界面。"""
        cfg = self.p.get("warehouse") or {}
        if not cfg.get("enabled", True):
            return True
        icon = cfg.get("icon_pt")
        deposit = cfg.get("deposit_pt")
        if not icon or not deposit:
            self.log("没标「仓库图标 / 一键放入」点位，跳过放仓库")
            return True
        gap = max(0.0, float(cfg.get("gap", 0.5) or 0.0))
        esc_times = max(0, int(cfg.get("esc_times", 2) or 0))
        open_wait = max(0.0, float(cfg.get("open_wait", 1.5) or 0.0))

        self.log("—— 一键放入仓库 ——")
        self.log("点「仓库图标」打开仓库")
        if not self.game.click_norm(icon):
            self.log("点仓库图标失败，跳过放仓库")
            return False
        self.game.sleep(gap + open_wait)
        self.log("点「一键放入」")
        if not self.game.click_norm(deposit):
            self.log("点「一键放入」失败，跳过")
            return False
        self.game.sleep(gap)
        for i in range(esc_times):
            self.log(f"按 ESC 回主界面（{i + 1}/{esc_times}）")
            self.game.tap("esc")
            self.game.sleep(gap)
        self.release_focus()
        return True

    def exit_dungeon(self) -> bool:
        """③ 退出：点「退出副本」→ 识别独立的「退出确认弹窗」→ 点「确认」。"""
        exit_cfg = self.p.get("exit") or {}
        exit_pt = exit_cfg.get("exit_pt")
        confirm_pt = exit_cfg.get("confirm_pt")
        spec = self.exit_dialog_spec()
        timeout = float(exit_cfg.get("dialog_timeout", 10.0))
        attempts = max(1, int(exit_cfg.get("retry", 3) or 1))
        exit_wait = float(exit_cfg.get("exit_wait", 1.0))
        confirm_wait = float(exit_cfg.get("confirm_wait", 1.0))
        settle = float(exit_cfg.get("settle", 10.0))
        poll = max(0.2, float((self.p.get("detect") or {}).get("poll", 0.5)))

        if not exit_pt:
            self.log("没有标定「退出副本」按钮，跳过退出")
            return False
        if not confirm_pt:
            self.log("没有标定「确认」按钮，退出可能卡在弹窗上")
            return False

        for attempt in range(1, attempts + 1):
            if self._stopping():
                return False
            self.log(f"退出副本第 {attempt}/{attempts} 次：点「退出副本」")
            self.game.click_norm(exit_pt)
            self.game.sleep(exit_wait)
            if not spec.get("roi"):
                # 没框退出确认弹窗区域：直接盲点确认（兼容老标定）
                self.log("没标定「退出确认弹窗区域」→ 直接点「确认」（盲点）")
                self.game.click_norm(confirm_pt)
                self.release_focus()
                self.game.sleep(confirm_wait)
                self.game.sleep(settle)
                return True
            if self.wait_anchor(spec, timeout, poll, "退出确认弹窗"):
                self.log("点「确认」→ 等退出加载")
                self.game.click_norm(confirm_pt)
                self.release_focus()
                self.game.sleep(confirm_wait)
                self.game.sleep(settle)
                return True
            self.log(f"没识别到退出确认弹窗（等了 {timeout:.0f}s），再点一次「退出副本」")
        self.log(f"退出失败：{attempts} 次都没识别到退出确认弹窗")
        return False

    # -- 一轮 --
    def run_round(self, seconds: Optional[float] = None) -> bool:
        """一轮：按疲劳决定【打怪点挂机】还是【打BOSS副本】。"""
        self.log("=== 开始一轮 ===")
        # 只有「前台模式 + 允许抢占」才需要一开始就切前台；
        # 混合模式只在真正点击时借一下鼠标，点完马上还回去。
        try:
            mode_name = self.game.input_mode()
        except Exception:
            mode_name = "hybrid"
        # 记下你现在用的窗口：点击时借了前台，打完/走到位就还给你
        pin = getattr(self.game, "pin_user_foreground", None)
        if callable(pin):
            try:
                pin()
            except Exception:
                pass
        if mode_name == "foreground" and getattr(self.game, "allow_grab", lambda: False)():
            self.log("前台模式：点击时用真实鼠标（点完会把前台还给你，"
                     "打怪/打BOSS的按键都走后台）")
        loop = self.p.get("loop") or {}
        if bool(loop.get("link_fatigue", True)):
            mode = self.resolve_mode()
        else:
            mode = "boss"
        if mode == "farm":
            self._mode = "farm"
            if not self.farm_round():
                return self._failed("打怪点挂机失败")
            return True
        self._mode = "boss"
        return self.boss_round(seconds)

    def boss_round(self, seconds: Optional[float] = None) -> bool:
        """一次 BOSS 副本：进本 → BOSS 点 → 打怪 → 拾取 → 退出。

        勾了「引路宏」时，进本 + 跑到 BOSS 这段改播放录制的鼠标宏，
        不再依赖地图点位；宏跑完若没看到 BOSS 身形，可选择回退到地图点位。
        """
        loop_cfg = self.p.get("loop") or {}
        kills = max(1, min(9, int(loop_cfg.get("boss_count", 1) or 1)))
        if kills > 1:
            self.log(f"本副本要打 {kills} 只 BOSS（分身怪）")
        if self.nav_macro_enabled():
            self.log("用引路宏导航（鼠标宏）：进本/跑向 BOSS 不再点地图")
            if not self.play_nav_macro():
                self.log("引路宏中途失败：角色可能停在副本里，"
                         "下一轮开始前请先确认回到打怪点")
                return self._failed("引路宏回放失败")
            spec = self._anchor_of("boss_present")
            if (spec or {}).get("roi"):
                self.release_focus()
                self.log("引路宏跑完，等角色停下再识别 BOSS")
                if self.wait_anchor_still(
                        spec, 12.0, "BOSS 身形", need=3,
                        matcher=self.boss_body_visible,
                        still_seconds=float(
                            spec.get("still_seconds", 0.8) or 0.8)):
                    if not self._fight_clones(kills, at_boss=True):
                        return self._failed("BOSS 点打怪失败")
                elif self.nav_macro().get("fallback", False):
                    self.log("引路宏没识别到 BOSS 身形 → 回退到地图点位")
                    if not self.enter_dungeon():
                        return self._failed("进副本失败")
                    if not self._fight_clones(kills):
                        return self._failed("BOSS 点打怪失败")
                else:
                    # 宏跑完人已经在副本里：先兜底退出去，
                    # 否则下一轮又会从打怪点开始放宏，全程点错。
                    self.log("引路宏没到 BOSS → 先兜底退出副本，"
                             "避免下一轮卡在副本里（可勾「宏失败回退地图点位」）")
                    self.loot_before_exit()
                    if not self.exit_dungeon():
                        self.log("兜底退出副本也失败，下一轮可能需要手动处理")
                    self.gap("兜底退出后缓冲")
                    return self._failed("引路宏没到 BOSS（已尝试退出副本）")
            else:
                self.log("没框 BOSS 身形 → 引路宏跑完直接开打")
                if not self._fight_clones(kills, at_boss=True):
                    return self._failed("BOSS 点打怪失败")
        else:
            if not self.enter_dungeon():
                return self._failed("进副本失败")
            if not self._fight_clones(kills):
                return self._failed("BOSS 点打怪失败")
        self.loot_before_exit()
        if not self.exit_dungeon():
            return self._failed("退出副本失败")
        self.gap("退出后缓冲")
        self.release_focus()
        self.restore_user_foreground()
        wh = self.p.get("warehouse") or {}
        if wh.get("enabled", True) and str(wh.get("after", "exit")) == "exit":
            if not self.deposit_to_warehouse():
                return self._failed("放入仓库失败")
        return True

    def _fight_clones(self, kills: int, at_boss: bool = False) -> bool:
        """打 kills 只 BOSS（分身怪要打完 3 只才算通过副本）。"""
        kills = max(1, min(9, int(kills or 1)))
        for i in range(1, kills + 1):
            if self._stopping():
                return False
            if kills > 1:
                self.log(f"—— BOSS 分身 {i}/{kills} ——")
            if i == 1 and at_boss:
                ok = self.fight_until_finish()
            else:
                ok = self.run_loops()      # 回地图重新点 BOSS 点，再打下一只
            if not ok:
                return False
        return True

    def _failed(self, reason: str) -> bool:
        self.log(f"本轮失败：{reason}")
        try:
            self.game.snapshot(f"fail_{reason}")
        except Exception:
            pass
        return False

    def run(self, rounds: Optional[int] = None,
            seconds: Optional[float] = None) -> int:
        retry = self.p.get("retry") or {}
        total = int(rounds if rounds is not None else retry.get("rounds", 0))
        gap = float(retry.get("gap", 8.0))
        max_fail = max(1, int(retry.get("max_fail", 3)))
        done = 0
        self.log(f"开始循环：轮数={total or '无限'} 间隔={gap:.0f}s")
        while not self._stopping():
            if total and done >= total:
                break
            ok = self.run_round(seconds)
            done += 1
            if ok:
                self.stats["ok"] += 1
                self.stats["failed_streak"] = 0
                self.log(f"第 {done} 轮完成 ✓")
            else:
                self.stats["fail"] += 1
                self.stats["failed_streak"] += 1
                if self.stats["failed_streak"] >= max_fail:
                    self.log("连续失败，已停止")
                    break
            if total and done >= total:
                break
            if self._sleep_with_stop(gap):
                break
        self.log(f"结束：成功 {self.stats['ok']} 轮 / 失败 {self.stats['fail']} 轮")
        return self.stats["ok"]

    def _sleep_with_stop(self, seconds: float) -> bool:
        end = self.game.now() + max(0.0, seconds)
        while self.game.now() < end:
            if self._stopping():
                return True
            self.game.sleep(min(0.5, max(0.05, end - self.game.now())))
        return self._stopping()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def list_windows(keyword: str = "") -> list[tuple[int, str]]:
    """列出候选游戏窗口。

    会排除：本程序自己的窗口（标题含“后台按键器”、或属于本进程）、
    没有标题的窗口、不可见窗口。
    """
    import os as _os

    my_pid = _os.getpid()
    found: list[tuple[int, str]] = []

    def cb(hwnd, _param):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        if "后台按键器" in title:
            return True
        try:
            pid = wt.DWORD()
            _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) == my_pid:
                return True
        except Exception:
            pass
        if keyword and keyword.lower() not in title.lower():
            return True
        found.append((int(hwnd), title))
        return True

    win32gui.EnumWindows(cb, None)
    return found


def find_window(keyword: str) -> Optional[int]:
    wins = list_windows(keyword)
    if not wins:
        return None
    wins.sort(key=lambda item: len(item[1]))
    return wins[0][0]


def snapshot_dir() -> str:
    folder = os.path.join(app._data_folder(), SNAPSHOT_DIR_NAME)
    os.makedirs(folder, exist_ok=True)
    return folder


def probe(hwnd: int, profile: dict, seconds: float = 0.0) -> int:
    game = WinGame(hwnd, profile, dry=True)
    runner = DungeonRunner(game, profile, verbose=True)
    if ((profile.get("farm") or {}).get("boss_hp") or {}).get("roi"):
        # 先拿当前画面当基准，这样下面能显示「图标相似度」
        runner.capture_boss_icon(samples=1, gap=0.0)
    cap = game.grab()
    if cap is None:
        print(f"后台截图失败：{game.last_error}")
        return 1
    print(f"后台截图正常：{cap[0]}x{cap[1]}")
    start = time.monotonic()
    try:
        while True:
            info = runner.probe()
            print(" ".join(f"{k}={v}" for k, v in info.items()) or "(还没标定判定区域)")
            if seconds and time.monotonic() - start >= seconds:
                break
            time.sleep(max(0.2, float((profile.get("detect") or {}).get("poll", 0.5))))
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="副本流程：地图1 进本 → 地图2 BOSS 点挂机 → 退出"
    )
    parser.add_argument("--list", action="store_true", help="列出窗口")
    parser.add_argument("--window", default="", help="窗口标题关键字")
    parser.add_argument("--hwnd", type=lambda v: int(v, 0), default=0)
    parser.add_argument("--profile", default="", help="标定文件路径")
    parser.add_argument("--run", action="store_true", help="开始跑")
    parser.add_argument("--probe", action="store_true", help="只看判定数值（不发按键）")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="--probe 时打印多少秒")
    parser.add_argument("--rounds", type=int, default=None, help="跑几轮")
    parser.add_argument("--farm-seconds", "--boss-seconds", dest="boss_seconds",
                        type=float, default=None,
                        help="覆盖 BOSS 战上限（秒，最大 1000；不是挂机时长）")
    parser.add_argument("--dry", action="store_true", help="模拟跑：不发真实按键")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        wins = list_windows(args.window)
        if not wins:
            print("没有找到窗口")
            return 1
        for hwnd, title in wins:
            print(f"0x{hwnd:08X}  {title}")
        return 0

    hwnd = int(args.hwnd or 0)
    if not hwnd:
        keyword = args.window or ""
        if not keyword:
            print("请用 --window 指定窗口标题关键字，或 --hwnd 指定句柄")
            return 2
        hwnd = find_window(keyword) or 0
        if not hwnd:
            print(f"没有找到标题包含「{keyword}」的窗口")
            return 1
    if not win32gui.IsWindow(hwnd):
        print("窗口句柄无效")
        return 1

    path = args.profile or profile_path()
    profile = load_profile(path)
    print(f"窗口：0x{hwnd:X} {win32gui.GetWindowText(hwnd)}")
    print(f"标定文件：{path}")

    if args.probe:
        return probe(hwnd, profile, args.seconds)

    if args.run:
        game = WinGame(hwnd, profile, dry=args.dry, snapshot_dir=snapshot_dir())
        runner = DungeonRunner(game, profile, verbose=args.verbose)
        try:
            runner.run(args.rounds, args.boss_seconds)
        except KeyboardInterrupt:
            print("\n已中断")
            return 130
        return 0 if runner.stats["fail"] == 0 else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
