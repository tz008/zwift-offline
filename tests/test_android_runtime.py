"""Host regressions for the actual embedded Android HTTP/TLS/relay backend."""

import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class EmbeddedAndroidTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Use a fresh process exactly like Android's isolated service. This also
        # prevents module globals, environment changes and test data leaking into
        # desktop startup tests or the developer's storage directory.
        cls.temp = tempfile.TemporaryDirectory(prefix='zoffline-android-')
        cls.directory = Path(cls.temp.name)
        cls.status_file = cls.directory / 'status.json'
        cls.stop_file = cls.directory / 'stop'
        cls.ports = {}
        reserved = []
        for name in ('api', 'cdn', 'management', 'tcp', 'udp'):
            kind = socket.SOCK_DGRAM if name == 'udp' else socket.SOCK_STREAM
            sock = socket.socket(socket.AF_INET, kind)
            sock.bind(('127.0.0.1', 0))
            cls.ports[name] = sock.getsockname()[1]
            reserved.append(sock)
        for sock in reserved:
            sock.close()
        script = r'''
import json, os, pathlib, sys, threading, time
import android_runtime
directory = pathlib.Path(sys.argv[1])
worker = threading.Thread(target=android_runtime.run, args=(sys.argv[2], str(directory/'storage'), str(directory/'logs'), json.loads(sys.argv[3])))
worker.start()
while worker.is_alive():
    (directory/'status.json.tmp').write_text(android_runtime.status(), encoding='utf-8')
    os.replace(str(directory/'status.json.tmp'), str(directory/'status.json'))
    if (directory/'stop').exists():
        android_runtime.stop()
    worker.join(0.05)
(directory/'status.json').write_text(android_runtime.status(), encoding='utf-8')
assert 'gevent' not in sys.modules, 'Embedded backend unexpectedly imports gevent'
'''
        cls.output = open(cls.directory / 'process.log', 'wb')
        cls.process = subprocess.Popen([sys.executable, '-c', script, str(cls.directory),
                                        str(ROOT), json.dumps(cls.ports)], cwd=ROOT,
                                       stdout=cls.output, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            state = cls.read_status()
            if state.get('state') == 'ready':
                return
            if state.get('state') == 'error' or cls.process.poll() is not None:
                break
            time.sleep(0.05)
        cls.process.kill()
        cls.process.wait(timeout=5)
        cls.output.close()
        details = (cls.directory / 'process.log').read_text(encoding='utf-8', errors='replace')
        cls.temp.cleanup()
        raise AssertionError('Android backend did not become ready: %s\n%s' % (state, details))

    @classmethod
    def read_status(cls):
        try:
            return json.loads(cls.status_file.read_text(encoding='utf-8'))
        except (FileNotFoundError, PermissionError, ValueError):
            return {}

    @classmethod
    def tearDownClass(cls):
        cls.stop_file.write_text('stop', encoding='utf-8')
        try:
            cls.process.wait(timeout=8)
            if cls.process.returncode != 0:
                raise AssertionError((cls.directory / 'process.log').read_text(encoding='utf-8', errors='replace'))
            if cls.read_status().get('state') != 'stopped':
                raise AssertionError('Runtime failed to stop: %s' % cls.read_status())
            # Stop must release listeners so the next service process can bind.
            for name, port in cls.ports.items():
                kind = socket.SOCK_DGRAM if name == 'udp' else socket.SOCK_STREAM
                with socket.socket(socket.AF_INET, kind) as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind(('127.0.0.1', port))
        finally:
            if cls.process.poll() is None:
                cls.process.kill()
                cls.process.wait(timeout=5)
            cls.output.close()
            cls.temp.cleanup()

    def request(self, path, *, method='GET', body=None, headers=None, listener='api'):
        if listener == 'api':
            context = ssl.create_default_context(cafile=str(ROOT / 'ssl' / 'cert-zwift-com.pem'))
            # Trust the repository CA while preserving validation of the game
            # domain. HTTPConnection normally uses its socket address for SNI.
            connection = http.client.HTTPSConnection('secure.zwift.com', self.ports['api'],
                                                     context=context, timeout=5)
            connection._create_connection = lambda address, timeout, source_address=None: socket.create_connection(
                ('127.0.0.1', self.ports['api']), timeout=timeout)
        else:
            connection = http.client.HTTPConnection('127.0.0.1', self.ports[listener], timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_https_health_and_private_database(self):
        status, _, body = self.request('/api/zoffline/health')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'service': 'zoffline', 'multiplayer': False})
        self.assertTrue((self.directory / 'storage' / 'zwift-offline.db').is_file())
        self.assertTrue((self.directory / 'logs' / 'android-server.log').is_file())

    def test_management_and_cdn_serve_bundled_resources(self):
        status, _, body = self.request('/user/zoffline/', listener='management')
        self.assertEqual(status, 200)
        self.assertIn(b'<html', body)
        status, _, body = self.request('/gameassets/MapSchedule_v2.xml', listener='cdn')
        self.assertEqual(status, 200)
        self.assertIn(b'<MapSchedule', body)

    def test_concurrent_https_requests_and_stalled_handshake(self):
        # An incomplete handshake must not hold the single accept loop hostage.
        with socket.create_connection(('127.0.0.1', self.ports['api']), timeout=2):
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as workers:
                futures = [workers.submit(self.request, '/api/zoffline/health') for _ in range(12)]
                self.assertTrue(all(future.result()[0] == 200 for future in futures))

    def test_auth_login_returns_virtual_relay_address(self):
        sys.path.insert(0, str(ROOT / 'protobuf'))
        import login_pb2
        import per_session_info_pb2
        request = login_pb2.LoginRequest()
        request.key = b'a' * 16
        status, _, body = self.request('/api/users/login', method='POST', body=request.SerializeToString(),
                                       headers={'User-Agent': 'Zwift Android', 'Content-Type': 'application/x-protobuf-lite'})
        self.assertEqual(status, 200)
        response = login_pb2.LoginResponse()
        response.ParseFromString(body)
        self.assertEqual(response.info.nodes.nodes[0].ip, '198.18.0.1')
        status, _, body = self.request('/api/tcp-config')
        self.assertEqual(status, 200)
        config = per_session_info_pb2.TcpConfig()
        config.ParseFromString(body)
        self.assertEqual(config.nodes[0].ip, '198.18.0.1')
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not self.read_status().get('game_login'):
            time.sleep(0.05)
        self.assertTrue(self.read_status().get('game_login'))


if __name__ == '__main__':
    unittest.main()
