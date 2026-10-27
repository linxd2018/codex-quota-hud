import unittest
from unittest.mock import Mock, patch

import follow_gpt as follow


class FollowGPTTests(unittest.TestCase):
    def test_existing_gpt_starts_once_and_manual_hud_exit_does_not_respawn(self):
        gate = follow.StartGate()
        self.assertTrue(gate.observe({(7, 100)}))
        self.assertFalse(gate.observe({(7, 100)}))
        self.assertFalse(gate.observe({(7, 100)}))

    def test_new_main_process_and_pid_reuse_each_trigger(self):
        gate = follow.StartGate()
        self.assertTrue(gate.observe({(7, 100)}))
        self.assertFalse(gate.observe(set()))
        self.assertTrue(gate.observe({(7, 200)}))
        self.assertTrue(gate.observe({(7, 200), (9, 300)}))

    def test_only_verified_openai_desktop_paths_match(self):
        self.assertTrue(follow.is_gpt_path(r'C:\Program Files\WindowsApps\OpenAI.Codex_26_x64__id\app\ChatGPT.exe'))
        self.assertTrue(follow.is_gpt_path(r'C:\Program Files\WindowsApps\OpenAI.ChatGPT-Desktop_1\app\ChatGPT.exe'))
        self.assertFalse(follow.is_gpt_path(r'C:\Temp\ChatGPT.exe'))
        self.assertFalse(follow.is_gpt_path(r'C:\Program Files\WindowsApps\OtherApp\ChatGPT.exe'))

    def test_frozen_command_uses_stable_executable_and_explicit_monitor_flag(self):
        with patch.object(follow.sys, 'frozen', True, create=True), patch.object(follow.sys, 'executable', r'D:\My HUD\CodexQuotaHUD.exe'):
            self.assertEqual(follow.launch_command(True), [r'D:\My HUD\CodexQuotaHUD.exe', '--follow-gpt'])
            self.assertEqual(follow.launch_command(), [r'D:\My HUD\CodexQuotaHUD.exe'])

    def test_registry_command_quotes_paths_with_spaces(self):
        with patch.object(follow, 'launch_command', return_value=[r'D:\My HUD\hud.exe', '--follow-gpt']):
            self.assertEqual(follow.startup_command(), r'"D:\My HUD\hud.exe" --follow-gpt')

    def test_enabling_registers_before_starting_and_verifies(self):
        registry, spawn = Mock(), Mock()
        with patch.object(follow, '_write_registration', registry), patch.object(follow, 'is_enabled', return_value=True), patch.object(follow, 'start_monitor', spawn):
            follow.set_enabled(True)
        registry.assert_called_once_with(True)
        spawn.assert_called_once()

    def test_registration_failure_does_not_report_enabled(self):
        with patch.object(follow, '_write_registration'), patch.object(follow, 'is_enabled', return_value=False), patch.object(follow, 'start_monitor') as spawn:
            with self.assertRaises(OSError):
                follow.set_enabled(True)
            spawn.assert_not_called()

    def test_disabling_does_not_start_monitor(self):
        with patch.object(follow, '_write_registration') as registry, patch.object(follow, 'is_enabled', return_value=False), patch.object(follow, 'start_monitor') as spawn:
            follow.set_enabled(False)
        registry.assert_called_once_with(False)
        spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
