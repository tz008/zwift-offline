"""Prepare Zwift's OBB trust bundle without extracting or deleting player data.

This is an independent implementation using Python's standard library. Unchanged
ZIP members retain their compressed bytes; directory records provide room for an
equal-length replacement. The original OBB is retained beside the patched file.
"""

from copy import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import zipfile


TARGET = 'dataES/cacert.pem'
PEM = re.compile(rb'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', re.S)


def _hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _bundle(existing, certificate):
    blocks = PEM.findall(certificate)
    if not blocks:
        raise ValueError('APK 中未找到有效的证书内容')
    normalized = existing.replace(b'\r\n', b'\n')
    eol = b'\r\n' if b'\r\n' in existing else b'\n'
    result = existing
    for block in blocks:
        block = block.replace(b'\r\n', b'\n')
        if block not in normalized:
            result = result.rstrip(b'\r\n') + eol + block.replace(b'\n', eol) + eol
            normalized += b'\n' + block
    return result


def _recover(path, journal):
    if not journal.exists():
        return
    record = json.loads(journal.read_text(encoding='utf-8'))
    backup = Path(record['backup'])
    if (Path(record['path']) != path or backup.parent != path.parent or
            not backup.name.startswith(path.name + '.zoffline-original-')):
        raise ValueError('补丁恢复记录与当前资源不匹配，请保留记录并检查文件')
    if not path.exists():
        if not backup.is_file() or _hash(backup) != record['original_sha256']:
            raise ValueError('上次操作中断，原始资源备份无法校验；请重新下载游戏资源')
        os.replace(backup, path)
    elif record.get('state') == 'prepared':
        actual = _hash(path)
        if actual not in (record['original_sha256'], record['patched_sha256']):
            raise ValueError('资源在上次补丁后已被其他程序修改，已保留备份，请重新检查')


def _build(source_path, destination, certificate, progress):
    with zipfile.ZipFile(source_path) as original:
        infos = sorted(original.infolist(), key=lambda item: item.header_offset)
        if len({item.filename for item in infos}) != len(infos):
            raise ValueError('资源包存在重复文件，无法安全修补')
        cert_info = original.getinfo(TARGET)
        if cert_info.file_size > 2 * 1024 * 1024:
            raise ValueError('游戏证书文件大小异常')
        existing = original.read(cert_info)
        updated_bundle = _bundle(existing, certificate)
        if existing == updated_bundle:
            return False
        if (len(infos) >= zipfile.ZIP_FILECOUNT_LIMIT or
                any(item.flag_bits & 1 or item.file_size >= zipfile.ZIP64_LIMIT or
                    item.compress_size >= zipfile.ZIP64_LIMIT or item.header_offset < 0
                    for item in infos)):
            raise ValueError('该资源包使用暂不支持的加密或 ZIP64 格式')
        report('正在制作证书补丁，其他游戏文件保持原内容', progress)
        with source_path.open('rb') as source, zipfile.ZipFile(destination, 'w', allowZip64=False) as patched:
            patched.comment = original.comment
            for index, info in enumerate(infos):
                if info.is_dir():
                    continue
                updated = copy(info)
                if info.filename == TARGET:
                    patched.writestr(updated, updated_bundle, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
                    continue
                end = infos[index + 1].header_offset if index + 1 < len(infos) else original.start_dir
                if end <= info.header_offset or end > original.start_dir:
                    raise ValueError('资源包文件位置异常')
                source.seek(info.header_offset)
                patched.fp.seek(patched.start_dir)
                updated.header_offset = patched.fp.tell()
                patched._writecheck(updated)
                remaining = end - info.header_offset
                while remaining:
                    block = source.read(min(remaining, 1024 * 1024))
                    if not block:
                        raise ValueError('读取资源包时遇到不完整文件')
                    patched.fp.write(block)
                    remaining -= len(block)
                patched.start_dir = patched.fp.tell()
                patched.filelist.append(updated)
                patched.NameToInfo[updated.filename] = updated
                patched._didModify = True
            central_size = sum(46 + len(item._encodeFilenameFlags()[0]) + len(item.extra) + len(item.comment)
                               for item in patched.filelist)
            padding = source_path.stat().st_size - (patched.start_dir + central_size + 22 + len(patched.comment))
            if padding < 0:
                raise ValueError('该资源包没有足够的补丁空间，原文件未改动；请使用电脑补丁工具')
            while padding:
                block = b'\0' * min(padding, 65536)
                patched.fp.write(block)
                padding -= len(block)
            patched.start_dir = patched.fp.tell()
        report('正在校验全部游戏文件，请稍候', progress)
        if destination.stat().st_size != source_path.stat().st_size:
            raise ValueError('补丁资源包长度不匹配')
        with zipfile.ZipFile(destination) as patched:
            expected = {item.filename: item for item in infos if not item.is_dir()}
            if set(patched.namelist()) != set(expected):
                raise ValueError('补丁资源文件列表不匹配')
            for info in patched.infolist():
                before = expected[info.filename]
                if info.filename != TARGET and (info.CRC, info.file_size, info.compress_size) != (
                        before.CRC, before.file_size, before.compress_size):
                    raise ValueError('其他游戏资源发生变化：' + info.filename)
            if patched.read(TARGET) != updated_bundle:
                raise ValueError('补丁证书校验失败')
            bad = patched.testzip()
            if bad is not None:
                raise ValueError('游戏文件完整性校验失败：' + bad)
    with destination.open('r+b') as stream:
        os.fsync(stream.fileno())
    return True


def report(message, callback):
    if callback is not None:
        callback.report(message)


def prepare(obb_path, certificate_path, state_directory, version_code, callback=None):
    """Return JSON for the Android UI. Requires the caller's exclusive file lock.

    Does not stop Zwift, clear its data, or modify its extracted private files.
    An interrupted rename is recovered before checking the next requested patch.
    """
    path = Path(obb_path).absolute()
    version = str(version_code)
    if not version.isdigit() or path.name != 'main.' + version + '.com.zwift.zwiftgame.obb':
        raise ValueError('资源包版本与已安装的 Zwift 不匹配')
    if path.is_symlink():
        raise ValueError('不支持符号链接资源包')
    state = Path(state_directory)
    state.mkdir(parents=True, exist_ok=True)
    journal = state / (version + '.json')
    report('正在检查当前 Zwift 版本的资源和证书', callback)
    _recover(path, journal)
    if not path.is_file():
        raise FileNotFoundError('无法访问当前版本的 OBB。请检查文件权限，并确认游戏资源已完整下载')
    certificate = Path(certificate_path).read_bytes()
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(TARGET)
        if info.file_size > 2 * 1024 * 1024:
            raise ValueError('游戏证书文件大小异常')
        current = archive.read(info)
        if _bundle(current, certificate) == current:
            return json.dumps({'state': 'ready', 'changed': False, 'version_code': version,
                               'message': '当前版本资源包已包含离线证书', 'path': str(path)}, ensure_ascii=False)
    if shutil.disk_usage(path.parent).free < path.stat().st_size + 16 * 1024 * 1024:
        raise ValueError('可用空间不足：需要额外约一个 OBB 大小来制作并校验补丁')
    try:
        with path.open('r+b'):
            pass
    except PermissionError as error:
        raise PermissionError('系统拒绝修改 OBB。请授权后重新打开 zoffline；若资源由电脑导入，请覆盖安装同版本 Zwift 修正文件权限后重试') from error
    stage = path.with_name(path.name + '.zoffline-patch.tmp')
    fingerprint = path.stat()
    original_hash = _hash(path)
    backup = path.with_name(path.name + '.zoffline-original-' + original_hash[:12])
    if backup.exists() and _hash(backup) != original_hash:
        raise ValueError('已有备份校验不匹配，已保留所有原文件')
    try:
        changed = _build(path, stage, certificate, callback)
        if not changed:
            raise ValueError('检查期间游戏资源已变化，请重新检查')
        after = path.stat()
        if ((after.st_size, after.st_mtime_ns, after.st_ino) !=
                (fingerprint.st_size, fingerprint.st_mtime_ns, fingerprint.st_ino) or _hash(path) != original_hash):
            raise ValueError('补丁期间游戏资源已更新，原文件未替换；请重新检查')
        patched_hash = _hash(stage)
        record = {'state': 'prepared', 'path': str(path), 'backup': str(backup),
                  'original_sha256': original_hash, 'patched_sha256': patched_hash,
                  'version_code': version, 'size': fingerprint.st_size}
        _write_json(journal, record)
        report('校验通过，正在保留原始备份并应用补丁', callback)
        moved = False
        try:
            if not backup.exists():
                os.replace(path, backup)
                moved = True
            os.replace(stage, path)
        except BaseException:
            if moved and not path.exists():
                os.replace(backup, path)
            raise
        if _hash(path) != patched_hash:
            raise ValueError('最终资源校验失败，已保留原始备份；请重新检查')
        record['state'] = 'complete'
        _write_json(journal, record)
        return json.dumps({'state': 'ready', 'changed': True, 'version_code': version,
                           'message': '证书补丁已完成，原始资源包已备份', 'path': str(path),
                           'backup': str(backup)}, ensure_ascii=False)
    except PermissionError as error:
        raise PermissionError('系统拒绝写入游戏资源目录。请授权后重新打开 zoffline；若资源由电脑导入，请覆盖安装同版本 Zwift 修正文件权限后重试') from error
    finally:
        if stage.exists():
            stage.unlink()
