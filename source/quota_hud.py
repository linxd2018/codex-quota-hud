"""Native, read-only by default Codex quota HUD (Python standard library)."""
import argparse
import datetime as dt
import json
import math
import os
import shutil
import sys
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox

BG = '#172633'
PANEL = '#213746'
TEXT = '#EAF2F5'
MUTED = '#8CA0AC'
TEAL = '#54C7C0'
BLUE = '#8BA8F4'
WARN = '#EDBB72'
RED = '#F05B70'
RED_OFF = '#673442'
GREEN = '#30B66C'


def remaining(used):
    try:
        value = float(used)
        return max(0., min(100., 100. - value)) if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def percent(value):
    return '未知' if value is None else ('%g%%' % round(value, 1))


def switch_image(root, enabled):
    """Small code-drawn switch; no font-dependent switch glyphs or image files."""
    image = tk.PhotoImage(master=root, width=44, height=24)
    background = GREEN if enabled else RED
    knob_x = 32 if enabled else 12
    for y in range(24):
        colors = []
        for x in range(44):
            track_x = max(12, min(x, 32))
            track = (x-track_x)**2 + (y-12)**2 <= 100
            knob = (x-knob_x)**2 + (y-12)**2 <= 64
            colors.append('white' if knob else ('#208852' if enabled else '#B33D4C') if track else background)
        image.put('{' + ' '.join(colors) + '}', to=(0, y))
    return image


def countdown(reset, now=None):
    try:
        seconds = max(0, math.ceil(float(reset) - (time.time() if now is None else now)))
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        return ('%d天 ' % days if days else '') + '%02d:%02d:%02d' % (hours, minutes, seconds)
    except (ValueError, TypeError, OverflowError):
        return '时间未知'


def reset_label(reset, now=None):
    try:
        stamp = dt.datetime.fromtimestamp(float(reset)).strftime('%m/%d %H:%M')
        return '窗口重置 ' + stamp + '  ·  ' + countdown(reset, now)
    except (ValueError, TypeError, OSError, OverflowError):
        return '窗口重置时间未知'


def freshness(snapshot, now=None):
    now = time.time() if now is None else now
    updated = snapshot.get('quota_updated_at')
    if snapshot.get('quota_error'):
        return '更新失败 · 数据已过期' if updated else '更新失败 · 暂无数据'
    if snapshot.get('refreshing'):
        started = snapshot.get('phase_started_at')
        return '查询较慢 · 仍在等待' if started and now - started > 30 else '正在更新…'
    if not updated:
        return '等待额度数据'
    if now - updated > 150:
        return '数据已过期 · 等待更新'
    return '更新于 ' + dt.datetime.fromtimestamp(updated).strftime('%H:%M:%S')


def read_json(path):
    try:
        with open(path, encoding='utf-8') as stream:
            result = json.load(stream)
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def discover_runtime(core):
    """Never invokes core's interactive prepare_codex_runtime."""
    saved = read_json(core.CONFIG_FILE)
    candidates = [saved.get('codex_cli_path'), shutil.which('codex.cmd'),
                  shutil.which('codex'), core.CODEX_JS]
    cli = next((resolved for path in candidates if isinstance(path, str)
                for resolved in [core.resolve_cli_js(path)] if resolved), None)
    node = saved.get('node_path')
    if not isinstance(node, str) or not os.path.isfile(node):
        node = shutil.which('node.exe') or shutil.which('node')
    core.CODEX_RUNTIME = {'node': node, 'codex_js': cli} if cli and node else None
    return core.CODEX_RUNTIME


def clamp_position(x, y, width, height, areas):
    """Choose nearest monitor work area, preserving absolute negative origins."""
    choices = []
    for left, top, right, bottom in areas:
        nx = max(left, min(x, max(left, right - width)))
        ny = max(top, min(y, max(top, bottom - height)))
        choices.append(((nx-x)**2 + (ny-y)**2, nx, ny))
    return min(choices)[1:] if choices else (x, y)


def monitor_work_areas():
    import ctypes
    from ctypes import wintypes
    class MonitorInfo(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT),
                    ('work', wintypes.RECT), ('flags', wintypes.DWORD)]
    user = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE,
                                      wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    user.GetMonitorInfoW.restype = wintypes.BOOL
    user.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM]
    user.EnumDisplayMonitors.restype = wintypes.BOOL
    areas = []
    def visit(handle, dc, rect, data):
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if user.GetMonitorInfoW(handle, ctypes.byref(info)):
            r = info.work
            areas.append((r.left, r.top, r.right, r.bottom))
        return True
    callback = callback_type(visit)
    user.EnumDisplayMonitors(None, None, callback, 0)
    return areas


class DemoService:
    """Explicit offline sample; has no account or task integration."""
    def __init__(self):
        self.enabled = False
        self.index = 0
        self.request_refresh()
    def start(self): pass
    def stop(self, timeout=2): self.enabled = False
    def set_enabled(self, value): self.enabled = bool(value)
    def request_refresh(self):
        self.index += 1
        self.updated = time.time()
    def snapshot(self):
        return dict(quota=dict(used=28 if self.index % 2 else 41,
                    secondary_used=46, window_minutes=300, secondary_window_minutes=10080,
                    reset_at=self.updated + 7350,
                    secondary_reset_at=self.updated + 215000),
                    quota_updated_at=self.updated, quota_error=None, refreshing=False,
                    enabled=self.enabled, status='离线演示 · 不连接账户',
                    pending_count=0, queued_count=0, uncertain_count=0)


class QuotaHUD:
    def __init__(self, root, service, core=None, demo=False, configured=True):
        self.root, self.service, self.core = root, service, core
        self.demo, self.configured = demo, configured
        self.started = False
        self.closed = False
        self.pinned = True
        self.last = {}
        self.targets = [None, None]
        self.animations = [None, None]
        self.shown = [None, None]
        self.settings_window = None
        self.hidden = False
        self.shell = None
        directory = os.path.dirname(core.CONFIG_FILE) if core else os.path.join(
            os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'CodexQuotaWatchdog')
        self.settings_path = os.path.join(directory, 'hud-settings.json')
        root.title('Codex Quota HUD' + (' — DEMO' if demo else ''))
        root.overrideredirect(True)
        root.attributes('-topmost', True)
        # Keep canvas geometry and font metrics in the same logical coordinate space.
        # Windows DPI virtualization scales the complete HUD consistently.
        root.tk.call('tk', 'scaling', 96 / 72)
        root.configure(bg=BG)
        self.canvas = tk.Canvas(root, width=440, height=266, bg=BG, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self._build()
        settings = read_json(self.settings_path)
        x, y = settings.get('x', root.winfo_screenwidth() - 480), settings.get('y', 80)
        try: x, y = int(x), int(y)
        except (TypeError, ValueError): x, y = 80, 80
        self._position(x, y)
        root.update_idletasks()
        self._round_window()
        root.after(100, self._round_window)
        root.bind('<Control-r>', lambda event: self.refresh())
        root.bind('<Control-R>', lambda event: self.refresh())
        root.bind('<space>', self._space_shortcut)
        root.bind('<Escape>', lambda event: self.close())
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.report_callback_exception = self._callback_error
        from windows_hud_shell import WindowsHUDShell
        self.shell = WindowsHUDShell()
        root.after(150, self._shell_events)
        if not self.shell.ready or not self.shell.hotkey:
            root.after(500, self._shell_warning)
        if configured:
            self._start()
        else:
            root.after(150, self.settings)
        self._poll()

    def _text(self, x, y, text='', size=10, color=TEXT, anchor='w', font='Microsoft YaHei UI', weight='normal'):
        return self.canvas.create_text(x, y, text=text, fill=color, anchor=anchor, font=(font, size, weight))

    def _button(self, x, text, action, hint):
        button = tk.Button(self.root, text=text, command=action, bg=BG, fg=MUTED,
                           activebackground=PANEL, activeforeground=TEXT, bd=0,
                           font=('Segoe UI', 12), cursor='hand2', takefocus=True)
        self.canvas.create_window(x, 23, window=button, width=28, height=28)
        button.bind('<Enter>', lambda e: self.canvas.itemconfigure(self.hint, text=hint))
        button.bind('<Leave>', lambda e: self.canvas.itemconfigure(self.hint, text=self.connection_text))
        return button

    def _build(self):
        c = self.canvas
        self.connection_text = '等待额度数据'
        self._text(20, 23, 'CODEX QUOTA', 11, weight='bold', font='Segoe UI')
        if self.demo: self._text(157, 23, 'DEMO', 8, WARN, font='Segoe UI', weight='bold')
        self.hint = self._text(20, 48, '等待额度数据', 8, MUTED)
        self._button(288, '↻', self.refresh, '立即刷新 · Ctrl+R')
        self._button(319, '⚙', self.settings, 'CLI 与 Node 路径设置')
        self.pin_button = self._button(350, '◇', self.pin, '置顶开关 · 当前置顶')
        self._button(381, '—', self.minimize, '最小化到托盘 · 继续监控')
        self._button(412, '×', self.close, '关闭 HUD 与自动接续')
        self.value_ids, self.reset_ids, self.bars = [], [], []
        for i, (name, y, color, height) in enumerate([('5 小时', 78, TEAL, 9), ('7 天', 140, BLUE, 6)]):
            self._text(20, y, name, 10, MUTED)
            self.value_ids.append(self._text(420, y, '未知', 21 if i == 0 else 17, color, 'e', 'Consolas', 'bold'))
            top = y + 19
            c.create_rectangle(20, top, 420, top + height, fill=PANEL, outline='')
            self.bars.append(c.create_rectangle(20, top, 20, top + height, fill=color, outline=''))
            for tick in (100, 200, 300):
                c.create_line(20 + tick, top, 20 + tick, top + height, fill=BG)
            self.reset_ids.append(self._text(20, top + height + 14, '窗口重置时间未知', 8, MUTED))
        c.create_line(20, 196, 420, 196, fill=PANEL)
        self.switch_images = [switch_image(self.root, False), switch_image(self.root, True)]
        self.power = tk.Button(self.root, text=' 自动接续  关', image=self.switch_images[0], compound='left', bg=RED_OFF, fg=TEXT, bd=0,
                               activebackground=RED, activeforeground='white', command=self.toggle,
                               font=('Microsoft YaHei UI', 11, 'bold'), cursor='hand2')
        c.create_window(118, 219, window=self.power, width=196, height=34)
        self._text(420, 218, 'Ctrl+Alt+H 隐藏 / 显示', 8, MUTED, 'e')
        self.status_id = self._text(20, 250, '自动接续已关闭', 9)
        self.count_id = self._text(420, 250, '待续 0 · 排队 0', 8, MUTED, 'e')
        c.bind('<ButtonPress-1>', self._drag_start)
        c.bind('<B1-Motion>', self._drag)
        c.bind('<ButtonRelease-1>', lambda e: self._save_position())

    def _space_shortcut(self, event):
        # Toplevel bindtags also receive descendant Button key events. Let the
        # focused control perform its own action without toggling automation.
        if event.widget is self.root or event.widget is self.canvas:
            self.toggle()
            return 'break'
        return None

    def _position(self, x, y):
        areas = [(0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())]
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            areas = monitor_work_areas() or areas
            x, y = clamp_position(x, y, 440, 266, areas)
            self.root.geometry('440x266')
            self.root.update_idletasks()
            user = ctypes.windll.user32
            user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user.GetAncestor.restype = wintypes.HWND
            user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user.SetWindowPos.restype = wintypes.BOOL
            hwnd = user.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
            user.SetWindowPos(hwnd, None, int(x), int(y), 440, 266, 0x0004 | 0x0010)
        else:
            x, y = clamp_position(x, y, 440, 266, areas)
            self.root.geometry('440x266%+d%+d' % (x, y))

    def _round_window(self):
        if os.name == 'nt':
            try:
                import ctypes
                from ctypes import wintypes
                user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
                user.GetParent.argtypes = [wintypes.HWND]
                user.GetParent.restype = wintypes.HWND
                user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
                user.GetAncestor.restype = wintypes.HWND
                gdi.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
                gdi.CreateRoundRectRgn.restype = wintypes.HANDLE
                user.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
                user.SetWindowRgn.restype = ctypes.c_int
                gdi.DeleteObject.argtypes = [wintypes.HANDLE]
                gdi.DeleteObject.restype = wintypes.BOOL
                hwnd = user.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
                get_style = user.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.GetWindowLongW
                set_style = user.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.SetWindowLongW
                get_style.argtypes = [wintypes.HWND, ctypes.c_int]
                get_style.restype = ctypes.c_ssize_t
                set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
                set_style.restype = ctypes.c_ssize_t
                style = get_style(hwnd, -20)
                set_style(hwnd, -20, (style | 0x00040000) & ~0x00000080)
                user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                              ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
                user.SetWindowPos.restype = wintypes.BOOL
                user.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)
                region = gdi.CreateRoundRectRgn(0, 0, self.root.winfo_width()+1,
                                                 self.root.winfo_height()+1, 22, 22)
                if region and not user.SetWindowRgn(hwnd, region, True):
                    gdi.DeleteObject(region)
            except Exception:
                log_error(traceback.format_exc())

    def _drag_start(self, event):
        self.drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())
        self.root.focus_set()

    def _drag(self, event):
        ox, oy, x, y = self.drag_origin
        self._position(x + event.x_root - ox, y + event.y_root - oy)

    def _save_position(self):
        if self.demo:
            return
        try:
            os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
            with open(self.settings_path, 'w', encoding='utf-8') as stream:
                json.dump(dict(x=self.root.winfo_x(), y=self.root.winfo_y()), stream)
        except OSError: pass

    def _shell_warning(self):
        if self.closed:
            return
        problems = []
        if not self.shell.ready:
            problems.append('托盘图标创建失败，窗口将保持可见。')
        if not self.shell.hotkey:
            problems.append('Ctrl+Alt+H 注册失败，可能已被其他程序占用；请使用托盘恢复窗口。')
        if problems:
            messagebox.showwarning('HUD 桌面功能提示', '\n'.join(problems), parent=self.root)

    def minimize(self):
        if self.closed:
            return
        if not self.shell or not self.shell.ready:
            messagebox.showwarning('无法最小化', '托盘不可用，窗口保持可见。', parent=self.root)
            return
        self._save_position()
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.destroy()
        self.root.withdraw()
        self.hidden = True
        self.shell.update(self.service.snapshot().get('enabled', False), True)

    def show(self):
        if self.closed:
            return
        self.root.deiconify()
        self.root.attributes('-topmost', self.pinned)
        self.root.lift()
        self.root.focus_force()
        self.hidden = False
        self._round_window()

    def toggle_visibility(self):
        if self.hidden:
            self.show()
        else:
            self.minimize()

    def _shell_events(self):
        if self.closed:
            return
        import queue
        while True:
            try:
                event = self.shell.events.get_nowait()
            except queue.Empty:
                break
            if event == 'visibility': self.toggle_visibility()
            elif event == 'show': self.show()
            elif event == 'toggle': self.toggle()
            elif event == 'close':
                self.close()
                return
            elif event in ('tray_failed', 'shell_error'):
                self.show()
                log_error('Windows HUD shell: ' + (self.shell.error or event))
                self._shell_warning()
        self.root.after(100, self._shell_events)

    def _start(self):
        if not self.started:
            self.service.start()
            self.started = True

    def refresh(self):
        if self.configured: self.service.request_refresh()
        else: self.settings()

    def toggle(self):
        if self.configured:
            self.service.set_enabled(not self.service.snapshot().get('enabled', False))
        else: self.settings()

    def pin(self):
        self.pinned = not self.pinned
        self.root.attributes('-topmost', self.pinned)
        self.pin_button.configure(fg=TEAL if self.pinned else MUTED)

    def _poll(self):
        if self.closed: return
        snapshot = self.service.snapshot() if self.started else {}
        self.last = snapshot
        now = time.time()
        self.connection_text = ('离线演示 · ' if self.demo else '') + (freshness(snapshot, now) if self.configured else '请配置 Codex CLI 与 Node.js')
        self.canvas.itemconfigure(self.hint, text=self.connection_text, fill=WARN if snapshot.get('quota_error') else MUTED)
        quota = snapshot.get('quota') or {}
        for i, (key, reset) in enumerate([('used', 'reset_at'), ('secondary_used', 'secondary_reset_at')]):
            value = remaining(quota.get(key))
            self.canvas.itemconfigure(self.value_ids[i], text=percent(value))
            self.canvas.itemconfigure(self.reset_ids[i], text=reset_label(quota.get(reset), now))
            if value != self.targets[i]:
                self.animations[i] = (time.monotonic(), self.shown[i] if self.shown[i] is not None else value, value)
                self.targets[i] = value
            self._animate(i)
        enabled = snapshot.get('enabled', False)
        self.power.configure(text=' 自动接续  开' if enabled else ' 自动接续  关', image=self.switch_images[int(bool(enabled))],
                             bg=GREEN if enabled else RED, fg='white')
        if self.shell:
            self.shell.update(enabled, self.hidden)
        status = snapshot.get('status', '自动接续已关闭')
        if snapshot.get('service_alive') is False and self.started:
            status = '后台服务已停止 · 请重启'
        stage_start = snapshot.get('phase_started_at')
        if stage_start and now - stage_start > 60 and snapshot.get('phase') not in (None, 'idle', 'waiting'):
            status = '阶段耗时较长 · ' + str(snapshot.get('phase'))
        self.canvas.itemconfigure(self.status_id, text=status[:17])
        counts = '待续 %s · 排队 %s' % (snapshot.get('pending_count', 0), snapshot.get('queued_count', 0))
        if snapshot.get('uncertain_count'): counts += ' · 待核 %s' % snapshot['uncertain_count']
        self.canvas.itemconfigure(self.count_id, text=counts)
        self.root.after(200, self._poll)

    def _animate(self, index):
        if self.closed: return
        animation = self.animations[index]
        if not animation: return
        start, old, target = animation
        fraction = min(1., (time.monotonic() - start) / .2)
        value = None if target is None else target if old is None else old + (target - old) * (1 - (1-fraction)**3)
        self.shown[index] = value
        coords = self.canvas.coords(self.bars[index])
        coords[2] = 20 + 4 * (value or 0)
        self.canvas.coords(self.bars[index], *coords)
        self.canvas.itemconfigure(self.bars[index], fill=WARN if value is not None and value < 15 else (TEAL if index == 0 else BLUE))
        if fraction < 1: self.root.after(16, lambda: self._animate(index) if self.animations[index] == animation else None)
        else: self.animations[index] = None

    def settings(self):
        if self.demo:
            messagebox.showinfo('离线演示', '演示模式不读取配置、不连接账户。', parent=self.root)
            return
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.lift(); return
        dialog = self.settings_window = tk.Toplevel(self.root)
        dialog.title('Codex HUD 设置')
        dialog.configure(bg=BG)
        dialog.resizable(False, False)
        dialog.attributes('-topmost', True)
        values = read_json(self.core.CONFIG_FILE)
        runtime = self.core.CODEX_RUNTIME or {}
        entries = []
        for row, (label, key, fallback) in enumerate([('Codex CLI（codex.cmd / codex.js）', 'codex_cli_path', runtime.get('codex_js', '')), ('Node.js（node.exe）', 'node_path', runtime.get('node', ''))]):
            tk.Label(dialog, text=label, bg=BG, fg=TEXT).grid(row=row*2, column=0, columnspan=2, sticky='w', padx=18, pady=(14, 4))
            entry = tk.Entry(dialog, width=58)
            entry.insert(0, values.get(key) or fallback)
            entry.grid(row=row*2+1, column=0, padx=(18, 5))
            def browse(target=entry):
                chosen = filedialog.askopenfilename(parent=dialog, title='选择可执行文件或 CLI 入口')
                if chosen: target.delete(0, 'end'); target.insert(0, chosen)
            tk.Button(dialog, text='浏览…', command=browse).grid(row=row*2+1, column=1, padx=(0, 18))
            entries.append(entry)
        error = tk.Label(dialog, text='仅保存路径；请先在 Codex CLI 完成登录。', bg=BG, fg=MUTED)
        error.grid(row=4, column=0, columnspan=2, padx=18, pady=12)
        def save():
            cli = self.core.resolve_cli_js(entries[0].get())
            node = os.path.expandvars(os.path.expanduser(entries[1].get().strip().strip('"')))
            if not cli or not os.path.isfile(node) or os.path.basename(node).lower() not in ('node.exe', 'node'):
                error.configure(text='路径无效：请选择 Codex CLI 与 node.exe。', fg=WARN); return
            try:
                saved = read_json(self.core.CONFIG_FILE)
                saved.update(codex_cli_path=cli, node_path=os.path.abspath(node))
                os.makedirs(os.path.dirname(self.core.CONFIG_FILE), exist_ok=True)
                with open(self.core.CONFIG_FILE, 'w', encoding='utf-8') as stream:
                    json.dump(saved, stream, ensure_ascii=False, indent=2)
                self.core.CODEX_RUNTIME = {'node': os.path.abspath(node), 'codex_js': cli}
                self.configured = True
                self._start()
                self.service.request_refresh()
                dialog.destroy()
            except OSError as exc: error.configure(text='保存失败：' + str(exc), fg=WARN)
        tk.Button(dialog, text='保存并刷新', command=save).grid(row=5, column=0, columnspan=2, pady=(0, 18))
        dialog.bind('<Escape>', lambda e: dialog.destroy())
        dialog.after(50, dialog.focus_force)

    def _callback_error(self, exc, value, tb):
        log_error(''.join(traceback.format_exception(exc, value, tb)))
        self.canvas.itemconfigure(self.hint, text='界面错误 · 详情见 hud-error.log', fill=WARN)

    def close(self):
        if self.closed: return
        self.closed = True
        self._save_position()
        self.service.set_enabled(False)
        if self.shell:
            self.shell.close()
        # stop may join a network worker; keep shutdown out of the UI thread.
        import threading
        threading.Thread(target=self.service.stop, kwargs={'timeout': 2}, daemon=True).start()
        self.root.destroy()


def log_error(detail):
    directory = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'CodexQuotaWatchdog')
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, 'hud-error.log')
    if os.path.exists(path) and os.path.getsize(path) > 1024*1024:
        os.replace(path, path + '.previous')
    with open(path, 'a', encoding='utf-8') as stream:
        stream.write('\n' + dt.datetime.now().isoformat() + '\n' + detail + '\n')


def main():
    # pythonw has no console streams; reused core logging must remain safe.
    if sys.stdout is None: sys.stdout = open(os.devnull, 'w', encoding='utf-8')
    if sys.stderr is None: sys.stderr = open(os.devnull, 'w', encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true', help='explicit offline sample, no account/network/task access')
    parser.add_argument('--auto-close', type=float, default=0)
    args = parser.parse_args()
    try:
        if os.name == 'nt':
            import ctypes
            try: ctypes.windll.shcore.SetProcessDpiAwareness(0)
            except (AttributeError, OSError): pass
        if args.demo:
            service, core, configured = DemoService(), None, True
        else:
            import codex_watchdog as core
            from watchdog_runtime import WatchdogService
            configured = bool(discover_runtime(core))
            service = WatchdogService(core=core)
        root = tk.Tk()
        hud = QuotaHUD(root, service, core=core, demo=args.demo, configured=configured)
        if args.auto_close > 0: root.after(int(args.auto_close*1000), hud.close)
        root.mainloop()
    except Exception:
        detail = traceback.format_exc()
        log_error(detail)
        if os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, 'HUD 启动失败。详情见 LocalAppData\\CodexQuotaWatchdog\\hud-error.log', 'Codex Quota HUD', 0x10)
        else: sys.stderr.write(detail)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
