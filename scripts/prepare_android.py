"""Stage an explicit public runtime allowlist; never package local player data."""

import hashlib
import json
from pathlib import Path
import shutil
import zipfile


ROOT = Path(__file__).resolve().parent.parent
PYTHON_FILES = (
    'android_runtime.py', 'runtime_paths.py', 'embedded_wsgi.py',
    'standalone.py', 'zwift_offline.py', 'peloton.py', 'solo_blob.py',
    'blob_gestures.py', 'tokens.py', 'online_sync.py', 'intervals_workouts.py',
    'trainingpeaks_workouts.py', 'workout_state.py', 'workouts_manifest.py',
    'planned_workouts.py', 'free_strava.py',
)


def prepare(root=ROOT):
    root = Path(root).resolve()
    android = root / 'android'
    tunnel_library = android / 'app/libs/hev-socks5-tunnel-2.19.3.aar'
    if hashlib.sha256(tunnel_library.read_bytes()).hexdigest() != (
            'ab6c581e6157680cfe5687ee7bbdcf95405c006c2db3701879ec44b9e5f83836'):
        raise ValueError('hev-socks5-tunnel AAR checksum mismatch')
    source = android / 'app/src/main/python'
    assets = android / 'app/src/main/assets/runtime'
    source.resolve().relative_to(android.resolve())
    # This is generated output only; source code stays in the repository root.
    if source.exists():
        shutil.rmtree(source)
    source.mkdir(parents=True)
    for name in PYTHON_FILES:
        shutil.copyfile(root / name, source / name)
    for proto in sorted((root / 'protobuf').glob('*_pb2.py')):
        shutil.copyfile(proto, source / proto.name)

    entries = []
    for line in (android / 'resources.txt').read_text(encoding='utf-8').splitlines():
        name = line.strip()
        if not name or name.startswith('#'):
            continue
        path = root / name
        if (Path(name).is_absolute() or '..' in Path(name).parts or
                name.split('/')[0] not in ('cdn', 'data', 'ssl', 'LICENSE') or
                not path.is_file()):
            raise ValueError('Invalid public Android resource: ' + name)
        entries.append((name, path.read_bytes()))
    for name in ('NOTICE.txt', 'hev-socks5-tunnel-LICENSE.txt',
                 'CPython-LICENSE.txt', 'Chaquopy-LICENSE.txt', 'OpenSSL-LICENSE.txt'):
        entries.append(('third-party/' + name, (android / 'third-party' / name).read_bytes()))
    assets.mkdir(parents=True, exist_ok=True)
    archive = assets / 'runtime.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for name, data in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 12, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, data)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (assets / 'runtime.sha256').write_text(digest, encoding='ascii')
    print('Android runtime: %d Python files, %d public resources (%s)' %
          (len(list(source.glob('*.py'))), len(entries), digest[:12]))
    return digest


if __name__ == '__main__':
    prepare()
