import os
import queue
import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from quota_glass import GlassHUD
from quota_hud import DemoService


class FakeShell:
    def __init__(self, ready=True, hotkey=True):
        self.ready, self.hotkey, self.pass_hotkey = ready, hotkey, hotkey
        self.events = queue.SimpleQueue()
        self.update = Mock()
        self.close = Mock()


class FakeEffects:
    def __init__(self, hwnd):
        self.clickthrough = False
        self.error = ''
    def set_clickthrough(self, enabled):
        self.clickthrough = enabled
        return True
    def apply_glass(self): return 'fallback'
    def disable_glass(self): return True
    def round_corners(self, radius=26): return True
    def set_topmost(self, enabled): return True
    def close(self): pass


class GlassUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.service = DemoService()
        self.shell = FakeShell()
        self.hud = GlassHUD(self.service, demo=True, shell=self.shell, effects_factory=FakeEffects)
        self.hud.show()
        self.app.processEvents()

    def tearDown(self):
        self.hud.close()
        self.app.processEvents()

    def test_fresh_start_is_interactive_and_automation_opt_in(self):
        self.assertFalse(self.hud.auto_switch.isChecked())
        self.assertIsNone(self.hud.pass_switch)
        self.assertFalse(self.hud.control.enabled)
        self.assertEqual(self.hud.auto_state.text(), '关')
        self.assertIn('#E45D69', self.hud.auto_state.styleSheet())

    def test_header_controls_share_size_and_center_line(self):
        sizes = {(button.width(), button.height()) for button in self.hud.header_buttons}
        centers = {button.geometry().center().y() for button in self.hud.header_buttons}
        self.assertEqual(sizes, {(30, 30)})
        self.assertEqual(len(centers), 1)

    def test_missing_blur_keeps_background_readable(self):
        self.hud.set_transparency(80)
        image = self.hud.grab().toImage()
        # Bare shell margin: failed native blur must not expose busy wallpaper.
        self.assertGreaterEqual(image.pixelColor(10, 100).alpha(), 235)
        self.assertEqual(self.hud.windowOpacity(), 1.)

    def test_quota_text_fits_at_normal_and_unknown_values(self):
        for value in ('100%', '未知'):
            for widget in self.hud.quota_values:
                widget.setText(value)
            self.app.processEvents()
            for widget in self.hud.quota_values:
                self.assertGreaterEqual(widget.width(), widget.fontMetrics().horizontalAdvance(value))
                self.assertGreaterEqual(widget.height(), widget.fontMetrics().height())

    def test_passthrough_only_exists_inside_settings(self):
        self.assertIsNone(self.hud.pass_switch)
        self.hud.settings()
        self.assertIs(self.hud.pass_switch.window(), self.hud.settings_dialog)
        self.hud.pass_switch.click()
        QTest.qWait(120)
        self.assertTrue(self.hud.control.enabled)
        self.hud.settings_dialog.close()

    def test_demo_cannot_change_follow_startup_registration(self):
        with patch('quota_glass.follow_gpt.is_enabled') as read, patch('quota_glass.follow_gpt.set_enabled') as write:
            self.hud.settings()
            self.assertFalse(self.hud.follow_checkbox.isEnabled())
            self.assertFalse(self.hud.follow_checkbox.isChecked())
            read.assert_not_called()
            write.assert_not_called()
        self.hud.settings_dialog.close()

    def test_slider_updates_background_live_without_fading_text_or_enabling_service(self):
        self.hud.settings()
        original = self.hud.quota_card.styleSheet()
        self.hud.transparency_slider.setValue(75)
        self.assertEqual(self.hud.transparency, 75)
        self.assertNotEqual(self.hud.quota_card.styleSheet(), original)
        self.assertEqual(self.hud.windowOpacity(), 1.)
        self.assertFalse(self.service.enabled)
        self.hud.settings_dialog.close()

    def test_transparency_preference_survives_restart(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            self.hud.demo = False
            self.hud.settings_path = str(Path(folder)/'hud-settings.json')
            self.hud.set_transparency(73)
            self.assertEqual(json.loads(Path(self.hud.settings_path).read_text())['transparency'], 73)
            core = SimpleNamespace(CONFIG_FILE=str(Path(folder)/'config.json'))
            other = GlassHUD(DemoService(), core=core, demo=False, shell=FakeShell(), effects_factory=FakeEffects)
            try:
                self.assertEqual(other.transparency, 73)
            finally:
                other.close()
            self.hud.demo = True

    def test_appearance_done_does_not_require_unmodified_connection_paths(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            resolver = Mock(return_value=None)
            self.hud.demo = False
            self.hud.configured = False
            self.hud.settings_path = str(Path(folder)/'hud-settings.json')
            self.hud.core = SimpleNamespace(CONFIG_FILE=str(Path(folder)/'config.json'),
                CODEX_RUNTIME={}, resolve_cli_js=resolver)
            try:
                self.hud.settings()
                self.hud.transparency_slider.setValue(65)
                self.hud.settings_done.click()
                self.assertFalse(self.hud.settings_dialog.isVisible())
                resolver.assert_not_called()
                self.assertFalse(self.hud.configured)
            finally:
                self.hud.demo = True

    def test_rejected_native_pin_keeps_button_in_actual_state(self):
        self.hud.effects.set_topmost = lambda enabled: False
        self.hud.pin_button.click()
        self.assertTrue(self.hud.pinned)
        self.assertTrue(self.hud.pin_button.isChecked())

    def test_demo_never_reads_personal_preferences(self):
        with patch('quota_glass.read_json', side_effect=AssertionError('personal preference read')):
            other = GlassHUD(DemoService(), demo=True, shell=FakeShell(), effects_factory=FakeEffects)
            other.close()

    def test_restore_recovers_a_window_outside_all_screens(self):
        self.hud.move(-100000, -100000)
        self.hud.preferences.update(x=-100000, y=-100000)
        self.hud.restore()
        bounds = self.hud.frameGeometry()
        self.assertTrue(any(screen.availableGeometry().intersects(bounds) for screen in self.app.screens()))

    def test_switch_color_and_real_service_state_agree(self):
        self.hud.toggle_automation()
        self.assertTrue(self.service.enabled)
        self.assertTrue(self.hud.auto_switch.isChecked())
        self.assertEqual(self.hud.auto_state.text(), '开')
        self.assertIn('#30B66C', self.hud.auto_state.styleSheet())

    def test_show_and_settings_restore_interaction_without_stopping_service(self):
        self.hud.toggle_automation()
        self.hud._set_clickthrough(True)
        self.assertTrue(self.hud.effects.clickthrough)
        self.hud.restore()
        self.assertFalse(self.hud.effects.clickthrough)
        self.hud._set_clickthrough(True)
        self.hud.settings()
        self.assertFalse(self.hud.effects.clickthrough)
        self.assertTrue(self.service.enabled)
        self.hud.settings_dialog.close()

    def test_hide_keeps_automation_and_restore_recovers(self):
        self.hud.toggle_automation()
        self.hud.minimize()
        self.assertTrue(self.hud.hidden)
        self.assertTrue(self.service.enabled)
        self.hud.restore()
        self.assertFalse(self.hud.hidden)
        self.assertFalse(self.hud.control.enabled)

    def test_clickthrough_shortcut_can_recover_without_window_mouse(self):
        self.hud._set_clickthrough(True)
        self.shell.events.put('show')
        self.hud._poll()
        self.assertFalse(self.hud.control.enabled)
        self.assertTrue(self.hud.isVisible())

    def test_restore_cancels_pending_clickthrough_enable(self):
        self.hud.toggle_clickthrough()
        self.hud.restore()
        QTest.qWait(130)
        self.assertFalse(self.hud.control.enabled)

    def test_settings_cancels_pending_clickthrough_enable(self):
        self.hud.toggle_clickthrough()
        self.hud.settings()
        QTest.qWait(130)
        self.assertFalse(self.hud.control.enabled)
        self.hud.settings_dialog.close()

    def test_refresh_changes_quota_without_enabling_automation(self):
        before = self.hud.quota_values[0].text()
        self.hud.refresh()
        self.hud._poll()
        self.assertNotEqual(self.hud.quota_values[0].text(), before)
        self.assertFalse(self.service.enabled)

    def test_records_keep_literal_task_text_and_do_not_send(self):
        self.hud.last['recent_events'] = [dict(title='<script>example</script>', thread_id='demo',
            state='confirmed', updated_at=100, attempts=1, error='已确认本次续接')]
        self.hud.records()
        self.assertEqual(self.hud.records_table.item(0, 0).text(), '<script>example</script>')
        self.assertIn('已确认', self.hud.records_table.item(0, 1).text())
        self.assertFalse(self.service.enabled)
        self.hud.records_dialog.close()


if __name__ == '__main__':
    unittest.main()
