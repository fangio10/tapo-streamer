# -*- mode: python ; coding: utf-8 -*-
import os

# Runtime data the frozen app needs (tapo-streamer.py loads cam.png from its
# base path, and onvif-zeep loads its WSDL files from a "wsdl" folder that
# sits next to the onvif package). The original spec had datas=[], so neither
# was bundled: the icon silently failed to load and PTZ (get_onvif_camera
# swallows the exception) would not work from the built binary.
datas = []

_icon = os.path.join(SPECPATH, 'cam.png')
if os.path.exists(_icon):
    datas.append((_icon, '.'))
else:
    print('WARNING: cam.png not found next to the spec; icon will not be bundled')

try:
    import onvif
    _wsdl = os.path.join(os.path.dirname(os.path.dirname(onvif.__file__)), 'wsdl')
    if os.path.isdir(_wsdl):
        datas.append((_wsdl, 'wsdl'))
    else:
        print('WARNING: onvif wsdl directory not found at', _wsdl, '- PTZ will not work in the built binary')
except ImportError:
    print('WARNING: onvif is not installed in the build environment - PTZ will not work in the built binary')


a = Analysis(
    ['tapo-streamer.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['PIL._tkinter_finder', 'PIL._imagingtk'],
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
    name='tapo-streamer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='tapo-streamer',
)
