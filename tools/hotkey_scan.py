# -*- coding: utf-8 -*-
r"""微信桥 · 全局热键可用性检测

用法：
    python hotkey_scan.py

为什么要有这个脚本：
    2026-10-09 实测：本机 Ctrl+Alt+字母（S/Q/D/X/A）**全部**注册失败
    （错误码 1409 = 已被别的程序占用）。与其让桥一个个瞎试，
    不如把所有想得到的组合一次性问清楚，把「哪些真的能用」钉死。

原理：
    RegisterHotKey(hwnd=NULL, id, mods, vk) 是合法调用 —— 不建窗口也能试注册，
    成功就立刻 UnregisterHotKey 释放。所以它能在任何进程里独立跑。
    热键是**全局资源**：如果微信桥正跑着并占着某个组合，这里会显示为被占用。

结果同时写到 hotkey_scan.txt（UTF-8），不依赖控制台编码。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import wx_tray as T          # noqa: E402  复用同一份常量和注册逻辑

OUT = BASE / "hotkey_scan.txt"

MOD_CTRL_ALT = T.MOD_CONTROL | T.MOD_ALT
MOD_CTRL_SHIFT_ALT = T.MOD_CONTROL | T.MOD_SHIFT | T.MOD_ALT

# 字母（当年失败的组合，用来交叉验证）
LETTERS = [(0x41 + i, chr(0x41 + i)) for i in range(26)]

# 主键盘数字 1..9 0
DIGITS = [(0x31 + i, str(i + 1)) for i in range(9)] + [(0x30, "0")]

# 小键盘数字 1..9 0（和主键盘是**不同的**组合，很少被占）
NUMPAD = [(0x61 + i, "小键盘%d" % (i + 1)) for i in range(9)] + [(0x60, "小键盘0")]

SECTIONS = [
    ("① Ctrl+Alt+字母（当年失败的组合，交叉验证）", MOD_CTRL_ALT, LETTERS),
    ("② Ctrl+Alt+数字 1~9、0（本次主方案）", MOD_CTRL_ALT, DIGITS),
    ("③ Ctrl+Alt+小键盘数字（备用，极少被占）", MOD_CTRL_ALT, NUMPAD),
    ("④ Ctrl+Shift+Alt+数字 1~9、0（深度备用）", MOD_CTRL_SHIFT_ALT, DIGITS),
]


def main():
    lines = []

    def out(s=""):
        lines.append(s)
        try:
            print(s, flush=True)
        except Exception:
            pass

    out("=" * 62)
    out("微信桥 · 全局热键可用性检测")
    out("=" * 62)
    out("")
    out("说明：✅ 可用   ❌ 被别的程序占用或注册失败")
    out("")

    free_primary = []
    free_all = []
    hid = 0xB100

    for title, mods, keys in SECTIONS:
        out("-" * 62)
        out(title)
        out("-" * 62)
        for vk, name in keys:
            hid += 1
            ok, err, desc = T.try_register_hotkey(None, mods, vk, hid)
            if ok:
                T.unregister_hotkey(None, hid)
            label = "%s+%s" % (
                "Ctrl+Alt" if mods == MOD_CTRL_ALT else "Ctrl+Shift+Alt", name)
            out("  %-22s %s" % (label, "✅ 可用" if ok else "❌ %s" % desc))
            if ok:
                free_all.append(label)
                if title.startswith("②"):
                    free_primary.append(label)
        out("")

    out("=" * 62)
    out("结论")
    out("=" * 62)
    if free_primary:
        out("主方案（Ctrl+Alt+数字）可用：%s" % "、".join(free_primary))
        out(">>> 桥会自动挑第一个：%s" % free_primary[0])
    elif free_all:
        out("⚠️ Ctrl+Alt+数字 全部不可用！")
        out("退路可用：%s" % "、".join(free_all[:8]))
    else:
        out("⚠️ 所有测过的组合都被占用了（这台机器上全局热键资源很紧张）")
        out("建议：改用托盘右键菜单「发送剪贴板里的文字」触发发送")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    out("")
    out("（完整结果已写入 %s）" % OUT)


if __name__ == "__main__":
    main()
