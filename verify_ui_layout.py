# -*- coding: utf-8 -*-
"""检查界面里有没有控件/子布局叠在一起（QGridLayout 同一格被占两次）。

跑法：python verify_ui_layout.py
"""
import sys

from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QLayout,
    QWidget,
)

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def cells(layout: QGridLayout):
    """列出这个 grid 里每个 item 占的格子 (row, col) 以及它的名字。"""
    out = []
    for i in range(layout.count()):
        item = layout.itemAt(i)
        row, col, rspan, cspan = layout.getItemPosition(i)
        if item is None:
            continue
        w = item.widget()
        if w is not None:
            name = w.text() if hasattr(w, "text") else type(w).__name__
            name = str(name)[:22]
        else:
            sub = item.layout()
            name = f"<{type(sub).__name__}#{i}>"
        for r in range(row, row + max(1, rspan)):
            for c in range(col, col + max(1, cspan)):
                out.append(((r, c), name))
    return out


def scan(widget: QWidget, path: str):
    layout = widget.layout()
    if isinstance(layout, QGridLayout):
        seen = {}
        for cell, name in cells(layout):
            if cell in seen and seen[cell] != name:
                check(f"{path} 第{cell[0]}行第{cell[1]}列没重叠", False,
                      f"「{seen[cell]}」和「{name}」叠在一起")
            seen.setdefault(cell, name)
        check(f"{path} 网格无重叠", True)
    if layout is not None:
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item is None:
                continue
            child_layout = item.layout()
            if child_layout is not None:
                _scan_layout(child_layout, f"{path}/{type(child_layout).__name__}")
            child = item.widget()
            if child is not None and child is not widget:
                scan(child, f"{path}/{type(child).__name__}")


def _scan_layout(layout: QLayout, path: str):
    if isinstance(layout, QGridLayout):
        seen = {}
        for cell, name in cells(layout):
            if cell in seen and seen[cell] != name:
                check(f"{path} 第{cell[0]}行第{cell[1]}列没重叠", False,
                      f"「{seen[cell]}」和「{name}」叠在一起")
            seen.setdefault(cell, name)
        check(f"{path} 网格无重叠", True)
    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item is None:
            continue
        sub = item.layout()
        if sub is not None:
            _scan_layout(sub, f"{path}/sub")
        child = item.widget()
        if child is not None:
            scan(child, f"{path}/{type(child).__name__}")


qa = QApplication.instance() or QApplication(sys.argv)
qa.setStyleSheet(__import__("creature_curios_keyer").APP_STYLE)

import creature_curios_keyer as app  # noqa: E402
import dungeon_flow as df  # noqa: E402

w = app.MainWindow()
w.resize(1600, 1200)
w.show()
tab = getattr(w, "dungeon_tab", None)
check("副本页存在", tab is not None)
if tab is not None:
    w.tabs.setCurrentWidget(tab)
    for _ in range(8):
        qa.processEvents()
    scan(tab, "副本流程页")


def check_widgets_are_saved(path: str = "dungeon_ui.py") -> None:
    """静态检查：每个输入控件都必须被 _save_from_ui 读到（防“死控件”）。

    上一版就出过这种坑：界面里多了一个“打怪时每”=spin_fatigue_every，
    但 _save_from_ui 只读另一个 spin，改它完全没效果，
    界面重叠检查也抓不到。这里不创建窗口，直接比 AST，纯静态、可重复。
    """
    import ast
    import os

    target = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    tree = ast.parse(open(target, encoding="utf-8").read())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "DungeonTab")
    prefixes = ("spin_", "chk_", "edit_")
    created = set()
    save_reads = set()
    for node in cls.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "self"
                    and sub.attr.startswith(prefixes)):
                if node.name == "_save_from_ui":
                    save_reads.add(sub.attr)
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "pair"
                    and len(sub.args) >= 4
                    and isinstance(sub.args[3], ast.Constant)):
                created.add(str(sub.args[3].value))
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                for t in targets:
                    if (isinstance(t, ast.Attribute)
                            and isinstance(t.value, ast.Name)
                            and t.value.id == "self"
                            and t.attr.startswith(prefixes)):
                        created.add(t.attr)
    dead = sorted(created - save_reads)
    check(f"{path}：{len(created)} 个输入控件都接进了保存（无死控件）",
          not dead, "没被 _save_from_ui 读到：" + "、".join(dead))


check_widgets_are_saved()

if tab is not None:
    check("副本页实例已创建", True)


def check_missing_items(tab) -> None:
    """勾了引路宏（而且有录制内容）就不该再要求地图点位。"""
    import json

    p = tab.profile
    backup = json.dumps(p, sort_keys=True, ensure_ascii=False)
    try:
        nav = p.setdefault("nav_macro", {})
        nav["enabled"] = True
        nav["fallback"] = False
        nav["events"] = [{"type": "key_down", "t": 0.0, "name": "m"}]
        enter = p.setdefault("enter", {})
        enter["dungeon_pt"] = None
        enter["confirm_pt"] = None
        farm = p.setdefault("farm", {})
        farm["boss_pt"] = None
        farm["farm_pt"] = [0.20, 0.30]
        p.setdefault("loop", {})["link_fatigue"] = True
        exit_cfg = p.setdefault("exit", {})
        exit_cfg["exit_pt"] = [0.94, 0.05]
        exit_cfg["confirm_pt"] = [0.55, 0.68]
        miss = tab._missing_items()
        check("用了引路宏 → 不再要求副本入口/BOSS 点", miss == [], str(miss))
        nav["fallback"] = True
        miss2 = tab._missing_items()
        check("勾了「回退地图点位」→ 又要求地图点位",
              any("副本入口" in m for m in miss2)
              and any("BOSS 点" in m for m in miss2), str(miss2))
        nav["events"] = []
        miss3 = tab._missing_items()
        check("引路宏没内容 → 退回要求地图点位", len(miss3) >= 3, str(miss3))
        nav["events"] = [{"type": "key_down", "t": 0.0, "name": "m"}]
        nav["fallback"] = False
        farm["farm_pt"] = None
        miss4 = tab._missing_items()
        check("没标打怪点（且开着联动）→ 提前拦下",
              any("打怪点" in m for m in miss4), str(miss4))
    finally:
        p.clear()
        p.update(json.loads(backup))


if tab is not None:
    check_missing_items(tab)


def check_group_overlaps(widget, path: str):
    """把每个 QGroupBox 里直接子控件两两比几何，找“真叠在一起”。"""
    from PySide6.QtWidgets import QGroupBox

    for gb in widget.findChildren(QGroupBox):
        lay = gb.layout()
        if lay is not None:
            lay.activate()
        gb.adjustSize()
        if lay is not None:
            lay.activate()
        qa.processEvents()
        rects = []
        for ch in gb.findChildren(QWidget):
            if ch.parent() is not gb or not ch.isVisible():
                continue
            geo = ch.geometry()
            if geo.width() <= 1 or geo.height() <= 1:
                continue
            label = ch.text() if hasattr(ch, "text") else ""
            rects.append((type(ch).__name__, str(label)[:12], geo))
        bad = 0
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                inter = rects[i][2].intersected(rects[j][2])
                if inter.width() > 4 and inter.height() > 4:
                    bad += 1
                    if bad <= 3:
                        print(f"    叠在一起：{rects[i][:2]} × {rects[j][:2]} {inter}")
        check(f"{path}/{gb.title()[:12]} 控件无重叠（{len(rects)} 个）", bad == 0,
              f"{bad} 处重叠")


if tab is not None:
    check_group_overlaps(tab, "副本流程页")

# 向导每页也扫一遍
from dungeon_wizard import DungeonWizard  # noqa: E402

wins = df.list_windows("CreatureCurios")
wiz = DungeonWizard(wins[0][0] if wins else 0, df.load_profile("__none__.json"))
for idx in range(len(wiz._pages)):
    wiz._go(idx)
    scan(wiz._pages[idx], f"向导第{idx + 1}页")
# 收尾：把热键/托盘/后台线程清掉再退出。
# 不清理的话 keyboard 的钩子线程会和解释器退出抢资源，
# 偶发退出码 127（输出全 PASS 但进程"崩"了）。
try:
    w._quitting = True
    w._cleanup()
except Exception:
    pass
try:
    if getattr(w, "_tray", None) is not None:
        w._tray.hide()
except Exception:
    pass
try:
    import keyboard as _kb
    _kb.unhook_all()
except Exception:
    pass
w.close()
del w
qa.processEvents()

print()
if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("PASS: 界面没有控件重叠")
