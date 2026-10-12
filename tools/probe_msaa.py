# -*- coding: utf-8 -*-
"""
微信桥 · MSAA / IAccessible 专项复测（只读）

为什么还要试：Qt 在 Windows 上同时挂两套无障碍通道 —— UIA 和 MSAA(IAccessible)。
本机 UIA 实测是空的，但 MSAA 可能还有内容。如果 MSAA 能用，发送链路就能改用 MSAA 实现。

对每个候选窗口（顶层 + 渲染子窗口）都试：
  OBJID_CLIENT(0xFFFFFFFC) ：通常给客户端内容
  OBJID_WINDOW(0)          ：窗口自身
结果写 msaa_report.txt（UTF-8）。
"""

import ctypes
import ctypes.wintypes as wt
import datetime
import os

BASE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(BASE, "msaa_report.txt")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
oleacc = ctypes.oledll.oleacc  # oledll 会自动检查 HRESULT
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
WECHAT_EXES = ("weixin.exe", "wechat.exe")

OBJID_WINDOW = 0
OBJID_CLIENT = 0xFFFFFFFC

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


def win_size(h):
    r = wt.RECT()
    if user32.GetWindowRect(h, ctypes.byref(r)):
        return (r.right - r.left, r.bottom - r.top)
    return (0, 0)


def targets():
    out = []

    def cb(h, _):
        exe = proc_path(win_pid(h))
        if os.path.basename(exe).lower() in WECHAT_EXES:
            w, hh = win_size(h)
            if w > 200 and hh > 200:
                out.append(h)
                kids = []

                def cb2(k, _):
                    kids.append(k)
                    return True

                user32.EnumChildWindows(h, WNDENUMPROC(cb2), 0)
                out.extend(kids)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def dump(hwnd, ACC, objid, max_depth=6, max_nodes=400):
    counter = [0]
    out = []
    stats = {}

    try:
        p = ctypes.POINTER(ACC.IAccessible)()
        oleacc.AccessibleObjectFromWindow(ctypes.c_void_p(hwnd), objid,
                                          ctypes.byref(ACC.IAccessible._iid_),
                                          ctypes.byref(p))
    except Exception as e:
        return ["AccessibleObjectFromWindow 失败：%r" % (e,)], {}

    if not p:
        return ["AccessibleObjectFromWindow 返回空指针"], {}

    try:
        n0 = p.accChildCount
    except Exception as e:
        n0 = "err:%r" % (e,)
    out.append("顶层 accChildCount = %s" % n0)
    if isinstance(n0, int):
        try:
            out.append("顶层 accName = %r" % (p.accName(0) or "",))
        except Exception as e:
            out.append("顶层 accName 读不到 %r" % (e,))

    def rec(node, depth, path):
        if counter[0] >= max_nodes or depth > max_depth:
            return
        try:
            n = node.accChildCount
        except Exception:
            n = 0
        if not isinstance(n, int):
            return
        for i in range(1, n + 1):
            counter[0] += 1
            if counter[0] > max_nodes:
                return
            try:
                nm = node.accName(i) or ""
                ro = node.accRole(i)
                st = node.accState(i)
                ch = node.accChild(i)
            except Exception:
                continue
            rn = ROLE.get(ro, str(ro))
            stats[rn] = stats.get(rn, 0) + 1
            out.append("%s[%s] role=%-11s state=0x%X name=%r" % ("  " * depth, path + [i], rn, st, nm[:60]))
            if ch is not None and depth < max_depth:
                try:
                    rec(ch, depth + 1, path + [i])
                except Exception:
                    pass

    try:
        rec(p, 0, [])
    except Exception as e:
        out.append("遍历中断 %r" % e)
    return out, stats


def main():
    import comtypes.client
    try:
        comtypes.client.GetModule("oleacc.dll")
    except Exception:
        pass
    from comtypes.gen import Accessibility as ACC

    log("微信桥 · MSAA 专项复测 · %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log("")

    ts = targets()
    log("候选窗口：%d 个" % len(ts))
    for h in ts:
        log("   hwnd=%-10s class=%-32s size=%sx%s title=%r" % (
            h, win_class(h), win_size(h)[0], win_size(h)[1], win_text(h)))
    log("")

    if not ts:
        log("!! 没有微信窗口（先打开微信）")

    for h in ts:
        for label, objid in (("OBJID_CLIENT", OBJID_CLIENT), ("OBJID_WINDOW", OBJID_WINDOW)):
            log("=" * 78)
            log("hwnd=%s class=%s · %s" % (h, win_class(h), label))
            log("=" * 78)
            try:
                out, st = dump(h, ACC, objid)
            except Exception as e:
                out, st = ["抛异常 %r" % e], {}
            log("role 统计：%s" % st)
            log("节点数：%d" % len(out))
            for l in out[:70]:
                log("   " + l)
            log("")

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("report: %s (%d lines)" % (REPORT, len(lines)))


if __name__ == "__main__":
    main()
