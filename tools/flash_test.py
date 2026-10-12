# -*- coding: utf-8 -*-
r"""闪烁实证：托盘图标真的在换吗？

两路取证：
  1. 记录每一次 NIM_MODIFY 切图标（谁、什么时候）——证明状态机在跑；
  2. 同时抓任务栏右下角截图——证明屏幕上真的看得出变化（如果图标没被折叠进 ^）。

⚠️ 会临时多出一个托盘图标（tooltip=「闪烁测试」），跑完自动移除。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wx_tray as T
from PIL import Image, ImageGrab

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "flash_test.txt")
lines = []


def L(s=""):
    lines.append(str(s))
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


_orig_set_icon = T.TrayHotkey._set_icon
_calls = []


def spy_set_icon(self, hicon):
    kind = "正常"
    if hicon == getattr(self, "_flash_hicon", None) and hicon:
        kind = "高亮"
    _calls.append((round(time.time() - T0, 2), kind, hicon))
    return _orig_set_icon(self, hicon)


T.TrayHotkey._set_icon = spy_set_icon

msgs = []
tray = T.TrayHotkey(
    tooltip="微信桥·闪烁测试（临时图标）",
    icon_path=os.path.join(BASE, "wx.ico"),
    icon_ok=os.path.join(BASE, "wx_ok.ico"),
    icon_err=os.path.join(BASE, "wx_err.ico"),
    show_icon=True,
    manage_console=False,
    hotkey=False,
    on_message=lambda s: msgs.append(str(s)),
)

L("=== 闪烁实证 ===")
ok = tray.start()
L("托盘启动: %s  图标已挂: %s" % (ok, tray._ok))
L("消息: %s" % msgs)
if not ok:
    L("托盘没起来，结束")
    sys.exit(1)

T0 = time.time()
frames = []


def shot(tag):
    im = ImageGrab.grab()
    box = (im.width - 420, im.height - 46, im.width, im.height)
    frames.append((tag, im.crop(box).resize((420 * 3, 46 * 3), Image.NEAREST)))


time.sleep(0.6)
shot("闪烁前")

L("")
L("--- 触发成功态闪烁（2.5 秒，每 0.5 秒切一次）---")
tray.flash("ok", seconds=2.5, interval=0.5)
for i in range(3):
    time.sleep(0.55)
    shot("闪烁中%.1fs" % ((i + 1) * 0.55))
time.sleep(1.6)
shot("闪烁后")

L("")
L("--- 触发失败态闪烁（1.5 秒，每 0.3 秒切一次）---")
tray.flash("err", seconds=1.5, interval=0.3)
time.sleep(1.0)
shot("红叉中")
time.sleep(1.2)
shot("红叉后")

L("")
L("切图标调用记录（相对秒 / 用的哪个图标）：")
for t, kind, h in _calls:
    L("   %6.2fs  %s" % (t, kind))

# 拼图：把所有帧竖着摞起来
w = max(f[1].width for f in frames)
h = sum(f[1].height + 26 for f in frames)
sheet = Image.new("RGB", (w, h), (250, 250, 250))
y = 0
for tag, img in frames:
    sheet.paste(img, (0, y))
    y += img.height + 26
out = os.path.join(BASE, "flash_frames.png")
sheet.save(out)
L("")
L("截图拼图 -> flash_frames.png（%d 帧）" % len(frames))

tray.cleanup()
time.sleep(0.5)
L("临时托盘图标已移除")
