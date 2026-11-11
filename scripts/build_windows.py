"""Create an isolated Windows build environment and one-file executable."""

import os
from pathlib import Path
import platform
import struct
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def run(*arguments):
    subprocess.run([str(argument) for argument in arguments], cwd=ROOT, check=True)


def main():
    if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64') or struct.calcsize('P') != 8:
        raise RuntimeError('Building requires 64-bit Windows and x64 CPython.')
    if sys.version_info[:2] not in ((3, 11), (3, 12)):
        raise RuntimeError('Building requires x64 CPython 3.11 or 3.12; found %s.' % sys.version.split()[0])
    build_environment = ROOT / '.venv-build'
    python = build_environment / 'Scripts' / 'python.exe'
    if not python.is_file():
        run(sys.executable, '-m', 'venv', build_environment)
    run(python, '-c', "import struct, sys; assert struct.calcsize('P') == 8 and sys.version_info[:2] in ((3,11),(3,12)), 'Remove .venv-build and rebuild with x64 CPython 3.11/3.12'")
    run(python, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', ROOT / 'requirements-windows-build.txt')
    run(python, '-m', 'pip', 'check')
    run(python, ROOT / 'scripts' / 'collect_licenses.py', ROOT / 'build' / 'third-party-licenses')
    run(python, '-m', 'PyInstaller', '--clean', '--noconfirm', ROOT / 'standalone.spec')
    executable = ROOT / 'dist' / 'zoffline.exe'
    if not executable.is_file():
        raise RuntimeError('PyInstaller finished without creating %s.' % executable)
    print('Built: %s (%.1f MiB)' % (executable, executable.stat().st_size / 1024 / 1024), flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print('Windows build failed: %s' % exc, file=sys.stderr)
        sys.exit(1)
