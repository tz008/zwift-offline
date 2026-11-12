"""Temporary Windows Zwift client configuration with a crash-recovery journal.

Only the Windows EXE calls this module. The source server keeps its existing
manual and remote-client configuration workflows.
"""

import base64
from collections import Counter
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import uuid

import windows_network


DOMAINS = (b'us-or-rly101.zwift.com', b'secure.zwift.com',
           b'cdn.zwift.com', b'launcher.zwift.com')
MUTEX_NAME = r'Global\zoffline-temporary-client-configuration-v1'
_CERT_PATTERN = re.compile(rb'-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----')


class ClientConfigurationError(RuntimeError):
    """Temporary configuration or its restoration could not complete."""


def _windows_directory():
    buffer = ctypes.create_unicode_buffer(32768)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    result = kernel.GetWindowsDirectoryW(buffer, len(buffer))
    if not result or result >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(buffer.value)


def _program_data_directory():
    buffer = ctypes.create_unicode_buffer(32768)
    shell = ctypes.windll.shell32
    shell.SHGetFolderPathW.argtypes = [wintypes.HWND, ctypes.c_int,
                                      wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR]
    result = shell.SHGetFolderPathW(None, 0x23, None, 0, buffer)
    if result:
        raise ClientConfigurationError('无法定位 Windows ProgramData 目录（代码 %s）。' % result)
    return Path(buffer.value)


def configuration_directory():
    """Keep the recovery journal outside the bundle and user progress files."""
    root = _program_data_directory() / 'zoffline' / 'client-config'
    root.mkdir(parents=True, exist_ok=True)
    # The journal holds the original hosts and cacert bytes. Limit it to
    # Administrators and SYSTEM even if ProgramData's inherited ACL is broad.
    icacls = _windows_directory() / 'System32' / 'icacls.exe'
    result = subprocess.run([str(icacls), str(root), '/inheritance:r',
                             '/grant:r', '*S-1-5-32-544:(OI)(CI)F',
                             '*S-1-5-18:(OI)(CI)F'], capture_output=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ClientConfigurationError('无法保护临时配置备份目录：%s' % root)
    return root


def acquire_configuration_mutex(wait_ms=0):
    """The main program and restoration helper serialize all client changes."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    result = kernel.WaitForSingleObject(handle, wait_ms)
    if result not in (0, 0x80):
        kernel.CloseHandle(handle)
        raise ClientConfigurationError('已有 zoffline 实例正在临时配置 Zwift。')
    return handle


def release_configuration_mutex(handle):
    kernel = ctypes.windll.kernel32
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex(handle)
    kernel.CloseHandle(handle)


def _cert_der():
    resource_root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    content = (resource_root / 'ssl' / 'cert-zwift-com.pem').read_bytes()
    match = _CERT_PATTERN.search(content)
    if match is None:
        raise ClientConfigurationError('EXE 内缺少有效的 Zwift 客户端证书。')
    return match.group(0), ssl.PEM_cert_to_DER_cert(match.group(0).decode('ascii'))


def _cert_store(read_only=False):
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    crypt.CertOpenStore.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    crypt.CertOpenStore.restype = ctypes.c_void_p
    flags = 0x20000 | 0x4000 | (0x8000 if read_only else 0)
    handle = crypt.CertOpenStore(ctypes.c_void_p(10), 0, None, flags,
                                  ctypes.cast(ctypes.c_wchar_p('ROOT'), ctypes.c_void_p))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return crypt, handle


def certificate_is_installed(der):
    crypt, store = _cert_store(read_only=True)
    digest = hashlib.sha1(der).digest()
    array = (ctypes.c_ubyte * len(digest)).from_buffer_copy(digest)

    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD),
                    ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

    blob = Blob(len(digest), array)
    crypt.CertFindCertificateInStore.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                  wintypes.DWORD, wintypes.DWORD,
                                                  ctypes.c_void_p, ctypes.c_void_p]
    crypt.CertFindCertificateInStore.restype = ctypes.c_void_p
    crypt.CertFreeCertificateContext.argtypes = [ctypes.c_void_p]
    crypt.CertCloseStore.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    try:
        context = crypt.CertFindCertificateInStore(store, 0x10001, 0, 0x10000,
                                                    ctypes.byref(blob), None)
        if context:
            crypt.CertFreeCertificateContext(context)
        return bool(context)
    finally:
        crypt.CertCloseStore(store, 0)


def _certificate_marker(session_id):
    return 'zoffline temporary ' + session_id


def add_certificate(der, session_id):
    crypt, store = _cert_store()
    encoded = ctypes.create_string_buffer(der)
    crypt.CertCreateCertificateContext.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    crypt.CertCreateCertificateContext.restype = ctypes.c_void_p
    crypt.CertSetCertificateContextProperty.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                        wintypes.DWORD, ctypes.c_void_p]
    crypt.CertSetCertificateContextProperty.restype = wintypes.BOOL
    crypt.CertAddCertificateContextToStore.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                       wintypes.DWORD, ctypes.c_void_p]
    crypt.CertAddCertificateContextToStore.restype = wintypes.BOOL
    crypt.CertFreeCertificateContext.argtypes = [ctypes.c_void_p]
    crypt.CertCloseStore.argtypes = [ctypes.c_void_p, wintypes.DWORD]

    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.c_void_p)]

    context = None
    try:
        context = crypt.CertCreateCertificateContext(0x10001, encoded, len(der))
        if not context:
            raise ctypes.WinError(ctypes.get_last_error())
        label = ctypes.create_unicode_buffer(_certificate_marker(session_id))
        blob = Blob(ctypes.sizeof(label), ctypes.cast(label, ctypes.c_void_p))
        if not crypt.CertSetCertificateContextProperty(context, 11, 0, ctypes.byref(blob)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not crypt.CertAddCertificateContextToStore(store, context, 1, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if context:
            crypt.CertFreeCertificateContext(context)
        crypt.CertCloseStore(store, 0)


def remove_certificate(der, session_id):
    crypt, store = _cert_store()
    digest = hashlib.sha1(der).digest()
    array = (ctypes.c_ubyte * len(digest)).from_buffer_copy(digest)

    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD),
                    ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

    blob = Blob(len(digest), array)
    crypt.CertFindCertificateInStore.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                  wintypes.DWORD, wintypes.DWORD,
                                                  ctypes.c_void_p, ctypes.c_void_p]
    crypt.CertFindCertificateInStore.restype = ctypes.c_void_p
    crypt.CertDeleteCertificateFromStore.argtypes = [ctypes.c_void_p]
    crypt.CertDeleteCertificateFromStore.restype = wintypes.BOOL
    crypt.CertGetCertificateContextProperty.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    crypt.CertGetCertificateContextProperty.restype = wintypes.BOOL
    crypt.CertCloseStore.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    try:
        context = crypt.CertFindCertificateInStore(store, 0x10001, 0, 0x10000,
                                                    ctypes.byref(blob), None)
        if context:
            size = wintypes.DWORD()
            if not crypt.CertGetCertificateContextProperty(context, 11, None, ctypes.byref(size)):
                error = ctypes.get_last_error()
                crypt.CertFreeCertificateContext(context)
                if error & 0xffffffff == 0x80092004:  # CRYPT_E_NOT_FOUND: not ours.
                    return
                raise ctypes.WinError(error)
            label = (ctypes.c_ubyte * size.value)()
            if not crypt.CertGetCertificateContextProperty(context, 11, label, ctypes.byref(size)):
                crypt.CertFreeCertificateContext(context)
                raise ctypes.WinError(ctypes.get_last_error())
            name = bytes(label[:size.value]).decode('utf-16-le').rstrip('\x00')
            if name != _certificate_marker(session_id):
                crypt.CertFreeCertificateContext(context)
                return
            if not crypt.CertDeleteCertificateFromStore(context):
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        crypt.CertCloseStore(store, 0)


def _active_host_domains(line):
    content = line.split(b'#', 1)[0].strip().lower().split()
    return set(content[1:]) & set(DOMAINS) if len(content) > 1 else set()


def _hosts_change(original, session_id, server_ip='127.0.0.1'):
    address = windows_network.validate_server_ip(server_ip, allow_loopback=True).encode('ascii')
    if b'\x00' in original:
        raise ClientConfigurationError('hosts 使用了不支持的编码，未修改文件。')
    bom = b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b''
    lines = original[len(bom):].splitlines(keepends=True)
    good = set()
    for line in lines:
        columns = line.split(b'#', 1)[0].strip().lower().split()
        if len(columns) > 1 and columns[0] == address:
            good.update(set(columns[1:]) & set(DOMAINS))
    conflicting = any(_active_host_domains(line) and
                      line.split(b'#', 1)[0].strip().lower().split()[0] != address
                      for line in lines)
    if good == set(DOMAINS) and not conflicting:
        return original
    prefix = b'# zoffline-temporary-' + session_id.encode('ascii') + b' '
    newline = b'\r\n' if b'\r\n' in original else b'\n'
    result = []
    for line in lines:
        if not _active_host_domains(line):
            result.append(line)
            continue
        result.append(prefix + line)
        # A hosts line may also serve unrelated names. Keep those active while
        # the Zwift names are temporarily redirected.
        columns = line.split(b'#', 1)[0].strip().split()
        unrelated = [name for name in columns[1:] if name.lower() not in DOMAINS]
        if unrelated:
            if not line.endswith((b'\r', b'\n')):
                result.append(newline)
            result.append(columns[0] + b' ' + b' '.join(unrelated) +
                          b' # zoffline-temporary-' + session_id.encode('ascii') + newline)
    modified = bom + b''.join(result)
    if modified and not modified.endswith((b'\r', b'\n')):
        modified += newline
    modified += address + b' ' + b' '.join(DOMAINS)
    modified += b' # zoffline-temporary-' + session_id.encode('ascii') + newline
    return modified


def _cacert_change(original, certificate, der, session_id):
    for match in _CERT_PATTERN.finditer(original):
        try:
            if ssl.PEM_cert_to_DER_cert(match.group(0).decode('ascii')) == der:
                return original
        except (UnicodeError, ValueError):
            continue
    marker = b'# zoffline-temporary-' + session_id.encode('ascii')
    return original + b'\n' + marker + b'\n' + certificate + b'\n'


def _encode(data):
    return base64.b64encode(data).decode('ascii')


def _decode(data):
    return base64.b64decode(data.encode('ascii'), validate=True)


def _save(path, journal):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(journal, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def _write(path, data):
    with path.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _flush_dns():
    executable = _windows_directory() / 'System32' / 'ipconfig.exe'
    result = subprocess.run([str(executable), '/flushdns'], capture_output=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ClientConfigurationError('hosts 已修改，但 Windows DNS 缓存刷新失败。')


def _paths(installation):
    return {'hosts': _windows_directory() / 'System32' / 'drivers' / 'etc' / 'hosts',
            'cacert': Path(installation) / 'data' / 'cacert.pem'}


def apply_configuration(installation, journal_directory, server_ip='127.0.0.1', allow_lan=False):
    """Journal before the first write; return the recoverable session file."""
    server_ip = windows_network.validate_server_ip(server_ip, allow_loopback=not allow_lan)
    session_id = uuid.uuid4().hex
    path = Path(journal_directory) / ('session-' + session_id + '.json')
    paths = _paths(installation) if installation is not None else {}
    certificate, der = _cert_der() if paths else (b'', b'')
    before = {name: target.read_bytes() for name, target in paths.items()}
    after = ({'hosts': _hosts_change(before['hosts'], session_id, server_ip),
              'cacert': _cacert_change(before['cacert'], certificate, der, session_id)} if paths else {})
    was_installed = certificate_is_installed(der) if paths else True
    journal = {'version': 1, 'session_id': session_id,
               'files': {name: {'path': str(paths[name]), 'before': _encode(before[name]),
                                'after': _encode(after[name])} for name in paths},
               'certificate_der': _encode(der), 'certificate_was_installed': was_installed,
               'certificate_add_attempted': False, 'firewall_add_attempted': False}
    _save(path, journal)
    try:
        for name, target in paths.items():
            if after[name] != before[name]:
                _write(target, after[name])
                if name == 'hosts':
                    _flush_dns()
        if not was_installed:
            journal['certificate_add_attempted'] = True
            _save(path, journal)
            try:
                add_certificate(der, session_id)
            except BaseException:
                # CERT_STORE_ADD_NEW did not add a certificate when it fails.
                # A different process may have installed the same certificate
                # after our preflight check; never remove that certificate.
                journal['certificate_add_attempted'] = False
                _save(path, journal)
                raise
        if allow_lan:
            journal['firewall_add_attempted'] = True
            _save(path, journal)
            windows_network.add_firewall_rules(session_id, server_ip)
        return path
    except BaseException:
        restore_configuration(path)
        raise


def _restore_hosts(current, before, after, session_id):
    if before == after:
        return current
    if current == after:
        return before
    if current == before:
        return current
    if after.startswith(current):
        return before  # Interrupted in-place write.
    marker = b'zoffline-temporary-' + session_id.encode('ascii')
    prefix = b'# ' + marker + b' '
    after_body = after[3:] if after.startswith(b'\xef\xbb\xbf') else after
    expected = Counter(line for line in after_body.splitlines(keepends=True) if marker in line)
    bom = b'\xef\xbb\xbf' if current.startswith(b'\xef\xbb\xbf') else b''
    lines = current[len(bom):].splitlines(keepends=True)
    restored = []
    for line in lines:
        if marker not in line:
            restored.append(line)
            continue
        if not expected[line]:
            raise ClientConfigurationError('hosts 中本次添加的内容被修改，无法安全自动还原；备份已保留。')
        expected[line] -= 1
        if line.startswith(prefix):
            restored.append(line[len(prefix):])
    if any(expected.values()):
        raise ClientConfigurationError('hosts 在运行期间被修改，无法安全自动还原；备份已保留。')
    return bom + b''.join(restored)


def _restore_cacert(current, before, after, session_id):
    if before == after:
        return current
    if current == after:
        return before
    if current == before:
        return current
    addition = after[len(before):] if after.startswith(before) else b''
    if not addition:
        raise ClientConfigurationError('证书文件在运行期间被修改，无法安全自动还原；备份已保留。')
    if current.startswith(before) and len(current) < len(after) and after.startswith(current):
        return before  # Interrupted append.
    if current.count(addition) == 1:
        return current.replace(addition, b'', 1)
    if addition not in current and current != before:
        original_certificates = []
        for match in _CERT_PATTERN.finditer(addition):
            try:
                original_certificates.append(ssl.PEM_cert_to_DER_cert(match.group(0).decode('ascii')))
            except (ValueError, UnicodeError):
                pass
        for match in _CERT_PATTERN.finditer(current):
            try:
                if ssl.PEM_cert_to_DER_cert(match.group(0).decode('ascii')) in original_certificates:
                    raise ClientConfigurationError('Zwift 证书文件被改写，仍含本次证书；备份已保留。')
            except (ValueError, UnicodeError):
                continue
        return current  # Zwift updater replaced the file and removed our certificate.
    raise ClientConfigurationError('证书文件在运行期间被修改，无法安全自动还原；备份已保留。')


def restore_configuration(journal_path):
    """Restore only this session's changes, retaining other concurrent edits."""
    journal_path = Path(journal_path)
    if not journal_path.is_file():
        return
    journal = json.loads(journal_path.read_text(encoding='utf-8'))
    if journal.get('version') != 1 or journal_path.name != 'session-' + journal['session_id'] + '.json':
        raise ClientConfigurationError('临时配置恢复记录格式无效。')
    errors = []
    for name in journal['files']:
        try:
            if name not in ('hosts', 'cacert'):
                raise ClientConfigurationError('恢复记录包含未知的文件类型。')
            entry = journal['files'][name]
            target = Path(entry['path'])
            before, after = _decode(entry['before']), _decode(entry['after'])
            current = target.read_bytes()
            if name == 'hosts':
                restored = _restore_hosts(current, before, after, journal['session_id'])
            else:
                restored = _restore_cacert(current, before, after, journal['session_id'])
            if restored != current:
                _write(target, restored)
            if name == 'hosts' and before != after:
                _flush_dns()
        except Exception as exc:
            errors.append('%s: %s' % (name, exc))
    if journal['certificate_add_attempted'] and not journal['certificate_was_installed']:
        try:
            remove_certificate(_decode(journal['certificate_der']), journal['session_id'])
        except Exception as exc:
            errors.append('Windows Root: %s' % exc)
    if journal.get('firewall_add_attempted', False):
        try:
            windows_network.remove_firewall_rules(journal['session_id'])
        except Exception as exc:
            errors.append('Windows Firewall: %s' % exc)
    if errors:
        raise ClientConfigurationError('临时配置未完全还原；恢复记录已保留：\n' + '\n'.join(errors))
    journal_path.unlink()


def restore_stale_sessions(directory):
    for journal in sorted(Path(directory).glob('session-*.json')):
        restore_configuration(journal)


def wait_for_parent_and_restore(pid, journal_path, directory):
    """Called in a detached, elevated copy of the same EXE."""
    journal_path = Path(journal_path).resolve()
    directory = Path(directory).resolve()
    if journal_path.parent != directory or not re.fullmatch(r'session-[0-9a-f]{32}\.json', journal_path.name):
        raise ClientConfigurationError('恢复记录路径无效。')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, int(pid))
    if not handle and ctypes.get_last_error() != 87:
        raise ctypes.WinError(ctypes.get_last_error())
    ready = journal_path.with_suffix('.ready')
    try:
        ready.write_text(str(pid), encoding='ascii')
        if handle:
            result = kernel.WaitForSingleObject(handle, 0xffffffff)
            if result != 0:
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if handle:
            kernel.CloseHandle(handle)
    try:
        mutex = acquire_configuration_mutex(30000)
        try:
            restore_configuration(journal_path)
        finally:
            release_configuration_mutex(mutex)
    finally:
        ready.unlink(missing_ok=True)
