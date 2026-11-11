# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH).resolve()
datas = []
for entry in (root / 'scripts' / 'windows_resources.txt').read_text(encoding='utf-8-sig').splitlines():
    entry = entry.strip()
    if not entry or entry.startswith('#'):
        continue
    relative = Path(entry)
    source = root / relative
    if relative.is_absolute() or '..' in relative.parts or not source.is_file():
        raise ValueError('Invalid or missing Windows bundle resource: %s' % entry)
    if relative.parts[0] not in ('cdn', 'data', 'ssl', 'LICENSE'):
        raise ValueError('Resource is outside the public bundle allowlist: %s' % entry)
    datas.append((str(source), str(relative.parent)))
licenses = root / 'build' / 'third-party-licenses'
if not (licenses / 'NOTICE.txt').is_file():
    raise ValueError('Dependency notices are missing; use build.cmd to build.')
datas.append((str(licenses), 'third-party-licenses'))

a = Analysis(
    [str(root / 'windows_launcher.py')],
    pathex=[str(root), str(root / 'protobuf')],
    binaries=[],
    datas=datas,
    hiddenimports=['standalone'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', '_tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='zoffline',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    uac_admin=False,
)
