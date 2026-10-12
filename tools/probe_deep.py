# -*- coding: utf-8 -*-
"""
微信桥 · 深层复测探针（只读）

在第一版探针（probe_wechat.py，UIA RawView 逐层遍历）判"不可行"之后，补三把没试过的钥匙：

  A. UIA ControlViewWalker 走一遍（RawView 死了不代表 ControlView 也死）
  B. MSAA / IAccessible（oleacc）：Qt 在 Windows 上同时挂 MSAA 和 UIA，
     如果 UIA 树是空的但 MSAA 有内容，这条路还能救
  C. GetFocusedElement 轮询：把"焦点元素"读出来 —— 树很浅的应用常常焦点元素是可读的。
     运行期间请把光标点进微信的输入框，脚本会抓它。

结果写 deep_report.txt（UTF-8）。
用法：python probe_deep.py [--poll 20]   # poll = 焦点轮询秒数
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import datetime
import os
import time

BASE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(BASE, "deep_report.txt")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
WECHAT_EXES = ("weixin.exe", "wechat.exe")

OBJID_CLIENT = 0xFFFFFFFC

CT = {
    50000: "Button", 50002: "CheckBox", 50003: "ComboBox", 50004: "Edit",
    50005: "Hyperlink", 50006: "Image", 50007: "ListItem", 50008: "List",
    50011: "MenuItem", 50013: "RadioButton", 50020: "Text", 50023: "Tree",
    50024: "TreeItem", 50025: "Custom", 50026: "Group", 50030: "Document",
    50032: "Window", 50033: "Pane", 50034: "Header", 50036: "Table",
    50037: "TitleBar",
}

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
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    return (0, 0, 0, 0)


def collect():
    """返回微信的全部窗口（顶层 + 子窗口），面积降序。"""
    tops = []

    def cb(h, _):
        exe = proc_path(win_pid(h))
        if os.path.basename(exe).lower() in WECHAT_EXES:
            tops.append(h)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)

    out = []

    def add(h, kind):
        x, y, w, hh = win_rect(h)
        out.append({"hwnd": h, "kind": kind, "class": win_class(h), "title": win_text(h),
                    "visible": bool(user32.IsWindowVisible(h)), "rect": (x, y, w, hh),
                    "area": w * hh})

    for t in tops:
        add(t, "top")
        kids = []

        def cb2(hh, _):
            kids.append(hh)
            return True

        user32.EnumChildWindows(t, WNDENUMPROC(cb2), 0)
        for k in kids:
            add(k, "child")

    out.sort(key=lambda w: w["area"], reverse=True)
    return out


# ------------------------------------------------------------ A. UIA

def uia_setup():
    import comtypes.client
    try:
        from comtypes.gen import UIAutomationClient as UIA
    except Exception:
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA
    return comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation), UIA


def walk_with(uia, walker, hwnd, max_depth=8, max_nodes=600, tag=""):
    out, stats, counter = [], {}, [0]
    try:
        el = uia.ElementFromHandle(hwnd)
    except Exception as e:
        return ["ElementFromHandle 失败 %r" % e], {}

    def rec(e, depth):
        if counter[0] >= max_nodes or depth > max_depth:
            return
        try:
            child = walker.GetFirstChildElement(e)
        except Exception as e:
            out.append("  " * depth + "GetFirstChildElement 失败 %r" % e)
            return
        while child is not None:
            counter[0] += 1
            if counter[0] > max_nodes:
                return
            try:
                ct = child.CurrentControlType
                nm = (child.CurrentName or "").strip()
                cls = child.CurrentClassName
                aid = child.CurrentAutomationId
            except Exception:
                ct, nm, cls, aid = 0, "", "<err>", ""
            ctn = CT.get(ct, str(ct))
            stats[ctn] = stats.get(ctn, 0) + 1
            out.append("%s%-9s name=%r class=%r aid=%r" % ("  " * depth, ctn, nm[:50], cls[:36], aid[:36]))
            if depth < max_depth:
                rec(child, depth + 1)
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception as e:
                out.append("  " * depth + "GetNextSiblingElement 失败 %r" % e)
                child = None

    rec(el, 0)
    return out, stats


def findall_counts(uia, UIA, hwnd):
    res = {}
    try:
        el = uia.ElementFromHandle(hwnd)
    except Exception as e:
        return {"err": repr(e)}
    for name, ctid in (("Edit", 50004), ("ListItem", 50007), ("Button", 50000),
                       ("Text", 50020), ("Document", 50030)):
        try:
            cond = uia.CreatePropertyCondition(30003, ctid)
            res[name] = el.FindAll(UIA.TreeScope_Subtree, cond).Length
        except Exception as e:
            res[name] = "err:%r" % (e,)
    return res


def dump_focused(uia, UIA, tag=""):
    try:
        el = uia.GetFocusedElement()
    except Exception as e:
        return "%s GetFocusedElement 失败 %r" % (tag, e)
    try:
        ct = CT.get(el.CurrentControlType, str(el.CurrentControlType))
        s = "%s 焦点元素: %-8s name=%r class=%r aid=%r" % (
            tag, ct, (el.CurrentName or "")[:50], (el.CurrentClassName or "")[:40],
            (el.CurrentAutomationId or "")[:40])
        # 往上追 4 层父节点，看能不能摸到微信
        p = el
        for _ in range(4):
            try:
                wk = uia.ControlViewWalker
                p = wk.GetParentElement(p)
                if p is None:
                    break
                s += "\n%s   ^父: %-8s name=%r class=%r aid=%r" % (
                    tag, CT.get(p.CurrentControlType, str(p.CurrentControlType)),
                    (p.CurrentName or "")[:40], (p.CurrentClassName or "")[:36],
                    (p.CurrentAutomationId or "")[:30])
            except Exception:
                break
        return s
    except Exception as e:
        return "%s 读焦点元素属性失败 %r" % (tag, e)


# ------------------------------------------------------------ B. MSAA

def msaa_setup():
    import comtypes.client
    import ctypes as C
    try:
        comtypes.client.GetModule("oleacc.dll")
    except Exception:
        pass
    from comtypes.gen import Accessibility as ACC
    oleacc = C.WinDLL("oleacc")
    return ACC, oleacc


def msaa_dump(hwnd, ACC, oleacc, max_depth=5, max_nodes=200):
    out, counter = [], [0]
    try:
        p = ctypes.POINTER(ACC.IAccessible)()
        oleacc.AccessibleObjectFromWindow.argtypes = [
            wt.HWND, wt.DWORD, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.POINTER(ACC.IAccessible))]
        oleacc.AccessibleObjectFromWindow.restype = ctypes.c_long
        hr = oleacc.AccessibleObjectFromWindow(hwnd, OBJID_CLIENT,
                                              ctypes.byref(ACC.IAccessible._iid_),
                                              ctypes.byref(p))
        if hr != 0 or not p:
            return ["AccessibleObjectFromWindow hr=0x%08X（拿不到 MSAA 接口）" % (hr & 0xFFFFFFFF)], {}
    except Exception as e:
        return ["MSAA 初始化失败 %r" % e], {}

    stats = {}

    def rec(node, depth, idx_path):
        if counter[0] >= max_nodes or depth > max_depth:
            return
        try:
            n = node.accChildCount
        except Exception:
            n = 0
        if n is None:
            n = 0
        for i in range(1, n + 1):
            counter[0] += 1
            if counter[0] > max_nodes:
                return
            try:
                nm = node.accName(i) or ""
                ro = node.accRole(i)
                ch = node.accChild(i)
            except Exception:
                continue
            rn = ROLE.get(ro, str(ro))
            stats[rn] = stats.get(rn, 0) + 1
            out.append("%s[%s] role=%-11s name=%r" % ("  " * depth, idx_path + [i], rn, nm[:60]))
            if ch is not None and depth < max_depth:
                try:
                    rec(ch, depth + 1, idx_path + [i])
                except Exception:
                    pass

    try:
        rec(p, 0, [])
    except Exception as e:
        out.append("MSAA 遍历中断 %r" % e)
    return out, stats


# ------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", type=int, default=20, help="焦点元素轮询秒数")
    ap.add_argument("--nodirect", action="store_true")
    args = ap.parse_args()

    log("微信桥深层复测 · %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log("")

    wins = collect()
    wx = [w for w in wins if w["class"] != ""]
    if not wx:
        log("!! 没有微信窗口，退出")
        with open(REPORT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return

    log("=" * 78)
    log("窗口清单（顶层 + 子窗口，面积降序）")
    log("=" * 78)
    for w in wx[:12]:
        log("  [%-5s] hwnd=%-10s vis=%-5s %4dx%-5d class=%-30s title=%r" % (
            w["kind"], w["hwnd"], w["visible"], w["rect"][2], w["rect"][3], w["class"], w["title"]))

    main_w = None
    for w in wx:
        if w["visible"] and w["rect"][2] > 400 and w["rect"][3] > 300:
            main_w = w
            break
    if main_w is None:
        log("!! 没有可见的大窗口，后面的 UIA/MSAA 用面积最大的那个")
        main_w = wx[0]
    log("")
    log(">>> 复测对象：hwnd=%s class=%s %dx%d title=%r" % (
        main_w["hwnd"], main_w["class"], main_w["rect"][2], main_w["rect"][3], main_w["title"]))

    try:
        uia, UIA = uia_setup()
    except Exception as e:
        log("UIA 初始化失败 %r" % e)
        uia = None

    if uia is not None:
        for tag, walker in (("A1 RawViewWalker", uia.RawViewWalker),
                            ("A2 ControlViewWalker", uia.ControlViewWalker)):
            log("")
            log("=" * 78)
            log("%s · hwnd=%s" % (tag, main_w["hwnd"]))
            log("=" * 78)
            out, st = walk_with(uia, walker, main_w["hwnd"])
            log("统计：%s" % st)
            log("节点数：%d" % len(out))
            for l in out[:80]:
                log("   " + l)
            log("")
            log("FindAll 按类型直搜：%s" % findall_counts(uia, UIA, main_w["hwnd"]))

        # 子窗口也搜一遍
        kids = [w for w in wx if w["kind"] == "child" and w["area"] > 100000]
        for k in kids[:2]:
            log("")
            log("=" * 78)
            log("A3 子窗口 hwnd=%s class=%s" % (k["hwnd"], k["class"]))
            log("=" * 78)
            out, st = walk_with(uia, uia.ControlViewWalker, k["hwnd"])
            log("统计：%s  节点数：%d" % (st, len(out)))
            for l in out[:40]:
                log("   " + l)
            log("FindAll：%s" % findall_counts(uia, UIA, k["hwnd"]))

        log("")
        log("=" * 78)
        log("C. 焦点元素轮询（%ds）—— 请在这期间把光标点进微信输入框" % args.poll)
        log("=" * 78)
        seen = set()
        t0 = time.time()
        while time.time() - t0 < args.poll:
            s = dump_focused(uia, UIA, "t=%.1f" % (time.time() - t0))
            if s not in seen:
                seen.add(s)
                log(s)
                log("")
            time.sleep(0.5)
        if not seen:
            log("（一次都没读到）")

    log("")
    log("=" * 78)
    log("B. MSAA / IAccessible 复测 · hwnd=%s" % main_w["hwnd"])
    log("=" * 78)
    try:
        ACC, oleacc = msaa_setup()
        out, st = msaa_dump(main_w["hwnd"], ACC, oleacc)
        log("统计：%s" % st)
        log("节点数：%d" % len(out))
        for l in out[:100]:
            log("   " + l)
    except Exception as e:
        log("MSAA 复测失败 %r" % e)

    log("")
    log("复测结束。")

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("report: %s (%d lines)" % (REPORT, len(lines)))


if __name__ == "__main__":
    main()
