# -*- coding: utf-8 -*-
"""
微信桥 · 可行性探针（只读，不改微信任何东西）

目的：在写桥之前，实测这台机器上的微信 4.1.15.13 到底能不能被驱动。
一次验两条候选子路线：

  路线甲（UIA / 控件树）：靠 mmui 控件树找到「输入框」和「文件传输助手」会话项。
  路线乙（键盘流 / 免控件树）：只要存在标题带「文件传输助手」的独立聊天窗口，
                              就能靠 Ctrl+V + Enter 发送，完全不碰控件树。

探针动作：
  第 1 级  Win32 枚举顶层窗口（exe 路径 / 标题 / 类名 / 是否可见 / 位置）
  第 2 级  UIA 默认状态读一次微信主窗口（预期：树很浅 = 被藏起来了）
  第 3 级  打开系统「读屏器正在运行」标志 -> 等 6 秒 -> 再用 RawView 逐层遍历
  收尾     还原读屏标志（可加 --keep 保留）

结果写 tools/probe_report.txt（UTF-8），不打印到控制台。

用法：
  python probe_wechat.py            # 跑完自动还原读屏标志
  python probe_wechat.py --keep     # 保留读屏标志（如果你要接着用 Inspect 看树）
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import datetime
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(BASE, "probe_report.txt")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

WECHAT_EXES = ("weixin.exe", "wechat.exe")

lines = []
KEEP_READER = False


def log(s=""):
    lines.append(str(s))


# ---------------------------------------------------------------- 第 1 级：Win32

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
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def win_rect(h):
    r = wt.RECT()
    if user32.GetWindowRect(h, ctypes.byref(r)):
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    return (0, 0, 0, 0)


def enum_top_windows():
    out = []

    def cb(h, _):
        pid = win_pid(h)
        exe = proc_path(pid)
        out.append({
            "hwnd": h,
            "pid": pid,
            "exe": exe,
            "exe_name": os.path.basename(exe).lower() if exe else "",
            "class": win_class(h),
            "title": win_text(h),
            "visible": bool(user32.IsWindowVisible(h)),
            "rect": win_rect(h),
            "kind": "top",
        })
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)

    # 子窗口也要收：微信 4.x 的真界面可能画在子 HWND（如 MMUIRenderSubWindowHW）里
    tops = [w for w in out if w["exe_name"] in WECHAT_EXES]
    for t in tops:
        kids = []

        def cb2(hh, _):
            kids.append(hh)
            return True

        user32.EnumChildWindows(t["hwnd"], WNDENUMPROC(cb2), 0)
        for hh in kids:
            out.append({
                "hwnd": hh,
                "pid": t["pid"],
                "exe": t["exe"],
                "exe_name": t["exe_name"],
                "class": win_class(hh),
                "title": win_text(hh),
                "visible": bool(user32.IsWindowVisible(hh)),
                "rect": win_rect(hh),
                "kind": "child of %s" % t["hwnd"],
            })
    return out


def dpi_info():
    try:
        sysdpi = user32.GetDpiForSystem()
    except Exception:
        sysdpi = None
    try:
        mon = user32.GetSystemMetrics(80)  # SM_CMONITORS
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
    except Exception:
        mon, sw, sh = None, None, None
    return sysdpi, mon, sw, sh


def dump_level1(wins):
    log("=" * 78)
    log("第 1 级 · Win32 顶层窗口枚举")
    log("=" * 78)

    wx = [w for w in wins if w["exe_name"] in WECHAT_EXES]
    if not wx:
        log("!! 没找到微信进程的窗口 —— 微信没启动 / 没登录。")
        log("   先把微信打开并登录，再重跑本探针。")
        return []

    vers = sorted({w["exe"] for w in wx})
    log("微信进程的 exe：")
    for v in vers:
        log("   " + v)
    log("")

    log("微信的全部窗口（顶层 + 子窗口，按面积从大到小）：")
    wx.sort(key=lambda w: w["rect"][2] * w["rect"][3], reverse=True)
    for w in wx:
        x, y, cw, ch = w["rect"]
        log("  [%-9s] hwnd=%-10s vis=%-5s class=%-26s %4dx%-6d @(%d,%d)  title=%r" % (
            w.get("kind", "top"), w["hwnd"], w["visible"], w["class"], cw, ch, x, y, w["title"]))

    sysdpi, mon, sw, sh = dpi_info()
    log("")
    log("显示环境：系统DPI=%s  显示器数=%s  主屏=%sx%s" % (sysdpi, mon, sw, sh))
    log("   （退化成 Qt51514QWindowIcon 的已知诱因：DPI 缩放 / 多屏混合 DPI / 显卡驱动 / GPU 渲染）")

    log("")
    big = [w for w in wx if w["rect"][2] > 300 and w["rect"][3] > 200]
    log("宽>300 且高>200 的候选主窗口：%d 个" % len(big))
    mmui = [w for w in big if w["class"] == "mmui::MainWindow"]
    qt = [w for w in big if w["class"].startswith("Qt")]
    log("   其中 class == 'mmui::MainWindow' ：%d 个 %s" % (len(mmui), "✓ 正常" if mmui else "✗ 缺失"))
    log("   其中 class 以 'Qt' 开头（退化包装窗）  ：%d 个 %s" % (len(qt), [w["class"] for w in qt]))
    if not mmui:
        log("   ⚠ 提示：主窗口类名不是 mmui::MainWindow。")
        log("     若微信此刻还在扫码登录页，这属正常现象 —— 登录后重跑本探针才作数。")

    # 路线乙：找标题带「文件传输助手」的独立聊天窗口
    log("")
    log("-" * 78)
    log("路线乙判据 · 有没有「标题带会话名」的独立窗口（免控件树键盘流用）")
    log("-" * 78)
    hit = [w for w in wx if "文件传输助手" in w["title"]]
    for w in wx:
        if w["title"] and w["title"] not in ("微信", "WeChat", "Weixin"):
            log("   标题非主窗口的：class=%-26s title=%r vis=%s" % (w["class"], w["title"], w["visible"]))
    if hit:
        log("   ✓ 找到 %d 个标题含「文件传输助手」的窗口：路线乙可行" % len(hit))
    else:
        log("   ✗ 没有标题含「文件传输助手」的窗口：")
        log("     （可能只是还没在微信里双击打开过文件传输助手的独立窗口，不代表路线乙不可行）")
    return wx


# ---------------------------------------------------------------- 第 2/3 级：UIA

def uia_root():
    import comtypes.client
    try:
        from comtypes.gen import UIAutomationClient as UIA
    except Exception:
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA
    uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
    return uia, UIA


CT = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
}


def walk(uia, UIA, hwnd, max_depth=6, max_nodes=500):
    walker = uia.RawViewWalker
    try:
        el = uia.ElementFromHandle(hwnd)
    except Exception as e:
        return ["ElementFromHandle 失败: %r" % e], {}

    out = []
    stats = {}
    counter = [0]

    def rec(e, depth):
        if counter[0] >= max_nodes or depth > max_depth:
            return
        child = walker.GetFirstChildElement(e)
        while child is not None:
            counter[0] += 1
            if counter[0] > max_nodes:
                out.append("  " * depth + "... (超过 %d 个节点，截断)" % max_nodes)
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
            out.append("%s%-9s name=%r class=%r aid=%r" % ("  " * depth, ctn, nm[:60], cls[:40], aid[:40]))
            if depth < max_depth:
                rec(child, depth + 1)
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                child = None

    try:
        rec(el, 0)
    except Exception as e:
        out.append("遍历中断: %r" % e)
    return out, stats


def findall_probe(uia, UIA, hwnd):
    """绕开逐层遍历（可能中途报 COMError），直接按控件类型整棵子树搜。"""
    res = {}
    try:
        el = uia.ElementFromHandle(hwnd)
    except Exception as e:
        return {"err": repr(e)}
    for name, ctid in (("Edit", 50004), ("ListItem", 50007), ("Text", 50020), ("Button", 50000)):
        try:
            cond = uia.CreatePropertyCondition(30003, ctid)  # UIA_ControlTypePropertyId
            found = el.FindAll(UIA.TreeScope_Subtree, cond)
            res[name] = found.Length
        except Exception as e:
            res[name] = "err:%r" % (e,)
    return res


def pick_candidates(wx, limit=4):
    """挑做 UIA 遍历的窗口：先 mmui::MainWindow，再按面积，最多 limit 个。"""
    vis = [w for w in wx if w["visible"] and w["rect"][2] > 150 and w["rect"][3] > 150]
    vis.sort(key=lambda w: (w["class"] != "mmui::MainWindow", -(w["rect"][2] * w["rect"][3])))
    return vis[:limit]


def uia_phase(wx, delay):
    log("")
    log("=" * 78)
    log("第 2 / 3 级 · UIA 控件树（读屏标志 前 -> 后）")
    log("=" * 78)

    log("")
    cands = pick_candidates(wx)
    log("UIA 候选窗口（按面积降序，最多 3 个）：")
    for c in cands:
        log("   hwnd=%-10s class=%-26s %4dx%-6d title=%r" % (
            c["hwnd"], c["class"], c["rect"][2], c["rect"][3], c["title"]))
    if not cands:
        log("!! 没有可见的微信窗口，跳过 UIA 部分（先把微信窗口显示出来再跑）")
        return

    try:
        uia, UIA = uia_root()
    except Exception as e:
        log("!! 初始化 UIA 客户端失败：%r" % e)
        return

    SPI_GETSCREENREADER = 0x0046
    SPI_SETSCREENREADER = 0x0047
    SPIF_UPDATEINIFILE = 0x01
    SPIF_SENDCHANGE = 0x02

    pv = wt.BOOL()
    ok = user32.SystemParametersInfoW(SPI_GETSCREENREADER, 0, ctypes.byref(pv), 0)
    orig = pv.value if ok else None
    log("读屏标志原始值：%r（取不到则为 None）" % orig)

    main = cands[0]
    log("")
    log("--- 2a. 打开读屏标志【之前】的树（预期：很浅）---")
    before, s1 = walk(uia, UIA, main["hwnd"])
    log("节点统计：%s" % s1)
    log("节点总数：%d" % len(before))
    for l in before[:40]:
        log("   " + l)

    log("")
    log("--- 2b. 打开读屏标志，等待 %ds 让微信重建无障碍树 ---" % delay)
    user32.SystemParametersInfoW(SPI_SETSCREENREADER, 1, ctypes.byref(wt.BOOL(True)),
                                 SPIF_UPDATEINIFILE | SPIF_SENDCHANGE)
    time.sleep(delay)

    all_stats = {}
    for idx, c in enumerate(cands):
        after, s2 = walk(uia, UIA, c["hwnd"], max_depth=7, max_nodes=700)
        log("")
        log("--- 2c-%d. 读屏标志打开后 · hwnd=%s class=%s %dx%d title=%r ---" % (
            idx + 1, c["hwnd"], c["class"], c["rect"][2], c["rect"][3], c["title"]))
        log("节点统计：%s" % s2)
        log("节点总数：%d" % len(after))
        for l in after[:120]:
            log("   " + l)
        for k, v in s2.items():
            all_stats[k] = all_stats.get(k, 0) + v
        fa = findall_probe(uia, UIA, c["hwnd"])
        log("FindAll 按类型直搜（绕开遍历中断）：%s" % fa)
        log("")
        if idx == 0:
            joined0 = "\n".join(after)

    joined = "\n".join(lines[-2000:])
    log("")
    log("-" * 78)
    log("路线甲判据 · 关键控件是否出现（所有候选窗口合计）")
    log("-" * 78)
    checks = [
        ("输入框 Edit", all_stats.get("Edit", 0) > 0),
        ("会话项 ListItem", all_stats.get("ListItem", 0) > 0),
        ("chat_input_field 输入框", "chat_input_field" in joined),
        ("session_item_ 会话项", "session_item_" in joined),
        ("mmui:: 类名出现", "mmui::" in joined),
    ]
    for name, okc in checks:
        log("   %s %s" % ("✓" if okc else "✗", name))
    verdict = "可行" if (all_stats.get("Edit", 0) > 0 or "chat_input_field" in joined) else "不可行"
    log("")
    log(">>> 路线甲（UIA 控件树）初步结论：%s" % verdict)

    if KEEP_READER:
        log("读屏标志【保留为开】（--keep）：要还原就再跑一次不带 --keep 的探针")
    else:
        user32.SystemParametersInfoW(SPI_SETSCREENREADER, 0 if orig is None else int(orig),
                                     ctypes.byref(wt.BOOL(bool(0 if orig is None else orig))),
                                     SPIF_UPDATEINIFILE | SPIF_SENDCHANGE)
        log("读屏标志已还原为 %r" % orig)


def main():
    global KEEP_READER
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留读屏标志不还原")
    ap.add_argument("--delay", type=int, default=6)
    args = ap.parse_args()
    KEEP_READER = bool(args.keep)

    log("微信桥可行性探针 · %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log("")

    wins = enum_top_windows()
    wx = dump_level1(wins)
    if wx:
        uia_phase(wx, args.delay)

    log("")
    log("探针结束。")

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("report written: %s (%d lines)" % (REPORT, len(lines)))


if __name__ == "__main__":
    main()
