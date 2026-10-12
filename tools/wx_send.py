# -*- coding: utf-8 -*-
"""
微信桥 · 发送核心（可复用模块）

从 login_web.py 抽出来，含义：
  - 给一个已经登录的浏览器上下文，往「文件传输助手」发一条消息
  - 发完**回读页面**确认消息真的出现，绝不假装成功

被 wx_bridge.py（托盘+热键程序）和 login_web.py（登录器）共用。
"""
import time

CDP_PORT = 9222
CDP_URL = "http://127.0.0.1:%d" % CDP_PORT

# 实测出来的选择器（微信文件传输助手网页版）
INPUT_SELECTORS = (
    "textarea.chat-panel__input-container",
    "textarea",
    "div[contenteditable='true']", "[contenteditable='true']",
    "[role=textbox]", "#chat-input", ".chat-input",
)


def find_wx_page(ctx):
    """在浏览器里找出那个「文件传输助手」页面（可能开着别的标签）"""
    for pg in ctx.pages:
        try:
            u = pg.url or ""
        except Exception:
            continue
        if "filehelper.weixin.qq.com" in u:
            return pg
    return None


def is_logged_in(page):
    """二维码 img 还在 => 未登录；出现输入框 => 已登录。"""
    try:
        for el in page.query_selector_all("img"):
            if "qrcode" in (el.get_attribute("src") or ""):
                return False
        if page.query_selector("textarea.chat-panel__input-container, textarea, "
                               "div[contenteditable='true'], [role=textbox]"):
            return True
    except Exception:
        pass
    return False


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
    """实测：真按钮是 a.chat-send__button（没字时带 chat-send__button__disabled）"""
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
    """只取聊天面板文本，避开输入框残留的干扰"""
    for sel in ("#chatPanel", ".chat-panel", ".chat"):
        el = page.query_selector(sel)
        if el is None:
            continue
        try:
            return el.inner_text() or ""
        except Exception:
            continue
    return body_text(page)


def send_message(page, text, fast=False):
    """往文件传输助手发一条消息。

    返回 (ok: bool, 说明: str)
    要点：
      - 必须用 fill()，不要用 keyboard.type() 逐字敲 —— 多行会踩"回车即发送"的坑
      - 点按钮前确认 class 里不再含 disabled
      - 发完回读聊天区，确认消息真出现
    """
    lines = []
    el, sel = find_input_el(page)
    if el is None:
        return False, "找不到输入框（可能还没登录，或页面没加载好）"
    lines.append("输入框:%s" % sel)

    # ---- 1. 填字 ----
    filled = False
    try:
        el.click()
        page.wait_for_timeout(150 if fast else 250)
        el.fill("")
        page.wait_for_timeout(80)
        el.fill(text)
        page.wait_for_timeout(400 if fast else 600)
        v = el.input_value()
        filled = (v.strip() == text.strip())
        if not filled:
            lines.append("fill后value=%r（与目标不一致）" % v[:60])
    except Exception as e:
        lines.append("fill失败:%r" % e)

    if not filled:
        try:
            el.click()
            page.wait_for_timeout(150)
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            page.keyboard.type(text, delay=15)
            page.wait_for_timeout(400)
            if el.input_value().strip() == text.strip():
                filled = True
                lines.append("改用键盘输入成功")
        except Exception as e:
            lines.append("type失败:%r" % e)

    if not filled:
        lines.append("警告:文字可能没填进去，仍尝试发送")

    # ---- 2. 发送：优先点真按钮 ----
    btn, bsel = find_send_btn(page)
    if btn is not None:
        try:
            cls = btn.get_attribute("class") or ""
            if "disabled" in cls:
                page.wait_for_timeout(600)
                cls = btn.get_attribute("class") or ""
            btn.click()
            lines.append("已点击发送按钮")
        except Exception as e:
            lines.append("点击失败:%r" % e)
    else:
        lines.append("没找到发送按钮，走回车")

    page.wait_for_timeout(1800 if fast else 2500)
    after = chat_area_text(page)
    if text in after:
        return True, " | ".join(lines + ["回读校验:OK 消息已出现在聊天区"])

    # ---- 3. 点击没生效 -> 试回车 ----
    try:
        el.click()
        page.wait_for_timeout(150)
        page.keyboard.press("Enter")
        page.wait_for_timeout(1800 if fast else 2500)
    except Exception as e:
        lines.append("回车失败:%r" % e)

    after = chat_area_text(page)
    if text in after:
        return True, " | ".join(lines + ["回读校验:OK(回车发送) 消息已出现在聊天区"])

    lines.append("回读校验:FAIL 聊天区没找到该消息")
    try:
        lines.append("输入框剩余=%r" % el.input_value()[:60])
    except Exception:
        pass
    return False, " | ".join(lines)
