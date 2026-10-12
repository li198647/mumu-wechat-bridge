# -*- coding: utf-8 -*-
"""快速检查常驻浏览器当前的登录态（只读，不改动任何东西）。

用法： python check_state.py
产物： tools/state_check.txt（文字结论）、tools/state_now.png（截图）
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "state_check.txt"
SHOT = BASE / "state_now.png"
CDP = "http://127.0.0.1:9222"

lines = []


def out(s=""):
    lines.append(str(s))
    try:
        print(s, flush=True)
    except Exception:
        pass


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP, timeout=15000)
        except Exception as e:
            out("!! 连不上常驻浏览器（9222）：%r" % e)
            out("结论：浏览器没在跑 → 启动桥时它会自己重新拉起来（可能需要重新扫码）")
            return
        out("已连上常驻浏览器")
        ctx = browser.contexts[0]
        page = None
        for pg in ctx.pages:
            if "weixin.qq.com" in (pg.url or ""):
                page = pg
                break
        if page is None:
            out("没找到微信页面；当前页面：")
            for pg in ctx.pages:
                out("   " + (pg.url or ""))
            return

        out("页面 URL ：%s" % page.url)
        try:
            out("页面标题：%s" % page.title())
        except Exception:
            pass

        try:
            txt = page.inner_text("body")
        except Exception as e:
            txt = ""
            out("读页面文本失败：%r" % e)

        flat = " ".join(txt.split())
        out("页面文本（前 400 字）：%s" % flat[:400])

        has_input = False
        try:
            has_input = bool(page.query_selector("textarea.chat-panel__input-container"))
        except Exception:
            pass
        has_qr = False
        try:
            has_qr = bool(page.query_selector("img[src*='qrcode']"))
        except Exception:
            pass

        out("")
        out("输入框 textarea.chat-panel__input-container ：%s" % ("有" if has_input else "无"))
        out("二维码 img[src*=qrcode]                    ：%s" % ("有" if has_qr else "无"))

        if has_input and not has_qr:
            out(">>> 结论：登录态还在，启动桥后【不用重新扫码】")
        elif has_qr:
            out(">>> 结论：已退回登录页 → 启动桥后【需要重新扫码】")
        else:
            out(">>> 结论：判不准，请看截图 %s" % SHOT)

        try:
            page.screenshot(path=str(SHOT), full_page=False)
            out("截图已存：%s" % SHOT)
        except Exception as e:
            out("截图失败：%r" % e)


try:
    main()
except Exception as e:
    out("!! 异常：%r" % e)
finally:
    OUT.write_text("\n".join(lines), encoding="utf-8")
