"""Windows LAN discovery, bounded connection checks and session firewall rules."""

import base64
import ctypes
from ctypes import wintypes
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import threading
import time


TCP_PORTS = (80, 443, 3025)
UDP_PORT = 3024


def validate_server_ip(value, allow_loopback=False):
    """Accept one explicit IPv4 address, never a hostname, URL or command."""
    try:
        address = ipaddress.IPv4Address(str(value).strip())
    except ipaddress.AddressValueError as exc:
        raise ValueError('请输入有效的 IPv4 地址，例如 192.168.1.100。') from exc
    if (address.is_unspecified or address.is_multicast or address.is_reserved or
            int(address) == 0xffffffff or (address.is_loopback and not allow_loopback)):
        raise ValueError('请填写可供其他电脑连接的 IPv4 地址。')
    return str(address)


def _powershell_executable():
    if os.name != 'nt':
        raise RuntimeError('局域网自动配置仅支持 Windows。')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    buffer = ctypes.create_unicode_buffer(32768)
    result = kernel.GetWindowsDirectoryW(buffer, len(buffer))
    if not result or result >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(buffer.value) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'


def _powershell(script, timeout=30):
    # Encode scripts as UTF-16LE instead of relying on cmd.exe quoting or the
    # machine's default console code page. No shell or PATH lookup is involved.
    script = ("$ErrorActionPreference = 'Stop'; "
              "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); " + script)
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    try:
        result = subprocess.run(
            [str(_powershell_executable()), '-NoLogo', '-NoProfile', '-NonInteractive',
             '-EncodedCommand', encoded], capture_output=True, encoding='utf-8',
            errors='replace', timeout=timeout,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('Windows 网络配置响应超时，请重试。') from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError('Windows 网络配置失败：%s' % (detail or result.returncode))
    return result.stdout.strip().lstrip('\ufeff')


def list_lan_addresses():
    """Return active NIC IPv4 addresses, including VPN adapters when present."""
    output = _powershell(
        "$connected = @(Get-NetIPInterface -AddressFamily IPv4 | "
        "Where-Object { $_.ConnectionState -eq 'Connected' } | "
        "Select-Object -ExpandProperty InterfaceIndex); "
        "$route = Get-NetRoute -AddressFamily IPv4 | "
        "Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' } | "
        "Sort-Object RouteMetric | Select-Object -First 1; "
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object { $_.AddressState -eq 'Preferred' -and $_.InterfaceIndex -in $connected } | "
        "Select-Object IPAddress, InterfaceAlias, "
        "@{Name='Preferred';Expression={ $_.InterfaceIndex -eq $route.InterfaceIndex }} | "
        "ConvertTo-Json -Compress", timeout=15)
    if not output:
        return []
    try:
        rows = json.loads(output)
    except ValueError as exc:
        raise RuntimeError('无法读取 Windows 网卡地址。') from exc
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        raise RuntimeError('Windows 返回了无效的网卡信息。')
    addresses = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            ip = validate_server_ip(row.get('IPAddress', ''))
        except ValueError:
            continue
        if ipaddress.IPv4Address(ip).is_link_local:
            continue
        if ip not in seen:
            addresses.append({'ip': ip, 'name': str(row.get('InterfaceAlias') or '网络适配器'),
                              '_preferred': row.get('Preferred') is True})
            seen.add(ip)
    addresses.sort(key=lambda item: (not item['_preferred'], item['name'].casefold(),
                                      int(ipaddress.IPv4Address(item['ip']))))
    return [{'ip': item['ip'], 'name': item['name']} for item in addresses]


def probe_server(ip, timeout=3, stop_event=None):
    """Verify TCP, the bundled TLS trust chain and the multiplayer service."""
    ip = validate_server_ip(ip)
    deadline = time.monotonic() + timeout * (len(TCP_PORTS) + 1)

    def check_deadline():
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError('连接检测已取消。')
        if time.monotonic() >= deadline:
            raise RuntimeError('服务器连接检测超时，请重试。')

    for port in TCP_PORTS:
        check_deadline()
        try:
            with socket.create_connection((ip, port), timeout=min(timeout, deadline - time.monotonic())):
                pass
        except OSError as exc:
            raise RuntimeError('无法连接 %s:%s。请确认服务器已开启且防火墙允许连接。' %
                               (ip, port)) from exc
    resource_root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    context = ssl.create_default_context(cafile=str(resource_root / 'ssl' / 'cert-zwift-com.pem'))
    # The application serves Zwift domain names; the user selects a numeric LAN
    # address. Keep chain verification enabled, using the certificate we ship.
    context.check_hostname = False
    connection = http.client.HTTPSConnection(ip, 443, timeout=timeout, context=context)
    finished = threading.Event()
    transport = []

    def interrupt_slow_response():
        # A socket inactivity timeout alone cannot bound a peer that drips
        # headers/body forever. Shutdown also wakes a currently blocked SSL read.
        while not finished.wait(0.05):
            if (time.monotonic() >= deadline or
                    (stop_event is not None and stop_event.is_set())):
                for sock in transport:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                return

    watcher = threading.Thread(target=interrupt_slow_response, daemon=True)
    watcher.start()
    try:
        check_deadline()
        connection.connect()
        transport.append(connection.sock)
        check_deadline()
        connection.request('GET', '/api/zoffline/health', headers={'Host': 'launcher.zwift.com'})
        response = connection.getresponse()
        payload = response.read(16385)
        if response.status != 200 or len(payload) > 16384:
            raise RuntimeError('该地址未返回可识别的 zoffline 服务。')
        try:
            health = json.loads(payload)
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError('该地址未返回可识别的 zoffline 服务。') from exc
        if not isinstance(health, dict) or health.get('service') != 'zoffline':
            raise RuntimeError('该地址不是 zoffline 服务器。')
        if health.get('multiplayer') is not True:
            raise RuntimeError('该服务器尚未开启多人模式，请让创建者选择“创建局域网”。')
        check_deadline()
    except Exception as exc:
        check_deadline()
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError('无法验证 zoffline 服务器，请检查地址和服务端证书。') from exc
    finally:
        finished.set()
        connection.close()
        watcher.join(timeout=0.2)
    return '服务器 TCP 连接正常；UDP 骑行同步需进入游戏确认。'


def _firewall_identity(session_id):
    if not isinstance(session_id, str) or not re.fullmatch(r'[0-9a-f]{32}', session_id):
        raise ValueError('防火墙配置会话标识无效。')
    return ('zoffline-temporary-%s' % session_id,
            'zoffline temporary session %s' % session_id)


def add_firewall_rules(session_id, server_ip):
    """Call only after journaling the attempt, since either rule can succeed."""
    server_ip = validate_server_ip(server_ip)
    prefix, description = _firewall_identity(session_id)
    commands = []
    for suffix, protocol, ports in (('tcp', 'TCP', '80,443,3025'), ('udp', 'UDP', '3024')):
        name = '%s-%s' % (prefix, suffix)
        commands.append(
            "New-NetFirewallRule -Name '%s' -DisplayName '%s' -Description '%s' "
            "-Direction Inbound -Action Allow -Protocol %s -LocalPort %s "
            "-LocalAddress '%s' -RemoteAddress LocalSubnet -Profile Private,Domain "
            "-PolicyStore PersistentStore -Enabled True | Out-Null" %
            (name, name, description, protocol, ports, server_ip))
    _powershell('; '.join(commands))


def remove_firewall_rules(session_id):
    """Idempotently remove only rules marked as belonging to this session."""
    prefix, description = _firewall_identity(session_id)
    # Enumerate the store so an absent rule is an ordinary empty result while
    # actual policy/permission errors still fail and preserve the recovery log.
    _powershell(
        "Get-NetFirewallRule -PolicyStore PersistentStore | "
        "Where-Object { ($_.Name -eq '%s-tcp' -or $_.Name -eq '%s-udp') "
        "-and $_.Description -eq '%s' } | Remove-NetFirewallRule" %
        (prefix, prefix, description))
