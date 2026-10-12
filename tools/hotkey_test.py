# -*- coding: utf-8 -*-
r"""微信桥 · 全局热键「真机端到端」测试

和 hotkey_scan.py 的区别：
    scan  只是"试着注册一下"，证明这个组合没被别人占；
    本脚本则真的建窗口 -> 装上热键 -> **真按一次** -> 确认回调真被触发。

三种测试方式（可叠加）：
    1. keybd_event 注入     —— 老 API，wx_bridge.py 抓选中文字就用它
    2. SendInput 注入       —— 新 API，注入更"正宗"
    3. --human N            —— 等**真人**按 N 秒（最权威：排除注入被拦的可能）

用法：
    python hotkey_test.py                          # 默认 Ctrl+Alt+B，两种注入都试
    python hotkey_test.py Ctrl+Alt+1
    python hotkey_test.py Ctrl+Alt+B --human 90    # 等你亲手按 90 秒

顺带取证：注入后立刻读 GetAsyncKeyState，证明"按键到底有没有真的进入系统"。
结果写 hotkey_test.txt（UTF-8），不依赖控制台编码。
"""
import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import wx_tray as T          # noqa: E402

OUT = BASE / "hotkey_test.txt"

u32 = ctypes.windll.user32
u32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_uint,
                            ctypes.c_void_p]
u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
u32.GetAsyncKeyState.restype = ctypes.c_short

KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1


# ---------------------------------------------------------------- SendInput
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


u32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
u32.SendInput.restype = wt.UINT

MODS = {
    "ctrl": T.MOD_CONTROL, "control": T.MOD_CONTROL,
    "alt": T.MOD_ALT, "shift": T.MOD_SHIFT, "win": T.MOD_WIN,
}


def parse_label(label):
    """'Ctrl+Alt+B' -> (mods, vk, [修饰键vk...], 规范名)"""
    mods = 0
    vk = None
    pretty = []
    for raw in label.replace("＋", "+").split("+"):
        tok = raw.strip()
        if not tok:
            continue
        low = tok.lower()
        if low in MODS:
            mods |= MODS[low]
            pretty.append("Ctrl" if low in ("ctrl", "control") else tok.capitalize())
            continue
        if len(tok) == 1 and tok.isalpha():
            vk = ord(tok.upper())
            pretty.append(tok.upper())
        elif len(tok) == 1 and tok.isdigit():
            vk = 0x30 + int(tok)
            pretty.append(tok)
        elif low.startswith("f") and low[1:].isdigit():
            vk = 0x6F + int(low[1:])
            pretty.append(tok.upper())
        elif "小键盘" in tok:
            d = "".join(c for c in tok if c.isdigit())
            vk = 0x60 if d == "0" else 0x60 + int(d)
            pretty.append(tok)
        else:
            raise ValueError("看不懂的按键：%r" % tok)
    if vk is None:
        raise ValueError("没有解析出主键：%r" % label)
    mod_vks = [m for m in (T.MOD_CONTROL, T.MOD_ALT, T.MOD_SHIFT, T.MOD_WIN)
               if mods & m]
    return mods, vk, mod_vks, "+".join(pretty)


def key_state(vk):
    return "按下" if (u32.GetAsyncKeyState(vk) & 0x8000) else "弹起"


def inject_keybd(mod_vks, vk, hold=0.12):
    """方式 1：keybd_event"""
    for m in mod_vks:
        u32.keybd_event(m, 0, 0, None)
        time.sleep(0.03)
    u32.keybd_event(vk, 0, 0, None)
    time.sleep(hold)                      # 按住一会儿，某些程序要求有"持续按下"的过程
    probe = "Ctrl=%s Alt=%s 主键=%s" % (
        key_state(T.MOD_CONTROL), key_state(T.MOD_ALT), key_state(vk))
    u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, None)
    for m in reversed(mod_vks):
        time.sleep(0.03)
        u32.keybd_event(m, 0, KEYEVENTF_KEYUP, None)
    return probe


def inject_sendinput(mod_vks, vk, hold=0.12):
    """方式 2：SendInput"""
    def send(vks, up=False):
        n = len(vks)
        arr = (INPUT * n)()
        for i, v in enumerate(vks):
            arr[i].type = INPUT_KEYBOARD
            arr[i].u.ki.wVk = v
            arr[i].u.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
        u32.SendInput(n, arr, ctypes.sizeof(INPUT))

    send(mod_vks)
    time.sleep(0.03)
    send([vk])
    time.sleep(hold)
    probe = "Ctrl=%s Alt=%s 主键=%s" % (
        key_state(T.MOD_CONTROL), key_state(T.MOD_ALT), key_state(vk))
    send([vk], up=True)
    time.sleep(0.03)
    send(list(reversed(mod_vks)), up=True)
    return probe


def main():
    argv = sys.argv[1:]
    label_arg = None
    human = 0
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--human":
            human = int(argv[i + 1]) if i + 1 < len(argv) else 60
            i += 2
            continue
        if a.startswith("--human="):
            human = int(a.split("=", 1)[1])
            i += 1
            continue
        if not a.startswith("--"):
            label_arg = label_arg or a
        i += 1
    label_arg = label_arg or "Ctrl+Alt+B"

    lines = []

    def flush():
        try:
            OUT.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass

    def out(s=""):
        lines.append(s)
        try:
            print(s, flush=True)
        except Exception:
            pass
        flush()          # 边跑边落盘，长等待时也能看到进度

    try:
        mods, vk, mod_vks, pretty = parse_label(label_arg)
    except Exception as e:
        out("参数解析失败：%s" % e)
        OUT.write_text("\n".join(lines), encoding="utf-8")
        return

    out("=" * 62)
    out("微信桥 · 热键真机端到端测试")
    out("=" * 62)
    out("目标组合：%s   模式：%s" % (
        pretty, "等真人按 %ds" % human if human else "自动注入"))
    out("")

    T.HOTKEY_CANDIDATES = [(mods, vk, pretty)]      # 只测这一个，不动源码

    fired = []
    notes = []

    def on_hotkey():
        fired.append((time.time(), threading.current_thread().name))

    tray = T.TrayHotkey(
        tooltip="热键测试（不挂图标）",
        icon_path=str(BASE / "wx.ico"),
        on_hotkey=on_hotkey,
        on_message=notes.append,
        show_icon=False,          # 不往托盘里塞重复图标
        manage_console=False,     # 不动调用者的控制台
    )

    out("--- 1. 建窗口并注册热键 ---")
    if not tray.start(timeout=6):
        out("   ❌ 窗口/热键线程没起来，测试中止")
        OUT.write_text("\n".join(lines), encoding="utf-8")
        return
    for n in notes:
        out("   · %s" % n)
    out("   实际注册到的热键：%s" % (tray.hotkey_name or "（无）"))
    if not tray.hotkey_ok:
        out("")
        out(">>> 结论：❌ 注册失败，这个组合用不了")
        OUT.write_text("\n".join(lines), encoding="utf-8")
        return

    time.sleep(0.6)          # 让消息循环进入 GetMessage

    if human:
        out("")
        out("--- 2. 等真人按键（%d 秒）---" % human)
        out("   👉 请现在亲手按一次 %s" % pretty)
        deadline = time.time() + human
        while time.time() < deadline and not fired:
            time.sleep(0.2)
    else:
        for name, fn in (("keybd_event", inject_keybd),
                         ("SendInput", inject_sendinput)):
            out("")
            out("--- 2. %s 注入 ---" % name)
            try:
                probe = fn(mod_vks, vk)
                out("   注入瞬间键状态取证：%s" % probe)
            except Exception as e:
                out("   注入本身报错：%r" % e)
            deadline = time.time() + 3.0
            while time.time() < deadline and not fired:
                time.sleep(0.1)
            if fired:
                out("   ✅ 触发成功（方式：%s）" % name)
                break
            out("   ❌ 没触发")
            time.sleep(0.5)

    out("")
    out("--- 3. 判定 ---")
    if fired:
        out("   ✅ 回调被触发：系统确实把 WM_HOTKEY 投递到了我们的窗口")
        out("   触发次数：%d，回调线程：%s" % (len(fired), fired[0][1]))
        out("")
        out(">>> 结论：✅ %s 完全可用（注册成功 + 真实按键能触发）" % pretty)
    else:
        out("   ❌ 注册成功，但一直没等到触发")
        out("   若本脚本是自动注入模式，不能据此判定热键有问题 ——")
        out("   自动化沙箱可能拦掉模拟输入；请用 --human 模式让真人按一次复核。")
        out("")
        out(">>> 结论：⚠️ 待复核（注册没问题，触发未验证）")

    out("")
    out("--- 4. 释放资源 ---")
    tray.cleanup()
    time.sleep(0.4)
    out("   热键已注销、窗口已释放，不留残留")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    out("")
    out("（完整结果已写入 %s）" % OUT)


if __name__ == "__main__":
    main()
