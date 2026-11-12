"""Windows desktop entry point; the source server remains standalone.py."""

import ctypes
from contextlib import contextmanager
from functools import partial
import json
import os
from pathlib import Path
import runpy
import secrets
import socket
import subprocess
import sys
import time
import traceback

from zwift_installation import find_zwift_installation, validate_zwift_installation
import windows_client_config as client_config


def runtime_directory():
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


def show_error(message):
    try:
        print(message, file=sys.stderr, flush=True)
    except Exception:
        pass  # A closed/redirected console must not prevent the native dialog.
    if os.name == 'nt':
        ctypes.windll.user32.MessageBoxW(None, message, 'zoffline', 0x10 | 0x10000)


def _is_elevated():
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def launch_zwift(installation):
    print('服务已就绪，正在启动 Zwift：%s' % installation, flush=True)
    subprocess.Popen([str(installation / 'ZwiftLauncher.exe')], cwd=str(installation))


def start_server(installation):
    runpy.run_module('standalone', run_name='__main__', init_globals={
        'on_server_ready': partial(launch_zwift, installation),
    })


def _process_name(pid):
    """Return the image name of a PID, or '' when it is not accessible."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                  ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, 0, int(pid))
    if not handle:
        return ''
    try:
        size = ctypes.c_ulong(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ''
        return os.path.basename(buffer.value)
    finally:
        kernel.CloseHandle(handle)


def _port_owner(port, kind):
    """Best-effort lookup of the local process holding a port: (pid, image name)."""
    protocol = 'TCP' if kind == socket.SOCK_STREAM else 'UDP'
    try:
        completed = subprocess.run(['netstat', '-ano'], capture_output=True,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError:
        return None
    # netstat writes in the OEM code page; decoding as UTF-8 silently yields nothing.
    output = completed.stdout.decode('oem', errors='replace') if completed.stdout else ''
    for line in output.splitlines():
        columns = line.split()
        if len(columns) < 4 or columns[0].upper() != protocol:
            continue
        if not columns[1].endswith(':%d' % port) or not columns[-1].isdigit():
            continue
        pid = int(columns[-1])
        if pid == os.getpid():
            continue
        return pid, _process_name(pid)
    return None


def preflight_ports(environ=None):
    """Check the same bind addresses and ports used by the original server."""
    environ = os.environ if environ is None else environ
    host = environ.get('ZOFFLINE_SERVER_HOST', '')
    endpoints = (
        (environ.get('ZOFFLINE_CDN_HOST', host), int(environ.get('ZOFFLINE_CDN_PORT', 80)), socket.SOCK_STREAM),
        (environ.get('ZOFFLINE_TCP_HOST', host), int(environ.get('ZOFFLINE_TCP_PORT', 3025)), socket.SOCK_STREAM),
        (environ.get('ZOFFLINE_UDP_HOST', host), int(environ.get('ZOFFLINE_UDP_PORT', 3024)), socket.SOCK_DGRAM),
        (environ.get('ZOFFLINE_API_HOST', host or '0.0.0.0'), int(environ.get('ZOFFLINE_API_PORT', 443)), socket.SOCK_STREAM),
    )
    for address, port, kind in endpoints:
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        try:
            with socket.socket(family, kind) as probe:
                probe.bind((address, port))
        except OSError as exc:
            owner = _port_owner(port, kind)
            if owner is not None:
                reason = '，该端口已被 %s（PID %d）占用，请先退出该程序' % (owner[1] or '其他进程', owner[0])
            elif getattr(exc, 'winerror', None) == 10013:
                # Windows reports wildcard-bind conflicts as "access denied", which
                # reads like a permissions problem but usually is another program.
                reason = '，可能被其他程序以通配地址占用'
            else:
                reason = ''
            raise RuntimeError('服务端口 %s 无法使用%s：%s' % (port, reason, exc)) from exc


def run_elevated_copy(installation, port, token, options=None):
    """Elevate only the temporary client-configuration helper."""
    shell = ctypes.windll.shell32
    shell.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                    ctypes.c_wchar_p, ctypes.c_wchar_p,
                                    ctypes.c_wchar_p, ctypes.c_int]
    shell.ShellExecuteW.restype = ctypes.c_void_p
    arguments = ['--config-helper', str(port), token, str(installation) if installation else '']
    if options is not None:
        arguments.append(json.dumps(options, ensure_ascii=True))
    if not getattr(sys, 'frozen', False):
        arguments.insert(0, str(Path(__file__).resolve()))
    result = shell.ShellExecuteW(None, 'runas', str(Path(sys.executable).resolve()),
                                 subprocess.list2cmdline(arguments), str(runtime_directory()), 0)
    if not result or result <= 32:
        raise RuntimeError('未获得修改 hosts 和证书所需的 Windows UAC 权限（代码 %s）。' % result)


def _send_message(channel, status, message=None):
    payload = {'status': status}
    if message is not None:
        payload['message'] = message
    channel.write((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
    channel.flush()


def _read_message(channel):
    line = channel.readline(8192)
    if not line or not line.endswith(b'\n'):
        raise RuntimeError('配置辅助进程连接中断。')
    message = json.loads(line.decode('utf-8'))
    if not isinstance(message, dict) or not isinstance(message.get('status'), str):
        raise RuntimeError('配置辅助进程返回了无效状态。')
    return message


def _start_elevated_configuration(installation, options=None):
    token = secrets.token_hex(32)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        if options is None:
            run_elevated_copy(installation, listener.getsockname()[1], token)
        else:
            run_elevated_copy(installation, listener.getsockname()[1], token, options)
        deadline = time.monotonic() + 120  # Includes time spent in the UAC prompt.
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('等待临时 hosts/证书配置超时。')
            listener.settimeout(remaining)
            try:
                connection, _ = listener.accept()
            except socket.timeout as exc:
                raise RuntimeError('等待临时 hosts/证书配置超时。') from exc
            connection.settimeout(90)  # Certificate/firewall setup and recovery watcher.
            channel = connection.makefile('rwb')
            accepted = False
            try:
                if channel.readline(128) != (token + '\n').encode('ascii'):
                    continue
                result = _read_message(channel)
                if result['status'] != 'ready':
                    raise RuntimeError(result.get('message', '临时 hosts/证书配置失败。'))
                connection.settimeout(None)
                accepted = True
                return connection, channel
            finally:
                if not accepted:
                    channel.close()
                    connection.close()


def _run_configuration_helper(port, token, installation, options=None):
    """Hold the elevated configuration until the user process stops."""
    with socket.create_connection(('127.0.0.1', int(port)), timeout=30) as connection:
        connection.settimeout(None)
        with connection.makefile('rwb') as channel:
            channel.write((token + '\n').encode('ascii'))
            channel.flush()
            try:
                if not _is_elevated():
                    raise RuntimeError('配置辅助进程未获得管理员权限。')
                if options is not None:
                    from windows_session import validate_options
                    options = validate_options(options)
                needs_client = options is None or options['ride']
                validated = validate_zwift_installation(installation) if needs_client else None
                if needs_client and validated is None:
                    raise RuntimeError('Zwift 安装目录在提权后不再有效：%s' % installation)
                configuration = (temporary_configuration(validated) if options is None else
                                 temporary_configuration(validated, options))
                with configuration:
                    _send_message(channel, 'ready')
                    try:
                        _read_message(channel)  # STOP or EOF, including forced parent exit.
                    except (OSError, RuntimeError, ValueError):
                        pass
                _send_message(channel, 'restored')
                return 0
            except Exception as exc:
                try:
                    _send_message(channel, 'error', str(exc))
                except OSError:
                    pass
                return 1


def _start_recovery_watcher(session):
    if getattr(sys, 'frozen', False):
        command = [sys.executable, '--restore-watch', str(os.getpid()), str(session)]
    else:
        command = [sys.executable, str(Path(__file__).resolve()), '--restore-watch',
                   str(os.getpid()), str(session)]
    process = subprocess.Popen(command, cwd=str(runtime_directory()),
                               creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, close_fds=True)
    ready = Path(session).with_suffix('.ready')
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if ready.is_file():
            return
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise RuntimeError('临时配置恢复进程未能就绪；本次配置将立即回滚。')


@contextmanager
def temporary_configuration(installation, options=None):
    mutex = client_config.acquire_configuration_mutex()
    session = None
    try:
        journal_directory = client_config.configuration_directory()
        client_config.restore_stale_sessions(journal_directory)
        if options is None:
            preflight_ports()
            session = client_config.apply_configuration(installation, journal_directory)
        else:
            session = client_config.apply_configuration(
                installation, journal_directory, server_ip=options['server_ip'],
                allow_lan=options['mode'] == 'host')
        _start_recovery_watcher(session)
        print('临时 hosts/证书已配置；退出时将自动恢复。', flush=True)
        yield
    finally:
        try:
            if session is not None:
                client_config.restore_configuration(session)
        finally:
            client_config.release_configuration_mutex(mutex)


def _run_configured(installation):
    with temporary_configuration(installation):
        start_server(installation)


def _run_with_elevated_configuration(installation):
    preflight_ports()
    connection, channel = _start_elevated_configuration(installation)
    try:
        print('临时 hosts/证书已配置；退出时将自动恢复。', flush=True)
        preflight_ports()
        start_server(installation)  # Runs and launches Zwift as the original user.
    finally:
        try:
            connection.settimeout(60)
            _send_message(channel, 'stop')
            result = _read_message(channel)
            if result['status'] != 'restored':
                raise RuntimeError(result.get('message', '临时 hosts/证书还原失败。'))
        finally:
            channel.close()
            connection.close()


def run_local():
    """Original local startup, retained for source integrations and regressions."""
    installation = find_zwift_installation(runtime_directory())
    if installation is None:
        show_error('请先安装Zwift')
        return 1
    print('找到 Zwift：%s\n正在启动 zoffline 服务……' % installation, flush=True)
    if _is_elevated():
        _run_configured(installation)
    else:
        _run_with_elevated_configuration(installation)
    return 0


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    try:
        if os.name != 'nt':
            raise RuntimeError('此启动入口仅支持 Windows；其他平台请运行 standalone.py。')
        if arguments:
            if len(arguments) == 3 and arguments[0] == '--restore-watch':
                client_config.wait_for_parent_and_restore(
                    int(arguments[1]), arguments[2], client_config.configuration_directory())
                return 0
            if len(arguments) == 4 and arguments[0] == '--config-helper':
                return _run_configuration_helper(arguments[1], arguments[2], arguments[3])
            if len(arguments) == 5 and arguments[0] == '--config-helper':
                return _run_configuration_helper(arguments[1], arguments[2], arguments[3],
                                                 json.loads(arguments[4]))
            if len(arguments) == 3 and arguments[0] == '--server-worker':
                logs = runtime_directory() / 'logs'
                logs.mkdir(parents=True, exist_ok=True)
                sys.stdout = sys.stderr = (logs / 'desktop-server.log').open(
                    'a', encoding='utf-8', buffering=1)
                from windows_session import run_server_worker
                return run_server_worker(int(arguments[1]), arguments[2])
            raise RuntimeError('未知启动参数。')
        from windows_gui import run_gui
        return run_gui()
    except KeyboardInterrupt:
        return 0
    except SystemExit as exc:
        if exc.code in (None, 0):
            return 0
        show_error('zoffline 启动失败（退出码 %s）。请查看控制台日志。' % exc.code)
        return 1
    except Exception as exc:
        try:
            traceback.print_exc()
        except Exception:
            pass
        if arguments and arguments[0] == '--server-worker':
            print('zoffline 服务启动失败：%s' % exc, file=sys.stderr, flush=True)
        else:
            show_error('zoffline 启动失败：\n%s' % exc)
        return 1


if __name__ == '__main__':
    result = 1
    try:
        for name in ('stdout', 'stderr'):
            stream = getattr(sys, name)
            if stream is None:
                stream = open(os.devnull, 'w', encoding='utf-8')
                setattr(sys, name, stream)
            elif hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8', errors='backslashreplace')
        result = main()
    finally:
        # Failed startup can leave non-daemon server threads behind. Always
        # terminate this process, even if a redirected output pipe is closed.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, UnicodeError, AttributeError):
                pass
        os._exit(result)
