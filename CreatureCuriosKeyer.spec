# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

rapidocr_datas = collect_data_files('rapidocr_onnxruntime')
rapidocr_hidden = collect_submodules('rapidocr_onnxruntime')

a = Analysis(
    ['creature_curios_keyer.py'],
    pathex=[],
    binaries=[],
    datas=rapidocr_datas,
    hiddenimports=[
        'stable_boss',
        'keyboard._winkeyboard',
        'keyboard._winmouse',
        'keyboard._generic',
        'mouse._winmouse',
        'mouse._generic',
        'mouse._mouse_event',
    ] + rapidocr_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'torch', 'torchvision', 'torchaudio', 'transformers', 'accelerate',
        'scipy', 'numba', 'pandas', 'matplotlib', 'sklearn', 'sympy',
        'networkx', 'tensorflow', 'jax', 'paddle', 'paddleocr', 'tensorboardX',
        'onnxruntime.quantization', 'onnxruntime.transformers',
        'onnxruntime.tools', 'onnxruntime.training',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CreatureCuriosKeyer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version_info.txt',
    icon=['app.ico'],
)
