# -*- coding: utf-8 -*-
r"""微信桥 · 光标旁提示条（纯 ctypes 自绘浮窗，零依赖）

用途：
    发送结果出来后，在鼠标光标旁边弹一个小条（例：绿色勾 +「已发到微信」），
    1.5 秒后自己消失。目的是让木木**在视线所在的位置**立刻知道成没成，
    不用去瞟托盘右下角。

为什么不用气泡通知（Shell_NotifyIcon + NIF_INFO）：
    1. Win10 可以整个关掉通知，关掉后什么都不弹；
    2. 气泡里想写什么都行、但**没法控制位置**，永远在右下角；
    3. 木木按热键时眼睛在看选中的文字，不在右下角。

关键实现要点（踩过的坑都在这）：
  - 必须 `WS_EX_NOACTIVATE`：否则弹出来会抢走焦点，你正在敲的字就断了。
  - 必须 `WS_EX_TRANSPARENT`：鼠标点击**穿透**它，永远不会挡住你点东西。
  - 必须 `WS_EX_TOOLWINDOW`：不然会在 Alt+Tab 列表里冒出来。
  - 必须 `WS_EX_TOPMOST`：否则浏览器全屏时根本看不见。
  - 分层窗口用 `SetLayeredWindowAttributes(LWA_ALPHA)`：整个窗口统一半透明，
    GDI 画的文字仍然**边缘清晰**（不像 UpdateLayeredWindow 那么麻烦）。
  - 圆角靠 `SetWindowRgn` + `CreateRoundRectRgn`：换尺寸时必须重设，否则残留旧形状。
  - 窗口必须建在**有消息循环的线程**里（这里自带一个专用线程），否则 WM_TIMER 收不到。
  - `show()` 会被发送线程调用 → 不能直接操作窗口，只能 PostMessage 把活派给窗口线程。

用法（自测）：
    python wx_toast.py                      # 弹一个「已发到微信」给你看
    python wx_toast.py 发送失败 --err        # 弹一个红色的
"""
import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time
import traceback

u32 = ctypes.windll.user32
g32 = ctypes.windll.gdi32
k32 = ctypes.windll.kernel32

# ---------------------------------------------------------------- 常量
WM_PAINT = 0x000F
WM_TIMER = 0x0113
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_APP = 0x8000
WM_TOAST_SHOW = WM_APP + 11

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

LWA_ALPHA = 0x00000002

DT_LEFT = 0x0000
DT_SINGLELINE = 0x0020
DT_VCENTER = 0x0004
DT_NOPREFIX = 0x0800

TRANSPARENT = 1
FW_NORMAL = 400
DEFAULT_CHARSET = 1
CLEARTYPE_QUALITY = 5

TIMER_HIDE = 1

# 外观
PAD_H = 13            # 左右内边距
PAD_V = 8             # 上下内边距
DOT_R = 8             # 状态圆点半径
DOT_GAP = 9           # 圆点到文字的距离
FONT_H = -19          # 负 = 字符高度（96 DPI 下即像素）
FONT_NAME = "Microsoft YaHei UI"
TOAST_ALPHA = 232     # 0-255，整体不透明度
RADIUS = 10           # 圆角

BG = 0x00201E1C       # COLORREF 是 0x00BBGGRR → 深灰
FG = 0x00FFFFFF       # 白字
CLR_GREEN = 0x0060C107
CLR_RED = 0x004A4BE2
CLR_WHITE = 0x00FFFFFF


def _rgb(r, g, b):
    return r | (g << 8) | (b << 16)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", ctypes.c_void_p), ("fErase", wt.BOOL),
                ("rcPaint", RECT), ("fRestore", wt.BOOL),
                ("fIncUpdate", wt.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT), ("style", wt.UINT),
        ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, ctypes.c_uint,
                             ctypes.c_ulonglong, ctypes.c_longlong)

# ---------------------------------------------------------------- 声明
u32.DefWindowProcW.restype = ctypes.c_longlong
u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.RegisterClassExW.restype = wt.ATOM
u32.RegisterClassExW.argtypes = [ctypes.c_void_p]
u32.CreateWindowExW.restype = wt.HWND
u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wt.HWND, wt.HMENU,
                                wt.HINSTANCE, ctypes.c_void_p]
u32.DestroyWindow.argtypes = [wt.HWND]
u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.PostQuitMessage.argtypes = [ctypes.c_int]
u32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
u32.GetSystemMetrics.restype = ctypes.c_int
u32.GetSystemMetrics.argtypes = [ctypes.c_int]
u32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
u32.GetMessageW.restype = ctypes.c_int
u32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
u32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
u32.DispatchMessageW.restype = ctypes.c_longlong
u32.SetLayeredWindowAttributes.restype = wt.BOOL
u32.SetLayeredWindowAttributes.argtypes = [wt.HWND, ctypes.c_uint32,
                                           ctypes.c_ubyte, wt.DWORD]
u32.SetWindowRgn.restype = ctypes.c_int
u32.SetWindowRgn.argtypes = [wt.HWND, ctypes.c_void_p, wt.BOOL]
u32.SetTimer.restype = ctypes.c_void_p
u32.SetTimer.argtypes = [wt.HWND, ctypes.c_void_p, wt.UINT, ctypes.c_void_p]
u32.KillTimer.restype = wt.BOOL
u32.KillTimer.argtypes = [wt.HWND, ctypes.c_void_p]
u32.BeginPaint.restype = ctypes.c_void_p
u32.BeginPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]
u32.EndPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]
u32.FillRect.restype = ctypes.c_int
u32.FillRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT), ctypes.c_void_p]
u32.InvalidateRect.argtypes = [wt.HWND, ctypes.c_void_p, wt.BOOL]
u32.GetClientRect.restype = wt.BOOL
u32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
u32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wt.UINT]
# ⚠️ GetDC 返回的是 64 位句柄，不声明 restype 会被默认的 c_int 截断 → 后续全崩
u32.GetDC.restype = ctypes.c_void_p
u32.GetDC.argtypes = [wt.HWND]
u32.ReleaseDC.restype = ctypes.c_int
u32.ReleaseDC.argtypes = [wt.HWND, ctypes.c_void_p]

# ⚠️ 同理：GetModuleHandleW 返回 HINSTANCE，不声明也会被截断
k32.GetModuleHandleW.restype = wt.HINSTANCE
k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]

g32.CreateSolidBrush.restype = ctypes.c_void_p
g32.CreateSolidBrush.argtypes = [ctypes.c_uint32]
g32.CreatePen.restype = ctypes.c_void_p
g32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint32]
g32.SelectObject.restype = ctypes.c_void_p
g32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
g32.DeleteObject.argtypes = [ctypes.c_void_p]
g32.CreateFontW.restype = ctypes.c_void_p
g32.CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, wt.DWORD, wt.DWORD,
                            wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
                            wt.DWORD, wt.DWORD, wt.LPCWSTR]
g32.GetTextExtentPoint32W.restype = wt.BOOL
g32.GetTextExtentPoint32W.argtypes = [ctypes.c_void_p, wt.LPCWSTR,
                                      ctypes.c_int, ctypes.POINTER(wt.SIZE)]
g32.SetBkMode.restype = ctypes.c_int
g32.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
g32.SetTextColor.restype = ctypes.c_uint32
g32.SetTextColor.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
# ⚠️ DrawTextW 在 user32，不在 gdi32（这个坑会直接 AttributeError）
u32.DrawTextW.restype = ctypes.c_int
u32.DrawTextW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, ctypes.c_int,
                          ctypes.POINTER(RECT), wt.UINT]
g32.Ellipse.restype = wt.BOOL
g32.Ellipse.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                        ctypes.c_int, ctypes.c_int]
g32.MoveToEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                         ctypes.c_void_p]
g32.LineTo.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
g32.CreateRoundRectRgn.restype = ctypes.c_void_p
g32.CreateRoundRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int]

SM_CXSCREEN, SM_CYSCREEN = 0, 1


class Toast:
    """光标旁提示条。线程安全：show() 可从任意线程调用。"""

    def __init__(self, font_name=FONT_NAME, font_h=FONT_H):
        self.font_name = font_name
        self.font_h = font_h
        self._hwnd = None
        self._wndproc = None
        self._thread = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._payload = ("", True)
        self._font = None
        self._brect = RECT(0, 0, 0, 0)
        self._ok = False
        self.err = None

    # -------------------------------------------------- 对外
    def start(self, timeout=5.0):
        self._thread = threading.Thread(target=self._run, name="wx-toast",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._ok

    def show(self, text, ok=True, seconds=1.5):
        """弹出提示条。text=要显示的话；ok=True 绿勾 / False 红叉。"""
        if not self._hwnd:
            return False
        with self._lock:
            self._payload = (str(text), bool(ok), float(seconds))
        try:
            u32.PostMessageW(self._hwnd, WM_TOAST_SHOW, 0, 0)
            return True
        except Exception:
            return False

    def close(self):
        if self._hwnd:
            try:
                u32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass

    # -------------------------------------------------- 内部
    def _measure(self, hdc, text):
        sz = wt.SIZE()
        g32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(sz))
        return int(sz.cx), int(sz.cy)

    def _do_show(self):
        with self._lock:
            text, ok, seconds = self._payload
        hdc = u32.GetDC(self._hwnd)
        old_font = None
        try:
            if not self._font:
                self._font = g32.CreateFontW(
                    self.font_h, 0, 0, 0, FW_NORMAL, 0, 0, 0,
                    DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0, self.font_name)
            old_font = g32.SelectObject(hdc, self._font)
            tw, th = self._measure(hdc, text)
        finally:
            if old_font:
                g32.SelectObject(hdc, old_font)
            u32.ReleaseDC(self._hwnd, hdc)

        w = PAD_H * 2 + DOT_R * 2 + DOT_GAP + tw
        h = PAD_V * 2 + max(th, DOT_R * 2)
        w, h = max(w, 90), max(h, 30)

        # 位置：光标右下 14px；靠屏幕边缘就翻到另一侧
        pt = wt.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        sw, sh = u32.GetSystemMetrics(SM_CXSCREEN), u32.GetSystemMetrics(SM_CYSCREEN)
        x, y = pt.x + 14, pt.y + 18
        if x + w > sw - 6:
            x = pt.x - w - 14
        if y + h > sh - 6:
            y = pt.y - h - 18
        x = max(4, min(x, sw - w - 4))
        y = max(4, min(y, sh - h - 4))

        rgn = g32.CreateRoundRectRgn(0, 0, w + 1, h + 1, RADIUS, RADIUS)
        if rgn:
            u32.SetWindowRgn(self._hwnd, rgn, True)
        u32.SetWindowPos(self._hwnd, wt.HWND(-1), x, y, w, h, 0x0010 | 0x0040)
        u32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
        u32.InvalidateRect(self._hwnd, None, True)
        u32.SetTimer(self._hwnd, TIMER_HIDE, int(seconds * 1000), None)

    def _paint(self):
        ps = PAINTSTRUCT()
        hdc = u32.BeginPaint(self._hwnd, ctypes.byref(ps))
        try:
            cr = RECT()
            u32.GetClientRect(self._hwnd, ctypes.byref(cr))
            self._brect = cr
            bg = g32.CreateSolidBrush(BG)
            u32.FillRect(hdc, ctypes.byref(cr), bg)
            g32.DeleteObject(bg)

            with self._lock:
                text, ok = self._payload[0], self._payload[1]

            cx = PAD_H + DOT_R
            cy = (cr.bottom - cr.top) // 2
            cc = CLR_GREEN if ok else CLR_RED
            br = g32.CreateSolidBrush(cc)
            pen_border = g32.CreatePen(0, 1, cc)
            old_br = g32.SelectObject(hdc, br)
            old_pen = g32.SelectObject(hdc, pen_border)
            g32.Ellipse(hdc, cx - DOT_R, cy - DOT_R, cx + DOT_R, cy + DOT_R)
            g32.SelectObject(hdc, old_pen)
            g32.SelectObject(hdc, old_br)
            g32.DeleteObject(pen_border)

            # 圆点里画白色的勾 / 叉
            pen_w = g32.CreatePen(0, 2, CLR_WHITE)
            old_pen2 = g32.SelectObject(hdc, pen_w)
            if ok:
                g32.MoveToEx(hdc, cx - 4, cy, None)
                g32.LineTo(hdc, cx - 1, cy + 3)
                g32.LineTo(hdc, cx + 4, cy - 4)
            else:
                g32.MoveToEx(hdc, cx - 3, cy - 3)
                g32.LineTo(hdc, cx + 3, cy + 3)
                g32.MoveToEx(hdc, cx + 3, cy - 3)
                g32.LineTo(hdc, cx - 3, cy + 3)
            g32.SelectObject(hdc, old_pen2)
            g32.DeleteObject(pen_w)
            g32.SelectObject(hdc, old_br)
            g32.DeleteObject(br)

            g32.SetBkMode(hdc, TRANSPARENT)
            g32.SetTextColor(hdc, FG)
            font = self._font or g32.CreateFontW(
                self.font_h, 0, 0, 0, FW_NORMAL, 0, 0, 0, DEFAULT_CHARSET,
                0, 0, CLEARTYPE_QUALITY, 0, self.font_name)
            self._font = font
            of = g32.SelectObject(hdc, font)
            tr = RECT(cx + DOT_R + DOT_GAP, cr.top, cr.right - PAD_H, cr.bottom)
            u32.DrawTextW(hdc, text, len(text), ctypes.byref(tr),
                          DT_LEFT | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX)
            g32.SelectObject(hdc, of)
        finally:
            u32.EndPaint(self._hwnd, ctypes.byref(ps))

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_PAINT:
                self._paint()
                return 0
            if msg == WM_TOAST_SHOW:
                self._do_show()
                return 0
            if msg == WM_TIMER and wparam == TIMER_HIDE:
                u32.KillTimer(hwnd, TIMER_HIDE)
                u32.ShowWindow(hwnd, SW_HIDE)
                return 0
            if msg == WM_CLOSE:
                u32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                u32.PostQuitMessage(0)
                return 0
        except Exception:
            self.err = traceback.format_exc()
        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self):
        try:
            hinst = k32.GetModuleHandleW(None)
            # ⚠️ 必须给取模加括号：`"...%d" % time.time_ns() % 10**9` 会被 Python
            #    解析成 `(... % ns) % 10**9`，第二次是对字符串取模 → TypeError。
            cls = "WxBridgeToastWnd_%d" % (time.time_ns() % 10 ** 9)
            self._wndproc = WNDPROC(self._on_message)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = cls
            if not u32.RegisterClassExW(ctypes.byref(wc)):
                self._fail("RegisterClassExW 失败，err=%s"
                           % ctypes.get_last_error())
                return
            ex = (WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
                  | WS_EX_LAYERED | WS_EX_TRANSPARENT)
            self._hwnd = u32.CreateWindowExW(ex, cls, "WxBridgeToast",
                                             WS_POPUP, 0, 0, 200, 40,
                                             None, None, hinst, None)
            if not self._hwnd:
                self._fail("CreateWindowExW 失败，err=%s"
                           % ctypes.get_last_error())
                return
            u32.SetLayeredWindowAttributes(self._hwnd, 0, TOAST_ALPHA,
                                           LWA_ALPHA)
            self._ok = True
            self._ready.set()
            msg = wt.MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            self._fail("提示条线程异常：\n" + traceback.format_exc())

    def _fail(self, why):
        """建窗口失败时必须**说出来**，否则就是无声的坑（今天刚踩过）"""
        self.err = why
        try:
            sys.stderr.write("[wx_toast] %s\n" % why)
        except Exception:
            pass
        self._ready.set()


# ---------------------------------------------------------------- 自测
def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    err = "--err" in sys.argv
    text = args[0] if args else ("发送失败" if err else "已发到微信")
    t = Toast()
    if not t.start():
        print("提示条窗口创建失败")
        return
    print("已弹出：%s（1.5 秒后自动消失）" % text)
    t.show(text, ok=not err, seconds=1.5)
    time.sleep(2.0)
    t.close()


if __name__ == "__main__":
    main()
