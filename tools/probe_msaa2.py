# -*- coding: utf-8 -*-
"""
微信桥 · MSAA 终极复测：按屏幕坐标问 MSAA

前两次探针（UIA 三种走法 / MSAA 子节点枚举）都读不到微信控件。
这一发换一个完全不同的入口：oleacc.AccessibleObjectFromPoint
—— 直接问系统"这个坐标上的无障碍对象是谁"。
它不依赖控件树遍历，只要微信把界面暴露给了 MSAA，就一定问得出来。

做法：在微信主窗口范围内铺一张 48px 的网格，逐点问一遍，
去重后列出所有"见过"的 (role, name)。如果整张网只能问出 1~2 种，
就说明微信 4.x 在本机确实没暴露任何界面元素。

结果写 msaa2_report.txt（UTF-8）。
"""

import ctypes
import ctypes.wintypes as wt
import datetime
import os

BASE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(BASE, "msaa2_report.txt")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
oleacc = ctypes.oledll.oleacc
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
WECHAT_EXES = ("weixin.exe", "wechat.exe")

ROLE = {
    9: "WINDOW", 10: "CLIENT", 11: "MENUPOPUP", 12: "MENUITEM", 14: "TEXT",
    15: "TITLEBAR", 16: "PANE", 20: "LINK", 21: "GRAPHIC", 22: "STATICTEXT",
    23: "TEXTFIELD", 28: "DIALOG", 34: "LIST", 35: "TABLE", 37: "CELL",
    42: "DOCUMENT", 43: "PUSHBUTTON", 44: "GROUPING", 45: "SEPARATOR",
    46: "TOOLBAR", 47: "LISTITEM", 48: "LISTBOX", 57: "TOOLTIP",
}

lines = []


def log(s=""):
    lines.append(str(s))


def win_text(h):
    n = user32.GetWindowTextLengthW(h)
    b = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(h, b, n + 1)
    return b.value


def win_class(h):
    b = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, b, 256)
    return b.value


def win_pid(h):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    return pid.value


def proc_path(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        return buf.value if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(h)


def win_rect(h):
    r = wt.RECT()
    if user32.GetWindowRect(h, ctypes.byref(r)):
        return (r.left, r.top, r.right, r.bottom)
    return (0, 0, 0, 0)


def main_window():
    best = None

    def cb(h, _):
        nonlocal best
        exe = proc_path(win_pid(h))
        if os.path.basename(exe).lower() in WECHAT_EXES and user32.IsWindowVisible(h):
            x1, y1, x2, y2 = win_rect(h)
            area = (x2 - x1) * (y2 - y1)
            if area > 100000 and (best is None or area > best[0]):
                best = (area, h)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return best[1] if best else None


def main():
    import comtypes.client
    try:
        comtypes.client.GetModule("oleacc.dll")
    except Exception:
        pass
    from comtypes.gen import Accessibility as ACC
    from comtypes.automation import VARIANT

    oleacc.AccessibleObjectFromPoint.argtypes = [
        wt.POINT,
        ctypes.POINTER(ctypes.POINTER(ACC.IAccessible)),
        ctypes.POINTER(VARIANT),
    ]
    oleacc.AccessibleObjectFromPoint.restype = ctypes.c_long

    log("微信桥 · MSAA 坐标复测 · %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log("")

    h = main_window()
    if not h:
        log("!! 没找到可见的微信主窗口")
    else:
        x1, y1, x2, y2 = win_rect(h)
        log("微信主窗口 hwnd=%s class=%s rect=(%d,%d)-(%d,%d) %dx%d" % (
            h, win_class(h), x1, y1, x2, y2, x2 - x1, y2 - y1))
        log("在窗口内按 48px 网格逐点问 AccessibleObjectFromPoint …")
        log("")

        seen = {}
        pts = 0
        for yy in range(y1 + 24, y2, 48):
            for xx in range(x1 + 24, x2, 48):
                pts += 1
                try:
                    p = ctypes.POINTER(ACC.IAccessible)()
                    vc = VARIANT()
                    hr = oleacc.AccessibleObjectFromPoint(wt.POINT(xx, yy), ctypes.byref(p), ctypes.byref(vc))
                    if hr != 0 or not p:
                        continue
                    try:
                        nm = p.accName(vc) or ""
                    except Exception:
                        nm = "<name读不到>"
                    try:
                        ro = p.accRole(vc)
                        rn = ROLE.get(ro, str(ro))
                    except Exception:
                        rn = "<role读不到>"
                    key = (rn, str(nm)[:40])
                    if key not in seen:
                        seen[key] = (xx, yy)
                except Exception:
                    pass

        log("共问了 %d 个坐标点，去掉重复后拿到 %d 种无障碍对象：" % (pts, len(seen)))
        for (rn, nm), (xx, yy) in sorted(seen.items(), key=lambda kv: -len(kv[0][1])):
            log("   role=%-12s name=%-42r 首见于 (%d,%d)" % (rn, nm, xx, yy))

        log("")
        kinds = {k[0] for k in seen}
        log("出现的 role 种类：%s" % sorted(kinds))
        if len(seen) <= 3 or not (kinds - {"WINDOW", "CLIENT", "PANE"}):
            log(">>> 结论：微信 4.x 在本机没有向 MSAA 暴露任何可用界面元素。")
        else:
            log(">>> 结论：MSAA 有内容！上面这些对象里若出现 TEXTFIELD/LISTITEM/PUSHBUTTON，")
            log("    发送链路可以改走 MSAA 实现。")

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("report: %s (%d lines)" % (REPORT, len(lines)))


if __name__ == "__main__":
    main()
