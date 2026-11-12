"""Lifecycle regressions without writing Windows system configuration."""

import ast
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import windows_launcher as launcher
import windows_session as session


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='zoffline LAN 中文 ')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.events = []
        self.controller = session.DesktopSession(self.directory, self.emit)
        self.server = Mock()
        self.server.process.poll.return_value = None
        self.original_socket, self.peer_socket = socket.socketpair()
        self.channel = self.original_socket.makefile('rwb')
        self.requests = []
        self.helper = threading.Thread(target=self.helper_peer, daemon=True)
        self.helper.start()
        self.addCleanup(self.close_peer)
        self.installation = patch.object(launcher, 'find_zwift_installation',
                                         return_value=self.directory / 'Zwift').start()
        self.configure = patch.object(launcher, '_start_elevated_configuration',
                                      return_value=(self.original_socket, self.channel)).start()
        self.preflight = patch.object(launcher, 'preflight_ports').start()
        self.launch = patch.object(launcher, 'launch_zwift').start()
        self.probe = patch.object(session, 'probe_server', return_value='TCP ready').start()
        self.server_class = patch.object(session, 'ServerProcess', return_value=self.server).start()
        self.addCleanup(patch.stopall)

    def helper_peer(self):
        with self.peer_socket, self.peer_socket.makefile('rwb') as peer:
            line = peer.readline()
            if line:
                self.requests.append(json.loads(line))
                peer.write(b'{"status":"restored"}\n')
                peer.flush()

    def close_peer(self):
        self.channel.close()
        self.original_socket.close()
        self.helper.join(timeout=2)
        self.assertFalse(self.helper.is_alive())

    def emit(self, kind, value):
        self.events.append((kind, value))
        if kind == 'running':
            self.controller.stop()

    def run_mode(self, mode, ride=True):
        options = {'mode': mode, 'server_ip': '192.168.1.100', 'ride': ride}
        settings = dict(session.DEFAULT_SETTINGS, mode=mode)
        self.controller._run(options, settings)
        return options

    def assert_restored(self):
        self.helper.join(timeout=2)
        self.assertEqual(self.requests, [{'status': 'stop'}])
        self.assertEqual(self.events[-1], ('stopped', None))

    def test_join_uses_remote_configuration_without_local_server_or_port_bind(self):
        options = self.run_mode('join')
        self.probe.assert_called_once_with('192.168.1.100', stop_event=self.controller.stop_event)
        self.configure.assert_called_once_with(self.directory / 'Zwift', options)
        self.preflight.assert_not_called()
        self.server_class.assert_not_called()
        self.launch.assert_called_once_with(self.directory / 'Zwift')
        self.assertFalse((self.directory / 'storage' / 'multiplayer.txt').exists())
        self.assert_restored()

    def test_server_only_needs_no_installation_or_client_launcher(self):
        options = self.run_mode('host', ride=False)
        self.installation.assert_not_called()
        self.launch.assert_not_called()
        self.configure.assert_called_once_with(None, options)
        self.assertTrue((self.directory / 'storage' / 'multiplayer.txt').is_file())
        self.assertEqual((self.directory / 'storage' / 'server-ip.txt').read_text(), '192.168.1.100')
        self.assertEqual(self.server.start.call_args.args, (options, self.controller.stop_event))
        self.assertTrue(callable(self.server.start.call_args.kwargs['check_helper']))
        self.server.stop.assert_called_once()
        self.assert_restored()

    def test_local_launcher_only_opens_after_server_readiness_and_preserves_multiplayer(self):
        storage = self.directory / 'storage'
        storage.mkdir()
        (storage / 'multiplayer.txt').write_text('existing')
        (storage / 'server-ip.txt').write_text('192.168.1.200')
        events = []
        self.server.start.side_effect = lambda *_, **__: events.append('ready')
        self.launch.side_effect = lambda *_: events.append('launch')
        self.run_mode('single')
        self.assertEqual(events, ['ready', 'launch'])
        self.assertEqual((storage / 'multiplayer.txt').read_text(), 'existing')
        self.assertEqual((storage / 'server-ip.txt').read_text(), '192.168.1.200')
        env = self.preflight.call_args.args[0]
        self.assertEqual(env['ZOFFLINE_MULTIPLAYER'], 'false')
        self.assertEqual(env['ZOFFLINE_SERVER_IP'], '127.0.0.1')
        self.assert_restored()

    def test_failed_server_start_stops_and_restores_without_game(self):
        self.server.start.side_effect = RuntimeError('bind failed')
        self.run_mode('host')
        self.server.stop.assert_called_once()
        self.launch.assert_not_called()
        self.helper.join(timeout=2)
        self.assertEqual(self.requests, [{'status': 'stop'}])
        self.assertIn(('error', 'bind failed'), self.events)

    def test_cancelled_server_start_stops_and_restores_without_game(self):
        self.server.start.side_effect = session.SessionCancelled()
        self.run_mode('host')
        self.server.stop.assert_called_once()
        self.launch.assert_not_called()
        self.assert_restored()

    def test_cancel_while_authorizing_restores_before_launching(self):
        def authorized(*_):
            self.controller.stop()
            return self.original_socket, self.channel
        self.configure.side_effect = authorized
        self.run_mode('join')
        self.launch.assert_not_called()
        self.server_class.assert_not_called()
        self.assert_restored()

    def test_helper_loss_during_server_start_never_launches_game(self):
        def lost_helper(*_, **__):
            self.peer_socket.shutdown(socket.SHUT_RDWR)
        self.server.start.side_effect = lost_helper
        self.run_mode('host')
        self.launch.assert_not_called()
        self.server.stop.assert_called_once()
        self.assertFalse(any(kind == 'running' for kind, _ in self.events))
        self.assertTrue(any(kind == 'error' and '辅助进程' in value for kind, value in self.events))

    def test_cancelling_connection_check_does_not_configure_or_report_failure(self):
        def cancelled(*_, **__):
            self.controller.stop()
            raise RuntimeError('连接检测已取消')
        self.probe.side_effect = cancelled
        self.run_mode('join')
        self.configure.assert_not_called()
        self.launch.assert_not_called()
        self.assertEqual(self.events[-1], ('stopped', None))

    def test_buffered_channel_close_failure_still_reports_stopped(self):
        proxy = Mock(wraps=self.channel)
        proxy.close.side_effect = ConnectionResetError('reset while flushing')
        self.configure.return_value = (self.original_socket, proxy)
        self.run_mode('join')
        self.assertEqual(self.events[-1][0], 'stopped')
        self.assertIn('配置连接关闭失败', self.events[-1][1])
        self.assertTrue(any(kind == 'error' for kind, _ in self.events))

    def test_missing_installation_fails_before_configuration_or_service(self):
        self.installation.return_value = None
        self.run_mode('join')
        self.configure.assert_not_called()
        self.server_class.assert_not_called()
        self.assertIn(('error', '请先安装Zwift'), self.events)

    def test_invalid_address_cannot_reach_configuration(self):
        self.controller._run({'mode': 'join', 'server_ip': '127.0.0.1'}, session.DEFAULT_SETTINGS)
        self.configure.assert_not_called()
        self.probe.assert_not_called()
        self.assertEqual(self.events[-1][0], 'stopped')


class SettingsAndServiceTests(unittest.TestCase):
    def test_preferences_roundtrip_and_corrupt_file_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = dict(session.DEFAULT_SETTINGS, mode='join', join_ip='192.168.1.2')
            session.save_settings(directory, settings)
            self.assertEqual(session.load_settings(directory), settings)
            path = Path(directory) / 'storage' / 'desktop-settings.json'
            for value in ('[]', 'broken', '{"mode":"bad","ride":"false"}'):
                path.write_text(value)
                self.assertEqual(session.load_settings(directory), session.DEFAULT_SETTINGS)

    def test_health_endpoint_reports_mode_without_exposing_player_data(self):
        source = Path(launcher.__file__).with_name('zwift_offline.py')
        tree = ast.parse(source.read_text(encoding='utf-8'))
        function = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                        and item.name == 'zoffline_health')
        app = SimpleNamespace(route=lambda *a, **kw: lambda f: f)
        for mode in (False, True):
            namespace = {'app': app, 'jsonify': lambda value: value, 'MULTIPLAYER': mode}
            exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
            self.assertEqual(namespace['zoffline_health'](), {'service': 'zoffline', 'multiplayer': mode})

    def test_default_entry_opens_gui_without_discovering_or_starting_service(self):
        with patch('windows_gui.run_gui', return_value=0) as gui, \
                patch.object(launcher, 'find_zwift_installation') as discovery:
            self.assertEqual(launcher.main([]), 0)
        gui.assert_called_once_with()
        discovery.assert_not_called()

    def test_server_only_helper_uses_options_without_installation_discovery(self):
        options = {'mode': 'host', 'server_ip': '192.168.1.100', 'ride': False}
        user, helper = socket.socketpair()
        events = []

        @contextmanager
        def configured(directory, actual):
            events.append((directory, actual))
            yield
            events.append('restored')

        def parent():
            with user, user.makefile('rwb') as channel:
                self.assertEqual(channel.readline(), b'token\n')
                self.assertEqual(json.loads(channel.readline())['status'], 'ready')
                launcher._send_message(channel, 'stop')
                self.assertEqual(json.loads(channel.readline())['status'], 'restored')

        thread = threading.Thread(target=parent, daemon=True)
        thread.start()
        with patch.object(socket, 'create_connection', return_value=helper), \
                patch.object(launcher, '_is_elevated', return_value=True), \
                patch.object(launcher, 'validate_zwift_installation') as validate, \
                patch.object(launcher, 'temporary_configuration', side_effect=configured):
            self.assertEqual(launcher._run_configuration_helper(1, 'token', '', options), 0)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        validate.assert_not_called()
        self.assertEqual(events, [(None, options), 'restored'])


class ServerProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='LAN child 中文 ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.worker = self.root / 'fake worker.py'
        self.worker.write_text(
            'import socket, sys\n'
            'connection = socket.create_connection(("127.0.0.1", int(sys.argv[2])))\n'
            'connection.sendall((sys.argv[3] + "\\n").encode())\n'
            'connection.sendall(b\'{"status":"ready"}\\n\')\n'
            'connection.recv(1024)\n', encoding='utf-8')
        self.server = session.ServerProcess(self.root, lambda *_: None)
        self.addCleanup(self.server.stop)

    def test_real_child_authenticates_reports_ready_and_exits_on_stop(self):
        with patch.object(launcher, '__file__', str(self.worker)):
            self.server.start({'mode': 'single', 'server_ip': '127.0.0.1'}, threading.Event(), timeout=5)
        self.assertIsNone(self.server.process.poll())
        self.server.stop()
        self.assertEqual(self.server.process.returncode, 0)

    def test_cancelled_start_leaves_no_worker(self):
        stop = threading.Event()
        stop.set()
        with patch.object(launcher, '__file__', str(self.worker)), self.assertRaises(session.SessionCancelled):
            self.server.start({'mode': 'single', 'server_ip': '127.0.0.1'}, stop, timeout=5)
        self.server.stop()
        self.assertIsNotNone(self.server.process.returncode)

    def test_worker_exits_when_parent_lifetime_socket_disappears(self):
        with patch.object(launcher, '__file__', str(self.worker)):
            self.server.start({'mode': 'single', 'server_ip': '127.0.0.1'}, threading.Event(), timeout=5)
        self.server.connection.close()
        self.server.connection = None
        self.assertEqual(self.server.process.wait(timeout=3), 0)


if __name__ == '__main__':
    unittest.main()
