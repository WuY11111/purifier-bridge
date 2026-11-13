"""托盘图标（纯 ctypes 调 Shell_NotifyIcon，不依赖第三方包）。

用法：
    tray = Tray(root, tooltip="散热管家", actions={...})
    tray.start()          # 内部线程跑消息循环
    tray.notify("标题", "正文")   # 气泡提示
    tray.remove()         # 退出前删除图标
"""

from __future__ import annotations

import math
import os
import struct
import threading
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_TRAY = 0x8001          # WM_APP + 1

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_WARNING = 0x01, 0x02

TPM_LEFTALIGN, TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0000, 0x0002, 0x0100
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040

ID_SHOW, ID_PAUSE, ID_RELEASE_EXIT = 1, 2, 3
ID_PRESET_QUIET, ID_PRESET_BALANCE, ID_PRESET_COOL = 4, 5, 6


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


def make_icon(path: str, size: int = 32) -> bool:
    """画一个「风扇」小图标并写成 .ico（BMP 形式，兼容性最好）。"""
    if os.path.exists(path):
        return True
    try:
        cx = cy = (size - 1) / 2.0
        r_out = size * 0.46
        blue = (0x4F, 0x8C, 0xFF)      # RGB
        rows = []
        for y in range(size):
            row = []
            for x in range(size):
                dx, dy = x - cx, y - cy
                d = math.hypot(dx, dy)
                a = 0
                rgb = (0, 0, 0)
                if d <= r_out + 1:
                    a = int(max(0.0, min(1.0, (r_out + 1 - d))) * 255)
                    rgb = blue
                    # 三片白色扇叶
                    for k in range(3):
                        th = math.radians(k * 120)
                        rx = dx * math.cos(-th) - dy * math.sin(-th)
                        ry = dx * math.sin(-th) + dy * math.cos(-th)
                        ry2 = ry + size * 0.20   # 往一个方向偏，像扇叶
                        if (rx / (size * 0.30)) ** 2 + (ry2 / (size * 0.12)) ** 2 <= 1.0:
                            rgb = (255, 255, 255)
                            break
                row.append((rgb[2], rgb[1], rgb[0], a))   # BGRA
            rows.append(row)

        xor = bytearray()
        for y in range(size - 1, -1, -1):        # 位图自下而上
            for (b, g, r, a) in rows[y]:
                xor += bytes((b, g, r, a))
        and_mask = bytearray(((size + 31) // 32) * 4 * size)

        bih = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
        image = bih + bytes(xor) + bytes(and_mask)
        header = struct.pack("<HHH", 0, 1, 1)
        entry = struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(image), 22)
        with open(path, "wb") as fh:
            fh.write(header + entry + image)
        return True
    except Exception:
        return False


class Tray(threading.Thread):
    """托盘图标 + 右键菜单 + 气泡提示。"""

    def __init__(self, icon_path: str, tooltip: str = "散热管家",
                 on_action=None, log=None) -> None:
        super().__init__(daemon=True)
        self.icon_path = icon_path
        self.tooltip = tooltip[:127]
        self.on_action = on_action or (lambda _id: None)
        self.log = log
        self.hwnd = None
        self.hicon = None
        self._nid = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._wndproc = None

    # -- 供外部调用 -------------------------------------------------------
    def wait_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)

    def notify(self, title: str, msg: str, warn: bool = False) -> None:
        if not self.hwnd:
            return
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_INFO
        nid.szInfo = (msg or "")[:255]
        nid.szInfoTitle = (title or "")[:63]
        nid.dwInfoFlags = NIIF_WARNING if warn else NIIF_INFO
        nid.uVersion = 10000
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def set_tooltip(self, text: str) -> None:
        """更新鼠标悬停提示（托盘气泡下方的文字）。"""
        if not self.hwnd:
            return
        self.tooltip = (text or "")[:127]
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_TIP
        nid.szTip = self.tooltip
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def remove(self) -> None:
        self._stop.set()
        if self.hwnd and self._nid is not None:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self.hwnd = None
        try:
            user32.PostMessageW(self.hwnd or 0, WM_DESTROY, 0, 0)
        except Exception:
            pass

    # -- 线程体 -----------------------------------------------------------
    def run(self) -> None:
        try:
            hinst = kernel32.GetModuleHandleW(None)
            cls_name = "PurifierTrayWnd"

            def _proc(hwnd, msg, wparam, lparam):
                try:
                    if msg == WM_TRAY:
                        low = lparam & 0xFFFF
                        if low in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                            self.on_action(ID_SHOW)
                        elif low == WM_RBUTTONUP:
                            self._popup(hwnd)
                    elif msg == WM_COMMAND:
                        self.on_action(wparam & 0xFFFF)
                    elif msg == WM_DESTROY:
                        user32.PostQuitMessage(0)
                        return 0
                except Exception:
                    pass
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._wndproc = WNDPROC(_proc)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            if not user32.RegisterClassW(ctypes.byref(wc)):
                err = kernel32.GetLastError()
                if err != 1410:      # 1410 = class already exists
                    if self.log:
                        self.log.warning("RegisterClass 失败: %s", err)
                    return

            self.hwnd = user32.CreateWindowExW(
                0, cls_name, "PurifierTray", 0, 0, 0, 0, 0,
                None, None, hinst, None)
            if not self.hwnd:
                if self.log:
                    self.log.warning("CreateWindowEx 失败")
                return

            if os.path.exists(self.icon_path):
                self.hicon = user32.LoadImageW(None, self.icon_path, IMAGE_ICON,
                                              32, 32, LR_LOADFROMFILE)
            if not self.hicon:
                self.hicon = user32.LoadIconW(None, ctypes.c_wchar_p(32512))  # IDI_APPLICATION

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = self.hicon
            nid.szTip = self.tooltip
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                if self.log:
                    self.log.warning("Shell_NotifyIcon 添加失败")
                return
            self._nid = nid
            self._ready.set()
            if self.log:
                self.log.info("托盘图标已创建")

            msg = wintypes.MSG()
            while not self._stop.is_set():
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as ex:
            if self.log:
                self.log.warning("托盘线程异常：%s", ex)
        finally:
            self._ready.set()

    def _popup(self, hwnd) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_SHOW, "显示窗口")
        user32.AppendMenuW(menu, MF_STRING, ID_PAUSE, "暂停 / 继续控制")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_PRESET_QUIET, "预设：静音")
        user32.AppendMenuW(menu, MF_STRING, ID_PRESET_BALANCE, "预设：均衡")
        user32.AppendMenuW(menu, MF_STRING, ID_PRESET_COOL, "预设：散热")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_RELEASE_EXIT, "交还自动并退出")
        user32.SetForegroundWindow(hwnd)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        cmd = user32.TrackPopupMenu(menu, TPM_LEFTALIGN | TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, 0, 0, 0)   # 让菜单正常关闭（MSDN 要求）
        user32.DestroyMenu(menu)
        if cmd:
            self.on_action(cmd)
