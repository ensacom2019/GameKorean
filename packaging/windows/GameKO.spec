# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


project_root = Path(SPECPATH).resolve().parents[1]
assets = project_root / 'src' / 'gameko' / 'assets'
datas = [
    (str(assets / 'NotoSansCJKkr-Regular.otf'), 'gameko/assets'),
    (str(assets / 'NotoSansKR-OFL.txt'), 'gameko/assets'),
    (str(assets / 'gameko_notosanscjkkr_sdf_u6000_3_23f1'), 'gameko/assets'),
    (str(assets / 'AINFORGE.png'), 'gameko/assets'),
]
datas += collect_data_files('UnityPy')


a = Analysis(
    [str(project_root / 'packaging' / 'windows' / 'launcher.py')],
    pathex=[str(project_root / 'src')],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='GameKO',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(project_root / 'packaging' / 'windows' / 'windows_version_info.txt'),
    icon=[str(assets / 'AINFORGE.ico')],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='GameKO',
)
