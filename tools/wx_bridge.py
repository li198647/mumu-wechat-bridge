# -*- coding: utf-8 -*-
r"""微信桥 · 主程序（一键启动：拉起浏览器 + 引导登录 + 托盘 + 全局热键）

怎么用：
    双击「启动微信桥.bat」（推荐），或直接 python wx_bridge.py

效果：
    1. 自己把常驻浏览器拉起来（带 9222 调试端口）
    2. 没登录就自动弹出「实时二维码」页面，用手机扫一次；二维码过期会自己换新
    3. 登录后缩进系统托盘，随时待命
    4. 在任意程序里选中文字 -> 按全局热键 -> 文字发到微信「文件传输助手」

⚠️ 为什么要用户自己双击启动：
   实测发现"由其他程序（Automation/父进程）启动的浏览器"会被连带清理，
    甚至连 CREATE_BREAKAWAY_FROM_JOB 都救不了。
    由你双击启动，浏览器才真正独立常驻，登录态也能长期保住。
"""
import ctypes
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import wx_send
import wx_toast
import wx_tray

BASE = Path(__file__).resolve().parent
ICON = BASE / "wx.ico"
ICON_OK = BASE / "wx_ok.ico"       # 闪烁用：绿底白勾
ICON_ERR = BASE / "wx_err.ico"     # 闪烁用：红底白叉
LOG_PATH = BASE / "wx_bridge.log"

# 视觉回执参数（2026-10-12 拷问定稿）
TOAST_SEC = 1.5                    # 光标旁提示条停留秒数
FLASH_OK_SEC = 2.5                 # 成功：图标闪烁秒数
FLASH_ERR_SEC = 4.0                # 失败：闪久一点，更容易被注意到
FLASH_INTERVAL = 0.5               # 每 0.5 秒切换一次图标

CDP_PORT = wx_send.CDP_PORT
CDP_URL = wx_send.CDP_URL
QR_PNG = BASE / "wx_qr.png"
QR_BIG = BASE / "wx_qr_big.png"
QR_LIVE = BASE / "wx_qr_live.html"
URLS = ["https://szfilehelper.weixin.qq.com/", "https://filehelper.weixin.qq.com/"]

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NEW_CONSOLE = 0x00000010

# 全局引用（托盘回调里要用）
tray = None
sender = None
toast = None

_log_lock = threading.Lock()


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)          # 控制台编码不对时不能让整程序崩
    except Exception:
        pass
    try:
        with _log_lock:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 剪贴板 & 按键
u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32

u32.OpenClipboard.restype = ctypes.c_bool
u32.OpenClipboard.argtypes = [ctypes.c_void_p]
u32.CloseClipboard.restype = ctypes.c_bool
u32.GetClipboardData.restype = ctypes.c_void_p
u32.GetClipboardData.argtypes = [ctypes.c_uint]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalLock.argtypes = [ctypes.c_void_p]
k32.GlobalUnlock.restype = ctypes.c_bool
k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
u32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_uint,
                            ctypes.c_void_p]

CF_UNICODETEXT = 13
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_ALT = 0x12
VK_C = 0x43
VK_LWIN, VK_RWIN = 0x5B, 0x5C
KEYEVENTF_KEYUP = 0x0002

u32.GetAsyncKeyState.restype = ctypes.c_short
u32.GetAsyncKeyState.argtypes = [ctypes.c_int]

# 只用"通用"键码（0x10/0x11/0x12）—— GetAsyncKeyState 对左右分键码不灵，
# 通用码能可靠反映"Ctrl 是否还按着"。
MODIFIER_VKS = (VK_SHIFT, VK_CONTROL, VK_ALT, VK_LWIN, VK_RWIN)


def get_clipboard_text():
    if not u32.OpenClipboard(None):
        return None
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = k32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.c_wchar_p(p).value
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()


def _key(vk, up=False):
    u32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, None)


def send_hotkey_combo(mods, vk):
    for m in mods:
        _key(m, False)
        time.sleep(0.02)
    _key(vk, False)
    time.sleep(0.03)
    _key(vk, True)
    for m in reversed(mods):
        time.sleep(0.02)
        _key(m, True)


def modifiers_down():
    """当前还按着的修饰键（返回虚拟键码列表）"""
    return [vk for vk in MODIFIER_VKS if u32.GetAsyncKeyState(vk) & 0x8000]


def wait_modifiers_released(timeout=1.2):
    """等用户把 Ctrl/Alt/Shift/Win 全松开（最多 timeout 秒）。

    ⚠️ 为什么非等不可（2026-10-12 定位到的真根因）：
       热键被按下的**那一刻**，手指还按着 Ctrl+Alt。此时桥注入 Ctrl+C，
       系统看到的其实是 **Ctrl+Alt+C** ——
         · 它不是"复制"，所以选中文字**根本没进剪贴板**（于是只能退回旧剪贴板）；
         · 而 Ctrl+Alt+C 正好是 **QQ 的长截图**，所以 QQ 会弹出来。
       两个毛病（收到旧内容 / QQ 弹截图）是**同一个根因**。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not modifiers_down():
            return True
        time.sleep(0.03)
    return False


def release_modifiers():
    """兜底：强制松开修饰键（即使用户还按着）。

    这样后面注入的一定是干净的 Ctrl+C，不会再变成 Ctrl+Alt+C。
    """
    for vk in (VK_ALT, VK_CONTROL, VK_SHIFT):
        _key(vk, True)
    time.sleep(0.06)


def _copy_once():
    send_hotkey_combo([VK_CONTROL], VK_C)
    time.sleep(0.35)
    return get_clipboard_text() or ""


def grab_selected_text():
    """先试「复制选中内容」，抓不到就退回剪贴板里现有的。返回 (text, 来源)"""
    before = get_clipboard_text() or ""

    # 关键一步：先确认修饰键松开，否则 Ctrl+C 会变成 Ctrl+Alt+C（复制失败 + 触发 QQ 长截图）
    if not wait_modifiers_released(1.2):
        release_modifiers()

    after = _copy_once()
    if not (after.strip() and after != before):
        # 第一次没复制到：强制松开修饰键再补一次（有的程序第一次会丢）
        release_modifiers()
        again = _copy_once()
        if again.strip():
            after = again

    if after.strip() and after != before:
        return after.strip(), "选中文字"
    if before.strip() and after == before:
        return before.strip(), "剪贴板原有内容"
    if after.strip():
        return after.strip(), "剪贴板"
    return "", "没取到任何文字"


# ---------------------------------------------------------------- 浏览器
def cdp_alive():
    s = socket.socket()
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", CDP_PORT)) == 0
    finally:
        s.close()


def spawn_browser(exe):
    cargs = [
        exe,
        "--remote-debugging-port=%d" % CDP_PORT,
        "--user-data-dir=%s" % (BASE / ".wxprofile"),
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--window-size=1280,900",
        "--headless=new",
        "about:blank",
    ]
    log("拉起常驻浏览器：%s" % exe)
    subprocess.Popen(cargs, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    for _ in range(60):
        time.sleep(0.5)
        if cdp_alive():
            return True
    return False


def shot_qr(page):
    """把当前二维码截出来（顺便出个放大版，供实时页面显示）"""
    try:
        for el in page.query_selector_all("img"):
            if "qrcode" not in (el.get_attribute("src") or ""):
                continue
            box = el.bounding_box()
            if not box or not (90 <= box["width"] <= 420):
                continue
            el.screenshot(path=str(QR_PNG))
            try:
                from PIL import Image
                im = Image.open(QR_PNG).convert("RGB")
                im.resize((im.width * 3, im.height * 3),
                          Image.NEAREST).save(str(QR_BIG))
            except Exception:
                pass
            return True
    except Exception:
        pass
    return False


# 「实时二维码」页面模板：运行时写出来，不进仓库（是运行产物，且内含本机路径）。
# __ABS__ 会被替换成本机 wx_qr.png 的 file:// 绝对地址（相对路径失败时兜底）。
QR_PAGE_TMPL = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>微信桥 · 扫码登录</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
    background: #f2f4f7;
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    color: #1a1a1a;
  }
  .card {
    background: #fff; border-radius: 18px; padding: 26px 30px 20px;
    box-shadow: 0 8px 30px rgba(0,0,0,.10);
    text-align: center; max-width: 420px;
  }
  h1 { font-size: 17px; margin: 0 0 4px; font-weight: 600; }
  .sub { font-size: 12.5px; color: #8a8f98; margin: 0 0 16px; }
  .qrbox {
    width: 300px; height: 300px; margin: 0 auto;
    display: flex; align-items: center; justify-content: center;
    background: #fff; border-radius: 12px; border: 1px solid #eef0f3;
  }
  #qr { width: 296px; height: 296px; image-rendering: pixelated; }
  .stamp { font-size: 12px; color: #9aa0a6; margin: 14px 0 0; }
</style>
</head>
<body>
  <div class="card">
    <h1>微信 · 文件传输助手网页版</h1>
    <p class="sub">用手机微信「扫一扫」，扫完在手机上点确认登录</p>
    <div class="qrbox"><img id="qr" src="wx_qr.png" alt="二维码"></div>
    <p class="stamp" id="stamp">正在读取二维码…</p>
  </div>
<script>
(function () {
  var img = document.getElementById('qr');
  var stamp = document.getElementById('stamp');
  var ABS = '__ABS__';
  var useAbs = false;
  img.onerror = function () {
    if (!useAbs) { useAbs = true; img.src = ABS + '?t=' + Date.now(); }
  };
  img.onload = function () {
    stamp.textContent = '二维码已更新 · ' + new Date().toLocaleTimeString() + '（过期会自动换新）';
  };
  setInterval(function () {
    img.src = (useAbs ? ABS : 'wx_qr.png') + '?t=' + Date.now();
  }, 2500);
})();
</script>
</body>
</html>
"""


def write_qr_page():
    """生成「实时二维码」预览页（每 2.5s 自动重新载入二维码图）。"""
    try:
        QR_LIVE.write_text(QR_PAGE_TMPL.replace("__ABS__", QR_PNG.as_uri()),
                           encoding="utf-8")
        return True
    except Exception as e:
        log("写二维码页面失败：%r" % e)
        return False


def ensure_login(wait_sec=600, refresh_sec=150):
    """确保浏览器在跑且已登录；没登录就弹二维码页面引导扫码。

    返回 (ok: bool, msg: str)
    """
    from playwright.sync_api import sync_playwright

    if not cdp_alive():
        with sync_playwright() as p0:
            exe = p0.chromium.executable_path
        if not spawn_browser(exe):
            return False, "浏览器没起来（%s 未就绪）" % CDP_URL

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(CDP_URL, timeout=20000)
        ctx = b.contexts[0] if b.contexts else b.new_context()

        page = wx_send.find_wx_page(ctx)
        if page is None:
            page = ctx.new_page()
            for u in URLS:
                try:
                    page.goto(u, wait_until="domcontentloaded", timeout=60000)
                    break
                except Exception as e:
                    log("  打开 %s 失败：%r" % (u, e))

        page.wait_for_timeout(3000)
        if wx_send.is_logged_in(page):
            return True, "已经是登录态（浏览器一直在跑）"

        log("未登录，打开实时二维码页面，请用手机微信扫码…")
        shot_qr(page)
        write_qr_page()
        try:
            os.startfile(str(QR_LIVE))
        except Exception as e:
            log("自动打开二维码页面失败：%r；请手动打开 %s" % (e, QR_LIVE))

        deadline = time.time() + wait_sec
        next_refresh = time.time() + refresh_sec
        while time.time() < deadline:
            try:
                txt = page.inner_text("body")
            except Exception:
                txt = ""
            scanning = any(k in txt for k in ("请在手机上确认", "已扫描",
                                              "确认登录", "扫描成功"))
            expired = ("扫码" in txt) and any(k in txt for k in ("失效", "过期"))

            if expired or (time.time() >= next_refresh and not scanning):
                try:
                    page.goto(URLS[0], wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3000)
                except Exception as e:
                    log("  刷新二维码失败：%r" % e)
                next_refresh = time.time() + refresh_sec
                log("  已换一张新二维码（过期会自动换新）")

            shot_qr(page)
            if wx_send.is_logged_in(page):
                return True, "扫码登录成功"
            page.wait_for_timeout(2000)

        return False, "等待扫码超时（%d 秒）" % wait_sec


# ---------------------------------------------------------------- 发送线程
class Sender:
    """常驻发送线程。

    Playwright 的同步 API 有线程亲和性，所以在**同一个**线程里全程持有它，
    外部通过队列投递任务。连不上就下次重连。
    """

    def __init__(self):
        self.q = queue.Queue()
        self.t = threading.Thread(target=self._run, name="wx-send", daemon=True)
        self.t.start()

    def send(self, text, timeout=30):
        ev = threading.Event()
        box = {}
        self.q.put((text, ev, box))
        if not ev.wait(timeout):
            return False, "发送超时（%ds 没等到结果）" % timeout
        return box.get("ok", False), box.get("msg", "未知结果")

    def _run(self):
        from playwright.sync_api import sync_playwright
        pw = None
        browser = None
        while True:
            text, ev, box = self.q.get()
            try:
                if browser is None:
                    if pw is None:
                        pw = sync_playwright().start()
                    browser = pw.chromium.connect_over_cdp(CDP_URL, timeout=15000)

                ctx = browser.contexts[0] if browser.contexts else None
                if ctx is None:
                    box["ok"], box["msg"] = False, "连上浏览器但拿不到上下文"
                    browser = None
                    continue

                page = wx_send.find_wx_page(ctx)
                if page is None:
                    box["ok"], box["msg"] = False, "没找到「文件传输助手」页面"
                    continue
                if not wx_send.is_logged_in(page):
                    box["ok"], box["msg"] = False, "登录已失效，请重启本程序重新扫码"
                    continue

                box["ok"], box["msg"] = wx_send.send_message(page, text, fast=True)
            except Exception as e:
                box["ok"], box["msg"] = False, "发送异常：%r" % e
                browser = None
            finally:
                ev.set()


# ---------------------------------------------------------------- 业务动作
def notice(ok, text):
    """统一的视觉回执（2026-10-12 拷问定稿）。

    成功/失败各来两路：
      1. 光标旁弹一个小提示条（就在你视线所在处，1.5 秒自动消失）
      2. 托盘图标闪一下（成功 = 绿勾，失败 = 红叉）
    ⚠️ 成功时**不再弹气泡**（木木明确要求：气泡里会显示消息内容预览，
       而他不想让内容出现在屏幕上）；失败仍保留红气泡，因为失败要更醒目。
    """
    if toast:
        toast.show(text, ok=ok, seconds=TOAST_SEC)
    if tray:
        tray.flash("ok" if ok else "err",
                   seconds=FLASH_OK_SEC if ok else FLASH_ERR_SEC,
                   interval=FLASH_INTERVAL)


def do_send(text, source=""):
    text = (text or "").strip()
    if not text:
        log("没取到文字，取消发送")
        if toast:
            toast.show("没取到文字", ok=False, seconds=TOAST_SEC)
        if tray:
            tray.notify("微信桥", "没取到文字（先选一段文字再按热键）",
                        wx_tray.NIIF_WARNING)
        return
    preview = text.replace("\n", " / ")[:40]
    log("准备发送（%s，%d 字）：%s" % (source or "?", len(text), preview))
    ok, msg = sender.send(text)
    log("发送%s —— %s" % ("成功" if ok else "失败", msg))
    if ok:
        notice(True, "已发到微信")
    else:
        notice(False, "发送失败")
        if tray:
            tray.notify("发送失败", "%s\n托盘右键可看日志" % msg[:100],
                        wx_tray.NIIF_ERROR)


def on_preview():
    """托盘菜单：预览提示效果（**不真的发消息**，纯给你看观感）

    先后演示「成功」和「失败」两种样式，中间隔 4 秒。
    """
    log("--- 托盘菜单：预览提示效果 ---")
    log("   第 1 段：成功样式（光标旁绿勾提示条 + 托盘图标闪绿勾）")
    if toast:
        toast.show("已发到微信", ok=True, seconds=TOAST_SEC)
    if tray:
        tray.flash("ok", seconds=FLASH_OK_SEC, interval=FLASH_INTERVAL)
    time.sleep(TOAST_SEC + 0.8)
    log("   第 2 段：失败样式（光标旁红叉提示条 + 托盘图标闪红叉）")
    if toast:
        toast.show("发送失败", ok=False, seconds=TOAST_SEC)
    if tray:
        tray.flash("err", seconds=FLASH_ERR_SEC, interval=FLASH_INTERVAL)
    log("预览结束（以上均未真的发送任何消息）")


def on_hotkey():
    log("--- 热键 %s 触发 ---" % (tray.hotkey_name if tray else "?"))
    held = modifiers_down()
    if held:
        names = {VK_SHIFT: "Shift", VK_CONTROL: "Ctrl", VK_ALT: "Alt",
                 VK_LWIN: "Win", VK_RWIN: "Win"}
        log("   （按下瞬间修饰键仍按着：%s —— 这就是 Ctrl+C 会变成 Ctrl+Alt+C 的原因，"
            "现在会等它松开再复制）" % "、".join(names.get(v, hex(v)) for v in held))
    # 抓字内部会先等修饰键松开（原来的 time.sleep(0.25) 死等不够稳，已内置）
    text, src = grab_selected_text()
    do_send(text, src)


def on_send_clipboard():
    log("--- 托盘菜单：发送剪贴板 ---")
    do_send((get_clipboard_text() or "").strip(), "剪贴板")


def on_scan():
    """托盘菜单：把候选热键全试一遍，告诉用户哪些真的能用"""
    log("--- 托盘菜单：热键检测 ---")
    try:
        ok_list, lines = wx_tray.scan_hotkeys()
    except Exception as e:
        log("热键检测失败：%r" % e)
        return
    for l in lines:
        log("   " + l)
    digits = [x for x in ok_list
              if x.startswith("Ctrl+Alt+") and "小键盘" not in x]
    held = tray.hotkey_name if tray and tray.hotkey_ok else "无"
    log("可用组合共 %d 个；本程序已占用：%s" % (len(ok_list), held))
    log("⚠️ 本检测只测 RegisterHotKey，测不出用键盘钩子抢键的程序"
        "（QQ 截图就是这类）；桥现在用钩子抢键，不受影响。")
    if tray:
        if digits:
            tray.notify("热键检测", "Ctrl+Alt+数字可用：%s\n"
                                    "本程序当前用：%s" % ("、".join(digits), held))
        else:
            tray.notify("热键检测", "Ctrl+Alt+数字不可用，请改用托盘菜单发送",
                        wx_tray.NIIF_WARNING)


def on_restart():
    """托盘菜单：重启自己（改完代码/换热键后用，登录态不受影响）

    顺序很讲究：**先释放托盘图标和热键，再拉新实例**，否则新实例会发现
    自己想要的组合被"上一个自己"占着，只能退而求其次。
    """
    log("--- 托盘菜单：重启微信桥 ---")
    try:
        if toast:
            toast.close()
        if tray:
            tray.cleanup()
        time.sleep(0.5)                      # 等 Explorer 把旧图标收掉、热键释放
        subprocess.Popen(
            [sys.executable, str(BASE / "wx_bridge.py")],
            cwd=str(BASE),
            creationflags=CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP,
        )
        log("已拉起新实例，本进程退出（浏览器和登录态会复用，不用重新扫码）")
    except Exception as e:
        log("重启失败：%r —— 请手动双击「启动微信桥.bat」" % e)
        return
    time.sleep(0.8)
    os._exit(0)


def on_command(name):
    if name == "quit":
        log("用户从托盘退出微信桥")
        if toast:
            toast.close()
        if tray:
            tray.cleanup()
        os._exit(0)
    elif name == "show":
        wx_tray.show_console()
    elif name == "hide":
        wx_tray.hide_console()
    elif name == "folder":
        try:
            os.startfile(str(BASE))
        except Exception as e:
            log("打开文件夹失败：%r" % e)
    elif name == "send_clip":
        threading.Thread(target=on_send_clipboard, daemon=True).start()
    elif name == "scan":
        threading.Thread(target=on_scan, daemon=True).start()
    elif name == "preview":
        threading.Thread(target=on_preview, daemon=True).start()
    elif name == "restart":
        threading.Thread(target=on_restart, daemon=True).start()


# ---------------------------------------------------------------- 主流程
def main():
    global tray, sender, toast

    log("")
    log("=" * 56)
    log("微信桥 启动")
    log("=" * 56)

    ok, msg = ensure_login()
    log("登录检查：%s —— %s" % ("✅" if ok else "❌", msg))

    sender = Sender()

    # 光标旁提示条（自绘浮窗）。起不来也不致命——托盘闪烁还在。
    toast = wx_toast.Toast()
    if toast.start():
        log("光标提示条已就绪（发送结果会弹在鼠标旁边）")
    else:
        log("⚠️ 光标提示条没起来：%s" % toast.err)
        log("   （不影响发送；托盘图标闪烁仍然可用）")

    tray = wx_tray.TrayHotkey(
        tooltip="微信桥 · 选中文字按热键即发送",
        icon_path=str(ICON),
        icon_ok=str(ICON_OK),
        icon_err=str(ICON_ERR),
        on_command=on_command,
        on_hotkey=on_hotkey,
        on_message=log,
    )
    if tray.start():
        if tray.hotkey_ok:
            mode = "钩子抢键" if tray.hotkey_mode == "hook" else "注册热键"
            log("托盘已就绪；全局热键：%s ✅（%s模式）" % (tray.hotkey_name, mode))
            tip = "选中文字后按 %s 发送" % tray.hotkey_name
        else:
            log("托盘已就绪，但候选热键全被占用；请用托盘菜单「发送剪贴板」")
            tip = "热键全被占用；托盘右键 → 热键检测 可看到哪些组合能用"
        tray.notify("微信桥已启动",
                    "%s\n发完会在鼠标旁弹提示，托盘图标也会闪一下" % tip)
    else:
        log("⚠️ 托盘启动失败，退化为纯控制台模式")

    if tray.hotkey_ok:
        log("桥在运行中。按 %s 发送选中文字；托盘图标右键可退出。" % tray.hotkey_name)
    else:
        log("桥在运行中。托盘图标右键可发送 / 退出。")
    log("提示方式：光标旁小提示 %.1fs + 托盘图标闪烁（成功 %.1fs / 失败 %.1fs）；"
        "想先看观感 → 托盘右键「预览提示效果」"
        % (TOAST_SEC, FLASH_OK_SEC, FLASH_ERR_SEC))

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出")
        if tray:
            tray.cleanup()


if __name__ == "__main__":
    main()
