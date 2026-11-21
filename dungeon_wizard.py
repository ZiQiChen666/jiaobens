#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""副本向导（三步流程版）

    ① 进本：地图1 点副本入口 → 等寻路 → 识别弹窗 → 点「进入游戏」→ 等加载
    ② 挂机：地图2 点 BOSS 点 → 等寻路 → 挂机 N 秒（喝药/拾取）
    ③ 退出：点「退出副本」→ 点「确认」→ 等加载 → 检测疲劳，不足就普通打怪

只问这些，问完 compose_profile() 自动拼成配置。
"""

from __future__ import annotations

import dungeon_flow as df
import dungeon_ui as dui

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


def compose_profile(base: dict, answers: dict) -> tuple[dict, list[str]]:
    """把向导答案拼成完整配置 + 步骤表。"""
    p = df._merge(df.DEFAULT_PROFILE, base or {})
    keys = p.setdefault("keys", {})
    keys["open_map"] = (answers.get("open_map") or keys.get("open_map") or "m").strip()
    keys["pickup"] = (answers.get("pickup") or keys.get("pickup") or "space").strip()

    enter = p.setdefault("enter", {})
    if answers.get("entry_pt"):
        enter["dungeon_pt"] = [round(float(v), 5) for v in answers["entry_pt"]]
    enter["travel_wait"] = float(answers.get("entry_wait") or 40.0)
    enter["dialog_timeout"] = float(answers.get("dialog_timeout") or 20.0)
    enter["retry"] = max(1, int(answers.get("retry") or 3))
    if answers.get("confirm_pt"):
        enter["confirm_pt"] = [round(float(v), 5) for v in answers["confirm_pt"]]
    enter["load_wait"] = float(answers.get("load_wait") or 10.0)
    if answers.get("entry_pt"):
        enter["entry_pt"] = [round(float(v), 5) for v in answers["entry_pt"]]

    farm = p.setdefault("farm", {})
    if answers.get("boss_pt"):
        farm["boss_pt"] = [round(float(v), 5) for v in answers["boss_pt"]]
    farm["travel_wait"] = float(answers.get("boss_wait") or 30.0)
    farm["seconds"] = max(5.0, float(answers.get("seconds") or 30.0))
    farm["hp_threshold"] = max(0, int(answers.get("hp_threshold") or 0))
    farm["potion_every"] = max(1.0, float(answers.get("potion_every") or 6.0))
    farm["potion_timer_enabled"] = bool(answers.get("potion_timer_enabled", False))
    farm["potion_timer_every"] = max(
        5.0, float(answers.get("potion_timer_every") or 60.0))
    farm["pickup_every"] = max(0.0, float(answers.get("pickup_every") or 8.0))
    farm["use_skills"] = bool(answers.get("use_skills", True))
    farm["pickup_on_a"] = bool(answers.get("pickup_on_a", True))
    p["action_gap"] = max(0.0, float(answers.get("action_gap") or 0.0))

    loop = p.setdefault("loop", {})
    loop["boss_seconds"] = max(
        5.0, min(1000.0, float(answers.get("seconds") or 120.0))
    )
    loop["fatigue_every"] = max(0.0, float(answers.get("fatigue_every") or 0.0))

    farm["farm_pt"] = (
        [round(float(v), 5) for v in answers["farm_pt"]]
        if answers.get("farm_pt") else farm.get("farm_pt")
    )

    mount = p.setdefault("mount", {})
    mount["enabled"] = bool(answers.get("mount_enabled", True))
    mount["key"] = (answers.get("mount_key") or mount.get("key") or "s").strip()
    mount["mount_text"] = (answers.get("mount_text")
                           or mount.get("mount_text") or "骑乘").strip()
    mount["rest_text"] = (answers.get("rest_text")
                          or mount.get("rest_text") or "休息").strip()
    if answers.get("mount_pt"):
        mount["mount_pt"] = [round(float(v), 5) for v in answers["mount_pt"]]

    exit_cfg = p.setdefault("exit", {})
    if answers.get("exit_pt"):
        exit_cfg["exit_pt"] = [round(float(v), 5) for v in answers["exit_pt"]]
    if answers.get("exit_confirm_pt"):
        exit_cfg["confirm_pt"] = [round(float(v), 5) for v in answers["exit_confirm_pt"]]
    exit_cfg["settle"] = float(answers.get("exit_settle") or 10.0)

    fatigue = p.setdefault("fatigue", {})
    fatigue["enabled"] = bool(answers.get("fatigue_enabled", True))
    try:
        low = int(answers.get("fatigue_low", -600))
    except (TypeError, ValueError):
        low = -600
    try:
        high = int(answers.get("fatigue_high", 600))
    except (TypeError, ValueError):
        high = 600
    if low >= high:
        low, high = -abs(low or 600), abs(high or 600)
    fatigue["low"] = low
    fatigue["high"] = high
    fatigue.pop("threshold", None)
    if answers.get("fatigue_pt"):
        fatigue["pt"] = [round(float(v), 5) for v in answers["fatigue_pt"]]

    p.setdefault("retry", {})["gap"] = float(answers.get("gap") or 8.0)

    plan: list[str] = []
    if str(p.get("input_mode", "foreground")) != "background":
        plan.append("前置：点开始后先把游戏窗口切到前台，用真实鼠标点击（运行期间别动鼠标）")
    else:
        plan.append("前置：后台模式，只发 PostMessage（不抢鼠标；部分游戏不认）")
    if enter.get("dungeon_pt"):
        plan.append(
            f"① 按「{keys['open_map']}」开地图1 → 点副本入口 → 等 {enter['travel_wait']:.0f}s "
            f"→ 等弹窗(最多 {enter['dialog_timeout']:.0f}s)"
            + (f"，没识别到就重新开图重点，最多 {enter['retry']} 次" if enter.get("retry") else "")
            + (" → 点「进入游戏」" if enter.get("confirm_pt") else "")
            + f" → 等加载 {enter['load_wait']:.0f}s"
        )
    else:
        plan.append("① 还没标副本入口：进本这一步会跳过")
    hp = farm.get("hp_threshold") or 0
    if mount.get("enabled", True):
        plan.append(
            f"每次寻路前按「{mount.get('key', 's')}」检查骑乘：弹窗出现 → 看状态 → "
            "没骑就点「骑乘」→ 关弹窗"
        )
    plan.append(
        f"打怪点按疲劳分流：疲劳 < 下限 {fatigue['low']} → 去打BOSS；"
        f"疲劳 > 上限 {fatigue['high']} → 回打怪点打小怪（后台按键，不占窗口）；"
        f"不设挂机时长，每 {loop['fatigue_every']:.0f}s 查一次疲劳"
    )
    plan.append(
        f"② 开地图2 → 点 BOSS 点 → 等 {farm['travel_wait']:.0f}s → "
        f"打BOSS（上限 {loop['boss_seconds']:.0f}s，血条消失就结束）"
        f"（喝药 {farm['potion_every']:.0f}s"
        + (f"，血量 < {hp} 才喝" if hp and farm.get("hp_roi") else "")
        + f"；每 {farm['pickup_every']:.0f}s 拾取一次）"
    )
    plan.append(
        "③ 点「退出副本」→ 等「退出确认弹窗」(最多 "
        f"{float((p.get('exit') or {}).get('dialog_timeout', 10.0)):.0f}s)"
        " → 点「确认」"
        + f" → 等加载 {exit_cfg['settle']:.0f}s"
        + f" → 等 {p['retry']['gap']:.0f}s 下一轮"
    )
    missing = []
    if not enter.get("dungeon_pt"):
        missing.append("地图1 的副本入口点")
    if not enter.get("confirm_pt"):
        missing.append("弹窗里的「进入游戏」按钮")
    if not enter.get("dialog", {}).get("roi"):
        missing.append("进本弹窗识别区域（没有它就只能盲点确认）")
    if not farm.get("boss_pt"):
        missing.append("地图2 的 BOSS 点")
    if bool(loop.get("link_fatigue", True)) and not farm.get("farm_pt"):
        missing.append("地图2 的打怪点（疲劳联动挂机用）")
    if not exit_cfg.get("exit_pt"):
        missing.append("「退出副本」按钮")
    if fatigue.get("enabled") and not (fatigue.get("pt") or fatigue.get("roi")):
        missing.append("疲劳图标点位（否则检测不了疲劳）")
    if missing:
        plan.append("还差：" + "、".join(missing))
    return p, plan


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    return lab


def _title(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet("font-size: 15px; font-weight: 700; color: #111111;")
    return lab


class DungeonWizard(QDialog):
    def __init__(self, hwnd: int, profile: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("副本向导（三步流程）")
        self.setModal(True)
        self.resize(920, 660)
        self.setStyleSheet(dui.LIGHT_QSS)
        self.hwnd = int(hwnd)
        self.profile = df._merge(df.DEFAULT_PROFILE, profile or {})
        self.plan_lines: list[str] = []

        enter = self.profile.get("enter") or {}
        farm = self.profile.get("farm") or {}
        exit_cfg = self.profile.get("exit") or {}
        fatigue = self.profile.get("fatigue") or {}
        keys = self.profile.get("keys") or {}
        self.answers = {
            "open_map": keys.get("open_map", "m"),
            "pickup": keys.get("pickup", "space"),
            "entry_pt": enter.get("dungeon_pt"),
            "entry_wait": float(enter.get("travel_wait", 40.0)),
            "dialog_timeout": float(enter.get("dialog_timeout", 20.0)),
            "retry": int(enter.get("retry", 3) or 3),
            "entry_pt": enter.get("entry_pt"),
            "confirm_pt": enter.get("confirm_pt"),
            "load_wait": float(enter.get("load_wait", 10.0)),
            "boss_pt": farm.get("boss_pt"),
            "boss_wait": float(farm.get("travel_wait", 30.0)),
            "seconds": float((self.profile.get("loop") or {}).get(
                "boss_seconds", farm.get("seconds", 120.0)) or 120.0),
            "hp_threshold": int(farm.get("hp_threshold", 0) or 0),
            "potion_every": float(farm.get("potion_every", 6.0)),
            "potion_timer_enabled": bool(
                farm.get("potion_timer_enabled", False)),
            "potion_timer_every": float(
                farm.get("potion_timer_every", 60.0) or 60.0),
            "pickup_every": float(farm.get("pickup_every", 8.0)),
            "use_skills": bool(farm.get("use_skills", True)),
            "fatigue_enabled": bool(fatigue.get("enabled", True)),
            "fatigue_low": int(fatigue.get("low", -abs(
                int(fatigue.get("threshold", 600) or 0)))),
            "fatigue_high": int(fatigue.get("high", abs(
                int(fatigue.get("threshold", 600) or 0)))),
            "fatigue_pt": fatigue.get("pt"),
            "fatigue_every": float((self.profile.get("loop") or {}).get("fatigue_every", 60.0)),
            "farm_pt": (self.profile.get("farm") or {}).get("farm_pt"),
            "pickup_on_a": bool((self.profile.get("farm") or {}).get("pickup_on_a", True)),
            "action_gap": float(self.profile.get("action_gap", 1.0)),
            "mount_enabled": bool((self.profile.get("mount") or {}).get("enabled", True)),
            "mount_key": (self.profile.get("mount") or {}).get("key", "s"),
            "mount_text": (self.profile.get("mount") or {}).get("mount_text", "骑乘"),
            "rest_text": (self.profile.get("mount") or {}).get("rest_text", "休息"),
            "mount_pt": (self.profile.get("mount") or {}).get("mount_pt"),
            "exit_pt": exit_cfg.get("exit_pt"),
            "exit_confirm_pt": exit_cfg.get("confirm_pt"),
            "exit_settle": float(exit_cfg.get("settle", 10.0)),
            "gap": float((self.profile.get("retry") or {}).get("gap", 8.0)),
        }
        self._pages: list[QWidget] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 12)
        self.header = QLabel()
        root.addWidget(self.header)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        btn_prev = QPushButton("上一步")
        btn_prev.clicked.connect(self._prev)
        self.btn_next = QPushButton("下一步")
        self.btn_next.setDefault(True)
        self.btn_next.setObjectName("primary")
        self.btn_next.clicked.connect(self._next)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(btn_prev)
        row.addStretch(1)
        row.addWidget(btn_cancel)
        row.addWidget(self.btn_next)
        root.addLayout(row)

        for page in (
            self._page_intro(),
            self._page_entry(),
            self._page_dialog(),
            self._page_boss(),
            self._page_farm(),
            self._page_fatigue(),
            self._page_mount(),
            self._page_exit(),
            self._page_summary(),
        ):
            self.stack.addWidget(page)
            self._pages.append(page)
        self._go(0)

    # ------------------------------------------------------------------
    @staticmethod
    def _pt_text(pt) -> str:
        if not pt:
            return "未设置"
        try:
            return f"已设置 ({float(pt[0]):.3f}, {float(pt[1]):.3f})"
        except (TypeError, ValueError, IndexError):
            return "未设置"

    def _go(self, index: int) -> None:
        self.index = max(0, min(index, len(self._pages) - 1))
        self.stack.setCurrentIndex(self.index)
        self.header.setText(f"第 {self.index + 1} / {len(self._pages)} 步")
        self.btn_next.setText("完成" if self.index == len(self._pages) - 1 else "下一步")
        if self.index == len(self._pages) - 1:
            self._enter_summary()

    def _prev(self) -> None:
        if self.index > 0:
            self._collect()
            self._go(self.index - 1)

    def _collect(self) -> None:
        self.answers["entry_wait"] = float(self.spin_entry_wait.value())
        self.answers["dialog_timeout"] = float(self.spin_dialog_timeout.value())
        self.answers["retry"] = int(self.spin_retry.value())
        self.answers["load_wait"] = float(self.spin_load_wait.value())
        self.answers["boss_wait"] = float(self.spin_boss_wait.value())
        self.answers["seconds"] = float(self.spin_seconds.value())
        self.answers["potion_every"] = float(self.spin_potion.value())
        self.answers["pickup_every"] = float(self.spin_pickup.value())
        self.answers["use_skills"] = bool(self.chk_skills.isChecked())
        self.answers["hp_threshold"] = int(self.spin_hp.value())
        self.answers["fatigue_enabled"] = bool(self.chk_fatigue.isChecked())
        self.answers["fatigue_low"] = int(self.spin_fatigue_low.value())
        self.answers["fatigue_high"] = int(self.spin_fatigue_high.value())
        self.answers["fatigue_every"] = float(self.spin_fatigue_every.value())
        self.answers["pickup_on_a"] = bool(self.chk_pickup_on_a.isChecked())
        self.answers["action_gap"] = float(self.spin_action_gap.value())
        self.answers["potion_timer_enabled"] = bool(
            self.chk_potion_timer.isChecked())
        self.answers["potion_timer_every"] = float(
            self.spin_potion_timer.value())
        self.answers["mount_enabled"] = bool(self.chk_mount.isChecked())
        self.answers["mount_key"] = self.edit_mount_key.text().strip() or "s"
        self.answers["mount_text"] = self.edit_mount_text.text().strip() or "骑乘"
        self.answers["rest_text"] = self.edit_rest_text.text().strip() or "休息"
        self.answers["exit_settle"] = float(self.spin_exit_settle.value())
        self.answers["gap"] = float(self.spin_gap.value())

    def _next(self) -> None:
        self._collect()
        if self.index >= len(self._pages) - 1:
            self.profile, self.plan_lines = compose_profile(self.profile, self.answers)
            try:
                # 有命名方案就写进当前方案，否则写默认标定（不然向导白标一遍）
                name = df.active_name()
                if name:
                    df.save_dungeon(name, self.profile)
                else:
                    df.save_profile(self.profile)
            except Exception as exc:
                QMessageBox.warning(self, "保存失败", str(exc))
                return
            self.accept()
            return
        self._go(self.index + 1)

    # ------------------------------------------------------------------
    def _pick_point(self, key: str, title: str, info: str, open_map: bool) -> None:
        pt = dui.pick_point(
            self, self.hwnd, title, info,
            (self.answers.get("open_map") or "m") if open_map else None,
            self.answers.get(key),
        )
        if not pt:
            return
        self.answers[key] = pt
        QMessageBox.information(self, "已记录", f"位置：{pt}")

    # ------------------------------------------------------------------
    def _page_intro(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(_title("三步流程，和你在游戏里的操作一一对应"))
        lay.addWidget(_hint(
            "① 进本：地图1 点副本入口 → 角色自动跑过去 → 弹出进本窗口 → "
            "程序识别到弹窗后点「进入游戏」→ 等加载。"
        ))
        lay.addWidget(_hint(
            "② 挂机：进副本后开地图2 → 点 BOSS 点 → 角色自动跑过去 → "
            "挂机 N 秒（平A + 技能 + 血量低喝药 + 定时拾取）。"
        ))
        lay.addWidget(_hint(
            "③ 退出：时间到 → 点「退出副本」→ 点「确认」→ 等加载 → "
            "疲劳 < 下限 去打BOSS，> 上限 回打怪点打小怪，循环往复。"
        ))
        lay.addWidget(_hint(
            "所有时间都能改：走进副本 40s、等弹窗 20s、进本加载 10s、"
            "跑向 BOSS 30s、打 BOSS 30s、退出加载 10s。"
        ))
        lay.addStretch(1)
        return page

    def _page_entry(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(_title("第 1 步：地图1 的「副本入口」"))
        lay.addWidget(_hint(
            "点下面的按钮：会先按一次开图键 → 截图 → 让你在图上点副本入口。"
        ))
        self.lbl_entry = QLabel(self._pt_text(self.answers.get("entry_pt")))
        lay.addWidget(self.lbl_entry)
        btn = QPushButton("截图并在图上点副本入口")
        btn.clicked.connect(self._pick_entry)
        lay.addWidget(btn)
        row = QHBoxLayout()
        row.addWidget(QLabel("点完后等寻路"))
        self.spin_entry_wait = self._dspin(1.0, 600.0, self.answers["entry_wait"])
        row.addWidget(self.spin_entry_wait)
        row.addWidget(_hint("默认 40 秒"), 1)
        lay.addLayout(row)
        lay.addStretch(1)
        return page

    @staticmethod
    def _dspin(low: float, high: float, value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setSuffix(" 秒")
        spin.setValue(float(value))
        spin.setFixedWidth(110)
        return spin

    def _pick_entry(self) -> None:
        self._pick_point("entry_pt", "点副本入口",
                         "这里显示的是地图1。点一下「副本入口」图标的位置。", True)
        self.lbl_entry.setText(self._pt_text(self.answers.get("entry_pt")))

    def _page_dialog(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(_title("第 2 步：进本弹窗（识别到才点）"))
        lay.addWidget(_hint(
            "先在游戏里正常点一次副本入口，让那个「进入游戏 / 确定」窗口弹出来，"
            "再回来点下面的按钮截图（弹窗留在屏幕上就行，截图是后台抓的）。"
        ))
        row = QHBoxLayout()
        btn_roi = QPushButton("① 框住弹窗区域（识别用）")
        btn_roi.clicked.connect(self._pick_dialog_roi)
        self.lbl_dialog = QLabel("未设置")
        row.addWidget(btn_roi)
        row.addWidget(self.lbl_dialog, 1)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        btn_ok = QPushButton("② 点「进入游戏」按钮的位置")
        btn_ok.clicked.connect(self._pick_confirm)
        self.lbl_confirm = QLabel(self._pt_text(self.answers.get("confirm_pt")))
        row2.addWidget(btn_ok)
        row2.addWidget(self.lbl_confirm, 1)
        lay.addLayout(row2)

        row2b = QHBoxLayout()
        btn_progress = QPushButton("③ 框住「加载进度条」区域")
        btn_progress.setToolTip("进本读条那条进度条；程序连拍多帧量填充比例，≥90% 才算加载完")
        btn_progress.clicked.connect(self._pick_progress)
        self.lbl_progress = QLabel("未设置")
        row2b.addWidget(btn_progress)
        row2b.addWidget(self.lbl_progress, 1)
        lay.addLayout(row2b)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("等弹窗最多"))
        self.spin_dialog_timeout = self._dspin(3.0, 300.0, self.answers["dialog_timeout"])
        row3.addWidget(self.spin_dialog_timeout)
        row3.addWidget(QLabel("没识别到就重试"))
        self.spin_retry = QSpinBox()
        self.spin_retry.setRange(1, 10)
        self.spin_retry.setValue(int(self.answers["retry"]))
        self.spin_retry.setFixedWidth(70)
        row3.addWidget(self.spin_retry)
        row3.addWidget(QLabel("次"))
        row3.addWidget(QLabel("进本加载"))
        self.spin_load_wait = self._dspin(1.0, 300.0, self.answers["load_wait"])
        row3.addWidget(self.spin_load_wait)
        row3.addStretch(1)
        lay.addLayout(row3)
        self._refresh_dialog_state()
        self._refresh_progress_state()
        lay.addStretch(1)
        return page

    def _refresh_dialog_state(self) -> None:
        spec = (self.profile.get("enter") or {}).get("dialog") or {}
        if spec.get("roi"):
            self.lbl_dialog.setText("已设置 \u2713")
            self.lbl_dialog.setStyleSheet("color: #15803d; font-weight: 600;")
        else:
            self.lbl_dialog.setText("未设置")
            self.lbl_dialog.setStyleSheet("color: #444444;")

    def _pick_dialog_roi(self) -> None:
        if dui.pick_region(self, self.hwnd, self.profile, "enter.dialog",
                           "进本弹窗区域",
                           "框住弹窗里固定不变的部分（标题/按钮区），别框会闪的动画"):
            self._refresh_dialog_state()

    def _pick_progress(self) -> None:
        if dui.pick_region(self, self.hwnd, self.profile, "enter.progress",
                           "加载进度条区域",
                           "框住进本时的读条（整条，从起点到终点）"):
            self._refresh_progress_state()

    def _refresh_progress_state(self) -> None:
        spec = (self.profile.get("enter") or {}).get("progress") or {}
        if (spec or {}).get("roi"):
            self.lbl_progress.setText("已设置 ✓")
            self.lbl_progress.setStyleSheet("color: #15803d; font-weight: 600;")
        else:
            self.lbl_progress.setText("未设置")
            self.lbl_progress.setStyleSheet("color: #444444;")

    def _pick_confirm(self) -> None:
        self._pick_point("confirm_pt", "点「进入游戏」按钮",
                         "这里显示的是当前画面。点一下弹窗里的「进入游戏 / 确定」按钮。",
                         False)
        self.lbl_confirm.setText(self._pt_text(self.answers.get("confirm_pt")))

    def _page_boss(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(_title("第 3 步：地图2 的「打怪点」和「BOSS 点」"))
        lay.addWidget(_hint(
            "进到副本里以后打开副本地图（地图2）：先点「打怪点」——"
            "程序会先跑到那里读疲劳值，再决定是打 BOSS 还是打怪；"
            "然后再点 BOSS 所在的位置。"
        ))
        row_farm = QHBoxLayout()
        btn_farm = QPushButton("① 截图并点「打怪点」")
        btn_farm.setToolTip(
            "地图2 上平时打小怪的位置；疲劳 < 下限 才从这里去打 BOSS"
        )
        btn_farm.clicked.connect(self._pick_farm_pt)
        self.lbl_farm = QLabel(self._pt_text(self.answers.get("farm_pt")))
        row_farm.addWidget(btn_farm)
        row_farm.addWidget(self.lbl_farm, 1)
        lay.addLayout(row_farm)
        row_boss = QHBoxLayout()
        btn = QPushButton("② 截图并点「副本内 BOSS 点」")
        btn.clicked.connect(self._pick_boss)
        self.lbl_boss = QLabel(self._pt_text(self.answers.get("boss_pt")))
        row_boss.addWidget(btn)
        row_boss.addWidget(self.lbl_boss, 1)
        lay.addLayout(row_boss)
        row_flag = QHBoxLayout()
        btn_bossflag = QPushButton("③ 框住「BOSS 身形」")
        btn_bossflag.setToolTip("框住 BOSS 本体；相似度高才开始打，不够就重新寻路")
        btn_bossflag.clicked.connect(lambda: self._pick_farm_roi("boss_present"))
        self.lbl_bossflag = QLabel("未设置")
        row_flag.addWidget(btn_bossflag)
        row_flag.addWidget(self.lbl_bossflag, 1)
        lay.addLayout(row_flag)

        row_finish = QHBoxLayout()
        btn_finish = QPushButton("④ 框住 BOSS 血量图标")
        btn_finish.setToolTip("框住 BOSS 头顶血条，或那个「打 BOSS 才出现」的图标；"
                             "开打时拍下它的样子，图标连续几次消失就判定打完")
        btn_finish.clicked.connect(lambda: self._pick_farm_roi("boss_hp"))
        self.lbl_finish = QLabel("未设置")
        row_finish.addWidget(btn_finish)
        row_finish.addWidget(self.lbl_finish, 1)
        lay.addLayout(row_finish)

        row = QHBoxLayout()
        row.addWidget(QLabel("点完后等寻路"))
        self.spin_boss_wait = self._dspin(1.0, 600.0, self.answers["boss_wait"])
        row.addWidget(self.spin_boss_wait)
        row.addWidget(_hint("默认 30 秒"), 1)
        lay.addLayout(row)
        self._refresh_farm_roi_state()
        lay.addStretch(1)
        return page

    def _pick_farm_roi(self, which: str) -> None:
        titles = {
            "boss_present": ("BOSS 身形",
                             "框住 BOSS 本体（别框会动的特效）"),
            "boss_hp": ("BOSS 血量图标",
                        "框住 BOSS 头顶的血条，或者那个「打 BOSS 才出现」的图标；"
                        "开打时拍下它的样子，图标消失就判定打完"),
        }
        title, info = titles.get(which, ("识别区域", ""))
        if dui.pick_region(self, self.hwnd, self.profile, f"farm.{which}",
                           title, info):
            if which == "boss_present":
                spec = (self.profile.get("farm") or {}).get("boss_present") or {}
                spec.update({"sim": 0.60, "search": 1.6, "tol": 30.0})
                self.profile.setdefault("farm", {})["boss_present"] = spec
            self._refresh_farm_roi_state()

    def _refresh_farm_roi_state(self) -> None:
        farm = self.profile.get("farm") or {}
        for key, label in (("boss_present", self.lbl_bossflag),
                           ("boss_hp", self.lbl_finish)):
            if ((farm.get(key) or {}).get("roi")):
                label.setText("已设置 ✓")
                label.setStyleSheet("color: #15803d; font-weight: 600;")
            else:
                label.setText("未设置")
                label.setStyleSheet("color: #444444;")

    def _pick_boss(self) -> None:
        self._pick_point("boss_pt", "点 BOSS 点",
                         "这里显示的是地图2（副本地图）。点一下 BOSS 所在的位置。", True)
        self.lbl_boss.setText(self._pt_text(self.answers.get("boss_pt")))

    def _pick_farm_pt(self) -> None:
        self._pick_point("farm_pt", "点打怪点",
                         "这里显示的是地图2（副本地图）。点一下平时打小怪的位置。", True)
        self.lbl_farm.setText(self._pt_text(self.answers.get("farm_pt")))

    def _page_farm(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(_title("第 4 步：BOSS 战（打多久 / 喝药 / 拾取）"))
        g = QGridLayout()
        g.addWidget(QLabel("打BOSS上限"), 0, 0)
        self.spin_seconds = self._dspin(5.0, 1000.0, self.answers["seconds"])
        g.addWidget(self.spin_seconds, 0, 1)
        g.addWidget(_hint("血条消失就提前结束；最大 1000 秒"), 0, 2, 1, 2)
        g.addWidget(QLabel("喝药间隔"), 0, 4)
        self.spin_potion = self._dspin(1.0, 60.0, self.answers["potion_every"])
        g.addWidget(self.spin_potion, 0, 5)
        g.addWidget(QLabel("拾取间隔"), 1, 0)
        self.spin_pickup = self._dspin(0.0, 120.0, self.answers["pickup_every"])
        g.addWidget(self.spin_pickup, 1, 1)
        g.addWidget(_hint("挂机时每隔这么多秒按一次拾取键"), 1, 2, 1, 2)
        self.chk_skills = QCheckBox("放技能 1/2")
        self.chk_skills.setChecked(bool(self.answers["use_skills"]))
        g.addWidget(self.chk_skills, 1, 4, 1, 2)

        self.chk_pickup_on_a = QCheckBox("每次平A后都拾取")
        self.chk_pickup_on_a.setChecked(bool(self.answers["pickup_on_a"]))
        g.addWidget(self.chk_pickup_on_a, 2, 0, 1, 2)
        g.addWidget(QLabel("动作缓冲"), 2, 2)
        self.spin_action_gap = self._dspin(0.0, 10.0, self.answers["action_gap"])
        g.addWidget(self.spin_action_gap, 2, 3)
        g.addWidget(_hint("秒（进本后/到点后/拾取后）"), 2, 4, 1, 3)

        g.addWidget(QLabel("每"), 3, 0)
        self.spin_fatigue_every = self._dspin(10.0, 600.0,
                                              self.answers["fatigue_every"])
        g.addWidget(self.spin_fatigue_every, 3, 1)
        g.addWidget(_hint("秒查一次疲劳（默认 60 秒；不设挂机时长）"), 3, 2, 1, 3)

        self.chk_potion_timer = QCheckBox("定时喝药")
        self.chk_potion_timer.setChecked(
            bool(self.answers["potion_timer_enabled"]))
        g.addWidget(self.chk_potion_timer, 4, 0, 1, 2)
        g.addWidget(QLabel("每"), 4, 2)
        self.spin_potion_timer = self._dspin(
            5.0, 600.0, self.answers["potion_timer_every"])
        g.addWidget(self.spin_potion_timer, 4, 3)
        g.addWidget(_hint("秒按一次（不管血量；适合 60 秒的 buff 药）"), 4, 4, 1, 3)
        lay.addLayout(g)

        row = QHBoxLayout()
        btn_hp = QPushButton("框选血量区域（可选）")
        btn_hp.clicked.connect(self._pick_hp)
        self.lbl_hp = QLabel()
        row.addWidget(btn_hp)
        row.addWidget(QLabel("血量低于"))
        self.spin_hp = QSpinBox()
        self.spin_hp.setRange(0, 9999999)
        self.spin_hp.setValue(int(self.answers["hp_threshold"]))
        self.spin_hp.setFixedWidth(100)
        row.addWidget(self.spin_hp)
        row.addWidget(QLabel("才喝药（Q+W）"))
        row.addWidget(self.lbl_hp, 1)
        lay.addLayout(row)
        self._refresh_hp_state()
        lay.addWidget(_hint("不框血量区域也行：按「喝药间隔」固定喝。"))
        lay.addStretch(1)
        return page

    def _refresh_hp_state(self) -> None:
        if (self.profile.get("farm") or {}).get("hp_roi"):
            self.lbl_hp.setText("血量区域已设置 \u2713")
            self.lbl_hp.setStyleSheet("color: #15803d; font-weight: 600;")
        else:
            self.lbl_hp.setText("未设置（按固定间隔喝）")
            self.lbl_hp.setStyleSheet("color: #444444;")

    def _pick_hp(self) -> None:
        if dui.pick_region(self, self.hwnd, self.profile, "farm.hp_roi",
                           "血量数字区域", "框住角色血量数字（如 1725/1780）"):
            self._refresh_hp_state()

    def _page_fatigue(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(_title("第 5 步：退出副本后检查疲劳"))
        self.chk_fatigue = QCheckBox("每轮退出副本后检测一次疲劳值")
        self.chk_fatigue.setChecked(bool(self.answers["fatigue_enabled"]))
        lay.addWidget(self.chk_fatigue)
        g = QGridLayout()
        g.addWidget(QLabel("疲劳下限"), 0, 0)
        self.spin_fatigue_low = QSpinBox()
        self.spin_fatigue_low.setRange(-999999, 999999)
        self.spin_fatigue_low.setValue(int(self.answers["fatigue_low"]))
        self.spin_fatigue_low.setFixedWidth(110)
        g.addWidget(self.spin_fatigue_low, 0, 1)
        g.addWidget(_hint("疲劳 < 下限 → 去打BOSS（可填负数）"), 0, 2, 1, 3)
        g.addWidget(QLabel("疲劳上限"), 1, 0)
        self.spin_fatigue_high = QSpinBox()
        self.spin_fatigue_high.setRange(-999999, 999999)
        self.spin_fatigue_high.setValue(int(self.answers["fatigue_high"]))
        self.spin_fatigue_high.setFixedWidth(110)
        g.addWidget(self.spin_fatigue_high, 1, 1)
        g.addWidget(_hint("疲劳 > 上限 → 回打怪点打小怪"), 1, 2, 1, 3)
        lay.addLayout(g)

        row = QHBoxLayout()
        btn_pt = QPushButton("① 点疲劳图标的位置")
        btn_pt.clicked.connect(self._pick_fatigue_pt)
        self.lbl_fatigue_pt = QLabel(self._pt_text(self.answers.get("fatigue_pt")))
        row.addWidget(btn_pt)
        row.addWidget(self.lbl_fatigue_pt, 1)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        btn_roi = QPushButton("② 框疲劳提示区域（可选）")
        btn_roi.clicked.connect(self._pick_fatigue_roi)
        self.lbl_fatigue_roi = QLabel()
        row2.addWidget(btn_roi)
        row2.addWidget(self.lbl_fatigue_roi, 1)
        lay.addLayout(row2)
        self._refresh_fatigue_roi()
        lay.addWidget(_hint(
            "疲劳检测 = 点一下疲劳图标 → 读弹出的「当前疲劳值:xxx/xxx」。"
            "不框区域的话会用主界面「疲劳检测」页框过的区域。"
        ))
        lay.addStretch(1)
        return page

    def _refresh_fatigue_roi(self) -> None:
        if (self.profile.get("fatigue") or {}).get("roi"):
            self.lbl_fatigue_roi.setText("已设置 \u2713")
            self.lbl_fatigue_roi.setStyleSheet("color: #15803d; font-weight: 600;")
        else:
            self.lbl_fatigue_roi.setText("未设置（沿用主界面框过的区域）")
            self.lbl_fatigue_roi.setStyleSheet("color: #444444;")

    def _pick_fatigue_pt(self) -> None:
        self._pick_point("fatigue_pt", "点疲劳图标",
                         "这里显示的是当前画面。点一下那个能弹出疲劳值的图标/按钮。",
                         False)
        self.lbl_fatigue_pt.setText(self._pt_text(self.answers.get("fatigue_pt")))

    def _pick_fatigue_roi(self) -> None:
        if dui.pick_region(self, self.hwnd, self.profile, "fatigue.roi",
                           "疲劳提示区域",
                           "先点疲劳图标让提示弹出来，再框住「当前疲劳值:xxx/xxx」那块文字"):
            self._refresh_fatigue_roi()

    def _page_mount(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(_title("第 6 步：宠物骑乘（每次寻路前按 S 检查）"))
        lay.addWidget(_hint(
            "按 S 会弹出骑乘窗口：程序先识别「弹窗出现了没有」，"
            "再看「骑乘状态标记」判断是不是已经骑上了；没骑就点「骑乘」按钮，"
            "然后关掉弹窗继续寻路。"
        ))
        row0 = QHBoxLayout()
        self.chk_mount = QCheckBox("每次寻路前检查骑乘")
        self.chk_mount.setChecked(bool(self.answers["mount_enabled"]))
        row0.addWidget(self.chk_mount)
        row0.addWidget(QLabel("检查键"))
        self.edit_mount_key = QLineEdit(str(self.answers["mount_key"]))
        self.edit_mount_key.setFixedWidth(50)
        row0.addWidget(self.edit_mount_key)
        row0.addStretch(1)
        lay.addLayout(row0)

        row = QHBoxLayout()
        btn_dialog = QPushButton("① 框住「骑乘弹窗」区域")
        btn_dialog.clicked.connect(lambda: self._pick_mount_roi("dialog"))
        self.lbl_mount_dialog = QLabel("未设置")
        row.addWidget(btn_dialog)
        row.addWidget(self.lbl_mount_dialog, 1)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        btn_text = QPushButton("② 框住宠物文本区域（OCR 读「骑乘/休息」）")
        btn_text.clicked.connect(lambda: self._pick_mount_roi("text"))
        self.lbl_mount_state = QLabel("未设置")
        row2.addWidget(btn_text)
        row2.addWidget(self.lbl_mount_state, 1)
        lay.addLayout(row2)

        row2b = QHBoxLayout()
        row2b.addWidget(QLabel("未骑乘文字"))
        self.edit_mount_text = QLineEdit("骑乘")
        self.edit_mount_text.setFixedWidth(70)
        row2b.addWidget(self.edit_mount_text)
        row2b.addWidget(QLabel("已骑乘文字"))
        self.edit_rest_text = QLineEdit("休息")
        self.edit_rest_text.setFixedWidth(70)
        row2b.addWidget(self.edit_rest_text)
        row2b.addStretch(1)
        lay.addLayout(row2b)

        row3 = QHBoxLayout()
        btn_pt = QPushButton("③ 点「骑乘」按钮的位置")
        btn_pt.clicked.connect(self._pick_mount_pt)
        self.lbl_mount_pt = QLabel(self._pt_text(self.answers.get("mount_pt")))
        row3.addWidget(btn_pt)
        row3.addWidget(self.lbl_mount_pt, 1)
        lay.addLayout(row3)
        self._refresh_mount_state()
        lay.addStretch(1)
        return page

    def _refresh_mount_state(self) -> None:
        mount = self.profile.get("mount") or {}
        self.edit_mount_text.setText(str(mount.get("mount_text", "骑乘")))
        self.edit_rest_text.setText(str(mount.get("rest_text", "休息")))
        for spec, label in (
            (mount.get("dialog") or {}, self.lbl_mount_dialog),
            ({"roi": mount.get("text_roi")}, self.lbl_mount_state),
        ):
            if (spec or {}).get("roi"):
                label.setText("已设置 ✓")
                label.setStyleSheet("color: #15803d; font-weight: 600;")
            else:
                label.setText("未设置")
                label.setStyleSheet("color: #444444;")

    def _pick_mount_roi(self, which: str) -> None:
        if which == "dialog":
            title, info = ("骑乘弹窗区域",
                           "按 S 把骑乘窗口弹出来再截图，框住这个弹窗里固定不变的部分")
        elif which == "text":
            title, info = ("宠物文本区域",
                           "框住宠物那一行文字：没骑时是「骑乘」，骑上后变「休息」")
        else:
            title, info = ("骑乘状态标记", "旧版用，可不标")
        if dui.pick_region(self, self.hwnd, self.profile, f"mount.{which}", title, info):
            self._refresh_mount_state()

    def _pick_mount_pt(self) -> None:
        self._pick_point("mount_pt", "点「骑乘」按钮",
                         "按 S 打开骑乘窗口，点一下窗口里的「骑乘」按钮。", False)
        self.lbl_mount_pt.setText(self._pt_text(self.answers.get("mount_pt")))

    def _page_exit(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(_title("第 6 步：怎么退出副本（识别弹窗 + 两个点）"))
        lay.addWidget(_hint(
            "退出和进本是两套识别，互不共用：这里框的是「确认退出」弹窗，"
            "跟第 2 步的「进入游戏」弹窗是两个不同区域。"
        ))
        row0 = QHBoxLayout()
        btn_dialog = QPushButton("① 框住「退出确认弹窗」区域（识别用）")
        btn_dialog.clicked.connect(self._pick_exit_dialog_roi)
        self.lbl_exit_dialog = QLabel("未设置")
        row0.addWidget(btn_dialog)
        row0.addWidget(self.lbl_exit_dialog, 1)
        lay.addLayout(row0)

        row = QHBoxLayout()
        btn_exit = QPushButton("② 点「退出副本」按钮的位置")
        btn_exit.clicked.connect(self._pick_exit)
        self.lbl_exit = QLabel(self._pt_text(self.answers.get("exit_pt")))
        row.addWidget(btn_exit)
        row.addWidget(self.lbl_exit, 1)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        btn_confirm = QPushButton("③ 点「确认」按钮的位置")
        btn_confirm.clicked.connect(self._pick_exit_confirm)
        self.lbl_exit_confirm = QLabel(self._pt_text(self.answers.get("exit_confirm_pt")))
        row2.addWidget(btn_confirm)
        row2.addWidget(self.lbl_exit_confirm, 1)
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("退出后加载"))
        self.spin_exit_settle = self._dspin(1.0, 300.0, self.answers["exit_settle"])
        row3.addWidget(self.spin_exit_settle)
        row3.addWidget(QLabel("每轮间隔"))
        self.spin_gap = self._dspin(0.0, 3600.0, self.answers["gap"])
        row3.addWidget(self.spin_gap)
        row3.addStretch(1)
        lay.addLayout(row3)
        lay.addStretch(1)
        self._refresh_exit_dialog()
        return page

    def _refresh_exit_dialog(self) -> None:
        spec = (self.profile.get("exit") or {}).get("dialog") or {}
        if (spec or {}).get("roi"):
            self.lbl_exit_dialog.setText("已设置 ✓")
            self.lbl_exit_dialog.setStyleSheet("color: #15803d; font-weight: 600;")
        else:
            self.lbl_exit_dialog.setText("未设置（没框就盲点确认）")
            self.lbl_exit_dialog.setStyleSheet("color: #444444;")

    def _pick_exit_dialog_roi(self) -> None:
        if dui.pick_region(
            self, self.hwnd, self.profile, "exit.dialog", "退出确认弹窗区域",
            "点一下「退出副本」，等「确认退出」弹窗出来再截图，"
            "框住这个弹窗里固定不变的部分（和进本弹窗是两个不同的区域）",
        ):
            self._refresh_exit_dialog()

    def _pick_exit(self) -> None:
        self._pick_point("exit_pt", "点「退出副本」按钮",
                         "这里显示的是当前画面。点一下「退出副本」按钮。", False)
        self.lbl_exit.setText(self._pt_text(self.answers.get("exit_pt")))

    def _pick_exit_confirm(self) -> None:
        self._pick_point("exit_confirm_pt", "点「确认」按钮",
                         "把「确认退出」弹窗显示出来，再点「确认/确定」按钮。", False)
        self.lbl_exit_confirm.setText(self._pt_text(self.answers.get("exit_confirm_pt")))

    def _page_summary(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(_title("就这些，下面是程序要执行的流程"))
        self.browser = QTextBrowser()
        lay.addWidget(self.browser, 1)
        lay.addWidget(_hint(
            "点「完成」保存。回到「副本流程」页：先「试跑一轮（不按键）」看顺序，"
            "没问题再点「▶ 开始运行」。多开就在 4 行里分别设挂机秒数。"
        ))
        return page

    def _enter_summary(self) -> None:
        self._collect()
        self.profile, self.plan_lines = compose_profile(self.profile, self.answers)
        html = ["<h3>执行流程</h3><ol>"]
        for line in self.plan_lines:
            html.append(f"<li>{line}</li>")
        html.append("</ol>")
        missing = [ln for ln in self.plan_lines if ln.startswith("还差")]
        if missing:
            html.append(f"<p style='color:#b45309'><b>{missing[0]}</b></p>")
        else:
            html.append("<p style='color:#15803d'><b>标定齐全，可以先试跑。</b></p>")
        html.append(f"<p style='color:#555555'>配置：{df.profile_path()}</p>")
        self.browser.setHtml("".join(html))
