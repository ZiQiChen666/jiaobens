#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""地图中文标注学习 / 识别寻路.

思路：
    * 打开游戏地图后截图 + OCR，读出地图上的中文标注；
    * 用户选择某个标注，把它记成“打怪点 / BOSS点”；
    * 运行时重新 OCR，找到同一个中文标注，点击它的位置；
    * 地图拖动/缩放也能跟着文字走，不依赖固定像素。

坐标全部相对游戏客户区左上角，使用归一化值保存。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
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
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
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


def compact_text(text: str) -> str:
    return re.sub(r"[\s:：,，.。|丨/\\_\-—]+", "", text or "")


def text_matches(target: str, candidate: str) -> float:
    a = compact_text(target)
    b = compact_text(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.92
    return SequenceMatcher(None, a, b).ratio()


class MapLabelCanvas(QWidget):
    """显示地图截图、OCR 框和已添加的目标点。"""

    item_selected = Signal(int)
    offset_calibrated = Signal(float, float)
    actual_marked = Signal(float, float)

    def __init__(self, image: QImage, parent=None) -> None:
        super().__init__(parent)
        self.image = image
        self.scale = 1.0
        self.items: list[dict] = []
        self.selected_index = -1
        self.targets: list[dict] = []
        self.calibrating_target = -1
        self.mark_actual = False
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

    def set_image(self, image: QImage, items: list[dict]) -> None:
        self.image = image
        self.items = items
        self.selected_index = -1
        self.calibrating_target = -1
        self._apply_size()
        self.update()

    def set_targets(self, targets: list[dict]) -> None:
        self.targets = targets
        self.update()

    def select_index(self, index: int) -> None:
        self.selected_index = index
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
        if self.mark_actual:
            self.mark_actual = False
            self.actual_marked.emit(nx, ny)
            return
        if 0 <= self.calibrating_target < len(self.targets):
            target = self.targets[self.calibrating_target]
            target["offset_x"] = nx - float(target.get("nx", 0.5))
            target["offset_y"] = ny - float(target.get("ny", 0.5))
            self.calibrating_target = -1
            self.offset_calibrated.emit(nx, ny)
            self.update()
            return
        iw = max(1, self.image.width())
        ih = max(1, self.image.height())
        for idx, item in enumerate(self.items):
            x0 = float(item.get("x0", 0)) / iw
            x1 = float(item.get("x1", 0)) / iw
            y0 = float(item.get("y0", 0)) / ih
            y1 = float(item.get("y1", 0)) / ih
            if x0 <= nx <= x1 and y0 <= ny <= y1:
                self.selected_index = idx
                self.item_selected.emit(idx)
                self.update()
                return

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self.rect(), self.image)
        iw = max(1, self.image.width())
        ih = max(1, self.image.height())
        s = self.scale
        painter.setFont(QFont("Microsoft YaHei", 8))
        for idx, item in enumerate(self.items):
            x0 = int(float(item.get("x0", 0)) * s)
            x1 = int(float(item.get("x1", 0)) * s)
            y0 = int(float(item.get("y0", 0)) * s)
            y1 = int(float(item.get("y1", 0)) * s)
            selected = idx == self.selected_index
            painter.setPen(
                QPen(QColor("#ffd60a") if selected else QColor(80, 180, 255, 170), 2 if selected else 1)
            )
            painter.drawRect(QRect(x0, y0, max(2, x1 - x0), max(2, y1 - y0)))
            if selected:
                painter.setPen(QPen(QColor("#ffd60a"), 1))
                painter.drawText(
                    x0,
                    max(12, y0 - 3),
                    str(item.get("text", ""))[:20],
                )
        for idx, target in enumerate(self.targets):
            try:
                nx = float(target.get("nx", 0.5)) + float(target.get("offset_x", 0.0))
                ny = float(target.get("ny", 0.5)) + float(target.get("offset_y", 0.0))
            except (TypeError, ValueError):
                continue
            x = int(nx * iw * s)
            y = int(ny * ih * s)
            color = QColor("#ff3b30") if target.get("kind") != "boss" else QColor("#3ddc84")
            painter.setPen(QPen(color, 3))
            painter.drawLine(x - 16, y, x + 16, y)
            painter.drawLine(x, y - 16, x, y + 16)
            painter.setPen(QPen(color, 1))
            painter.setFont(QFont("Microsoft YaHei", 9, QFont.Weight.Bold))
            painter.drawText(
                x + 20,
                max(14, y - 6),
                f"{target.get('name', '')} [{TARGET_KINDS.get(str(target.get('kind')), '')}]",
            )
        if self.mark_actual:
            painter.setPen(QPen(QColor("#ff3b30"), 2))
            painter.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
            painter.drawText(20, 30, "请点击真正的入口位置（会自动关联最近的中文标注）")
        elif 0 <= self.calibrating_target < len(self.targets):
            painter.setPen(QPen(QColor("#ffd60a"), 2))
            painter.setFont(QFont("Microsoft YaHei", 11, QFont.Weight.Bold))
            painter.drawText(20, 30, "请在截图上点击真正的入口位置…")


class MapLabelDialog(QDialog):
    """打开地图后，截图 + OCR，选择中文标注并保存为地图目标。"""

    def __init__(self, hwnd: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("地图标注学习（中文 OCR）")
        self.resize(1500, 950)
        self.hwnd = int(hwnd)
        self.maps = load_maps()
        self.current_map_index = -1
        self.image: Optional[QImage] = None
        self.items: list[dict] = []
        self.targets: list[dict] = []
        self.selected_item = -1
        self.saved = False
        self._manual_marking = False

        self._build_ui()
        self._refresh_map_combo()
        self.capture_and_ocr()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(8)

        top = QGroupBox("当前地图")
        top_form = QFormLayout(top)
        self.edit_map_name = QLineEdit()
        self.edit_map_name.setPlaceholderText("例如：晨瞻之森|赤砂壳原")
        self.edit_map_check = QLineEdit()
        self.edit_map_check.setPlaceholderText(
            "用于运行时确认地图的文字，例如：晨瞻之森（可留空）"
        )
        row = QHBoxLayout()
        row.addWidget(QLabel("已保存地图"))
        self.combo_maps = QComboBox()
        self.combo_maps.setMinimumWidth(260)
        self.btn_load_map = QPushButton("载入")
        self.btn_capture = QPushButton("重新截图 + OCR")
        row.addWidget(self.combo_maps, 1)
        row.addWidget(self.btn_load_map)
        row.addWidget(self.btn_capture)
        top_form.addRow("地图名", self.edit_map_name)
        top_form.addRow("地图校验文字", self.edit_map_check)
        top_form.addRow("", row)
        root.addWidget(top)

        body = QHBoxLayout()
        body.setSpacing(10)

        left = QVBoxLayout()
        self.status = QLabel("正在截图/OCR…")
        self.status.setStyleSheet("color: #1565c0;")
        left.addWidget(self.status)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.canvas = MapLabelCanvas(QImage(), self)
        scroll.setWidget(self.canvas)
        left.addWidget(scroll, 1)
        body.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(8)
        ocr_box = QGroupBox("OCR 识别到的中文标注")
        ocr_layout = QVBoxLayout(ocr_box)
        self.list_ocr = QListWidget()
        self.list_ocr.setMinimumWidth(320)
        ocr_layout.addWidget(self.list_ocr)
        right.addWidget(ocr_box, 1)

        target_box = QGroupBox("把选中的标注设为目标")
        target_form = QFormLayout(target_box)
        self.edit_target_name = QLineEdit()
        self.combo_target_kind = QComboBox()
        for key, label in TARGET_KINDS.items():
            self.combo_target_kind.addItem(label, key)
        self.btn_add_target = QPushButton("添加/更新这个目标")
        target_form.addRow("目标名称", self.edit_target_name)
        target_form.addRow("目标类型", self.combo_target_kind)
        target_form.addRow("", self.btn_add_target)
        quick_row = QHBoxLayout()
        self.btn_auto_labels = QPushButton("自动标注全部地图文字")
        self.btn_auto_labels.setToolTip(
            "自动把地图区域识别到的中文标注加入候选列表，你再删掉不需要的"
        )
        self.btn_manual_point = QPushButton("手动点选坐标")
        self.btn_manual_point.setToolTip(
            "不依赖 OCR：先填名称/选类型，再点这里，然后在截图上点真正的入口"
        )
        self.btn_mark_actual = QPushButton("点选实际入口（自动关联文字）")
        self.btn_mark_actual.setToolTip(
            "先选目标类型，再点这里，然后在截图上点真正入口；程序会自动关联最近的中文标注并保存偏移"
        )
        self.btn_set_farm = QPushButton("选中→打怪点")
        self.btn_set_boss = QPushButton("选中→BOSS点")
        quick_row.addWidget(self.btn_auto_labels)
        quick_row.addWidget(self.btn_manual_point)
        quick_row.addWidget(self.btn_mark_actual)
        quick_row.addWidget(self.btn_set_farm)
        quick_row.addWidget(self.btn_set_boss)
        target_form.addRow("快捷", quick_row)
        right.addWidget(target_box)

        targets_box = QGroupBox("本图已添加的目标")
        targets_layout = QVBoxLayout(targets_box)
        self.list_targets = QListWidget()
        targets_layout.addWidget(self.list_targets)
        target_btns = QHBoxLayout()
        self.btn_calibrate = QPushButton("校准实际入口")
        self.btn_delete_target = QPushButton("删除目标")
        target_btns.addWidget(self.btn_calibrate)
        target_btns.addWidget(self.btn_delete_target)
        targets_layout.addLayout(target_btns)
        right.addWidget(targets_box, 1)

        body.addLayout(right, 0)
        root.addLayout(body, 1)

        bottom = QHBoxLayout()
        self.btn_save = QPushButton("保存地图")
        self.btn_save.setMinimumHeight(36)
        self.btn_cancel = QPushButton("关闭")
        bottom.addStretch(1)
        bottom.addWidget(self.btn_cancel)
        bottom.addWidget(self.btn_save)
        root.addLayout(bottom)

        hint = QLabel(
            "推荐手动标注：打开地图 → 重新截图 → 填名称/选类型 → 点“手动点选坐标” → "
            "在截图上点真正入口。程序只记住“名称 → 窗口相对坐标”，不依赖 OCR。"
            "OCR 认不出小字时完全不影响手动标注。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        root.addWidget(hint)

        self.list_ocr.currentRowChanged.connect(self._on_ocr_row)
        self.list_targets.currentRowChanged.connect(self._on_target_row)
        self.btn_add_target.clicked.connect(self.add_target)
        self.btn_auto_labels.clicked.connect(self.auto_annotate_all)
        self.btn_manual_point.clicked.connect(self.start_manual_point)
        self.btn_mark_actual.clicked.connect(self.start_mark_actual)
        self.canvas.actual_marked.connect(self._on_actual_marked_or_manual)
        self.btn_set_farm.clicked.connect(lambda: self.set_selected_kind("farm"))
        self.btn_set_boss.clicked.connect(lambda: self.set_selected_kind("boss"))
        self.btn_calibrate.clicked.connect(self.start_calibrate)
        self.btn_delete_target.clicked.connect(self.delete_target)
        self.btn_capture.clicked.connect(self.capture_and_ocr)
        self.btn_load_map.clicked.connect(self.load_selected_map)
        self.btn_save.clicked.connect(self.save_current_map)
        self.btn_cancel.clicked.connect(self.reject)
        self.canvas.item_selected.connect(self._on_canvas_item)
        self.canvas.offset_calibrated.connect(self._on_offset_done)
        self.combo_target_kind.currentIndexChanged.connect(self._on_target_kind)

    # ------------------------------------------------------------------
    def _refresh_map_combo(self) -> None:
        self.combo_maps.clear()
        for mp in self.maps:
            self.combo_maps.addItem(str(mp.get("name", "未命名")), mp)

    def load_selected_map(self) -> None:
        mp = self.combo_maps.currentData()
        if not isinstance(mp, dict):
            return
        self.edit_map_name.setText(str(mp.get("name", "")))
        self.edit_map_check.setText(str(mp.get("check", "")))
        self.targets = [dict(t) for t in mp.get("targets", []) if isinstance(t, dict)]
        self.canvas.set_targets(self.targets)
        self._refresh_target_list()
        self.status.setText(f"已载入地图：{mp.get('name', '')}")
        self.current_map_index = self.maps.index(mp)

    # ------------------------------------------------------------------
    def capture_and_ocr(self) -> None:
        app = _app()
        if app is None or not hasattr(app, "capture_client_bgra"):
            QMessageBox.warning(self, "不可用", "主程序截图模块不可用。")
            return
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            QMessageBox.warning(self, "找不到游戏窗口", "请先绑定/启动 CreatureCurios。")
            return
        self.status.setText("正在截图…")
        QApplication.processEvents()
        cap, err = app.capture_client_bgra(self.hwnd)
        if cap is None:
            self.status.setText(f"截图失败：{err}")
            return
        w, h, data = cap
        self.image = QImage(
            bytes(data), w, h, w * 4, QImage.Format.Format_RGB32
        ).copy()
        # 先把截图显示出来：即使 OCR 失败，也可以手动点选坐标
        self.items = []
        self.list_ocr.clear()
        self.canvas.set_image(self.image, [])
        self.canvas.set_targets(self.targets)
        self.status.setText("截图完成。OCR 识别中…（识别不到可直接手动点选）")
        QApplication.processEvents()
        try:
            items = app.ocr_screen_items(data, w, h)
        except Exception as exc:
            self.status.setText(
                f"OCR 失败：{type(exc).__name__}: {exc}；可直接用“手动点选坐标”"
            )
            return
        items = [
            it
            for it in items
            if float(it.get("score", 0)) >= 0.25
            and str(it.get("text", "")).strip()
        ]
        items.sort(key=lambda it: (float(it.get("y0", 0)), float(it.get("x0", 0))))
        self.items = items
        self.selected_item = -1
        self.list_ocr.clear()
        for idx, item in enumerate(items):
            text = str(item.get("text", ""))
            if not text:
                continue
            self.list_ocr.addItem(
                f"{text}   ({float(item.get('score', 0)):.2f})"
            )
        self.canvas.set_image(self.image, items)
        self.canvas.set_targets(self.targets)
        self.status.setText(
            f"识别到 {len(items)} 个文字框；请选中地图上的中文标注。"
        )
        if not self.edit_map_name.text().strip():
            self._guess_map_name(items)

    def _guess_map_name(self, items: list[dict]) -> None:
        candidates = []
        for item in items:
            text = str(item.get("text", ""))
            if any(k in text for k in ("森", "原", "海湾", "城", "岛", "谷")):
                candidates.append(text)
        if candidates:
            self.edit_map_name.setText(candidates[0][:40])

    # ------------------------------------------------------------------
    def _on_ocr_row(self, row: int) -> None:
        if 0 <= row < len(self.items):
            self.selected_item = row
            self.canvas.select_index(row)
            text = str(self.items[row].get("text", ""))
            if text and not self.edit_target_name.text().strip():
                self.edit_target_name.setText(text)
            self.status.setText(f"已选标注：{text}")

    def _on_canvas_item(self, index: int) -> None:
        if 0 <= index < len(self.items):
            self.list_ocr.setCurrentRow(index)

    def _on_target_kind(self, _index: int) -> None:
        pass

    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_map_label(item: dict) -> bool:
        text = str(item.get("text", "")).strip()
        if not text:
            return False
        if float(item.get("score", 0)) < 0.35:
            return False
        if len(text) > 14:
            return False
        chinese = len(re.findall(r"[一-鿿]", text))
        if chinese < 2:
            return False
        block_words = (
            "当前位置", "传送门", "危险区域", "说明", "任务", "经验", "获得",
            "系统", "世界", "发送", "仓库", "设置", "杂货铺", "制作组",
            "背包", "材料", "装备", "全部", "出售", "单价", "总价",
            "数量", "最大", "攻击", "防御", "生命", "速度", "命中",
            "暴击", "闪躲", "疲劳", "名称", "类型", "等级", "属性",
        )
        if any(w in text for w in block_words):
            return False
        # 太靠上的 HUD / 太靠下的聊天栏先排除
        ny = float(item.get("ny", 0.5))
        if ny < 0.12 or ny > 0.82:
            return False
        return True

    def auto_annotate_all(self) -> None:
        if not self.items:
            QMessageBox.information(self, "没有文字", "先截图 + OCR。")
            return
        added = 0
        existing = {compact_text(str(t.get("text", ""))) for t in self.targets}
        for item in self.items:
            if not self._looks_like_map_label(item):
                continue
            text = str(item.get("text", "")).strip()
            key = compact_text(text)
            if key in existing:
                continue
            self.targets.append(
                {
                    "name": text,
                    "kind": "other",
                    "text": text,
                    "nx": float(item.get("nx", 0.5)),
                    "ny": float(item.get("ny", 0.5)),
                    "offset_x": 0.0,
                    "offset_y": 0.0,
                }
            )
            existing.add(key)
            added += 1
        self.canvas.set_targets(self.targets)
        self._refresh_target_list()
        if self.targets:
            self.list_targets.setCurrentRow(0)
        self.status.setText(
            f"自动标注完成：新增 {added} 个候选；在右侧目标列表里选出打怪点/BOSS点即可"
        )

    def start_manual_point(self) -> None:
        if self.image is None:
            QMessageBox.information(self, "没有地图", "先点“重新截图 + OCR”把地图截下来。")
            return
        name = self.edit_target_name.text().strip()
        if not name:
            name, ok = QInputDialog.getText(
                self, "目标名称", "给这个入口起个名字（例如：大角城 / 打怪点 / BOSS）："
            )
            if not ok or not name.strip():
                return
            self.edit_target_name.setText(name.strip())
        self._manual_marking = True
        self.canvas.mark_actual = True
        self.canvas.update()
        self.status.setText("请在左侧截图上点击真正的入口坐标（不依赖 OCR）")

    def _on_manual_marked(self, nx: float, ny: float) -> None:
        name = self.edit_target_name.text().strip()
        if not name:
            name, ok = QInputDialog.getText(
                self, "目标名称", "给这个入口起个名字："
            )
            if not ok or not name.strip():
                return
            name = name.strip()
            self.edit_target_name.setText(name)
        kind = str(self.combo_target_kind.currentData() or "farm")
        target = {
            "name": name,
            "kind": kind,
            "text": "",
            "nx": float(nx),
            "ny": float(ny),
            "offset_x": 0.0,
            "offset_y": 0.0,
        }
        for idx, old in enumerate(self.targets):
            if old.get("name") == name and old.get("kind") == kind:
                self.targets[idx] = target
                break
        else:
            self.targets.append(target)
        self._manual_marking = False
        self.canvas.set_targets(self.targets)
        self._refresh_target_list()
        self.list_targets.setCurrentRow(len(self.targets) - 1)
        self.status.setText(
            f"已保存{TARGET_KINDS.get(kind, kind)}「{name}」："
            f"{nx*100:.1f}%, {ny*100:.1f}%（窗口相对坐标）"
        )

    def start_mark_actual(self) -> None:
        if self.image is None:
            QMessageBox.information(self, "没有地图", "先截图 + OCR。")
            return
        self.canvas.mark_actual = True
        self.canvas.update()
        self.status.setText(
            "请在左侧截图上点击真正的入口；会自动找最近的中文标注并保存偏移"
        )

    def _on_actual_marked_or_manual(self, nx: float, ny: float) -> None:
        if self._manual_marking:
            self._on_manual_marked(nx, ny)
        else:
            self._on_actual_marked(nx, ny)

    def _on_actual_marked(self, nx: float, ny: float) -> None:
        # 找最近的中文标注作为锚点
        best = None
        best_dist = 1e9
        for item in self.items:
            if not self._looks_like_map_label(item):
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            dx = float(item.get("nx", 0.5)) - nx
            dy = float(item.get("ny", 0.5)) - ny
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best = item
        kind = str(self.combo_target_kind.currentData() or "farm")
        if best is not None and best_dist <= 0.32:
            text = str(best.get("text", "")).strip()
            target = {
                "name": self.edit_target_name.text().strip() or text,
                "kind": kind,
                "text": text,
                "nx": float(best.get("nx", 0.5)),
                "ny": float(best.get("ny", 0.5)),
                "offset_x": nx - float(best.get("nx", 0.5)),
                "offset_y": ny - float(best.get("ny", 0.5)),
            }
            note = f"已关联标注「{text}」，偏移 ({target['offset_x']*100:.1f}%, {target['offset_y']*100:.1f}%)"
        else:
            target = {
                "name": self.edit_target_name.text().strip() or f"入口 {len(self.targets)+1}",
                "kind": kind,
                "text": "",
                "nx": nx,
                "ny": ny,
                "offset_x": 0.0,
                "offset_y": 0.0,
            }
            note = "附近没有可关联的中文标注，已保存为固定坐标"
        # 同名同类型更新
        for idx, old in enumerate(self.targets):
            if old.get("name") == target["name"] and old.get("kind") == target["kind"]:
                self.targets[idx] = target
                break
        else:
            self.targets.append(target)
        self.canvas.set_targets(self.targets)
        self._refresh_target_list()
        self.list_targets.setCurrentRow(len(self.targets) - 1)
        self.status.setText(f"已添加{TARGET_KINDS.get(kind, kind)}：{target['name']}；{note}")

    def set_selected_kind(self, kind: str) -> None:
        row = self.list_targets.currentRow()
        if 0 <= row < len(self.targets):
            self.targets[row]["kind"] = kind
            self.canvas.set_targets(self.targets)
            self._refresh_target_list()
            self.list_targets.setCurrentRow(row)
            self.status.setText(
                f"已把「{self.targets[row].get('name', '')}」设为{TARGET_KINDS.get(kind, kind)}"
            )
            return
        if 0 <= self.selected_item < len(self.items):
            item = self.items[self.selected_item]
            text = str(item.get("text", "")).strip()
            name = self.edit_target_name.text().strip() or text
            target = {
                "name": name,
                "kind": kind,
                "text": text,
                "nx": float(item.get("nx", 0.5)),
                "ny": float(item.get("ny", 0.5)),
                "offset_x": 0.0,
                "offset_y": 0.0,
            }
            self.targets.append(target)
            self.canvas.set_targets(self.targets)
            self._refresh_target_list()
            self.list_targets.setCurrentRow(len(self.targets) - 1)
            self.status.setText(
                f"已添加「{name}」为{TARGET_KINDS.get(kind, kind)}"
            )
            return
        QMessageBox.information(
            self, "先选标注", "请先在左侧截图/OCR列表里选一个标注，或在目标列表里选一个目标。"
        )

    # ------------------------------------------------------------------
    def add_target(self) -> None:
        if not (0 <= self.selected_item < len(self.items)):
            QMessageBox.information(self, "先选标注", "请先选中一个 OCR 中文标注。")
            return
        name = self.edit_target_name.text().strip()
        if not name:
            name = str(self.items[self.selected_item].get("text", "")).strip()
        if not name:
            QMessageBox.warning(self, "缺少名称", "请填写目标名称。")
            return
        item = self.items[self.selected_item]
        target = {
            "name": name,
            "kind": str(self.combo_target_kind.currentData() or "farm"),
            "text": str(item.get("text", "")).strip(),
            "nx": float(item.get("nx", 0.5)),
            "ny": float(item.get("ny", 0.5)),
            "offset_x": 0.0,
            "offset_y": 0.0,
        }
        # 同名同类型则更新
        for idx, old in enumerate(self.targets):
            if old.get("name") == name and old.get("kind") == target["kind"]:
                self.targets[idx] = target
                break
        else:
            self.targets.append(target)
        self.canvas.set_targets(self.targets)
        self._refresh_target_list()
        self.status.setText(f"已添加目标：{name}（{TARGET_KINDS.get(target['kind'])}）")

    def _refresh_target_list(self) -> None:
        self.list_targets.blockSignals(True)
        self.list_targets.clear()
        for target in self.targets:
            anchor = str(target.get("text", "")).strip() or "固定坐标"
            self.list_targets.addItem(
                f"{target.get('name', '')}  [{TARGET_KINDS.get(str(target.get('kind')), '')}]  "
                f"锚点：{anchor}"
            )
        self.list_targets.blockSignals(False)

    def _on_target_row(self, row: int) -> None:
        if 0 <= row < len(self.targets):
            target = self.targets[row]
            self.edit_target_name.setText(str(target.get("name", "")))
            idx = self.combo_target_kind.findData(str(target.get("kind", "farm")))
            if idx >= 0:
                self.combo_target_kind.setCurrentIndex(idx)

    def start_calibrate(self) -> None:
        row = self.list_targets.currentRow()
        if row < 0 or row >= len(self.targets):
            QMessageBox.information(self, "先选目标", "请先在“本图已添加的目标”里选中一个目标。")
            return
        self.canvas.calibrating_target = row
        self.canvas.update()
        self.status.setText("请在左侧截图上点击真正的入口位置（会记录相对标注的偏移）")

    def _on_offset_done(self, nx: float, ny: float) -> None:
        self.status.setText(
            f"点击偏移已校准：本次点击 {nx*100:.1f}%, {ny*100:.1f}%"
        )

    def delete_target(self) -> None:
        row = self.list_targets.currentRow()
        if row < 0 or row >= len(self.targets):
            return
        del self.targets[row]
        self.canvas.set_targets(self.targets)
        self._refresh_target_list()

    # ------------------------------------------------------------------
    def save_current_map(self) -> None:
        name = self.edit_map_name.text().strip()
        if not name:
            QMessageBox.warning(self, "缺少地图名", "请填写地图名。")
            return
        if not self.targets:
            QMessageBox.warning(self, "没有目标", "请至少添加一个目标。")
            return
        profile = {
            "name": name,
            "check": self.edit_map_check.text().strip(),
            "targets": [dict(t) for t in self.targets],
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        existing = next(
            (i for i, mp in enumerate(self.maps) if str(mp.get("name", "")) == name),
            None,
        )
        if existing is not None:
            self.maps[existing] = profile
            self.current_map_index = existing
        else:
            self.maps.append(profile)
            self.current_map_index = len(self.maps) - 1
        if not save_maps(self.maps):
            QMessageBox.warning(self, "保存失败", f"无法写入：{MAPS_PATH}")
            return
        self.saved = True
        self._refresh_map_combo()
        self.status.setText(f"已保存地图：{name}（{len(self.targets)} 个目标）")
        beep(1500, 100)
        QMessageBox.information(
            self, "已保存", f"地图「{name}」已保存。\n{MAPS_PATH}"
        )
        self.accept()


class MapTravelRunner(QThread):
    """打开地图 → OCR 找目标标注 → 点击 → 关闭地图 → 等自动寻路。"""

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
        retries: int = 3,
        parent=None,
    ):
        super().__init__(parent)
        self.hwnd = int(hwnd)
        self.profile = dict(profile)
        self.target = dict(target)
        self.map_key = str(map_key or "m")
        self.close_key = str(close_key or "esc")
        self.travel_wait = max(1.0, float(travel_wait))
        self.retries = max(1, int(retries))
        self._stop = False
        self._last_image: Optional[QImage] = None
        self._fail_reason = ""

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

    def _find_target(self) -> Optional[dict]:
        app = _app()
        if app is None:
            return None
        cap, _err = app.capture_client_bgra(self.hwnd)
        if cap is None:
            return None
        w, h, data = cap
        self._last_image = QImage(
            bytes(data), w, h, w * 4, QImage.Format.Format_RGB32
        ).copy()
        items = app.ocr_screen_items(data, w, h)
        check = compact_text(str(self.profile.get("check", "")))
        if check:
            texts = [compact_text(str(it.get("text", ""))) for it in items]
            if not any(check in t or t in check for t in texts if t):
                self._fail_reason = f"没识别到地图校验文字「{check}」，可能不是目标地图"
                self.log(self._fail_reason)
                return None
        target_text = str(self.target.get("text", "")).strip()
        best = None
        best_score = 0.0
        for item in items:
            ratio = text_matches(target_text, str(item.get("text", "")))
            if ratio > best_score:
                best_score = ratio
                best = item
        if best is not None and best_score >= 0.60:
            best = dict(best)
            best["_match_score"] = best_score
            return best
        self._fail_reason = f"没识别到目标标注「{target_text}」"
        return None

    def run(self) -> None:
        try:
            if not win32gui.IsWindow(self.hwnd):
                raise RuntimeError("游戏窗口已关闭。")
            self.log(f"打开地图，寻找标注「{self.target.get('text', '')}」…")
            self._tap_key(self.map_key)
            if not self._sleep(1.2):
                raise RuntimeError("已停止。")
            target_text = str(self.target.get("text", "")).strip()
            if not target_text:
                # 没有文字锚点：直接使用学习时记录的实际窗口坐标
                nx = float(self.target.get("nx", 0.5)) + float(
                    self.target.get("offset_x", 0.0)
                )
                ny = float(self.target.get("ny", 0.5)) + float(
                    self.target.get("offset_y", 0.0)
                )
                self.log(
                    f"目标没有文字锚点，使用固定窗口坐标 "
                    f"{nx*100:.1f}%, {ny*100:.1f}%"
                )
                self._click_norm(nx, ny)
                if not self._sleep(0.5):
                    raise RuntimeError("已停止。")
                self._tap_key(self.close_key)
                self.log(f"已关闭地图，等待自动寻路 {self.travel_wait:.0f} 秒…")
                if not self._sleep(self.travel_wait):
                    raise RuntimeError("已停止。")
                self.done.emit()
                return
            match = None
            self._fail_reason = ""
            for attempt in range(self.retries):
                if self._stop:
                    raise RuntimeError("已停止。")
                match = self._find_target()
                if match is not None:
                    break
                self.log(f"第 {attempt + 1} 次没找到标注，重试…")
                if not self._sleep(0.8):
                    raise RuntimeError("已停止。")
            if match is None:
                path = ""
                if self._last_image is not None:
                    name = (
                        "map_fail_"
                        f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                    )
                    path = os.path.join(APP_FOLDER, name)
                    try:
                        self._last_image.save(path)
                    except Exception:
                        path = ""
                raise RuntimeError(
                    (self._fail_reason or "地图上没识别到目标标注")
                    + (f"；截图：{path}" if path else "")
                )
            nx = float(match.get("nx", 0.5)) + float(self.target.get("offset_x", 0.0))
            ny = float(match.get("ny", 0.5)) + float(self.target.get("offset_y", 0.0))
            self.log(
                f"找到「{match.get('text', '')}」({match.get('_match_score', 0):.2f})，"
                f"点击 {nx*100:.1f}%, {ny*100:.1f}%"
            )
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
