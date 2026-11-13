"""Small threaded WSGI adapter for platforms without gevent (Android).

The interface matches the subset of gevent's WSGIServer used by zoffline.
TLS handshakes happen on request threads, so a slow handshake cannot block
new game connections. Only Python's standard library is needed.
"""

import socket
import socketserver
import ssl
import threading
import time
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer as BaseServer


class _RequestHandler(WSGIRequestHandler):
    def get_environ(self):
        environ = super().get_environ()
        if self.server.tls_context is not None:
            environ['HTTPS'] = 'on'
        return environ

    def log_message(self, format_string, *args):
        logger = self.server.request_logger
        if logger is not None:
            logger.info('%s - %s', self.address_string(), format_string % args)


class _ThreadedServer(socketserver.ThreadingMixIn, BaseServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 64

    def process_request_thread(self, request, client_address):
        tracked_request = request
        with self.requests_lock:
            self.requests.add(request)
        try:
            request.settimeout(15)
            if self.tls_context is not None:
                request = self.tls_context.wrap_socket(request, server_side=True,
                                                      do_handshake_on_connect=False)
                with self.requests_lock:
                    self.requests.discard(tracked_request)
                    self.requests.add(request)
                tracked_request = request
                request.do_handshake()
            # zoffline handlers consume a request body using Content-Length;
            # wsgiref deliberately closes each HTTP response (HTTP/1.0).
            self.finish_request(request, client_address)
        except (ssl.SSLError, ConnectionError, TimeoutError, OSError):
            pass  # A closed or untrusted game connection is not a server crash.
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            with self.requests_lock:
                self.requests.discard(tracked_request)


class WSGIServer:
    def __init__(self, listener, application, log=None, ssl_context=None):
        self.listener = listener
        self.application = application
        self.log = log
        self.ssl_context = ssl_context
        self._server = None
        self._thread = None
        self._stop_lock = threading.Lock()

    def start(self):
        server_class = _ThreadedServer
        if ':' in self.listener[0]:
            class IPv6Server(_ThreadedServer):
                address_family = socket.AF_INET6
            server_class = IPv6Server
        server = server_class(self.listener, _RequestHandler)
        server.tls_context = self.ssl_context
        server.request_logger = self.log
        server.requests = set()
        server.requests_lock = threading.Lock()
        server.set_app(self.application)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever,
                                        kwargs={'poll_interval': 0.1}, daemon=True,
                                        name='zoffline-wsgi')
        self._thread.start()

    def serve_forever(self):
        if self._thread is None:
            self.start()
        self._thread.join()

    def stop(self):
        with self._stop_lock:
            if self._server is None:
                return
            server, self._server = self._server, None
            server.shutdown()
            server.server_close()
            # Let short activity writes finish before the service process exits.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with server.requests_lock:
                    if not server.requests:
                        return
                time.sleep(0.02)
            with server.requests_lock:
                requests = list(server.requests)
            for request in requests:
                try:
                    request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                request.close()
