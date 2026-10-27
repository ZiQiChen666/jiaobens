#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打怪路线编辑器.

用法：
    1. 新建路线，填写路线名/地图名；
    2. 按 F8，切到游戏地图，点击地图入口；
    3. 按 F9，切到游戏，按顺序点击路径点；按 F10 停止；
    4. 在表格里给每一步填等待时间和说明；
    5. 保存路线。

只记录鼠标左键点击，不发送任何输入。游戏需要在前台，点击需要落在游戏窗口内。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import threading
from typing import Optional

from PySide6.QtCore import QRect, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import win32api
import win32gui

try:
    import mouse
except Exception:
    mouse = None

try:
    import keyboard
except Exception:
    keyboard = None

try:
    import winsound
except Exception:
    winsound = None

def _app():
    """取主程序模块，兼容直接运行 .py（__main__）和打包后运行。"""
    return sys.modules.get("creature_curios_keyer") or sys.modules.get("__main__")


ROUTES_PATH = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "CreatureCuriosKeyer",
    "routes.json",
)


def beep(freq: int = 900, ms: int = 90) -> None:
    if winsound is None:
        return
    try:
        winsound.Beep(freq, ms)
    except Exception:
        pass


def find_game_window() -> int:
    found: list[tuple[int, str, str]] = []

    def cb(hwnd: int, _lparam) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd).strip()
            if "CreatureCurios".lower() not in title.lower():
                return True
            found.append((int(hwnd), title, win32gui.GetClassName(hwnd)))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    for hwnd, _title, cls in found:
        if "unity" in cls.lower():
            return hwnd
    return found[0][0] if found else 0


class PointPickCanvas(QWidget):
    """在截图上点一个点，返回窗口内归一化坐标。"""

    point_changed = Signal(object)

    def __init__(self, image: QImage, parent=None) -> None:
        super().__init__(parent)
        self.image = image
        self.scale = 1.0
        self.point: Optional[tuple[float, float]] = None
        self._apply_size()

    def _apply_size(self) -> None:
        iw = max(1, self.image.width())
        ih = max(1, self.image.height())
        screen = QApplication.primaryScreen()
        max_w, max_h = 1500, 900
        if screen is not None:
            avail = screen.availableGeometry()
            max_w = max(400, min(1500, avail.width() - 80))
            max_h = max(300, min(900, avail.height() - 180))
        self.scale = min(max_w / iw, max_h / ih, 1.0)
        self.setFixedSize(
            max(1, int(iw * self.scale)), max(1, int(ih * self.scale))
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        iw = max(1, self.image.width())
        ih = max(1, self.image.height())
        x = event.position().x() / max(0.0001, self.scale)
        y = event.position().y() / max(0.0001, self.scale)
        nx = max(0.0, min(1.0, x / iw))
        ny = max(0.0, min(1.0, y / ih))
        self.point = (nx, ny)
        self.point_changed.emit(self.point)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self.rect(), self.image)
        # 10% 网格，方便确认位置
        painter.setPen(QPen(QColor(255, 255, 255, 90), 1))
        iw = max(1, self.image.width())
        ih = max(1, self.image.height())
        for i in range(1, 10):
            x = int(iw * i / 10 * self.scale)
            y = int(ih * i / 10 * self.scale)
            painter.drawLine(x, 0, x, self.height())
            painter.drawLine(0, y, self.width(), y)
        if self.point is not None:
            x = int(self.point[0] * iw * self.scale)
            y = int(self.point[1] * ih * self.scale)
            painter.setPen(QPen(QColor("#ff3b30"), 3))
            painter.drawLine(x - 18, y, x + 18, y)
            painter.drawLine(x, y - 18, x, y + 18)
            painter.setFont(QFont("Microsoft YaHei", 10, QFont.Weight.Bold))
            painter.drawText(
                x + 22,
                max(16, y - 8),
                f"{self.point[0]*100:.1f}%, {self.point[1]*100:.1f}%",
            )


class PointPickDialog(QDialog):
    def __init__(
        self,
        image: QImage,
        title: str,
        info: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)
        info_label = QLabel(info)
        info_label.setWordWrap(True)
        layout.addWidget(info_label)
        self.canvas = PointPickCanvas(image, self)
        layout.addWidget(self.canvas, 0, Qt.AlignmentFlag.AlignCenter)
        self.status = QLabel("尚未选择点位")
        self.status.setStyleSheet("color: #b26a00;")
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.btn_ok = QPushButton("使用这个点")
        self.btn_ok.setEnabled(False)
        self.btn_cancel = QPushButton("取消")
        self.btn_ok.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_cancel)
        buttons.addWidget(self.btn_ok)
        layout.addLayout(buttons)
        self.canvas.point_changed.connect(self._on_point)
        self.adjustSize()

    def _on_point(self, point) -> None:
        if point is None:
            return
        self.status.setText(
            f"已选：{point[0]*100:.1f}%, {point[1]*100:.1f}%"
        )
        self.status.setStyleSheet("color: #2e7d32;")
        self.btn_ok.setEnabled(True)

    def selected_point(self) -> Optional[tuple[float, float]]:
        return self.canvas.point


class ClickPathRecorder:
    """记录游戏窗口内的连续鼠标左键点击。"""

    def __init__(self, hwnd: int, callback):
        self.hwnd = int(hwnd)
        self.callback = callback
        self._hook = None
        self._active = False

    def start(self) -> None:
        if mouse is None:
            raise RuntimeError("缺少 mouse 库。")
        self._active = True
        self._hook = mouse.hook(self._on_mouse)

    def stop(self) -> None:
        self._active = False
        try:
            if self._hook is not None:
                mouse.unhook(self._hook)
        except Exception:
            pass
        self._hook = None

    def _on_mouse(self, event) -> None:
        if not self._active:
            return
        try:
            if not isinstance(event, mouse.ButtonEvent):
                return
            if event.event_type != "down" or str(event.button) != "left":
                return
            app = _app()
            if app is None or not app._foreground_is_window(self.hwnd):
                return
            x, y = mouse.get_position()
            left, top = win32gui.ClientToScreen(self.hwnd, (0, 0))
            _l, _t, cw, ch = win32gui.GetClientRect(self.hwnd)
            cw, ch = int(cw), int(ch)
            if cw <= 0 or ch <= 0:
                return
            if not (left <= x < left + cw and top <= y < top + ch):
                return
            nx = (float(x) - float(left)) / float(cw)
            ny = (float(y) - float(top)) / float(ch)
            self.callback(max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny)))
        except Exception:
            pass


class RouteEditor(QMainWindow):
    arm_entrance_requested = Signal()
    start_record_requested = Signal()
    stop_record_requested = Signal()
    point_recorded = Signal(float, float)
    entrance_recorded = Signal(object)

    def __init__(self, game_hwnd: int = 0, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("打怪路线编辑器")
        self.resize(1180, 860)
        self.hwnd = int(game_hwnd) if game_hwnd and win32gui.IsWindow(game_hwnd) else find_game_window()
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.routes: list[dict] = []
        self.current_index = -1
        self.entrance: Optional[tuple[float, float]] = None
        self._point_capture = None
        self._click_recorder: Optional[ClickPathRecorder] = None
        self._hotkeys: list = []
        self._closing = False

        self._build_ui()
        self._connect_signals()
        self._install_hotkeys()
        self._load_routes()
        self._update_entrance_label()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(8)

        split = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("路线列表"))
        self.list_routes = QListWidget()
        self.list_routes.setMinimumWidth(190)
        left_layout.addWidget(self.list_routes, 1)
        row = QHBoxLayout()
        self.btn_new = QPushButton("新建路线")
        self.btn_del = QPushButton("删除路线")
        row.addWidget(self.btn_new)
        row.addWidget(self.btn_del)
        left_layout.addLayout(row)
        split.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        form_box = QGroupBox("路线信息")
        form = QFormLayout(form_box)
        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("例如：冰牙鲨 / 大角犀 / 刷材料")
        self.edit_map = QLineEdit()
        self.edit_map.setPlaceholderText("当前地图名，例如：晨瞻之森 / 赤砂壳原")
        self.edit_map_key = QLineEdit("m")
        self.edit_map_key.setFixedWidth(80)
        self.edit_close_key = QLineEdit("esc")
        self.edit_close_key.setFixedWidth(80)
        self.combo_path_mode = QComboBox()
        self.combo_path_mode.addItem("游戏世界（关闭地图后点）", "world")
        self.combo_path_mode.addItem("地图界面（地图保持打开）", "map")
        self.combo_path_mode.setFixedWidth(230)
        self.spin_entrance_wait = QDoubleSpinBox()
        self.spin_entrance_wait.setRange(0, 180)
        self.spin_entrance_wait.setValue(20)
        self.spin_entrance_wait.setSuffix(" 秒")
        self.spin_entrance_wait.setFixedWidth(100)
        self.lbl_entrance = QLabel("入口点：未记录")
        self.lbl_entrance.setStyleSheet("color: #b26a00;")
        self.btn_capture_entrance = QPushButton("截图标记入口")
        self.btn_capture_entrance.setToolTip(
            "先打开游戏地图，再点这里；程序截取游戏画面，你在截图上点入口，"
            "不需要按 F8，也不会走到入口里"
        )
        self.btn_arm_entrance = QPushButton("F8 记录入口")
        self.btn_arm_entrance.setToolTip("备用：按 F8 后，切到游戏地图，点击一次入口点")
        form.addRow("路线名", self.edit_name)
        form.addRow("地图名", self.edit_map)
        form.addRow("路径所在界面", self.combo_path_mode)
        keys_row = QHBoxLayout()
        keys_row.addWidget(QLabel("地图键"))
        keys_row.addWidget(self.edit_map_key)
        keys_row.addSpacing(10)
        keys_row.addWidget(QLabel("关闭键"))
        keys_row.addWidget(self.edit_close_key)
        keys_row.addSpacing(10)
        keys_row.addWidget(QLabel("入口等待"))
        keys_row.addWidget(self.spin_entrance_wait)
        keys_row.addStretch(1)
        form.addRow("按键/等待", keys_row)
        entrance_row = QHBoxLayout()
        entrance_row.addWidget(self.lbl_entrance, 1)
        entrance_row.addWidget(self.btn_capture_entrance)
        entrance_row.addWidget(self.btn_arm_entrance)
        form.addRow("入口", entrance_row)
        right_layout.addWidget(form_box)

        rec_box = QGroupBox("路径步骤")
        rec_layout = QVBoxLayout(rec_box)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["#", "类型", "内容（相对窗口左上角）", "等待(秒)", "说明"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        rec_layout.addWidget(self.table, 1)

        btns = QHBoxLayout()
        self.btn_start_record = QPushButton("开始连续记录（3秒后）")
        self.btn_start_record.setToolTip("点按钮后有 3 秒切到游戏；也可以直接按 F9 立即开始")
        self.btn_stop_record = QPushButton("停止记录")
        self.btn_capture_step = QPushButton("截图添加路径点")
        self.btn_capture_step.setToolTip(
            "先站到路径位置，再点这里；程序截取游戏画面，你在截图上点目标位置"
        )
        self.btn_add_wait = QPushButton("添加等待")
        self.btn_add_key = QPushButton("添加按键")
        self.btn_up = QPushButton("上移")
        self.btn_down = QPushButton("下移")
        self.btn_del_step = QPushButton("删除步骤")
        for b in (
            self.btn_start_record,
            self.btn_stop_record,
            self.btn_capture_step,
            self.btn_add_wait,
            self.btn_add_key,
            self.btn_up,
            self.btn_down,
            self.btn_del_step,
        ):
            btns.addWidget(b)
        btns.addStretch(1)
        rec_layout.addLayout(btns)
        right_layout.addWidget(rec_box, 1)

        fixed_box = QGroupBox("固定路径输入（可选）")
        fixed_layout = QVBoxLayout(fixed_box)
        self.text_fixed = QPlainTextEdit()
        self.text_fixed.setPlaceholderText(
            "每行一个步骤，例如：\n"
            "入口 40,29 等待20 说明: 地图入口\n"
            "点击 50,50 等待8 说明: 走到路口\n"
            "按键 w 按住0.5 等待1 说明: 开门\n"
            "等待 3 说明: 等加载"
        )
        self.text_fixed.setMaximumHeight(120)
        fixed_layout.addWidget(self.text_fixed)
        fixed_row = QHBoxLayout()
        self.btn_parse = QPushButton("解析并替换步骤")
        self.btn_preview = QPushButton("预览当前路线 JSON")
        fixed_row.addWidget(self.btn_parse)
        fixed_row.addWidget(self.btn_preview)
        fixed_row.addStretch(1)
        fixed_layout.addLayout(fixed_row)
        right_layout.addWidget(fixed_box)

        note_box = QGroupBox("路线说明")
        note_layout = QVBoxLayout(note_box)
        self.text_note = QPlainTextEdit()
        self.text_note.setMaximumHeight(70)
        self.text_note.setPlaceholderText("例如：到冰牙海湾后沿右边走，看到蓝色水晶后往上…")
        note_layout.addWidget(self.text_note)
        right_layout.addWidget(note_box)

        bottom = QHBoxLayout()
        self.lbl_status = QLabel("就绪")
        self.lbl_status.setStyleSheet("color: #1565c0;")
        self.btn_save = QPushButton("保存路线")
        self.btn_save.setMinimumHeight(34)
        bottom.addWidget(self.lbl_status, 1)
        bottom.addWidget(self.btn_save)
        right_layout.addLayout(bottom)

        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)

        hint = QLabel(
            "所有坐标以游戏窗口客户区左上角为基准（表格里显示为窗口内百分比），"
            "窗口移动或改大小不影响。"
            "热键：F8 记录入口点；F9 开始连续记录点击；F10 停止记录。"
            "记录时请让游戏在前台，点击落在游戏窗口内。"
            "这里只记录点击，不会自动操作游戏。"
            f"  保存位置：{ROUTES_PATH}"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        root.addWidget(hint)

    def _connect_signals(self) -> None:
        self.arm_entrance_requested.connect(
            self.arm_entrance, Qt.ConnectionType.QueuedConnection
        )
        self.start_record_requested.connect(
            self.start_recording, Qt.ConnectionType.QueuedConnection
        )
        self.stop_record_requested.connect(
            self.stop_recording, Qt.ConnectionType.QueuedConnection
        )
        self.point_recorded.connect(
            self.add_click_step, Qt.ConnectionType.QueuedConnection
        )
        self.entrance_recorded.connect(
            self.set_entrance, Qt.ConnectionType.QueuedConnection
        )
        self.btn_capture_entrance.clicked.connect(self.record_entrance_by_screenshot)
        self.btn_arm_entrance.clicked.connect(self.arm_entrance)
        self.btn_start_record.clicked.connect(self.start_recording_delayed)
        self.btn_capture_step.clicked.connect(self.add_step_by_screenshot)
        self.btn_stop_record.clicked.connect(self.stop_recording)
        self.btn_add_wait.clicked.connect(self.add_wait_step)
        self.btn_add_key.clicked.connect(self.add_key_step)
        self.btn_up.clicked.connect(lambda: self.move_step(-1))
        self.btn_down.clicked.connect(lambda: self.move_step(1))
        self.btn_del_step.clicked.connect(self.delete_step)
        self.btn_parse.clicked.connect(self.parse_fixed_path)
        self.btn_preview.clicked.connect(self.preview_json)
        self.btn_new.clicked.connect(self.new_route)
        self.btn_del.clicked.connect(self.delete_route)
        self.btn_save.clicked.connect(self.save_route)
        self.list_routes.currentRowChanged.connect(self.load_route_at)

    def _install_hotkeys(self) -> None:
        if keyboard is None:
            self.lbl_status.setText("缺少 keyboard 库，热键不可用；仍可用按钮操作。")
            return
        try:
            self._hotkeys.append(
                keyboard.add_hotkey(
                    "f8", self.arm_entrance_requested.emit, suppress=False
                )
            )
            self._hotkeys.append(
                keyboard.add_hotkey(
                    "f9", self.start_record_requested.emit, suppress=False
                )
            )
            self._hotkeys.append(
                keyboard.add_hotkey(
                    "f10", self.stop_record_requested.emit, suppress=False
                )
            )
        except Exception as exc:
            self.lbl_status.setText(f"热键注册失败：{exc}")

    # ------------------------------------------------------------------
    def _load_routes(self) -> None:
        try:
            with open(ROUTES_PATH, encoding="utf-8") as f:
                data = json.load(f)
            routes = data.get("routes", [])
            if isinstance(routes, list):
                self.routes = [r for r in routes if isinstance(r, dict)]
        except FileNotFoundError:
            self.routes = []
        except Exception as exc:
            QMessageBox.warning(self, "读取路线失败", f"{type(exc).__name__}: {exc}")
            self.routes = []
        self.refresh_route_list()
        if self.routes:
            self.list_routes.setCurrentRow(0)
        else:
            self.new_route(confirm=False)

    def refresh_route_list(self) -> None:
        self.list_routes.blockSignals(True)
        self.list_routes.clear()
        for route in self.routes:
            self.list_routes.addItem(str(route.get("name", "未命名")))
        self.list_routes.blockSignals(False)

    def _save_routes(self) -> bool:
        os.makedirs(os.path.dirname(ROUTES_PATH), exist_ok=True)
        tmp = ROUTES_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"routes": self.routes},
                    f,
                    ensure_ascii=False,
                    indent=1,
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, ROUTES_PATH)
            return True
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return False

    def new_route(self, confirm: bool = True) -> None:
        if confirm and (self.edit_name.text().strip() or self.table.rowCount()):
            answer = QMessageBox.question(
                self,
                "新建路线",
                "当前编辑的内容没有保存，继续新建？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.current_index = -1
        self.list_routes.clearSelection()
        self.edit_name.setText("新路线")
        self.edit_map.setText("")
        self.edit_map_key.setText("m")
        self.edit_close_key.setText("esc")
        self.combo_path_mode.setCurrentIndex(0)
        self.spin_entrance_wait.setValue(20)
        self.entrance = None
        self.table.setRowCount(0)
        self.text_note.clear()
        self.text_fixed.clear()
        self._update_entrance_label()
        self.lbl_status.setText("新路线：请填写名称，按 F8 记录地图入口")

    def load_route_at(self, index: int) -> None:
        if index < 0 or index >= len(self.routes):
            return
        route = self.routes[index]
        self.current_index = index
        self.edit_name.setText(str(route.get("name", "")))
        self.edit_map.setText(str(route.get("map", "")))
        self.edit_map_key.setText(str(route.get("map_key", "m")))
        self.edit_close_key.setText(str(route.get("close_key", "esc")))
        mode = str(route.get("path_mode", "world"))
        idx = self.combo_path_mode.findData(mode)
        self.combo_path_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.spin_entrance_wait.setValue(float(route.get("entrance_wait", 20.0) or 0.0))
        entrance = route.get("entrance")
        self.entrance = None
        try:
            if isinstance(entrance, (list, tuple)) and len(entrance) == 2:
                self.entrance = (float(entrance[0]), float(entrance[1]))
        except (TypeError, ValueError):
            pass
        self._update_entrance_label()
        self.table.setRowCount(0)
        for step in route.get("steps", []):
            if isinstance(step, dict):
                self._append_step(step)
        self.text_note.setPlainText(str(route.get("note", "")))
        self.lbl_status.setText(f"已加载：{route.get('name', '')}")

    def delete_route(self) -> None:
        if self.current_index < 0 or self.current_index >= len(self.routes):
            QMessageBox.information(self, "删除路线", "请先在左侧选择一条路线。")
            return
        name = str(self.routes[self.current_index].get("name", ""))
        answer = QMessageBox.question(
            self,
            "删除路线",
            f"确定删除路线「{name}」？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        del self.routes[self.current_index]
        self._save_routes()
        self.refresh_route_list()
        self.current_index = -1
        if self.routes:
            self.list_routes.setCurrentRow(0)
        else:
            self.new_route(confirm=False)

    # ------------------------------------------------------------------
    def arm_entrance(self) -> None:
        self._cancel_point_capture()
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            self.hwnd = find_game_window()
        if not self.hwnd:
            self.lbl_status.setText("找不到游戏窗口，请先启动 CreatureCurios。")
            return
        try:
            app = _app()
            if app is None or not hasattr(app, "PointCapture"):
                raise RuntimeError("主程序模块不可用。")
            self._point_capture = app.PointCapture(
                self.hwnd,
                "entrance",
                lambda _kind, point: self.entrance_recorded.emit(point),
            )
            self._point_capture.start()
        except Exception as exc:
            self._point_capture = None
            self.lbl_status.setText(f"入口点记录失败：{exc}")
            return
        beep(1100, 80)
        self.lbl_status.setText("请切到游戏地图，点击一次入口点（只记录第一个左键）")

    def _cancel_point_capture(self) -> None:
        capture = self._point_capture
        self._point_capture = None
        if capture is not None:
            try:
                capture.cancel()
            except Exception:
                pass

    def set_entrance(self, point) -> None:
        try:
            nx, ny = float(point[0]), float(point[1])
        except Exception:
            return
        self.entrance = (max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny)))
        self._cancel_point_capture()
        self._update_entrance_label()
        beep(1400, 80)
        self.lbl_status.setText("入口点已记录")

    def _update_entrance_label(self) -> None:
        if self.entrance is None:
            self.lbl_entrance.setText("入口点：未记录")
            self.lbl_entrance.setStyleSheet("color: #b26a00;")
        else:
            self.lbl_entrance.setText(
                f"入口点：{self.entrance[0]*100:.1f}%, {self.entrance[1]*100:.1f}%"
            )
            self.lbl_entrance.setStyleSheet("color: #2e7d32;")

    # ------------------------------------------------------------------
    def _capture_game_image(self) -> Optional[QImage]:
        app = _app()
        if app is None or not hasattr(app, "capture_client_bgra"):
            self.lbl_status.setText("主程序截图模块不可用。")
            return None
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            self.hwnd = find_game_window()
        if not self.hwnd:
            self.lbl_status.setText("找不到游戏窗口，请先启动 CreatureCurios。")
            return None
        cap, err = app.capture_client_bgra(self.hwnd)
        if cap is None:
            self.lbl_status.setText(f"截图失败：{err}")
            return None
        w, h, data = cap
        return QImage(
            bytes(data), w, h, w * 4, QImage.Format.Format_RGB32
        ).copy()

    def record_entrance_by_screenshot(self) -> None:
        self._cancel_point_capture()
        image = self._capture_game_image()
        if image is None:
            return
        dlg = PointPickDialog(
            image,
            "标记地图入口",
            "先确认游戏里地图已经打开。然后在这张截图上点击地图入口。\n"
            "这里只是点截图，不会走到入口、不会进入副本。",
            self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            point = dlg.selected_point()
            if point is not None:
                self.set_entrance(point)

    def add_step_by_screenshot(self) -> None:
        image = self._capture_game_image()
        if image is None:
            return
        dlg = PointPickDialog(
            image,
            "标记路径点",
            "先让游戏画面停在当前路径位置。然后在这张截图上点击目标点。\n"
            "这里只是点截图，不会操作游戏。",
            self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            point = dlg.selected_point()
            if point is not None:
                self.add_click_step(point[0], point[1])

    def start_recording_delayed(self) -> None:
        if self._click_recorder is not None:
            return
        self.lbl_status.setText("3 秒后开始记录，请切到游戏；F10 或“停止记录”结束")
        beep(1000, 80)
        QTimer.singleShot(3000, self.start_recording)

    # ------------------------------------------------------------------
    def start_recording(self) -> None:
        if self._click_recorder is not None:
            return
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            self.hwnd = find_game_window()
        if not self.hwnd:
            self.lbl_status.setText("找不到游戏窗口，请先启动 CreatureCurios。")
            return
        try:
            self._click_recorder = ClickPathRecorder(
                self.hwnd,
                lambda nx, ny: self.point_recorded.emit(nx, ny),
            )
            self._click_recorder.start()
        except Exception as exc:
            self._click_recorder = None
            self.lbl_status.setText(f"连续记录失败：{exc}")
            return
        beep(1200, 80)
        self.lbl_status.setText("连续记录中：切到游戏，按顺序点击路径点；F10 停止")

    def stop_recording(self) -> None:
        recorder = self._click_recorder
        self._click_recorder = None
        if recorder is not None:
            recorder.stop()
            beep(600, 100)
            self.lbl_status.setText("连续记录已停止")
        self._cancel_point_capture()

    def add_click_step(self, nx: float, ny: float) -> None:
        step = {
            "type": "click",
            "x": float(nx),
            "y": float(ny),
            "wait": 5.0,
            "note": "",
        }
        self._append_step(step)
        self.lbl_status.setText(
            f"已记录点击 {nx*100:.1f}%, {ny*100:.1f}%（等待可修改）"
        )

    # ------------------------------------------------------------------
    def _append_step(self, step: dict) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        typ = str(step.get("type", "click"))
        if typ == "click":
            content = f"{float(step.get('x', 0))*100:.1f}, {float(step.get('y', 0))*100:.1f}"
            wait = float(step.get("wait", 5.0) or 0.0)
            type_text = "点击"
        elif typ == "key":
            content = f"{step.get('name', '')}|{float(step.get('hold', 0.05) or 0.0):.3f}"
            wait = float(step.get("wait", 1.0) or 0.0)
            type_text = "按键"
        else:
            content = "-"
            wait = float(step.get("seconds", 3.0) or 0.0)
            type_text = "等待"
        self.table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
        self.table.setItem(row, 1, QTableWidgetItem(type_text))
        item_content = QTableWidgetItem(content)
        item_content.setFlags(item_content.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, 2, item_content)
        self.table.setItem(row, 3, QTableWidgetItem(f"{wait:g}"))
        self.table.setItem(row, 4, QTableWidgetItem(str(step.get("note", ""))))

    def add_wait_step(self) -> None:
        self._append_step({"type": "wait", "seconds": 3.0, "note": ""})
        self.table.selectRow(self.table.rowCount() - 1)

    def add_key_step(self) -> None:
        key, ok = QInputDialog.getText(
            self, "添加按键", "按键名（例如 w / f / 1 / esc）："
        )
        if not ok or not key.strip():
            return
        hold, ok = QInputDialog.getDouble(
            self, "按住时间", "按住秒数：", 0.05, 0.0, 10.0, 3
        )
        if not ok:
            return
        self._append_step(
            {
                "type": "key",
                "name": key.strip(),
                "hold": float(hold),
                "wait": 1.0,
                "note": "",
            }
        )
        self.table.selectRow(self.table.rowCount() - 1)

    def move_step(self, delta: int) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        target = row + delta
        if target < 0 or target >= self.table.rowCount():
            return
        self._swap_rows(row, target)
        self.table.selectRow(target)

    def _swap_rows(self, a: int, b: int) -> None:
        data_a = [self.table.item(a, c).text() if self.table.item(a, c) else "" for c in range(5)]
        data_b = [self.table.item(b, c).text() if self.table.item(b, c) else "" for c in range(5)]
        for c in range(5):
            self.table.setItem(a, c, QTableWidgetItem(data_b[c]))
            self.table.setItem(b, c, QTableWidgetItem(data_a[c]))
        self._renumber()

    def delete_step(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        self.table.removeRow(row)
        self._renumber()

    def _renumber(self) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                self.table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
            else:
                item.setText(str(row + 1))

    # ------------------------------------------------------------------
    def _parse_percent(self, text: str) -> Optional[float]:
        try:
            value = float(text.strip().replace("%", ""))
        except (TypeError, ValueError):
            return None
        if value > 1.0:
            value /= 100.0
        return max(0.0, min(1.0, value))

    def _client_size(self) -> tuple[int, int]:
        try:
            _l, _t, w, h = win32gui.GetClientRect(self.hwnd)
            return max(1, int(w)), max(1, int(h))
        except Exception:
            return 0, 0

    def _parse_user_coord(self, text: str, dimension: int) -> Optional[float]:
        """固定路径坐标：40%,29% 按百分比；546,225 按窗口像素。"""
        raw = text.strip()
        if not raw:
            return None
        if raw.endswith("%"):
            return self._parse_percent(raw)
        try:
            value = float(raw)
        except ValueError:
            return None
        if abs(value) <= 1.0:
            return max(0.0, min(1.0, value))
        # 没有写 % 且数值 <=100：兼容以前的百分比写法；
        # 大于 100 的一律按窗口像素处理。
        if abs(value) <= 100.0:
            return max(0.0, min(1.0, value / 100.0))
        if dimension > 0:
            return max(0.0, min(1.0, value / float(dimension)))
        return None

    def parse_fixed_path(self) -> None:
        text = self.text_fixed.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "固定路径", "请先粘贴路径内容。")
            return
        answer = QMessageBox.question(
            self,
            "解析固定路径",
            "会用输入内容替换当前入口和步骤，继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.table.setRowCount(0)
        entrance = None
        entrance_wait = float(self.spin_entrance_wait.value())
        errors: list[str] = []
        cw, ch = self._client_size()
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            note = ""
            m_note = re.search(r"(?:说明|note)\s*[:：]\s*(.*)$", line, re.IGNORECASE)
            if m_note:
                note = m_note.group(1).strip()
                line = line[: m_note.start()].strip()
            m_wait = re.search(r"(?:等待|wait)\s*[:：]?\s*(\d+(?:\.\d+)?)", line, re.IGNORECASE)
            wait = float(m_wait.group(1)) if m_wait else None
            if m_wait:
                line = (line[: m_wait.start()] + line[m_wait.end():]).strip()
            if line.startswith(("入口", "entrance")):
                line = re.sub(r"^(入口|entrance)\s*", "", line, flags=re.IGNORECASE)
                m_xy = re.search(r"(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)", line)
                if not m_xy:
                    errors.append(f"第 {lineno} 行：入口缺少坐标")
                    continue
                nx = self._parse_user_coord(m_xy.group(1), cw)
                ny = self._parse_user_coord(m_xy.group(2), ch)
                if nx is None or ny is None:
                    errors.append(f"第 {lineno} 行：入口坐标无效")
                    continue
                entrance = (nx, ny)
                if wait is not None:
                    entrance_wait = wait
                continue
            if line.startswith(("点击", "click")):
                line = re.sub(r"^(点击|click)\s*", "", line, flags=re.IGNORECASE)
                m_xy = re.search(r"(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)", line)
                if not m_xy:
                    errors.append(f"第 {lineno} 行：点击缺少坐标")
                    continue
                nx = self._parse_user_coord(m_xy.group(1), cw)
                ny = self._parse_user_coord(m_xy.group(2), ch)
                if nx is None or ny is None:
                    errors.append(f"第 {lineno} 行：点击坐标无效")
                    continue
                self._append_step(
                    {
                        "type": "click",
                        "x": nx,
                        "y": ny,
                        "wait": wait if wait is not None else 5.0,
                        "note": note,
                    }
                )
                continue
            if line.startswith(("按键", "key")):
                line = re.sub(r"^(按键|key)\s*", "", line, flags=re.IGNORECASE)
                m_hold = re.search(r"(?:按住|hold)\s*[:：]?\s*(\d+(?:\.\d+)?)", line, re.IGNORECASE)
                hold = float(m_hold.group(1)) if m_hold else 0.05
                if m_hold:
                    line = (line[: m_hold.start()] + line[m_hold.end():]).strip()
                m_key = re.search(r"[A-Za-z0-9_]+", line)
                if not m_key:
                    errors.append(f"第 {lineno} 行：按键缺少键名")
                    continue
                self._append_step(
                    {
                        "type": "key",
                        "name": m_key.group(0),
                        "hold": hold,
                        "wait": wait if wait is not None else 1.0,
                        "note": note,
                    }
                )
                continue
            if line.startswith(("等待", "wait")):
                seconds = wait if wait is not None else 3.0
                self._append_step(
                    {"type": "wait", "seconds": seconds, "note": note}
                )
                continue
            errors.append(f"第 {lineno} 行：无法识别：{raw}")
        if entrance is not None:
            self.entrance = entrance
            self.spin_entrance_wait.setValue(entrance_wait)
            self._update_entrance_label()
        if errors:
            QMessageBox.warning(
                self, "部分行未解析", "\n".join(errors[:12])
            )
        self.lbl_status.setText(
            f"固定路径解析完成：{self.table.rowCount()} 个步骤"
        )

    # ------------------------------------------------------------------
    def _collect_route(self) -> Optional[dict]:
        name = self.edit_name.text().strip()
        if not name:
            QMessageBox.warning(self, "缺少路线名", "请填写路线名。")
            return None
        if self.entrance is None:
            QMessageBox.warning(
                self, "缺少入口点", "请按 F8 在游戏地图上记录一次入口点。"
            )
            return None
        steps: list[dict] = []
        for row in range(self.table.rowCount()):
            type_text = self.table.item(row, 1).text() if self.table.item(row, 1) else ""
            content = self.table.item(row, 2).text() if self.table.item(row, 2) else ""
            wait_text = self.table.item(row, 3).text() if self.table.item(row, 3) else "0"
            note = self.table.item(row, 4).text() if self.table.item(row, 4) else ""
            try:
                wait = float(wait_text)
            except ValueError:
                QMessageBox.warning(self, "等待时间无效", f"第 {row+1} 行等待时间不是数字。")
                return None
            if type_text == "点击":
                m = re.search(r"(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)", content)
                if not m:
                    QMessageBox.warning(self, "坐标无效", f"第 {row+1} 行点击坐标无效。")
                    return None
                nx = self._parse_percent(m.group(1))
                ny = self._parse_percent(m.group(2))
                if nx is None or ny is None:
                    QMessageBox.warning(self, "坐标无效", f"第 {row+1} 行点击坐标无效。")
                    return None
                steps.append(
                    {"type": "click", "x": nx, "y": ny, "wait": wait, "note": note}
                )
            elif type_text == "按键":
                parts = content.split("|", 1)
                key = parts[0].strip()
                hold = 0.05
                if len(parts) > 1:
                    try:
                        hold = float(parts[1])
                    except ValueError:
                        hold = 0.05
                if not key:
                    QMessageBox.warning(self, "按键无效", f"第 {row+1} 行没有键名。")
                    return None
                steps.append(
                    {
                        "type": "key",
                        "name": key,
                        "hold": hold,
                        "wait": wait,
                        "note": note,
                    }
                )
            elif type_text == "等待":
                steps.append(
                    {"type": "wait", "seconds": wait, "note": note}
                )
            else:
                QMessageBox.warning(self, "类型无效", f"第 {row+1} 行类型无效。")
                return None
        return {
            "name": name,
            "map": self.edit_map.text().strip(),
            "map_key": self.edit_map_key.text().strip() or "m",
            "close_key": self.edit_close_key.text().strip() or "esc",
            "path_mode": str(self.combo_path_mode.currentData() or "world"),
            "entrance": [float(self.entrance[0]), float(self.entrance[1])],
            "entrance_wait": float(self.spin_entrance_wait.value()),
            "steps": steps,
            "note": self.text_note.toPlainText().strip(),
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def save_route(self) -> None:
        route = self._collect_route()
        if route is None:
            return
        name = route["name"]
        existing = next(
            (i for i, r in enumerate(self.routes) if str(r.get("name", "")) == name),
            None,
        )
        if existing is not None and existing != self.current_index:
            answer = QMessageBox.question(
                self,
                "路线已存在",
                f"已有同名路线「{name}」，覆盖？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if existing is not None:
            self.routes[existing] = route
            self.current_index = existing
        else:
            self.routes.append(route)
            self.current_index = len(self.routes) - 1
        if not self._save_routes():
            return
        self.refresh_route_list()
        self.list_routes.setCurrentRow(self.current_index)
        self.lbl_status.setText(
            f"已保存：{name}（{len(route['steps'])} 个步骤）→ {ROUTES_PATH}"
        )
        beep(1500, 100)

    def preview_json(self) -> None:
        route = self._collect_route()
        if route is None:
            return
        text = json.dumps(route, ensure_ascii=False, indent=2)
        dlg = QMessageBox(self)
        dlg.setWindowTitle("路线 JSON")
        dlg.setText(text)
        dlg.setDetailedText(text)
        dlg.exec()

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        self._closing = True
        self.stop_recording()
        for handle in self._hotkeys:
            try:
                keyboard.remove_hotkey(handle)
            except Exception:
                pass
        self._hotkeys = []
        super().closeEvent(event)

