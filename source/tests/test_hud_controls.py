import unittest
from types import SimpleNamespace

from hud_controls import ClickthroughControl, window_title, health_label


class Effects:
    def __init__(self):
        self.enabled = False
        self.fail = False

    def set_clickthrough(self, enabled):
        if self.fail:
            return False
        self.enabled = enabled
        return True


class ClickthroughTests(unittest.TestCase):
    def setUp(self):
        self.effects = Effects()
        self.shell = SimpleNamespace(ready=True, pass_hotkey=False)
        self.control = ClickthroughControl(self.effects, self.shell)

    def test_cannot_enable_without_any_recovery_route(self):
        self.shell.ready = False
        self.assertFalse(self.control.set_enabled(True))
        self.assertFalse(self.effects.enabled)
        self.assertFalse(self.control.enabled)

    def test_hotkey_alone_can_recover_and_show_disables_passthrough(self):
        self.shell.ready, self.shell.pass_hotkey = False, True
        self.assertTrue(self.control.set_enabled(True))
        self.assertTrue(self.effects.enabled)
        self.assertTrue(self.control.restore_interaction())
        self.assertFalse(self.effects.enabled)
        self.assertFalse(self.control.enabled)

    def test_native_failure_never_claims_success(self):
        self.effects.fail = True
        self.assertFalse(self.control.set_enabled(True))
        self.assertFalse(self.control.enabled)
        self.effects.fail = False
        self.control.set_enabled(True)
        self.effects.fail = True
        self.assertFalse(self.control.restore_interaction())
        self.assertTrue(self.control.enabled)

    def test_lost_recovery_routes_release_passthrough(self):
        self.control.set_enabled(True)
        self.shell.ready = False
        self.assertTrue(self.control.ensure_recoverable())
        self.assertFalse(self.effects.enabled)

    def test_native_partial_failure_tracks_actual_transparent_state(self):
        self.effects.clickthrough = True
        self.effects.fail = True
        self.assertFalse(self.control.set_enabled(True))
        self.assertTrue(self.control.enabled)

    def test_service_health_is_distinct_from_automation_switch(self):
        self.assertIn('已停止', health_label({'service_alive': False, 'enabled': True}))
        self.assertIn('等待额度', health_label({'service_alive': True, 'enabled': True, 'monitor_state': 'waiting'}))
        self.assertIn('已关闭', health_label({'service_alive': True, 'enabled': False}))

    def test_actual_window_duration_labels(self):
        self.assertEqual(window_title({'window_minutes': 300}, 'primary'), '5 小时窗口')
        self.assertEqual(window_title({'window_minutes': 15}, 'primary'), '15 分钟窗口')
        self.assertEqual(window_title({'secondary_window_minutes': 10080}, 'secondary'), '7 天窗口')
        self.assertEqual(window_title({}, 'secondary'), '周额度窗口')


if __name__ == '__main__':
    unittest.main()
