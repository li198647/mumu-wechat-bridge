# -*- coding: utf-8 -*-
"""
微信桥 · 官方「文件传输助手网页版」常驻桥（第一阶段：登录 + 待命 + 发送验证）
通道：https://filehelper.weixin.qq.com/

设计要点（重要）：
  - 网页版登录态**关掉浏览器就保不住**（实测 cookie 里的凭证写不到磁盘），
    所以本桥按"常驻"设计：扫码成功后浏览器一直开着，桥就一直可用。
  - 登录成功后进入【待命模式】，轮询同目录下的 send_cmd.txt：
      收到普通文本 -> 往「文件传输助手」发出去，并把结果写进 send_result.txt
      收到 __probe__ -> 重新 dump 一遍页面控件到 chat_dom.txt
    => 这样我可以反复实测发送，不需要木木反复扫码。

用法：
  python login_web.py --probe              只探测登录页结构
  python login_web.py                      登录 + 待命（默认待命 1800s）
  python login_web.py --wait 600 --serve 3600
  python login_web.py --headed             有界面模式
"""
import argparse
import json
import os
import socket
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
PROFILE = BASE / ".wxprofile"
CDP_PORT = 9222
CDP_URL = "http://127.0.0.1:%d" % CDP_PORT
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
QR_PNG = BASE / "wx_qr.png"
QR_BIG = BASE / "wx_qr_big.png"
PAGE_PNG = BASE / "wx_login_page.png"
REPORT = BASE / "login_report.txt"
CMD_FILE = BASE / "send_cmd.txt"
RES_FILE = BASE / "send_result.txt"
STATE_JSON = BASE / "state.json"
DOM_TXT = BASE / "chat_dom.txt"

URLS = [
    "https://szfilehelper.weixin.qq.com/",   # 登录后实际跳转到的域名（cookie 挂在这个域）
    "https://filehelper.weixin.qq.com/",     # 初始入口 / 二维码入口
]
URL = URLS[1]

STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || {runtime: {}};
"""

ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
]

_lines = []


def log(s=""):
    s = str(s)
    print(s, flush=True)
    _lines.append(s)
    try:
        REPORT.write_text("\n".join(_lines), encoding="utf-8")
    except Exception:
        pass


def save_qr(el):
    el.screenshot(path=str(QR_PNG))
    try:
        from PIL import Image
        im = Image.open(QR_PNG).convert("RGB")
        im.resize((im.width * 3, im.height * 3), Image.NEAREST).save(str(QR_BIG))
    except Exception:
        pass


def cdp_alive():
    s = socket.socket()
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", CDP_PORT)) == 0
    finally:
        s.close()


def ensure_browser(p, headed=False):
    """确保有一个【独立常驻】的 Chromium 在跑（带调试端口），桥脚本只是连上去。

    为什么要这样：微信网页版的会话与「浏览器进程是否活着」绑定 ——
    浏览器一关，服务端 session 立刻失效（实测 cookie 快照也救不回来）。
    所以让浏览器独立常驻，桥脚本随便重启，登录态都不受影响，不用反复扫码。
    """
    if not cdp_alive():
        exe = p.chromium.executable_path
        cargs = [
            exe,
            "--remote-debugging-port=%d" % CDP_PORT,
            "--user-data-dir=%s" % PROFILE,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--window-size=1280,900",
            "--window-position=0,0",
        ]
        if not headed:
            cargs.append("--headless=new")
        cargs.append("about:blank")

        log("启动独立常驻浏览器：%s" % exe)
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            # 先试"脱离作业对象"：这样启动它的脚本被强杀时，浏览器也不会被连带杀掉
            subprocess.Popen(
                cargs,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags | CREATE_BREAKAWAY_FROM_JOB,
            )
            log("  （已请求脱离作业对象 CREATE_BREAKAWAY_FROM_JOB）")
        except OSError as e:
            log("  脱离作业对象失败（%r），退回普通分离方式" % e)
            subprocess.Popen(
                cargs,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        for _ in range(60):
            time.sleep(0.5)
            if cdp_alive():
                break
        if not cdp_alive():
            raise RuntimeError("独立浏览器启动失败：30s 内 %s 未就绪" % CDP_URL)
        log("独立浏览器已就绪（%s）" % CDP_URL)
    else:
        log("复用已在运行的独立浏览器（%s）" % CDP_URL)

    log("CDP 连接中…")
    browser = p.chromium.connect_over_cdp(CDP_URL, timeout=20000)
    log("CDP 连接成功（contexts=%d）" % len(browser.contexts))
    ctx = browser.contexts[0] if browser.contexts else browser.new_context(
        viewport={"width": 1280, "height": 860}, locale="zh-CN")
    return browser, ctx


def inject_state(ctx):
    """把 state.json 里的登录凭证注入浏览器。

    背景：Chromium 的 profile 里 cookie 的 encrypted_value 写不进去（实测长度为 0），
    但 Playwright 导出的 state.json 是**明文**且可用 —— 所以每次启动都拿它恢复登录，
    免得木木反复扫码。
    """
    if not STATE_JSON.exists():
        log("没有 state.json，跳过凭证注入")
        return
    try:
        st = json.loads(STATE_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        log("读 state.json 失败：%r" % e)
        return

    cks = st.get("cookies") or []
    if cks:
        try:
            ctx.add_cookies(cks)
            log("已注入 %d 条 cookie（来自 state.json）" % len(cks))
        except Exception as e:
            log("注入 cookie 失败：%r" % e)

    for o in st.get("origins") or []:
        kvs = o.get("localStorage") or []
        if not kvs:
            continue
        body = "\n".join(
            "try{localStorage.setItem(%s,%s)}catch(e){}" % (
                json.dumps(kv["name"]), json.dumps(kv["value"]))
            for kv in kvs
        )
        script = "if (location.origin === %s) { %s }" % (json.dumps(o.get("origin", "")), body)
        try:
            ctx.add_init_script(script)
            log("已注册 localStorage 注入：%s（%d 项）" % (o.get("origin"), len(kvs)))
        except Exception as e:
            log("注册 localStorage 脚本失败：%r" % e)


# ---------------------------------------------------------------- 登录

def find_qr(page):
    cands = []
    for sel in ("img[src*=qrcode]", "iframe", "img", "canvas", "[class*=qr] img"):
        try:
            for el in page.query_selector_all(sel):
                box = el.bounding_box()
                if not box:
                    continue
                w, h = box["width"], box["height"]
                if 90 <= w <= 420 and 90 <= h <= 420 and abs(w - h) <= max(20, w * 0.2):
                    cands.append((w * h, el))
        except Exception:
            continue
    if not cands:
        return None
    cands.sort(key=lambda t: t[0], reverse=True)
    return cands[0][1]


def is_logged_in(page):
    """二维码 img 还在 => 未登录；出现输入框 => 已登录。"""
    try:
        for el in page.query_selector_all("img"):
            if "qrcode" in (el.get_attribute("src") or ""):
                return False
        if page.query_selector("div[contenteditable='true'], div[contenteditable=true], "
                               "textarea, #chat-input, .chat-input, [role=textbox]"):
            return True
        txt = page.inner_text("body")
        return ("扫码" not in txt) and ("传输文件" not in txt) and ("消息" in txt)
    except Exception:
        return False


def probe_login_page(ctx):
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)
    log("URL   : %s" % page.url)
    log("TITLE : %r" % page.title())
    for sel in ("canvas", "img", "iframe", "svg"):
        els = page.query_selector_all(sel)
        log("%-7s %d 个" % (sel, len(els)))
        for i, el in enumerate(els[:6]):
            try:
                log("   [%d] box=%s src=%s" % (i, el.bounding_box(),
                                               (el.get_attribute("src") or "")[:80]))
            except Exception as e:
                log("   [%d] %r" % (i, e))
    (BASE / "wx_login_page.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(PAGE_PNG), full_page=True)
    log("已存 wx_login_page.html / .png")


def do_login(ctx, wait_sec, refresh_sec=150):
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    def load():
        """优先访问 sz 域（登录态 cookie 挂在那个域下），拿到二维码或检测到登录即停。"""
        for u in URLS:
            try:
                page.goto(u, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log("  访问 %s 失败：%r" % (u, e))
                continue
            page.wait_for_timeout(3500)
            if is_logged_in(page):
                log("  在 %s 检测到已登录" % u)
                return
            if find_qr(page) is not None:
                log("  在 %s 拿到二维码" % u)
                return
        log("  两个域名都没拿到二维码，也没检出登录态")

    load()
    log("页面已打开：%s  （标题 %r）" % (page.url, page.title()))

    if is_logged_in(page):
        log(">>> 已经是登录态（profile 里存着有效会话），无需扫码。")
        return True, page

    deadline = time.time() + wait_sec
    next_refresh = time.time() + refresh_sec
    last_beat = 0.0
    gen = 0

    while time.time() < deadline:
        try:
            txt = page.inner_text("body")
        except Exception:
            txt = ""
        scanning = any(k in txt for k in ("请在手机上确认", "已扫描", "确认登录", "扫描成功"))

        need_reload, reason = False, ""
        if ("扫码" in txt) and any(k in txt for k in ("失效", "过期")):
            need_reload, reason = True, "页面提示二维码已失效"
        elif time.time() >= next_refresh and not scanning:
            need_reload, reason = True, "定期换新（每 %ds）" % refresh_sec

        if need_reload:
            gen += 1
            log("[%s] 换一张新二维码（第 %d 张）…" % (reason, gen))
            try:
                load()
            except Exception as e:
                log("  刷新失败：%r" % e)
            next_refresh = time.time() + refresh_sec

        el = find_qr(page)
        if el is not None:
            try:
                save_qr(el)
                (BASE / "wx_qr_stamp.txt").write_text(
                    "%d|%s" % (int(time.time()), "第%d张" % max(gen, 1)), encoding="utf-8")
                if time.time() - last_beat > 20:
                    log("二维码就绪 -> wx_qr.png（%s）" % ("第%d张" % max(gen, 1)))
                    last_beat = time.time()
            except Exception as e:
                log("二维码截图失败：%r" % e)

        if is_logged_in(page):
            log(">>> 登录成功！")
            page.wait_for_timeout(2500)
            page.screenshot(path=str(BASE / "wx_after_login.png"), full_page=True)
            return True, page

        page.wait_for_timeout(2000)

    page.screenshot(path=str(PAGE_PNG), full_page=True)
    log(">>> 等待超时，仍未检测到登录成功。整页截图 -> %s" % PAGE_PNG)
    return False, page


# ---------------------------------------------------------------- 控件探测

INPUT_SELECTORS = (
    "textarea.chat-panel__input-container",   # 实测：本站在用的输入框
    "textarea",
    "div[contenteditable='true']", "[contenteditable='true']",
    "[role=textbox]", "#chat-input", ".chat-input",
)


def dump_controls(page, tag=""):
    out = []
    out.append("=" * 70)
    out.append("控件探测 %s   %s" % (tag, time.strftime("%H:%M:%S")))
    out.append("=" * 70)
    out.append("URL: %s" % page.url)

    out.append("\n--- 输入框 ---")
    for sel in INPUT_SELECTORS:
        try:
            els = page.query_selector_all(sel)
        except Exception as e:
            out.append("%-28s 查询失败 %r" % (sel, e))
            continue
        if not els:
            continue
        out.append("%-28s %d 个" % (sel, len(els)))
        for i, el in enumerate(els[:5]):
            try:
                out.append("    [%d] tag=%s id=%s cls=%r ce=%s ph=%r box=%s" % (
                    i, el.evaluate("e=>e.tagName"), el.get_attribute("id"),
                    (el.get_attribute("class") or "")[:50], el.get_attribute("contenteditable"),
                    el.get_attribute("placeholder"), el.bounding_box()))
            except Exception as e:
                out.append("    [%d] %r" % (i, e))

    out.append("\n--- 按钮 / 可点元素 ---")
    seen = 0
    try:
        for el in page.query_selector_all("button, [role=button], [class*=btn], "
                                          "[class*=send], [class*=Send], [class*=button]"):
            try:
                t = (el.inner_text() or "").strip()[:20]
                box = el.bounding_box()
                if not box:
                    continue
                out.append("    tag=%s txt=%r cls=%r box=%s" % (
                    el.evaluate("e=>e.tagName"), t, (el.get_attribute("class") or "")[:50], box))
                seen += 1
                if seen >= 25:
                    break
            except Exception:
                continue
    except Exception as e:
        out.append("查询失败 %r" % e)

    out.append("\n--- 消息/列表容器候选 ---")
    for sel in ("[class*=msg]", "[class*=message]", "[class*=chat]", "[class*=list]",
                "[class*=content]", "[class*=scroll]", "[class*=item]"):
        try:
            els = page.query_selector_all(sel)
        except Exception:
            continue
        if not els:
            continue
        out.append("%-24s %d 个" % (sel, len(els)))
        for i, el in enumerate(els[:4]):
            try:
                out.append("    [%d] tag=%s id=%s cls=%r box=%s txt=%r" % (
                    i, el.evaluate("e=>e.tagName"), el.get_attribute("id"),
                    (el.get_attribute("class") or "")[:55], el.bounding_box(),
                    (el.inner_text() or "")[:60]))
            except Exception:
                continue

    out.append("\n--- body 文本 ---")
    try:
        out.append(page.inner_text("body")[:800])
    except Exception as e:
        out.append("读取失败 %r" % e)

    txt = "\n".join(out)
    DOM_TXT.write_text(txt, encoding="utf-8")
    (BASE / "chat_page.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(BASE / "chat_page.png"), full_page=True)
    log("控件探测已写入 chat_dom.txt（另有 chat_page.html / .png）")


# ---------------------------------------------------------------- 发送

def find_input_el(page):
    for sel in INPUT_SELECTORS:
        try:
            for el in page.query_selector_all(sel):
                box = el.bounding_box()
                if box and box["width"] > 40 and box["height"] > 8:
                    return el, sel
        except Exception:
            continue
    return None, None


def find_send_btn(page):
    """实测：真正的发送按钮是 a.chat-send__button（未输入文字时带 chat-send__button__disabled）"""
    for sel in ("a.chat-send__button", ".chat-send__button", "[class*=send__button]"):
        try:
            el = page.query_selector(sel)
            if el is not None and el.bounding_box():
                return el, sel
        except Exception:
            continue
    for el in page.query_selector_all("a, button"):
        try:
            if (el.inner_text() or "").strip() == "发送" and el.bounding_box():
                return el, "by-text"
        except Exception:
            continue
    return None, None


def body_text(page):
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def chat_area_text(page):
    """只取聊天面板的文本，避开输入框内容的干扰"""
    for sel in ("#chatPanel", ".chat-panel", ".chat"):
        el = page.query_selector(sel)
        if el is None:
            continue
        try:
            return el.inner_text() or ""
        except Exception:
            continue
    return body_text(page)


def send_message(page, text):
    """返回 (ok, 说明)"""
    lines = []
    el, sel = find_input_el(page)
    if el is None:
        return False, "找不到输入框"
    lines.append("输入框:%s" % sel)

    # ---- 1. 填字：先 fill（会正确触发 input 事件） ----
    filled = False
    try:
        el.click()
        page.wait_for_timeout(250)
        el.fill("")
        page.wait_for_timeout(150)
        el.fill(text)
        page.wait_for_timeout(700)
        v = el.input_value()
        lines.append("fill后value=%r" % v)
        filled = (v.strip() == text.strip())
    except Exception as e:
        lines.append("fill失败:%r" % e)

    # ---- 2. fill 不灵就改用真实键盘逐字输入 ----
    if not filled:
        try:
            el.click()
            page.wait_for_timeout(200)
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            page.keyboard.type(text, delay=20)
            page.wait_for_timeout(700)
            v = el.input_value()
            lines.append("type后value=%r" % v)
            filled = (v.strip() == text.strip())
        except Exception as e:
            lines.append("type失败:%r" % e)

    if not filled:
        lines.append("警告:文字可能没填进去，仍继续尝试发送")

    # ---- 3. 发送：优先点真正的按钮 ----
    btn, bsel = find_send_btn(page)
    if btn is None:
        lines.append("没找到发送按钮，直接走回车")
    else:
        try:
            cls = btn.get_attribute("class") or ""
            lines.append("按钮:%s class=%r" % (bsel, cls))
            if "disabled" in cls:
                page.wait_for_timeout(900)
                lines.append("重查class=%r" % (btn.get_attribute("class") or ""))
            btn.click()
            lines.append("已点击发送按钮")
        except Exception as e:
            lines.append("点击失败:%r" % e)

    page.wait_for_timeout(2500)
    after = chat_area_text(page)
    if text in after:
        return True, " | ".join(lines + ["回读校验:OK 聊天区已出现该消息"])

    # ---- 4. 点击没生效 -> 试回车 ----
    lines.append("点击后未见消息，改试回车")
    try:
        el.click()
        page.wait_for_timeout(250)
        page.keyboard.press("Enter")
        page.wait_for_timeout(2500)
    except Exception as e:
        lines.append("回车失败:%r" % e)

    after = chat_area_text(page)
    if text in after:
        return True, " | ".join(lines + ["回读校验:OK(回车发送) 聊天区已出现该消息"])

    lines.append("回读校验:FAIL 未找到消息")
    lines.append("聊天区文本=%r" % after[:400])
    try:
        lines.append("输入框剩余=%r" % el.input_value()[:80])
    except Exception:
        pass
    return False, " | ".join(lines)


# ---------------------------------------------------------------- 待命

def serve(ctx, page, max_sec):
    try:
        ctx.storage_state(path=str(STATE_JSON))
        log("已导出登录态快照 -> state.json")
    except Exception as e:
        log("导出登录态失败：%r" % e)

    dump_controls(page, tag="(登录后)")

    if CMD_FILE.exists():
        try:
            CMD_FILE.unlink()
        except Exception:
            pass

    log("")
    log(">>> 进入待命模式，最多 %ds。命令文件：%s" % (max_sec, CMD_FILE))
    end = time.time() + max_sec
    while time.time() < end:
        if CMD_FILE.exists():
            try:
                txt = CMD_FILE.read_text(encoding="utf-8")
            except Exception as e:
                txt = ""
                log("读命令文件失败 %r" % e)
            try:
                CMD_FILE.unlink()
            except Exception:
                pass

            txt = txt.strip()
            if not txt:
                continue
            if txt == "__probe__":
                dump_controls(page, tag="(手动探测)")
                RES_FILE.write_text("[OK] 已重新探测，见 chat_dom.txt", encoding="utf-8")
                continue
            if txt.startswith("__eval__"):
                js = txt[len("__eval__"):]
                try:
                    r = page.evaluate(js)
                    out = json.dumps(r, ensure_ascii=False, default=str)
                except Exception as e:
                    out = "错误: %r" % e
                RES_FILE.write_text("[OK] " + out[:4000], encoding="utf-8")
                log("eval: %s" % out[:400])
                continue
            if txt == "__shot__":
                page.screenshot(path=str(BASE / "manual_shot.png"), full_page=True)
                RES_FILE.write_text("[OK] 截图 -> manual_shot.png", encoding="utf-8")
                continue
            log("收到发送命令：%r" % txt[:100])
            ok, msg = send_message(page, txt)
            RES_FILE.write_text(("[OK] " if ok else "[FAIL] ") + msg, encoding="utf-8")
            log("发送结果：%s —— %s" % ("成功" if ok else "失败", msg))
            if ok:
                try:
                    page.screenshot(path=str(BASE / "after_send.png"), full_page=True)
                except Exception:
                    pass

        try:
            page.wait_for_timeout(1500)
        except Exception as e:
            log("页面已断开：%r" % e)
            break

    log(">>> 待命结束。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--wait", type=int, default=300, help="等待扫码秒数")
    ap.add_argument("--serve", type=int, default=1800, help="登录后待命秒数")
    args = ap.parse_args()

    log("=" * 62)
    log("微信桥 · 网页版常驻桥（独立浏览器 + CDP）")
    log("profile : %s" % PROFILE)
    log("cdp     : %s" % CDP_URL)
    log("=" * 62)

    with sync_playwright() as p:
        browser, ctx = ensure_browser(p, headed=args.headed)
        try:
            # 说明：不再注入 stealth / cookie。
            #  - stealth 实测不需要（headless 下页面完全正常）
            #  - cookie 注入实测无效（浏览器一关服务端 session 就注销了）
            #  - 而且 ctx.add_init_script / add_cookies 在 CDP 连上的 context 上会挂住
            if args.probe:
                log("进入探测模式…")
                probe_login_page(ctx)
                return
            log("进入登录流程…")
            ok, page = do_login(ctx, args.wait)
            if ok:
                serve(ctx, page, args.serve)
        finally:
            try:
                ctx.storage_state(path=str(STATE_JSON))
                log("已更新登录态快照 -> state.json")
            except Exception as e:
                log("导出登录态失败：%r" % e)
            # 只断开连接，**不关浏览器** —— 浏览器要一直活着才能保住登录态
            log("桥脚本退出（浏览器继续常驻）。")


if __name__ == "__main__":
    main()
