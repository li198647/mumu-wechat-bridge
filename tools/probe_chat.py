# -*- coding: utf-8 -*-
"""
微信桥 · 聊天界面探测（只读，不发任何消息）
目的：
  1) 验证登录态是否已持久化到 .wxprofile（重开浏览器应免扫码）
  2) 摸清发送要用的三个东西：输入框 / 发送按钮 / 消息列表（回读校验用）
用法：python probe_chat.py
"""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
PROFILE = BASE / ".wxprofile"
REPORT = BASE / "chat_report.txt"
HTML_OUT = BASE / "chat_page.html"
SHOT = BASE / "chat_page.png"
URL = "https://filehelper.weixin.qq.com/"

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
    REPORT.write_text("\n".join(_lines), encoding="utf-8")


def describe(el):
    try:
        return {
            "tag": el.evaluate("e => e.tagName"),
            "id": el.get_attribute("id"),
            "cls": (el.get_attribute("class") or "")[:70],
            "ce": el.get_attribute("contenteditable"),
            "ph": el.get_attribute("placeholder"),
            "txt": (el.inner_text() or "")[:40],
            "box": el.bounding_box(),
        }
    except Exception as e:
        return {"err": repr(e)}


def main():
    log("=" * 62)
    log("微信桥 · 聊天界面探测（只读）")
    log("=" * 62)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), headless=True,
            viewport={"width": 1280, "height": 860},
            locale="zh-CN", timezone_id="Asia/Shanghai", args=ARGS,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

        log("URL   : %s" % page.url)
        log("TITLE : %r" % page.title())

        # ---- 1. 登录态判定 ----
        qr_alive = False
        for el in page.query_selector_all("img"):
            if "qrcode" in (el.get_attribute("src") or ""):
                qr_alive = True
        log("")
        log("--- 1. 登录态 ---")
        log("二维码是否还在（True=未登录）: %s" % qr_alive)
        verdict = "✅ 登录态已持久化，重开浏览器免扫码" if not qr_alive else "❌ 又回到未登录，cookie 没保住"
        log("结论: %s" % verdict)

        page.screenshot(path=str(SHOT), full_page=True)
        log("整页截图 -> chat_page.png")
        (BASE / "chat_page.html").write_text(page.content(), encoding="utf-8")
        log("HTML -> chat_page.html")

        if qr_alive:
            log("未登录，后面的控件探测没意义，退出。")
            ctx.close()
            return

        # ---- 2. 输入框 ----
        log("")
        log("--- 2. 输入框候选 ---")
        for sel in ("textarea", "[contenteditable=true]", "[contenteditable='']",
                    "div[role=textbox]", ".chat-input", "#chat-input", ".input-area"):
            els = page.query_selector_all(sel)
            log("%-26s %d 个" % (sel, len(els)))
            for i, el in enumerate(els[:4]):
                log("    [%d] %s" % (i, describe(el)))

        # ---- 3. 按钮 ----
        log("")
        log("--- 3. 按钮 ---")
        btns = page.query_selector_all("button, [role=button], .btn, [class*=send], [class*=Send]")
        log("共 %d 个" % len(btns))
        for i, el in enumerate(btns[:25]):
            d = describe(el)
            log("    [%d] tag=%s cls=%s txt=%r box=%s" % (
                i, d.get("tag"), d.get("cls"), d.get("txt"), d.get("box")))

        # ---- 4. 消息列表 ----
        log("")
        log("--- 4. 可能的消息列表容器 ---")
        for sel in ("[class*=msg]", "[class*=message]", "[class*=chat-list]",
                    "[class*=list]", "[class*=content]", "[class*=scroll]"):
            els = page.query_selector_all(sel)
            if not els:
                continue
            log("%-24s %d 个，前 3：" % (sel, len(els)))
            for i, el in enumerate(els[:3]):
                d = describe(el)
                log("    [%d] tag=%s id=%s cls=%s box=%s txt=%r" % (
                    i, d.get("tag"), d.get("id"), d.get("cls"), d.get("box"), d.get("txt")))

        # ---- 5. 页面文本 ----
        log("")
        log("--- 5. body 文本 ---")
        try:
            log(page.inner_text("body")[:600])
        except Exception as e:
            log("读取失败 %r" % e)

        log("")
        log("探测结束，未发送任何消息。")
        ctx.close()


if __name__ == "__main__":
    main()
