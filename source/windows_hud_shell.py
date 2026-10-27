"""Windows tray and global hotkeys, standard library only, owned by GUI thread."""
import ctypes as C
from ctypes import wintypes as W
import os
import queue
import uuid


class WindowsHUDShell:
    CALLBACK = 0x8001
    HOTKEY = 0x5148
    PASS_HOTKEY = 0x5150

    def __init__(self):
        self.events = queue.SimpleQueue()
        self.ready = False
        self.hotkey = False
        self.pass_hotkey = False
        self.clickthrough = False
        self.summary = ''
        self.enabled = False
        self.hidden = False
        self.hwnd = None
        self.error = ''
        if os.name != 'nt':
            self.error = '托盘与全局快捷键仅支持 Windows'
            return
        try:
            self._initialize()
        except Exception as exc:
            self.error = str(exc)
            self.close()

    def _initialize(self):
        self.user = C.WinDLL('user32', use_last_error=True)
        self.shell = C.WinDLL('shell32', use_last_error=True)
        kernel = C.WinDLL('kernel32', use_last_error=True)
        self.proc_type = C.WINFUNCTYPE(C.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        class WindowClass(C.Structure):
            _fields_ = [('style', W.UINT), ('proc', self.proc_type),
                        ('class_extra', C.c_int), ('window_extra', C.c_int),
                        ('instance', W.HINSTANCE), ('icon', W.HICON),
                        ('cursor', W.HANDLE), ('background', W.HBRUSH),
                        ('menu', W.LPCWSTR), ('name', W.LPCWSTR)]
        class NotifyIcon(C.Structure):
            _fields_ = [('size', W.DWORD), ('hwnd', W.HWND), ('id', W.UINT),
                        ('flags', W.UINT), ('callback', W.UINT), ('icon', W.HICON),
                        ('tip', W.WCHAR * 128), ('state', W.DWORD), ('state_mask', W.DWORD),
                        ('info', W.WCHAR * 256), ('version', W.UINT),
                        ('info_title', W.WCHAR * 64), ('info_flags', W.DWORD),
                        ('guid', C.c_byte * 16), ('balloon_icon', W.HICON)]
        def signature(dll, name, args, result):
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, result
            return fn
        signature(kernel, 'GetModuleHandleW', [W.LPCWSTR], W.HMODULE)
        signature(self.user, 'RegisterClassW', [C.POINTER(WindowClass)], W.ATOM)
        signature(self.user, 'UnregisterClassW', [W.LPCWSTR, W.HINSTANCE], W.BOOL)
        signature(self.user, 'CreateWindowExW', [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                  C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID], W.HWND)
        signature(self.user, 'DefWindowProcW', [W.HWND, W.UINT, W.WPARAM, W.LPARAM], C.c_ssize_t)
        signature(self.user, 'DestroyWindow', [W.HWND], W.BOOL)
        signature(self.user, 'RegisterHotKey', [W.HWND, C.c_int, W.UINT, W.UINT], W.BOOL)
        signature(self.user, 'UnregisterHotKey', [W.HWND, C.c_int], W.BOOL)
        signature(self.user, 'RegisterWindowMessageW', [W.LPCWSTR], W.UINT)
        signature(self.user, 'LoadIconW', [W.HINSTANCE, C.c_void_p], W.HICON)
        signature(self.shell, 'Shell_NotifyIconW', [W.DWORD, C.POINTER(NotifyIcon)], W.BOOL)
        signature(self.user, 'CreatePopupMenu', [], W.HMENU)
        signature(self.user, 'AppendMenuW', [W.HMENU, W.UINT, C.c_size_t, W.LPCWSTR], W.BOOL)
        signature(self.user, 'GetCursorPos', [C.POINTER(W.POINT)], W.BOOL)
        signature(self.user, 'SetForegroundWindow', [W.HWND], W.BOOL)
        signature(self.user, 'TrackPopupMenu', [W.HMENU, W.UINT, C.c_int, C.c_int, C.c_int, W.HWND, C.c_void_p], W.UINT)
        signature(self.user, 'DestroyMenu', [W.HMENU], W.BOOL)
        signature(self.user, 'PostMessageW', [W.HWND, W.UINT, W.WPARAM, W.LPARAM], W.BOOL)
        self.instance = kernel.GetModuleHandleW(None)
        self.class_name = 'CodexHUDShell_' + uuid.uuid4().hex
        self.taskbar_created = self.user.RegisterWindowMessageW('TaskbarCreated')
        self.proc = self.proc_type(self._window_proc)
        self.window_class = WindowClass()
        self.window_class.proc = self.proc
        self.window_class.instance = self.instance
        self.window_class.name = self.class_name
        self.atom = self.user.RegisterClassW(C.byref(self.window_class))
        if not self.atom:
            raise C.WinError(C.get_last_error())
        self.hwnd = self.user.CreateWindowExW(0, self.class_name, 'Codex HUD tray', 0,
                                             0, 0, 0, 0, None, None, self.instance, None)
        if not self.hwnd:
            raise C.WinError(C.get_last_error())
        self.icon_data = NotifyIcon()
        self.icon_data.size = C.sizeof(NotifyIcon)
        self.icon_data.hwnd, self.icon_data.id = self.hwnd, 1
        self.icon_data.flags = 1 | 2 | 4
        self.icon_data.callback = self.CALLBACK
        self.icon_data.icon = self.user.LoadIconW(None, C.c_void_p(32512))
        self.icon_data.tip = 'Codex 额度 HUD · 自动接续已关闭'
        self.ready = bool(self.shell.Shell_NotifyIconW(0, C.byref(self.icon_data)))
        if not self.ready:
            self.error = '无法创建系统托盘图标'
        self.hotkey = bool(self.user.RegisterHotKey(self.hwnd, self.HOTKEY, 0x4000 | 1 | 2, ord('H')))
        self.pass_hotkey = bool(self.user.RegisterHotKey(self.hwnd, self.PASS_HOTKEY, 0x4000 | 1 | 2, ord('P')))

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == 0x0312 and wparam == self.HOTKEY:
                self.events.put('visibility')
                return 0
            if message == 0x0312 and wparam == self.PASS_HOTKEY:
                self.events.put('clickthrough')
                return 0
            if message == self.CALLBACK:
                if lparam == 0x0203:  # left double click
                    self.events.put('show')
                elif lparam == 0x0205:  # right release
                    self._menu(hwnd)
                return 0
            if message == self.taskbar_created:
                self.ready = bool(self.shell.Shell_NotifyIconW(0, C.byref(self.icon_data)))
                if not self.ready:
                    self.events.put('tray_failed')
                return 0
        except Exception as exc:
            self.error = str(exc)
            self.events.put('shell_error')
        return self.user.DefWindowProcW(hwnd, message, wparam, lparam)

    def _menu(self, hwnd):
        menu = self.user.CreatePopupMenu()
        if not menu:
            return
        try:
            self.user.AppendMenuW(menu, 0, 1, '显示 HUD' if self.hidden else '隐藏到托盘')
            self.user.AppendMenuW(menu, 0, 2, '关闭自动接续' if self.enabled else '开启自动接续')
            self.user.AppendMenuW(menu, 0, 4, '关闭鼠标穿透' if self.clickthrough else '开启鼠标穿透')
            self.user.AppendMenuW(menu, 0, 5, '恢复记录')
            self.user.AppendMenuW(menu, 0x800, 0, None)
            self.user.AppendMenuW(menu, 0, 3, '退出')
            point = W.POINT()
            self.user.GetCursorPos(C.byref(point))
            self.user.SetForegroundWindow(hwnd)
            command = self.user.TrackPopupMenu(menu, 0x0100 | 0x0002, point.x, point.y, 0, hwnd, None)
            self.user.PostMessageW(hwnd, 0, 0, 0)
            event = {1: 'visibility', 2: 'toggle', 3: 'close', 4: 'clickthrough', 5: 'records'}.get(command)
            if event:
                self.events.put(event)
        finally:
            self.user.DestroyMenu(menu)

    def update(self, enabled, hidden, clickthrough=False, summary=''):
        self.hidden = hidden
        if (self.enabled, self.clickthrough, self.summary) != (enabled, clickthrough, summary):
            self.enabled = enabled
            self.clickthrough = clickthrough
            self.summary = summary
            if self.ready:
                tip = ('Codex 额度 HUD · 自动接续' + ('已开启' if enabled else '已关闭')
                       + ' · 穿透' + ('开' if clickthrough else '关')
                       + (' · ' + str(summary) if summary else ''))
                # WCHAR is UTF-16: supplementary characters consume two units.
                self.icon_data.tip = tip.replace('\0', '').encode('utf-16-le')[:254].decode('utf-16-le', errors='ignore')
                if not self.shell.Shell_NotifyIconW(1, C.byref(self.icon_data)):
                    self.ready = False
                    self.events.put('tray_failed')

    def close(self):
        if not self.hwnd:
            if getattr(self, 'atom', None):
                self.user.UnregisterClassW(self.class_name, self.instance)
                self.atom = None
            return
        if hasattr(self, 'icon_data'):
            self.shell.Shell_NotifyIconW(2, C.byref(self.icon_data))
        if self.hotkey:
            self.user.UnregisterHotKey(self.hwnd, self.HOTKEY)
        if self.pass_hotkey:
            self.user.UnregisterHotKey(self.hwnd, self.PASS_HOTKEY)
        self.user.DestroyWindow(self.hwnd)
        self.hwnd = None
        self.user.UnregisterClassW(self.class_name, self.instance)
        self.atom = None
        self.ready = self.hotkey = self.pass_hotkey = False
