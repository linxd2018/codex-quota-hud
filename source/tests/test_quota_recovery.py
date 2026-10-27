import time
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import codex_watchdog as watchdog


class QuotaRecoveryTests(unittest.TestCase):
    def snapshot(self, **changes):
        result = dict(available=False, source="app-server", used=100,
                      secondary_used=37, reset_at=int(time.time()) - 60,
                      secondary_reset_at=int(time.time()) + 604800)
        result.update(changes)
        return result

    def test_elapsed_reset_uses_backup_to_confirm_recovery(self):
        recovered = self.snapshot(available=True, source="ping备用", used=0)
        with patch.object(watchdog, "quota_from_app_server", return_value=self.snapshot()), \
                patch.object(watchdog, "quota_from_ping", return_value=recovered) as ping:
            result = watchdog.read_quota()
        self.assertTrue(result["available"])
        ping.assert_called_once()
        self.assertIn("fallback_reason", result)

    def test_weekly_limit_with_future_reset_keeps_waiting(self):
        with patch.object(watchdog, "quota_from_app_server",
                          return_value=self.snapshot(secondary_used=100)), \
                patch.object(watchdog, "quota_from_ping") as ping:
            self.assertFalse(watchdog.read_quota()["available"])
        ping.assert_not_called()

    def test_normal_available_read_never_pings(self):
        with patch.object(watchdog, "quota_from_app_server",
                          return_value=self.snapshot(available=True, used=6)), \
                patch.object(watchdog, "quota_from_ping") as ping:
            self.assertTrue(watchdog.read_quota()["available"])
        ping.assert_not_called()

    def test_failed_reset_confirmation_does_not_claim_recovery(self):
        with patch.object(watchdog, "quota_from_app_server", return_value=self.snapshot()), \
                patch.object(watchdog, "quota_from_ping", side_effect=OSError("offline")):
            result = watchdog.read_quota()
        self.assertFalse(result["available"])
        self.assertIn("confirmation_error", result)

    def test_worker_logging_does_not_block_on_console_output(self):
        messages = []
        errors = []
        def worker():
            try:
                watchdog.log("background heartbeat")
            except Exception as exc:
                errors.append(str(exc))
        with patch.object(watchdog, "LOG_SINK", messages.append, create=True), \
                patch("builtins.print", side_effect=RuntimeError("console paused")):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(1)
        self.assertFalse(errors)
        self.assertEqual(messages, ["background heartbeat"])

    @unittest.skipUnless(watchdog.os.name == "nt", "Windows process preflight")
    def test_cancel_during_tasklist_prevents_queue_subprocess(self):
        enabled = [True]
        def preflight(*args, **kwargs):
            enabled[0] = False
            return SimpleNamespace(stdout="codex.exe", stderr="", returncode=0)
        with patch.object(watchdog.subprocess, "run", side_effect=preflight) as run:
            ok, detail = watchdog.queue_resume(
                {"thread_id": "thread", "cwd": ""},
                can_submit=lambda: enabled[0])
        self.assertFalse(ok)
        self.assertIn("未投递", detail)
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
