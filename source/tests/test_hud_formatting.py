import unittest
from unittest.mock import patch
import quota_hud as hud


class QuotaFormattingTests(unittest.TestCase):
    def test_missing_or_nonfinite_quota_never_becomes_full(self):
        for value in (None, 'bad', float('nan'), float('inf')):
            self.assertIsNone(hud.remaining(value))
            self.assertEqual(hud.percent(hud.remaining(value)), '未知')
        self.assertEqual(hud.remaining(100), 0)
        self.assertEqual(hud.remaining(27.5), 72.5)
        self.assertEqual(hud.remaining(110), 0)

    def test_countdown_preserves_days_and_expired_window(self):
        self.assertEqual(hud.countdown(90062, now=0), '1天 01:01:02')
        self.assertEqual(hud.countdown(10, now=11), '00:00:00')
        self.assertEqual(hud.countdown(None, now=0), '时间未知')
        self.assertEqual(hud.countdown(float('nan'), now=0), '时间未知')

    def test_failed_refresh_distinguishes_retained_and_absent_data(self):
        self.assertEqual(hud.freshness({'quota_error': 'offline', 'quota_updated_at': 100}, now=101), '更新失败 · 数据已过期')
        self.assertEqual(hud.freshness({'quota_error': 'offline'}, now=101), '更新失败 · 暂无数据')
        self.assertIn('已过期', hud.freshness({'quota_updated_at': 100}, now=251))
        self.assertIn('较慢', hud.freshness({'refreshing': True, 'phase_started_at': 100}, now=131))

    def test_runtime_discovery_does_not_prompt_when_config_stale(self):
        from types import SimpleNamespace
        core = SimpleNamespace(CONFIG_FILE='absent-config', CODEX_JS='missing', CODEX_RUNTIME=None,
                               resolve_cli_js=lambda path: 'resolved.js' if path == 'cli-on-path' else None)
        with patch.object(hud.shutil, 'which', side_effect=lambda name: {'codex.cmd': 'cli-on-path', 'node.exe': 'node-on-path'}.get(name)):
            self.assertEqual(hud.discover_runtime(core), {'node': 'node-on-path', 'codex_js': 'resolved.js'})

    def test_toggle_is_opt_in_and_demo_refresh_has_no_account(self):
        service = hud.DemoService()
        self.assertFalse(service.snapshot()['enabled'])
        app = hud.QuotaHUD.__new__(hud.QuotaHUD)
        app.configured, app.service = True, service
        app.toggle()
        self.assertTrue(service.snapshot()['enabled'])
        app.toggle()
        self.assertFalse(service.snapshot()['enabled'])
        previous = service.snapshot()['quota']['used']
        app.refresh()
        self.assertNotEqual(service.snapshot()['quota']['used'], previous)

    def test_space_only_toggles_from_background_not_focused_controls(self):
        from types import SimpleNamespace
        service = hud.DemoService()
        app = hud.QuotaHUD.__new__(hud.QuotaHUD)
        app.configured, app.service = True, service
        app.root, app.canvas = object(), object()
        # Button class handlers already act before the toplevel binding.
        # A focused power Button must retain its one toggle, and other
        # controls must never gain an unrelated automation side effect.
        focused_power = object()
        app.toggle()
        self.assertIsNone(app._space_shortcut(SimpleNamespace(widget=focused_power)))
        self.assertTrue(service.snapshot()['enabled'])
        app.toggle()
        for control in (object(), object(), object()):
            app._space_shortcut(SimpleNamespace(widget=control))
            self.assertFalse(service.snapshot()['enabled'])
        self.assertEqual(app._space_shortcut(SimpleNamespace(widget=app.canvas)), 'break')
        self.assertTrue(service.snapshot()['enabled'])
        app._space_shortcut(SimpleNamespace(widget=app.root))
        self.assertFalse(service.snapshot()['enabled'])

    def test_negative_monitor_origins_and_work_area_clamping(self):
        monitors = [(-1920, 0, 0, 1040), (0, 0, 1920, 1040), (0, -1080, 1920, 0)]
        self.assertEqual(hud.clamp_position(-1500, 200, 440, 228, monitors), (-1500, 200))
        self.assertEqual(hud.clamp_position(100, -900, 440, 228, monitors), (100, -900))
        self.assertEqual(hud.clamp_position(-2000, 1000, 440, 228, monitors), (-1920, 812))

    def test_monitor_gap_is_never_a_valid_restored_position(self):
        monitors = [(-1920, 0, 0, 1040), (600, 0, 2520, 1040)]
        self.assertEqual(hud.clamp_position(100, 200, 440, 228, monitors), (600, 200))
        self.assertEqual(hud.clamp_position(-100, 200, 440, 228, monitors), (-440, 200))

    def test_demo_close_does_not_replace_real_position_preference(self):
        app = hud.QuotaHUD.__new__(hud.QuotaHUD)
        app.demo = True
        # No filesystem or root exists: early return must be independent of both.
        app._save_position()


if __name__ == '__main__': unittest.main()
