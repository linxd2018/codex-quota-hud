"""Per-user, opt-in GPT desktop launch companion. No task automation is enabled."""
import ctypes as C
from ctypes import wintypes as W
import logging
from logging.handlers import RotatingFileHandler
import ntpath
import os
from pathlib import Path
import subprocess
import sys
import time

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = 'CodexQuotaHUD.FollowGPT'


class StartGate:
    def __init__(self):
        self.previous = set()

    def observe(self, roots):
        roots = set(roots)
        started = bool(roots - self.previous)
        self.previous = roots
        return started


def is_gpt_path(path):
    path = ntpath.normcase(path)
    return (ntpath.basename(path) == 'chatgpt.exe' and
            ('\\windowsapps\\openai.codex_' in path or
             '\\windowsapps\\openai.chatgpt' in path))


def launch_command(monitor=False):
    if getattr(sys, 'frozen', False):
        command = [sys.executable]
    else:
        interpreter = Path(sys.executable).with_name('pythonw.exe')
        command = [str(interpreter if interpreter.exists() else Path(sys.executable)),
                   str(Path(__file__).resolve().with_name('hud_entry.py'))]
    return command + (['--follow-gpt'] if monitor else [])


def startup_command():
    return subprocess.list2cmdline(launch_command(True))


def is_enabled():
    if os.name != 'nt':
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, kind = winreg.QueryValueEx(key, RUN_NAME)
            return kind == winreg.REG_SZ and value == startup_command()
    except FileNotFoundError:
        return False


def _write_registration(enabled):
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def start_monitor():
    return subprocess.Popen(launch_command(True), creationflags=subprocess.CREATE_NO_WINDOW,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def set_enabled(enabled):
    _write_registration(bool(enabled))
    if is_enabled() != bool(enabled):
        raise OSError('跟随启动设置未能保存')
    if enabled:
        start_monitor()


class DesktopProcesses:
    """Inspect only executable paths, session and process creation time; never command text."""
    def __init__(self):
        self.kernel = k = C.WinDLL('kernel32', use_last_error=True)
        class Entry(C.Structure):
            _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('pid', W.DWORD),
                        ('heap', C.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
                        ('parent', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD),
                        ('exe', W.WCHAR * 260)]
        self.Entry = Entry
        k.CreateToolhelp32Snapshot.argtypes, k.CreateToolhelp32Snapshot.restype = [W.DWORD, W.DWORD], W.HANDLE
        for name in ['Process32FirstW', 'Process32NextW']:
            getattr(k, name).argtypes = [W.HANDLE, C.POINTER(Entry)]
            getattr(k, name).restype = W.BOOL
        k.OpenProcess.argtypes, k.OpenProcess.restype = [W.DWORD, W.BOOL, W.DWORD], W.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
        k.QueryFullProcessImageNameW.restype = W.BOOL
        k.GetProcessTimes.argtypes = [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4
        k.GetProcessTimes.restype = W.BOOL
        k.ProcessIdToSessionId.argtypes, k.ProcessIdToSessionId.restype = [W.DWORD, C.POINTER(W.DWORD)], W.BOOL
        k.CloseHandle.argtypes, k.CloseHandle.restype = [W.HANDLE], W.BOOL
        session = W.DWORD()
        if not k.ProcessIdToSessionId(os.getpid(), C.byref(session)):
            raise C.WinError(C.get_last_error())
        self.session = session.value

    def roots(self):
        k = self.kernel
        snapshot = k.CreateToolhelp32Snapshot(2, 0)
        if snapshot == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        found = {}
        try:
            entry = self.Entry()
            entry.size = C.sizeof(entry)
            more = k.Process32FirstW(snapshot, C.byref(entry))
            while more:
                if entry.exe.lower() == 'chatgpt.exe':
                    session = W.DWORD()
                    if k.ProcessIdToSessionId(entry.pid, C.byref(session)) and session.value == self.session:
                        handle = k.OpenProcess(0x1000, False, entry.pid)
                        if handle:
                            try:
                                path, size = C.create_unicode_buffer(32768), W.DWORD(32768)
                                times = [W.FILETIME() for _ in range(4)]
                                if (k.QueryFullProcessImageNameW(handle, 0, path, C.byref(size)) and
                                        is_gpt_path(path.value) and
                                        k.GetProcessTimes(handle, *(C.byref(t) for t in times))):
                                    created = times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
                                    found[entry.pid] = (entry.parent, created)
                            finally:
                                k.CloseHandle(handle)
                more = k.Process32NextW(snapshot, C.byref(entry))
        finally:
            k.CloseHandle(snapshot)
        return {(pid, value[1]) for pid, value in found.items() if value[0] not in found}


def monitor():
    k = C.WinDLL('kernel32', use_last_error=True)
    k.CreateMutexW.argtypes, k.CreateMutexW.restype = [C.c_void_p, W.BOOL, W.LPCWSTR], W.HANDLE
    k.CloseHandle.argtypes, k.CloseHandle.restype = [W.HANDLE], W.BOOL
    handle = k.CreateMutexW(None, False, r'Local\CodexQuotaHUD.FollowGPT')
    error = C.get_last_error()
    if not handle:
        raise C.WinError(error)
    try:
        if error == 183:
            return 0
        folder = Path(os.environ['LOCALAPPDATA']) / 'CodexQuotaWatchdog'
        folder.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger('follow-gpt')
        sink = RotatingFileHandler(folder / 'follow-gpt.log', maxBytes=200000, backupCount=1, encoding='utf-8')
        sink.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(sink)
        logger.setLevel(logging.INFO)
        gate, processes = StartGate(), DesktopProcesses()
        logger.info('GPT follow monitor started')
        try:
            while is_enabled():
                try:
                    if gate.observe(processes.roots()):
                        subprocess.Popen(launch_command(), creationflags=subprocess.CREATE_NO_WINDOW,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        logger.info('GPT desktop started; requested HUD launch')
                except OSError:
                    logger.exception('GPT follow check failed')
                time.sleep(2)
            logger.info('GPT follow monitor disabled')
        finally:
            logger.removeHandler(sink)
            sink.close()
        return 0
    finally:
        k.CloseHandle(handle)
