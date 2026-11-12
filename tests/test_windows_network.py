"""LAN input, discovery, service identity and firewall ownership regressions."""

import base64
import json
import http.server
import http.client
from pathlib import Path
import ssl
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import windows_network as network


class AddressTests(unittest.TestCase):
    def test_ipv4_rejects_non_addresses_and_unsafe_targets(self):
        for value in ('', 'localhost', 'https://192.168.1.2', '192.168.1.2:443',
                      '::1', '0.0.0.0', '224.0.0.1', '255.255.255.255',
                      '127.0.0.1', "1.2.3.4'; Remove-Item x", '240.1.2.3'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                network.validate_server_ip(value)
        self.assertEqual(network.validate_server_ip(' 192.168.1.100 '), '192.168.1.100')
        self.assertEqual(network.validate_server_ip('127.0.0.1', allow_loopback=True), '127.0.0.1')

    def test_discovery_handles_single_nic_and_removes_unusable_addresses(self):
        with patch.object(network, '_powershell', return_value=json.dumps({
                'IPAddress': '192.168.1.100', 'InterfaceAlias': '以太网'})):
            self.assertEqual(network.list_lan_addresses(), [{'ip': '192.168.1.100', 'name': '以太网'}])
        rows = [{'IPAddress': ip, 'InterfaceAlias': 'Ethernet'} for ip in
                ('127.0.0.1', '169.254.1.4', '224.0.0.1', '192.168.1.100', '192.168.1.100')]
        with patch.object(network, '_powershell', return_value=json.dumps(rows)):
            self.assertEqual(network.list_lan_addresses(), [{'ip': '192.168.1.100', 'name': 'Ethernet'}])
        with patch.object(network, '_powershell', return_value=''):
            self.assertEqual(network.list_lan_addresses(), [])

    def test_default_route_interface_is_preferred_to_virtual_adapters(self):
        rows = [{'IPAddress': '172.27.176.1', 'InterfaceAlias': 'vEthernet', 'Preferred': False},
                {'IPAddress': '172.16.12.22', 'InterfaceAlias': '以太网', 'Preferred': True}]
        with patch.object(network, '_powershell', return_value=json.dumps(rows)):
            self.assertEqual(network.list_lan_addresses()[0]['ip'], '172.16.12.22')


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.sockets = patch.object(network.socket, 'create_connection').start()
        self.https = patch.object(network.http.client, 'HTTPSConnection').start()
        self.context = patch.object(network.ssl, 'create_default_context').start()
        self.addCleanup(patch.stopall)
        self.response = self.https.return_value.getresponse.return_value
        self.response.status = 200
        self.response.read.return_value = b'{"service":"zoffline","multiplayer":true}'

    def test_multiplayer_service_check_keeps_tls_verification_and_bounds_io(self):
        message = network.probe_server('192.168.1.100', timeout=1)
        self.assertIn('UDP', message)
        self.assertEqual([call.args[0] for call in self.sockets.call_args_list],
                         [('192.168.1.100', 80), ('192.168.1.100', 443), ('192.168.1.100', 3025)])
        self.https.assert_called_once_with('192.168.1.100', 443, timeout=1, context=self.context.return_value)
        self.context.assert_called_once_with(cafile=str(Path(network.__file__).parent / 'ssl' / 'cert-zwift-com.pem'))
        self.assertIs(self.context.return_value.check_hostname, False)
        self.response.read.assert_called_once_with(16385)
        self.https.return_value.close.assert_called_once()

    def test_closed_tcp_port_does_not_try_https(self):
        self.sockets.side_effect = OSError('refused')
        with self.assertRaisesRegex(RuntimeError, '192.168.1.100:80'):
            network.probe_server('192.168.1.100')
        self.https.assert_not_called()

    def test_wrong_service_singleplayer_oversized_and_bad_json_are_rejected(self):
        for payload in (b'{"service":"different","multiplayer":true}',
                        b'{"service":"zoffline","multiplayer":false}',
                        b'[]', b'not json', b'x' * 16385):
            with self.subTest(payload=payload[:80]), self.assertRaises(RuntimeError):
                self.response.read.return_value = payload
                network.probe_server('192.168.1.100')

    def test_tls_failure_is_reported_and_connection_closed(self):
        self.https.return_value.request.side_effect = ssl.SSLError('untrusted')
        with self.assertRaisesRegex(RuntimeError, '证书'):
            network.probe_server('192.168.1.100')
        self.https.return_value.close.assert_called_once()


class FirewallTests(unittest.TestCase):
    def test_rules_are_scoped_and_restore_matches_session_ownership(self):
        session = 'a' * 32
        with patch.object(network, '_powershell') as run:
            network.add_firewall_rules(session, '192.168.1.100')
            script = run.call_args.args[0]
            self.assertIn('-LocalAddress \'192.168.1.100\'', script)
            self.assertIn('-RemoteAddress LocalSubnet', script)
            self.assertIn('-Profile Private,Domain', script)
            self.assertIn('-LocalPort 80,443,3025', script)
            self.assertIn('-Protocol UDP -LocalPort 3024', script)
            self.assertIn('zoffline-temporary-' + session + '-tcp', script)
            network.remove_firewall_rules(session)
            script = run.call_args.args[0]
            self.assertIn("$_.Name -eq 'zoffline-temporary-" + session + '-tcp\'', script)
            self.assertIn("$_.Description -eq 'zoffline temporary session " + session + "'", script)
            self.assertNotIn('-ErrorAction SilentlyContinue', script)

    def test_malformed_identity_and_ip_never_reach_powershell(self):
        with patch.object(network, '_powershell') as run:
            with self.assertRaises(ValueError):
                network.add_firewall_rules('bad-id', '192.168.1.100')
            with self.assertRaises(ValueError):
                network.add_firewall_rules('a' * 32, '127.0.0.1')
            with self.assertRaises(ValueError):
                network.remove_firewall_rules('*')
            run.assert_not_called()

    def test_powershell_is_hidden_encoded_and_bounded(self):
        with patch.object(network, '_powershell_executable', return_value=Path('C:/Windows/powershell.exe')), \
                patch.object(network.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=0, stdout='done', stderr='')) as run:
            self.assertEqual(network._powershell("Write-Output '中文'", timeout=4), 'done')
        call = run.call_args
        self.assertEqual(call.args[0][0], str(Path('C:/Windows/powershell.exe')))
        self.assertIn('-EncodedCommand', call.args[0])
        self.assertIn("Write-Output '中文'", base64.b64decode(call.args[0][-1]).decode('utf-16-le'))
        self.assertEqual(call.kwargs['timeout'], 4)
        self.assertFalse(call.kwargs.get('shell', False))
        self.assertIn('creationflags', call.kwargs)


class ProbeDeadlineTests(unittest.TestCase):
    def test_slow_https_body_is_bounded_and_cancellable(self):
        class SlowHealth(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', '10000')
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b' ')
                        self.wfile.flush()
                        time.sleep(0.03)
                except OSError:
                    pass

            def log_message(self, *_):
                pass

        root = Path(network.__file__).parent
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), SlowHealth)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(root / 'ssl' / 'cert-zwift-com.pem'),
                                str(root / 'ssl' / 'key-zwift-com.pem'))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
        thread.start()
        actual_https = http.client.HTTPSConnection

        def connect(ip, port, **kwargs):
            return actual_https(ip, server.server_port, **kwargs)

        try:
            with patch.object(network, 'validate_server_ip', return_value='127.0.0.1'), \
                    patch.object(network, 'TCP_PORTS', ()), \
                    patch.object(network, 'http', SimpleNamespace(client=SimpleNamespace(
                        HTTPSConnection=connect, HTTPException=http.client.HTTPException))):
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, '超时'):
                    network.probe_server('127.0.0.1', timeout=0.3)
                self.assertLess(time.monotonic() - started, 1.5)
                stop = threading.Event()
                timer = threading.Timer(0.15, stop.set)
                timer.start()
                started = time.monotonic()
                try:
                    with self.assertRaisesRegex(RuntimeError, '取消'):
                        network.probe_server('127.0.0.1', timeout=2, stop_event=stop)
                finally:
                    timer.cancel()
                self.assertLess(time.monotonic() - started, 1.5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
