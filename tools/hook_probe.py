# -*- coding: utf-8 -*-
r"""低级键盘钩子探针 · 验证能不能抢在 QQ 前面处理 Ctrl+Alt+B

背景（2026-10-12 实测）：
    微信桥用 RegisterHotKey 注册 Ctrl+Alt+B，**注册成功**（注册失败才会报 1409，
    说明没有任何程序用这个 API 占着这个键）；但木木按下后，QQ 的截图也同时弹出。
    → 结论：QQ 是用**低级键盘钩子 / RawInput** 这类"比 RegisterHotKey 更早拿到按键"
      的机制响应的。RegisterHotKey 探测不到它 —— 这就是"检测说可用、实际被抢"的原因。

本探针做的事：
    装一个 WH_KEYBOARD_LL 钩子，命中 Ctrl+Alt+B 时按 --pass / --block 决定是否吞掉，
    然后木木按一下，看 QQ 的截图还弹不弹：
        不弹  → 钩子能压住 QQ → 微信桥可以改用钩子方案（Ctrl+Alt+B 能保住）
        还弹  → QQ 的监听比钩子更底层（RawInput / 设备层）→ 只能换键，
                 或者让木木在 QQ 设置里把截图的快捷键改走

用法：
    python hook_probe.py --block 60     # 拦截：吞掉 Ctrl+Alt+B
    python hook_probe.py --pass  60     # 放行：只观察（对照组）

产物： tools/hook_probe.txt
"""
import ctypes
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "hook_probe.txt"

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_QUIT = 0x0012

VK_CTRL = 0x11
VK_ALT = 0x12
VK_B = 0x42
VK_LCONTROL, VK_RCONTROL = 0xA2, 0xA3
VK_LALT, VK_RALT = 0xA4, 0xA5

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int,
                              wintypes.WPARAM, wintypes.LPARAM)

state = {"ctrl": False, "alt": False, "hits": 0, "blocked": 0}
BLOCK = False
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
                log("[%s] 钩子捕获 Ctrl+Alt+B 第 %d 次（模式=%s）"
                    % (time.strftime("%H:%M:%S"), state["hits"],
                       "拦截" if BLOCK else "放行"))
                if BLOCK:
                    state["blocked"] += 1
                    flush()
                    return 1          # 吞掉：后面的钩子和 RegisterHotKey 都收不到
                flush()
        except Exception as e:
            log("!! 回调异常：%r" % e)
            flush()
    return user32.CallNextHookEx(None, nCode, wParam, lParam)


hook_proc = HOOKPROC(handler)     # 必须保住引用，否则被 GC 回收会直接崩


def parse_args():
    args = sys.argv[1:]
    block = False
    secs = 60
    mode_seen = False
    for i, a in enumerate(args):
        if a in ("--block", "--pass"):
            block = (a == "--block")
            mode_seen = True
            if i + 1 < len(args) and args[i + 1].lstrip("-").isdigit():
                secs = int(args[i + 1])
    if not mode_seen:
        block = True          # 默认拦截（最关键的实验）
    return block, secs


def main():
    global BLOCK
    BLOCK, secs = parse_args()

    _lines[:] = [
        "=" * 62,
        "低级键盘钩子探针  模式=%s  时长=%ds"
        % ("拦截（吞掉 Ctrl+Alt+B）" if BLOCK else "放行（只观察）", secs),
        "=" * 62,
        "请按一次 Ctrl+Alt+B",
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
        log("-" * 62)
        log("时间到。共捕获 Ctrl+Alt+B  %d 次；其中被吞掉 %d 次"
            % (state["hits"], state["blocked"]))
        if state["hits"] == 0:
            log(">>> 没收到按键：可能是没按，或者钩子没装上")
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
