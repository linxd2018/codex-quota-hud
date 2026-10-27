# Third-party components

This package includes Python, Tcl/Tk, PySide6 Essentials / Qt and Shiboken6, and is built with PyInstaller. The application logic is local; it does not bundle or copy either comparison project's implementation.

- Python: Python Software Foundation license, https://docs.python.org/3/license.html
- Tcl/Tk: BSD-style licenses, https://www.tcl.tk/software/tcltk/license.html
- PySide6 / Shiboken6: Qt for Python open-source licensing (LGPLv3/GPLv3 as applicable), https://doc.qt.io/qtforpython-6/licenses.html ; corresponding sources: https://code.qt.io/pyside/pyside-setup.git/
- Qt: module-specific open-source licenses; corresponding source and notices: https://code.qt.io/cgit/qt/ ; https://doc.qt.io/qt-6/licenses-used-in-qt.html
- PyInstaller bootloader: GPL with the distribution exception, https://pyinstaller.org/en/stable/license.html
- Python.NET: MIT license, https://github.com/pythonnet/pythonnet/blob/master/LICENSE
- clr-loader: MIT license, https://github.com/pythonnet/clr-loader/blob/master/LICENSE

The rounded backdrop bridge is provided as source in `native/RoundedBackdrop.cs` and uses Windows' built-in .NET Framework and Composition APIs. Rebuild it with `tools/build_backdrop.ps1` on Windows 10 or newer.

Qt shared libraries remain separate inside `_internal`, rather than being statically linked into the executable. Installed wheel-provided license notices are included under `licenses/` where supplied by their distributions.

Product comparisons informing this change: https://github.com/songyb111-gachon/codex-auto-resume-windows and https://github.com/nesszer/Win-CodexBar . Those products are independent; no affiliation is implied.
