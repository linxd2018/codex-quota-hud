import unittest
from quota_glass import normalized_transparency, surface_alphas


class AppearancePolicyTests(unittest.TestCase):
    def test_invalid_saved_values_have_readable_default(self):
        for value in (None, 'oops', float('nan'), float('inf'), True):
            self.assertEqual(normalized_transparency(value), 55)
        self.assertEqual(normalized_transparency(-10), 0)
        self.assertEqual(normalized_transparency(900), 80)

    def test_more_transparency_reduces_each_background_layer(self):
        solid, normal, clear = [surface_alphas(value) for value in (0, 55, 80)]
        self.assertEqual(solid['base'], 255)
        self.assertTrue(solid['base'] > normal['base'] > clear['base'])
        self.assertTrue(solid['card'] > normal['card'] > clear['card'])
        self.assertLess(normal['base'], 140)
        self.assertLess(normal['card'], 60)
        self.assertGreater(clear['base'], 40)


if __name__ == '__main__':
    unittest.main()
