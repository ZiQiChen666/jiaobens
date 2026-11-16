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
            name = f"<layout {type(sub).__name__}>"
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
tab = getattr(w, "dungeon_tab", None)
check("副本页存在", tab is not None)
if tab is not None:
    scan(tab, "副本流程页")

# 向导每页也扫一遍
from dungeon_wizard import DungeonWizard  # noqa: E402

wins = df.list_windows("CreatureCurios")
wiz = DungeonWizard(wins[0][0] if wins else 0, df.load_profile("__none__.json"))
for idx in range(len(wiz._pages)):
    wiz._go(idx)
    scan(wiz._pages[idx], f"向导第{idx + 1}页")
w.close()

print()
if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("PASS: 界面没有控件重叠")
