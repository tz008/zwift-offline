"""Chaquopy entry point; run once in Android's dedicated ``:offline`` process.

The Android service invokes run on a background thread, polls status from its
main thread, and calls stop before ending its own isolated process. No phone
or Zwift private files are modified by this module.
"""

import json
import logging
import os
from pathlib import Path
import runpy
import socket
import threading
import time
import traceback


_lock = threading.RLock()
_stop_event = threading.Event()
_stopped_event = threading.Event()
_servers = []
_state = {'state': 'stopped', 'message': '', 'client_seen': False, 'game_login': False}
_used = False


def status():
    with _lock:
        return json.dumps(_state, ensure_ascii=False)


def _update(state, message=''):
    with _lock:
        _state.update(state=state, message=message)


def _register(server):
    with _lock:
        _servers.append(server)


def _shutdown_servers():
    with _lock:
        servers = list(reversed(_servers))
    for server in servers:
        try:
            if hasattr(server, 'stop'):
                server.stop()
            else:
                server.shutdown()
                server.server_close()
        except Exception:
            logging.exception('Android service listener shutdown failed')
    _stopped_event.set()


def stop():
    """Request shutdown without blocking the Android service's main thread."""
    with _lock:
        if _stop_event.is_set():
            return
        _stop_event.set()
        if _state['state'] != 'error':
            _update('stopping', '正在停止服务')
    threading.Thread(target=_shutdown_servers, name='zoffline-stop', daemon=True).start()


def run(resource_dir, storage_dir, logs_dir, ports=None):
    """Start solo mode, blocking until stopped; optional ports support host tests."""
    global _used
    with _lock:
        if _used:
            raise RuntimeError('Android runtime must restart in a fresh service process')
        _used = True
        if _stop_event.is_set():
            _update('stopped')
            return
        _update('starting', '正在启动本机服务')
    try:
        resources = Path(str(resource_dir)).resolve()
        storage = Path(str(storage_dir)).resolve()
        logs = Path(str(logs_dir)).resolve()
        storage.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        if not (resources / 'cdn' / 'gameassets' / 'Zwift_Updates_Root' / 'Zwift_ver_cur.xml').is_file():
            raise RuntimeError('APK 内的服务资源不完整')
        listeners = {'cdn': 8080, 'api': 8443, 'management': 8081, 'tcp': 3025, 'udp': 3024}
        if ports:
            listeners.update(ports)
        environment = {
            'ZOFFLINE_RESOURCE_DIR': str(resources),
            'ZOFFLINE_STORAGE_DIR': str(storage),
            'ZOFFLINE_LOGS_DIR': str(logs),
            'ZOFFLINE_SERVER_HOST': '127.0.0.1',
            'ZOFFLINE_SERVER_IP': '198.18.0.1',
            'ZOFFLINE_ADVERTISED_IP': '198.18.0.1',
            'ZOFFLINE_MULTIPLAYER': 'false',
            'ZOFFLINE_WSGI_BACKEND': 'embedded',
            'ZOFFLINE_DISABLE_CDN_PROXY': 'true',
            'ZOFFLINE_DISABLE_FAKE_DNS': 'true',
            'ZOFFLINE_API_USE_CERT': 'true',
        }
        for name, port in listeners.items():
            environment['ZOFFLINE_%s_PORT' % name.upper()] = str(port)
        os.environ.update(environment)
        file_handler = logging.FileHandler(logs / 'android-server.log', encoding='utf-8')
        file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
        logging.getLogger().addHandler(file_handler)

        # Fail before importing the server (which creates DB tables and workers).
        sockets = []
        try:
            for name, port in listeners.items():
                kind = socket.SOCK_DGRAM if name == 'udp' else socket.SOCK_STREAM
                sock = socket.socket(socket.AF_INET, kind)
                sockets.append(sock)
                sock.bind(('127.0.0.1', int(port)))
        finally:
            for sock in sockets:
                sock.close()
        import zwift_offline as zo
        zo.server_stop_event = _stop_event
        zo.server_created_callback = _register

        @zo.app.after_request
        def record_client_response(response):
            # The management WebView uses a separate port. Only an actual
            # successful game login proves Zwift accepted TLS and credentials.
            from flask import request
            if int(request.environ.get('SERVER_PORT', 0)) == listeners['api']:
                with _lock:
                    _state['client_seen'] = True
                    if request.path == '/api/users/login' and response.status_code == 200:
                        _state['game_login'] = True
            return response

        def ready():
            if _stop_event.is_set():
                # Shutdown may have arrived while imports/DB setup were running.
                threading.Thread(target=_shutdown_servers, daemon=True).start()
            else:
                _update('ready', '本机服务已就绪')

        runpy.run_module('standalone', run_name='__main__', init_globals={
            'on_server_ready': ready,
            'on_server_created': _register,
            'server_stop_event': _stop_event,
        })
        if _state['state'] != 'error':
            _update('stopped', '服务已停止')
    except BaseException as exc:
        _update('error', str(exc))
        logging.error('Android runtime failed:\n%s', traceback.format_exc())
        stop()
    finally:
        _stop_event.set()
        _shutdown_servers()
