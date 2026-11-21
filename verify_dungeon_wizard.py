# -*- coding: utf-8 -*-
"""验证副本向导（三步流程版）：compose_profile + 界面走完 + 保存。

跑法：python verify_dungeon_wizard.py
"""
import os
import sys
import tempfile

import dungeon_flow as df
import dungeon_wizard as dw

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


base = df.load_profile("__no_such_profile__.json")
answers = {
    "open_map": "m",
    "pickup": "space",
    "entry_pt": [0.30, 0.55],
    "entry_wait": 40.0,
    "dialog_timeout": 25.0,
    "retry": 4,
    "confirm_pt": [0.50, 0.55],
    "load_wait": 10.0,
    "boss_pt": [0.70, 0.40],
    "boss_wait": 30.0,
    "seconds": 45.0,
    "hp_threshold": 2000,
    "potion_every": 6.0,
    "potion_timer_enabled": True,
    "potion_timer_every": 45.0,
    "pickup_every": 9.0,
    "use_skills": True,
    "fatigue_enabled": True,
    "fatigue_low": -500,
    "fatigue_high": 900,
    "fatigue_pt": [0.10, 0.90],
    "farm_pt": [0.60, 0.60],
    "fatigue_every": 60.0,
    "mount_enabled": True,
    "mount_key": "s",
    "mount_pt": [0.50, 0.20],
    "exit_pt": [0.94, 0.05],
    "exit_confirm_pt": [0.55, 0.68],
    "exit_settle": 10.0,
    "gap": 12.0,
}
profile, plan = dw.compose_profile(base, answers)

check("走进副本 40s", profile["enter"]["travel_wait"] == 40.0)
check("等弹窗超时", profile["enter"]["dialog_timeout"] == 25.0)
check("没弹窗重试次数", profile["enter"]["retry"] == 4)
check("进本加载 10s", profile["enter"]["load_wait"] == 10.0)
check("跑向 BOSS 30s", profile["farm"]["travel_wait"] == 30.0)
check("BOSS 战上限", profile["loop"]["boss_seconds"] == 45.0)
check("喝药间隔 6s", profile["farm"]["potion_every"] == 6.0)
check("定时喝药 45s",
      profile["farm"]["potion_timer_enabled"] is True
      and profile["farm"]["potion_timer_every"] == 45.0,
      str({k: profile["farm"].get(k) for k in
           ("potion_timer_enabled", "potion_timer_every")}))
check("拾取间隔", profile["farm"]["pickup_every"] == 9.0)
check("退出后加载 10s", profile["exit"]["settle"] == 10.0)
check("退出两个点", profile["exit"]["exit_pt"] == [0.94, 0.05]
      and profile["exit"]["confirm_pt"] == [0.55, 0.68])
check("疲劳：开启 + 上下限 + 图标点",
      profile["fatigue"]["enabled"] is True
      and profile["fatigue"]["low"] == -500
      and profile["fatigue"]["high"] == 900
      and "threshold" not in profile["fatigue"]
      and profile["fatigue"]["pt"] == [0.1, 0.9])
check("每轮间隔", profile["retry"]["gap"] == 12.0)
check("步骤表 3 步 + 疲劳说明", len(plan) >= 3
      and any("①" in l for l in plan)
      and any("②" in l for l in plan)
      and any("③" in l for l in plan)
      and any("疲劳" in l for l in plan), str(plan))
check("页面默认值：40/24/10/30/30/10",
      df.DEFAULT_PROFILE["enter"]["travel_wait"] == 40.0
      and df.DEFAULT_PROFILE["enter"]["dialog_timeout"] == 24.0
      and df.DEFAULT_PROFILE["enter"]["load_wait"] == 10.0
      and df.DEFAULT_PROFILE["farm"]["travel_wait"] == 30.0
      and df.DEFAULT_PROFILE["farm"]["seconds"] == 30.0
      and df.DEFAULT_PROFILE["exit"]["settle"] == 10.0)

profile2, plan2 = dw.compose_profile(base, {})
check("全部跳过也不崩，并列出缺失", any(l.startswith("还差") for l in plan2), str(plan2))
check("全部跳过时也点名「打怪点」",
      any("打怪点" in l for l in plan2), str(plan2))

a = df.copy_profile_for_slot(profile, 60)
b = df.copy_profile_for_slot(profile, 300)
check("多开不再设挂机时长（只改 BOSS 上限）",
      a["loop"]["boss_seconds"] == 60.0
      and b["loop"]["boss_seconds"] == 300.0
      and profile["loop"]["boss_seconds"] == 45.0
      and "farm_seconds" not in a["loop"])

# ---- 界面走一遍 -------------------------------------------------------
_real_save = df.save_profile
_real_active = df.active_name
_real_path = df.profile_path
tmp = os.path.join(tempfile.gettempdir(), "wizard_verify3.json")
if os.path.exists(tmp):
    os.remove(tmp)
df.save_profile = lambda prof, path=None: _real_save(prof, tmp)
# 本机可能挂着命名方案；强制走「默认标定」分支，保证写的是 tmp
df.active_name = lambda: ""
df.profile_path = lambda: tmp

try:
    from PySide6.QtWidgets import QApplication

    qa = QApplication.instance() or QApplication(sys.argv)
    wins = df.list_windows("CreatureCurios")
    hwnd = wins[0][0] if wins else 0

    wiz = dw.DungeonWizard(hwnd, df.load_profile("__no_such_profile__.json"))
    check("向导页数 = 9（介绍/进本/弹窗/BOSS/挂机/疲劳/骑乘/退出/汇总）",
          len(wiz._pages) == 9, str(len(wiz._pages)))
    check("BOSS 页有「打怪点」按钮和标签",
          hasattr(wiz, "lbl_farm") and wiz.lbl_farm.text() == "未设置",
          getattr(getattr(wiz, "lbl_farm", None), "text", lambda: "?")())

    wiz.answers.update({
        "entry_pt": [0.30, 0.55], "confirm_pt": [0.50, 0.55],
        "boss_pt": [0.70, 0.40], "farm_pt": [0.60, 0.60], "exit_pt": [0.94, 0.05],
        "exit_confirm_pt": [0.55, 0.68], "fatigue_pt": [0.10, 0.90],
    })
    wiz.spin_entry_wait.setValue(40.0)
    wiz.spin_dialog_timeout.setValue(24.0)
    wiz.spin_retry.setValue(3)
    wiz.spin_load_wait.setValue(10.0)
    wiz.spin_boss_wait.setValue(30.0)
    wiz.spin_seconds.setValue(30.0)
    wiz.spin_potion.setValue(6.0)
    wiz.spin_pickup.setValue(8.0)
    wiz.spin_hp.setValue(1600)
    wiz.chk_fatigue.setChecked(True)
    wiz.spin_fatigue_low.setValue(-700)
    wiz.spin_fatigue_high.setValue(1000)
    wiz.spin_fatigue_every.setValue(120.0)
    wiz.spin_exit_settle.setValue(10.0)
    wiz.spin_gap.setValue(8.0)
    wiz.chk_mount.setChecked(True)
    wiz.edit_mount_key.setText("s")

    guard = 0
    while wiz.index < len(wiz._pages) - 1 and guard < 20:
        wiz._next()
        guard += 1
    check("能走到汇总页", wiz.index == len(wiz._pages) - 1, f"index={wiz.index}")
    html = wiz.browser.toHtml()
    check("汇总页有流程内容", ("执行流程" in html) or ("①" in html), html[:80])

    wiz._next()  # 完成 → 保存
    saved = df.load_profile(tmp)
    check("保存走进副本 40s", saved["enter"]["travel_wait"] == 40.0)
    check("保存等弹窗 24s", saved["enter"]["dialog_timeout"] == 24.0)
    check("保存进本加载 10s", saved["enter"]["load_wait"] == 10.0)
    check("保存跑向 BOSS 30s", saved["farm"]["travel_wait"] == 30.0)
    check("保存 BOSS 上限 30s", saved["loop"]["boss_seconds"] == 30.0)
    check("保存退出后加载 10s", saved["exit"]["settle"] == 10.0)
    check("保存疲劳上下限", saved["fatigue"]["enabled"] is True
          and saved["fatigue"]["low"] == -700
          and saved["fatigue"]["high"] == 1000
          and saved["fatigue"]["pt"] == [0.1, 0.9])
    check("保存了打怪点", saved["farm"]["farm_pt"] == [0.6, 0.6])
    check("保存了疲劳检查间隔",
          saved["loop"]["fatigue_every"] == 120.0,
          str(saved["loop"].get("fatigue_every")))
    check("保存了骑乘设置",
          saved["mount"]["enabled"] is True and saved["mount"]["key"] == "s")
    check("保存两个退出点", saved["exit"]["exit_pt"] == [0.94, 0.05]
          and saved["exit"]["confirm_pt"] == [0.55, 0.68])
except Exception:
    import traceback
    traceback.print_exc()
    check("向导界面测试", False)
finally:
    df.save_profile = _real_save
    df.active_name = _real_active
    df.profile_path = _real_path
    try:
        os.remove(tmp)
    except OSError:
        pass

print()
if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("PASS: 副本向导（三步流程版）自检通过")
