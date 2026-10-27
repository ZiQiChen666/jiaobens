#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路径执行 / 路径编辑.

路径步骤 = 地图 + 已标注目标 + 等待。

运行时：
    1. OCR 游戏右上角小字，识别当前地图；
    2. 在当前地图的标注里找到目标；
    3. 按 M 打开地图 → 点击目标 → 只按一次 Esc 关闭；
    4. 如果画面出现“退出游戏”，再补一次 Esc；
    5. 等待自动寻路；
    6. 执行下一步。
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import sys
import time
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

import win32gui

from map_labels import (
    TARGET_KINDS,
    _app,
    _valid_map_xy,
    _valid_rect,
    load_maps,
)


APP_FOLDER = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"),
    "CreatureCuriosKeyer",
)
PATHS_PATH = os.path.join(APP_FOLDER, "paths.json")
PATH_LOG_PATH = os.path.join(APP_FOLDER, "path_run.log")


def _compact(text: str) -> str:
    return "".join(
        ch for ch in str(text or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff"
    )


def load_paths() -> list[dict]:
    try:
        with open(PATHS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        paths = data.get("paths", [])
        if isinstance(paths, list):
            return [p for p in paths if isinstance(p, dict)]
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return []


def save_paths(paths: list[dict]) -> bool:
    os.makedirs(os.path.dirname(PATHS_PATH), exist_ok=True)
    tmp = PATHS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"paths": paths}, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PATHS_PATH)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def ocr_top_right_map(hwnd: int) -> tuple[str, list[str]]:
    """OCR 右上角小字（地图名/区域名）。"""
    app = _app()
    if app is None:
        return "", []
    try:
        from PIL import Image
        import numpy as np
    except Exception:
        return "", []
    cap, _err = app.capture_client_bgra(hwnd)
    if cap is None:
        return "", []
    w, h, data = cap
    arr = np.frombuffer(data, dtype=np.uint8).reshape((h, w, 4))
    rgb = arr[:, :, [2, 1, 0]].copy()
    image = Image.fromarray(rgb, "RGB")
    # 右上角小字区域（按 1366x768 实测）
    x0 = int(w * 0.78)
    y0 = int(h * 0.10)
    x1 = w
    y1 = int(h * 0.38)
    crop = image.crop((x0, y0, x1, y1))
    crop = crop.resize((crop.width * 3, crop.height * 3), Image.Resampling.LANCZOS)
    engine = app._get_ocr_engine()
    with app._OCR_LOCK:
        result, _elapse = engine(np.array(crop)[:, :, ::-1])
    texts = [str(it[1]).strip() for it in (result or []) if len(it) >= 3]
    return " ".join(texts), texts


def match_map_profile(profiles: list[dict], texts: list[str]) -> Optional[dict]:
    blob = _compact(" ".join(texts))
    if not blob:
        return None
    best = None
    best_score = 0.0
    from difflib import SequenceMatcher

    for profile in profiles:
        name = str(profile.get("name", ""))
        parts = [
            p for p in re.split(r"[|｜/、,，\s]+", name) if len(_compact(p)) >= 2
        ]
        for part in parts:
            key = _compact(part)
            if key and key in blob:
                return profile
        full = _compact(name)
        if full and full in blob:
            return profile
        for part in parts:
            key = _compact(part)
            ratio = SequenceMatcher(None, key, blob).ratio()
            if ratio > best_score:
                best_score = ratio
                best = profile
    if best is not None and best_score >= 0.55:
        return best
    return None


def find_target(profile: dict, target_name: str) -> Optional[dict]:
    wanted = _compact(target_name)
    for target in profile.get("targets", []):
        if not isinstance(target, dict):
            continue
        if _compact(target.get("name", "")) == wanted:
            return target
    for target in profile.get("targets", []):
        if not isinstance(target, dict):
            continue
        if wanted and wanted in _compact(target.get("name", "")):
            return target
    return None


class PathRunner(QThread):
    status = Signal(str, str)
    done = Signal()
    failed = Signal(str)

    def __init__(
        self,
        hwnd: int,
        path: dict,
        profiles: list[dict],
        map_key: str = "m",
        close_key: str = "esc",
        parent=None,
    ):
        super().__init__(parent)
        self.hwnd = int(hwnd)
        self.path = dict(path)
        self.profiles = list(profiles)
        self.map_key = str(map_key or "m")
        self.close_key = str(close_key or "esc")
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def log(self, text: str) -> None:
        line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {text}"
        try:
            with open(PATH_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        print(line)
        try:
            self.status.emit(text, "warn")
        except Exception:
            pass

    def _sleep(self, seconds: float) -> bool:
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end:
            if self._stop:
                return False
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))
        return not self._stop

    def _tap_key(self, name: str) -> None:
        app = _app()
        if app is None:
            raise RuntimeError("主程序模块不可用。")
        app._force_foreground_window(self.hwnd)
        time.sleep(0.1)
        app._send_key_name(name, True)
        time.sleep(0.05)
        app._send_key_name(name, False)

    def _click_norm(self, nx: float, ny: float) -> None:
        app = _app()
        if app is None:
            raise RuntimeError("主程序模块不可用。")
        _l, _t, cw, ch = win32gui.GetClientRect(self.hwnd)
        cw, ch = int(cw), int(ch)
        if cw <= 0 or ch <= 0:
            raise RuntimeError("游戏客户区尺寸无效。")
        x = max(1, min(cw - 2, int(round(nx * cw))))
        y = max(1, min(ch - 2, int(round(ny * ch))))
        sx, sy = win32gui.ClientToScreen(self.hwnd, (x, y))
        app._force_foreground_window(self.hwnd)
        time.sleep(0.1)
        app._send_mouse_move_to(int(sx), int(sy))
        time.sleep(0.06)
        app._send_mouse_button("left", True)
        time.sleep(0.07)
        app._send_mouse_button("left", False)
        time.sleep(0.15)

    def _ensure_no_exit_menu(self) -> None:
        """如果当前已经出现“退出游戏”菜单，按一次 Esc 关掉；不额外多按。"""
        app = _app()
        if app is None or not hasattr(app, "ocr_screen_items"):
            return
        cap, _err = app.capture_client_bgra(self.hwnd)
        if cap is None:
            return
        w, h, data = cap
        try:
            items = app.ocr_screen_items(data, w, h)
        except Exception:
            return
        texts = [_compact(it.get("text", "")) for it in items]
        if any("退出游戏" in t for t in texts if t):
            self.log("检测到“退出游戏”菜单，先按一次 Esc 关闭")
            self._tap_key(self.close_key)
            time.sleep(0.5)

    def _close_map_safely(self) -> None:
        # 只按一次：地图是 Esc 关闭的；多按会打开别的菜单
        self._tap_key(self.close_key)
        time.sleep(0.55)
        app = _app()
        if app is None or not hasattr(app, "ocr_screen_items"):
            return
        cap, _err = app.capture_client_bgra(self.hwnd)
        if cap is None:
            return
        w, h, data = cap
        try:
            items = app.ocr_screen_items(data, w, h)
        except Exception:
            return
        texts = [_compact(it.get("text", "")) for it in items]
        if any("退出游戏" in t for t in texts if t):
            self.log("检测到“退出游戏”菜单，再按一次 Esc 关闭")
            self._tap_key(self.close_key)
            time.sleep(0.5)

    def _target_norm(self, profile: dict, target: dict) -> tuple[float, float]:
        rect = _valid_rect(profile.get("map_rect_norm"))
        mx, my = _valid_map_xy(target)
        if rect is not None and mx is not None and my is not None:
            x0, y0, x1, y1 = rect
            return (
                max(0.0, min(1.0, x0 + mx * (x1 - x0))),
                max(0.0, min(1.0, y0 + my * (y1 - y0))),
            )
        nx = float(target.get("nx", 0.5))
        ny = float(target.get("ny", 0.5))
        return max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny))

    def _read_current_profile(self) -> Optional[dict]:
        _text, texts = ocr_top_right_map(self.hwnd)
        return match_map_profile(self.profiles, texts)

    @staticmethod
    def _same_map(a: str, b: str) -> bool:
        ca, cb = _compact(a), _compact(b)
        if not ca or not cb:
            return False
        return ca in cb or cb in ca

    def _ensure_entered(
        self,
        profile: dict,
        target: dict,
        expected_map: str,
    ) -> bool:
        """如果没进入期望的新地图，就在标记点 5% 半径内绕圈重点，直到进入。"""
        current = self._read_current_profile()
        if current is not None and self._same_map(
            str(current.get("name", "")), expected_map
        ):
            self.log(f"已进入目标地图：{expected_map}")
            return True

        # 以点击目标的窗口坐标为圆心，5% 地图范围作为半径
        base_nx, base_ny = self._target_norm(profile, target)
        rect = _valid_rect(profile.get("map_rect_norm"))
        if rect is not None:
            x0, y0, x1, y1 = rect
            rx = 0.05 * (x1 - x0)
            ry = 0.05 * (y1 - y0)
        else:
            rx = ry = 0.05
        self.log(
            f"没识别到新地图「{expected_map}」，在标记点附近 5% 半径绕圈重试…"
        )
        attempts = 0
        for loop in range(2):
            for k in range(8):
                if self._stop:
                    raise RuntimeError("已停止。")
                angle = 2.0 * math.pi * k / 8.0 + loop * math.pi / 8.0
                nx = base_nx + rx * math.cos(angle)
                ny = base_ny + ry * math.sin(angle)
                if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
                    continue
                attempts += 1
                self.log(f"绕圈第 {attempts} 次：{nx*100:.1f}%, {ny*100:.1f}%")
                self._tap_key(self.map_key)
                if not self._sleep(1.2):
                    raise RuntimeError("已停止。")
                self._click_norm(nx, ny)
                if not self._sleep(0.5):
                    raise RuntimeError("已停止。")
                self._close_map_safely()
                if not self._sleep(4.0):
                    raise RuntimeError("已停止。")
                current = self._read_current_profile()
                if current is not None and self._same_map(
                    str(current.get("name", "")), expected_map
                ):
                    self.log(f"绕圈后成功进入：{expected_map}")
                    return True
        self.log(f"绕圈 {attempts} 次仍未进入「{expected_map}」")
        return False

    def run(self) -> None:
        try:
            steps = [s for s in self.path.get("steps", []) if isinstance(s, dict)]
            if not steps:
                raise RuntimeError("路径没有步骤。")
            self._ensure_no_exit_menu()
            for index, step in enumerate(steps, 1):
                if self._stop:
                    raise RuntimeError("已停止。")
                step_map = str(step.get("map", "")).strip()
                target_name = str(step.get("target", "")).strip()
                wait = max(0.0, float(step.get("wait", 15.0) or 0.0))
                self.log(f"第 {index}/{len(steps)} 步：识别当前地图…")
                text, texts = ocr_top_right_map(self.hwnd)
                profile = match_map_profile(self.profiles, texts)
                if profile is None:
                    raise RuntimeError(
                        f"右上角没识别出地图（OCR：{text[:80] or '空'}）"
                    )
                profile_name = str(profile.get("name", ""))
                self.log(f"当前地图：{profile_name}")
                if step_map:
                    a = _compact(step_map)
                    b = _compact(profile_name)
                    if a not in b and b not in a:
                        raise RuntimeError(
                            f"当前地图是「{profile_name}」，不是路径要求的「{step_map}」"
                        )
                target = find_target(profile, target_name)
                if target is None:
                    raise RuntimeError(
                        f"地图「{profile_name}」里没有目标「{target_name}」"
                    )
                nx, ny = self._target_norm(profile, target)
                self.log(
                    f"前往「{target_name}」：窗口坐标 {nx*100:.1f}%, {ny*100:.1f}%"
                )
                self._tap_key(self.map_key)
                if not self._sleep(1.2):
                    raise RuntimeError("已停止。")
                self._click_norm(nx, ny)
                if not self._sleep(0.5):
                    raise RuntimeError("已停止。")
                self._close_map_safely()
                self.log(f"等待 {wait:.0f} 秒…")
                if not self._sleep(wait):
                    raise RuntimeError("已停止。")
                # 如果本步应该进入新地图（下一步的地图与当前不同），
                # 但右上角还没识别到新地图，就在标记点 5% 半径绕圈重试。
                if index < len(steps):
                    next_step = steps[index]
                    next_map = str(next_step.get("map", "")).strip()
                    if next_map and not self._same_map(profile_name, next_map):
                        if not self._ensure_entered(profile, target, next_map):
                            raise RuntimeError(
                                f"没有进入新地图「{next_map}」"
                            )
            self.log("路径执行完成。")
            self.done.emit()
        except Exception as exc:
            msg = str(exc)
            if msg != "已停止。":
                self.log(f"失败：{msg}")
                self.failed.emit(msg)
            else:
                self.failed.emit("已停止。")


class PathEditorDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("路径编辑（地图 + 入口 + 等待）")
        self.resize(1050, 760)
        self.paths = load_paths()
        self.profiles = load_maps()
        self.current_index = -1
        self.steps: list[dict] = []
        self._build_ui()
        self._refresh_path_list()
        if self.paths:
            self.list_paths.setCurrentRow(0)
        else:
            self.new_path()

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        left = QVBoxLayout()
        left.addWidget(QLabel("路径列表"))
        self.list_paths = QListWidget()
        left.addWidget(self.list_paths, 1)
        lr = QHBoxLayout()
        self.btn_new = QPushButton("新建路径")
        self.btn_del_path = QPushButton("删除路径")
        lr.addWidget(self.btn_new)
        lr.addWidget(self.btn_del_path)
        left.addLayout(lr)
        root.addLayout(left, 0)

        right = QVBoxLayout()
        form = QFormLayout()
        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("例如：刷怪点 → BOSS点")
        form.addRow("路径名", self.edit_name)
        right.addLayout(form)

        add_box = QGroupBox("添加步骤")
        af = QFormLayout(add_box)
        self.combo_map = QComboBox()
        for mp in self.profiles:
            self.combo_map.addItem(str(mp.get("name", "未命名")), mp)
        self.combo_target = QComboBox()
        self.spin_wait = QDoubleSpinBox()
        self.spin_wait.setRange(0, 300)
        self.spin_wait.setValue(15)
        self.spin_wait.setSuffix(" 秒")
        self.edit_note = QLineEdit()
        self.edit_note.setPlaceholderText("说明（可选）")
        self.btn_add = QPushButton("添加这个步骤")
        af.addRow("地图", self.combo_map)
        af.addRow("目标", self.combo_target)
        af.addRow("等待", self.spin_wait)
        af.addRow("说明", self.edit_note)
        af.addRow("", self.btn_add)
        right.addWidget(add_box)

        right.addWidget(QLabel("路径步骤"))
        self.list_steps = QListWidget()
        right.addWidget(self.list_steps, 1)
        br = QHBoxLayout()
        self.btn_up = QPushButton("上移")
        self.btn_down = QPushButton("下移")
        self.btn_del_step = QPushButton("删除步骤")
        br.addWidget(self.btn_up)
        br.addWidget(self.btn_down)
        br.addWidget(self.btn_del_step)
        br.addStretch(1)
        self.btn_save = QPushButton("保存路径")
        br.addWidget(self.btn_save)
        right.addLayout(br)
        root.addLayout(right, 1)

        self.btn_new.clicked.connect(self.new_path)
        self.btn_del_path.clicked.connect(self.delete_path)
        self.combo_map.currentIndexChanged.connect(self._refresh_targets)
        self.btn_add.clicked.connect(self.add_step)
        self.btn_up.clicked.connect(lambda: self.move_step(-1))
        self.btn_down.clicked.connect(lambda: self.move_step(1))
        self.btn_del_step.clicked.connect(self.delete_step)
        self.btn_save.clicked.connect(self.save_path)
        self.list_paths.currentRowChanged.connect(self.load_path_at)
        self._refresh_targets()

    def _refresh_path_list(self) -> None:
        self.list_paths.blockSignals(True)
        self.list_paths.clear()
        for path in self.paths:
            self.list_paths.addItem(str(path.get("name", "未命名")))
        self.list_paths.blockSignals(False)

    def _refresh_targets(self, *_args) -> None:
        self.combo_target.clear()
        mp = self.combo_map.currentData()
        if isinstance(mp, dict):
            for target in mp.get("targets", []):
                if isinstance(target, dict):
                    kind = TARGET_KINDS.get(str(target.get("kind")), "")
                    self.combo_target.addItem(
                        f"{target.get('name', '')} [{kind}]", target
                    )

    def _refresh_steps(self) -> None:
        self.list_steps.clear()
        for idx, step in enumerate(self.steps, 1):
            self.list_steps.addItem(
                f"{idx}. {step.get('map', '')} → {step.get('target', '')}  "
                f"等待 {float(step.get('wait', 0)):.0f}s  {step.get('note', '')}"
            )

    def new_path(self) -> None:
        self.current_index = -1
        self.list_paths.clearSelection()
        self.edit_name.setText("新路径")
        self.steps = []
        self._refresh_steps()

    def load_path_at(self, index: int) -> None:
        if index < 0 or index >= len(self.paths):
            return
        path = self.paths[index]
        self.current_index = index
        self.edit_name.setText(str(path.get("name", "")))
        self.steps = [dict(s) for s in path.get("steps", []) if isinstance(s, dict)]
        self._refresh_steps()

    def delete_path(self) -> None:
        if self.current_index < 0 or self.current_index >= len(self.paths):
            return
        del self.paths[self.current_index]
        save_paths(self.paths)
        self.current_index = -1
        self._refresh_path_list()
        if self.paths:
            self.list_paths.setCurrentRow(0)
        else:
            self.new_path()

    def add_step(self) -> None:
        mp = self.combo_map.currentData()
        target = self.combo_target.currentData()
        if not isinstance(mp, dict) or not isinstance(target, dict):
            QMessageBox.information(
                self,
                "缺少目标",
                "先选择地图和目标。\n如果地图没有目标，先去人工标注地图。",
            )
            return
        self.steps.append(
            {
                "map": str(mp.get("name", "")),
                "target": str(target.get("name", "")),
                "wait": float(self.spin_wait.value()),
                "note": self.edit_note.text().strip(),
            }
        )
        self._refresh_steps()
        self.list_steps.setCurrentRow(len(self.steps) - 1)

    def move_step(self, delta: int) -> None:
        row = self.list_steps.currentRow()
        if row < 0:
            return
        target = row + delta
        if target < 0 or target >= len(self.steps):
            return
        self.steps[row], self.steps[target] = self.steps[target], self.steps[row]
        self._refresh_steps()
        self.list_steps.setCurrentRow(target)

    def delete_step(self) -> None:
        row = self.list_steps.currentRow()
        if 0 <= row < len(self.steps):
            del self.steps[row]
            self._refresh_steps()

    def save_path(self) -> None:
        name = self.edit_name.text().strip()
        if not name:
            QMessageBox.warning(self, "缺少路径名", "请填写路径名。")
            return
        if not self.steps:
            QMessageBox.warning(self, "没有步骤", "请至少添加一个步骤。")
            return
        path = {
            "name": name,
            "steps": [dict(s) for s in self.steps],
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        existing = next(
            (i for i, p in enumerate(self.paths) if str(p.get("name", "")) == name),
            None,
        )
        if existing is not None:
            self.paths[existing] = path
            self.current_index = existing
        else:
            self.paths.append(path)
            self.current_index = len(self.paths) - 1
        if not save_paths(self.paths):
            QMessageBox.warning(self, "保存失败", f"无法写入：{PATHS_PATH}")
            return
        self._refresh_path_list()
        self.list_paths.setCurrentRow(self.current_index)
        QMessageBox.information(
            self, "已保存", f"路径「{name}」已保存。\n{PATHS_PATH}"
        )
