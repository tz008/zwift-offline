"""Find Windows Zwift installations without searching directory trees."""

import ctypes
import os
from pathlib import Path
import re
import stat

try:
    import winreg
except ImportError:
    winreg = None


REQUIRED_FILES = ('ZwiftLauncher.exe', 'ZwiftApp.exe', 'data/cacert.pem')


class InstallationDiscoveryError(RuntimeError):
    """Windows could not check one or more installation locations."""


def candidate_directory(value):
    """Parse directories, executable commands and registry DisplayIcon values."""
    value = os.path.expandvars(str(value).strip())
    if not value or '\x00' in value or len(value) > 32767:
        return None
    quoted = re.match(r'^"([^"]+)"', value)
    if quoted:
        value = quoted.group(1)
    else:
        executable = re.match(r'(?i)^(.+?\.exe)(?:\s*,\s*-?\d+|\s+.*)?$', value)
        if executable:
            value = executable.group(1)
    path = Path(value)
    if path.suffix.lower() == '.exe':
        path = path.parent
    if not path.is_absolute() or str(path).startswith(('\\\\', '//')):
        return None
    if os.name == 'nt' and ctypes.windll.kernel32.GetDriveTypeW(path.anchor) in (1, 4):
        return None  # Unavailable or mapped network drives can block stat().
    return path


def _registry_candidates(errors):
    if winreg is None:
        return
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    hives = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)

    def open_key(hive, key, view):
        return winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view)

    def query(key, name):
        try:
            value, _ = winreg.QueryValueEx(key, name)
            return value if isinstance(value, str) else ''
        except FileNotFoundError:
            return ''

    for hive in hives:
        for view in views:
            for executable in ('ZwiftLauncher.exe', 'ZwiftApp.exe'):
                name = 'SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\' + executable
                try:
                    with open_key(hive, name, view) as key:
                        yield query(key, '')
                        yield query(key, 'Path')
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    errors.append('注册表 %s: %s' % (name, exc))
            root = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
            try:
                with open_key(hive, root, view) as key:
                    count = min(winreg.QueryInfoKey(key)[0], 4096)
                    names = []
                    for index in range(count):
                        try:
                            names.append(winreg.EnumKey(key, index))
                        except OSError:
                            break  # Uninstaller changed the keys during enumeration.
                    names.sort()
                for name in names:
                    recognized = False
                    try:
                        with open_key(hive, root + '\\' + name, view) as key:
                            display_name = query(key, 'DisplayName').casefold()
                            if display_name == 'zwift' or display_name.startswith('zwift '):
                                recognized = True
                                for field in ('InstallLocation', 'DisplayIcon', 'UninstallString'):
                                    yield query(key, field)
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        if recognized or 'zwift' in name.casefold():
                            errors.append('注册表 %s: %s' % (name, exc))
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append('注册表 %s: %s' % (root, exc))


def _fixed_drives():
    if os.name != 'nt':
        return []
    kernel32 = ctypes.windll.kernel32
    mask = kernel32.GetLogicalDrives()
    return [Path('%s:\\' % chr(65 + index)) for index in range(26)
            if mask & (1 << index) and kernel32.GetDriveTypeW('%s:\\' % chr(65 + index)) == 3]


def _common_candidates():
    for name in ('ProgramFiles(x86)', 'ProgramW6432', 'ProgramFiles'):
        if os.environ.get(name):
            yield Path(os.environ[name]) / 'Zwift'
    if os.environ.get('LOCALAPPDATA'):
        root = Path(os.environ['LOCALAPPDATA'])
        yield root / 'Programs' / 'Zwift'
        yield root / 'Zwift'
    for drive in _fixed_drives():
        for relative in ('Zwift', 'Games/Zwift', 'Program Files (x86)/Zwift', 'Program Files/Zwift'):
            yield drive / relative


def validate_zwift_installation(value):
    """Return an installation containing every file required by this launcher."""
    directory = candidate_directory(value)
    if directory is None:
        return None
    if all(stat.S_ISREG((directory / name).stat().st_mode) for name in REQUIRED_FILES):
        return directory
    return None


def find_zwift_installation(runtime_directory):
    """Return the first valid install, None if absent, or raise on access errors.

    Order: legacy cache, HKCU then HKLM (64 then 32 bit), environment defaults,
    then fixed drives in letter order. Access errors permit later candidates.
    """
    errors = []
    cache = Path(runtime_directory) / 'scripts' / 'zwift_location.txt'

    def candidates():
        try:
            with cache.open(encoding='utf-8-sig') as stream:
                yield stream.readline(32768).strip()
        except UnicodeError:
            try:
                with cache.open(encoding='mbcs') as stream:
                    yield stream.readline(32768).strip()
            except (OSError, UnicodeError) as exc:
                errors.append('%s: %s' % (cache, exc))
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append('%s: %s' % (cache, exc))
        yield from _registry_candidates(errors)
        yield from _common_candidates()

    seen = set()
    for value in candidates():
        directory = candidate_directory(value)
        if directory is None:
            continue
        # Windows paths are case-insensitive, but casefold() also merges
        # distinct Unicode names such as Straße and Strasse.
        identity = os.path.normcase(os.path.normpath(str(directory)))
        if identity in seen:
            continue
        seen.add(identity)
        try:
            # stat preserves PermissionError, which exists()/is_file() may hide.
            validated = validate_zwift_installation(directory)
            if validated is not None:
                return validated
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            errors.append('%s: %s' % (directory, exc))
    if errors:
        raise InstallationDiscoveryError('无法检查 Zwift 安装目录：\n' + '\n'.join(errors[:8]))
    return None
