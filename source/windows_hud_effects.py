"""Windows effects for an application-owned HWND, using only the standard library.

The caller owns painting a translucent surface (Qt WA_TranslucentBackground).
No global alpha is applied, so text and controls retain their own opacity.
"""
import ctypes as C
from ctypes import wintypes as W
import os


class WindowEffects:
    STYLE_INDEX = -20
    CLICK_MASK = 0x00080000 | 0x00000020  # WS_EX_LAYERED | WS_EX_TRANSPARENT

    def __init__(self, hwnd):
        self.hwnd = int(hwnd or 0)
        self.error = ''
        self.clickthrough = False
        self._original_bits = None
        self._closed = False
        self._glass = False
        self._backdrop = None
        self._original_topmost = None
        self._rounded = False
        self._rounded_geometry = None
        self.user = None
        if os.name != 'nt':
            self.error = 'Windows effects require Windows'
            return
        self.user = C.WinDLL('user32', use_last_error=True)
        self.user.IsWindow.argtypes = [W.HWND]
        self.user.IsWindow.restype = W.BOOL
        get_name = 'GetWindowLongPtrW' if C.sizeof(C.c_void_p) == 8 else 'GetWindowLongW'
        set_name = 'SetWindowLongPtrW' if C.sizeof(C.c_void_p) == 8 else 'SetWindowLongW'
        self._get = getattr(self.user, get_name)
        self._get.argtypes, self._get.restype = [W.HWND, C.c_int], C.c_ssize_t
        self._set = getattr(self.user, set_name)
        self._set.argtypes, self._set.restype = [W.HWND, C.c_int, C.c_ssize_t], C.c_ssize_t
        self.user.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int,
                                          C.c_int, C.c_int, W.UINT]
        self.user.SetWindowPos.restype = W.BOOL
        self.user.GetClientRect.argtypes, self.user.GetClientRect.restype = [W.HWND, C.POINTER(W.RECT)], W.BOOL
        self.user.SetWindowRgn.argtypes, self.user.SetWindowRgn.restype = [W.HWND, W.HANDLE, W.BOOL], C.c_int
        self.user.GetWindowRect.argtypes, self.user.GetWindowRect.restype = [W.HWND, C.POINTER(W.RECT)], W.BOOL
        self.user.IsWindowVisible.argtypes, self.user.IsWindowVisible.restype = [W.HWND], W.BOOL
        self.user.IsIconic.argtypes, self.user.IsIconic.restype = [W.HWND], W.BOOL

    def round_corners(self, radius=26):
        """Clip this frameless, previously unregioned HWND; radius is Qt logical px.

        Region ownership transfers to Windows only after SetWindowRgn succeeds.
        Client coordinates match window coordinates for this frameless HUD.
        Call again after a resize or DPI change.
        """
        if self._closed:
            self.error = 'HUD effects already closed'
            return False
        region = None
        gdi = None
        try:
            self._valid()
            rect = W.RECT()
            if not self.user.GetClientRect(self.hwnd, C.byref(rect)):
                raise C.WinError(C.get_last_error())
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width <= 0 or height <= 0 or not 0 < float(radius) < 10000:
                raise ValueError('Rounded HUD requires positive size and radius')
            dpi_fn = getattr(self.user, 'GetDpiForWindow', None)
            dpi = 96
            if dpi_fn is not None:
                dpi_fn.argtypes, dpi_fn.restype = [W.HWND], W.UINT
                dpi = dpi_fn(self.hwnd)
                if not dpi:
                    raise OSError('HUD window DPI unavailable')
            geometry = (width, height, dpi, float(radius))
            if self._rounded and self._rounded_geometry == geometry:
                return True
            diameter = max(2, min(width, height, round(float(radius) * dpi / 96 * 2)))
            gdi = C.WinDLL('gdi32', use_last_error=True)
            gdi.CreateRoundRectRgn.argtypes = [C.c_int] * 6
            gdi.CreateRoundRectRgn.restype = W.HANDLE
            gdi.DeleteObject.argtypes, gdi.DeleteObject.restype = [W.HANDLE], W.BOOL
            region = gdi.CreateRoundRectRgn(0, 0, width + 1, height + 1, diameter, diameter)
            if not region:
                raise OSError('Could not allocate rounded HUD region')
            if not self.user.SetWindowRgn(self.hwnd, region, True):
                raise OSError('Windows rejected rounded HUD region')
            region = None  # owned by Windows now; never DeleteObject this handle
            self._rounded = True
            self._rounded_geometry = geometry
            self.error = ''
            return True
        except (OSError, ValueError, OverflowError) as exc:
            self.error = str(exc)
            return False
        finally:
            if region and gdi is not None:
                gdi.DeleteObject(region)

    def _valid(self):
        if self.user is None or not self.hwnd or not self.user.IsWindow(self.hwnd):
            raise OSError('Invalid or destroyed HUD window')

    def _read_style(self):
        self._valid()
        C.set_last_error(0)
        value = self._get(self.hwnd, self.STYLE_INDEX)
        error = C.get_last_error()
        if value == 0 and error:
            raise C.WinError(error)
        return value

    def _write_style(self, value):
        self._valid()
        C.set_last_error(0)
        previous = self._set(self.hwnd, self.STYLE_INDEX, value)
        error = C.get_last_error()
        if previous == 0 and error:
            raise C.WinError(error)
        # FRAMECHANGED | NOMOVE | NOSIZE | NOZORDER | NOACTIVATE
        if not self.user.SetWindowPos(self.hwnd, None, 0, 0, 0, 0, 0x37):
            raise C.WinError(C.get_last_error())
        if self._read_style() != value:
            raise OSError('HUD window style verification failed')

    def set_clickthrough(self, enabled):
        """Return True only when requested style is verified on the HWND."""
        if self._closed:
            self.error = 'HUD effects already closed'
            return False
        before = None
        try:
            before = self._read_style()
            if self._original_bits is None:
                self._original_bits = before & self.CLICK_MASK
            desired = ((before | self.CLICK_MASK) if enabled else
                       ((before & ~self.CLICK_MASK) | self._original_bits))
            if desired != before:
                self._write_style(desired)
            self.clickthrough = bool(self._read_style() & 0x20)
            if enabled and not self.clickthrough:
                raise OSError('HUD click-through verification failed')
            self.error = ''
            return True
        except (OSError, ValueError) as exc:
            self.error = str(exc)
            # A failed frame notification may occur after the style changed.
            # Roll back owned bits only; keep any unrelated concurrent changes.
            if before is not None:
                try:
                    current = self._read_style()
                    self._write_style((current & ~self.CLICK_MASK) | (before & self.CLICK_MASK))
                except OSError:
                    pass
            try:
                self.clickthrough = bool(self._read_style() & 0x20)
            except OSError:
                self.clickthrough = False
            return False

    def _accent(self, state):
        self._valid()
        class Accent(C.Structure):
            _fields_ = [('state', C.c_int), ('flags', C.c_int),
                        ('color', W.DWORD), ('animation', C.c_int)]
        class Composition(C.Structure):
            _fields_ = [('attribute', C.c_int), ('data', C.c_void_p),
                        ('size', C.c_size_t)]
        fn = getattr(self.user, 'SetWindowCompositionAttribute', None)
        if fn is None:
            raise OSError('Windows acrylic API unavailable')
        fn.argtypes, fn.restype = [W.HWND, C.POINTER(Composition)], W.BOOL
        # A light native tint: the HUD paints the user's chosen background alpha.
        # The former 85% tint hid almost all of the desktop below the Qt surface.
        accent = Accent(state, 0, 0x20FBF7F5, 0)
        data = Composition(19, C.cast(C.pointer(accent), C.c_void_p), C.sizeof(accent))
        C.set_last_error(0)
        if not fn(self.hwnd, C.byref(data)):
            error = C.get_last_error()
            raise OSError('Windows acrylic request rejected' + (f' ({error})' if error else ''))

    def set_topmost(self, enabled):
        """Pin without recreating the Qt HWND or losing its native effects."""
        if self._closed:
            self.error = 'HUD effects already closed'
            return False
        try:
            before = bool(self._read_style() & 8)
            if self._original_topmost is None:
                self._original_topmost = before
            # HWND_TOPMOST / HWND_NOTOPMOST; NOMOVE | NOSIZE | NOACTIVATE
            if not self.user.SetWindowPos(self.hwnd, -1 if enabled else -2,
                                          0, 0, 0, 0, 0x13):
                raise C.WinError(C.get_last_error())
            if bool(self._read_style() & 8) != bool(enabled):
                raise OSError('HUD topmost verification failed')
            self.error = ''
            self.sync_backdrop()
            return True
        except OSError as exc:
            self.error = str(exc)
            return False

    def _create_backdrop(self):
        from composition_backdrop import create_backdrop
        return create_backdrop(self.hwnd)

    def sync_backdrop(self, visible=None):
        if self._backdrop is None:
            return not self._glass
        try:
            self._valid()
            rect = W.RECT()
            if not self.user.GetWindowRect(self.hwnd, C.byref(rect)):
                raise C.WinError(C.get_last_error())
            showing = bool(self.user.IsWindowVisible(self.hwnd) and not self.user.IsIconic(self.hwnd))
            if visible is not None:
                showing = showing and bool(visible)
            # Hide promptly when Windows cloaks the HUD on another desktop.
            try:
                dwm = C.WinDLL('dwmapi')
                dwm.DwmGetWindowAttribute.argtypes = [W.HWND,W.DWORD,C.c_void_p,W.DWORD]
                dwm.DwmGetWindowAttribute.restype = C.c_long
                cloaked = W.DWORD()
                if dwm.DwmGetWindowAttribute(self.hwnd,14,C.byref(cloaked),C.sizeof(cloaked)) == 0:
                    showing = showing and not bool(cloaked.value)
            except OSError:
                pass
            dpi_fn = getattr(self.user,'GetDpiForWindow',None)
            if dpi_fn:
                dpi_fn.argtypes,dpi_fn.restype = [W.HWND],W.UINT
            scale = (dpi_fn(self.hwnd) or 96)/96 if dpi_fn else 1.
            self._backdrop.Sync(rect.left,rect.top,max(1,rect.right-rect.left),max(1,rect.bottom-rect.top),
                                27.*scale,bool(self._read_style() & 8),showing)
            return True
        except Exception as exc:
            detail = 'Rounded backdrop sync failed: ' + str(exc)
            self.disable_glass()
            self.error = detail
            return False

    def apply_glass(self):
        if self._closed:
            self.error = 'HUD effects already closed'
            return 'fallback'
        try:
            # Accent4 draws an independently rectangular surface on Windows 10.
            # The new bridge clips its Composition visual, not just the HWND.
            self._accent(0)
            if self._backdrop is None:
                self._backdrop = self._create_backdrop()
            self._glass = True
            if not self.sync_backdrop():
                return 'fallback'
            self.error = ''
            return 'acrylic'
        except Exception as exc:
            self.disable_glass()
            self.error = str(exc)
            return 'fallback'

    def disable_glass(self):
        backdrop, self._backdrop = self._backdrop, None
        self._glass = False
        try:
            if backdrop is not None:
                backdrop.Dispose()
            self._accent(0)
            self.error = ''
            return True
        except Exception as exc:
            self.error = str(exc)
            return False

    def close(self):
        if self._closed:
            return
        if self._original_bits is not None:
            self.set_clickthrough(False)
        if self._original_topmost is not None:
            self.set_topmost(self._original_topmost)
        self.disable_glass()
        if self._rounded:
            try:
                self._valid()
                if not self.user.SetWindowRgn(self.hwnd, None, True):
                    raise OSError('Could not clear rounded HUD region')
                self._rounded = False
                self._rounded_geometry = None
            except OSError as exc:
                self.error = str(exc)
        self._closed = True
