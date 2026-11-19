#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""副本流程页（主界面「副本流程」标签页）

流程就三步，界面上也只围绕这三步：

    ① 进本  地图1 点「副本入口」→ 识别进本弹窗 → 点「进入游戏」
    ② 挂机  地图2 点「BOSS 点」→ 挂机 N 秒（喝药 / 拾取由这里配）
    ③ 退出  点「退出副本」→ 点「确认」

多开：下面 4 行，每个窗口可以设不同的挂机秒数，各跑各的。
"""

from __future__ import annotations

import datetime
import json
import os
import threading
from typing import Optional

import creature_curios_keyer as app
import dungeon_flow as df

try:
    import win32gui
except ImportError:  # pragma: no cover
    win32gui = None  # type: ignore

from PySide6.QtCore import QMetaObject, QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QInputDialog,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


LIGHT_QSS = """
QWidget { background: #ffffff; color: #111111; font-size: 13px; }
QScrollArea, QScrollArea > QWidget > QWidget { background: #ffffff; }
QGroupBox {
    background: #ffffff; border: 1px solid #c3c8cf; border-radius: 8px;
    margin-top: 14px; padding: 14px 12px 12px 12px; font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 10px; padding: 0 5px; color: #111111; background: #ffffff;
}
QLabel { color: #111111; background: transparent; }
QLabel#muted { color: #333333; }
QCheckBox { color: #111111; background: transparent; }
QPushButton {
    background: #f1f3f6; color: #111111; border: 1px solid #b7bdc6;
    border-radius: 6px; padding: 6px 10px;
}
QPushButton:hover { background: #e5e9ef; }
QPushButton:disabled { color: #999999; background: #f5f6f8; }
QPushButton#primary {
    background: #16a34a; color: #ffffff; font-weight: 700;
    border: none; padding: 9px 18px; font-size: 14px;
}
QPushButton#primary:hover { background: #15803d; }
QPushButton#primary:disabled { background: #9fd3b0; color: #ffffff; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextBrowser {
    background: #ffffff; color: #111111; border: 1px solid #b7bdc6;
    border-radius: 6px; padding: 4px 6px;
}
QComboBox QAbstractItemView { background: #ffffff; color: #111111; }
QPlainTextEdit { font-family: Consolas, "Microsoft YaHei UI", monospace; }
"""


def qimage_from_frame(frame) -> QImage:
    w, h, data = frame
    return QImage(bytes(data), w, h, w * 4, QImage.Format.Format_RGB32).copy()


def map_png_path(tag: str = "map") -> str:
    return os.path.join(app._data_folder(), f"dungeon_{tag}.png")


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setObjectName("muted")
    return lab


def pick_region(parent, hwnd: int, profile: dict, where: str,
                title: str, info: str) -> bool:
    """截图 + 框选区域。where: 'enter.dialog' 或 'farm.hp_roi'。"""
    if not hwnd:
        QMessageBox.warning(parent, "没有绑定窗口",
                            "请先到「单开挂机」页绑定 CreatureCurios 游戏窗口。")
        return False
    frame = app.capture_client_bgra(hwnd)[0]
    if frame is None:
        QMessageBox.warning(parent, "截图失败", "抓不到游戏画面（窗口是否最小化？）")
        return False
    cache = {"frame": frame}
    image = qimage_from_frame(frame)

    spec = {}
    if where == "enter.dialog":
        spec = ((profile.get("enter") or {}).get("dialog") or {})
    elif where == "farm.hp_roi":
        spec = {"roi": (profile.get("farm") or {}).get("hp_roi")}
    current = None
    roi = spec.get("roi")
    if roi:
        w, h = image.width(), image.height()
        current = (
            int(roi[0] * w), int(roi[1] * h),
            max(5, int(roi[2] * w)), max(3, int(roi[3] * h)),
        )

    def recapture():
        cap = app.capture_client_bgra(hwnd)[0]
        if cap is None:
            return None
        cache["frame"] = cap
        return qimage_from_frame(cap)

    dialog = app.RegionSelectDialog(
        image, current, recapture, parent, title,
        info + chr(10) + "（框完点「保存区域」；比目标松一圈，四周留点背景）",
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return False
    rect = dialog.selected_roi()
    if rect is None:
        return False
    f = cache["frame"]
    w, h = f[0], f[1]
    roi_norm = [
        round(rect[0] / w, 6), round(rect[1] / h, 6),
        round(rect[2] / w, 6), round(rect[3] / h, 6),
    ]
    if where == "enter.dialog":
        sig = df.signature(df.crop_norm(f, roi_norm), 12)
        profile.setdefault("enter", {})["dialog"] = {
            "roi": roi_norm, "grid": 12, "sig": sig, "sim": 0.90,
        }
    elif where == "farm.hp_roi":
        profile.setdefault("farm", {})["hp_roi"] = roi_norm
    elif where == "fatigue.roi":
        profile.setdefault("fatigue", {})["roi"] = roi_norm
    elif where == "enter.progress":
        profile.setdefault("enter", {})["progress"] = {
            "roi": roi_norm, "min_fill": 0.90, "samples": 3, "interval": 0.25,
        }
    elif where == "enter.loaded":
        sig = df.signature(df.crop_norm(f, roi_norm), 12)
        profile.setdefault("enter", {})["loaded"] = {
            "roi": roi_norm, "grid": 12, "sig": sig, "sim": 0.90,
        }
    elif where == "farm.boss_present":
        sig = df.signature(df.crop_norm(f, roi_norm), 12)
        profile.setdefault("farm", {})["boss_present"] = {
            "roi": roi_norm, "grid": 12, "sig": sig, "sigs": [sig],
            "sim": 0.60, "search": 1.6, "tol": 30.0,
            "still_seconds": 0.8, "still_timeout": 12.0, "still_diff": 0.015,
        }
    elif where == "farm.boss_hp":
        profile.setdefault("farm", {})["boss_hp"] = {
            "roi": roi_norm, "min_fill": 0.15, "miss": 3,
        }
    elif where == "farm.arrived":
        sig = df.signature(df.crop_norm(f, roi_norm), 12)
        profile.setdefault("farm", {})["arrived"] = {
            "roi": roi_norm, "grid": 12, "sig": sig, "sim": 0.90,
        }
    elif where == "mount.text":
        profile.setdefault("mount", {})["text_roi"] = roi_norm
    elif where in ("mount.dialog", "mount.state"):
        sig = df.signature(df.crop_norm(f, roi_norm), 12)
        profile.setdefault("mount", {})[where.split(".")[1]] = {
            "roi": roi_norm, "grid": 12, "sig": sig, "sim": 0.90,
        }
    elif where == "exit.dialog":
        sig = df.signature(df.crop_norm(f, roi_norm), 12)
        profile.setdefault("exit", {})["dialog"] = {
            "roi": roi_norm, "grid": 12, "sig": sig, "sim": 0.90,
        }
    else:
        return False
    try:
        df.save_profile(profile)
    except Exception:
        pass
    return True


def pick_point(parent, hwnd: int, title: str, info: str,
               open_map_key: Optional[str] = None,
               current: Optional[list] = None) -> Optional[list[float]]:
    """截一张图，让用户点一个位置；open_map_key 不为空时先按开图键。"""
    if not hwnd:
        QMessageBox.warning(parent, "没有绑定窗口",
                            "请先到「单开挂机」页绑定 CreatureCurios 游戏窗口。")
        return None

    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setModal(True)
    dialog.resize(980, 700)
    lay = QVBoxLayout(dialog)
    lay.addWidget(_hint(info))

    canvas = MapCanvas()
    lay.addWidget(canvas, 1)

    state = {"pt": list(current) if current else None}

    def refresh_marks() -> None:
        canvas.set_points(
            [{"name": "已选", "kind": "point", "pt": state["pt"]}]
            if state["pt"] else []
        )

    def on_click(nx: float, ny: float) -> None:
        state["pt"] = [round(nx, 5), round(ny, 5)]
        refresh_marks()
        lbl.setText(f"已选：{state['pt']}")

    def capture(send_key: bool) -> None:
        if send_key and open_map_key:
            df.WinGame(hwnd, {}).tap(open_map_key)
            import time
            time.sleep(1.2)
        frame = app.capture_client_bgra(hwnd)[0]
        if frame is None:
            QMessageBox.warning(dialog, "截图失败", "抓不到游戏画面（窗口是否最小化？）")
            return
        canvas.set_image(qimage_from_frame(frame))
        refresh_marks()

    canvas.point_clicked.connect(on_click)
    row = QHBoxLayout()
    btn_shot = QPushButton("重新截图")
    btn_shot.clicked.connect(lambda: capture(bool(open_map_key)))
    lbl = QLabel("还没选点：在图上点一下位置")
    lbl.setObjectName("muted")
    btn_ok = QPushButton("保存")
    btn_ok.setDefault(True)
    btn_ok.clicked.connect(dialog.accept)
    btn_cancel = QPushButton("取消")
    btn_cancel.clicked.connect(dialog.reject)
    row.addWidget(btn_shot)
    row.addWidget(lbl, 1)
    row.addWidget(btn_cancel)
    row.addWidget(btn_ok)
    lay.addLayout(row)

    capture(bool(open_map_key))
    if state["pt"]:
        refresh_marks()
        lbl.setText(f"当前：{state['pt']}")
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return state["pt"]


class MapCanvas(QWidget):
    """显示一张截图，点一下给一个归一化坐标；已选点画在上面。"""

    point_clicked = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pix: Optional[QPixmap] = None
        self._points: list[dict] = []
        self.setMinimumSize(420, 280)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_image(self, image: QImage) -> None:
        self._pix = QPixmap.fromImage(image) if not image.isNull() else None
        self.update()

    def set_points(self, points: list[dict]) -> None:
        self._points = list(points)
        self.update()

    def _target_rect(self):
        if self._pix is None:
            return 0, 0, 1, 1
        aw, ah = self.width(), self.height()
        iw, ih = self._pix.width(), self._pix.height()
        scale = min(aw / iw, ah / ih)
        w, h = int(iw * scale), int(ih * scale)
        return (aw - w) // 2, (ah - h) // 2, max(1, w), max(1, h)

    def _to_norm(self, pos):
        ox, oy, w, h = self._target_rect()
        nx = (pos.x() - ox) / w
        ny = (pos.y() - oy) / h
        if nx < 0 or ny < 0 or nx > 1 or ny > 1:
            return None
        return nx, ny

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(28, 30, 34))
        if self._pix is None:
            painter.setPen(QColor(150, 150, 156))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "还没有截图")
            return
        ox, oy, w, h = self._target_rect()
        painter.drawPixmap(ox, oy, w, h, self._pix)
        for item in self._points:
            try:
                nx, ny = float(item["pt"][0]), float(item["pt"][1])
            except (TypeError, ValueError, IndexError):
                continue
            x, y = ox + nx * w, oy + ny * h
            painter.setPen(QPen(QColor(255, 210, 90), 2))
            painter.setBrush(QColor(255, 210, 90))
            painter.drawEllipse(QPoint(int(x), int(y)), 6, 6)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(QPoint(int(x) + 9, int(y) - 6),
                             str(item.get("name") or ""))

    def mousePressEvent(self, event) -> None:
        if self._pix is None:
            return
        n = self._to_norm(event.position().toPoint())
        if n is not None:
            self.point_clicked.emit(n[0], n[1])


class DungeonThread(QThread):
    line = Signal(str)
    finished_ok = Signal(int, int)

    def __init__(self, hwnd: int, profile: dict, rounds: Optional[int],
                 dry: bool = False, prefix: str = "", parent=None):
        super().__init__(parent)
        self.hwnd = int(hwnd)
        self.profile = profile
        self.rounds = rounds
        self.dry = bool(dry)
        self.prefix = prefix
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            game = df.WinGame(
                self.hwnd, self.profile, log=self.line.emit, dry=self.dry,
                snapshot_dir=df.snapshot_dir(), prefix=self.prefix,
                stop_event=self._stop,
            )
            runner = df.DungeonRunner(
                game, self.profile, log=self.line.emit,
                stop_event=self._stop, verbose=True,
            )
            runner.run(self.rounds)
            self.finished_ok.emit(runner.stats["ok"], runner.stats["fail"])
        except Exception as exc:  # pragma: no cover
            self.line.emit(f"[错误] {type(exc).__name__}: {exc}")
            self.finished_ok.emit(0, 0)


class DungeonTab(QWidget):
    hotkey_start = Signal()
    hotkey_stop = Signal()

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.profile = df.load_profile()
        self.threads: dict[int, DungeonThread] = {}
        self._nav_recorder = None
        self._nav_pending = False
        self._loading = True
        self.setStyleSheet(LIGHT_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll)

        root = QVBoxLayout(body)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addWidget(self._build_header())
        root.addWidget(self._build_dungeon_bar())
        root.addWidget(self._build_banner())
        root.addWidget(self._build_points())
        root.addWidget(self._build_farm())
        root.addWidget(self._build_nav_macro())
        root.addWidget(self._build_exit())
        root.addWidget(self._build_multi())
        root.addWidget(self._build_run())
        root.addStretch(1)

        self._loading = False
        self._refresh_all()
        self._install_hotkeys()

    # ------------------------------------------------------------------
    def _build_header(self) -> QWidget:
        box = QGroupBox("当前游戏窗口")
        row = QHBoxLayout(box)
        self.lbl_window = QLabel("未绑定")
        self.lbl_window.setWordWrap(True)
        btn = QPushButton("刷新")
        btn.setFixedWidth(80)
        btn.clicked.connect(self._refresh_all)
        row.addWidget(self.lbl_window, 1)
        row.addWidget(btn)
        return box

    # ------------------------------------------------------------------
    # 副本方案（命名保存 / 切换 / 删除）
    # ------------------------------------------------------------------
    def _build_dungeon_bar(self) -> QWidget:
        box = QGroupBox("副本方案（每个副本/每个号可以存一份）")
        row = QHBoxLayout(box)
        row.addWidget(QLabel("当前方案"))
        self.combo_dungeon = QComboBox()
        self.combo_dungeon.setMinimumWidth(200)
        self.combo_dungeon.currentIndexChanged.connect(self._on_dungeon_changed)
        row.addWidget(self.combo_dungeon)

        self.btn_save_dungeon = QPushButton("保存副本")
        self.btn_save_dungeon.setToolTip("把当前这套标定+参数保存成命名方案")
        self.btn_save_dungeon.clicked.connect(self._save_dungeon)
        self.btn_rename_dungeon = QPushButton("重命名")
        self.btn_rename_dungeon.clicked.connect(self._rename_dungeon)
        self.btn_delete_dungeon = QPushButton("删除")
        self.btn_delete_dungeon.clicked.connect(self._delete_dungeon)
        for btn in (self.btn_save_dungeon, self.btn_rename_dungeon,
                    self.btn_delete_dungeon):
            row.addWidget(btn)
        row.addWidget(_hint("切换方案会立刻换成那套点位/区域/时间参数"), 1)
        return box

    def _refresh_dungeon_combo(self) -> None:
        names = df.list_dungeons()
        active = df.active_name()
        self.combo_dungeon.blockSignals(True)
        self.combo_dungeon.clear()
        self.combo_dungeon.addItem("（未命名/当前标定）", "")
        for name in names:
            self.combo_dungeon.addItem(name, name)
        index = self.combo_dungeon.findData(active)
        self.combo_dungeon.setCurrentIndex(index if index >= 0 else 0)
        self.combo_dungeon.blockSignals(False)

    def _on_dungeon_changed(self, _index: int) -> None:
        if self._loading:
            return
        name = self.combo_dungeon.currentData() or ""
        if not name:
            df.set_active("")
            self._log("已切到「未命名/当前标定」（下次保存写回默认文件）")
            return
        data = df.load_dungeon(name)
        if data is None:
            QMessageBox.warning(self, "读取失败", f"方案「{name}」读不出来")
            return
        self.profile = data
        df.set_active(name)
        self._refresh_all()
        self._log(f"已切换到副本方案「{name}」")

    def _save_dungeon(self) -> None:
        name = self.combo_dungeon.currentData() or ""
        if not name:
            name, ok = QInputDialog.getText(
                self, "保存副本",
                "给这个副本方案起个名字（比如：幽暗森林、小号专用）：",
            )
            if not ok or not str(name).strip():
                return
        name = str(name).strip()
        if name in df.list_dungeons():
            ret = QMessageBox.question(self, "覆盖方案",
                                       f"方案「{name}」已存在，要覆盖吗？")
            if ret != QMessageBox.StandardButton.Yes:
                return
        try:
            path = df.save_dungeon(name, self.profile)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self._refresh_dungeon_combo()
        self.combo_dungeon.setCurrentIndex(self.combo_dungeon.findData(name))
        self._log(f"已保存副本方案「{name}」→ {path}")

    def _rename_dungeon(self) -> None:
        current = self.combo_dungeon.currentData() or ""
        if not current:
            QMessageBox.information(self, "重命名",
                                    "先用「保存副本」给当前标定起个名字。")
            return
        name, ok = QInputDialog.getText(self, "重命名方案", "新名字：", text=current)
        if not ok or not str(name).strip():
            return
        name = str(name).strip()
        if name == current:
            return
        data = df.load_dungeon(current)
        if data is None:
            QMessageBox.warning(self, "失败", "读不到原方案")
            return
        try:
            df.save_dungeon(name, data)
            df.delete_dungeon(current)
        except Exception as exc:
            QMessageBox.warning(self, "失败", str(exc))
            return
        self._refresh_dungeon_combo()
        self.combo_dungeon.setCurrentIndex(self.combo_dungeon.findData(name))
        self._log(f"已重命名：「{current}」→「{name}」")

    def _delete_dungeon(self) -> None:
        current = self.combo_dungeon.currentData() or ""
        if not current:
            return
        ret = QMessageBox.question(self, "删除方案",
                                   f"确定删除副本方案「{current}」吗？")
        if ret != QMessageBox.StandardButton.Yes:
            return
        df.delete_dungeon(current)
        self.profile = df.load_profile()
        self._refresh_all()
        self._log(f"已删除方案「{current}」")

    def _build_banner(self) -> QWidget:
        box = QGroupBox("不会配？点这里（推荐）")
        row = QHBoxLayout(box)
        row.addWidget(_hint(
            "向导会按你说的三步问你："
            "① 地图1 点副本入口 → ② 识别进本弹窗点「进入游戏」 → "
            "③ 地图2 点 BOSS 点挂机 → ④ 退出副本/确认。"
            "问完自动把流程和参数拼好。"
        ), 1)
        self.btn_wizard = QPushButton("开始向导")
        self.btn_wizard.setMinimumHeight(38)
        self.btn_wizard.setStyleSheet("font-weight: 700;")
        self.btn_wizard.clicked.connect(self._open_wizard)
        row.addWidget(self.btn_wizard)
        return box

    # ------------------------------------------------------------------
    # 点位（①进本 ②BOSS ③退出）
    # ------------------------------------------------------------------
    def _build_points(self) -> QWidget:
        box = QGroupBox("点位（点一下地图/画面上的位置）")
        g = QGridLayout(box)
        self.lbl_pt = {}
        self.btn_pt = {}
        items = [
            ("farm_pt", "① 打怪点（地图1，挂机用）", "map"),
            ("dungeon_pt", "① 副本入口（地图1）", "map"),
            ("enter_confirm", "① 弹窗「进入游戏」按钮", "shot"),
            ("boss_pt", "② BOSS 点（地图2）", "map"),
            ("exit_pt", "③「退出副本」按钮（第一下）", "shot"),
            ("exit_confirm", "③「确认」按钮（第二下）", "shot"),
            ("fatigue_pt", "④ 疲劳图标（点一下弹疲劳值）", "shot"),
            ("mount_pt", "⑤「骑乘」按钮（未骑乘时点它）", "shot"),
            ("wh_icon", "⑥ 仓库图标（点它打开仓库）", "shot"),
            ("wh_deposit", "⑥「一键放入」按钮", "shot"),
        ]
        for i, (key, title, kind) in enumerate(items):
            btn = QPushButton("设置")
            btn.setFixedWidth(72)
            btn.clicked.connect(lambda _=False, k=key, t=title, kd=kind:
                                self._pick_point(k, t, kd))
            lbl = QLabel("未设置")
            lbl.setObjectName("muted")
            g.addWidget(QLabel(title), i, 0)
            g.addWidget(lbl, i, 1)
            g.addWidget(btn, i, 2)
            self.lbl_pt[key] = lbl
            self.btn_pt[key] = btn

        btn_dialog = QPushButton("框选")
        btn_dialog.setFixedWidth(72)
        btn_dialog.clicked.connect(self._pick_dialog)
        self.lbl_dialog = QLabel("未设置")
        self.lbl_dialog.setObjectName("muted")
        row0 = len(items) + 1
        g.addWidget(QLabel("① 进本弹窗识别区域（区分「进入游戏」弹窗）"), row0, 0)
        g.addWidget(self.lbl_dialog, row0, 1)
        g.addWidget(btn_dialog, row0, 2)

        btn_exit_dialog = QPushButton("框选")
        btn_exit_dialog.setFixedWidth(72)
        btn_exit_dialog.clicked.connect(self._pick_exit_dialog)
        self.lbl_exit_dialog = QLabel("未设置")
        self.lbl_exit_dialog.setObjectName("muted")
        g.addWidget(QLabel("③ 退出确认弹窗识别区域（区分「确认退出」弹窗）"), row0 + 1, 0)
        g.addWidget(self.lbl_exit_dialog, row0 + 1, 1)
        g.addWidget(btn_exit_dialog, row0 + 1, 2)

        btn_mount_dialog = QPushButton("框选")
        btn_mount_dialog.setFixedWidth(72)
        btn_mount_dialog.clicked.connect(lambda: self._pick_mount_roi("dialog"))
        self.lbl_mount_dialog = QLabel("未设置")
        self.lbl_mount_dialog.setObjectName("muted")
        g.addWidget(QLabel("⑤ 骑乘弹窗区域（按 S 弹出来的窗）"), row0 + 2, 0)
        g.addWidget(self.lbl_mount_dialog, row0 + 2, 1)
        g.addWidget(btn_mount_dialog, row0 + 2, 2)

        btn_mount_text = QPushButton("框选")
        btn_mount_text.setFixedWidth(72)
        btn_mount_text.clicked.connect(lambda: self._pick_mount_roi("text"))
        self.lbl_mount_state = QLabel("未设置")
        self.lbl_mount_state.setObjectName("muted")
        g.addWidget(QLabel("⑤ 宠物文本区域（OCR：未骑乘写「骑乘」，已骑乘写「休息」）"),
                    row0 + 3, 0)
        g.addWidget(self.lbl_mount_state, row0 + 3, 1)
        g.addWidget(btn_mount_text, row0 + 3, 2)

        btn_loaded = QPushButton("框选")
        btn_loaded.setFixedWidth(72)
        btn_loaded.clicked.connect(lambda: self._pick_extra_roi("enter.progress"))
        self.lbl_loaded = QLabel("未设置")
        self.lbl_loaded.setObjectName("muted")
        g.addWidget(QLabel("① 加载进度条区域（框住进本时的读条）"),
                    row0 + 5, 0)
        g.addWidget(self.lbl_loaded, row0 + 5, 1)
        g.addWidget(btn_loaded, row0 + 5, 2)

        btn_bossflag = QPushButton("框选")
        btn_bossflag.setFixedWidth(72)
        btn_bossflag.clicked.connect(lambda: self._pick_extra_roi("farm.boss_present"))
        self.lbl_bossflag = QLabel("未设置")
        self.lbl_bossflag.setObjectName("muted")
        g.addWidget(QLabel("② BOSS 身形（先框选，再采 10 张样本）"), row0 + 6, 0)
        g.addWidget(self.lbl_bossflag, row0 + 6, 1)
        btn_boss_samples = QPushButton("采样本")
        btn_boss_samples.setFixedWidth(72)
        btn_boss_samples.setToolTip(
            "站在 BOSS 面前点这个：自动抓 10 张（每 0.3 秒一张），"
            "不同角度/动作都能认出来"
        )
        btn_boss_samples.clicked.connect(self._capture_boss_samples)
        g.addWidget(btn_bossflag, row0 + 6, 2)
        g.addWidget(btn_boss_samples, row0 + 6, 3)

        btn_bosshp = QPushButton("框选")
        btn_bosshp.setFixedWidth(72)
        btn_bosshp.clicked.connect(lambda: self._pick_extra_roi("farm.boss_hp"))
        self.lbl_finish = QLabel("未设置")
        self.lbl_finish.setObjectName("muted")
        g.addWidget(QLabel("③ BOSS 血条区域（血条没了 = 打完）"), row0 + 7, 0)
        g.addWidget(self.lbl_finish, row0 + 7, 1)
        g.addWidget(btn_bosshp, row0 + 7, 2)

        row = QHBoxLayout()
        row.addWidget(QLabel("未骑乘文字"))
        self.edit_mount_text = QLineEdit("骑乘")
        self.edit_mount_text.setFixedWidth(70)
        self.edit_mount_text.editingFinished.connect(self._save_from_ui)
        row.addWidget(self.edit_mount_text)
        row.addWidget(QLabel("已骑乘文字"))
        self.edit_rest_text = QLineEdit("休息")
        self.edit_rest_text.setFixedWidth(70)
        self.edit_rest_text.editingFinished.connect(self._save_from_ui)
        row.addWidget(self.edit_rest_text)
        row.addWidget(_hint("读到「未骑乘文字」才点「骑乘」；读到「已骑乘文字」就不点"), 1)
        g.addLayout(row, row0 + 8, 0, 1, 3)
        return box

    def _build_farm(self) -> QWidget:
        box = QGroupBox("时间参数 / 识别 / 打怪设置（都能改）")
        g = QGridLayout(box)
        g.setHorizontalSpacing(18)
        g.setVerticalSpacing(10)
        g.setColumnMinimumWidth(0, 118)
        g.setColumnMinimumWidth(1, 96)
        g.setColumnMinimumWidth(2, 118)
        g.setColumnMinimumWidth(3, 96)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)

        def pair(row: int, col: int, text: str, attr: str,
                 low: float, high: float, value: float):
            g.addWidget(QLabel(text), row, col)
            spin = self._dspin(low, high, value, None, 0, 0)
            setattr(self, attr, spin)
            g.addWidget(spin, row, col + 1)
            return spin

        # ---------- 时间参数（每行两个） ----------
        pair(0, 0, "进本寻路", "spin_entry_wait", 1.0, 600.0, 40.0)
        pair(0, 2, "等弹窗", "spin_dialog_timeout", 3.0, 300.0, 24.0)
        pair(1, 0, "跑向BOSS", "spin_boss_wait", 1.0, 600.0, 30.0)
        pair(1, 2, "进本加载", "spin_load_wait", 1.0, 300.0, 15.0)
        pair(2, 0, "退出后加载", "spin_exit_settle", 1.0, 300.0, 10.0)
        pair(2, 2, "每轮间隔", "spin_gap", 0.0, 3600.0, 8.0)
        pair(3, 0, "喝药间隔", "spin_potion", 1.0, 60.0, 6.0)
        pair(3, 2, "拾取间隔", "spin_pickup", 0.0, 120.0, 8.0)
        pair(4, 0, "打BOSS上限", "spin_seconds", 5.0, 1000.0, 120.0)
        pair(4, 2, "查疲劳间隔", "spin_trash_seconds", 10.0, 600.0, 60.0)

        g.addWidget(QLabel("没弹窗重试"), 5, 0)
        self.spin_retry = QSpinBox()
        self.spin_retry.setRange(1, 10)
        self.spin_retry.setValue(3)
        self.spin_retry.setSuffix(" 次")
        self.spin_retry.setMinimumWidth(96)
        self.spin_retry.setMaximumWidth(120)
        self.spin_retry.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_retry, 5, 1)


        g.addWidget(QLabel("轮数(0=无限)"), 6, 0)
        self.spin_rounds = QSpinBox()
        self.spin_rounds.setRange(0, 9999)
        self.spin_rounds.setMinimumWidth(96)
        self.spin_rounds.setMaximumWidth(120)
        self.spin_rounds.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_rounds, 6, 1)

        g.addWidget(QLabel("血量阈值"), 6, 2)
        self.spin_hp = QSpinBox()
        self.spin_hp.setRange(0, 9999999)
        self.spin_hp.setValue(0)
        self.spin_hp.setMinimumWidth(96)
        self.spin_hp.setMaximumWidth(120)
        self.spin_hp.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_hp, 6, 3)
        self.btn_hp_roi = QPushButton("框血量区域")
        self.btn_hp_roi.clicked.connect(self._pick_hp)
        g.addWidget(self.btn_hp_roi, 7, 0)
        self.lbl_hp = QLabel("未设置（不框就按固定间隔喝药）")
        self.lbl_hp.setObjectName("muted")
        g.addWidget(self.lbl_hp, 7, 1, 1, 3)

        # ---------- BOSS / 打怪 循环 ----------
        self.chk_link = QCheckBox("打怪点挂机 ↔ 打BOSS 联动（按疲劳切换）")
        self.chk_link.setChecked(True)
        self.chk_link.setToolTip(
            "勾选：先在打怪点挂机，疲劳 < -阈值 就去刷BOSS；"
            "刷到疲劳 > +阈值 就回打怪点挂机。"
        )
        self.chk_link.toggled.connect(self._save_from_ui)
        g.addWidget(self.chk_link, 10, 0, 1, 4)

        row_wh = QHBoxLayout()
        self.chk_warehouse = QCheckBox("打完 BOSS 一键放入仓库")
        self.chk_warehouse.setChecked(True)
        self.chk_warehouse.toggled.connect(self._save_from_ui)
        row_wh.addWidget(self.chk_warehouse)
        row_wh.addWidget(QLabel("ESC 次数"))
        self.spin_esc_times = QSpinBox()
        self.spin_esc_times.setRange(0, 5)
        self.spin_esc_times.setValue(2)
        self.spin_esc_times.setFixedWidth(60)
        self.spin_esc_times.valueChanged.connect(self._save_from_ui)
        row_wh.addWidget(self.spin_esc_times)
        row_wh.addWidget(QLabel("每步缓冲"))
        self.spin_wh_gap = self._dspin(0.0, 5.0, 0.5, None, 0, 0)
        row_wh.addWidget(self.spin_wh_gap)
        row_wh.addStretch(1)
        g.addLayout(row_wh, 11, 0, 1, 4)

        self.chk_allow_grab = QCheckBox(
            "允许抢占鼠标键盘（我在电脑前时勾选；不勾=纯后台，绝不动光标/不切前台）"
        )
        self.chk_allow_grab.setChecked(False)
        self.chk_allow_grab.setToolTip(
            "不勾：只用 PostMessage 发消息，鼠标/前台完全不动（人不在时用这个）。\n"
            "勾选：需要点击时才借鼠标/切前台，点完立刻还原（人在电脑前时用）。"
        )
        self.chk_allow_grab.toggled.connect(self._save_from_ui)
        g.addWidget(self.chk_allow_grab, 12, 0, 1, 4)

        row_mode = QHBoxLayout()
        row_mode.addWidget(QLabel("输入模式"))
        self.combo_input_mode = QComboBox()
        self.combo_input_mode.addItem("混合（推荐）：点击时借鼠标，键盘后台", "hybrid")
        self.combo_input_mode.addItem("前台：整个流程占用鼠标键盘", "foreground")
        self.combo_input_mode.addItem("纯后台：只发消息（部分游戏不认）", "background")
        self.combo_input_mode.setMinimumWidth(300)
        self.combo_input_mode.setToolTip(
            "混合模式：需要点击时临时把游戏切前台、借光标点一下，点完立刻还原；"
            "按键全部走后台 PostMessage。"
        )
        self.combo_input_mode.currentIndexChanged.connect(self._save_from_ui)
        row_mode.addWidget(self.combo_input_mode)
        row_mode.addStretch(1)
        g.addLayout(row_mode, 13, 0, 1, 4)

        row_sim = QHBoxLayout()
        row_sim.addWidget(QLabel("识别相似度"))
        self.spin_sim = self._dspin(0.30, 0.99, 0.80, None, 0, 0)
        self.spin_sim.setSingleStep(0.05)
        row_sim.addWidget(self.spin_sim)
        row_sim.addWidget(QLabel("宽松阈值"))
        self.spin_sim_loose = self._dspin(0.20, 0.95, 0.60, None, 0, 0)
        self.spin_sim_loose.setSingleStep(0.05)
        row_sim.addWidget(self.spin_sim_loose)
        row_sim.addStretch(1)
        g.addLayout(row_sim, 14, 0, 1, 4)

        row_gap = QHBoxLayout()
        self.chk_pickup_on_a = QCheckBox("每次平A后都拾取")
        self.chk_pickup_on_a.setChecked(True)
        self.chk_pickup_on_a.toggled.connect(self._save_from_ui)
        row_gap.addWidget(self.chk_pickup_on_a)
        row_gap.addWidget(QLabel("动作缓冲"))
        self.spin_action_gap = self._dspin(0.0, 10.0, 1.0, None, 0, 0)
        row_gap.addWidget(self.spin_action_gap)
        row_gap.addWidget(QLabel("骑乘键"))
        self.edit_mount_key = QLineEdit()
        self.edit_mount_key.setFixedWidth(44)
        self.edit_mount_key.editingFinished.connect(self._save_from_ui)
        row_gap.addWidget(self.edit_mount_key)
        self.chk_mount = QCheckBox("寻路前按 S 查骑乘")
        self.chk_mount.setChecked(True)
        self.chk_mount.toggled.connect(self._save_from_ui)
        row_gap.addWidget(self.chk_mount)
        row_gap.addStretch(1)
        g.addLayout(row_gap, 15, 0, 1, 4)

        row_keys = QHBoxLayout()
        row_keys.addWidget(QLabel("开图键"))
        self.edit_map_key = QLineEdit()
        self.edit_map_key.setFixedWidth(44)
        self.edit_map_key.editingFinished.connect(self._save_from_ui)
        row_keys.addWidget(self.edit_map_key)
        row_keys.addWidget(QLabel("拾取键"))
        self.edit_pickup_key = QLineEdit()
        self.edit_pickup_key.setFixedWidth(44)
        self.edit_pickup_key.editingFinished.connect(self._save_from_ui)
        row_keys.addWidget(self.edit_pickup_key)
        row_keys.addWidget(QLabel("喝药键"))
        self.edit_potion_key = QLineEdit()
        self.edit_potion_key.setFixedWidth(44)
        self.edit_potion_key.editingFinished.connect(self._save_from_ui)
        row_keys.addWidget(self.edit_potion_key)
        self.chk_skills = QCheckBox("放技能 1/2")
        self.chk_skills.toggled.connect(self._save_from_ui)
        row_keys.addWidget(self.chk_skills)
        row_keys.addStretch(1)
        g.addLayout(row_keys, 16, 0, 1, 4)

        return box

    def _dspin(self, low: float, high: float, value: float,
               grid, row: int, col: int) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setSuffix(" 秒")
        spin.setValue(value)
        spin.setMinimumWidth(96)
        spin.setMaximumWidth(120)
        spin.valueChanged.connect(self._save_from_ui)
        if grid is not None:
            grid.addWidget(spin, row, col)
        return spin

    def _build_nav_macro(self) -> QWidget:
        box = QGroupBox("引路宏（鼠标宏：地图点识别不准时，用录制的点击/按键引路）")
        g = QGridLayout(box)
        self.chk_nav_macro = QCheckBox(
            "用引路宏导航（替代「地图1 副本入口 + 地图2 BOSS 点」这两步）"
        )
        self.chk_nav_macro.setToolTip(
            "录制一段从打怪点 → 开图 → 点副本入口 → 进入游戏 → 跑到 BOSS 的操作；\n"
            "运行到 BOSS 流程时直接回放这段鼠标宏，不再依赖地图点位。\n"
            "录的是左键点击和按键（鼠标移动/拖拽不录）。"
        )
        self.chk_nav_macro.toggled.connect(self._save_from_ui)
        g.addWidget(self.chk_nav_macro, 0, 0, 1, 4)

        self.chk_nav_fallback = QCheckBox("宏跑完没到 BOSS 时，回退到地图点位")
        self.chk_nav_fallback.setChecked(False)
        self.chk_nav_fallback.toggled.connect(self._save_from_ui)
        g.addWidget(self.chk_nav_fallback, 0, 4, 1, 2)

        row = QHBoxLayout()
        self.btn_nav_record = QPushButton("录制引路宏（3 秒后开始）")
        self.btn_nav_record.clicked.connect(self._record_nav_macro)
        self.btn_nav_stop = QPushButton("停止并保存")
        self.btn_nav_stop.setEnabled(False)
        self.btn_nav_stop.clicked.connect(self._stop_nav_record)
        self.btn_nav_clear = QPushButton("清空")
        self.btn_nav_clear.clicked.connect(self._clear_nav_macro)
        self.btn_test_a = QPushButton("测试后台平A")
        self.btn_test_a.setToolTip("给当前绑定窗口发一次后台 A，验证游戏认不认 PostMessage")
        self.btn_test_a.clicked.connect(self._test_background_a)
        self.lbl_nav_macro = QLabel("未录制")
        self.lbl_nav_macro.setObjectName("muted")
        row.addWidget(self.btn_nav_record)
        row.addWidget(self.btn_nav_stop)
        row.addWidget(self.btn_nav_clear)
        row.addWidget(self.btn_test_a)
        row.addWidget(self.lbl_nav_macro, 1)
        g.addLayout(row, 1, 0, 1, 6)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("回放速度"))
        self.spin_nav_speed = QDoubleSpinBox()
        self.spin_nav_speed.setRange(0.25, 3.0)
        self.spin_nav_speed.setSingleStep(0.25)
        self.spin_nav_speed.setValue(1.0)
        self.spin_nav_speed.setFixedWidth(90)
        self.spin_nav_speed.valueChanged.connect(self._save_from_ui)
        row2.addWidget(self.spin_nav_speed)
        row2.addWidget(_hint(
            "先在打怪点站好 → 点「录制引路宏」→ 3 秒内切到游戏，正常开图/进本/跑到 BOSS "
            "→ 切回本程序点「停止并保存」。只录左键点击和按键。"
        ), 1)
        g.addLayout(row2, 2, 0, 1, 6)
        return box

    def _test_background_a(self) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有绑定窗口", "请先绑定游戏窗口。")
            return
        self._save_from_ui()
        try:
            game = df.WinGame(int(hwnd), self.profile, log=self._log)
            target = game.message_target()
            ok = game.tap("a")
        except Exception as exc:
            self._log(f"测试后台平A失败：{exc}")
            return
        self._log(
            f"测试后台平A → {'已发送' if ok else '发送失败'}"
            f"（目标窗口 0x{int(hwnd):X}，焦点子窗口 0x{int(target):X}）"
        )
        if not ok:
            self._log("如果游戏没反应：用管理员运行；窗口别最小化；"
                      "或者勾「允许抢占鼠标键盘」让点击走真实鼠标")

    def _record_nav_macro(self) -> None:
        if self._nav_recorder is not None or self._nav_pending:
            self._log("引路宏已经在录制/等待开始")
            return
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有绑定窗口", "请先绑定游戏窗口，再录引路宏。")
            return
        self._save_from_ui()
        self._nav_pending = True
        self.btn_nav_record.setEnabled(False)
        self.btn_nav_stop.setEnabled(True)
        self.lbl_nav_macro.setText("3 秒后开始录制…请切到游戏")
        self._log("引路宏：3 秒后开始录制，切到游戏正常操作；完成后切回来点「停止并保存」")
        QTimer.singleShot(3000, self._start_nav_record)

    def _start_nav_record(self) -> None:
        if not self._nav_pending:
            return
        self._nav_pending = False
        hwnd = self._hwnd()
        if not hwnd:
            self._reset_nav_record_buttons()
            return
        try:
            recorder = app.MacroRecorder(int(hwnd))
            recorder.start()
        except Exception as exc:
            self._log(f"引路宏录制启动失败：{exc}")
            self._reset_nav_record_buttons()
            return
        self._nav_recorder = recorder
        self.lbl_nav_macro.setText("录制中…操作完切回来点「停止并保存」")
        self._log("引路宏：开始录制（只录游戏窗口内的左键点击和按键）")

    def _reset_nav_record_buttons(self) -> None:
        self.btn_nav_record.setEnabled(True)
        self.btn_nav_stop.setEnabled(False)

    def _stop_nav_record(self) -> None:
        if self._nav_pending and self._nav_recorder is None:
            self._nav_pending = False
            self._reset_nav_record_buttons()
            self._update_nav_macro_label()
            self._log("引路宏：已取消")
            return
        recorder = self._nav_recorder
        if recorder is None:
            self._reset_nav_record_buttons()
            return
        self._nav_recorder = None
        try:
            events = recorder.stop()
            meta = recorder.meta()
        except Exception as exc:
            self._log(f"引路宏停止失败：{exc}")
            self._reset_nav_record_buttons()
            return
        kept = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            typ = str(ev.get("type", ""))
            if typ == "down" and str(ev.get("button", "left")) == "left":
                nx, ny = ev.get("nx"), ev.get("ny")
                if nx is None or ny is None:
                    continue
                kept.append({
                    "type": "down", "t": float(ev.get("t", 0.0)),
                    "nx": float(nx), "ny": float(ny),
                })
            elif typ == "key_down":
                name = str(ev.get("name", "")).strip()
                low = name.lower()
                # F1~F12 是热键，不写进引路宏，避免回放时触发别的功能
                if low.startswith("f") and low[1:].isdigit():
                    continue
                if name:
                    kept.append({
                        "type": "key_down", "t": float(ev.get("t", 0.0)),
                        "name": name,
                    })
        nav = self.profile.setdefault("nav_macro", {})
        nav["events"] = kept
        nav["meta"] = meta if isinstance(meta, dict) else {}
        self._save()
        self._reset_nav_record_buttons()
        self._update_nav_macro_label()
        if kept:
            self._log(f"引路宏：已保存 {len(kept)} 个动作")
        else:
            self._log("引路宏：没录到点击/按键（要游戏在前台、鼠标在游戏窗口内）")

    def _clear_nav_macro(self) -> None:
        nav = self.profile.setdefault("nav_macro", {})
        nav["events"] = []
        nav["meta"] = {}
        self._save()
        self._update_nav_macro_label()
        self._log("引路宏：已清空")

    def _update_nav_macro_label(self) -> None:
        nav = self.profile.get("nav_macro") or {}
        events = nav.get("events") or []
        if not events:
            self.lbl_nav_macro.setText("未录制")
            return
        clicks = sum(1 for e in events if e.get("type") == "down")
        keys = sum(1 for e in events if e.get("type") == "key_down")
        self.lbl_nav_macro.setText(f"已录制：{clicks} 次点击 / {keys} 个按键")

    def _build_exit(self) -> QWidget:
        box = QGroupBox("④ 疲劳联动：按上下阈值自动切换打怪 / 打BOSS")
        g = QGridLayout(box)
        self.chk_fatigue = QCheckBox("启用疲劳检测（每 1 分钟查一次，按上下阈值切换）")
        self.chk_fatigue.setChecked(True)
        self.chk_fatigue.toggled.connect(self._save_from_ui)
        g.addWidget(self.chk_fatigue, 0, 0, 1, 3)

        g.addWidget(QLabel("疲劳阈值 ±"), 0, 3)
        self.spin_fatigue = QSpinBox()
        self.spin_fatigue.setRange(0, 999999)
        self.spin_fatigue.setValue(600)
        self.spin_fatigue.setFixedWidth(100)
        self.spin_fatigue.setToolTip(
            "打小怪时：疲劳 < -阈值 → 去打BOSS；"
            "打BOSS时：疲劳 > +阈值 → 回打怪点打小怪。"
        )
        self.spin_fatigue.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_fatigue, 0, 4)

        self.btn_fatigue_roi = QPushButton("框疲劳提示区域（可选）")
        self.btn_fatigue_roi.clicked.connect(self._pick_fatigue_roi)
        g.addWidget(self.btn_fatigue_roi, 1, 0)
        self.lbl_fatigue_roi = QLabel("未设置（会沿用主界面「疲劳检测」框过的区域）")
        g.addWidget(self.lbl_fatigue_roi, 1, 1, 1, 6)
        g.addWidget(_hint(
            "不需要设挂机时长：每 60 秒查一次疲劳。"
            "打小怪时疲劳 < -阈值 就去打BOSS；打BOSS时疲劳 > +阈值 就回打怪点。"
        ), 2, 0, 1, 8)
        return box

    # ------------------------------------------------------------------
    # 多开
    # ------------------------------------------------------------------
    def _build_multi(self) -> QWidget:
        box = QGroupBox("多开（每行一个窗口；F8 全部开始 / F9 全部停止）")
        g = QGridLayout(box)
        self.multi_combos: list[QComboBox] = []
        self.multi_seconds: list[QDoubleSpinBox] = []   # 已废弃：挂机不再设时长
        self.multi_state: list[QLabel] = []
        self.multi_names: list[QLabel] = []
        self.multi_buttons: list[QPushButton] = []
        self._name_readers: dict[int, object] = {}
        for i in range(4):
            combo = QComboBox()
            combo.setMinimumWidth(240)
            state = QLabel("未运行")
            state.setObjectName("muted")
            btn = QPushButton("开始")
            btn.setFixedWidth(72)
            btn.clicked.connect(lambda _=False, idx=i: self._toggle_slot(idx))
            name_lbl = QLabel("角色: --")
            name_lbl.setObjectName("muted")
            name_lbl.setMinimumWidth(140)
            combo.currentIndexChanged.connect(
                lambda _=0, idx=i: self._refresh_slot_name(idx)
            )
            g.addWidget(QLabel(f"窗口 {i + 1}"), i, 0)
            g.addWidget(combo, i, 1)
            g.addWidget(name_lbl, i, 2)
            g.addWidget(btn, i, 3)
            g.addWidget(state, i, 4, 1, 4)
            self.multi_combos.append(combo)
            self.multi_state.append(state)
            self.multi_names.append(name_lbl)
            self.multi_buttons.append(btn)
        row = QHBoxLayout()
        btn_refresh = QPushButton("刷新窗口列表")
        btn_refresh.clicked.connect(self._refresh_windows)
        btn_names = QPushButton("读取名字")
        btn_names.setToolTip("用主界面「框选角色名区域」那块区域 OCR 读角色名")
        btn_names.clicked.connect(self._read_all_names)
        btn_all = QPushButton("全部开始")
        btn_all.clicked.connect(lambda: [self._toggle_slot(i) for i in range(4)
                                        if self.multi_buttons[i].text() == "开始"])
        btn_stop = QPushButton("全部停止")
        btn_stop.clicked.connect(self._stop_all)
        row.addWidget(btn_refresh)
        row.addWidget(btn_names)
        row.addWidget(btn_all)
        row.addWidget(btn_stop)
        row.addStretch(1)
        g.addLayout(row, 4, 0, 1, 8)
        return box

    def _refresh_slot_name(self, index: int) -> None:
        """刷新某一行的角色名（用主程序的 NameReader + 角色名区域）。"""
        if not (0 <= index < len(self.multi_names)):
            return
        label = self.multi_names[index]
        hwnd = self.multi_combos[index].currentData()
        if not hwnd:
            label.setText("角色: --")
            return
        roi = getattr(self.host, "_name_roi_norm", None)
        if not roi:
            title = self.multi_combos[index].currentText()
            label.setText(f"角色: {title.split(' [0x')[0][:12]}")
            label.setToolTip("主界面还没框选「角色名区域」，先用窗口标题显示")
            return
        if win32gui and win32gui.IsIconic(int(hwnd)):
            label.setText("角色: 窗口最小化")
            return
        old = self._name_readers.get(index)
        if old is not None and getattr(old, "isRunning", lambda: False)():
            try:
                old.stop()
            except Exception:
                pass
        label.setText("角色: 读取中…")
        try:
            reader = app.NameReader(int(hwnd), roi, parent=self)
        except Exception as exc:
            label.setText("角色: 读取失败")
            self._log(f"读角色名失败：{exc}")
            return
        self._name_readers[index] = reader

        def on_result(name: str, message: str, idx: int = index) -> None:
            if not (0 <= idx < len(self.multi_names)):
                return
            text = (name or "").strip()
            if text:
                self.multi_names[idx].setText(f"角色: {text}")
                self.multi_names[idx].setStyleSheet(
                    "color: #15803d; font-weight: 600;"
                )
            else:
                self.multi_names[idx].setText("角色: 识别失败")
                self.multi_names[idx].setToolTip(str(message))

        reader.result.connect(on_result)
        reader.start()

    def _read_all_names(self) -> None:
        for i in range(len(self.multi_combos)):
            self._refresh_slot_name(i)

    def _build_run(self) -> QWidget:
        box = QGroupBox("运行（单窗口）")
        lay = QVBoxLayout(box)
        row = QHBoxLayout()
        self.btn_probe = QPushButton("看一眼当前判定")
        self.btn_probe.setToolTip("抓一帧，看弹窗是否识别到、血量读数（不发按键）")
        self.btn_probe.clicked.connect(self._probe)
        self.btn_dry = QPushButton("试跑一轮（不按键）")
        self.btn_dry.clicked.connect(lambda: self._start_single(dry=True))
        self.btn_start = QPushButton("▶ 开始运行")
        self.btn_start.setObjectName("primary")
        self.btn_start.setMinimumHeight(38)
        self.btn_start.clicked.connect(lambda: self._start_single(dry=False))
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_single)
        row.addWidget(self.btn_probe)
        row.addWidget(self.btn_dry)
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        self.lbl_state = QLabel("未运行")
        self.lbl_state.setObjectName("muted")
        row.addWidget(self.lbl_state, 1)
        lay.addLayout(row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(170)
        self.log.setPlaceholderText("运行日志")
        lay.addWidget(self.log)
        return box

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------
    def _hwnd(self) -> int:
        hwnd = int(getattr(self.host, "target_hwnd", 0) or 0)
        if hwnd and win32gui and win32gui.IsWindow(hwnd):
            return hwnd
        return 0

    def _log(self, text: str) -> None:
        self.log.appendPlainText(f"[{datetime.datetime.now():%H:%M:%S}] {text}")

    def _save(self) -> None:
        """有命名的副本方案就存进那个方案（这样“默认就是那个副本”），
        否则写默认标定文件。"""
        try:
            name = df.active_name()
            if name:
                df.save_dungeon(name, self.profile)
            else:
                df.save_profile(self.profile)
        except Exception as exc:
            self._log(f"[警告] 保存失败：{exc}")

    def _refresh_all(self) -> None:
        self._refresh_dungeon_combo()
        hwnd = self._hwnd()
        self.lbl_window.setText(
            f"已绑定：0x{hwnd:08X}  {win32gui.GetWindowText(hwnd)}"
            if hwnd else "未绑定（请到「单开挂机」页绑定）"
        )
        self._refresh_windows()
        self._load_ui()

    def _load_ui(self) -> None:
        self._loading = True
        try:
            p = self.profile
            enter = p.get("enter") or {}
            farm = p.get("farm") or {}
            exit_cfg = p.get("exit") or {}
            keys = p.get("keys") or {}
            loop = p.get("loop") or {}

            self.spin_entry_wait.setValue(float(enter.get("travel_wait", 40.0)))
            self.spin_dialog_timeout.setValue(float(enter.get("dialog_timeout", 20.0)))
            self.spin_retry.setValue(int(enter.get("retry", 3) or 3))
            self.spin_load_wait.setValue(float(enter.get("load_wait", 10.0)))
            self.spin_boss_wait.setValue(float(farm.get("travel_wait", 30.0)))
            self.spin_seconds.setValue(
                float(loop.get("boss_seconds", farm.get("seconds", 120.0)) or 120.0)
            )
            self.spin_hp.setValue(int(farm.get("hp_threshold", 0) or 0))
            self.spin_potion.setValue(float(farm.get("potion_every", 6.0)))
            self.spin_pickup.setValue(float(farm.get("pickup_every", 8.0)))
            self.chk_skills.setChecked(bool(farm.get("use_skills", True)))
            mount = p.get("mount") or {}
            loop = p.get("loop") or {}
            self.chk_mount.setChecked(bool(mount.get("enabled", True)))
            self.edit_mount_key.setText(str(mount.get("key", "s")))

            self.spin_trash_seconds.setValue(
                float(loop.get("fatigue_every", 60.0))
            )
            self.chk_link.setChecked(bool(loop.get("link_fatigue", True)))
            detect = p.get("detect") or {}
            self.spin_sim.setValue(float(detect.get("sim", 0.80)))
            self.spin_sim_loose.setValue(float(detect.get("sim_loose", 0.60)))
            self.chk_pickup_on_a.setChecked(bool(farm.get("pickup_on_a", True)))
            self.spin_action_gap.setValue(float(p.get("action_gap", 1.0)))
            self.spin_rounds.setValue(int((p.get("retry") or {}).get("rounds", 0) or 0))
            self.edit_mount_text.setText(str(mount.get("mount_text", "骑乘")))
            self.edit_rest_text.setText(str(mount.get("rest_text", "休息")))
            for name, spec, label in (
                ("骑乘弹窗", mount.get("dialog") or {}, self.lbl_mount_dialog),
                ("宠物文本", {"roi": mount.get("text_roi")}, self.lbl_mount_state),
            ):
                if (spec or {}).get("roi"):
                    label.setText("已设置 ✓")
                    label.setStyleSheet("color: #15803d; font-weight: 600;")
                else:
                    label.setText("未设置")
                    label.setStyleSheet("color: #444444;")
            self.chk_allow_grab.setChecked(bool(p.get("allow_grab", False)))
            nav = p.get("nav_macro") or {}
            self.chk_nav_macro.setChecked(bool(nav.get("enabled", False)))
            self.chk_nav_fallback.setChecked(bool(nav.get("fallback", False)))
            try:
                self.spin_nav_speed.setValue(float(nav.get("speed", 1.0) or 1.0))
            except (TypeError, ValueError):
                self.spin_nav_speed.setValue(1.0)
            self._update_nav_macro_label()
            _mode = str(p.get("input_mode", "hybrid"))
            _idx = self.combo_input_mode.findData(_mode)
            self.combo_input_mode.setCurrentIndex(_idx if _idx >= 0 else 0)
            self.edit_map_key.setText(str(keys.get("open_map", "m")))
            self.edit_pickup_key.setText(str(keys.get("pickup", "space")))
            self.edit_potion_key.setText(
                str((farm.get("potion_keys") or ["q"])[0])
            )
            wh = p.get("warehouse") or {}
            self.chk_warehouse.setChecked(bool(wh.get("enabled", True)))
            self.spin_esc_times.setValue(int(wh.get("esc_times", 2) or 2))
            self.spin_wh_gap.setValue(float(wh.get("gap", 0.5)))
            self.spin_exit_settle.setValue(float(exit_cfg.get("settle", 10.0)))
            self.spin_gap.setValue(float((p.get("retry") or {}).get("gap", 8.0)))
            fatigue = p.get("fatigue") or {}
            self.chk_fatigue.setChecked(bool(fatigue.get("enabled", True)))
            self.spin_fatigue.setValue(int(fatigue.get("threshold", 800) or 0))

            states = {
                "dungeon_pt": (enter.get("dungeon_pt"), "① 副本入口"),
                "enter_confirm": (enter.get("confirm_pt"), "① 进入游戏"),
                "boss_pt": (farm.get("boss_pt"), "② BOSS 点"),
                "exit_pt": (exit_cfg.get("exit_pt"), "③ 退出副本"),
                "exit_confirm": (exit_cfg.get("confirm_pt"), "③ 确认"),
                "fatigue_pt": ((p.get("fatigue") or {}).get("pt"), "④ 疲劳图标"),
                "mount_pt": ((p.get("mount") or {}).get("mount_pt"), "⑤ 骑乘按钮"),
                "farm_pt": (farm.get("farm_pt"), "① 打怪点"),
                "wh_icon": ((p.get("warehouse") or {}).get("icon_pt"), "⑥ 仓库图标"),
                "wh_deposit": ((p.get("warehouse") or {}).get("deposit_pt"),
                               "⑥ 一键放入"),
            }
            for key, (pt, _name) in states.items():
                lbl = self.lbl_pt[key]
                if pt:
                    lbl.setText(f"已设置 ({pt[0]:.3f}, {pt[1]:.3f})")
                    lbl.setStyleSheet("color: #15803d; font-weight: 600;")
                else:
                    lbl.setText("未设置")
                    lbl.setStyleSheet("color: #444444;")
            for name, spec, label in (
                ("进本", enter.get("dialog") or {}, self.lbl_dialog),
                ("退出", exit_cfg.get("dialog") or {}, self.lbl_exit_dialog),
            ):
                if (spec or {}).get("roi"):
                    label.setText("已设置 ✓")
                    label.setStyleSheet("color: #15803d; font-weight: 600;")
                else:
                    label.setText("未设置")
                    label.setStyleSheet("color: #444444;")
            for spec, label in (
                (enter.get("progress") or {}, self.lbl_loaded),
                (farm.get("boss_present") or {}, self.lbl_bossflag),
                (farm.get("boss_hp") or {}, self.lbl_finish),
            ):
                if (spec or {}).get("roi"):
                    extra = ""
                    if isinstance(spec, dict) and "sigs" in spec:
                        extra = f"，样本 {len([t for t in (spec.get('sigs') or []) if t])} 张"
                    label.setText(f"已设置 ✓{extra}")
                    label.setStyleSheet("color: #15803d; font-weight: 600;")
                else:
                    label.setText("未设置（可选）")
                    label.setStyleSheet("color: #444444;")
            hp_roi = farm.get("hp_roi")
            if hp_roi:
                self.lbl_hp.setText("已设置 ✓")
                self.lbl_hp.setStyleSheet("color: #15803d; font-weight: 600;")
            else:
                self.lbl_hp.setText("未设置（不框就按固定间隔喝药）")
                self.lbl_hp.setStyleSheet("color: #444444;")
            if (p.get("fatigue") or {}).get("roi"):
                self.lbl_fatigue_roi.setText("已设置 ✓")
                self.lbl_fatigue_roi.setStyleSheet("color: #15803d; font-weight: 600;")
            else:
                self.lbl_fatigue_roi.setText(
                    "未设置（会沿用主界面「疲劳检测」框过的区域）"
                )
                self.lbl_fatigue_roi.setStyleSheet("color: #444444;")
        finally:
            self._loading = False

    def _save_from_ui(self) -> None:
        if self._loading:
            return
        p = self.profile
        p.setdefault("farm", {}).update({
            "seconds": float(self.spin_seconds.value()),
            "hp_threshold": int(self.spin_hp.value()),
            "potion_every": float(self.spin_potion.value()),
            "pickup_every": float(self.spin_pickup.value()),
            "use_skills": bool(self.chk_skills.isChecked()),
        })
        p.setdefault("keys", {}).update({
            "open_map": self.edit_map_key.text().strip() or "m",
            "pickup": self.edit_pickup_key.text().strip() or "space",
        })
        p.setdefault("enter", {}).update({
            "travel_wait": float(self.spin_entry_wait.value()),
            "dialog_timeout": float(self.spin_dialog_timeout.value()),
            "retry": int(self.spin_retry.value()),
            "load_wait": float(self.spin_load_wait.value()),
        })
        p.setdefault("farm", {})["travel_wait"] = float(self.spin_boss_wait.value())
        p.setdefault("exit", {})["settle"] = float(self.spin_exit_settle.value())
        p.setdefault("farm", {})["potion_keys"] = [
            self.edit_potion_key.text().strip() or "q"
        ]
        p.setdefault("warehouse", {}).update({
            "enabled": bool(self.chk_warehouse.isChecked()),
            "esc_times": int(self.spin_esc_times.value()),
            "gap": float(self.spin_wh_gap.value()),
        })
        p.setdefault("fatigue", {}).update({
            "enabled": bool(self.chk_fatigue.isChecked()),
            "threshold": int(self.spin_fatigue.value()),
        })
        p.setdefault("mount", {}).update({
            "enabled": bool(self.chk_mount.isChecked()),
            "key": self.edit_mount_key.text().strip() or "s",
            "mount_text": self.edit_mount_text.text().strip() or "骑乘",
            "rest_text": self.edit_rest_text.text().strip() or "休息",
        })
        p.setdefault("loop", {}).update({
            "boss_seconds": float(self.spin_seconds.value()),
            "fatigue_every": float(self.spin_trash_seconds.value()),
            "link_fatigue": bool(self.chk_link.isChecked()),
            "pickup_on_a": bool(self.chk_pickup_on_a.isChecked()),
        })
        p["action_gap"] = float(self.spin_action_gap.value())
        p.setdefault("detect", {}).update({
            "sim": float(self.spin_sim.value()),
            "sim_loose": float(self.spin_sim_loose.value()),
        })
        p.setdefault("retry", {})["rounds"] = int(self.spin_rounds.value())
        p["allow_grab"] = bool(self.chk_allow_grab.isChecked())
        p["input_mode"] = self.combo_input_mode.currentData() or "hybrid"
        nav = p.setdefault("nav_macro", {})
        nav["enabled"] = bool(self.chk_nav_macro.isChecked())
        nav["fallback"] = bool(self.chk_nav_fallback.isChecked())
        nav["speed"] = float(self.spin_nav_speed.value())
        p.setdefault("retry", {})["gap"] = float(self.spin_gap.value())
        self._save()

    def _refresh_windows(self) -> None:
        """刷新窗口列表，并把窗口按顺序自动排到 4 行里（不重复）。"""
        wins = df.list_windows("CreatureCurios")
        # 稳定排序：先按标题，再按句柄
        wins = sorted(wins, key=lambda item: (item[1], item[0]))
        # 当前绑定的窗口排第一，其余按顺序
        bound = self._hwnd()
        if bound:
            wins.sort(key=lambda item: 0 if item[0] == bound else 1)

        # 每行都已经塞进同样的候选列表
        for combo in self.multi_combos:
            combo.blockSignals(True)
            combo.clear()
            for hwnd, title in wins:
                combo.addItem(f"{title[:34]} [0x{hwnd:X}]", hwnd)
            combo.blockSignals(False)

        # 自动排队：第 1 行放第 1 个窗口，第 2 行放第 2 个……互不重复
        for i, combo in enumerate(self.multi_combos):
            if i in self.threads:
                continue                      # 这行正在跑，别动它
            combo.blockSignals(True)
            if i < len(wins):
                idx = combo.findData(wins[i][0])
                combo.setCurrentIndex(idx if idx >= 0 else -1)
            else:
                combo.setCurrentIndex(-1)
            combo.blockSignals(False)

        self._log(f"窗口列表已刷新：找到 {len(wins)} 个窗口，已按顺序排到 "
                  f"{min(len(wins), len(self.multi_combos))} 行")
        for i in range(len(self.multi_combos)):
            self._refresh_slot_name(i)

    # ------------------------------------------------------------------
    # 标定动作
    # ------------------------------------------------------------------
    def _pick_point(self, key: str, title: str, kind: str) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有绑定窗口", "请先绑定游戏窗口。")
            return
        map_key = self.edit_map_key.text().strip() or "m"
        open_key = map_key if kind == "map" else None
        info = {
            "dungeon_pt": "先打开地图1（城镇/世界地图），点一下副本入口图标的位置。",
            "enter_confirm": "把进本弹窗显示出来，然后在截图上点「进入游戏/确定」按钮。",
            "boss_pt": "打开地图2（副本地图），点一下 BOSS 所在的位置。",
            "exit_pt": "在副本里打开退出菜单，点「退出副本」按钮。",
            "exit_confirm": "把确认弹窗显示出来，点「确认/确定」按钮（第二下）。",
            "fatigue_pt": "点一下游戏里那个能弹出疲劳值的图标/按钮位置。",
            "mount_pt": "按 S 打开骑乘窗口，点一下里面的「骑乘」按钮。",
            "farm_pt": "打开地图1，点一下「打怪点」位置（疲劳不够时就在这挂机）。",
            "wh_icon": "点一下打开仓库的那个图标/按钮（先打开仓库界面再截图）。",
            "wh_deposit": "在仓库界面里点一下「一键放入」按钮。",
        }.get(key, "")
        if kind == "map":
            info += chr(10) + f"（点「重新截图」会先按一次开图键「{map_key}」）"
        current = None
        p = self.profile
        if key == "dungeon_pt":
            current = (p.get("enter") or {}).get("dungeon_pt")
        elif key == "enter_confirm":
            current = (p.get("enter") or {}).get("confirm_pt")
        elif key == "boss_pt":
            current = (p.get("farm") or {}).get("boss_pt")
        elif key == "exit_pt":
            current = (p.get("exit") or {}).get("exit_pt")
        elif key == "exit_confirm":
            current = (p.get("exit") or {}).get("confirm_pt")
        elif key == "fatigue_pt":
            current = (p.get("fatigue") or {}).get("pt")
        elif key == "farm_pt":
            current = (p.get("farm") or {}).get("farm_pt")
        elif key == "wh_icon":
            current = (p.get("warehouse") or {}).get("icon_pt")
        elif key == "wh_deposit":
            current = (p.get("warehouse") or {}).get("deposit_pt")
        elif key == "mount_pt":
            current = (p.get("mount") or {}).get("mount_pt")

        pt = pick_point(self, hwnd, title, info, open_key, current)
        if not pt:
            return
        if key == "dungeon_pt":
            p.setdefault("enter", {})["dungeon_pt"] = pt
        elif key == "enter_confirm":
            p.setdefault("enter", {})["confirm_pt"] = pt
        elif key == "boss_pt":
            p.setdefault("farm", {})["boss_pt"] = pt
        elif key == "exit_pt":
            p.setdefault("exit", {})["exit_pt"] = pt
        elif key == "exit_confirm":
            p.setdefault("exit", {})["confirm_pt"] = pt
        elif key == "fatigue_pt":
            p.setdefault("fatigue", {})["pt"] = pt
        elif key == "farm_pt":
            p.setdefault("farm", {})["farm_pt"] = pt
        elif key == "wh_icon":
            p.setdefault("warehouse", {})["icon_pt"] = pt
        elif key == "wh_deposit":
            p.setdefault("warehouse", {})["deposit_pt"] = pt
        elif key == "mount_pt":
            p.setdefault("mount", {})["mount_pt"] = pt
        self._save()
        self._load_ui()
        self._log(f"已设置：{title} → {pt}")

    def _pick_dialog(self) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有绑定窗口", "请先绑定游戏窗口。")
            return
        if pick_region(
            self, hwnd, self.profile, "enter.dialog", "进本弹窗区域",
            "把「进入游戏」那个弹窗显示出来再截图，然后框住弹窗里固定不变的部分"
            "（标题/按钮区都行，不要框会闪的动画）。",
        ):
            self._load_ui()
            self._log("已设置：进本弹窗识别区域")

    def _capture_boss_samples(self) -> None:
        """站在 BOSS 面前点一下：连拍 10 张当样本（不同角度/动作都能认出来）。"""
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有绑定窗口", "请先绑定游戏窗口。")
            return
        spec = (self.profile.get("farm") or {}).get("boss_present") or {}
        roi = (spec or {}).get("roi")
        if not roi:
            QMessageBox.information(
                self, "先框选",
                "先用「框选」把 BOSS 本体框一下（框住 BOSS 身上不动的部分），"
                "然后再点「采样本」。",
            )
            return
        sigs = [t for t in (spec.get("sigs") or []) if t]
        if spec.get("sig") and spec.get("sig") not in sigs:
            sigs.insert(0, spec["sig"])
        added = 0
        grid = int(spec.get("grid", 12))
        for i in range(10):
            frame = app.capture_client_bgra(hwnd)[0]
            if frame is None:
                break
            try:
                patch = df.crop_norm(frame, roi)
                sig = df.signature(patch, grid)
            except Exception:
                sig = []
            if not sig:
                break
            if all(df.similarity(sig, old, 30.0) < 0.98 for old in sigs):
                sigs.append(sig)
                added += 1
            self._log(f"采样本 {i + 1}/10 …")
            self._game_sleep(0.3)
        spec["sigs"] = sigs
        spec["sig"] = sigs[0] if sigs else []
        self.profile.setdefault("farm", {})["boss_present"] = spec
        self._save()
        self._load_ui()
        self._log(f"BOSS 样本采集完成：新增 {added} 张，共 {len(sigs)} 张")
        QMessageBox.information(
            self, "采样本完成",
            f"新增 {added} 张，共 {len(sigs)} 张样本。\n"
            "建议：BOSS 走动/放技能时多点几次「采样本」，样本越多越不容易漏。",
        )

    @staticmethod
    def _game_sleep(seconds: float) -> None:
        import time as _time
        _t = _time.monotonic() + max(0.0, float(seconds))
        while _time.monotonic() < _t:
            _time.sleep(0.02)

    def _pick_extra_roi(self, where: str) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有绑定窗口", "请先绑定游戏窗口。")
            return
        titles = {
            "enter.progress": ("加载进度条区域",
                               "进本时那条读条：框住整条进度条（左边起点到右边终点），"
                               "程序会连拍多帧量填充比例，≥90% 才算加载完"),
            "farm.boss_present": ("BOSS 身形",
                                  "框住 BOSS 本体（身上的特征，不要框会动的特效）；"
                                  "跑到 BOSS 点后相似度够高才开始打，不够就重新寻路"),
            "farm.boss_hp": ("BOSS 血条区域",
                             "框住 BOSS 头顶那条血条；打怪过程中血条连续几次不见了，"
                             "就判定 BOSS 已死"),
        }
        title, info = titles.get(where, ("识别区域", ""))
        if pick_region(self, hwnd, self.profile, where, title, info):
            self._load_ui()
            self._log(f"已设置：{title}")

    def _pick_mount_roi(self, which: str) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有绑定窗口", "请先绑定游戏窗口。")
            return
        if which == "dialog":
            title = "骑乘弹窗区域"
            info = ("按 S 把骑乘窗口弹出来再截图，框住这个弹窗里固定不变的部分"
                    "（用来判断「按 S 后弹窗出来了没有」）")
        elif which == "text":
            title = "宠物文本区域"
            info = ("框住宠物/坐骑那一行文字（没骑时是「骑乘」，骑上后变「休息」），"
                    "程序 OCR 读它来判断要不要点")
        else:
            title = "骑乘状态标记"
            info = "（旧版用，可不标）框住「已骑乘时才有的那块」"
        if pick_region(self, hwnd, self.profile, f"mount.{which}", title, info):
            self._load_ui()
            self._log(f"已设置：{title}")

    def _pick_exit_dialog(self) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有绑定窗口", "请先绑定游戏窗口。")
            return
        if pick_region(
            self, hwnd, self.profile, "exit.dialog", "退出确认弹窗区域",
            "点一下「退出副本」，等「确认退出」那个弹窗出来再截图，"
            "框住这个弹窗里固定不变的部分（和进本弹窗是两个不同的区域）。",
        ):
            self._load_ui()
            self._log("已设置：退出确认弹窗识别区域")

    def _pick_fatigue_roi(self) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有绑定窗口", "请先绑定游戏窗口。")
            return
        if pick_region(self, hwnd, self.profile, "fatigue.roi",
                       "疲劳提示区域",
                       "先点一下疲劳图标让提示弹出来，再截图框住"
                       "「当前疲劳值:xxx/xxx」那块文字"):
            self._load_ui()
            self._log("已设置：疲劳提示区域")

    def _pick_hp(self) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有绑定窗口", "请先绑定游戏窗口。")
            return
        if pick_region(self, hwnd, self.profile, "farm.hp_roi",
                       "血量数字区域", "框住角色血量数字（如 1725/1780）"):
            self._load_ui()
            self._log("已设置：血量区域")

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------
    def _probe(self) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有绑定窗口", "请先绑定游戏窗口。")
            return
        game = df.WinGame(hwnd, self.profile, log=self._log)
        runner = df.DungeonRunner(game, self.profile, log=self._log)
        info = runner.probe()
        if not info:
            self._log("看一眼：截图失败")
            return
        self._log("看一眼：" + "  ".join(f"{k}={v}" for k, v in info.items()))

    def _missing_items(self) -> list[str]:
        """开跑前检查，缺什么直接告诉用户。"""
        p = self.profile
        enter = p.get("enter") or {}
        farm = p.get("farm") or {}
        exit_cfg = p.get("exit") or {}
        missing = []
        if not enter.get("dungeon_pt"):
            missing.append("① 副本入口（地图1）")
        if not enter.get("confirm_pt"):
            missing.append("① 弹窗「进入游戏」按钮")
        if not farm.get("boss_pt"):
            missing.append("② BOSS 点（地图2）")
        if not exit_cfg.get("exit_pt"):
            missing.append("③「退出副本」按钮")
        if not exit_cfg.get("confirm_pt"):
            missing.append("③「确认」按钮")
        return missing

    def _start_single(self, dry: bool) -> None:
        if self._nav_recorder is not None or self._nav_pending:
            self._log("正在录引路宏，先点「停止并保存」再开始运行")
            return
        thread = self.threads.get(0)
        if thread is not None:
            if thread.isRunning():
                self._log("已经在运行了（点「停止」先停）")
                return
            # 线程已结束但没清理：清掉，允许重新开始
            self.threads.pop(0, None)
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有绑定窗口", "请先绑定游戏窗口。")
            return
        missing = self._missing_items()
        if missing:
            QMessageBox.warning(
                self, "还没标定完",
                "下面这些还没设置，跑了也没用：\n\n"
                + "\n".join("· " + m for m in missing)
                + "\n\n点上面的「开始向导」一步步标定最快。",
            )
            self._log("无法开始：还缺 " + "、".join(missing))
            return
        self._save_from_ui()
        rounds = int(self.spin_rounds.value()) or None
        self.btn_start.setEnabled(False)
        self.btn_dry.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.lbl_state.setText("运行中…（试跑，不按键）" if dry else "运行中…")
        _mode_text = {
            "hybrid": "混合模式：点击时借鼠标，键盘后台",
            "foreground": "前台模式：全程占用鼠标键盘",
            "background": "纯后台：只发消息",
        }.get(self.combo_input_mode.currentData() or "hybrid", "")
        self._log(f"=== 副本方案「{self.combo_dungeon.currentData() or '未命名'}」"
                  f"要执行的流程（{_mode_text}）===")
        if not self.chk_allow_grab.isChecked():
            self._log("未允许抢占：纯后台（不切前台、不动光标），鼠标键盘完全归你")
        elif (self.combo_input_mode.currentData() or "hybrid") == "hybrid":
            self._log("已允许抢占：需要点击时才切前台+借鼠标，点完立刻还原")
        self._log(f"① 按「{self.edit_map_key.text().strip() or 'm'}」开地图1 → 点副本入口 "
                  f"→ 等 {self.spin_entry_wait.value():.0f}s → 等进本弹窗"
                  f"（识别到才点「进入游戏」）→ 等加载 {self.spin_load_wait.value():.0f}s")
        nav = self.profile.get("nav_macro") or {}
        if nav.get("enabled") and nav.get("events"):
            self._log("引路宏：已启用（进本/跑向 BOSS 用录制的鼠标宏，不点地图点位）")
        self._log(
            f"② 疲劳 < -阈值 → 打BOSS（上限 {self.spin_seconds.value():.0f}s，血条没了就结束）；"
            f"疲劳 > +阈值 → 回打怪点挂机；不设挂机时长，"
            f"每 {self.spin_trash_seconds.value():.0f}s 查一次疲劳"
        )
        self._log(f"③ 点「退出副本」→ 识别退出确认弹窗 → 点「确认」→ 等 {self.spin_exit_settle.value():.0f}s")
        self._launch(0, hwnd, rounds if not dry else 1, dry=dry)

    def _launch(self, slot: int, hwnd: int, rounds: Optional[int],
                dry: bool = False) -> None:
        profile = json.loads(json.dumps(self.profile))
        thread = DungeonThread(
            hwnd, profile, rounds, dry=dry,
            prefix=f"[窗口{slot + 1}] ", parent=self,
        )
        thread.line.connect(self._log)
        thread.finished_ok.connect(
            lambda ok, fail, s=slot: self._slot_done(s, ok, fail)
        )
        thread.finished.connect(lambda s=slot: self._slot_finished(s))
        self.threads[slot] = thread
        self.multi_buttons[slot].setText("停止")
        self.multi_state[slot].setText("运行中…")
        self._log(f"窗口{slot + 1} 开始（按疲劳自动挂机/打BOSS）"
                  + ("（试跑，不发按键）" if dry else ""))
        thread.start()

    def _toggle_slot(self, slot: int) -> None:
        if self._nav_recorder is not None or self._nav_pending:
            self._log("正在录引路宏，先点「停止并保存」再开始运行")
            return
        thread = self.threads.get(slot)
        if thread is not None:
            if thread.isRunning():
                thread.stop()
                self.multi_state[slot].setText("正在停止…")
            else:
                self.threads.pop(slot, None)
                self.multi_buttons[slot].setText("开始")
                self.multi_state[slot].setText("未运行")
            return
        hwnd = self.multi_combos[slot].currentData()
        if not hwnd:
            QMessageBox.warning(self, "没有窗口", "这一行还没选游戏窗口。")
            return
        self._save_from_ui()
        self._launch(slot, int(hwnd), int(self.spin_rounds.value()) or None,
                     dry=False)

    def _stop_all(self) -> None:
        for slot, thread in list(self.threads.items()):
            thread.stop()
            self.multi_state[slot].setText("正在停止…")

    def _stop_single(self) -> None:
        thread = self.threads.get(0)
        if thread is None:
            self._log("当前没有在跑")
            return
        thread.stop()
        self._log("已请求停止（最多 1 秒内生效）…")
        self.lbl_state.setText("正在停止…")

    def _slot_done(self, slot: int, ok: int, fail: int) -> None:
        self._log(f"窗口{slot + 1} 结束：成功 {ok} 轮 / 失败 {fail} 轮")

    def _slot_finished(self, slot: int) -> None:
        thread = self.threads.pop(slot, None)
        if thread is not None:
            thread.deleteLater()
        self.multi_buttons[slot].setText("开始")
        self.multi_state[slot].setText("未运行")
        self.btn_start.setEnabled(True)
        self.btn_dry.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_state.setText("未运行")
        self._log(f"窗口{slot + 1} 已结束")

    # ------------------------------------------------------------------
    # F8 开始 / F9 停止（全局热键）
    # ------------------------------------------------------------------
    def _install_hotkeys(self) -> None:
        self.hotkey_start.connect(self._on_hotkey_start,
                                  Qt.ConnectionType.QueuedConnection)
        self.hotkey_stop.connect(self._on_hotkey_stop,
                                 Qt.ConnectionType.QueuedConnection)
        try:
            import keyboard as _kb
        except Exception:
            self._log("没装 keyboard 库，F8/F9 热键不可用")
            return
        ok = []
        for key, sig in (("f8", self.hotkey_start), ("f9", self.hotkey_stop)):
            try:
                _kb.add_hotkey(key, sig.emit, suppress=False)
                ok.append(key.upper())
            except Exception as exc:
                self._log(f"注册 {key.upper()} 失败：{exc}")
        if ok:
            self._log(f"全局热键已注册：{' / '.join(ok)}（F8 开始，F9 停止）")

    def _on_hotkey_start(self) -> None:
        if self._nav_recorder is not None or self._nav_pending:
            self._log("[F8] 正在录引路宏，先停止录制")
            return
        if self.threads:
            self._log("[F8] 已经在运行了")
            return
        missing = self._missing_items()
        if missing:
            self._log("[F8] 还没标定完：" + "、".join(missing))
            QMessageBox.warning(
                self, "还没标定完",
                "下面这些还没设置，按 F8 也跑不了：\n\n"
                + "\n".join("· " + m for m in missing),
            )
            return
        self._save_from_ui()
        self._log("[F8] 开始运行：先启动多开里已选窗口，没选则启动单窗口")
        started = 0
        used: set[int] = set()
        for i in range(4):
            hwnd = self.multi_combos[i].currentData()
            if not hwnd:
                continue
            hwnd = int(hwnd)
            if hwnd in used:
                continue
            used.add(hwnd)
            self._toggle_slot(i)
            started += 1
        if not started:
            self._start_single(dry=False)

    def _on_hotkey_stop(self) -> None:
        self._log("[F9] 停止")
        if self._nav_recorder is not None or self._nav_pending:
            self._stop_nav_record()
            return
        self._stop_all()
        self._stop_single()

    # ------------------------------------------------------------------
    # 向导
    # ------------------------------------------------------------------
    def _open_wizard(self) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有绑定窗口", "请先绑定游戏窗口，再点向导。")
            return
        from dungeon_wizard import DungeonWizard

        dialog = DungeonWizard(hwnd, self.profile, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.profile = dialog.profile
        self._save()
        self._refresh_all()
        self._log("=== 向导完成 ===")
        for line in dialog.plan_lines:
            self._log("  · " + line)
