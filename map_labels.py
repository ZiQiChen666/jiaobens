#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""地图人工标注 / 编辑 / 寻路（不依赖 OCR）.

坐标体系：
    窗口客户区左上角 = 窗口零点
    地图零点/范围     = 在截图上人工标定
    目标点            = 相对地图零点的地图内坐标

保存：
    %APPDATA%\\CreatureCuriosKeyer\\maps.json
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
from typing import Optional

from PySide6.QtCore import QRect, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import win32gui

try:
    import winsound
except Exception:
    winsound = None


APP_FOLDER = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "CreatureCuriosKeyer",
)
MAPS_PATH = os.path.join(APP_FOLDER, "maps.json")
TRAVEL_LOG_PATH = os.path.join(APP_FOLDER, "map_travel.log")
PATHS_PATH = os.path.join(APP_FOLDER, "paths.json")
PATH_LOG_PATH = os.path.join(APP_FOLDER, "path_run.log")

TARGET_KINDS = {
    "farm": "打怪点",
    "boss": "BOSS点",
    "other": "其他入口",
}


def _app():
    return sys.modules.get("creature_curios_keyer") or sys.modules.get("__main__")


def beep(freq: int = 900, ms: int = 80) -> None:
    if winsound is None:
        return
    try:
        winsound.Beep(freq, ms)
    except Exception:
        pass


def load_maps() -> list[dict]:
    try:
        with open(MAPS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        maps = data.get("maps", [])
        if isinstance(maps, list):
            return [m for m in maps if isinstance(m, dict)]
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return []


def save_maps(maps: list[dict]) -> bool:
    os.makedirs(os.path.dirname(MAPS_PATH), exist_ok=True)
    tmp = MAPS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"maps": maps}, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, MAPS_PATH)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


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


def _compact(text: str) -> str:
    return "".join(
        ch for ch in str(text or "") if ch.isalnum() or "一" <= ch <= "鿿"
    )


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
    # 右上角圆圈区域：按 1366x768 实测约在 x 0.78~1.0, y 0.12~0.35
    x0 = int(w * 0.78)
    y0 = int(h * 0.12)
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
    for profile in profiles:
        name = str(profile.get("name", ""))
        parts = [p for p in re.split(r"[|｜/、,，\s]+", name) if len(_compact(p)) >= 2]
        for part in parts:
            key = _compact(part)
            if key and key in blob:
                return profile
        full = _compact(name)
        if full and full in blob:
            return profile
        # 简单相似度兜底
        from difflib import SequenceMatcher
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


def _valid_rect(rect) -> Optional[tuple[float, float, float, float]]:
    try:
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            return None
        x0, y0, x1, y1 = (float(v) for v in rect)
        if 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0:
            return (x0, y0, x1, y1)
    except (TypeError, ValueError):
        pass
    return None


def _valid_map_xy(target: dict) -> tuple[Optional[float], Optional[float]]:
    try:
        mx = target.get("map_x")
        my = target.get("map_y")
        if mx is None or my is None:
            return None, None
        return (
            max(0.0, min(1.0, float(mx))),
            max(0.0, min(1.0, float(my))),
        )
    except (TypeError, ValueError):
        return None, None


class MapPointCanvas(QWidget):
    """显示地图截图、地图范围框和人工标注的目标点。"""

    point_marked = Signal(float, float)
    map_corner_marked = Signal(float, float)

    def __init__(self, image: QImage, parent=None) -> None:
        super().__init__(parent)
        self.image = image
        self.scale = 1.0
        self.map_rect: Optional[tuple[float, float, float, float]] = None
        self.targets: list[dict] = []
        self.mark_actual = False
        self.map_corner_mode = False
        self._apply_size()

    def _apply_size(self) -> None:
        iw = max(1, self.image.width())
        ih = max(1, self.image.height())
        screen = QApplication.primaryScreen()
        max_w, max_h = 1500, 900
        if screen is not None:
            avail = screen.availableGeometry()
            max_w = max(500, min(1500, avail.width() - 120))
            max_h = max(350, min(900, avail.height() - 220))
        self.scale = min(max_w / iw, max_h / ih, 1.0)
        self.setFixedSize(
            max(1, int(iw * self.scale)), max(1, int(ih * self.scale))
        )

    def set_image(self, image: QImage) -> None:
        self.image = image
        self._apply_size()
        self.update()

    def set_targets(self, targets: list[dict]) -> None:
        self.targets = targets
        self.update()

    def _to_norm(self, pos) -> tuple[float, float]:
        iw = max(1, self.image.width())
        ih = max(1, self.image.height())
        x = pos.x() / max(0.0001, self.scale)
        y = pos.y() / max(0.0001, self.scale)
        return (
            max(0.0, min(1.0, x / iw)),
            max(0.0, min(1.0, y / ih)),
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        nx, ny = self._to_norm(event.position().toPoint())
        if self.map_corner_mode:
            self.map_corner_marked.emit(nx, ny)
            return
        if self.mark_actual:
            self.mark_actual = False
            self.point_marked.emit(nx, ny)
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self.rect(), self.image)
        iw = max(1, self.image.width())
        ih = max(1, self.image.height())
        s = self.scale

        if self.map_rect is not None:
            x0, y0, x1, y1 = self.map_rect
            rx0 = int(x0 * iw * s)
            ry0 = int(y0 * ih * s)
            rx1 = int(x1 * iw * s)
            ry1 = int(y1 * ih * s)
            painter.setPen(QPen(QColor("#ffd60a"), 2))
            painter.drawRect(QRect(rx0, ry0, max(2, rx1 - rx0), max(2, ry1 - ry0)))
            painter.drawLine(rx0 - 14, ry0, rx0 + 14, ry0)
            painter.drawLine(rx0, ry0 - 14, rx0, ry0 + 14)
            painter.setFont(QFont("Microsoft YaHei", 9, QFont.Weight.Bold))
            painter.drawText(rx0 + 16, max(14, ry0 - 4), "地图零点")

        for target in self.targets:
            mx, my = _valid_map_xy(target)
            if mx is None or my is None:
                try:
                    nx = float(target.get("nx", 0.5))
                    ny = float(target.get("ny", 0.5))
                except (TypeError, ValueError):
                    continue
            elif self.map_rect is not None:
                x0, y0, x1, y1 = self.map_rect
                nx = x0 + mx * (x1 - x0)
                ny = y0 + my * (y1 - y0)
            else:
                continue
            x = int(nx * iw * s)
            y = int(ny * ih * s)
            kind = str(target.get("kind", "other"))
            color = QColor("#ff3b30") if kind == "farm" else (
                QColor("#3ddc84") if kind == "boss" else QColor("#8ab4f8")
            )
            painter.setPen(QPen(color, 3))
            painter.drawLine(x - 16, y, x + 16, y)
            painter.drawLine(x, y - 16, x, y + 16)
            painter.setPen(QPen(color, 1))
            painter.setFont(QFont("Microsoft YaHei", 9, QFont.Weight.Bold))
            painter.drawText(
                x + 20,
                max(14, y - 6),
                f"{target.get('name', '')} [{TARGET_KINDS.get(kind, '')}]",
            )

        if self.map_corner_mode:
            painter.setPen(QPen(QColor("#ffd60a"), 2))
            painter.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
            painter.drawText(20, 30, "请点击地图左上角零点，再点右下角确定地图范围")
        elif self.mark_actual:
            painter.setPen(QPen(QColor("#ff3b30"), 2))
            painter.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
            painter.drawText(20, 30, "请点击真正的入口位置")


class MapLabelDialog(QDialog):
    """人工标注 / 编辑地图目标."""

    def __init__(self, hwnd: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("地图人工标注 / 编辑")
        self.resize(1500, 950)
        self.hwnd = int(hwnd)
        self.maps = load_maps()
        self.image: Optional[QImage] = None
        self.map_rect_norm: Optional[tuple[float, float, float, float]] = None
        self.targets: list[dict] = []
        self._map_corners: list[tuple[float, float]] = []
        self._mark_mode = "add"       # add / repoint
        self._repoint_index = -1
        self.saved = False
        self._build_ui()
        self._refresh_map_combo()
        self.capture_image()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(8)

        top = QGroupBox("地图")
        form = QFormLayout(top)
        self.edit_map_name = QLineEdit()
        self.edit_map_name.setPlaceholderText("例如：晨瞻之森|赤砂壳原")
        self.lbl_map_rect = QLabel("地图零点/范围：未标定")
        self.lbl_map_rect.setStyleSheet("color: #b26a00;")
        self.btn_calibrate = QPushButton("标定地图零点/范围")
        self.btn_capture = QPushButton("重新截图")
        self.combo_maps = QComboBox()
        self.combo_maps.setMinimumWidth(260)
        self.btn_load = QPushButton("载入已保存地图")
        self.btn_new = QPushButton("新建地图")
        row = QHBoxLayout()
        row.addWidget(QLabel("已保存"))
        row.addWidget(self.combo_maps, 1)
        row.addWidget(self.btn_load)
        row.addWidget(self.btn_new)
        row.addWidget(self.btn_capture)
        form.addRow("地图名", self.edit_map_name)
        rect_row = QHBoxLayout()
        rect_row.addWidget(self.lbl_map_rect, 1)
        rect_row.addWidget(self.btn_calibrate)
        form.addRow("地图范围", rect_row)
        form.addRow("", row)
        root.addWidget(top)

        body = QHBoxLayout()
        body.setSpacing(10)

        left = QVBoxLayout()
        self.status = QLabel("正在截图…")
        self.status.setStyleSheet("color: #1565c0;")
        left.addWidget(self.status)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.canvas = MapPointCanvas(QImage(), self)
        scroll.setWidget(self.canvas)
        left.addWidget(scroll, 1)
        body.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(8)
        target_box = QGroupBox("目标点")
        tf = QFormLayout(target_box)
        self.edit_target_name = QLineEdit()
        self.edit_target_name.setPlaceholderText("例如：大角城 / 打怪点 / BOSS")
        self.combo_kind = QComboBox()
        for key, label in TARGET_KINDS.items():
            self.combo_kind.addItem(label, key)
        self.btn_add = QPushButton("手动添加目标（点坐标）")
        self.btn_add.setMinimumHeight(34)
        self.btn_repoint = QPushButton("重新点选选中目标")
        self.btn_apply = QPushButton("应用名称/类型到选中目标")
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_repoint)
        btn_row.addWidget(self.btn_apply)
        tf.addRow("名称", self.edit_target_name)
        tf.addRow("类型", self.combo_kind)
        tf.addRow("", self.btn_add)
        tf.addRow("", btn_row)
        right.addWidget(target_box)

        list_box = QGroupBox("本图目标")
        lv = QVBoxLayout(list_box)
        self.list_targets = QListWidget()
        lv.addWidget(self.list_targets)
        self.btn_delete = QPushButton("删除选中目标")
        lv.addWidget(self.btn_delete)
        right.addWidget(list_box, 1)

        body.addLayout(right, 0)
        root.addLayout(body, 1)

        bottom = QHBoxLayout()
        self.btn_save = QPushButton("保存地图")
        self.btn_save.setMinimumHeight(38)
        self.btn_close = QPushButton("关闭")
        bottom.addStretch(1)
        bottom.addWidget(self.btn_close)
        bottom.addWidget(self.btn_save)
        root.addLayout(bottom)

        hint = QLabel(
            "流程：打开游戏地图 → 重新截图 → 标定地图左上角零点/右下角范围 → "
            "填名称/类型 → 手动添加目标 → 在截图上点真正入口。"
            "坐标保存为地图内相对坐标，不依赖 OCR。载入已保存地图后可以继续修改目标并重新保存。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        root.addWidget(hint)

        self.btn_capture.clicked.connect(self.capture_image)
        self.btn_calibrate.clicked.connect(self.start_calibrate_map)
        self.btn_load.clicked.connect(self.load_selected_map)
        self.btn_new.clicked.connect(self.new_map)
        self.btn_add.clicked.connect(self.start_add_target)
        self.btn_repoint.clicked.connect(self.start_repoint_target)
        self.btn_apply.clicked.connect(self.apply_target_info)
        self.btn_delete.clicked.connect(self.delete_target)
        self.btn_save.clicked.connect(self.save_current_map)
        self.btn_close.clicked.connect(self.reject)
        self.list_targets.currentRowChanged.connect(self._on_target_row)
        self.canvas.map_corner_marked.connect(self._on_map_corner)
        self.canvas.point_marked.connect(self._on_point_marked)

    # ------------------------------------------------------------------
    def _refresh_map_combo(self) -> None:
        self.combo_maps.clear()
        for mp in self.maps:
            self.combo_maps.addItem(str(mp.get("name", "未命名")), mp)

    def capture_image(self) -> None:
        app = _app()
        if app is None or not hasattr(app, "capture_client_bgra"):
            QMessageBox.warning(self, "不可用", "主程序截图模块不可用。")
            return
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            QMessageBox.warning(self, "找不到游戏窗口", "请先绑定/启动 CreatureCurios。")
            return
        cap, err = app.capture_client_bgra(self.hwnd)
        if cap is None:
            QMessageBox.warning(self, "截图失败", err or "无法截取游戏画面。")
            return
        w, h, data = cap
        self.image = QImage(
            bytes(data), w, h, w * 4, QImage.Format.Format_RGB32
        ).copy()
        self.canvas.set_image(self.image)
        self.canvas.map_rect = self.map_rect_norm
        self.canvas.set_targets(self.targets)
        self._update_map_rect_label()
        self.status.setText("截图完成：请标定地图零点/范围，然后手动添加目标。")

    # ------------------------------------------------------------------
    def start_calibrate_map(self) -> None:
        if self.image is None:
            QMessageBox.information(self, "没有截图", "先点“重新截图”。")
            return
        self._map_corners = []
        self.canvas.map_corner_mode = True
        self.canvas.update()
        self.status.setText("请点击地图左上角零点（地图图像/面板左上角）")

    def _on_map_corner(self, nx: float, ny: float) -> None:
        self._map_corners.append((nx, ny))
        if len(self._map_corners) == 1:
            self.status.setText("再点击地图右下角，确定地图范围")
            return
        (x0, y0), (x1, y1) = self._map_corners[:2]
        self._map_corners = []
        self.canvas.map_corner_mode = False
        rx0, rx1 = sorted((x0, x1))
        ry0, ry1 = sorted((y0, y1))
        if rx1 - rx0 < 0.02 or ry1 - ry0 < 0.02:
            self.canvas.update()
            self.status.setText("地图范围太小，请重新标定")
            return
        self.map_rect_norm = (rx0, ry0, rx1, ry1)
        self.canvas.map_rect = self.map_rect_norm
        # 已有目标按新范围重算地图内坐标
        for target in self.targets:
            try:
                nx = float(target.get("nx", 0.5))
                ny = float(target.get("ny", 0.5))
            except (TypeError, ValueError):
                continue
            mx = (nx - rx0) / max(0.0001, rx1 - rx0)
            my = (ny - ry0) / max(0.0001, ry1 - ry0)
            target["map_x"] = max(0.0, min(1.0, mx))
            target["map_y"] = max(0.0, min(1.0, my))
        self.canvas.set_targets(self.targets)
        self._refresh_target_list()
        self._update_map_rect_label()
        self.status.setText("地图范围已标定；现在可以添加目标。")

    def _update_map_rect_label(self) -> None:
        if self.map_rect_norm is None:
            self.lbl_map_rect.setText("地图零点/范围：未标定")
            self.lbl_map_rect.setStyleSheet("color: #b26a00;")
        else:
            x0, y0, x1, y1 = self.map_rect_norm
            self.lbl_map_rect.setText(
                f"地图范围：{x0*100:.1f},{y0*100:.1f} → {x1*100:.1f},{y1*100:.1f}"
            )
            self.lbl_map_rect.setStyleSheet("color: #2e7d32;")

    # ------------------------------------------------------------------
    def start_add_target(self) -> None:
        if self.image is None:
            QMessageBox.information(self, "没有截图", "先点“重新截图”。")
            return
        if not self.edit_target_name.text().strip():
            QMessageBox.information(self, "缺少名称", "先填目标名称。")
            return
        if self.map_rect_norm is None:
            QMessageBox.information(
                self, "还没标定地图", "请先标定地图零点/范围，再添加目标。"
            )
            return
        self._mark_mode = "add"
        self._repoint_index = -1
        self.canvas.mark_actual = True
        self.canvas.update()
        self.status.setText("请在截图上点击真正的入口位置")

    def start_repoint_target(self) -> None:
        row = self.list_targets.currentRow()
        if row < 0 or row >= len(self.targets):
            QMessageBox.information(self, "先选目标", "请先在目标列表里选中一个目标。")
            return
        if self.image is None:
            QMessageBox.information(self, "没有截图", "先点“重新截图”。")
            return
        if self.map_rect_norm is None:
            QMessageBox.information(self, "还没标定地图", "请先标定地图范围。")
            return
        self._mark_mode = "repoint"
        self._repoint_index = row
        self.canvas.mark_actual = True
        self.canvas.update()
        self.status.setText("请在截图上重新点击这个目标真正的位置")

    def _on_point_marked(self, nx: float, ny: float) -> None:
        if self.map_rect_norm is None:
            QMessageBox.information(self, "还没标定地图", "请先标定地图范围。")
            return
        x0, y0, x1, y1 = self.map_rect_norm
        mx = (float(nx) - x0) / max(0.0001, x1 - x0)
        my = (float(ny) - y0) / max(0.0001, y1 - y0)
        if not (0.0 <= mx <= 1.0 and 0.0 <= my <= 1.0):
            QMessageBox.information(
                self, "点在地图外", "这个点不在标定的地图范围内，请重新点。"
            )
            return
        mx = max(0.0, min(1.0, mx))
        my = max(0.0, min(1.0, my))
        if self._mark_mode == "repoint" and 0 <= self._repoint_index < len(self.targets):
            target = self.targets[self._repoint_index]
            target["nx"] = float(nx)
            target["ny"] = float(ny)
            target["map_x"] = mx
            target["map_y"] = my
            self.status.setText(
                f"已重新点选「{target.get('name', '')}」：地图坐标 {mx*100:.1f}%, {my*100:.1f}%"
            )
        else:
            kind = str(self.combo_kind.currentData() or "farm")
            name = self.edit_target_name.text().strip() or f"入口 {len(self.targets)+1}"
            target = {
                "name": name,
                "kind": kind,
                "nx": float(nx),
                "ny": float(ny),
                "map_x": mx,
                "map_y": my,
            }
            for idx, old in enumerate(self.targets):
                if old.get("name") == name and old.get("kind") == kind:
                    self.targets[idx] = target
                    break
            else:
                self.targets.append(target)
            self.status.setText(
                f"已添加{TARGET_KINDS.get(kind, kind)}「{name}」：地图坐标 {mx*100:.1f}%, {my*100:.1f}%"
            )
        self._mark_mode = "add"
        self._repoint_index = -1
        self.canvas.set_targets(self.targets)
        self._refresh_target_list()
        self.list_targets.setCurrentRow(len(self.targets) - 1)

    # ------------------------------------------------------------------
    def _refresh_target_list(self) -> None:
        self.list_targets.blockSignals(True)
        self.list_targets.clear()
        for target in self.targets:
            mx, my = _valid_map_xy(target)
            coord = (
                f"地图 {mx*100:.1f}%, {my*100:.1f}%"
                if mx is not None and my is not None
                else "无地图坐标"
            )
            self.list_targets.addItem(
                f"{target.get('name', '')}  "
                f"[{TARGET_KINDS.get(str(target.get('kind')), '')}]  {coord}"
            )
        self.list_targets.blockSignals(False)

    def _on_target_row(self, row: int) -> None:
        if 0 <= row < len(self.targets):
            target = self.targets[row]
            self.edit_target_name.setText(str(target.get("name", "")))
            idx = self.combo_kind.findData(str(target.get("kind", "farm")))
            if idx >= 0:
                self.combo_kind.setCurrentIndex(idx)

    def apply_target_info(self) -> None:
        row = self.list_targets.currentRow()
        if row < 0 or row >= len(self.targets):
            QMessageBox.information(self, "先选目标", "请先在目标列表里选中一个目标。")
            return
        name = self.edit_target_name.text().strip()
        if not name:
            QMessageBox.information(self, "缺少名称", "名称不能为空。")
            return
        kind = str(self.combo_kind.currentData() or "farm")
        self.targets[row]["name"] = name
        self.targets[row]["kind"] = kind
        self.canvas.set_targets(self.targets)
        self._refresh_target_list()
        self.list_targets.setCurrentRow(row)
        self.status.setText(f"已更新目标：{name}（{TARGET_KINDS.get(kind, kind)}）")

    def delete_target(self) -> None:
        row = self.list_targets.currentRow()
        if row < 0 or row >= len(self.targets):
            return
        name = self.targets[row].get("name", "")
        del self.targets[row]
        self.canvas.set_targets(self.targets)
        self._refresh_target_list()
        self.status.setText(f"已删除目标：{name}")

    # ------------------------------------------------------------------
    def load_selected_map(self) -> None:
        mp = self.combo_maps.currentData()
        if not isinstance(mp, dict):
            return
        self.edit_map_name.setText(str(mp.get("name", "")))
        self.map_rect_norm = _valid_rect(mp.get("map_rect_norm"))
        self.canvas.map_rect = self.map_rect_norm
        self.targets = []
        for target in mp.get("targets", []):
            if not isinstance(target, dict):
                continue
            item = dict(target)
            mx, my = _valid_map_xy(item)
            if (mx is None or my is None) and self.map_rect_norm is not None:
                try:
                    nx = float(item.get("nx", 0.5))
                    ny = float(item.get("ny", 0.5))
                except (TypeError, ValueError):
                    nx = ny = None
                if nx is not None and ny is not None:
                    x0, y0, x1, y1 = self.map_rect_norm
                    item["map_x"] = max(0.0, min(1.0, (nx - x0) / max(0.0001, x1 - x0)))
                    item["map_y"] = max(0.0, min(1.0, (ny - y0) / max(0.0001, y1 - y0)))
            self.targets.append(item)
        self.canvas.set_targets(self.targets)
        self._refresh_target_list()
        self._update_map_rect_label()
        self.status.setText(f"已载入地图：{mp.get('name', '')}，可以修改后重新保存")

    def new_map(self) -> None:
        self.edit_map_name.clear()
        self.map_rect_norm = None
        self.canvas.map_rect = None
        self.targets = []
        self.canvas.set_targets(self.targets)
        self._refresh_target_list()
        self._update_map_rect_label()
        self.status.setText("新地图：先标定地图范围，再添加目标。")

    def save_current_map(self) -> None:
        name = self.edit_map_name.text().strip()
        if not name:
            QMessageBox.warning(self, "缺少地图名", "请填写地图名。")
            return
        if self.map_rect_norm is None:
            QMessageBox.warning(self, "缺少地图范围", "请先标定地图零点/范围。")
            return
        if not self.targets:
            QMessageBox.warning(self, "没有目标", "请至少添加一个目标。")
            return
        missing = []
        for target in self.targets:
            mx, my = _valid_map_xy(target)
            if mx is None or my is None:
                missing.append(str(target.get("name", "未命名")))
        if missing:
            QMessageBox.warning(
                self,
                "目标缺少地图坐标",
                "以下目标没有地图坐标，请“重新点选选中目标”：\n"
                + "、".join(missing),
            )
            return
        profile = {
            "name": name,
            "map_rect_norm": list(self.map_rect_norm),
            "targets": [
                {
                    "name": str(t.get("name", "")),
                    "kind": str(t.get("kind", "other")),
                    "nx": float(t.get("nx", 0.5)),
                    "ny": float(t.get("ny", 0.5)),
                    "map_x": float(t.get("map_x")),
                    "map_y": float(t.get("map_y")),
                }
                for t in self.targets
            ],
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        existing = next(
            (i for i, mp in enumerate(self.maps) if str(mp.get("name", "")) == name),
            None,
        )
        if existing is not None:
            self.maps[existing] = profile
        else:
            self.maps.append(profile)
        if not save_maps(self.maps):
            QMessageBox.warning(self, "保存失败", f"无法写入：{MAPS_PATH}")
            return
        self.saved = True
        self._refresh_map_combo()
        idx = next(
            (i for i in range(self.combo_maps.count()) if self.combo_maps.itemText(i) == name),
            -1,
        )
        if idx >= 0:
            self.combo_maps.setCurrentIndex(idx)
        self.status.setText(f"已保存地图：{name}（{len(self.targets)} 个目标）")
        beep(1500, 100)
        QMessageBox.information(
            self, "已保存", f"地图「{name}」已保存/更新。\n{MAPS_PATH}"
        )


class MapTravelRunner(QThread):
    """打开地图 → 点击人工记录的地图坐标 → 关闭地图 → 等待自动寻路。"""

    status = Signal(str, str)
    done = Signal()
    failed = Signal(str)

    def __init__(
        self,
        hwnd: int,
        profile: dict,
        target: dict,
        map_key: str = "m",
        close_key: str = "esc",
        travel_wait: float = 20.0,
        parent=None,
    ):
        super().__init__(parent)
        self.hwnd = int(hwnd)
        self.profile = dict(profile)
        self.target = dict(target)
        self.map_key = str(map_key or "m")
        self.close_key = str(close_key or "esc")
        self.travel_wait = max(1.0, float(travel_wait))
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def log(self, text: str) -> None:
        line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {text}"
        try:
            with open(TRAVEL_LOG_PATH, "a", encoding="utf-8") as f:
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

    def _target_norm(self) -> tuple[float, float]:
        rect = _valid_rect(self.profile.get("map_rect_norm"))
        mx, my = _valid_map_xy(self.target)
        if rect is not None and mx is not None and my is not None:
            x0, y0, x1, y1 = rect
            nx = x0 + mx * (x1 - x0)
            ny = y0 + my * (y1 - y0)
            return (
                max(0.0, min(1.0, nx)),
                max(0.0, min(1.0, ny)),
            )
        nx = float(self.target.get("nx", 0.5))
        ny = float(self.target.get("ny", 0.5))
        return (
            max(0.0, min(1.0, nx)),
            max(0.0, min(1.0, ny)),
        )

    def run(self) -> None:
        try:
            if not win32gui.IsWindow(self.hwnd):
                raise RuntimeError("游戏窗口已关闭。")
            nx, ny = self._target_norm()
            self.log(
                f"打开地图，前往「{self.target.get('name', '')}」"
                f"（窗口坐标 {nx*100:.1f}%, {ny*100:.1f}%）…"
            )
            self._tap_key(self.map_key)
            if not self._sleep(1.2):
                raise RuntimeError("已停止。")
            self._click_norm(nx, ny)
            if not self._sleep(0.5):
                raise RuntimeError("已停止。")
            self._tap_key(self.close_key)
            self.log(f"已关闭地图，等待自动寻路 {self.travel_wait:.0f} 秒…")
            if not self._sleep(self.travel_wait):
                raise RuntimeError("已停止。")
            self.log("到达等待结束。")
            self.done.emit()
        except Exception as exc:
            msg = str(exc)
            if msg != "已停止。":
                self.log(f"失败：{msg}")
                self.failed.emit(msg)
            else:
                self.failed.emit("已停止。")
