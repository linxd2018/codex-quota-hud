import unittest
from tools.build_support import filter_system_icu


class BuildDependencyTests(unittest.TestCase):
    def test_windows_icu_is_not_shadowed_by_poppler_dlls(self):
        binaries = [
            ('icuuc.dll', r'C:\runtime\native\poppler\Library\bin\icuuc.dll', 'BINARY'),
            ('icudt78.dll', r'C:\runtime\native\poppler\Library\bin\icudt78.dll', 'BINARY'),
            ('PySide6/Qt6Core.dll', r'C:\venv\PySide6\Qt6Core.dll', 'BINARY'),
            ('python312.dll', r'C:\Python\python312.dll', 'BINARY'),
        ]
        output = filter_system_icu(binaries)
        self.assertEqual([row[0] for row in output], ['PySide6/Qt6Core.dll', 'python312.dll'])

    def test_unrelated_versioned_icu_and_other_dependencies_are_kept(self):
        rows = [('icuuc78.dll', r'C:\venv\icuuc78.dll', 'BINARY'),
                ('Qt6Widgets.dll', r'C:\venv\Qt6Widgets.dll', 'BINARY')]
        self.assertEqual(filter_system_icu(rows), rows)


if __name__ == '__main__':
    unittest.main()
