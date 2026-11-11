"""Focused regressions for Windows discovery, startup order and error handling."""

import ast
from contextlib import contextmanager
import ctypes
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import windows_launcher
import zwift_installation as discovery


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='zoffline tests ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = patch.object(discovery, '_registry_candidates', return_value=[]).start()
        self.common = patch.object(discovery, '_common_candidates', return_value=[]).start()
        self.addCleanup(patch.stopall)

    def install(self, name):
        directory = self.root / name
        for name in discovery.REQUIRED_FILES:
            file = directory / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'test fixture')
        return directory

    def test_legacy_cache_has_stable_priority_over_registry(self):
        preferred = self.install('中文 Zwift')
        other = self.install('another installation')
        cache = self.root / 'scripts' / 'zwift_location.txt'
        cache.parent.mkdir()
        cache.write_text('"%s"\n' % preferred, encoding='utf-8-sig')
        self.registry.return_value = [other]
        self.assertEqual(discovery.find_zwift_installation(self.root), preferred)
        self.registry.assert_not_called()

    def test_incomplete_installation_is_skipped(self):
        incomplete = self.install('incomplete')
        (incomplete / 'data' / 'cacert.pem').unlink()
        valid = self.install('valid')
        self.registry.return_value = [incomplete, valid]
        self.assertEqual(discovery.find_zwift_installation(self.root), valid)

    def test_launcher_directory_is_not_a_valid_executable(self):
        incomplete = self.root / 'directory instead of executable'
        (incomplete / 'ZwiftLauncher.exe').mkdir(parents=True)
        self.common.return_value = [incomplete]
        self.assertIsNone(discovery.find_zwift_installation(self.root))

    def test_absence_returns_none(self):
        self.common.return_value = [self.root / 'absent']
        self.assertIsNone(discovery.find_zwift_installation(self.root))

    def test_access_error_does_not_hide_a_later_valid_installation(self):
        blocked = self.root / 'blocked'
        valid = self.install('valid')
        original_stat = Path.stat

        def fake_stat(path, *args, **kwargs):
            if path == blocked / 'ZwiftLauncher.exe':
                raise PermissionError('access denied')
            return original_stat(path, *args, **kwargs)

        self.registry.return_value = [blocked, valid]
        with patch.object(Path, 'stat', fake_stat):
            self.assertEqual(discovery.find_zwift_installation(self.root), valid)

    def test_access_failure_is_distinct_from_not_installed(self):
        self.common.return_value = [self.root / 'blocked']
        with patch.object(Path, 'stat', side_effect=PermissionError('access denied')):
            with self.assertRaisesRegex(discovery.InstallationDiscoveryError, 'access denied'):
                discovery.find_zwift_installation(self.root)

    def test_duplicate_candidates_are_only_checked_once(self):
        missing = self.root / 'Missing'
        self.common.return_value = [missing, str(missing).upper(), missing]
        with patch.object(Path, 'stat', side_effect=FileNotFoundError) as stat:
            self.assertIsNone(discovery.find_zwift_installation(self.root))
        self.assertEqual(stat.call_count, 1)

    def test_commands_and_icons_support_spaces_and_chinese(self):
        directory = self.root / '中文 Program Files' / 'Zwift'
        for value in (str(directory), '"%s"' % directory,
                      '"%s",0' % (directory / 'ZwiftLauncher.exe'),
                      '"%s" /SILENT' % (directory / 'unins000.exe'),
                      '%s /SILENT' % (directory / 'unins000.exe')):
            with self.subTest(value=value):
                self.assertEqual(discovery.candidate_directory(value), directory)

    def test_relative_and_network_records_are_skipped_before_stat(self):
        for value in ('Zwift', r'\\server\share\Zwift', r'\\?\UNC\server\share\Zwift'):
            self.assertIsNone(discovery.candidate_directory(value))

    def test_malformed_cache_record_does_not_prevent_registry_discovery(self):
        valid = self.install('valid')
        cache = self.root / 'scripts' / 'zwift_location.txt'
        cache.parent.mkdir()
        cache.write_text(str(self.root / 'invalid') + '\x00broken', encoding='utf-8')
        self.registry.return_value = [valid]
        self.assertEqual(discovery.find_zwift_installation(self.root), valid)

    @unittest.skipUnless(hasattr(ctypes, 'windll'), 'Windows drive types')
    def test_mapped_network_drive_is_skipped(self):
        with patch.object(ctypes.windll.kernel32, 'GetDriveTypeW', return_value=4):
            self.assertIsNone(discovery.candidate_directory(r'Z:\Games\Zwift'))


class FakeRegistry:
    HKEY_CURRENT_USER = 'HKCU'
    HKEY_LOCAL_MACHINE = 'HKLM'
    KEY_READ = 1
    KEY_WOW64_64KEY = 0x100
    KEY_WOW64_32KEY = 0x200

    def __init__(self, entries):
        self.entries = entries

    @contextmanager
    def OpenKey(self, hive, path, reserved, access):
        identity = (hive, access & ~self.KEY_READ, path)
        if identity not in self.entries:
            raise FileNotFoundError(path)
        yield self.entries[identity]

    @staticmethod
    def QueryValueEx(key, name):
        if name not in key:
            raise FileNotFoundError(name)
        return key[name], 1

    @staticmethod
    def QueryInfoKey(key):
        return len(key.get('children', [])), 0, 0

    @staticmethod
    def EnumKey(key, index):
        return key['children'][index]


class RegistryTests(unittest.TestCase):
    def test_hklm_32bit_uninstall_location_is_discovered(self):
        root = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
        fake = FakeRegistry({
            ('HKLM', 0x200, root): {'children': ['{UUID}_is1']},
            ('HKLM', 0x200, root + r'\{UUID}_is1'): {
                'DisplayName': 'Zwift', 'InstallLocation': r'F:\Zwift',
            },
        })
        errors = []
        with patch.object(discovery, 'winreg', fake):
            candidates = list(discovery._registry_candidates(errors))
        self.assertIn(r'F:\Zwift', candidates)
        self.assertEqual(errors, [])

    def test_hkcu_app_path_precedes_machine_registry(self):
        root = 'SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\ZwiftLauncher.exe'
        fake = FakeRegistry({
            ('HKCU', 0x100, root): {'': r'C:\User Games\Zwift\ZwiftLauncher.exe'},
            ('HKLM', 0x200, root): {'': r'F:\Zwift\ZwiftLauncher.exe'},
        })
        with patch.object(discovery, 'winreg', fake):
            candidates = [item for item in discovery._registry_candidates([]) if item]
        self.assertEqual(candidates, [r'C:\User Games\Zwift\ZwiftLauncher.exe', r'F:\Zwift\ZwiftLauncher.exe'])


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.preflight = patch.object(windows_launcher, 'preflight_ports').start()
        self.elevation = patch.object(windows_launcher, '_is_elevated', return_value=True).start()
        self.addCleanup(patch.stopall)

    def test_absence_shows_exact_prompt_and_never_starts_server(self):
        with patch.object(windows_launcher, 'find_zwift_installation', return_value=None), \
                patch.object(windows_launcher, 'show_error') as error, \
                patch.object(windows_launcher, '_run_configured') as server:
            self.assertEqual(windows_launcher.main([]), 1)
        error.assert_called_once_with('请先安装Zwift')
        server.assert_not_called()

    def test_permission_error_is_not_reported_as_missing_install(self):
        with patch.object(windows_launcher, 'find_zwift_installation', side_effect=PermissionError('denied')), \
                patch.object(windows_launcher, 'show_error') as error, \
                patch.object(windows_launcher, '_run_configured') as server:
            self.assertEqual(windows_launcher.main([]), 1)
        self.assertIn('denied', error.call_args.args[0])
        self.assertNotEqual(error.call_args.args[0], '请先安装Zwift')
        server.assert_not_called()

    def test_existing_installation_is_passed_to_original_server(self):
        installation = Path(tempfile.gettempdir()) / '中文 Zwift'
        with patch.object(windows_launcher, 'find_zwift_installation', return_value=installation), \
                patch.object(windows_launcher, '_run_configured') as server:
            self.assertEqual(windows_launcher.main([]), 0)
        server.assert_called_once_with(installation)

    def test_standard_user_keeps_server_and_launcher_in_original_process(self):
        installation = Path(tempfile.gettempdir()) / '中文 Zwift'
        self.elevation.return_value = False
        with patch.object(windows_launcher, 'find_zwift_installation', return_value=installation), \
                patch.object(windows_launcher, '_run_with_elevated_configuration') as run, \
                patch.object(windows_launcher, '_run_configured') as elevated_run:
            self.assertEqual(windows_launcher.main([]), 0)
        run.assert_called_once_with(installation)
        elevated_run.assert_not_called()

    def test_helper_receives_original_users_installation_without_rediscovery(self):
        installation = str(Path(tempfile.gettempdir()) / '中文 Zwift')
        with patch.object(windows_launcher, '_run_configuration_helper', return_value=0) as helper, \
                patch.object(windows_launcher, 'find_zwift_installation') as discover:
            self.assertEqual(windows_launcher.main(['--config-helper', '12345', 'token', installation]), 0)
        helper.assert_called_once_with('12345', 'token', installation)
        discover.assert_not_called()

    def test_elevated_helper_configures_passed_install_and_restores_on_stop(self):
        installation = Path(tempfile.gettempdir()) / '中文 Zwift'
        events = []
        replies = []

        @contextmanager
        def configured(directory):
            events.append(('configure', directory))
            try:
                yield
            finally:
                events.append(('restore', directory))

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)

            def parent():
                with listener.accept()[0] as connection, connection.makefile('rwb') as channel:
                    replies.append(channel.readline())
                    replies.append(json.loads(channel.readline()))
                    channel.write(b'{"status": "stop"}\n')
                    channel.flush()
                    replies.append(json.loads(channel.readline()))

            thread = threading.Thread(target=parent)
            thread.start()
            try:
                with patch.object(windows_launcher, 'validate_zwift_installation', return_value=installation), \
                        patch.object(windows_launcher, 'temporary_configuration', side_effect=configured), \
                        patch.object(windows_launcher, 'find_zwift_installation') as discover:
                    self.assertEqual(windows_launcher._run_configuration_helper(
                        listener.getsockname()[1], 'test-token', str(installation)), 0)
                discover.assert_not_called()
            finally:
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(replies, [b'test-token\n', {'status': 'ready'}, {'status': 'restored'}])
        self.assertEqual(events, [('configure', installation), ('restore', installation)])

    def test_user_server_waits_for_helper_restore_before_exiting(self):
        installation = Path(tempfile.gettempdir()) / '中文 Zwift'
        user_socket, helper_socket = socket.socketpair()
        channel = user_socket.makefile('rwb')
        received = []

        def helper():
            with helper_socket, helper_socket.makefile('rwb') as peer:
                received.append(windows_launcher._read_message(peer))
                windows_launcher._send_message(peer, 'restored')

        thread = threading.Thread(target=helper)
        thread.start()
        with patch.object(windows_launcher, '_start_elevated_configuration',
                          return_value=(user_socket, channel)), \
                patch.object(windows_launcher, 'start_server') as server:
            windows_launcher._run_with_elevated_configuration(installation)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        server.assert_called_once_with(installation)
        self.assertEqual(received, [{'status': 'stop'}])

    def test_partial_startup_failure_has_visible_error(self):
        with patch.object(windows_launcher, 'find_zwift_installation', return_value=Path(tempfile.gettempdir())), \
                patch.object(windows_launcher, '_run_configured', side_effect=OSError('port is in use')), \
                patch.object(windows_launcher, 'show_error') as error:
            self.assertEqual(windows_launcher.main([]), 1)
        self.assertIn('port is in use', error.call_args.args[0])

    def test_original_server_receives_callback_without_early_launcher_start(self):
        installation = Path(tempfile.gettempdir()) / '中文 Zwift'
        with patch.object(windows_launcher.runpy, 'run_module') as run, \
                patch.object(windows_launcher, 'launch_zwift') as launch:
            windows_launcher.start_server(installation)
            launch.assert_not_called()
            run.assert_called_once()
            self.assertEqual(run.call_args.args, ('standalone',))
            self.assertEqual(run.call_args.kwargs['run_name'], '__main__')
            run.call_args.kwargs['init_globals']['on_server_ready']()
            launch.assert_called_once_with(installation)

    def test_launcher_uses_argument_list_and_installation_working_directory(self):
        installation = Path(tempfile.gettempdir()) / '中文 Program Files' / 'Zwift'
        with patch.object(windows_launcher.subprocess, 'Popen') as popen:
            windows_launcher.launch_zwift(installation)
        popen.assert_called_once_with([str(installation / 'ZwiftLauncher.exe')], cwd=str(installation))

    def test_frozen_runtime_directory_uses_executable_not_current_directory(self):
        executable = Path(tempfile.gettempdir()) / '中文 Distribution' / 'zoffline.exe'
        with patch.object(windows_launcher.sys, 'frozen', True, create=True), \
                patch.object(windows_launcher.sys, 'executable', str(executable)):
            self.assertEqual(windows_launcher.runtime_directory(), executable.resolve().parent)

    @unittest.skipUnless(hasattr(ctypes, 'windll'), 'Windows native dialog')
    def test_error_uses_native_unicode_dialog(self):
        with patch.object(ctypes.windll.user32, 'MessageBoxW', return_value=1) as dialog:
            windows_launcher.show_error('请先安装Zwift')
        self.assertEqual(dialog.call_args.args[1], '请先安装Zwift')
        self.assertTrue(dialog.call_args.args[3] & 0x10000)

    @unittest.skipUnless(hasattr(ctypes, 'windll'), 'Windows native dialog')
    def test_closed_console_does_not_prevent_error_dialog(self):
        with patch.object(windows_launcher.sys, 'stderr') as stderr, \
                patch.object(ctypes.windll.user32, 'MessageBoxW', return_value=1) as dialog:
            stderr.write.side_effect = BrokenPipeError('closed pipe')
            windows_launcher.show_error('请先安装Zwift')
        self.assertEqual(dialog.call_args.args[1], '请先安装Zwift')


class ServerReadinessTests(unittest.TestCase):
    """Exercise the real startup function without importing server side effects."""

    def run_server(self, events, start_error=None):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / 'zwift_offline.py').read_text(encoding='utf-8'))
        function = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == 'run_standalone')
        server = Mock()

        def start():
            if start_error:
                raise start_error
            events.append('listening')

        server.start.side_effect = start
        server.serve_forever.side_effect = lambda: events.append('serving')
        namespace = {
            'MULTIPLAYER': True, 'LoginManager': MagicMock(), 'app': Mock(), 'db': Mock(),
            'threading': MagicMock(), 'send_server_back_online_message': Mock(), 'remove_inactive': Mock(),
            'logger': Mock(), 'ZWIFT_VER_CUR': 'test', 'SSL_DIR': str(root / 'ssl'),
            'os': SimpleNamespace(environ={'ZOFFLINE_API_USE_CERT': 'false'}),
            'ssl': MagicMock(), 'CleanWSGIServer': Mock(return_value=server),
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(root / 'zwift_offline.py'), 'exec'), namespace)
        namespace['run_standalone']({}, {}, {}, {}, {}, Mock(), Mock(),
                                    on_ready=lambda: events.append('launch'))

    def test_launcher_runs_after_successful_https_bind(self):
        events = []
        self.run_server(events)
        self.assertEqual(events, ['listening', 'launch', 'serving'])

    def test_failed_https_bind_never_launches_zwift(self):
        events = []
        with self.assertRaisesRegex(OSError, 'address in use'):
            self.run_server(events, OSError('address in use'))
        self.assertEqual(events, [])


if __name__ == '__main__':
    unittest.main()
