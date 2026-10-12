# -*- coding: utf-8 -*-
r"""微信桥 · 托盘图标 + 全局热键（纯 ctypes，零依赖）

职责有三件：
  1. 在系统托盘挂一个图标，右键能菜单、能退出
  2. 用**低级键盘钩子**锁定 Ctrl+Alt+B（首选），按下时回调 on_hotkey，
     并把按键**吞掉** —— 这样 QQ 的截图之类抢同一个键的程序就收不到，不会再弹
  3. `flash(kind, seconds)` 让托盘图标闪一下（成功绿勾 / 失败红叉），
     作为「已经发到微信了」的视觉回执

⚠️ 关于闪烁：**Windows 根本没有「让托盘图标闪烁」的 API**。Shell_NotifyIcon 只给了
   ADD / MODIFY / DELETE，所谓闪烁只能靠 SetTimer 定时反复 NIM_MODIFY 在两个图标之间
   来回切（社区通行的做法）。而 WM_TIMER 必须由**有消息循环的线程**接收 ——
   正好就是这里的托盘线程，所以定时器装在这。

为什么不用 RegisterHotKey（2026-10-12 实测教训）：
    桥原先用 RegisterHotKey 注册，**注册成功**，但按下去 QQ 的截图同时也弹 ——
    QQ 用的是低级键盘钩子这类"更早拿到按键"的机制，RegisterHotKey 抢不过它。
    改用自己的 WH_KEYBOARD_LL 并在命中时 return 1，QQ 就彻底收不到了。
    （钩子装不上时自动退回 RegisterHotKey 逐个试候选，保证不至于不可用。）

沿用 QQ 桥（qq_tray.py）里那套已经踩过坑的写法：
  - 所有 Win32 调用**显式声明** argtypes/restype（64 位下不声明会被静默截断，
    窗口过程里调 DefWindowProcW 还会抛 ArgumentError 被 ctypes 静默吞掉）
  - 窗口过程对象必须留引用（`self._wndproc`），被 GC 掉 = 进程崩
  - 加图标前先 DELETE 再 ADD（Explorer 重启后会留幽灵图标）
  - 弹菜单前必须 SetForegroundWindow，否则点菜单外面它不消失
  - 处理 "TaskbarCreated" 消息（Explorer 重启后把图标重新挂上）

⚠️ 铁律：**只有托盘真的建起来了，才允许藏窗口、摘 X**。
   否则万一图标没出现（Explorer 异常），用户就再没有任何退出入口了。
"""
import ctypes
import ctypes.wintypes as wt
import os
import threading
import time

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

# ---------------------------------------------------------------- 常量
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_NULL = 0x0000
WM_HOTKEY = 0x0312
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_FLASH = WM_APP + 2        # 请求开始闪烁（可从任意线程 PostMessage 过来）
WM_TIMER = 0x0113
TIMER_FLASH = 1              # 闪烁用定时器 ID

GWL_STYLE = -16
WS_SYSMENU = 0x00080000
SW_HIDE = 0
SW_SHOW = 5
SW_RESTORE = 9

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
NIF_INFO = 0x10
NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x1, 0x2, 0x3

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
IDI_APPLICATION = 32512
SM_CXSMICON = 49

TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800

# 热键
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
HOTKEY_ID = 0xB001

# 主键盘数字 1..9、0 的虚拟键码（0x31..0x39 / 0x30）
DIGITS = [(0x31 + i, str(i + 1)) for i in range(9)] + [(0x30, "0")]
# 小键盘数字 —— 和主键盘是**不同的**组合
NUMPAD = [(0x61 + i, "小键盘%d" % (i + 1)) for i in range(9)]

# 候选热键：
#   2026-10-09 木木实测 —— Ctrl+Alt+字母里的 S/Q/D/X/A 全部被占用（错误码 1409），
#   Ctrl+Alt+数字 1~9、0 全空。
#   **木木指定首选 Ctrl+Alt+B**（实测可注册），故排在第一位；其余按顺序兜底。
#   ⚠️ 避开微信自己的 Ctrl+Alt+W（唤出微信）和 QQ 桥的 Ctrl+B
VK_B = 0x42

# ---------------------------------------------------------------- 低级键盘钩子
# 为什么必须用钩子（2026-10-12 实测）：
#   微信桥用 RegisterHotKey 注册 Ctrl+Alt+B —— **注册成功**（失败才会报 1409），
#   但木木按下后 QQ 的截图**同时也弹出来**。原因是 QQ 用的是 WH_KEYBOARD_LL
#   这类"比 RegisterHotKey 更早拿到按键"的机制：RegisterHotKey 相当于向系统领了
#   一张号码牌，而钩子在号码牌之前就把按键截住了，两者互不冲突 → 一边发消息一边弹截图。
#   解决：自己也装一个 WH_KEYBOARD_LL，命中时 return 1 把按键**吞掉**，
#   QQ 就再也收不到，自然不会弹截图（tools/hook_probe2.py 已实测验证：
#   吞掉后没有任何新窗口冒出来；不吞则 QQ 截图照弹）。
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105

VK_CONTROL_KEY = 0x11
VK_MENU_KEY = 0x12          # Alt
VK_SHIFT_KEY = 0x10
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_LCONTROL, VK_RCONTROL = 0xA2, 0xA3
VK_LALT, VK_RALT = 0xA4, 0xA5
VK_LSHIFT, VK_RSHIFT = 0xA0, 0xA1

CTRL_VKS = (VK_CONTROL_KEY, VK_LCONTROL, VK_RCONTROL)
ALT_VKS = (VK_MENU_KEY, VK_LALT, VK_RALT)
SHIFT_VKS = (VK_SHIFT_KEY, VK_LSHIFT, VK_RSHIFT)
WIN_VKS = (VK_LWIN, VK_RWIN)

# 同一个组合连按的最小间隔（防按住不放时系统自动重复触发刷屏）
HOTKEY_DEBOUNCE = 0.4

HOTKEY_CANDIDATES = (
    [(MOD_CONTROL | MOD_ALT, VK_B, "Ctrl+Alt+B")]          # ← 木木首选
    + [(MOD_CONTROL | MOD_ALT, vk, "Ctrl+Alt+" + nm) for vk, nm in DIGITS]
    + [(MOD_CONTROL | MOD_ALT, vk, "Ctrl+Alt+" + nm) for vk, nm in NUMPAD]
    + [(MOD_CONTROL | MOD_SHIFT | MOD_ALT, vk, "Ctrl+Shift+Alt+" + nm)
       for vk, nm in DIGITS]
)

# RegisterHotKey 失败时 GetLastError 的常见含义
ERR_NAMES = {
    1400: "窗口句柄无效（程序内部问题）",
    1409: "已被别的程序占用",
    1419: "该热键未注册",
    87: "参数非法（程序内部问题）",
}

CMD_SEND_CLIP, CMD_SHOW, CMD_HIDE, CMD_FOLDER, CMD_QUIT, CMD_SCAN, CMD_RESTART, \
    CMD_PREVIEW = 1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008
CMD_NAMES = {
    CMD_SEND_CLIP: "send_clip",
    CMD_SHOW: "show",
    CMD_HIDE: "hide",
    CMD_FOLDER: "folder",
    CMD_QUIT: "quit",
    CMD_SCAN: "scan",
    CMD_RESTART: "restart",
    CMD_PREVIEW: "preview",
}


# ---------------------------------------------------------------- 结构体
class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT), ("style", wt.UINT),
        ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
        ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, ctypes.c_uint,
                             ctypes.c_ulonglong, ctypes.c_longlong)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    """低级键盘钩子回调拿到的按键信息（MSDN LowLevelKeyboardProc）"""
    _fields_ = [
        ("vkCode", wt.DWORD), ("scanCode", wt.DWORD),
        ("flags", wt.DWORD), ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


# LRESULT CALLBACK LowLevelKeyboardProc(int nCode, WPARAM wParam, LPARAM lParam)
HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int,
                              wt.WPARAM, wt.LPARAM)

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
u32.IsWindowVisible.argtypes = [wt.HWND]
u32.IsWindowVisible.restype = wt.BOOL
u32.SetForegroundWindow.argtypes = [wt.HWND]
u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.PostQuitMessage.argtypes = [ctypes.c_int]
u32.GetSystemMetrics.argtypes = [ctypes.c_int]
u32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
u32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
u32.GetMessageW.restype = ctypes.c_int
u32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
u32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
u32.DispatchMessageW.restype = ctypes.c_longlong
u32.RegisterWindowMessageW.restype = wt.UINT
u32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
u32.SetWindowLongPtrW.restype = ctypes.c_longlong
u32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_longlong]
u32.SetTimer.restype = ctypes.c_void_p          # UINT_PTR，必须声明否则被截断
u32.SetTimer.argtypes = [wt.HWND, ctypes.c_void_p, wt.UINT, ctypes.c_void_p]
u32.KillTimer.restype = wt.BOOL
u32.KillTimer.argtypes = [wt.HWND, ctypes.c_void_p]
u32.GetWindowLongPtrW.restype = ctypes.c_longlong
u32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
u32.LoadImageW.restype = wt.HANDLE
u32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int,
                           ctypes.c_int, wt.UINT]
u32.LoadIconW.restype = wt.HANDLE
u32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
u32.RegisterHotKey.restype = wt.BOOL
u32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
u32.UnregisterHotKey.restype = wt.BOOL
u32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]

# ---- 低级键盘钩子（抢键用）----
# ⚠️ 钩子回调是在**安装它的那个线程**的消息循环里被调用的，
#    所以必须装在托盘的托盘线程（它有 GetMessage 循环），不能装在纯 sleep 的线程。
u32.SetWindowsHookExW.restype = ctypes.c_void_p          # HHOOK
u32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                  ctypes.c_void_p, wt.DWORD]
u32.UnhookWindowsHookEx.restype = wt.BOOL
u32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
u32.CallNextHookEx.restype = ctypes.c_longlong
u32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                               wt.WPARAM, wt.LPARAM]
u32.CreatePopupMenu.restype = wt.HMENU
u32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
u32.TrackPopupMenu.restype = ctypes.c_int
u32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, wt.HWND, ctypes.c_void_p]

# ⚠️ GetConsoleWindow 在 kernel32，不在 user32
k32.GetConsoleWindow.restype = wt.HWND
k32.GetModuleHandleW.restype = wt.HINSTANCE
k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]

shell32.Shell_NotifyIconW.restype = wt.BOOL
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

# ⚠️ 读错误码必须用 use_last_error=True 的句柄：
#    ctypes.windll 不保存 last error，ctypes.get_last_error() 会永远返回 0，
#    之前的代码就是靠猜才把 1400(句柄无效) 也报成"被占用"。
_u32e = ctypes.WinDLL("user32", use_last_error=True)
_u32e.RegisterHotKey.restype = wt.BOOL
_u32e.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
_u32e.UnregisterHotKey.restype = wt.BOOL
_u32e.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]


def try_register_hotkey(hwnd, mods, vk, hotkey_id=HOTKEY_ID):
    """试注册一个全局热键，返回 (ok, err_code, 说明)。

    hwnd 传 None 也合法（热键挂到当前线程），所以检测工具不必建窗口。
    """
    if _u32e.RegisterHotKey(hwnd, hotkey_id, mods | MOD_NOREPEAT, vk):
        return True, 0, "可用"
    err = ctypes.get_last_error()
    return False, err, "%s（%s）" % (ERR_NAMES.get(err, "注册失败"), err)


def unregister_hotkey(hwnd, hotkey_id=HOTKEY_ID):
    try:
        return bool(_u32e.UnregisterHotKey(hwnd, hotkey_id))
    except Exception:
        return False


def scan_hotkeys():
    """从**当前进程**试注册每个候选组合，返回 (可用列表, 明细行列表)。

    ⚠️ 热键是全局资源：本进程自己已经占着的那个会显示成"被占用"，属正常。
    ⚠️ **盲区（2026-10-12 实测踩到）**：这里测的只是 RegisterHotKey，
       只能发现"用 RegisterHotKey 占键"的程序；对用**低级键盘钩子**抢键的程序
       （QQ 的截图正是这一类）**完全测不出来** —— 它会显示"可用"，
       但真按下去还是被 QQ 抢走。所以本工具的结论只能当参考；
       要确认某个组合干不干净，得**真人按一下**才算数。
       微信桥现在改用钩子抢键，已经不怕这类抢占了。
    """
    ok_list, lines = [], []
    for i, (mods, vk, label) in enumerate(HOTKEY_CANDIDATES):
        hid = 0xB100 + i          # 每次换一个 id，避免 id 冲撞
        ok, err, desc = try_register_hotkey(None, mods, vk, hid)
        if ok:
            unregister_hotkey(None, hid)
            ok_list.append(label)
        lines.append("%s %-20s %s" % ("✅" if ok else "❌", label,
                                      "可用" if ok else desc))
    return ok_list, lines


def available():
    return os.name == "nt" and hasattr(ctypes, "windll")


# ---------------------------------------------------------------- 控制台窗口
def console_hwnd():
    try:
        return k32.GetConsoleWindow() or None
    except Exception:
        return None


def show_console():
    h = console_hwnd()
    if not h:
        return False
    u32.ShowWindow(h, SW_SHOW)
    u32.ShowWindow(h, SW_RESTORE)
    try:
        u32.SetForegroundWindow(h)
    except Exception:
        pass
    return True


def hide_console():
    h = console_hwnd()
    if not h:
        return False
    u32.ShowWindow(h, SW_HIDE)
    return not u32.IsWindowVisible(h)


def strip_close_button():
    h = console_hwnd()
    if not h:
        return None
    try:
        style = u32.GetWindowLongPtrW(h, GWL_STYLE)
        if not (style & WS_SYSMENU):
            return style
        u32.SetWindowLongPtrW(h, GWL_STYLE, style & ~WS_SYSMENU)
        return style
    except Exception:
        return None


def restore_style(style):
    h = console_hwnd()
    if not h or style is None:
        return
    try:
        u32.SetWindowLongPtrW(h, GWL_STYLE, style)
    except Exception:
        pass


# ---------------------------------------------------------------- 托盘 + 热键
class TrayHotkey:
    """托盘图标 + 全局热键，共用一个消息循环。

    回调（都在工作线程里跑，不会卡住消息循环）：
      on_command(name)   托盘菜单命令，name ∈ {'send_clip','show','hide','folder','quit'}
      on_hotkey()        全局热键按下
      on_message(text)   非致命但值得记录的事（写日志用）
    """

    def __init__(self, tooltip, icon_path=None, on_command=None,
                 on_hotkey=None, on_message=None, hotkey=True,
                 show_icon=True, manage_console=True,
                 icon_ok=None, icon_err=None):
        """
        show_icon=False    只建窗口不挂托盘图标（测试脚本用，免得冒出重复图标）
        manage_console=False  不去动调用者的控制台（测试脚本用）
        icon_ok / icon_err  闪烁时交替显示的两个图标（成功绿勾 / 失败红叉）
        """
        self.tooltip = (tooltip or "")[:127]
        self.icon_path = icon_path
        self.icon_ok = icon_ok
        self.icon_err = icon_err
        self.on_command = on_command
        self.on_hotkey = on_hotkey
        self.on_message = on_message
        self.want_hotkey = hotkey
        self.show_icon = show_icon
        self.manage_console = manage_console

        self._hwnd = None
        self._hicon = None
        self._menu = None
        self._wndproc = None
        self._thread = None
        self._ready = threading.Event()
        self._ok = False
        self._hotkey_ok = False
        self.hotkey_name = ""
        self._taskbar_created = 0
        self._old_style = None
        self._stripped = False

        # ---- 图标闪烁（2026-10-12 新增）----
        # Windows 没有「让托盘图标闪烁」的 API，只能 SetTimer + 反复 NIM_MODIFY
        # 在两个图标之间切换来伪造闪烁。
        self._icon_cache = {}        # path -> HICON（避免每次闪烁都读磁盘）
        self._flash_lock = threading.Lock()
        self._flash_kind = ""        # "" / "ok" / "err"
        self._flash_until = 0.0
        self._flash_interval = 0.5
        self._flash_on = False
        self._flash_hicon = None

        # ---- 钩子抢键（2026-10-12 新增）----
        self._hook = None            # HHOOK（钩子句柄）
        self._hook_proc = None       # 必须留引用：被 GC 回收 = 进程直接崩
        self._hotkey_mods = 0        # 当前锁定的修饰键
        self._hotkey_vk = 0          # 当前锁定的主键
        self._mods = {"ctrl": False, "alt": False, "shift": False, "win": False}
        self._last_fire = 0.0        # 去抖用
        self.hotkey_mode = ""        # "hook" = 钩子抢键 / "register" = 退化为注册热键

    # -------------------------------------------------- 对外
    def start(self, timeout=5.0):
        if not available():
            return False
        self._thread = threading.Thread(target=self._run, name="wx-tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._ok

    @property
    def hotkey_ok(self):
        return self._hotkey_ok

    def stop(self):
        if self._hwnd:
            try:
                u32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass

    def cleanup(self):
        self._restore_ui()

    def notify(self, title, text, level=NIIF_INFO):
        """弹托盘气泡通知（窗口缩起来后，用户就靠这个看结果）"""
        if not self._hwnd or not self._ok:
            return False
        try:
            nid = self._nid()
            nid.uFlags = NIF_INFO
            nid.szInfoTitle = str(title)[:63]
            nid.szInfo = str(text)[:255]
            nid.dwInfoFlags = level
            nid.uVersion = 0
            return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))
        except Exception as e:
            self._note("气泡通知失败: %r" % e)
            return False

    def _set_tip(self, text):
        """更新托盘悬浮提示（热键定下来之后才知道该写哪个，所以得能改）"""
        self.tooltip = (text or "")[:127]
        if not self._ok:
            return False
        try:
            nid = self._nid()
            nid.uFlags = NIF_TIP
            nid.szTip = self.tooltip
            return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))
        except Exception:
            return False

    # -------------------------------------------------- 图标闪烁
    def _load_icon_file(self, path):
        """按路径加载图标并**缓存句柄**（闪烁时每 0.5 秒要换一次，不能每次读盘）"""
        if not path:
            return None
        if path in self._icon_cache:
            return self._icon_cache[path]
        h = None
        try:
            if os.path.exists(path):
                size = u32.GetSystemMetrics(SM_CXSMICON) or 16
                h = u32.LoadImageW(None, path, IMAGE_ICON, size, size,
                                   LR_LOADFROMFILE)
        except Exception:
            h = None
        self._icon_cache[path] = h
        return h

    def _set_icon(self, hicon):
        """只换图标，不动其他字段"""
        if not (self._hwnd and self._ok and hicon):
            return False
        try:
            nid = self._nid()
            nid.uFlags = NIF_ICON
            nid.hIcon = hicon
            return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))
        except Exception as e:
            self._note("换托盘图标失败: %r" % e)
            return False

    def flash(self, kind="ok", seconds=2.5, interval=0.5):
        """让托盘图标闪一下（**可从任意线程调用**）。

        Windows 没有闪烁 API：这里是 SetTimer + 反复 NIM_MODIFY 在两个图标间切换。
        kind="ok"  → 在「正常图标」和「绿勾图标」之间切
        kind="err" → 在「正常图标」和「红叉图标」之间切
        """
        if not (self._hwnd and self._ok and self.show_icon):
            return False
        with self._flash_lock:
            self._flash_kind = "err" if kind == "err" else "ok"
            self._flash_until = time.time() + max(0.4, float(seconds))
            self._flash_interval = max(0.15, min(float(interval), 1.0))
            self._flash_on = True     # 立刻切到高亮图标，不等第一个 tick
        try:
            u32.PostMessageW(self._hwnd, WM_FLASH, 0, 0)
            return True
        except Exception:
            return False

    def _flash_start(self):
        with self._flash_lock:
            kind = self._flash_kind
            interval = self._flash_interval
        path = self.icon_ok if kind == "ok" else self.icon_err
        h = self._load_icon_file(path)
        if not h:
            self._note("闪烁图标加载失败（%s），跳过本次闪烁" % path)
            return
        self._flash_hicon = h
        u32.SetTimer(self._hwnd, TIMER_FLASH, int(interval * 1000), None)
        self._set_icon(h)

    def _flash_tick(self):
        if time.time() >= self._flash_until:
            u32.KillTimer(self._hwnd, TIMER_FLASH)
            self._set_icon(self._hicon)      # 变回普通图标
            with self._flash_lock:
                self._flash_kind = ""
            return
        self._flash_on = not self._flash_on
        self._set_icon(self._flash_hicon if self._flash_on else self._hicon)

    def _flash_stop(self):
        try:
            if self._hwnd:
                u32.KillTimer(self._hwnd, TIMER_FLASH)
        except Exception:
            pass
        with self._flash_lock:
            self._flash_kind = ""
        if self._hicon:
            self._set_icon(self._hicon)

    def _restore_ui(self):
        self._flash_stop()
        if self.show_icon:
            self._remove()
        self._uninstall_hook()
        if self._hwnd:
            try:
                u32.UnregisterHotKey(self._hwnd, HOTKEY_ID)
            except Exception:
                pass
        self._hotkey_ok = False
        if self._stripped:
            restore_style(self._old_style)
            self._stripped = False
        if self.manage_console:
            show_console()

    # -------------------------------------------------- 钩子抢键
    def _mods_match(self):
        """当前按住的修饰键是否满足锁定组合（多按了别的修饰键也放行）"""
        need, m = self._hotkey_mods, self._mods
        if need & MOD_CONTROL and not m["ctrl"]:
            return False
        if need & MOD_ALT and not m["alt"]:
            return False
        if need & MOD_SHIFT and not m["shift"]:
            return False
        if need & MOD_WIN and not m["win"]:
            return False
        return True

    def _update_mods(self, vk, down):
        if vk in CTRL_VKS:
            self._mods["ctrl"] = down
        elif vk in ALT_VKS:
            self._mods["alt"] = down
        elif vk in SHIFT_VKS:
            self._mods["shift"] = down
        elif vk in WIN_VKS:
            self._mods["win"] = down

    def _kb_proc(self, nCode, wParam, lParam):
        """低级键盘钩子过程：命中锁定组合就**吞掉**（return 1），QQ 再也收不到。

        ⚠️ 回调里绝不能干重活（抓字+发消息要 4 秒），否则整个系统的键盘都会卡；
           所以命中后只负责"起线程 + 吞掉"。
        """
        try:
            if nCode == 0:
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk = int(kb.vkCode)
                down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                if vk in CTRL_VKS or vk in ALT_VKS or vk in SHIFT_VKS or vk in WIN_VKS:
                    self._update_mods(vk, down)
                elif down and vk == self._hotkey_vk and self._mods_match():
                    now = time.time()
                    if now - self._last_fire >= HOTKEY_DEBOUNCE:
                        self._last_fire = now
                        if self.on_hotkey:
                            threading.Thread(target=self.on_hotkey,
                                             daemon=True).start()
                    return 1        # ← 吞掉：QQ 等下游程序全收不到
        except Exception as e:
            self._note("钩子回调异常: %r" % e)
        return u32.CallNextHookEx(None, nCode, wParam, lParam)

    def _install_hook(self, mods, vk, label):
        """装低级键盘钩子并锁定 (mods, vk)。返回是否成功。"""
        self._hotkey_mods = mods
        self._hotkey_vk = vk
        self._hook_proc = HOOKPROC(self._kb_proc)   # 留引用，别让 GC 收走
        # WH_KEYBOARD_LL 的钩子过程在本进程内，hMod 传 NULL 即可
        self._hook = u32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_proc,
                                           None, 0)
        if not self._hook:
            try:
                err = ctypes.get_last_error()
            except Exception:
                err = 0
            self._note("⚠️ 装键盘钩子失败（错误码 %s），改用注册热键" % err)
            self._hook_proc = None
            return False
        self._hotkey_ok = True
        self.hotkey_mode = "hook"
        self.hotkey_name = label
        self._note("✅ 全局热键已就绪：%s（钩子抢键模式，QQ 不会再抢）" % label)
        return True

    def _uninstall_hook(self):
        if self._hook:
            try:
                u32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
        self._hook = None
        self._hook_proc = None

    # -------------------------------------------------- 内部
    def _note(self, text):
        if self.on_message:
            try:
                self.on_message(text)
            except Exception:
                pass

    def _fire(self, name):
        if not self.on_command:
            return
        try:
            self.on_command(name)
        except Exception as e:
            self._note("托盘命令 %s 处理出错: %r" % (name, e))

    def _icon(self):
        h = self._load_icon_file(self.icon_path)
        if h:
            return h
        if self.icon_path and os.path.exists(self.icon_path):
            self._note("托盘图标 LoadImage 失败，改用系统默认图标")
        return u32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))

    def _nid(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self.tooltip
        return nid

    def _add(self):
        nid = self._nid()
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            return True
        return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))

    def _remove(self):
        if not self._hwnd:
            return
        try:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))
        except Exception:
            pass

    def _build_menu(self):
        if not self._menu:
            hk = ("（%s）" % self.hotkey_name) if self._hotkey_ok else ""
            m = u32.CreatePopupMenu()
            u32.AppendMenuW(m, MF_STRING, CMD_SEND_CLIP,
                            "发送剪贴板里的文字" + hk)
            u32.AppendMenuW(m, MF_STRING, CMD_SCAN, "热键检测（看哪些组合可用）")
            u32.AppendMenuW(m, MF_STRING, CMD_PREVIEW,
                            "预览提示效果（闪烁 + 光标提示）")
            u32.AppendMenuW(m, MF_SEPARATOR, 0, None)
            u32.AppendMenuW(m, MF_STRING, CMD_SHOW, "显示日志窗口")
            u32.AppendMenuW(m, MF_STRING, CMD_HIDE, "隐藏窗口（缩回托盘）")
            u32.AppendMenuW(m, MF_STRING, CMD_FOLDER, "打开工作文件夹")
            u32.AppendMenuW(m, MF_SEPARATOR, 0, None)
            u32.AppendMenuW(m, MF_STRING, CMD_RESTART, "重启微信桥")
            u32.AppendMenuW(m, MF_STRING, CMD_QUIT, "退出微信桥")
            self._menu = m
        return self._menu

    def _popup(self):
        try:
            u32.SetForegroundWindow(self._hwnd)
        except Exception:
            pass
        pt = wt.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        cmd = u32.TrackPopupMenu(self._build_menu(),
                                 TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                 pt.x, pt.y, 0, self._hwnd, None)
        try:
            u32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        except Exception:
            pass
        name = CMD_NAMES.get(cmd)
        if name:
            self._fire(name)

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_HOTKEY and wparam == HOTKEY_ID:
                if self.on_hotkey:
                    # ⚠️ 必须另起线程：回调里要抓字+发消息，会阻塞消息循环
                    threading.Thread(target=self.on_hotkey, daemon=True).start()
                return 0
            if msg == WM_FLASH:
                self._flash_start()
                return 0
            if msg == WM_TIMER and wparam == TIMER_FLASH:
                self._flash_tick()
                return 0
            if msg == WM_TRAY:
                if lparam == WM_RBUTTONUP:
                    self._popup()
                elif lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._fire("show")
                return 0
            if self._taskbar_created and msg == self._taskbar_created:
                if self._add():
                    self._note("资源管理器重启过，托盘图标已重新挂上")
                else:
                    self._note("资源管理器重启后，托盘图标没能重新挂上")
                return 0
            if msg == WM_CLOSE:
                self._restore_ui()
                u32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                u32.PostQuitMessage(0)
                return 0
        except Exception as e:
            self._note("托盘消息处理出错: %r" % e)
        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self):
        try:
            self._taskbar_created = u32.RegisterWindowMessageW("TaskbarCreated")
            hinst = k32.GetModuleHandleW(None)
            cls = "WxBridgeTrayWnd_%d" % os.getpid()
            self._wndproc = WNDPROC(self._on_message)

            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = cls
            if not u32.RegisterClassExW(ctypes.byref(wc)):
                self._note("RegisterClassExW 失败，托盘没起来")
                return

            self._hwnd = u32.CreateWindowExW(0, cls, "WxBridgeTray", 0,
                                             0, 0, 0, 0, None, None, hinst, None)
            if not self._hwnd:
                self._note("CreateWindowExW 失败，托盘没起来")
                return

            self._hicon = self._icon()
            if self.show_icon:
                if not self._add():
                    self._note("Shell_NotifyIcon(NIM_ADD) 失败，托盘没起来")
                    u32.DestroyWindow(self._hwnd)
                    self._hwnd = None
                    return

            self._ok = True

            # 全局热键：**优先用钩子抢键**（能压住 QQ 这类抢键程序），
            # 钩子装不上（极罕见）才退回 RegisterHotKey 逐个试候选。
            if self.want_hotkey:
                mods0, vk0, label0 = HOTKEY_CANDIDATES[0]
                if self._install_hook(mods0, vk0, label0):
                    self._set_tip("微信桥 · %s 发送选中文字" % label0)
                else:
                    failed = []
                    for mods, vk, label in HOTKEY_CANDIDATES:
                        ok, err, desc = try_register_hotkey(self._hwnd, mods, vk)
                        if ok:
                            self._hotkey_ok = True
                            self.hotkey_mode = "register"
                            self.hotkey_name = label
                            self._note("✅ 全局热键已注册：%s（注册热键模式）" % label)
                            break
                        failed.append("%s=%s" % (label, desc))
                    if not self._hotkey_ok:
                        self._note("⚠️ 钩子和 %d 个候选热键全部失败：%s"
                                   % (len(failed), "；".join(failed)))
                        self._note("   可跑 tools/hotkey_scan.py 看这台机器上哪些组合可用，"
                                   "或改用托盘菜单「发送剪贴板」")
                    else:
                        # 把热键名挂到托盘悬浮提示上，用户鼠标一停就知道按哪个
                        self._set_tip("微信桥 · %s 发送选中文字" % self.hotkey_name)

            self._ready.set()      # ← 到这里才算"托盘真的好了"

            # 铁律：只有到这里才允许藏窗口 / 摘 X
            if self.manage_console:
                self._old_style = strip_close_button()
                self._stripped = self._old_style is not None
                hide_console()

            msg = wt.MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            self._note("托盘线程异常: %r" % e)
        finally:
            self._ready.set()
