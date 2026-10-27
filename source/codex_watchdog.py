#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻监控 Codex 额度中断并反复续接原会话。仅依赖 Python 标准库。"""
from __future__ import print_function

import argparse
import hashlib
import datetime
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except (AttributeError, OSError):
    pass


HOME = os.path.expanduser("~")
CODEX_DIR = os.environ.get("CODEX_HOME", os.path.join(HOME, ".codex"))
STATE_DB = os.path.join(os.environ.get("LOCALAPPDATA", os.path.join(HOME, "AppData", "Local")),
                        "CodexQuotaWatchdog", "watchdog.sqlite")
LOCK_FILE = STATE_DB + ".lock"
CONFIG_FILE = os.path.join(os.path.dirname(STATE_DB), "config.json")
CODEX_JS = os.path.join(HOME, "AppData", "Roaming", "npm", "node_modules",
                        "@openai", "codex", "bin", "codex.js")
CODEX_RUNTIME = None
POLL_SECONDS = 60
INITIAL_LOOKBACK_HOURS = 72
QUEUE_TIMEOUT = 240
QUEUE_OBSERVE_SECONDS = 180
SUBPROCESS_OPTIONS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
LOG_SINK = None


def log(message):
    if LOG_SINK is not None:
        LOG_SINK(message)
    # A selected/paused console can block print(). Never put worker progress
    # behind that console lock; the service writes its durable log separately.
    if threading.current_thread() is not threading.main_thread() or sys.stdout is None:
        return
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (stamp, message), flush=True)


def disable_console_quick_edit():
    """防止 Windows 控制台误选文本使整个前台输出停住。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = kernel.GetStdHandle(-10 & 0xffffffff)
        mode = wintypes.DWORD()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        target = (mode.value | 0x0080) & ~0x0040
        if not kernel.SetConsoleMode(handle, target):
            return None
        return {"before": mode.value, "after": target, "quick_edit_disabled": True}
    except (OSError, AttributeError):
        return None


def state_connection():
    parent = os.path.dirname(STATE_DB)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    con = sqlite3.connect(STATE_DB, timeout=15)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS resume_events (
        thread_id TEXT NOT NULL,
        failed_turn_id TEXT NOT NULL,
        title TEXT,
        cwd TEXT,
        state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        last_error TEXT,
        PRIMARY KEY(thread_id, failed_turn_id)
    )""")
    con.commit()
    return con


def resolve_cli_js(path):
    """把 codex.cmd、npm 全局目录或 codex.js 路径解析为 CLI JS 入口。"""
    path = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
    if os.path.isdir(path):
        candidates = [
            os.path.join(path, "node_modules", "@openai", "codex", "bin", "codex.js"),
            os.path.join(path, "@openai", "codex", "bin", "codex.js"),
            os.path.join(path, "bin", "codex.js"),
        ]
    elif os.path.basename(path).lower() == "codex.js":
        candidates = [path]
    elif os.path.basename(path).lower() in ("codex.cmd", "codex.ps1", "codex"):
        npm_root = os.path.dirname(path)
        candidates = [os.path.join(npm_root, "node_modules", "@openai", "codex",
                                   "bin", "codex.js")]
    else:
        return None
    return next((os.path.abspath(candidate) for candidate in candidates
                 if os.path.isfile(candidate)), None)


def prompt_path(prompt, validator):
    while True:
        try:
            value = input(prompt).strip().strip('"')
        except (EOFError, KeyboardInterrupt):
            return None
        if not value:
            return None
        valid = validator(value)
        if valid:
            return valid
        print("路径无效，或该位置没有找到 Codex CLI 文件。请重新输入；留空退出。")


def prepare_codex_runtime(force_prompt=False):
    """自动定位 CLI；默认路径失效时在启动终端询问并保存自定义路径。"""
    global CODEX_RUNTIME
    saved = {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as stream:
            saved = json.load(stream)
    except (OSError, ValueError):
        pass
    if not isinstance(saved, dict):
        saved = {}

    saved_cli = saved.get("codex_cli_path")
    cli_js = resolve_cli_js(saved_cli) if saved_cli and not force_prompt else None
    if not force_prompt and cli_js is None:
        on_path = shutil.which("codex.cmd") or shutil.which("codex")
        cli_js = resolve_cli_js(on_path) if on_path else None
    if not force_prompt and cli_js is None and os.path.isfile(CODEX_JS):
        cli_js = CODEX_JS

    saved_node = saved.get("node_path")
    node = saved_node if saved_node and os.path.isfile(saved_node) else None
    if not force_prompt and node is None:
        node = shutil.which("node.exe") or shutil.which("node")

    if force_prompt or cli_js is None:
        print("无法从默认位置找到 Codex CLI。请提供 codex.cmd 路径或 npm 全局目录。")
        print("Codex App（例如 Codex.exe）路径不能替代 CLI 路径。")
        selected = prompt_path("Codex CLI 路径（留空退出）: ", resolve_cli_js)
        if not selected:
            print("未配置 Codex CLI，已取消启动。")
            return False
        cli_js = selected

    if force_prompt or node is None:
        print("找不到 Node.js；请提供 node.exe 路径。")
        selected_node = prompt_path(
            "Node.js 路径（留空退出）: ",
            lambda value: os.path.abspath(os.path.expandvars(
                os.path.expanduser(value))) if os.path.isfile(os.path.expandvars(
                    os.path.expanduser(value))) else None)
        if not selected_node:
            print("未配置 Node.js，已取消启动。")
            return False
        node = selected_node

    CODEX_RUNTIME = {"node": node, "codex_js": cli_js}
    if force_prompt or not saved or saved.get("codex_cli_path") != cli_js or saved.get("node_path") != node:
        parent = os.path.dirname(CONFIG_FILE)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        with open(CONFIG_FILE, "w", encoding="utf-8") as stream:
            json.dump({"codex_cli_path": cli_js, "node_path": node}, stream,
                      ensure_ascii=False, indent=2)
        print("CLI 路径配置已保存到: %s" % CONFIG_FILE)
    return True


def codex_command(*arguments):
    """直接调用 Node 与 Codex JS 入口，绕过 Windows .cmd/.ps1 包装器。"""
    if not CODEX_RUNTIME:
        raise RuntimeError("Codex CLI 尚未配置")
    return [CODEX_RUNTIME["node"], CODEX_RUNTIME["codex_js"]] + list(arguments)


class SingleInstance(object):
    def __init__(self):
        self.handle = None

    def acquire(self):
        parent = os.path.dirname(LOCK_FILE)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        self.handle = open(LOCK_FILE, "a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            self.handle.close()
            self.handle = None
            return False
        return True

    def release(self):
        if not self.handle:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        self.handle.close()
        self.handle = None


def readonly_connection(filename):
    if not os.path.isfile(filename):
        raise IOError("找不到 Codex 数据库: %s" % filename)
    return sqlite3.connect("file:%s?mode=ro" % filename.replace("\\", "/"), uri=True,
                           timeout=5)


def quota_from_app_server(timeout=20):
    """通过 app-server 的只读 rateLimits/read 读取当前账号额度。"""
    process = subprocess.Popen(codex_command("app-server", "--stdio"), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               universal_newlines=True, bufsize=1,
                               encoding="utf-8", errors="replace", **SUBPROCESS_OPTIONS)
    lines = queue.Queue()

    def reader():
        try:
            for line in process.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def send(payload):
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()

    request_sent = False
    deadline = time.monotonic() + timeout
    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex-quota-watchdog", "version": "1.0"}}})
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=min(1.0, max(0.1, deadline - time.monotonic())))
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if line is None:
                break
            try:
                response = json.loads(line)
            except ValueError:
                continue
            if response.get("id") == 1 and not request_sent:
                if response.get("error"):
                    raise RuntimeError("app-server initialize 失败: %s" % response["error"])
                send({"method": "initialized", "params": {}})
                send({"id": 2, "method": "account/rateLimits/read"})
                request_sent = True
            elif response.get("id") == 2:
                if response.get("error"):
                    raise RuntimeError("额度只读接口失败: %s" % response["error"])
                result = response.get("result") or {}
                buckets = result.get("rateLimitsByLimitId") or {}
                rate = buckets.get("codex") or result.get("rateLimits") or {}
                primary = rate.get("primary") or {}
                secondary = rate.get("secondary") or {}
                usage = {"used": primary.get("usedPercent"),
                         "secondary_used": secondary.get("usedPercent"),
                         "window_minutes": primary.get("windowDurationMins"),
                         "secondary_window_minutes": secondary.get("windowDurationMins"),
                         "reset_at": primary.get("resetsAt"),
                         "secondary_reset_at": secondary.get("resetsAt")}
                reached = rate.get("rateLimitReachedType")
                allowed = result.get("ordinaryUsageAllowed")
                if allowed is False or reached:
                    return dict(usage, available=False, source="app-server",
                                reason=reached or "ordinaryUsageAllowed=false")
                if allowed is True:
                    return dict(usage, available=True, source="app-server", reason="")
                raise RuntimeError("额度响应缺少 ordinaryUsageAllowed，无法安全判断")
        raise RuntimeError("app-server 额度查询超时或连接关闭")
    finally:
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def quota_from_ping(timeout=20):
    """备用探测：只在 app-server 只读查询失败时发送最小 ping 请求。"""
    auth_path = os.path.join(CODEX_DIR, "auth.json")
    with open(auth_path, encoding="utf-8") as stream:
        auth = json.load(stream)
    tokens = auth["tokens"]
    headers = {
        "Authorization": "Bearer " + tokens["access_token"],
        "chatgpt-account-id": tokens["account_id"],
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "OpenAI-Beta": "responses=experimental",
        "originator": "Codex Desktop",
        "User-Agent": "codex_desktop",
    }
    body = {"model": "gpt-6.1-sol", "instructions": "reply ok",
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "ping"}]}],
            "stream": True, "store": False}
    request = urllib.request.Request(
        "https://chatgpt.com/backend-api/codex/responses",
        data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    status, response_headers, response_body = None, {}, ""
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
        status, response_headers = response.status, response.headers
        response.close()
    except urllib.error.HTTPError as exc:
        status, response_headers = exc.code, exc.headers
        try:
            response_body = exc.read(2000).decode("utf-8", "replace")
        finally:
            exc.close()
    lowered = {key.lower(): value for key, value in response_headers.items()}
    if status == 429:
        reset = lowered.get("x-codex-primary-reset-at")
        if not reset and response_body:
            try:
                reset = json.loads(response_body).get("error", {}).get("resets_at")
            except (ValueError, AttributeError):
                pass
        return {"available": False, "source": "ping备用", "used":
                lowered.get("x-codex-primary-used-percent"),
                "secondary_used": lowered.get("x-codex-secondary-used-percent"),
                "reset_at": int(reset) if reset else None, "reason": "HTTP 429"}
    if status is not None and 200 <= status < 300:
        return {"available": True, "source": "ping备用",
                "used": lowered.get("x-codex-primary-used-percent"),
                "secondary_used": lowered.get("x-codex-secondary-used-percent"),
                "reset_at": None, "reason": ""}
    raise RuntimeError("ping 备用额度探测返回 HTTP %s" % status)


def needs_reset_confirmation(quota, now=None):
    """只读接口的耗尽窗口已过重置时刻时，不能无限相信旧的受限快照。"""
    if quota.get("available"):
        return False
    now = time.time() if now is None else now
    expired = False
    for used_key, reset_key in (("used", "reset_at"),
                                ("secondary_used", "secondary_reset_at")):
        try:
            used = float(quota[used_key])
        except (KeyError, TypeError, ValueError):
            continue
        if used < 100:
            continue
        try:
            reset_at = float(quota[reset_key])
        except (KeyError, TypeError, ValueError):
            return False
        if reset_at > now - 5:
            return False
        expired = True
    return expired


def read_quota():
    try:
        result = quota_from_app_server()
    except Exception as primary_error:
        try:
            result = quota_from_ping()
            result["fallback_reason"] = str(primary_error)[:180]
            return result
        except Exception as fallback_error:
            raise RuntimeError("只读接口失败 (%s)；ping 备用也失败 (%s)" %
                               (str(primary_error)[:180], str(fallback_error)[:180]))
    if needs_reset_confirmation(result):
        try:
            confirmed = quota_from_ping()
            confirmed["fallback_reason"] = "只读额度仍受限，但耗尽窗口的重置时间已过；已二次确认"
            return confirmed
        except Exception as exc:
            result["confirmation_error"] = str(exc)[:180]
    return result


def remaining_percent(used):
    if used is None:
        return "未知"
    try:
        return "%.0f%%" % max(0, min(100, 100 - float(used)))
    except (TypeError, ValueError):
        return "未知"


def show_startup_quota(quota):
    log("启动时额度概览（剩余）")
    log("  5 小时窗口: %s（已用 %s%%）" %
        (remaining_percent(quota.get("used")),
         "?" if quota.get("used") is None else quota["used"]))
    log("  7 天窗口:   %s（已用 %s%%）" %
        (remaining_percent(quota.get("secondary_used")),
         "?" if quota.get("secondary_used") is None else quota["secondary_used"]))
    log("  查询方式: %s；当前%s" %
        (quota.get("source"), "额度可用" if quota.get("available") else "额度受限"))
    if not quota.get("available") and quota.get("reset_at"):
        reset_at = datetime.datetime.fromtimestamp(int(quota["reset_at"]))
        log("  预计恢复: %s" % reset_at.strftime("%Y-%m-%d %H:%M:%S"))
    if quota.get("fallback_reason"):
        log("  只读接口不可用，已切换到 ping 备用探测。")


def quota_error(error_json):
    if not error_json:
        return False
    try:
        item = json.loads(error_json)
        info = item.get("codexErrorInfo") or item.get("type") or ""
        message = item.get("message") or ""
        return ("usageLimitExceeded" in info or "usage_limit_reached" in info or
                "usage limit" in message.lower())
    except (ValueError, AttributeError):
        lowered = error_json.lower()
        return "usagelimitexceeded" in lowered or "usage_limit_reached" in lowered


def read_latest_events(lookback_hours, thread_ids=None, directories=None):
    state_path = os.path.join(CODEX_DIR, "state_5.sqlite")
    history_path = os.path.join(CODEX_DIR, "thread_history_1.sqlite")
    state = readonly_connection(state_path)
    history = readonly_connection(history_path)
    try:
        threads = state.execute("""SELECT id,title,cwd,agent_path,archived,source
            FROM threads WHERE COALESCE(archived,0)=0""").fetchall()
        roots = {}
        tids = set(thread_ids or [])
        dirs = [os.path.normcase(os.path.abspath(p)) for p in (directories or [])]
        for tid, title, cwd, agent_path, archived, source in threads:
            if agent_path or "subagent" in (source or "").lower():
                continue
            cwd_value = os.path.normcase(os.path.abspath(cwd)) if cwd else ""
            if tids or dirs:
                if tid not in tids and not any(
                    cwd_value == directory or cwd_value.startswith(directory.rstrip("\\/") + os.sep)
                    for directory in dirs):
                    continue
            roots[tid] = (title or "(无标题)", cwd or "")
        if not roots:
            return []
        latest = history.execute("""SELECT t.thread_id,t.turn_id,t.status,t.error_json,
                t.started_at,t.completed_at
            FROM thread_turns t
            JOIN (SELECT thread_id,MAX(rowid) AS latest_row FROM thread_turns
                  GROUP BY thread_id) x ON t.rowid=x.latest_row""").fetchall()
        cutoff = time.time() - lookback_hours * 3600
        found = []
        for tid, turn_id, status, error_json, started, completed in latest:
            if tid not in roots or status != "failed" or not quota_error(error_json):
                continue
            ended = completed or started or 0
            if ended < cutoff:
                continue
            found.append({"thread_id": tid, "turn_id": turn_id,
                          "title": roots[tid][0], "cwd": roots[tid][1],
                          "completed_at": ended})
        return found
    finally:
        state.close()
        history.close()


def latest_turn(thread_id):
    con = readonly_connection(os.path.join(CODEX_DIR, "thread_history_1.sqlite"))
    try:
        row = con.execute("""SELECT turn_id,status,error_json,completed_at
            FROM thread_turns WHERE thread_id=? ORDER BY rowid DESC LIMIT 1""",
                          (thread_id,)).fetchone()
        if not row:
            return None
        return {"turn_id": row[0], "status": row[1], "error": row[2],
                "completed_at": row[3]}
    finally:
        con.close()


def discover_events(con, args):
    items = read_latest_events(args.lookback_hours, args.thread, args.cwd)
    now = time.time()
    added = []
    for item in items:
        cursor = con.execute("""INSERT OR IGNORE INTO resume_events
            (thread_id,failed_turn_id,title,cwd,state,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)""",
                    (item["thread_id"], item["turn_id"], item["title"],
                     item["cwd"], "pending", now, now))
        if cursor.rowcount:
            added.append(item)
    con.commit()
    return added


def resume_marker(item):
    """Stable per-incident identity; do not expose paths or task contents."""
    identity = json.dumps([item["thread_id"], item.get("turn_id", item.get("failed_turn_id"))],
                          separators=(",", ":"), ensure_ascii=False)
    return "[codex-watchdog-resume:%s]" % hashlib.sha256(identity.encode("utf-8")).hexdigest()


def confirmation_turn(thread_id, failed_turn_id):
    """Only a matching user text attached to a later turn proves delivery."""
    marker = resume_marker({"thread_id": thread_id, "turn_id": failed_turn_id})
    con = readonly_connection(os.path.join(CODEX_DIR, "thread_history_1.sqlite"))
    try:
        rows = con.execute("""SELECT t.turn_id,i.item_json FROM thread_turns t
            JOIN thread_items i ON i.thread_id=t.thread_id AND i.turn_id=t.turn_id
            WHERE t.thread_id=? AND i.item_type='userMessage'
            AND t.rowid>(SELECT rowid FROM thread_turns WHERE thread_id=? AND turn_id=?)
            ORDER BY t.rowid""", (thread_id, thread_id, failed_turn_id))
        for turn_id, raw in rows:
            try:
                item = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(item, dict) or item.get("type") != "userMessage":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            if any(isinstance(part, dict) and part.get("type") == "text"
                   and isinstance(part.get("text"), str) and marker in part["text"]
                   for part in content):
                return turn_id
        return None
    finally:
        con.close()


def resume_message(item):
    parts = ["请继续这个 Codex 会话中用户原先要求的任务。上一轮因额度耗尽中断；"]
    if item.get("cwd"):
        parts.append("工作目录为 %s；" % item["cwd"])
    parts.append("先阅读原会话、检查已完成的工作和现有改动，从断点继续。遵循原任务范围与约束，不要重做已完成内容。若还需要更多步骤，继续执行；若任务已经完成，做必要核验并简要汇报。")
    return "".join(parts) + "\n" + resume_marker(item)


def queue_resume(item, can_submit=None):
    if os.name == "nt":
        codex_running = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq codex.exe"], capture_output=True,
            text=True, timeout=15, errors="replace", **SUBPROCESS_OPTIONS)
        if "codex.exe" not in (codex_running.stdout or "").lower():
            return False, "没有检测到 codex.exe，桌面客户端可能未打开"
    try:
        if can_submit is not None and not can_submit():
            return False, "自动接续已关闭，未投递"
        result = subprocess.run(codex_command("queue", "--thread", item["thread_id"],
                                 "--message", resume_message(item)),
                                capture_output=True, text=True, timeout=QUEUE_TIMEOUT,
                                encoding="utf-8", errors="replace", **SUBPROCESS_OPTIONS)
    except subprocess.TimeoutExpired:
        return None, "queue 等待超时，结果不明确"
    except Exception as exc:
        return False, "queue 调用异常: %s" % str(exc)[:240]
    output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    lowered = output.lower()
    if (result.returncode != 0 or "error:" in lowered or "failed to queue" in lowered or
            "no rollout" in lowered):
        return False, output[:600] or "queue 退出码 %s" % result.returncode
    return True, output[:300] or "命令已接受，等待新 turn"


def queue_resume_guarded(item, can_submit):
    return queue_resume(item, can_submit=can_submit)


def set_event(con, thread_id, turn_id, state, error=None, attempt_delta=0):
    con.execute("""UPDATE resume_events SET state=?,last_error=?,attempts=attempts+?,
        updated_at=? WHERE thread_id=? AND failed_turn_id=?""",
                (state, error, attempt_delta, time.time(), thread_id, turn_id))
    con.commit()


def sync_event_states(con):
    rows = con.execute("""SELECT thread_id,failed_turn_id,state,updated_at
        FROM resume_events WHERE state IN ('sending','queued','uncertain')
        OR state='superseded'""").fetchall()
    now = time.time()
    for thread_id, failed_turn, state, updated_at in rows:
        try:
            confirmed = confirmation_turn(thread_id, failed_turn)
            latest = latest_turn(thread_id)
        except Exception as exc:
            log("核对会话 %s 失败，稍后重试: %s" % (thread_id[:8], str(exc)[:140]))
            continue
        if confirmed:
            set_event(con, thread_id, failed_turn, "confirmed")
        elif state == "superseded":
            # A turn may precede its user item in the history mirror. Recheck
            # evidence only: never place these events back in the send queue.
            continue
        elif latest and latest["turn_id"] != failed_turn:
            set_event(con, thread_id, failed_turn, "superseded")
            log("会话 %s 已观察到新 turn %s" % (thread_id[:8], str(latest["turn_id"])[:8]))
        elif state == "uncertain":
            continue
        elif state == "sending":
            set_event(con, thread_id, failed_turn, "uncertain",
                      "程序曾在发送期间退出；需人工核查，避免重复投递")
            log("会话 %s 发送状态不明确，标记为需人工核查" % thread_id[:8])
        elif now - updated_at >= QUEUE_OBSERVE_SECONDS:
            set_event(con, thread_id, failed_turn, "uncertain",
                      "排队后未观察到新 turn；需人工核查，避免重复投递")
            log("会话 %s 排队后未出现新 turn，标记为需人工核查" % thread_id[:8])


def event_dict(row):
    return {"thread_id": row[0], "turn_id": row[1], "title": row[2], "cwd": row[3],
            "state": row[4], "attempts": row[5], "updated_at": row[6], "error": row[7]}


def matches_scope(thread_id, cwd, thread_ids=None, directories=None):
    tids = set(thread_ids or [])
    dirs = [os.path.normcase(os.path.abspath(path)) for path in (directories or [])]
    if not tids and not dirs:
        return True
    cwd_value = os.path.normcase(os.path.abspath(cwd)) if cwd else ""
    return thread_id in tids or any(
        cwd_value == directory or
        cwd_value.startswith(directory.rstrip("\\/") + os.sep)
        for directory in dirs)


def pending_events(con, args):
    rows = con.execute("""SELECT thread_id,failed_turn_id,title,cwd,state,attempts,
        updated_at,last_error FROM resume_events WHERE state='pending'
        ORDER BY created_at""").fetchall()
    ready = []
    now = time.time()
    for row in rows:
        item = event_dict(row)
        if not matches_scope(row[0], row[3], args.thread, args.cwd):
            continue
        if item["attempts"]:
            delay = min(900, max(30, 30 * (2 ** min(item["attempts"] - 1, 4))))
            if now - item["updated_at"] < delay:
                continue
        ready.append(item)
    return ready


def show_status(con):
    rows = con.execute("""SELECT thread_id,failed_turn_id,title,cwd,state,attempts,
        updated_at,last_error FROM resume_events ORDER BY updated_at DESC LIMIT 30""").fetchall()
    if not rows:
        print("尚无额度中断记录。")
    for row in rows:
        item = event_dict(row)
        when = datetime.datetime.fromtimestamp(item["updated_at"]).strftime("%m-%d %H:%M:%S")
        print("%-10s thread=%s turn=%s 次数=%d 更新=%s  %s" %
              (item["state"], item["thread_id"][:8], item["turn_id"][:8],
               item["attempts"], when, item["title"][:44]))
        if item["error"]:
            print("           %s" % item["error"][:220])


def cmd_retry(args):
    con = state_connection()
    try:
        row = con.execute("""SELECT state FROM resume_events
            WHERE thread_id=? AND failed_turn_id=?""", (args.thread, args.turn)).fetchone()
        if not row:
            print("没有找到这条恢复事件。")
            return 1
        if row[0] != "uncertain":
            print("只有 uncertain 事件能人工重置；当前状态: %s" % row[0])
            return 1
        set_event(con, args.thread, args.turn, "pending", "用户手动确认重试")
        print("已重新排入看门狗。若之前的 queue 实际已接受，可能产生重复消息。")
        return 0
    finally:
        con.close()


def cmd_watch(args):
    from watchdog_runtime import WatchdogService
    console_mode = disable_console_quick_edit()
    service = WatchdogService(core=sys.modules[__name__], poll_interval=args.interval,
                              thread_ids=args.thread, directories=args.cwd,
                              lookback_hours=args.lookback_hours, display_mode=True)
    service.set_enabled(True)
    service.start()
    log("常驻看门狗已启动；每 %d 秒检查一次，按 Ctrl+C 停止。" % args.interval)
    if console_mode:
        log("已关闭控制台快速编辑，避免误选文本暂停监控输出。")
    log("日志和心跳保存在: %s" % os.path.dirname(STATE_DB))
    last_quota_at, next_report = None, 0
    try:
        while True:
            snapshot = service.snapshot()
            updated_at = snapshot.get("quota_updated_at")
            if updated_at and updated_at != last_quota_at:
                show_startup_quota(snapshot["quota"])
                last_quota_at = updated_at
            if time.monotonic() >= next_report:
                log("%s；待续接 %d，已排队 %d，需核查 %d；阶段: %s" % (
                    snapshot.get("status", "正在启动"), snapshot.get("pending_count", 0),
                    snapshot.get("queued_count", 0), snapshot.get("uncertain_count", 0),
                    snapshot.get("phase", "初始化")))
                if snapshot.get("quota_error"):
                    log("额度查询错误: %s" % snapshot["quota_error"])
                next_report = time.monotonic() + args.interval
            if snapshot.get("monitor_state") == "blocked":
                log("另一个看门狗持有监控锁，本次未重复启动。")
                return 2
            if not snapshot.get("service_alive", True):
                log("监控线程已退出，请查看 watchdog.log。")
                return 1
            time.sleep(.2)
    except KeyboardInterrupt:
        log("正在停止自动接续…")
        return 0
    finally:
        service.stop(timeout=3)
        log("已停止后续自动续接；已排入 Codex 的任务不会被取消。")


def make_parser():
    parser = argparse.ArgumentParser(description="Codex 额度持续监控与任务自动续接")
    sub = parser.add_subparsers(dest="command")
    watch = sub.add_parser("watch", help="持续监控并反复续接")
    watch.add_argument("--interval", type=int, default=POLL_SECONDS,
                       help="检查间隔秒数，最小 30，默认 60")
    watch.add_argument("--lookback-hours", type=int, default=INITIAL_LOOKBACK_HOURS,
                       help="首次扫描的失败回溯小时数，默认 72")
    watch.add_argument("--thread", action="append", help="只监控该会话；可重复指定")
    watch.add_argument("--cwd", action="append", help="只监控该目录及其子目录；可重复指定")
    status = sub.add_parser("status", help="查看恢复事件状态")
    sub.add_parser("config", help="重新设置 Codex CLI 与 Node.js 路径")
    retry = sub.add_parser("retry-uncertain", help="人工确认后重试状态不明的事件")
    retry.add_argument("--thread", required=True, help="Codex 会话 ID")
    retry.add_argument("--turn", required=True, help="被额度中断的 turn ID")
    return parser


def main(argv=None):
    args = make_parser().parse_args(argv)
    if args.command == "status":
        con = state_connection()
        try:
            show_status(con)
        finally:
            con.close()
        return 0
    if args.command == "config":
        return 0 if prepare_codex_runtime(force_prompt=True) else 1
    if args.command == "retry-uncertain":
        return cmd_retry(args)
    if args.command in (None, "watch"):
        if args.command is None:
            args.interval = POLL_SECONDS
            args.lookback_hours = INITIAL_LOOKBACK_HOURS
            args.thread = None
            args.cwd = None
        if args.interval < 30:
            print("--interval 最小为 30 秒")
            return 2
        if args.lookback_hours < 1:
            print("--lookback-hours 最小为 1 小时")
            return 2
        if not prepare_codex_runtime():
            return 2
        return cmd_watch(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
