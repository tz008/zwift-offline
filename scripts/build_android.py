"""Build and check the local-solo APK using the checked-in Gradle wrapper."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', type=Path, help='Android SDK directory (or configure android/local.properties)')
    args = parser.parse_args()
    if args.sdk:
        sdk = args.sdk.resolve()
        if not (sdk / 'platforms/android-36/android.jar').is_file():
            parser.error('SDK must include platforms;android-36 and build-tools;35.0.0')
        (ROOT / 'android/local.properties').write_text('sdk.dir=' + sdk.as_posix() + '\n', encoding='utf-8')
    subprocess.run([sys.executable, str(ROOT / 'scripts/prepare_android.py')], cwd=ROOT, check=True)
    env = os.environ.copy()
    env['ZOFFLINE_BUILD_PYTHON'] = sys.executable
    wrapper = ROOT / 'android' / ('gradlew.bat' if os.name == 'nt' else 'gradlew')
    command = [str(wrapper), '--no-daemon', '--console=plain', 'testDebugUnitTest', 'lintDebug', 'assembleDebug']
    if os.name == 'nt':
        command = [os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c'] + command
    subprocess.run(command, cwd=ROOT / 'android', env=env, check=True)
    apk = ROOT / 'android/app/build/outputs/apk/debug/app-debug.apk'
    destination = ROOT / 'dist/zoffline-android-solo.apk'
    destination.parent.mkdir(exist_ok=True)
    shutil.copyfile(apk, destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix('.apk.sha256').write_text(digest + '  ' + destination.name + '\n', encoding='ascii')
    print('APK:', destination)
    print('SHA256:', digest)


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
