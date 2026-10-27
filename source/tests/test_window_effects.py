import ctypes as C
import os
import unittest
from unittest.mock import patch, Mock
from ctypes import wintypes as W

from windows_hud_effects import WindowEffects


@unittest.skipUnless(os.name == 'nt', 'Windows owned HWND integration')
class WindowEffectsTests(unittest.TestCase):
    def setUp(self):
        from windows_hud_shell import WindowsHUDShell
        self.shell = WindowsHUDShell()
        self.assertIsNotNone(self.shell.hwnd, self.shell.error)
        self.effects = WindowEffects(self.shell.hwnd)

    def tearDown(self):
        self.effects.close()
        self.shell.close()

    def test_real_style_roundtrip_preserves_other_bits(self):
        original = self.effects._read_style()
        self.assertTrue(self.effects.set_clickthrough(True), self.effects.error)
        self.assertEqual(self.effects._read_style() & 0x80020, 0x80020)
        # Simulate another owner changing an unrelated bit while enabled.
        self.effects._write_style(self.effects._read_style() | 0x80)
        self.assertTrue(self.effects.set_clickthrough(False), self.effects.error)
        self.assertEqual(self.effects._read_style() & 0x80020, original & 0x80020)
        self.assertTrue(self.effects._read_style() & 0x80)

    def test_close_restores_owned_bits_and_is_idempotent(self):
        original = self.effects._read_style()
        self.assertTrue(self.effects.set_clickthrough(True))
        self.effects.close()
        self.effects.close()
        self.assertEqual(self.effects._read_style() & 0x80020, original & 0x80020)
        self.assertFalse(self.effects.set_clickthrough(True))

    def test_invalid_hwnd_never_reports_success(self):
        effects = WindowEffects(0)
        self.assertFalse(effects.set_clickthrough(True))
        self.assertEqual(effects.apply_glass(), 'fallback')
        effects.close()

    def test_failed_style_change_reports_actual_state(self):
        with patch.object(self.effects, '_write_style', side_effect=OSError('denied')):
            self.assertFalse(self.effects.set_clickthrough(True))
        self.assertFalse(self.effects.clickthrough)
        self.assertIn('denied', self.effects.error)

    def test_rejected_glass_uses_fallback(self):
        with patch.object(self.effects, '_accent', side_effect=OSError('unsupported')):
            self.assertEqual(self.effects.apply_glass(), 'fallback')
        self.assertIn('unsupported', self.effects.error)

    def test_glass_reports_only_actual_api_outcome(self):
        result = self.effects.apply_glass()
        self.assertIn(result, ('acrylic', 'fallback'))
        if result == 'fallback':
            self.assertTrue(self.effects.error)

    def test_glass_uses_clipped_backdrop_without_rectangular_accent(self):
        backdrop = Mock()
        with patch.object(self.effects, '_accent') as accent, patch.object(
                self.effects, '_create_backdrop', return_value=backdrop):
            self.assertEqual(self.effects.apply_glass(), 'acrylic')
            backdrop.Sync.assert_called_once()
            self.assertEqual([call.args[0] for call in accent.call_args_list], [0])
            self.assertTrue(self.effects.disable_glass())
            backdrop.Dispose.assert_called_once()
            self.assertIsNone(self.effects._backdrop)

    def test_backdrop_sync_failure_disposes_and_falls_back(self):
        backdrop = Mock()
        backdrop.Sync.side_effect = RuntimeError('composition unavailable')
        with patch.object(self.effects, '_accent'), patch.object(
                self.effects, '_create_backdrop', return_value=backdrop):
            self.assertEqual(self.effects.apply_glass(), 'fallback')
            backdrop.Dispose.assert_called_once()
            self.assertIsNone(self.effects._backdrop)
            self.assertFalse(self.effects._glass)
            self.assertIn('composition unavailable', self.effects.error)

    def test_topmost_roundtrip(self):
        original = bool(self.effects._read_style() & 8)
        self.assertTrue(self.effects.set_topmost(True), self.effects.error)
        self.assertTrue(self.effects._read_style() & 8)
        self.assertTrue(self.effects.set_topmost(False), self.effects.error)
        self.assertFalse(self.effects._read_style() & 8)
        self.effects.close()
        self.assertEqual(bool(self.effects._read_style() & 8), original)

    def test_rounded_native_region_clips_corner_and_preserves_styles(self):
        # Resize only this invisible, owned test HWND.
        self.assertTrue(self.effects.user.SetWindowPos(self.shell.hwnd, None, 0, 0,
                                                       420, 420, 0x16))
        before = self.effects._read_style()
        self.assertTrue(self.effects.round_corners(26), self.effects.error)
        self.assertEqual(self.effects._read_style(), before)
        gdi = C.WinDLL('gdi32', use_last_error=True)
        gdi.CreateRectRgn.argtypes, gdi.CreateRectRgn.restype = [C.c_int] * 4, W.HANDLE
        gdi.PtInRegion.argtypes, gdi.PtInRegion.restype = [W.HANDLE, C.c_int, C.c_int], W.BOOL
        gdi.DeleteObject.argtypes, gdi.DeleteObject.restype = [W.HANDLE], W.BOOL
        self.effects.user.GetWindowRgn.argtypes = [W.HWND, W.HANDLE]
        self.effects.user.GetWindowRgn.restype = C.c_int
        region = gdi.CreateRectRgn(0, 0, 0, 0)
        try:
            self.assertEqual(self.effects.user.GetWindowRgn(self.shell.hwnd, region), 3)
            self.assertFalse(gdi.PtInRegion(region, 0, 0))
            self.assertTrue(gdi.PtInRegion(region, 210, 210))
            self.effects.close()
            self.assertEqual(self.effects.user.GetWindowRgn(self.shell.hwnd, region), 0)
        finally:
            gdi.DeleteObject(region)


if __name__ == '__main__':
    unittest.main()
