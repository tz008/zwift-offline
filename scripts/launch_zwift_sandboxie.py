"""Launch a second Zwift in an existing Sandboxie box; host setup stays in the GUI."""

import argparse
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))

from windows_client_config import DOMAINS, _CERT_PATTERN, _cert_der
from windows_network import probe_server, validate_server_ip
from zwift_installation import find_zwift_installation, validate_zwift_installation


INSTALL_URL = 'https://github.com/sandboxie-plus/Sandboxie/releases/latest'


def find_sandboxie(directory=None):
    if directory:
        candidates = [Path(directory).expanduser().resolve()]
    else:
        located = shutil.which('SbieIni.exe')
        candidates = [Path(located).parent] if located else []
        for variable in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)'):
            if os.environ.get(variable):
                for name in ('Sandboxie-Plus', 'Sandboxie'):
                    candidates.append(Path(os.environ[variable]) / name)
    for candidate in candidates:
        start, ini = candidate / 'Start.exe', candidate / 'SbieIni.exe'
        if start.is_file() and ini.is_file():
            return start, ini
    detail = '指定目录缺少 Start.exe 或 SbieIni.exe。' if directory else '未找到 Sandboxie Plus。'
    raise RuntimeError(detail + '\n请安装后重试，或指定 --sandboxie-dir。\n' + INSTALL_URL)


def decode_output(data):
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    if b'\x00' in data:
        return data.decode('utf-16-le')
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode('mbcs' if os.name == 'nt' else 'utf-8', errors='replace')


def query_tool(command):
    result = subprocess.run(command, capture_output=True, timeout=15,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        detail = decode_output(result.stderr or result.stdout).strip()
        raise RuntimeError('Sandboxie 查询失败（退出码 %s）：%s' % (result.returncode, detail))
    return decode_output(result.stdout)


def check_box(start, ini, box):
    available = query_tool([str(ini), 'query', '/boxes', '*']).splitlines()
    if box.lower() not in {line.strip().lower() for line in available}:
        raise RuntimeError('沙箱 %s 不存在、未启用或当前用户不可访问。\n'
                           '请在 Sandboxie Plus 中新建同名的专用标准沙箱。' % box)
    lines = query_tool([str(start), '/silent', '/box:' + box, '/listpids']).splitlines()
    if (not lines or any(not re.fullmatch(r'[0-9]+', line) for line in lines)
            or int(lines[0]) != len(lines) - 1
            or any(int(line) <= 0 for line in lines[1:])):
        raise RuntimeError('无法读取沙箱进程列表；请确认 Sandboxie 服务/驱动正常运行。')
    if int(lines[0]):
        raise RuntimeError('沙箱 %s 已有 %s 个进程，请先正常退出该沙箱中的程序。'
                           % (box, lines[0]))


def check_domains(server_ip):
    for value in DOMAINS:
        domain = value.decode('ascii')
        try:
            addresses = {entry[4][0] for entry in socket.getaddrinfo(
                domain, None, socket.AF_INET, socket.SOCK_STREAM)}
        except OSError as exc:
            raise RuntimeError('无法解析 %s；请先通过现有 GUI 启动多人服务。' % domain) from exc
        if addresses != {server_ip}:
            raise RuntimeError('%s 当前指向 %s，预期 %s。\n'
                               '请在现有 GUI 选择“创建局域网”，勾选“本机也参与骑行”并保持运行。'
                               % (domain, ', '.join(sorted(addresses)) or '无地址', server_ip))


def check_client_certificate(installation):
    _, expected = _cert_der()
    content = (installation / 'data' / 'cacert.pem').read_bytes()
    for match in _CERT_PATTERN.finditer(content):
        try:
            if ssl.PEM_cert_to_DER_cert(match.group(0).decode('ascii')) == expected:
                return
        except (UnicodeError, ValueError):
            continue
    raise RuntimeError('所选 Zwift 的 data/cacert.pem 未包含离线服务证书。\n'
                       '请确认现有 GUI 配置的是同一安装目录，再启动本机骑行。')


def main(arguments=None):
    parser = argparse.ArgumentParser(description='复用已运行的多人服务，在专用沙箱中启动第二份 Zwift。')
    parser.add_argument('--server-ip', required=True, help='现有 GUI 创建局域网时显示的服务器 IPv4')
    parser.add_argument('--box', default='ZofflineZwift2', help='预先创建的标准沙箱名，默认 ZofflineZwift2')
    parser.add_argument('--sandboxie-dir', help='包含 Start.exe 与 SbieIni.exe 的 Sandboxie 安装目录')
    parser.add_argument('--zwift-dir', help='Zwift 安装目录；默认复用项目的自动发现')
    parser.add_argument('--check', action='store_true', help='只检查条件并展示命令，不启动游戏')
    options = parser.parse_args(arguments)
    try:
        if os.name != 'nt':
            raise RuntimeError('此试验入口仅支持 Windows。')
        server_ip = validate_server_ip(options.server_ip)
        if not re.fullmatch(r'[A-Za-z0-9_]{1,32}', options.box):
            raise RuntimeError('沙箱名需为 1–32 位英文字母、数字或下划线。')
        try:
            installation = (validate_zwift_installation(options.zwift_dir)
                            if options.zwift_dir else find_zwift_installation(ROOT))
        except (FileNotFoundError, NotADirectoryError):
            installation = None
        if installation is None:
            raise RuntimeError('未找到完整 Zwift 安装，请安装或指定 --zwift-dir；'
                               '目录需包含 ZwiftLauncher.exe、ZwiftApp.exe 和 data/cacert.pem。')
        print('Zwift：%s' % installation, flush=True)
        start, ini = find_sandboxie(options.sandboxie_dir)
        print('Sandboxie：%s\n沙箱：%s' % (start.parent, options.box), flush=True)
        check_box(start, ini, options.box)
        check_domains(server_ip)
        check_client_certificate(installation)
        print(probe_server(server_ip), flush=True)
        command = [str(start), '/box:' + options.box, str(installation / 'ZwiftLauncher.exe')]
        print('启动命令：' + subprocess.list2cmdline(command), flush=True)
        if options.check:
            print('检查通过；尚未启动第二份游戏。', flush=True)
            return 0
        # Start.exe returns after dispatch; its success is not proof of a running game.
        result = subprocess.run(command, cwd=str(installation), timeout=20)
        if result.returncode:
            raise RuntimeError('Sandboxie 启动请求失败（退出码 %s），请查看 Sandboxie 提示。'
                               % result.returncode)
        print('已发送沙箱启动请求。请在新窗口登录不同的本地账号；实际双开和进图需确认。', flush=True)
        return 0
    except subprocess.TimeoutExpired:
        print('操作超时，请检查 Sandboxie 状态；如已出现游戏窗口，请勿重复启动。', file=sys.stderr)
    except (OSError, RuntimeError, ValueError) as exc:
        print('双开试验未完成：%s' % exc, file=sys.stderr)
    return 1


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    raise SystemExit(main())
