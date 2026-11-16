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
import os
import threading
from typing import Optional

import creature_curios_keyer as app
import dungeon_flow as df

try:
    import win32gui
except ImportError:  # pragma: no cover
    win32gui = None  # type: ignore

from PySide6.QtCore import QPoint, Qt, QThread, Signal
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
    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.profile = df.load_profile()
        self.threads: dict[int, DungeonThread] = {}
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
        root.addWidget(self._build_exit())
        root.addWidget(self._build_multi())
        root.addWidget(self._build_run())
        root.addStretch(1)

        self._loading = False
        self._refresh_all()

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
            ("dungeon_pt", "① 副本入口（地图1）", "map"),
            ("entry_pt", "① 副本门口/NPC（可选，走到后点它）", "shot"),
            ("enter_confirm", "① 弹窗「进入游戏」按钮", "shot"),
            ("farm_pt", "② 打怪点（地图2，在这里读疲劳）", "map"),
            ("boss_pt", "② BOSS 点（地图2）", "map"),
            ("exit_pt", "③「退出副本」按钮（第一下）", "shot"),
            ("exit_confirm", "③「确认」按钮（第二下）", "shot"),
            ("fatigue_pt", "④ 疲劳图标（点一下弹疲劳值）", "shot"),
            ("mount_pt", "⑤「骑乘」按钮（未骑乘时点它）", "shot"),
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
        g.addLayout(row, row0 + 4, 0, 1, 3)
        return box

    def _build_farm(self) -> QWidget:
        box = QGroupBox("时间参数（都能改）与挂机设置")
        g = QGridLayout(box)
        g.addWidget(QLabel("走进副本(寻路)"), 0, 0)
        self.spin_entry_wait = self._dspin(1.0, 600.0, 40.0, g, 0, 1)

        g.addWidget(QLabel("等弹窗最多"), 0, 2)
        self.spin_dialog_timeout = self._dspin(3.0, 300.0, 20.0, g, 0, 3)

        g.addWidget(QLabel("没弹窗重试"), 0, 4)
        self.spin_retry = QSpinBox()
        self.spin_retry.setRange(1, 10)
        self.spin_retry.setValue(3)
        self.spin_retry.setFixedWidth(70)
        self.spin_retry.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_retry, 0, 5)

        g.addWidget(QLabel("进本加载"), 0, 6)
        self.spin_load_wait = self._dspin(1.0, 300.0, 10.0, g, 0, 7)

        g.addWidget(QLabel("跑向BOSS"), 1, 0)
        self.spin_boss_wait = self._dspin(1.0, 600.0, 30.0, g, 1, 1)

        g.addWidget(QLabel("BOSS 点挂机"), 1, 2)
        self.spin_seconds = self._dspin(5.0, 3600.0, 30.0, g, 1, 3)

        g.addWidget(QLabel("退出后加载"), 1, 4)
        self.spin_exit_settle = self._dspin(1.0, 300.0, 10.0, g, 1, 5)

        g.addWidget(QLabel("每轮间隔"), 1, 6)
        self.spin_gap = self._dspin(0.0, 3600.0, 8.0, g, 1, 7)

        g.addWidget(QLabel("喝药间隔"), 2, 0)
        self.spin_potion = self._dspin(1.0, 60.0, 6.0, g, 2, 1)

        g.addWidget(QLabel("拾取间隔"), 2, 2)
        self.spin_pickup = self._dspin(0.0, 120.0, 8.0, g, 2, 3)

        g.addWidget(QLabel("血量阈值"), 2, 4)
        self.spin_hp = QSpinBox()
        self.spin_hp.setRange(0, 9999999)
        self.spin_hp.setValue(0)
        self.spin_hp.setFixedWidth(100)
        self.spin_hp.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_hp, 2, 5)
        self.btn_hp_roi = QPushButton("框血量区域")
        self.btn_hp_roi.clicked.connect(self._pick_hp)
        g.addWidget(self.btn_hp_roi, 2, 6)
        self.lbl_hp = QLabel("未设置（不框就按固定间隔喝药）")
        g.addWidget(self.lbl_hp, 2, 7)

        self.chk_foreground = QCheckBox("前台模式：先切到游戏窗口，用真实鼠标键盘（宏方式）")
        self.chk_foreground.setChecked(True)
        self.chk_foreground.setToolTip(
            "勾选：点「开始运行」会把游戏切到前台，并用真实鼠标点击、真实按键（和鼠标宏一样）。"
            "运行期间会占用你的鼠标键盘，别同时用电脑。\n"
            "不勾：只用 PostMessage 后台消息（不抢鼠标），但有些游戏不认，会出现“开了地图就不动”。"
        )
        self.chk_foreground.toggled.connect(self._save_from_ui)
        g.addWidget(self.chk_foreground, 5, 0, 1, 8)

        row = QHBoxLayout()
        self.chk_skills = QCheckBox("放技能 1/2")
        self.chk_skills.toggled.connect(self._save_from_ui)
        row.addWidget(self.chk_skills)
        row.addWidget(QLabel("开图键"))
        self.edit_map_key = QLineEdit()
        self.edit_map_key.setFixedWidth(54)
        self.edit_map_key.editingFinished.connect(self._save_from_ui)
        row.addWidget(self.edit_map_key)
        row.addWidget(QLabel("拾取键"))
        self.edit_pickup_key = QLineEdit()
        self.edit_pickup_key.setFixedWidth(54)
        self.edit_pickup_key.editingFinished.connect(self._save_from_ui)
        row.addWidget(self.edit_pickup_key)
        row.addWidget(QLabel("轮数(0=无限)"))
        self.spin_rounds = QSpinBox()
        self.spin_rounds.setRange(0, 9999)
        self.spin_rounds.setFixedWidth(80)
        self.spin_rounds.valueChanged.connect(self._save_from_ui)
        row.addWidget(self.spin_rounds)
        row.addWidget(QLabel("骑乘键"))
        self.edit_mount_key = QLineEdit()
        self.edit_mount_key.setFixedWidth(44)
        self.edit_mount_key.editingFinished.connect(self._save_from_ui)
        row.addWidget(self.edit_mount_key)
        self.chk_mount = QCheckBox("每次寻路前按 S 检查骑乘")
        self.chk_mount.setChecked(True)
        self.chk_mount.toggled.connect(self._save_from_ui)
        row.addWidget(self.chk_mount)
        row.addStretch(1)
        g.addLayout(row, 3, 0, 1, 8)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("疲劳 < -阈值 打BOSS"))
        self.spin_boss_seconds = self._dspin(5.0, 600.0, 30.0, None, 0, 0)
        row2.addWidget(self.spin_boss_seconds)
        row2.addWidget(QLabel("秒 ×"))
        self.spin_boss_rounds = QSpinBox()
        self.spin_boss_rounds.setRange(1, 99)
        self.spin_boss_rounds.setFixedWidth(60)
        self.spin_boss_rounds.valueChanged.connect(self._save_from_ui)
        row2.addWidget(self.spin_boss_rounds)
        row2.addWidget(QLabel("只"))
        row2.addSpacing(16)
        row2.addWidget(QLabel("疲劳 > +阈值 打怪"))
        self.spin_trash_seconds = self._dspin(5.0, 1800.0, 60.0, None, 0, 0)
        row2.addWidget(self.spin_trash_seconds)
        row2.addWidget(QLabel("秒 ×"))
        self.spin_trash_rounds = QSpinBox()
        self.spin_trash_rounds.setRange(1, 99)
        self.spin_trash_rounds.setFixedWidth(60)
        self.spin_trash_rounds.valueChanged.connect(self._save_from_ui)
        row2.addWidget(self.spin_trash_rounds)
        row2.addWidget(QLabel("轮"))
        row2.addSpacing(16)
        row2.addWidget(QLabel("打怪时每"))
        self.spin_fatigue_every = self._dspin(0.0, 600.0, 60.0, None, 0, 0)
        row2.addWidget(self.spin_fatigue_every)
        row2.addWidget(QLabel("秒查疲劳(0=不查)"))
        row2.addStretch(1)
        g.addLayout(row2, 4, 0, 1, 8)
        return box

    def _dspin(self, low: float, high: float, value: float,
               grid, row: int, col: int) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setSuffix(" 秒")
        spin.setValue(value)
        spin.setFixedWidth(100)
        spin.valueChanged.connect(self._save_from_ui)
        if grid is not None:
            grid.addWidget(spin, row, col)
        return spin

    def _build_exit(self) -> QWidget:
        box = QGroupBox("④ 退出副本后：检测疲劳，不足就先普通挂机打怪")
        g = QGridLayout(box)
        self.chk_fatigue = QCheckBox("每轮退出副本后检测一次疲劳值")
        self.chk_fatigue.setChecked(True)
        self.chk_fatigue.toggled.connect(self._save_from_ui)
        g.addWidget(self.chk_fatigue, 0, 0, 1, 2)

        g.addWidget(QLabel("疲劳阈值"), 0, 2)
        self.spin_fatigue = QSpinBox()
        self.spin_fatigue.setRange(0, 999999)
        self.spin_fatigue.setValue(800)
        self.spin_fatigue.setFixedWidth(100)
        self.spin_fatigue.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_fatigue, 0, 3)

        g.addWidget(QLabel("低于阈值先普通打怪"), 0, 4)
        self.spin_fatigue_farm = QDoubleSpinBox()
        self.spin_fatigue_farm.setRange(0.0, 7200.0)
        self.spin_fatigue_farm.setSuffix(" 秒")
        self.spin_fatigue_farm.setValue(300.0)
        self.spin_fatigue_farm.setFixedWidth(110)
        self.spin_fatigue_farm.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_fatigue_farm, 0, 5)

        self.btn_fatigue_roi = QPushButton("框疲劳提示区域（可选）")
        self.btn_fatigue_roi.clicked.connect(self._pick_fatigue_roi)
        g.addWidget(self.btn_fatigue_roi, 1, 0)
        self.lbl_fatigue_roi = QLabel("未设置（会沿用主界面「疲劳检测」框过的区域）")
        g.addWidget(self.lbl_fatigue_roi, 1, 1, 1, 6)
        g.addWidget(_hint(
            "流程：退出副本 → 点一下上面④设置的「疲劳图标」→ 读取弹出的疲劳值 → "
            "低于阈值就先普通挂机打怪一段时间，再继续下一轮副本。"
        ), 2, 0, 1, 8)
        return box

    # ------------------------------------------------------------------
    # 多开
    # ------------------------------------------------------------------
    def _build_multi(self) -> QWidget:
        box = QGroupBox("多开（每个窗口可设不同挂机时长）")
        g = QGridLayout(box)
        self.multi_combos: list[QComboBox] = []
        self.multi_seconds: list[QDoubleSpinBox] = []
        self.multi_state: list[QLabel] = []
        self.multi_buttons: list[QPushButton] = []
        for i in range(4):
            combo = QComboBox()
            combo.setMinimumWidth(240)
            spin = QDoubleSpinBox()
            spin.setRange(5.0, 3600.0)
            spin.setSuffix(" 秒")
            spin.setFixedWidth(100)
            state = QLabel("未运行")
            state.setObjectName("muted")
            btn = QPushButton("开始")
            btn.setFixedWidth(72)
            btn.clicked.connect(lambda _=False, idx=i: self._toggle_slot(idx))
            g.addWidget(QLabel(f"窗口 {i + 1}"), i, 0)
            g.addWidget(combo, i, 1)
            g.addWidget(QLabel("挂机"), i, 2)
            g.addWidget(spin, i, 3)
            g.addWidget(btn, i, 4)
            g.addWidget(state, i, 5, 1, 3)
            self.multi_combos.append(combo)
            self.multi_seconds.append(spin)
            self.multi_state.append(state)
            self.multi_buttons.append(btn)
        row = QHBoxLayout()
        btn_refresh = QPushButton("刷新窗口列表")
        btn_refresh.clicked.connect(self._refresh_windows)
        btn_all = QPushButton("全部开始")
        btn_all.clicked.connect(lambda: [self._toggle_slot(i) for i in range(4)
                                        if self.multi_buttons[i].text() == "开始"])
        btn_stop = QPushButton("全部停止")
        btn_stop.clicked.connect(self._stop_all)
        row.addWidget(btn_refresh)
        row.addWidget(btn_all)
        row.addWidget(btn_stop)
        row.addStretch(1)
        g.addLayout(row, 4, 0, 1, 8)
        return box

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

            self.spin_entry_wait.setValue(float(enter.get("travel_wait", 40.0)))
            self.spin_dialog_timeout.setValue(float(enter.get("dialog_timeout", 20.0)))
            self.spin_retry.setValue(int(enter.get("retry", 3) or 3))
            self.spin_load_wait.setValue(float(enter.get("load_wait", 10.0)))
            self.spin_boss_wait.setValue(float(farm.get("travel_wait", 30.0)))
            self.spin_seconds.setValue(float(farm.get("seconds", 30.0)))
            self.spin_hp.setValue(int(farm.get("hp_threshold", 0) or 0))
            self.spin_potion.setValue(float(farm.get("potion_every", 6.0)))
            self.spin_pickup.setValue(float(farm.get("pickup_every", 8.0)))
            self.chk_skills.setChecked(bool(farm.get("use_skills", True)))
            mount = p.get("mount") or {}
            loop = p.get("loop") or {}
            self.chk_mount.setChecked(bool(mount.get("enabled", True)))
            self.edit_mount_key.setText(str(mount.get("key", "s")))
            self.spin_boss_seconds.setValue(float(loop.get("boss_seconds", 30.0)))
            self.spin_boss_rounds.setValue(int(loop.get("boss_rounds", 1) or 1))
            self.spin_trash_seconds.setValue(float(loop.get("trash_seconds", 60.0)))
            self.spin_trash_rounds.setValue(int(loop.get("trash_rounds", 1) or 1))
            self.spin_fatigue_every.setValue(float(loop.get("fatigue_every", 60.0)))
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
            self.chk_foreground.setChecked(
                str(p.get("input_mode", "foreground")) != "background"
            )
            self.edit_map_key.setText(str(keys.get("open_map", "m")))
            self.edit_pickup_key.setText(str(keys.get("pickup", "space")))
            self.spin_exit_settle.setValue(float(exit_cfg.get("settle", 10.0)))
            self.spin_gap.setValue(float((p.get("retry") or {}).get("gap", 8.0)))
            fatigue = p.get("fatigue") or {}
            self.chk_fatigue.setChecked(bool(fatigue.get("enabled", True)))
            self.spin_fatigue.setValue(int(fatigue.get("threshold", 800) or 0))
            self.spin_fatigue_farm.setValue(float(fatigue.get("farm_seconds", 300.0)))

            states = {
                "dungeon_pt": (enter.get("dungeon_pt"), "① 副本入口"),
                "enter_confirm": (enter.get("confirm_pt"), "① 进入游戏"),
                "boss_pt": (farm.get("boss_pt"), "② BOSS 点"),
                "exit_pt": (exit_cfg.get("exit_pt"), "③ 退出副本"),
                "exit_confirm": (exit_cfg.get("confirm_pt"), "③ 确认"),
                "fatigue_pt": ((p.get("fatigue") or {}).get("pt"), "④ 疲劳图标"),
                "farm_pt": (farm.get("farm_pt"), "② 打怪点"),
                "mount_pt": ((p.get("mount") or {}).get("mount_pt"), "⑤ 骑乘按钮"),
                "entry_pt": (enter.get("entry_pt"), "① 副本门口/NPC"),
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
        p.setdefault("fatigue", {}).update({
            "enabled": bool(self.chk_fatigue.isChecked()),
            "threshold": int(self.spin_fatigue.value()),
            "farm_seconds": float(self.spin_fatigue_farm.value()),
        })
        p.setdefault("mount", {}).update({
            "enabled": bool(self.chk_mount.isChecked()),
            "key": self.edit_mount_key.text().strip() or "s",
            "mount_text": self.edit_mount_text.text().strip() or "骑乘",
            "rest_text": self.edit_rest_text.text().strip() or "休息",
        })
        p.setdefault("loop", {}).update({
            "boss_seconds": float(self.spin_boss_seconds.value()),
            "boss_rounds": int(self.spin_boss_rounds.value()),
            "trash_seconds": float(self.spin_trash_seconds.value()),
            "trash_rounds": int(self.spin_trash_rounds.value()),
            "fatigue_every": float(self.spin_fatigue_every.value()),
        })
        p.setdefault("retry", {})["rounds"] = int(self.spin_rounds.value())
        p["input_mode"] = "foreground" if self.chk_foreground.isChecked() else "background"
        p.setdefault("retry", {})["gap"] = float(self.spin_gap.value())
        self._save()

    def _refresh_windows(self) -> None:
        wins = df.list_windows("CreatureCurios")
        for combo in self.multi_combos:
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for hwnd, title in wins:
                combo.addItem(f"{title[:36]} [0x{hwnd:X}]", hwnd)
            if current:
                index = combo.findData(current)
                if index >= 0:
                    combo.setCurrentIndex(index)
            combo.blockSignals(False)
        bound = self._hwnd()
        if bound:
            for combo in self.multi_combos:
                index = combo.findData(bound)
                if index >= 0:
                    combo.setCurrentIndex(index)
                    break

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
            "farm_pt": "打开地图2（副本地图），点一下「打怪点」位置（在这里读疲劳）。",
            "mount_pt": "按 S 打开骑乘窗口，点一下里面的「骑乘」按钮。",
            "entry_pt": ("可选：走到副本门口后，如果还要点一下传送门/NPC 才弹窗，"
                         "把那个位置点上（不标就直接等弹窗）。"),
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
        elif key == "mount_pt":
            current = (p.get("mount") or {}).get("mount_pt")
        elif key == "entry_pt":
            current = (p.get("enter") or {}).get("entry_pt")

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
        elif key == "mount_pt":
            p.setdefault("mount", {})["mount_pt"] = pt
        elif key == "entry_pt":
            p.setdefault("enter", {})["entry_pt"] = pt
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
        self._log(f"=== 副本方案「{self.combo_dungeon.currentData() or '未命名'}」"
                  "要执行的流程（"
                  + ("前台模式：真实鼠标键盘" if self.chk_foreground.isChecked()
                     else "后台模式：PostMessage")
                  + "）===")
        if self.chk_foreground.isChecked():
            self._log("开始运行会先把游戏窗口切到前台，然后用真实鼠标点击地图点 → 运行期间别动鼠标")
        self._log(f"① 按「{self.edit_map_key.text().strip() or 'm'}」开地图1 → 点副本入口 "
                  f"→ 等 {self.spin_entry_wait.value():.0f}s → 等进本弹窗"
                  f"（识别到才点「进入游戏」）→ 等加载 {self.spin_load_wait.value():.0f}s")
        self._log(f"② 开地图2 → 点 BOSS 点 → 等 {self.spin_boss_wait.value():.0f}s "
                  f"→ 挂机 {self.spin_seconds.value():.0f}s（喝药 {self.spin_potion.value():.0f}s / "
                  f"拾取 {self.spin_pickup.value():.0f}s）")
        self._log(f"③ 点「退出副本」→ 识别退出确认弹窗 → 点「确认」→ 等 {self.spin_exit_settle.value():.0f}s")
        self._launch(0, hwnd, rounds if not dry else 1, dry=dry,
                     seconds=float(self.spin_seconds.value()))

    def _launch(self, slot: int, hwnd: int, rounds: Optional[int],
                dry: bool, seconds: float) -> None:
        profile = df.copy_profile_for_slot(self.profile, seconds)
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
        self._log(f"窗口{slot + 1} 开始（挂机 {seconds:.0f}s）"
                  + ("（试跑，不发按键）" if dry else ""))
        thread.start()

    def _toggle_slot(self, slot: int) -> None:
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
        seconds = float(self.multi_seconds[slot].value())
        self._save_from_ui()
        self._launch(slot, int(hwnd), int(self.spin_rounds.value()) or None,
                     dry=False, seconds=seconds)

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
