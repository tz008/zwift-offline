"""Regression tests for temporary hosts/certificate changes and recovery."""

import ctypes
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import windows_client_config as config
import windows_launcher


CERTIFICATE = b'-----BEGIN CERTIFICATE-----\nZml4dHVyZQ==\n-----END CERTIFICATE-----'
DER = b'fixture certificate'


class TemporaryConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='zoffline 中文 test ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.hosts = self.root / 'hosts'
        self.cacert = self.root / 'Zwift 中文' / 'data' / 'cacert.pem'
        self.cacert.parent.mkdir(parents=True)
        self.journals = self.root / 'journal'
        self.journals.mkdir()
        self.hosts.write_bytes(b'# original hosts\r\n1.2.3.4 unrelated.example\r\n')
        self.cacert.write_bytes(b'original certificate bundle\n')
        self.original_hosts = self.hosts.read_bytes()
        self.original_cacert = self.cacert.read_bytes()
        self.mock_paths = patch.object(config, '_paths', return_value={
            'hosts': self.hosts, 'cacert': self.cacert}).start()
        self.mock_cert = patch.object(config, '_cert_der', return_value=(CERTIFICATE, DER)).start()
        self.installed = patch.object(config, 'certificate_is_installed', return_value=False).start()
        self.add = patch.object(config, 'add_certificate').start()
        self.remove = patch.object(config, 'remove_certificate').start()
        self.flush_dns = patch.object(config, '_flush_dns').start()
        self.firewall_add = patch.object(config.windows_network, 'add_firewall_rules').start()
        self.firewall_remove = patch.object(config.windows_network, 'remove_firewall_rules').start()
        self.addCleanup(patch.stopall)

    def apply(self):
        return config.apply_configuration(self.cacert.parent.parent, self.journals)

    def test_adds_then_restores_both_files_and_new_root_cert(self):
        session = self.apply()
        self.assertIn(b'127.0.0.1 ' + b' '.join(config.DOMAINS), self.hosts.read_bytes())
        self.assertIn(CERTIFICATE, self.cacert.read_bytes())
        self.assertTrue(session.exists())
        self.add.assert_called_once_with(DER, session.stem.removeprefix('session-'))
        config.restore_configuration(session)
        self.assertEqual(self.hosts.read_bytes(), self.original_hosts)
        self.assertEqual(self.cacert.read_bytes(), self.original_cacert)
        self.remove.assert_called_once_with(DER, session.stem.removeprefix('session-'))
        self.assertFalse(session.exists())

    def test_remote_hosts_override_restores_existing_local_configuration(self):
        original = b'127.0.0.1 ' + b' '.join(config.DOMAINS) + b'\r\n'
        self.hosts.write_bytes(original)
        session = config.apply_configuration(self.cacert.parent.parent, self.journals,
                                             server_ip='192.168.1.100')
        self.assertIn(b'192.168.1.100 ' + b' '.join(config.DOMAINS), self.hosts.read_bytes())
        config.restore_configuration(session)
        self.assertEqual(self.hosts.read_bytes(), original)
        self.firewall_add.assert_not_called()
        self.firewall_remove.assert_not_called()

    def test_server_only_journals_firewall_before_add_without_touching_client(self):
        def add(session_id, server_ip):
            journal_path = self.journals / ('session-' + session_id + '.json')
            journal = json.loads(journal_path.read_text(encoding='utf-8'))
            self.assertTrue(journal['firewall_add_attempted'])
            self.assertEqual(journal['files'], {})
            self.assertEqual(server_ip, '192.168.1.100')
        self.firewall_add.side_effect = add
        session = config.apply_configuration(None, self.journals, server_ip='192.168.1.100', allow_lan=True)
        self.mock_paths.assert_not_called()
        self.mock_cert.assert_not_called()
        self.installed.assert_not_called()
        self.add.assert_not_called()
        config.restore_configuration(session)
        self.firewall_remove.assert_called_once_with(session.stem.removeprefix('session-'))
        self.remove.assert_not_called()
        self.assertEqual(self.hosts.read_bytes(), self.original_hosts)
        self.assertFalse(session.exists())

    def test_partial_firewall_failure_rolls_back_client_and_attempted_rules(self):
        self.firewall_add.side_effect = RuntimeError('UDP rule failed')
        with self.assertRaisesRegex(RuntimeError, 'UDP rule failed'):
            config.apply_configuration(self.cacert.parent.parent, self.journals,
                                       server_ip='192.168.1.100', allow_lan=True)
        self.firewall_remove.assert_called_once()
        self.assertEqual(self.hosts.read_bytes(), self.original_hosts)
        self.assertEqual(self.cacert.read_bytes(), self.original_cacert)
        self.assertFalse(list(self.journals.glob('session-*.json')))

    def test_firewall_restore_failure_preserves_journal_and_retries(self):
        session = config.apply_configuration(None, self.journals,
                                             server_ip='192.168.1.100', allow_lan=True)
        self.firewall_remove.side_effect = PermissionError('firewall denied')
        with self.assertRaisesRegex(config.ClientConfigurationError, 'Windows Firewall'):
            config.restore_configuration(session)
        self.assertTrue(session.exists())
        self.firewall_remove.side_effect = None
        config.restore_stale_sessions(self.journals)
        self.assertFalse(session.exists())

    def test_client_restore_conflict_still_removes_firewall_and_keeps_record(self):
        session = config.apply_configuration(self.cacert.parent.parent, self.journals,
                                             server_ip='192.168.1.100', allow_lan=True)
        changed = self.hosts.read_bytes().replace(b'192.168.1.100 ', b'192.168.1.101 ')
        self.hosts.write_bytes(changed)
        with self.assertRaises(config.ClientConfigurationError):
            config.restore_configuration(session)
        self.firewall_remove.assert_called_once()
        self.assertEqual(self.hosts.read_bytes(), changed)
        self.assertTrue(session.exists())

    def test_previous_version_journal_without_firewall_field_restores(self):
        session = self.apply()
        journal = json.loads(session.read_text(encoding='utf-8'))
        del journal['firewall_add_attempted']
        session.write_text(json.dumps(journal), encoding='utf-8')
        config.restore_configuration(session)
        self.firewall_remove.assert_not_called()
        self.assertEqual(self.hosts.read_bytes(), self.original_hosts)

    def test_invalid_lan_address_makes_no_changes(self):
        with self.assertRaises(ValueError):
            config.apply_configuration(None, self.journals, allow_lan=True)
        self.firewall_add.assert_not_called()
        self.assertFalse(list(self.journals.glob('session-*.json')))

    def test_existing_hosts_and_certificates_survive_unchanged(self):
        hosts = b'127.0.0.1 ' + b' '.join(config.DOMAINS) + b'\r\n'
        self.hosts.write_bytes(hosts)
        self.cacert.write_bytes(b'bundle\n' + CERTIFICATE + b'\n')
        with patch.object(config.ssl, 'PEM_cert_to_DER_cert', return_value=DER):
            self.installed.return_value = True
            session = self.apply()
            config.restore_configuration(session)
        self.assertEqual(self.hosts.read_bytes(), hosts)
        self.assertEqual(self.cacert.read_bytes(), b'bundle\n' + CERTIFICATE + b'\n')
        self.add.assert_not_called()
        self.remove.assert_not_called()

    def test_original_conflicting_mapping_and_concurrent_edits_survive(self):
        original = b'\xef\xbb\xbf8.8.8.8 secure.zwift.com\r\n1.2.3.4 unrelated.example\r\n'
        self.hosts.write_bytes(original)
        session = self.apply()
        self.assertTrue(self.hosts.read_bytes().startswith(b'\xef\xbb\xbf# zoffline-temporary-'))
        with self.hosts.open('ab') as stream:
            stream.write(b'9.9.9.9 newly-added.example\r\n')
        with self.cacert.open('ab') as stream:
            stream.write(b'OTHER CERTIFICATE\n')
        config.restore_configuration(session)
        self.assertEqual(self.hosts.read_bytes(), original + b'9.9.9.9 newly-added.example\r\n')
        self.assertEqual(self.cacert.read_bytes(), self.original_cacert + b'OTHER CERTIFICATE\n')

    def test_unrelated_names_on_conflicting_hosts_line_stay_active(self):
        original = b'10.0.0.1 secure.zwift.com intranet.local # company\r\n'
        self.hosts.write_bytes(original)
        session = self.apply()
        active = self.hosts.read_bytes()
        self.assertIn(b'10.0.0.1 intranet.local # zoffline-temporary-', active)
        self.assertIn(b'127.0.0.1 ' + b' '.join(config.DOMAINS), active)
        config.restore_configuration(session)
        self.assertEqual(self.hosts.read_bytes(), original)

    def test_concurrent_edit_to_owned_hosts_line_is_reported_and_preserved(self):
        original = b'10.0.0.1 secure.zwift.com intranet.local\n'
        self.hosts.write_bytes(original)
        session = self.apply()
        changed = self.hosts.read_bytes().replace(
            b'10.0.0.1 intranet.local #', b'10.0.0.1 intranet.local intranet2.local #')
        self.hosts.write_bytes(changed)
        with self.assertRaises(config.ClientConfigurationError):
            config.restore_configuration(session)
        self.assertEqual(self.hosts.read_bytes(), changed)
        self.assertEqual(self.cacert.read_bytes(), self.original_cacert)
        self.remove.assert_called_once_with(DER, session.stem.removeprefix('session-'))
        self.assertTrue(session.exists())

    def test_rollback_if_certificate_installation_fails(self):
        self.add.side_effect = PermissionError('denied')
        with self.assertRaisesRegex(PermissionError, 'denied'):
            self.apply()
        self.assertEqual(self.hosts.read_bytes(), self.original_hosts)
        self.assertEqual(self.cacert.read_bytes(), self.original_cacert)
        self.assertEqual(list(self.journals.glob('session-*.json')), [])
        self.remove.assert_not_called()

    def test_stale_session_is_restored_on_next_start(self):
        session = self.apply()
        config.restore_stale_sessions(self.journals)
        self.assertFalse(session.exists())
        self.assertEqual(self.hosts.read_bytes(), self.original_hosts)
        self.assertEqual(self.cacert.read_bytes(), self.original_cacert)

    def test_unchanged_files_allow_external_updates(self):
        self.hosts.write_bytes(b'127.0.0.1 ' + b' '.join(config.DOMAINS) + b'\n')
        self.cacert.write_bytes(CERTIFICATE)
        self.installed.return_value = True
        with patch.object(config.ssl, 'PEM_cert_to_DER_cert', return_value=DER):
            session = self.apply()
            self.hosts.write_bytes(self.hosts.read_bytes() + b'# concurrent update\n')
            self.cacert.write_bytes(self.cacert.read_bytes() + b'\n# concurrent update\n')
            config.restore_configuration(session)
        self.assertIn(b'concurrent update', self.hosts.read_bytes())
        self.assertIn(b'concurrent update', self.cacert.read_bytes())

    def test_partial_hosts_write_is_restored_from_journal(self):
        session = self.apply()
        desired = self.hosts.read_bytes()
        self.hosts.write_bytes(desired[:len(desired)//2])
        config.restore_configuration(session)
        self.assertEqual(self.hosts.read_bytes(), self.original_hosts)

    def test_tampered_hosts_marker_keeps_recovery_record(self):
        self.hosts.write_bytes(b'8.8.8.8 secure.zwift.com\n')
        session = self.apply()
        self.hosts.write_bytes(self.hosts.read_bytes().replace(
            b'127.0.0.1 ' + b' '.join(config.DOMAINS),
            b'127.0.0.2 ' + b' '.join(config.DOMAINS)))
        with self.assertRaises(config.ClientConfigurationError):
            config.restore_configuration(session)
        self.assertTrue(session.exists())
        self.assertEqual(self.cacert.read_bytes(), self.original_cacert)
        self.remove.assert_called_once_with(DER, session.stem.removeprefix('session-'))

    def test_reformatted_certificate_keeps_recovery_record(self):
        session = self.apply()
        self.cacert.write_bytes(self.original_cacert + b'# externally rewritten\n' + CERTIFICATE)
        with patch.object(config.ssl, 'PEM_cert_to_DER_cert', return_value=DER):
            with self.assertRaises(config.ClientConfigurationError):
                config.restore_configuration(session)
        self.assertTrue(session.exists())

    def test_user_and_helper_exchange_ready_stop_and_restore_real_test_files(self):
        helpers = []
        results = []
        installation = self.cacert.parent.parent

        def launch(directory, port, token):
            def run():
                results.append(windows_launcher._run_configuration_helper(port, token, str(directory)))
            thread = threading.Thread(target=run, daemon=True)
            helpers.append(thread)
            thread.start()

        def server(directory):
            self.assertEqual(directory, installation)
            self.assertIn(b'127.0.0.1 ' + b' '.join(config.DOMAINS), self.hosts.read_bytes())
            self.assertIn(CERTIFICATE, self.cacert.read_bytes())

        with patch.object(windows_launcher, '_is_elevated', return_value=True), \
                patch.object(windows_launcher, 'run_elevated_copy', side_effect=launch), \
                patch.object(windows_launcher, 'validate_zwift_installation', return_value=installation), \
                patch.object(windows_launcher, '_start_recovery_watcher'), \
                patch.object(windows_launcher, 'preflight_ports'), \
                patch.object(windows_launcher, 'start_server', side_effect=server), \
                patch.object(config, 'acquire_configuration_mutex', return_value=17), \
                patch.object(config, 'release_configuration_mutex'), \
                patch.object(config, 'configuration_directory', return_value=self.journals), \
                patch.object(config, 'restore_stale_sessions'):
            windows_launcher._run_with_elevated_configuration(installation)
            helpers[0].join(timeout=5)
        self.assertFalse(helpers[0].is_alive())
        self.assertEqual(results, [0])
        self.assertEqual(self.hosts.read_bytes(), self.original_hosts)
        self.assertEqual(self.cacert.read_bytes(), self.original_cacert)
        self.assertFalse(list(self.journals.glob('session-*.json')))

    def test_helper_restores_when_user_connection_disappears(self):
        installation = self.cacert.parent.parent
        helpers = []

        def launch(directory, port, token):
            thread = threading.Thread(
                target=windows_launcher._run_configuration_helper,
                args=(port, token, str(directory)), daemon=True)
            helpers.append(thread)
            thread.start()

        with patch.object(windows_launcher, '_is_elevated', return_value=True), \
                patch.object(windows_launcher, 'run_elevated_copy', side_effect=launch), \
                patch.object(windows_launcher, 'validate_zwift_installation', return_value=installation), \
                patch.object(windows_launcher, '_start_recovery_watcher'), \
                patch.object(windows_launcher, 'preflight_ports'), \
                patch.object(config, 'acquire_configuration_mutex', return_value=17), \
                patch.object(config, 'release_configuration_mutex'), \
                patch.object(config, 'configuration_directory', return_value=self.journals), \
                patch.object(config, 'restore_stale_sessions'):
            connection, channel = windows_launcher._start_elevated_configuration(installation)
            self.assertIn(CERTIFICATE, self.cacert.read_bytes())
            channel.close()
            connection.close()
            helpers[0].join(timeout=5)
        self.assertFalse(helpers[0].is_alive())
        self.assertEqual(self.hosts.read_bytes(), self.original_hosts)
        self.assertEqual(self.cacert.read_bytes(), self.original_cacert)
        self.assertFalse(list(self.journals.glob('session-*.json')))


class LauncherFailureTests(unittest.TestCase):
    def test_normal_server_return_restores_configuration(self):
        calls = []
        with patch.object(windows_launcher.client_config, 'acquire_configuration_mutex', return_value=17), \
                patch.object(windows_launcher.client_config, 'release_configuration_mutex',
                             side_effect=lambda _: calls.append('release')), \
                patch.object(windows_launcher.client_config, 'configuration_directory', return_value=Path('C:/journal')), \
                patch.object(windows_launcher.client_config, 'restore_stale_sessions'), \
                patch.object(windows_launcher, 'preflight_ports'), \
                patch.object(windows_launcher.client_config, 'apply_configuration',
                             return_value=Path('C:/journal/session.json')), \
                patch.object(windows_launcher, '_start_recovery_watcher'), \
                patch.object(windows_launcher, 'start_server', side_effect=lambda _: calls.append('serve')), \
                patch.object(windows_launcher.client_config, 'restore_configuration',
                             side_effect=lambda _: calls.append('restore')):
            windows_launcher._run_configured(Path('C:/Zwift'))
        self.assertEqual(calls, ['serve', 'restore', 'release'])

    def test_server_failure_still_restores_configuration(self):
        calls = []
        with patch.object(windows_launcher.client_config, 'acquire_configuration_mutex', return_value=17), \
                patch.object(windows_launcher.client_config, 'release_configuration_mutex',
                             side_effect=lambda _: calls.append('release')), \
                patch.object(windows_launcher.client_config, 'configuration_directory', return_value=Path('C:/journal')), \
                patch.object(windows_launcher.client_config, 'restore_stale_sessions'), \
                patch.object(windows_launcher, 'preflight_ports'), \
                patch.object(windows_launcher.client_config, 'apply_configuration',
                             return_value=Path('C:/journal/session.json')), \
                patch.object(windows_launcher, '_start_recovery_watcher'), \
                patch.object(windows_launcher, 'start_server', side_effect=RuntimeError('service failed')), \
                patch.object(windows_launcher.client_config, 'restore_configuration',
                             side_effect=lambda _: calls.append('restore')):
            with self.assertRaisesRegex(RuntimeError, 'service failed'):
                windows_launcher._run_configured(Path('C:/Zwift'))
        self.assertEqual(calls, ['restore', 'release'])

    def test_broken_stderr_still_shows_dialog(self):
        with patch('windows_gui.run_gui', side_effect=RuntimeError('failure')), \
                patch.object(windows_launcher.traceback, 'print_exc', side_effect=BrokenPipeError), \
                patch.object(windows_launcher, 'show_error') as dialog:
            self.assertEqual(windows_launcher.main([]), 1)
        self.assertIn('failure', dialog.call_args.args[0])

    def test_port_failure_prevents_client_configuration(self):
        with patch.object(windows_launcher.client_config, 'acquire_configuration_mutex', return_value=17), \
                patch.object(windows_launcher.client_config, 'release_configuration_mutex') as release, \
                patch.object(windows_launcher.client_config, 'configuration_directory', return_value=Path('C:/journal')), \
                patch.object(windows_launcher.client_config, 'restore_stale_sessions') as stale, \
                patch.object(windows_launcher, 'preflight_ports', side_effect=OSError('port occupied')), \
                patch.object(windows_launcher.client_config, 'apply_configuration') as apply:
            with self.assertRaisesRegex(OSError, 'port occupied'):
                windows_launcher._run_configured(Path('C:/Zwift'))
        stale.assert_called_once()
        apply.assert_not_called()
        release.assert_called_once_with(17)


@unittest.skipUnless(os.name == 'nt', 'Windows Crypt32 memory store')
class WindowsCertificateOwnershipTests(unittest.TestCase):
    def test_only_this_session_can_remove_its_root_certificate(self):
        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        crypt.CertOpenStore.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                        ctypes.c_ulong, ctypes.c_void_p]
        crypt.CertOpenStore.restype = ctypes.c_void_p
        crypt.CertDuplicateStore.argtypes = [ctypes.c_void_p]
        crypt.CertDuplicateStore.restype = ctypes.c_void_p
        crypt.CertCloseStore.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        crypt.CertAddEncodedCertificateToStore.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                            ctypes.c_void_p, ctypes.c_ulong,
                                                            ctypes.c_ulong, ctypes.c_void_p]
        crypt.CertAddEncodedCertificateToStore.restype = ctypes.c_int
        store = crypt.CertOpenStore(ctypes.c_void_p(2), 0, None, 0, None)
        self.assertTrue(store)
        _, der = config._cert_der()
        session = 'a' * 32

        def open_memory_store(read_only=False):
            return crypt, crypt.CertDuplicateStore(store)

        try:
            with patch.object(config, '_cert_store', side_effect=open_memory_store):
                self.assertFalse(config.certificate_is_installed(der))
                config.add_certificate(der, session)
                self.assertTrue(config.certificate_is_installed(der))
                config.remove_certificate(der, 'b' * 32)
                self.assertTrue(config.certificate_is_installed(der))
                config.remove_certificate(der, session)
                self.assertFalse(config.certificate_is_installed(der))

                encoded = ctypes.create_string_buffer(der)
                self.assertTrue(crypt.CertAddEncodedCertificateToStore(
                    store, 0x10001, encoded, len(der), 1, None))
                config.remove_certificate(der, session)
                self.assertTrue(config.certificate_is_installed(der))
        finally:
            crypt.CertCloseStore(store, 0)


if __name__ == '__main__':
    unittest.main()
