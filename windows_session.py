"""Desktop session lifecycle; Zwift and the server run as the original user."""

import json
import os
from pathlib import Path
import secrets
import select
import socket
import subprocess
import sys
import threading
import time

import windows_launcher as launcher
from windows_network import validate_server_ip, probe_server


DEFAULT_SETTINGS = {'mode': 'single', 'host_ip': '', 'join_ip': '', 'ride': True}


def load_settings(directory):
    settings = DEFAULT_SETTINGS.copy()
    path = Path(directory) / 'storage' / 'desktop-settings.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            for name, default in settings.items():
                if type(data.get(name)) is type(default):
                    settings[name] = data[name]
    except (OSError, ValueError):
        pass
    if settings['mode'] not in ('single', 'host', 'join'):
        settings['mode'] = 'single'
    return settings


def save_settings(directory, settings):
    path = Path(directory) / 'storage' / 'desktop-settings.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def validate_options(options):
    if not isinstance(options, dict) or options.get('mode') not in ('single', 'host', 'join'):
        raise ValueError('请选择本机单机、创建局域网或加入局域网。')
    mode = options['mode']
    ride = options.get('ride', True)
    if type(ride) is not bool:
        raise ValueError('本机骑行选项无效。')
    ip = '127.0.0.1' if mode == 'single' else validate_server_ip(options.get('server_ip', ''))
    return {'mode': mode, 'server_ip': ip, 'ride': ride if mode == 'host' else True}


def server_environment(options):
    """Override desktop modes without deleting an existing multiplayer setup."""
    environ = os.environ.copy()
    ip = options['server_ip']
    environ.update(ZOFFLINE_MULTIPLAYER='true' if options['mode'] == 'host' else 'false',
                   ZOFFLINE_SERVER_IP=ip, ZOFFLINE_SERVER_HOST=ip,
                   ZOFFLINE_API_USE_CERT='true', PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
    for service, port in (('CDN', 80), ('API', 443), ('TCP', 3025), ('UDP', 3024)):
        environ['ZOFFLINE_%s_HOST' % service] = ip
        environ['ZOFFLINE_%s_PORT' % service] = str(port)
    return environ


def prepare_server_storage(directory, options):
    storage = Path(directory) / 'storage'
    storage.mkdir(parents=True, exist_ok=True)
    if options['mode'] == 'host':
        # Keep account/profile data and pre-existing multiplayer configuration.
        (storage / 'multiplayer.txt').touch(exist_ok=True)
        (storage / 'server-ip.txt').write_text(options['server_ip'], encoding='ascii')


def run_server_worker(port, token):
    """Run original server on its main thread, with a parent lifetime channel."""
    with socket.create_connection(('127.0.0.1', port), timeout=10) as connection:
        connection.settimeout(None)
        connection.sendall((token + '\n').encode('ascii'))

        def watch_parent():
            try:
                # A stop message, a failed connection or EOF all end this worker.
                connection.recv(1024)
            finally:
                os._exit(0)

        threading.Thread(target=watch_parent, daemon=True).start()

        def ready():
            connection.sendall(b'{"status":"ready"}\n')

        launcher.runpy.run_module('standalone', run_name='__main__',
                                  init_globals={'on_server_ready': ready})
    return 0


class SessionCancelled(Exception):
    pass


class ServerProcess:
    def __init__(self, directory, emit):
        self.directory = Path(directory)
        self.emit = emit
        self.process = None
        self.connection = None
        self.reader = None
        self.log_offset = 0
        self.log_path = self.directory / 'logs' / 'desktop-server.log'

    def _read_output(self):
        try:
            with self.log_path.open('r', encoding='utf-8', errors='replace') as log:
                log.seek(self.log_offset)
                while True:
                    line = log.readline()
                    if line:
                        self.emit('log', line.rstrip())
                    elif self.process.poll() is not None:
                        break
                    else:
                        time.sleep(0.1)
        except OSError as exc:
            self.emit('log', '无法读取服务日志：%s' % exc)

    def start(self, options, stop_event, timeout=60, check_helper=None):
        token = secrets.token_hex(32)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.touch(exist_ok=True)
        self.log_offset = self.log_path.stat().st_size
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(0.2)
            command = [sys.executable]
            if not getattr(sys, 'frozen', False):
                command.append(str(Path(launcher.__file__).resolve()))
            command += ['--server-worker', str(listener.getsockname()[1]), token]
            self.process = subprocess.Popen(
                command, cwd=str(self.directory), env=server_environment(options),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.reader = threading.Thread(target=self._read_output, daemon=True)
            self.reader.start()
            deadline = time.monotonic() + timeout
            pending = b''
            authenticated = False
            while time.monotonic() < deadline:
                if stop_event.is_set():
                    raise SessionCancelled()
                if check_helper is not None:
                    check_helper()
                if self.process.poll() is not None:
                    raise RuntimeError('服务启动失败（退出码 %s），请查看下方日志。' % self.process.returncode)
                if self.connection is None:
                    try:
                        self.connection, _ = listener.accept()
                        self.connection.settimeout(0.2)
                    except socket.timeout:
                        continue
                try:
                    block = self.connection.recv(8192)
                except socket.timeout:
                    continue
                if not block:
                    raise RuntimeError('服务启动连接已中断，请查看下方日志。')
                pending += block
                if len(pending) > 8192:
                    raise RuntimeError('服务进程返回了无效状态。')
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    if not authenticated:
                        if line != token.encode('ascii'):
                            self.connection.close()
                            self.connection = None
                            pending = b''
                            break
                        authenticated = True
                    elif json.loads(line).get('status') == 'ready':
                        return
            raise RuntimeError('等待服务启动超时，请查看下方日志。')

    def stop(self):
        if self.connection is not None:
            try:
                self.connection.sendall(b'{"status":"stop"}\n')
            except OSError:
                pass
            self.connection.close()
            self.connection = None
        if self.process is not None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
        if self.reader is not None:
            self.reader.join(timeout=2)


class DesktopSession:
    def __init__(self, directory, emit):
        self.directory = Path(directory)
        self.emit = emit
        self.stop_event = threading.Event()
        self.thread = None

    @property
    def active(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, options, settings):
        if self.active:
            raise RuntimeError('当前会话尚未结束。')
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, args=(options, settings), daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _cancelled(self):
        if self.stop_event.is_set():
            raise SessionCancelled()

    @staticmethod
    def _check_helper(connection):
        if select.select([connection], [], [], 0)[0]:
            raise RuntimeError('临时配置辅助进程已断开，请停止游戏并重新连接。')

    def _run(self, options, settings):
        server = None
        connection = channel = None
        failure = None
        try:
            options = validate_options(options)
            installation = None
            if options['ride']:
                self.emit('status', '正在检查 Zwift 安装……')
                installation = launcher.find_zwift_installation(self.directory)
                if installation is None:
                    raise RuntimeError('请先安装Zwift')
            if options['mode'] == 'join':
                self.emit('status', '正在检测服务器 %s……' % options['server_ip'])
                self.emit('log', probe_server(options['server_ip'], stop_event=self.stop_event))
            else:
                launcher.preflight_ports(server_environment(options))
            self._cancelled()
            save_settings(self.directory, settings)
            self.emit('status', '等待 Windows 授权并准备临时配置……')
            connection, channel = launcher._start_elevated_configuration(installation, options)
            self._cancelled()
            self._check_helper(connection)
            if options['mode'] != 'join':
                prepare_server_storage(self.directory, options)
                self.emit('status', '正在启动服务……')
                server = ServerProcess(self.directory, self.emit)
                server.start(options, self.stop_event, check_helper=lambda: self._check_helper(connection))
                self._cancelled()
                self._check_helper(connection)
            if options['ride']:
                self.emit('status', '正在打开 Zwift Launcher……')
                launcher.launch_zwift(installation)
            self.emit('running', options)
            while not self.stop_event.wait(0.25):
                if server is not None and server.process.poll() is not None:
                    raise RuntimeError('服务已退出（退出码 %s），请查看日志。' % server.process.returncode)
                # The helper normally sends nothing until restoration. Detect its
                # early death rather than showing a stale "running" status.
                self._check_helper(connection)
        except SessionCancelled:
            pass
        except Exception as exc:
            if not self.stop_event.is_set():
                failure = str(exc)
                self.emit('log', failure)
        finally:
            self.emit('status', '正在停止服务并恢复本次配置……')
            if server is not None:
                try:
                    server.stop()
                except Exception as exc:
                    failure = (failure + '\n' if failure else '') + '服务停止失败：%s' % exc
            if channel is not None:
                try:
                    connection.settimeout(60)
                    launcher._send_message(channel, 'stop')
                    result = launcher._read_message(channel)
                    if result['status'] != 'restored':
                        raise RuntimeError(result.get('message', '配置还原失败。'))
                except Exception as exc:
                    failure = (failure + '\n' if failure else '') + '配置还原未确认：%s' % exc
                finally:
                    for resource in (channel, connection):
                        try:
                            resource.close()
                        except Exception as exc:
                            # A reset can make buffered STOP flush again on
                            # close. It must never suppress the terminal event.
                            failure = (failure + '\n' if failure else '') + '配置连接关闭失败：%s' % exc
            if failure:
                self.emit('error', failure)
            self.emit('stopped', failure)
