#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「跨图打BOSS」分页：图2 挂机打怪 → 去图1 打BOSS → 回图2 打怪点。

这一页是独立功能（和「副本流程」分开存标定），配置在：
    %APPDATA%\\CreatureCuriosKeyer\\cross_profile.json
"""

from __future__ import annotations

import datetime
import threading
import time
from typing import Optional

import creature_curios_keyer as app
import dungeon_cross as dc
import dungeon_flow as df
import dungeon_ui as dui

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

try:
    import win32gui
except ImportError:  # pragma: no cover
    win32gui = None


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setObjectName("muted")
    return lab


class CrossThread(QThread):
    line = Signal(str)
    finished_ok = Signal(int, int)

    def __init__(self, hwnd: int, profile: dict, rounds: Optional[int],
                 dry: bool = False, mode: str = "run", parent=None):
        super().__init__(parent)
        self.hwnd = int(hwnd)
        self.profile = profile
        self.rounds = rounds
        self.dry = bool(dry)
        self.mode = mode          # run / go / back / fatigue
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            game = df.WinGame(
                self.hwnd, self.profile, log=self.line.emit, dry=self.dry,
                snapshot_dir=df.snapshot_dir(), prefix="[跨图] ",
                stop_event=self._stop,
            )
            runner = dc.CrossRunner(
                game, self.profile, log=self.line.emit,
                stop_event=self._stop, verbose=True,
            )
            if self.mode in ("go", "back"):
                ok = runner.test_path(self.mode)
                self.finished_ok.emit(1 if ok else 0, 0 if ok else 1)
            elif self.mode == "fatigue":
                cur, mx, msg = runner.fatigue_check()
                if cur is None:
                    self.line.emit(f"疲劳读不到：{msg}")
                else:
                    self.line.emit(f"疲劳读到：{cur}" + (f"/{mx}" if mx else ""))
                self.finished_ok.emit(0, 0)
            else:
                runner.run(self.rounds)
                self.finished_ok.emit(runner.stats["ok"], runner.stats["fail"])
        except Exception as exc:  # pragma: no cover
            self.line.emit(f"[错误] {type(exc).__name__}: {exc}")
            self.finished_ok.emit(0, 0)


class CrossTab(QWidget):
    """跨图打BOSS 分页。"""

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.profile = dc.load_profile()
        self.thread: Optional[CrossThread] = None
        self._loading = True
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._save_now)
        self._last_pick_frame = None
        self.step_widgets: dict = {}
        self.step_wait: dict = {}
        self.step_open_map: dict = {}
        self.step_close_map: dict = {}

        self.setStyleSheet(dui.LIGHT_QSS)
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
        root.addWidget(self._build_farm_points())
        root.addWidget(self._build_steps("② 去程路径（图2 → 图1）", "go_steps"))
        root.addWidget(self._build_dungeon())
        root.addWidget(self._build_boss())
        root.addWidget(self._build_steps("⑤ 回程路径（图1 → 图2）", "back_steps"))
        root.addWidget(self._build_params())
        root.addWidget(self._build_run())
        root.addStretch(1)

        self._loading = False
        self._load_ui()

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build_header(self) -> QWidget:
        box = QGroupBox("当前游戏窗口（和「单开挂机」绑定的同一个）")
        row = QHBoxLayout(box)
        self.lbl_window = QLabel("未绑定")
        self.lbl_window.setWordWrap(True)
        btn = QPushButton("刷新")
        btn.setFixedWidth(80)
        btn.clicked.connect(self._load_ui)
        row.addWidget(self.lbl_window, 1)
        row.addWidget(btn)
        return box

    def _build_farm_points(self) -> QWidget:
        box = QGroupBox("① 图2 打怪点（可以标多个，用下拉框切换）")
        g = QGridLayout(box)
        g.addWidget(QLabel("当前打怪点"), 0, 0)
        self.combo_farm = QComboBox()
        self.combo_farm.setMinimumWidth(220)
        self.combo_farm.currentIndexChanged.connect(self._on_farm_changed)
        g.addWidget(self.combo_farm, 0, 1)
        row = QHBoxLayout()
        self.btn_farm_pick = QPushButton("标定 / 改点")
        self.btn_farm_pick.clicked.connect(self._pick_farm_point)
        self.btn_farm_add = QPushButton("新增")
        self.btn_farm_add.clicked.connect(self._add_farm_point)
        self.btn_farm_del = QPushButton("删除")
        self.btn_farm_del.clicked.connect(self._del_farm_point)
        self.btn_farm_rename = QPushButton("改名")
        self.btn_farm_rename.clicked.connect(self._rename_farm_point)
        for b in (self.btn_farm_pick, self.btn_farm_add,
                  self.btn_farm_del, self.btn_farm_rename):
            row.addWidget(b)
        row.addStretch(1)
        g.addLayout(row, 1, 0, 1, 6)

        g.addWidget(QLabel("寻路等待"), 2, 0)
        self.spin_farm_travel = self._dspin(1.0, 600.0, 20.0)
        self.spin_farm_travel.valueChanged.connect(self._on_farm_travel)
        g.addWidget(self.spin_farm_travel, 2, 1)
        g.addWidget(_hint("点完打怪点后等多久（秒）——角色自动寻路过去"), 2, 2, 1, 4)
        g.addWidget(_hint(
            "标定方法：角色站在图2 → 点「标定/改点」（会自动按开图键截图）"
            "→ 在图上点打怪的位置。挂机就在这个点打怪。"
        ), 3, 0, 1, 6)
        return box

    def _build_steps(self, title: str, key: str) -> QWidget:
        box = QGroupBox(title)
        v = QVBoxLayout(box)
        lst = QListWidget()
        lst.setMinimumHeight(96)
        lst.currentRowChanged.connect(lambda _i, k=key: self._load_step_editor(k))
        self.step_widgets[key] = lst
        v.addWidget(lst)

        row = QHBoxLayout()
        btn_click = QPushButton("＋ 加点击")
        btn_click.setToolTip("在图上点一个位置（会自动按开图键截图）")
        btn_click.clicked.connect(lambda _c=False, k=key: self._add_click_step(k))
        btn_key = QPushButton("＋ 加按键")
        btn_key.clicked.connect(lambda _c=False, k=key: self._add_key_step(k))
        btn_enter = QPushButton("＋ 进本")
        btn_enter.setToolTip("在这一步执行「进入副本」（点副本入口→进本弹窗→进度条）")
        btn_enter.clicked.connect(
            lambda _c=False, k=key: self._add_special_step(k, "enter"))
        btn_exit = QPushButton("＋ 退本")
        btn_exit.setToolTip("在这一步执行「退出副本」（退出副本→确认→等加载）")
        btn_exit.clicked.connect(
            lambda _c=False, k=key: self._add_special_step(k, "exit"))
        btn_up = QPushButton("上移")
        btn_up.clicked.connect(lambda _c=False, k=key: self._move_step(k, -1))
        btn_down = QPushButton("下移")
        btn_down.clicked.connect(lambda _c=False, k=key: self._move_step(k, 1))
        btn_del = QPushButton("删除")
        btn_del.clicked.connect(lambda _c=False, k=key: self._del_step(k))
        for b in (btn_click, btn_key, btn_enter, btn_exit,
                  btn_up, btn_down, btn_del):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("这一步等"))
        spin = self._dspin(0.0, 600.0, 8.0)
        spin.setEnabled(False)
        spin.valueChanged.connect(lambda val, k=key: self._on_step_wait(k, val))
        self.step_wait[key] = spin
        row2.addWidget(spin)
        chk = QCheckBox("点击前先开地图")
        chk.setEnabled(False)
        chk.toggled.connect(lambda val, k=key: self._on_step_open_map(k, bool(val)))
        self.step_open_map[key] = chk
        row2.addWidget(chk)
        chk_close = QCheckBox("点完关地图")
        chk_close.setEnabled(False)
        chk_close.toggled.connect(
            lambda val, k=key: self._on_step_close_map(k, bool(val)))
        self.step_close_map[key] = chk_close
        row2.addWidget(chk_close)
        row2.addWidget(_hint("按顺序执行；「加点击」时程序会先按开图键再截图"), 1)
        v.addLayout(row2)
        return box

    def _build_dungeon(self) -> QWidget:
        box = QGroupBox("③ 图1 的副本（进本 / 退本）")
        g = QGridLayout(box)
        row_auto = QHBoxLayout()
        self.chk_auto_enter = QCheckBox("到图1 后自动「进入副本」")
        self.chk_auto_enter.setChecked(True)
        self.chk_auto_enter.setToolTip(
            "去程路径走完后：点副本入口 → 等进本弹窗 → 点「进入游戏」→ 等进度条")
        self.chk_auto_enter.toggled.connect(self._save_from_ui)
        row_auto.addWidget(self.chk_auto_enter)
        self.chk_auto_exit = QCheckBox("打完 BOSS 自动「退出副本」")
        self.chk_auto_exit.setChecked(True)
        self.chk_auto_exit.setToolTip(
            "打完 BOSS → 拾取 → 点「退出副本」→ 点「确认」→ 等加载，然后再走回程")
        self.chk_auto_exit.toggled.connect(self._save_from_ui)
        row_auto.addWidget(self.chk_auto_exit)
        row_auto.addStretch(1)
        g.addLayout(row_auto, 0, 0, 1, 8)

        self.lbl_pt = {}
        for i, (key, title) in enumerate((
                ("enter.dungeon_pt", "副本入口"),
                ("enter.confirm_pt", "进入游戏按钮"),
                ("exit.exit_pt", "退出副本按钮"),
                ("exit.confirm_pt", "确认按钮"))):
            r = 1 + i // 2
            c = (i % 2) * 4
            g.addWidget(QLabel(title), r, c)
            lbl = QLabel("未设置")
            lbl.setObjectName("muted")
            self.lbl_pt[key] = lbl
            g.addWidget(lbl, r, c + 1, 1, 2)
            btn = QPushButton("标定")
            btn.setFixedWidth(80)
            btn.clicked.connect(
                lambda _c=False, k=key: self._pick_dungeon_point(k))
            g.addWidget(btn, r, c + 3)

        self.lbl_region = {}
        for i, (kind, title) in enumerate((
                ("enter_dialog", "进本弹窗区域"),
                ("progress", "加载进度条区域"),
                ("exit_dialog", "退本确认弹窗"))):
            r = 3 + i // 2
            c = (i % 2) * 4
            g.addWidget(QLabel(title), r, c)
            lbl = QLabel("未设置（可选）")
            lbl.setObjectName("muted")
            self.lbl_region[kind] = lbl
            g.addWidget(lbl, r, c + 1, 1, 2)
            btn = QPushButton("框选")
            btn.setFixedWidth(80)
            btn.clicked.connect(lambda _c=False, k=kind: self._pick_spec(k))
            g.addWidget(btn, r, c + 3)

        g.addWidget(QLabel("进本寻路"), 5, 0)
        self.spin_entry_wait = self._dspin(1.0, 600.0, 40.0)
        self.spin_entry_wait.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_entry_wait, 5, 1)
        g.addWidget(QLabel("等弹窗"), 5, 2)
        self.spin_dialog_timeout = self._dspin(3.0, 300.0, 24.0)
        self.spin_dialog_timeout.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_dialog_timeout, 5, 3)
        g.addWidget(QLabel("进本加载"), 5, 4)
        self.spin_load_wait = self._dspin(1.0, 300.0, 15.0)
        self.spin_load_wait.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_load_wait, 5, 5)
        g.addWidget(QLabel("退本加载"), 5, 6)
        self.spin_exit_settle = self._dspin(1.0, 300.0, 10.0)
        self.spin_exit_settle.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_exit_settle, 5, 7)

        g.addWidget(_hint(
            "不勾「自动进本/退本」也可以：在 ②/⑤ 的路径里用「＋ 进本 / ＋ 退本」"
            "把这两步插到任意位置。弹窗/进度条区域不框也能跑（会自动盲点 + 按已进本"
            "处理），框了更稳。"
        ), 6, 0, 1, 8)
        return box

    def _build_boss(self) -> QWidget:
        box = QGroupBox("④ 图1 的 BOSS（点位 / 身形 / 血量）")
        g = QGridLayout(box)
        self.lbl_boss_pt = QLabel("未设置")
        self.lbl_boss_pt.setObjectName("muted")
        g.addWidget(QLabel("BOSS 点"), 0, 0)
        g.addWidget(self.lbl_boss_pt, 0, 1, 1, 2)
        btn_pt = QPushButton("标定")
        btn_pt.setFixedWidth(80)
        btn_pt.clicked.connect(self._pick_boss_point)
        g.addWidget(btn_pt, 0, 3)

        self.lbl_boss_present = QLabel("未设置（不框就默认到了）")
        self.lbl_boss_present.setObjectName("muted")
        g.addWidget(QLabel("BOSS 身形"), 1, 0)
        g.addWidget(self.lbl_boss_present, 1, 1, 1, 2)
        btn_present = QPushButton("框选")
        btn_present.setFixedWidth(80)
        btn_present.clicked.connect(lambda: self._pick_spec("present"))
        btn_samples = QPushButton("采样本")
        btn_samples.setFixedWidth(80)
        btn_samples.setToolTip("站在 BOSS 面前点它：连拍 10 张当样本（不同动作/角度）")
        btn_samples.clicked.connect(self._capture_samples)
        row_p = QHBoxLayout()
        row_p.addWidget(btn_present)
        row_p.addWidget(btn_samples)
        g.addLayout(row_p, 1, 3)

        self.lbl_boss_hp = QLabel("未设置（不框就按时间停）")
        self.lbl_boss_hp.setObjectName("muted")
        g.addWidget(QLabel("BOSS 血量"), 2, 0)
        g.addWidget(self.lbl_boss_hp, 2, 1, 1, 2)
        btn_hp = QPushButton("框选")
        btn_hp.setFixedWidth(80)
        btn_hp.clicked.connect(lambda: self._pick_spec("boss_hp"))
        g.addWidget(btn_hp, 2, 3)

        g.addWidget(QLabel("BOSS 点寻路"), 3, 0)
        self.spin_boss_travel = self._dspin(1.0, 600.0, 25.0)
        self.spin_boss_travel.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_boss_travel, 3, 1)
        g.addWidget(QLabel("BOSS 上限"), 3, 2)
        self.spin_boss_seconds = self._dspin(5.0, 1000.0, 120.0)
        self.spin_boss_seconds.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_boss_seconds, 3, 3)

        row_clone = QHBoxLayout()
        self.chk_clone = QCheckBox("BOSS 有分身（打完继续打下一只）")
        self.chk_clone.toggled.connect(self._save_from_ui)
        row_clone.addWidget(self.chk_clone)
        row_clone.addWidget(QLabel("一共打"))
        self.spin_boss_count = QSpinBox()
        self.spin_boss_count.setRange(2, 9)
        self.spin_boss_count.setValue(3)
        self.spin_boss_count.setSuffix(" 只")
        self.spin_boss_count.setFixedWidth(80)
        self.spin_boss_count.valueChanged.connect(self._save_from_ui)
        row_clone.addWidget(self.spin_boss_count)
        row_clone.addStretch(1)
        g.addLayout(row_clone, 4, 0, 1, 4)
        return box

    def _build_params(self) -> QWidget:
        box = QGroupBox("⑥ 参数（疲劳 / 喝药 / 打怪）")
        g = QGridLayout(box)
        g.addWidget(QLabel("疲劳下限"), 0, 0)
        self.spin_low = QSpinBox()
        self.spin_low.setRange(-999999, 999999)
        self.spin_low.setFixedWidth(100)
        self.spin_low.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_low, 0, 1)
        g.addWidget(QLabel("疲劳上限"), 0, 2)
        self.spin_high = QSpinBox()
        self.spin_high.setRange(-999999, 999999)
        self.spin_high.setFixedWidth(100)
        self.spin_high.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_high, 0, 3)
        g.addWidget(QLabel("查疲劳间隔"), 0, 4)
        self.spin_fatigue_every = self._dspin(10.0, 600.0, 90.0)
        self.spin_fatigue_every.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_fatigue_every, 0, 5)

        self.chk_potion = QCheckBox("血量低于阈值喝药")
        self.chk_potion.toggled.connect(self._save_from_ui)
        g.addWidget(self.chk_potion, 1, 0, 1, 2)
        g.addWidget(QLabel("阈值"), 1, 2)
        self.spin_hp = QSpinBox()
        self.spin_hp.setRange(0, 9999999)
        self.spin_hp.setFixedWidth(100)
        self.spin_hp.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_hp, 1, 3)
        g.addWidget(QLabel("喝药键"), 1, 4)
        self.edit_keys = QLineEdit("q,w,e")
        self.edit_keys.setFixedWidth(90)
        self.edit_keys.editingFinished.connect(self._save_from_ui)
        g.addWidget(self.edit_keys, 1, 5)

        self.lbl_hp_roi = QLabel("未设置（不框就按固定间隔喝）")
        self.lbl_hp_roi.setObjectName("muted")
        g.addWidget(QLabel("玩家血量区"), 2, 0)
        g.addWidget(self.lbl_hp_roi, 2, 1, 1, 2)
        btn_hp_roi = QPushButton("框选")
        btn_hp_roi.setFixedWidth(80)
        btn_hp_roi.clicked.connect(lambda: self._pick_spec("player_hp"))
        g.addWidget(btn_hp_roi, 2, 3)
        g.addWidget(QLabel("喝药间隔"), 2, 4)
        self.spin_potion_every = self._dspin(1.0, 120.0, 6.0)
        self.spin_potion_every.valueChanged.connect(self._save_from_ui)
        g.addWidget(self.spin_potion_every, 2, 5)

        row = QHBoxLayout()
        self.chk_skills = QCheckBox("放技能 1/2")
        self.chk_skills.toggled.connect(self._save_from_ui)
        row.addWidget(self.chk_skills)
        self.chk_pickup_a = QCheckBox("每次平A后拾取")
        self.chk_pickup_a.toggled.connect(self._save_from_ui)
        row.addWidget(self.chk_pickup_a)
        row.addWidget(QLabel("每轮间隔"))
        self.spin_gap = self._dspin(0.0, 3600.0, 5.0)
        self.spin_gap.valueChanged.connect(self._save_from_ui)
        row.addWidget(self.spin_gap)
        row.addStretch(1)
        g.addLayout(row, 3, 0, 1, 6)

        # 疲劳：图标位置 + 提示区域（不标就会沿用主界面框过的区域）
        self.lbl_fatigue_pt = QLabel("未设置")
        self.lbl_fatigue_pt.setObjectName("muted")
        g.addWidget(QLabel("疲劳图标"), 4, 0)
        g.addWidget(self.lbl_fatigue_pt, 4, 1, 1, 2)
        btn_fat_pt = QPushButton("标定")
        btn_fat_pt.setFixedWidth(80)
        btn_fat_pt.clicked.connect(self._pick_fatigue_pt)
        g.addWidget(btn_fat_pt, 4, 3)

        self.lbl_fatigue_roi = QLabel("未设置")
        self.lbl_fatigue_roi.setObjectName("muted")
        g.addWidget(QLabel("疲劳区域"), 5, 0)
        g.addWidget(self.lbl_fatigue_roi, 5, 1, 1, 2)
        row_fat = QHBoxLayout()
        btn_fat_roi = QPushButton("框选")
        btn_fat_roi.setFixedWidth(80)
        btn_fat_roi.clicked.connect(lambda: self._pick_spec("fatigue_roi"))
        btn_fat_test = QPushButton("读一次疲劳")
        btn_fat_test.setToolTip("点一下疲劳图标并 OCR 读一次，日志里能看到读到多少")
        btn_fat_test.clicked.connect(self._test_fatigue)
        row_fat.addWidget(btn_fat_roi)
        row_fat.addWidget(btn_fat_test)
        row_fat.addStretch(1)
        g.addLayout(row_fat, 5, 3)

        row_map = QHBoxLayout()
        self.chk_close_map = QCheckBox("点完地图点位就关地图")
        self.chk_close_map.setChecked(True)
        self.chk_close_map.setToolTip(
            "点完地图上的点以后，按一下关图键（有些游戏不点一下就一直是地图，人物不走）")
        self.chk_close_map.toggled.connect(self._save_from_ui)
        row_map.addWidget(self.chk_close_map)
        row_map.addWidget(QLabel("关图键"))
        self.edit_close_key = QLineEdit("esc")
        self.edit_close_key.setFixedWidth(70)
        self.edit_close_key.editingFinished.connect(self._save_from_ui)
        row_map.addWidget(self.edit_close_key)
        row_map.addWidget(QLabel("开图键"))
        self.edit_map_key = QLineEdit("m")
        self.edit_map_key.setFixedWidth(70)
        self.edit_map_key.editingFinished.connect(self._save_from_ui)
        row_map.addWidget(self.edit_map_key)
        row_map.addStretch(1)
        g.addLayout(row_map, 6, 0, 1, 6)

        g.addWidget(_hint(
            "疲劳图标/区域不标也能跑：会自动沿用主界面「疲劳检测」框过的区域；"
            "但这页标了更准。点「读一次疲劳」可以先确认能不能读到。"
        ), 7, 0, 1, 6)
        return box

    def _build_run(self) -> QWidget:
        box = QGroupBox("运行")
        v = QVBoxLayout(box)
        row = QHBoxLayout()
        self.btn_start = QPushButton("▶ 开始运行")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(lambda: self._start(dry=False))
        self.btn_dry = QPushButton("试跑一轮（不按键）")
        self.btn_dry.clicked.connect(lambda: self._start(dry=True))
        self.btn_test_go = QPushButton("试跑去程（图2→图1）")
        self.btn_test_go.clicked.connect(lambda: self._test_path("go"))
        self.btn_test_back = QPushButton("试跑回程（图1→打怪点）")
        self.btn_test_back.clicked.connect(lambda: self._test_path("back"))
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        self.lbl_state = QLabel("未运行")
        self.lbl_state.setObjectName("muted")
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_dry)
        row.addWidget(self.btn_test_go)
        row.addWidget(self.btn_test_back)
        row.addWidget(self.btn_stop)
        row.addWidget(self.lbl_state, 1)
        v.addLayout(row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(150)
        self.log.setMaximumBlockCount(1200)
        self.log.setUndoRedoEnabled(False)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log.setPlaceholderText("运行日志")
        v.addWidget(self.log)
        return box

    def _dspin(self, low: float, high: float, value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setValue(value)
        spin.setSuffix(" 秒")
        spin.setMinimumWidth(96)
        spin.setMaximumWidth(130)
        return spin

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
        try:
            self._save_timer.start()
        except Exception:
            self._save_now()

    def _save_now(self) -> None:
        try:
            dc.save_profile(self.profile)
        except Exception as exc:
            self._log(f"[警告] 保存失败：{exc}")

    def _load_ui(self) -> None:
        self._loading = True
        try:
            p = self.profile
            farm = p.get("farm") or {}
            loop = p.get("loop") or {}
            fatigue = p.get("fatigue") or {}
            cross = p.get("cross") or {}

            hwnd = self._hwnd()
            self.lbl_window.setText(
                f"已绑定：0x{hwnd:08X}  {win32gui.GetWindowText(hwnd)}"
                if hwnd else "未绑定（请到「单开挂机」页绑定）"
            )
            # 打怪点
            self.combo_farm.blockSignals(True)
            self.combo_farm.clear()
            for i, item in enumerate(cross.get("farm_points") or []):
                name = str(item.get("name") or f"打怪点{i + 1}")
                pt = item.get("pt")
                text = f"{name}（已标定）" if pt else f"{name}（未标定）"
                self.combo_farm.addItem(text, i)
            idx = int(cross.get("farm_index", 0) or 0)
            if 0 <= idx < self.combo_farm.count():
                self.combo_farm.setCurrentIndex(idx)
            self.combo_farm.blockSignals(False)
            point = self._current_point()
            self.spin_farm_travel.blockSignals(True)
            self.spin_farm_travel.setValue(
                float(point.get("travel_wait", 20.0) or 20.0) if point else 20.0)
            self.spin_farm_travel.blockSignals(False)

            # 路径
            for key in ("go_steps", "back_steps"):
                self._refresh_steps(key)

            # BOSS
            self.lbl_boss_pt.setText(
                f"已设置 ({farm.get('boss_pt')[0]:.3f}, "
                f"{farm.get('boss_pt')[1]:.3f})"
                if farm.get("boss_pt") else "未设置"
            )
            present = farm.get("boss_present") or {}
            self.lbl_boss_present.setText(
                "已设置 ✓" + (
                    f"，样本 {len([t for t in (present.get('sigs') or []) if t])} 张"
                    if present.get("sigs") else "")
                if present.get("roi") else "未设置（不框就默认到了）"
            )
            hp = farm.get("boss_hp") or {}
            self.lbl_boss_hp.setText(
                f"已设置（{hp.get('mode', 'ocr')}）" if hp.get("roi") else "未设置（不框就按时间停）"
            )
            # 副本（进本/退本）
            enter = p.get("enter") or {}
            exit_cfg = p.get("exit") or {}
            crop = p.get("cross") or {}
            self.chk_auto_enter.setChecked(bool(crop.get("auto_enter", True)))
            self.chk_auto_exit.setChecked(bool(crop.get("auto_exit", True)))
            self.spin_entry_wait.setValue(float(enter.get("travel_wait", 40.0) or 40.0))
            self.spin_dialog_timeout.setValue(
                float(enter.get("dialog_timeout", 24.0) or 24.0))
            self.spin_load_wait.setValue(float(enter.get("load_wait", 15.0) or 15.0))
            self.spin_exit_settle.setValue(float(exit_cfg.get("settle", 10.0) or 10.0))
            self._refresh_dungeon_labels()

            self.spin_boss_travel.setValue(float(farm.get("travel_wait", 25.0) or 25.0))
            self.spin_boss_seconds.setValue(float(loop.get("boss_seconds", 120.0) or 120.0))
            count = int(loop.get("boss_count", 1) or 1)
            self.chk_clone.setChecked(count > 1)
            self.spin_boss_count.setValue(max(2, min(9, count)))

            # 参数
            f_low = fatigue.get("low")
            f_high = fatigue.get("high")
            if f_low is None or f_high is None:
                th = abs(int(fatigue.get("threshold", 600) or 0))
                f_low = -th if f_low is None else f_low
                f_high = th if f_high is None else f_high
            self.spin_low.setValue(int(f_low))
            self.spin_high.setValue(int(f_high))
            self.spin_fatigue_every.setValue(
                float(loop.get("fatigue_every", 90.0) or 90.0))
            hp_threshold = int(farm.get("hp_threshold", 0) or 0)
            self.chk_potion.setChecked(hp_threshold > 0)
            self.spin_hp.setValue(hp_threshold)
            keys = [str(k) for k in (farm.get("potion_keys") or ["q", "w", "e"])]
            self.edit_keys.setText(",".join(keys))
            self.spin_potion_every.setValue(float(farm.get("potion_every", 6.0) or 6.0))
            self.lbl_hp_roi.setText(
                "已设置 ✓" if farm.get("hp_roi") else "未设置（不框就按固定间隔喝）")
            self.chk_skills.setChecked(bool(farm.get("use_skills", True)))
            self.chk_pickup_a.setChecked(bool(farm.get("pickup_on_a", True)))
            self.spin_gap.setValue(float((p.get("retry") or {}).get("gap", 5.0) or 5.0))

            # 疲劳 / 关地图
            fat_pt = fatigue.get("pt")
            if fat_pt:
                self.lbl_fatigue_pt.setText(
                    f"已设置 ({fat_pt[0]:.3f}, {fat_pt[1]:.3f})")
                self.lbl_fatigue_pt.setStyleSheet(
                    "color: #15803d; font-weight: 600;")
            else:
                self.lbl_fatigue_pt.setText("未设置")
                self.lbl_fatigue_pt.setStyleSheet("color: #444444;")
            fat_roi = fatigue.get("roi")
            self.lbl_fatigue_roi.setText(
                "已设置 ✓" if fat_roi else "未设置（会沿用主界面框过的）")
            self.lbl_fatigue_roi.setStyleSheet(
                "color: #15803d; font-weight: 600;" if fat_roi
                else "color: #444444;")
            self.chk_close_map.setChecked(
                bool(cross.get("close_map_after_click", True)))
            self.edit_close_key.setText(str(cross.get("close_key") or "esc"))
            self.edit_map_key.setText(str(cross.get("map_key") or "m"))
        finally:
            self._loading = False

    def _refresh_dungeon_labels(self) -> None:
        p = self.profile
        enter = p.get("enter") or {}
        exit_cfg = p.get("exit") or {}
        for key, holder, field in (
                ("enter.dungeon_pt", enter, "dungeon_pt"),
                ("enter.confirm_pt", enter, "confirm_pt"),
                ("exit.exit_pt", exit_cfg, "exit_pt"),
                ("exit.confirm_pt", exit_cfg, "confirm_pt")):
            pt = holder.get(field)
            label = self.lbl_pt[key]
            if pt:
                label.setText(f"已设置 ({pt[0]:.3f}, {pt[1]:.3f})")
                label.setStyleSheet("color: #15803d; font-weight: 600;")
            else:
                label.setText("未设置")
                label.setStyleSheet("color: #444444;")
        for kind, spec in (
                ("enter_dialog", enter.get("dialog") or {}),
                ("progress", enter.get("progress") or {}),
                ("exit_dialog", exit_cfg.get("dialog") or {})):
            label = self.lbl_region[kind]
            if (spec or {}).get("roi"):
                label.setText("已设置 ✓")
                label.setStyleSheet("color: #15803d; font-weight: 600;")
            else:
                label.setText("未设置（可选）")
                label.setStyleSheet("color: #444444;")

    # ------------------------------------------------------------------
    # 打怪点
    # ------------------------------------------------------------------
    def _points(self) -> list:
        return self.profile.setdefault("cross", {}).setdefault("farm_points", [])

    def _current_point(self) -> Optional[dict]:
        points = self._points()
        if not points:
            return None
        idx = int((self.profile.get("cross") or {}).get("farm_index", 0) or 0)
        return points[max(0, min(idx, len(points) - 1))]

    def _on_farm_changed(self, index: int) -> None:
        if self._loading:
            return
        crop = self.profile.setdefault("cross", {})
        crop["farm_index"] = max(0, int(index))
        point = self._current_point()
        self.spin_farm_travel.blockSignals(True)
        self.spin_farm_travel.setValue(
            float(point.get("travel_wait", 20.0) or 20.0) if point else 20.0)
        self.spin_farm_travel.blockSignals(False)
        self._save()

    def _on_farm_travel(self, value: float) -> None:
        if self._loading:
            return
        point = self._current_point()
        if point is not None:
            point["travel_wait"] = max(1.0, float(value))
            self._save()

    def _pick_farm_point(self) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有窗口", "请先到「单开挂机」页绑定游戏窗口。")
            return
        point = self._current_point()
        if point is None:
            QMessageBox.warning(self, "没有打怪点", "先点「新增」加一个打怪点。")
            return
        pt = dui.pick_point(
            self, hwnd, "点「图2 打怪点」",
            "这里是图2（打怪地图）：点一下平时打怪的位置。",
            self._map_key(), point.get("pt"),
        )
        if not pt:
            return
        point["pt"] = [round(float(pt[0]), 6), round(float(pt[1]), 6)]
        self._save()
        self._load_ui()
        self._log(f"已标定打怪点「{point.get('name')}」：({pt[0]:.3f}, {pt[1]:.3f})")

    def _add_farm_point(self) -> None:
        name, ok = QInputDialog.getText(
            self, "新增打怪点", "名字（比如：左侧小怪点）",
            text=f"打怪点{len(self._points()) + 1}")
        if not ok or not str(name).strip():
            return
        self._points().append({
            "name": str(name).strip(),
            "pt": None,
            "travel_wait": float(self.spin_farm_travel.value()) or 20.0,
        })
        crop = self.profile.setdefault("cross", {})
        crop["farm_index"] = len(self._points()) - 1
        self._save()
        self._load_ui()

    def _del_farm_point(self) -> None:
        points = self._points()
        if len(points) <= 1:
            QMessageBox.information(self, "至少留一个", "打怪点至少要留一个。")
            return
        idx = int((self.profile.get("cross") or {}).get("farm_index", 0) or 0)
        name = points[idx].get("name")
        if QMessageBox.question(
                self, "删除", f"确定删除「{name}」？") != QMessageBox.StandardButton.Yes:
            return
        points.pop(idx)
        crop = self.profile.setdefault("cross", {})
        crop["farm_index"] = max(0, idx - 1)
        self._save()
        self._load_ui()

    def _rename_farm_point(self) -> None:
        point = self._current_point()
        if point is None:
            return
        name, ok = QInputDialog.getText(
            self, "改名", "新名字", text=str(point.get("name") or ""))
        if not ok or not str(name).strip():
            return
        point["name"] = str(name).strip()
        self._save()
        self._load_ui()

    # ------------------------------------------------------------------
    # 路径步骤
    # ------------------------------------------------------------------
    def _steps(self, key: str) -> list:
        crop = self.profile.setdefault("cross", {})
        return crop.setdefault(key, [])

    def _refresh_steps(self, key: str) -> None:
        lst = self.step_widgets[key]
        lst.blockSignals(True)
        lst.clear()
        for i, st in enumerate(self._steps(key), 1):
            lst.addItem(self._step_text(i, st))
        lst.blockSignals(False)
        self._load_step_editor(key)

    @staticmethod
    def _step_text(index: int, st: dict) -> str:
        wait = float(st.get("wait", 0.0) or 0.0)
        typ = str(st.get("type"))
        if typ == "enter":
            return f"{index}. 进入副本（点副本入口 → 进本弹窗 → 进度条）"
        if typ == "exit":
            return f"{index}. 退出副本（退出副本 → 确认 → 等加载）"
        if typ == "click":
            pt = st.get("pt") or [0.0, 0.0]
            extra = "" if st.get("open_map", True) else "（不开图）"
            return (f"{index}. 点地图点 ({float(pt[0]):.3f}, {float(pt[1]):.3f})"
                    f"{extra}  等 {wait:.1f}s")
        return f"{index}. 按键「{st.get('key')}」  等 {wait:.1f}s"

    def _selected_step(self, key: str):
        row = self.step_widgets[key].currentRow()
        steps = self._steps(key)
        if 0 <= row < len(steps):
            return steps[row]
        return None

    def _load_step_editor(self, key: str) -> None:
        st = self._selected_step(key)
        spin = self.step_wait[key]
        chk = self.step_open_map[key]
        chk_close = self.step_close_map[key]
        spin.blockSignals(True)
        chk.blockSignals(True)
        chk_close.blockSignals(True)
        if st is None:
            spin.setEnabled(False)
            chk.setEnabled(False)
            chk_close.setEnabled(False)
        else:
            spin.setEnabled(True)
            spin.setValue(float(st.get("wait", 0.0) or 0.0))
            is_click = str(st.get("type")) == "click"
            chk.setEnabled(is_click)
            chk.setChecked(bool(st.get("open_map", True)))
            chk_close.setEnabled(is_click)
            chk_close.setChecked(bool(st.get("close_map", True)))
        spin.blockSignals(False)
        chk.blockSignals(False)
        chk_close.blockSignals(False)

    def _on_step_wait(self, key: str, value: float) -> None:
        if self._loading:
            return
        st = self._selected_step(key)
        if st is None:
            return
        st["wait"] = max(0.0, float(value))
        row = self.step_widgets[key].currentRow()
        self.step_widgets[key].item(row).setText(self._step_text(row + 1, st))
        self._save()

    def _on_step_open_map(self, key: str, value: bool) -> None:
        if self._loading:
            return
        st = self._selected_step(key)
        if st is None or str(st.get("type")) != "click":
            return
        st["open_map"] = bool(value)
        row = self.step_widgets[key].currentRow()
        self.step_widgets[key].item(row).setText(self._step_text(row + 1, st))
        self._save()

    def _on_step_close_map(self, key: str, value: bool) -> None:
        if self._loading:
            return
        st = self._selected_step(key)
        if st is None or str(st.get("type")) != "click":
            return
        st["close_map"] = bool(value)
        row = self.step_widgets[key].currentRow()
        self.step_widgets[key].item(row).setText(self._step_text(row + 1, st))
        self._save()

    def _add_click_step(self, key: str) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有窗口", "请先到「单开挂机」页绑定游戏窗口。")
            return
        pt = dui.pick_point(
            self, hwnd, "点一个路径点",
            "先让角色站在这一步该在的地图（图2 或 图1），"
            "再点「确认」→ 程序会按开图键截图，然后在图上点要去的点。",
            self._map_key(), None,
        )
        if not pt:
            return
        self._steps(key).append({
            "type": "click",
            "pt": [round(float(pt[0]), 6), round(float(pt[1]), 6)],
            "wait": 8.0,
            "open_map": True,
            "close_map": True,
        })
        self._save()
        self._refresh_steps(key)
        self.step_widgets[key].setCurrentRow(len(self._steps(key)) - 1)

    def _add_key_step(self, key: str) -> None:
        name, ok = QInputDialog.getText(
            self, "加按键", "要按的键（比如 esc、m、f）", text="esc")
        if not ok or not str(name).strip():
            return
        self._steps(key).append({
            "type": "key", "key": str(name).strip(), "wait": 1.0,
        })
        self._save()
        self._refresh_steps(key)
        self.step_widgets[key].setCurrentRow(len(self._steps(key)) - 1)

    def _add_special_step(self, key: str, typ: str) -> None:
        self._steps(key).append({"type": typ, "wait": 0.0})
        self._save()
        self._refresh_steps(key)
        self.step_widgets[key].setCurrentRow(len(self._steps(key)) - 1)

    def _move_step(self, key: str, delta: int) -> None:
        lst = self.step_widgets[key]
        steps = self._steps(key)
        row = lst.currentRow()
        target = row + delta
        if not (0 <= row < len(steps)) or not (0 <= target < len(steps)):
            return
        steps[row], steps[target] = steps[target], steps[row]
        self._save()
        self._refresh_steps(key)
        lst.setCurrentRow(target)

    def _del_step(self, key: str) -> None:
        lst = self.step_widgets[key]
        steps = self._steps(key)
        row = lst.currentRow()
        if not (0 <= row < len(steps)):
            return
        steps.pop(row)
        self._save()
        self._refresh_steps(key)
        if steps:
            lst.setCurrentRow(min(row, len(steps) - 1))

    # ------------------------------------------------------------------
    # 点位 / 区域
    # ------------------------------------------------------------------
    def _map_key(self) -> str:
        return str((self.profile.get("cross") or {}).get("map_key") or "m")

    def _pick_boss_point(self) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有窗口", "请先到「单开挂机」页绑定游戏窗口。")
            return
        farm = self.profile.setdefault("farm", {})
        pt = dui.pick_point(
            self, hwnd, "点「图1 BOSS 点」",
            "这里是图1（BOSS 地图）：点一下 BOSS 所在的位置。",
            self._map_key(), farm.get("boss_pt"),
        )
        if not pt:
            return
        farm["boss_pt"] = [round(float(pt[0]), 6), round(float(pt[1]), 6)]
        self._save()
        self._load_ui()
        self._log(f"已标定图1 BOSS 点：({pt[0]:.3f}, {pt[1]:.3f})")

    def _pick_dungeon_point(self, key: str) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有窗口", "请先到「单开挂机」页绑定游戏窗口。")
            return
        enter = self.profile.setdefault("enter", {})
        exit_cfg = self.profile.setdefault("exit", {})
        which, field = key.split(".", 1)
        holder = enter if which == "enter" else exit_cfg
        titles = {
            "enter.dungeon_pt": (
                "点「图1 副本入口」",
                "这里是图1 的地图：点副本入口/传送门的位置。"),
            "enter.confirm_pt": (
                "点「进入游戏」按钮",
                "先让进本弹窗弹出来，再点弹窗里的「进入游戏」。"),
            "exit.exit_pt": (
                "点「退出副本」按钮",
                "先把「退出副本」按钮露出来（在副本里），再点它。"),
            "exit.confirm_pt": (
                "点「确认」按钮",
                "先让退出确认弹窗弹出来，再点「确认」。"),
        }
        title, info = titles.get(key, ("点一个位置", ""))
        pt = dui.pick_point(
            self, hwnd, title, info,
            self._map_key() if key == "enter.dungeon_pt" else None,
            holder.get(field),
        )
        if not pt:
            return
        holder[field] = [round(float(pt[0]), 6), round(float(pt[1]), 6)]
        self._save()
        self._load_ui()
        self._log(f"已设置：{title}")

    def _pick_fatigue_pt(self) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有窗口", "请先到「单开挂机」页绑定游戏窗口。")
            return
        fatigue = self.profile.setdefault("fatigue", {})
        pt = dui.pick_point(
            self, hwnd, "点「疲劳图标」",
            "点一下游戏里那个会弹出疲劳值的图标/按钮（点开能看到当前疲劳值）。",
            None, fatigue.get("pt"),
        )
        if not pt:
            return
        fatigue["pt"] = [round(float(pt[0]), 6), round(float(pt[1]), 6)]
        fatigue["enabled"] = True
        self._save()
        self._load_ui()
        self._log(f"已标定疲劳图标：({pt[0]:.3f}, {pt[1]:.3f})")

    def _test_fatigue(self) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有窗口", "请先到「单开挂机」页绑定游戏窗口。")
            return
        if self.thread is not None and self.thread.isRunning():
            self._log("正在运行，先停下再试")
            return
        self._save_from_ui()
        import json as _json
        profile = _json.loads(_json.dumps(self.profile))
        self.thread = CrossThread(hwnd, profile, 0, dry=False,
                                  mode="fatigue", parent=self)
        self.thread.line.connect(self._log)
        self.thread.finished_ok.connect(self._on_finished)
        self.thread.finished.connect(self._on_thread_finished)
        self._set_running(True, "读疲劳中…")
        self._log("读一次疲劳：点住图标 → 等提示框 → OCR（最多 3 秒 × 2 次）")
        self.thread.start()

    def _test_path(self, which: str) -> None:
        if self.thread is not None and self.thread.isRunning():
            self._log("已经在运行了（先点「停止」）")
            return
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有窗口", "请先到「单开挂机」页绑定游戏窗口。")
            return
        self._save_from_ui()
        import json as _json
        profile = _json.loads(_json.dumps(self.profile))
        self.thread = CrossThread(hwnd, profile, 1, dry=False,
                                  mode=("back" if which == "back" else "go"),
                                  parent=self)
        self.thread.line.connect(self._log)
        self.thread.finished_ok.connect(self._on_finished)
        self.thread.finished.connect(self._on_thread_finished)
        self._set_running(
            True, "试跑去程中…" if which != "back" else "试跑回程中…")
        self._log("试跑" + ("去程（图2 → 图1）" if which != "back"
                            else "回程（图1 → 打怪点）")
                  + "：只走路径，不打怪")
        self.thread.start()

    def _pick_spec(self, kind: str) -> None:
        """框一个区域：present=图1 BOSS 身形，boss_hp=图1 BOSS 血量，player_hp=玩家血量。"""
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有窗口", "请先到「单开挂机」页绑定游戏窗口。")
            return
        farm = self.profile.setdefault("farm", {})
        enter = self.profile.setdefault("enter", {})
        exit_cfg = self.profile.setdefault("exit", {})
        fatigue = self.profile.setdefault("fatigue", {})
        if kind == "fatigue_roi":
            spec = {"roi": fatigue.get("roi")}
            title = "框住疲劳提示区域"
            info = ("点一下疲劳图标会弹出「当前疲劳值:xxx/xxx」，框住这段文字"
                    "（别框到会变的东西）")
        elif kind == "enter_dialog":
            spec = enter.get("dialog") or {}
            title = "框住「进入游戏」弹窗"
            info = "先让进本弹窗弹出来再截图，框住弹窗里固定不变的部分"
        elif kind == "progress":
            spec = enter.get("progress") or {}
            title = "框住加载进度条"
            info = "进本读条：框住整条进度条（左边起点到右边终点）"
        elif kind == "exit_dialog":
            spec = exit_cfg.get("dialog") or {}
            title = "框住「退出确认」弹窗"
            info = "先让退出确认弹窗弹出来再截图，框住固定不变的部分"
        elif kind == "present":
            spec = farm.get("boss_present") or {}
            title = "框住图1 的 BOSS 身形"
            info = ("框住 BOSS 本体（别框会动的特效）；"
                    "到位后画面静止、相似度够高才开始打")
        elif kind == "boss_hp":
            spec = farm.get("boss_hp") or {}
            title = "框住图1 的 BOSS 血量（名字/数字）"
            info = "框住 BOSS 头顶的血条/名字/数字；连续几次读不到就判定打完"
        else:
            spec = {"roi": farm.get("hp_roi")}
            title = "框住玩家血量数字"
            info = "框住自己的血量数字（如 1725/1780）"
        roi_norm = self._pick_region(hwnd, spec.get("roi"), title, info)
        if roi_norm is None:
            return
        if kind == "fatigue_roi":
            fatigue["roi"] = roi_norm
            fatigue["enabled"] = True
        elif kind in ("enter_dialog", "exit_dialog"):
            sig = df.signature(df.crop_norm(self._last_pick_frame, roi_norm), 12)
            holder = enter if kind == "enter_dialog" else exit_cfg
            holder["dialog"] = {
                "roi": roi_norm, "grid": 12, "sig": sig, "sim": 0.90,
            }
        elif kind == "progress":
            enter["progress"] = {
                "roi": roi_norm, "min_fill": 0.90, "samples": 3,
                "interval": 0.25,
            }
        elif kind == "present":
            sig = df.signature(df.crop_norm(self._last_pick_frame, roi_norm), 12)
            farm["boss_present"] = {
                "roi": roi_norm, "grid": 12, "sig": sig, "sigs": [sig],
                "sim": 0.60, "min_sim": 0.75, "search": 1.6, "tol": 30.0,
                "still_seconds": 0.8, "still_timeout": 12.0, "still_diff": 0.05,
            }
        elif kind == "boss_hp":
            farm["boss_hp"] = {
                "roi": roi_norm, "mode": "ocr", "kill_hp": 0, "miss": 3,
                "ocr_every": 3.0, "grace": 6.0, "min_fill": 0.15,
            }
        else:
            farm["hp_roi"] = roi_norm
        self._save()
        self._load_ui()
        self._log(f"已设置：{title}")

    def _pick_region(self, hwnd: int, current_roi, title: str, info: str):
        """截图 + 框选，返回归一化 roi（不自动存盘，由调用方写进配置）。"""
        frame = app.capture_client_bgra(hwnd)[0]
        if frame is None:
            QMessageBox.warning(self, "截图失败",
                                "抓不到游戏画面（窗口是否最小化？）")
            return None
        cache = {"frame": frame}
        image = dui.qimage_from_frame(frame)
        current = None
        if current_roi:
            w, h = image.width(), image.height()
            current = (
                int(current_roi[0] * w), int(current_roi[1] * h),
                max(5, int(current_roi[2] * w)), max(3, int(current_roi[3] * h)),
            )

        def recapture():
            cap = app.capture_client_bgra(hwnd)[0]
            if cap is None:
                return None
            cache["frame"] = cap
            return dui.qimage_from_frame(cap)

        dialog = app.RegionSelectDialog(
            image, current, recapture, self, title,
            info + chr(10) + "（框完点「保存区域」；比目标松一圈，四周留点背景）",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        rect = dialog.selected_roi()
        if rect is None:
            return None
        f = cache["frame"]
        self._last_pick_frame = f
        w, h = f[0], f[1]
        return [
            round(rect[0] / w, 6), round(rect[1] / h, 6),
            round(rect[2] / w, 6), round(rect[3] / h, 6),
        ]

    def _capture_samples(self) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有窗口", "请先到「单开挂机」页绑定游戏窗口。")
            return
        farm = self.profile.setdefault("farm", {})
        spec = farm.get("boss_present") or {}
        roi = spec.get("roi")
        if not roi:
            QMessageBox.information(self, "先框选", "请先「框选」BOSS 身形。")
            return
        sigs = [t for t in (spec.get("sigs") or []) if t]
        added = 0
        self._log("采 BOSS 样本：连拍 10 张（每 0.3 秒一张）…")
        for _ in range(10):
            frame = app.capture_client_bgra(hwnd)[0]
            if frame is None:
                continue
            try:
                sig = df.signature(df.crop_norm(frame, roi), 12)
            except Exception:
                continue
            if not sig:
                continue
            if all(df.similarity(sig, old, 30.0) < 0.98 for old in sigs):
                sigs.append(sig)
                added += 1
            time.sleep(0.3)
        spec["sigs"] = sigs
        spec["sig"] = sigs[0] if sigs else []
        farm["boss_present"] = spec
        self._save()
        self._load_ui()
        self._log(f"采样完成：新增 {added} 张，共 {len(sigs)} 张")

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------
    def _save_from_ui(self, *_args) -> None:
        if self._loading:
            return
        p = self.profile
        farm = p.setdefault("farm", {})
        loop = p.setdefault("loop", {})
        fatigue = p.setdefault("fatigue", {})
        farm["travel_wait"] = float(self.spin_boss_travel.value())
        loop["boss_seconds"] = float(self.spin_boss_seconds.value())
        loop["boss_count"] = (int(self.spin_boss_count.value())
                              if self.chk_clone.isChecked() else 1)
        loop["fatigue_every"] = float(self.spin_fatigue_every.value())
        fatigue["low"] = int(self.spin_low.value())
        fatigue["high"] = int(self.spin_high.value())
        fatigue["enabled"] = True
        farm["hp_threshold"] = (int(self.spin_hp.value())
                                if self.chk_potion.isChecked() else 0)
        raw = (self.edit_keys.text() or "").replace("，", ",")
        raw = raw.replace(" ", ",").replace("、", ",")
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        farm["potion_keys"] = keys or ["q", "w", "e"]
        farm["potion_every"] = float(self.spin_potion_every.value())
        farm["use_skills"] = bool(self.chk_skills.isChecked())
        farm["pickup_on_a"] = bool(self.chk_pickup_a.isChecked())
        p.setdefault("retry", {})["gap"] = float(self.spin_gap.value())
        enter = p.setdefault("enter", {})
        enter["travel_wait"] = float(self.spin_entry_wait.value())
        enter["dialog_timeout"] = float(self.spin_dialog_timeout.value())
        enter["load_wait"] = float(self.spin_load_wait.value())
        p.setdefault("exit", {})["settle"] = float(self.spin_exit_settle.value())
        cross = p.setdefault("cross", {})
        cross["auto_enter"] = bool(self.chk_auto_enter.isChecked())
        cross["auto_exit"] = bool(self.chk_auto_exit.isChecked())
        cross["close_map_after_click"] = bool(self.chk_close_map.isChecked())
        cross["close_key"] = self.edit_close_key.text().strip() or "esc"
        cross["map_key"] = self.edit_map_key.text().strip() or "m"
        self._save()

    def _set_running(self, running: bool, state: str = "") -> None:
        self.btn_start.setEnabled(not running)
        self.btn_dry.setEnabled(not running)
        self.btn_test_go.setEnabled(not running)
        self.btn_test_back.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        if state:
            self.lbl_state.setText(state)

    def _start(self, dry: bool = False) -> None:
        if self.thread is not None and self.thread.isRunning():
            self._log("已经在运行了（先点「停止」）")
            return
        hwnd = self._hwnd()
        if not hwnd:
            QMessageBox.warning(self, "没有窗口", "请先到「单开挂机」页绑定游戏窗口。")
            return
        self._save_from_ui()
        missing = dc.missing_items(self.profile)
        if missing:
            QMessageBox.warning(
                self, "还没标定完",
                "下面这些还没设置，跑不起来：\n\n" + "\n".join("· " + m for m in missing),
            )
            return
        import json as _json
        profile = _json.loads(_json.dumps(self.profile))
        self.thread = CrossThread(hwnd, profile, None if not dry else 1,
                                  dry=dry, parent=self)
        self.thread.line.connect(self._log)
        self.thread.finished_ok.connect(self._on_finished)
        self.thread.finished.connect(self._on_thread_finished)
        self._set_running(True, "运行中…（试跑，不按键）" if dry else "运行中…")
        point = self._current_point() or {}
        self._log(f"=== 跨图打BOSS 开始：图2「{point.get('name')}」挂机 → "
                  f"图1 打BOSS → 回图2 ===")
        self.thread.start()

    def _stop(self) -> None:
        thread = self.thread
        if thread is None:
            self._log("当前没有在跑")
            return
        thread.stop()
        self.lbl_state.setText("正在停止…")
        self._log("已请求停止（最多 1 秒内生效）…")

    def _on_finished(self, ok: int, fail: int) -> None:
        self._log(f"结束：成功 {ok} 轮 / 失败 {fail} 轮")

    def _on_thread_finished(self) -> None:
        thread = self.thread
        self.thread = None
        if thread is not None:
            thread.deleteLater()
        self._set_running(False, "未运行")
