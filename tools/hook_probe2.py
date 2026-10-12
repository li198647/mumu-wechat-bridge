# -*- coding: utf-8 -*-
r"""钩子抢键实验 v2 · 带"窗口取证"版本

和 v1 的区别：
    v1 只能证明「钩子收到了按键、并且吞掉了」，但**看不到 QQ 有没有跟着弹截图**。
    v2 在捕获按键的瞬间，自动对比系统顶层窗口快照，把"新冒出来的窗口"揪出来 ——
    QQ 的截图一旦被唤起，就会在窗口列表里露馅。

判据：
    捕获后没有新窗口（尤其没有 QQ 的）  → 钩子压住了 QQ  → 微信桥可改用钩子方案
    捕获后冒出 QQ 的全屏/遮罩窗口        → QQ 比钩子更早拿到按键 → 只能换键或改 QQ 设置

用法： python hook_probe2.py --block 300
产物： tools/hook_probe2.txt
"""
import ctypes
import sys
import threading
import time
import winsound
from ctypes import wintypes
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "hook_probe2.txt"

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_QUIT = 0x0012

VK_CTRL, VK_ALT, VK_B = 0x11, 0x12, 0x42
VK_LCONTROL, VK_RCONTROL = 0xA2, 0xA3
VK_LALT, VK_RALT = 0xA4, 0xA5

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int,
                              wintypes.WPARAM, wintypes.LPARAM)

state = {"ctrl": False, "alt": False, "hits": 0, "blocked": 0}
BLOCK = True
_lines = []
_lock = threading.Lock()


def log(s=""):
    with _lock:
        _lines.append(str(s))


def flush():
    with _lock:
        text = "\n".join(_lines)
    try:
        OUT.write_text(text, encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 窗口取证
def enum_windows():
    out = []

    def cb(h, _):
        try:
            if not user32.IsWindowVisible(h):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            cls = ctypes.create_unicode_buffer(512)
            user32.GetClassNameW(h, cls, 512)
            ttl = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(h, ttl, 512)
            r = wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            w, hh = r.right - r.left, r.bottom - r.top
            out.append((int(pid.value), cls.value, ttl.value, w, hh))
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def diff_windows(before, after):
    bs = {(p, c, t) for p, c, t, w, h in before}
    adds = [x for x in after if (x[0], x[1], x[2]) not in bs]
    return adds


def investigate(tag):
    """捕获后 2.5 秒拍快照，和基准比，找出新冒出来的窗口"""
    threading.Thread(target=_investigate, args=(tag,), daemon=True).start()


def _investigate(tag):
    time.sleep(2.5)
    after = enum_windows()
    adds = diff_windows(BASE_SNAP, after)
    log("   ── 按键后窗口取证（%s）──" % tag)
    if not adds:
        log("   ✅ 没有任何新窗口冒出来（QQ 的截图看起来被压住了）")
    else:
        for p, c, t, w, h in adds:
            log("   ⚠️ 新窗口：pid=%-6s %dx%-5s class=%-28s title=%r"
                % (p, w, h, c, t))
    log("")
    flush()


BASE_SNAP = []


# ---------------------------------------------------------------- 钩子
def handler(nCode, wParam, lParam):
    if nCode == 0:
        try:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = int(kb.vkCode)
            down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            if vk in (VK_CTRL, VK_LCONTROL, VK_RCONTROL):
                state["ctrl"] = down
            elif vk in (VK_ALT, VK_LALT, VK_RALT):
                state["alt"] = down
            elif vk == VK_B and down and state["ctrl"] and state["alt"]:
                state["hits"] += 1
                log("[%s] ✅ 钩子捕获 Ctrl+Alt+B 第 %d 次（模式=%s）"
                    % (time.strftime("%H:%M:%S"), state["hits"],
                       "拦截" if BLOCK else "放行"))
                flush()
                try:
                    winsound.Beep(880, 120)      # 给木木一个"收到了"的听觉反馈
                except Exception:
                    pass
                investigate("第%d次" % state["hits"])
                if BLOCK:
                    state["blocked"] += 1
                    return 1                     # 吞掉
        except Exception as e:
            log("!! 回调异常：%r" % e)
            flush()
    return user32.CallNextHookEx(None, nCode, wParam, lParam)


hook_proc = HOOKPROC(handler)


def main():
    global BLOCK, BASE_SNAP
    args = sys.argv[1:]
    secs = 300
    for i, a in enumerate(args):
        if a in ("--block", "--pass"):
            BLOCK = (a == "--block")
            if i + 1 < len(args) and args[i + 1].isdigit():
                secs = int(args[i + 1])

    BASE_SNAP = enum_windows()

    _lines[:] = [
        "=" * 64,
        "钩子抢键实验 v2  模式=%s  时长=%ds" % (
            "拦截（吞掉 Ctrl+Alt+B）" if BLOCK else "放行（只观察）", secs),
        "=" * 64,
        "基准窗口快照：%d 个可见窗口" % len(BASE_SNAP),
        "",
        ">>> 请按一次 Ctrl+Alt+B （会听到一声蜂鸣 = 探针收到了）",
        "",
    ]
    flush()

    h = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hook_proc, None, 0)
    if not h:
        log("!! 装钩子失败，错误码 %s" % ctypes.get_last_error())
        flush()
        return
    log("钩子已装上，等待按键…")
    flush()

    tid = kernel32.GetCurrentThreadId()

    def stopper():
        time.sleep(secs)
        log("")
        log("-" * 64)
        log("时间到。共捕获 %d 次；吞掉 %d 次" % (state["hits"], state["blocked"]))
        if state["hits"] == 0:
            log(">>> 一次都没按到")
        flush()
        user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)

    threading.Thread(target=stopper, daemon=True).start()

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

    user32.UnhookWindowsHookEx(h)
    log("钩子已卸载")
    flush()


if __name__ == "__main__":
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                         ctypes.c_void_p, wintypes.DWORD]
    user32.CallNextHookEx.restype = ctypes.c_int
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    main()
