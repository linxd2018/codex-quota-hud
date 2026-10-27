import unittest
import os
from unittest.mock import Mock
from quota_hud import QuotaHUD, DemoService


class VisibilityTests(unittest.TestCase):
    def app(self, ready=True):
        app = QuotaHUD.__new__(QuotaHUD)
        app.root = Mock()
        app.service = DemoService()
        app.service.set_enabled(True)
        app.shell = Mock(ready=ready)
        app.hidden = False
        app.closed = False
        app.pinned = True
        app.settings_window = None
        app._round_window = Mock()
        app._save_position = Mock()
        return app

    def test_hide_preserves_monitor_and_restore(self):
        app = self.app()
        app.minimize()
        self.assertTrue(app.hidden)
        app.root.withdraw.assert_called_once()
        self.assertTrue(app.service.enabled)
        app.show()
        self.assertFalse(app.hidden)
        app.root.deiconify.assert_called_once()
        self.assertTrue(app.service.enabled)

    def test_failed_tray_never_hides_window(self):
        from unittest.mock import patch
        app = self.app(False)
        with patch('quota_hud.messagebox.showwarning'):
            app.minimize()
        app.root.withdraw.assert_not_called()
        self.assertFalse(app.hidden)

    def test_global_toggle_roundtrip(self):
        app = self.app()
        app.toggle_visibility()
        app.toggle_visibility()
        self.assertFalse(app.hidden)
        self.assertTrue(app.service.enabled)

    def test_close_cleans_shell_and_disables_monitor(self):
        app = self.app()
        app.close()
        app.shell.close.assert_called_once()
        self.assertFalse(app.service.enabled)
        app.root.destroy.assert_called_once()


@unittest.skipUnless(os.name == 'nt', 'Windows shell integration')
class NativeShellTests(unittest.TestCase):
    def test_hotkeys_register_and_release_independently(self):
        from windows_hud_shell import WindowsHUDShell
        first = WindowsHUDShell()
        second = None
        try:
            if not first.hotkey or not first.pass_hotkey:
                self.skipTest('An unrelated application already reserves HUD shortcuts')
            self.assertTrue(first.user.UnregisterHotKey(first.hwnd, first.HOTKEY))
            first.hotkey = False
            second = WindowsHUDShell()
            self.assertTrue(second.hotkey)
            self.assertFalse(second.pass_hotkey)
            first.close()  # only its remaining P registration is owned
            third = WindowsHUDShell()
            try:
                self.assertFalse(third.hotkey)  # still owned by second
                self.assertTrue(third.pass_hotkey)
            finally:
                third.close()
        finally:
            first.close()
            if second:
                second.close()

    def test_real_tray_hotkey_conflict_and_cleanup(self):
        from windows_hud_shell import WindowsHUDShell
        first = WindowsHUDShell()
        second = None
        try:
            self.assertTrue(first.ready, first.error)
            was_available = first.hotkey
            pass_available = first.pass_hotkey
            second = WindowsHUDShell()
            self.assertTrue(second.ready, second.error)
            self.assertFalse(second.hotkey)
            self.assertFalse(second.pass_hotkey)
            first.close()
            first.close()  # repeated cleanup is safe
            self.assertIsNone(first.hwnd)
            second.close()
            third = WindowsHUDShell()
            try:
                self.assertTrue(third.ready, third.error)
                self.assertEqual(third.hotkey, was_available)
                self.assertEqual(third.pass_hotkey, pass_available)
                third.update(True, True)
                self.assertTrue(third.enabled)
                self.assertTrue(third.hidden)
                third.update(True, False, True, 'confirmed 3')
                self.assertTrue(third.clickthrough)
                self.assertIn('confirmed 3', third.icon_data.tip)
                third.update(True, False, True, '🪟' * 200)
                self.assertLessEqual(len(third.icon_data.tip.encode('utf-16-le')), 254)
                third._window_proc(third.hwnd, 0x0312, third.PASS_HOTKEY, 0)
                self.assertEqual(third.events.get_nowait(), 'clickthrough')
            finally:
                third.close()
        finally:
            first.close()
            if second:
                second.close()


if __name__ == '__main__':
    unittest.main()
