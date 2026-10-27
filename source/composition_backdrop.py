"""Load the local, source-built rounded Composition bridge on the GUI thread."""
from pathlib import Path


def create_backdrop(hwnd):
    import clr
    from System import IntPtr
    library = Path(__file__).resolve().parent / 'native' / 'RoundedBackdrop.dll'
    if not library.is_file():
        raise RuntimeError('Rounded backdrop bridge is missing')
    clr.AddReference(str(library))
    from CodexQuotaBackdrop import RoundedBackdrop
    return RoundedBackdrop(IntPtr(int(hwnd)))
