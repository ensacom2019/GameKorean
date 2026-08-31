# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

datas = [('src/gameko/assets/NotoSansCJKkr-Regular.otf', 'gameko/assets'), ('src/gameko/assets/NotoSansKR-OFL.txt', 'gameko/assets'), ('src/gameko/assets/gameko_notosanscjkkr_sdf_u6000_3_23f1', 'gameko/assets'), ('src/gameko/assets/AINFORGE.png', 'gameko/assets')]
datas += collect_data_files('UnityPy')


a = Analysis(
    ['launcher.py'],
    pathex=['src'],
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
    version='windows_version_info.txt',
    icon=['src/gameko/assets/AINFORGE.ico'],
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
