# Build the local Qt HUD; never bundle a private DLL over Windows' ICU API.
from pathlib import Path
import sys

root = Path(SPECPATH)
sys.path.insert(0, str(root))
from tools.build_support import filter_system_icu
from PyInstaller.utils.hooks import collect_dynamic_libs

a = Analysis(
    [str(root / 'hud_entry.py')],
    pathex=[str(root)], binaries=collect_dynamic_libs('clr_loader'),
    datas=[(str(root/'native/RoundedBackdrop.dll'),'native')],
    hiddenimports=['clr','pythonnet','clr_loader','_cffi_backend'], hookspath=[],
    hooksconfig={}, runtime_hooks=[],
    excludes=['numpy', 'matplotlib', 'pandas', 'PIL', 'IPython', 'pytest'],
    noarchive=False, optimize=0,
)
a.binaries = filter_system_icu(a.binaries)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name='CodexQuotaHUD',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False,
    argv_emulation=False, target_arch=None, codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               upx_exclude=[], name='CodexQuotaHUD')
