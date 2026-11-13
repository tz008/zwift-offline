"""Verify actual ZIP content, equal-size replacement, and interrupted OBB recovery."""

from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import android_obb_patch as obb


ROOT = Path(__file__).resolve().parents[1]


class AndroidObbPatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'main.168455.com.zwift.zwiftgame.obb'
        self.state = self.root / 'state'
        self.certificate = ROOT / 'ssl/cert-zwift-com.pem'
        self.old_bundle = b'# Original official roots\n'
        self.asset = bytes(range(256)) * 128
        with zipfile.ZipFile(self.source, 'w', zipfile.ZIP_DEFLATED) as archive:
            for index in range(80):
                archive.writestr('dataES/directories/' + str(index) + '/', b'')
            archive.writestr('dataES/cacert.pem', self.old_bundle)
            archive.writestr('dataES/assets/Worlds/\u4e16\u754c.bin', self.asset)
            archive.comment = b'Zwift archive comment'
        self.original = self.source.read_bytes()

    def prepare(self, **kwargs):
        return json.loads(obb.prepare(str(self.source), str(self.certificate), str(self.state), '168455', **kwargs))

    def test_keeps_original_backup_preserves_official_roots_and_other_assets(self):
        result = self.prepare()
        self.assertTrue(result['changed'])
        self.assertEqual('ready', result['state'])
        self.assertEqual(self.original, Path(result['backup']).read_bytes())
        self.assertEqual(len(self.original), self.source.stat().st_size)
        with zipfile.ZipFile(self.source) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(self.asset, archive.read('dataES/assets/Worlds/\u4e16\u754c.bin'))
            self.assertEqual(b'Zwift archive comment', archive.comment)
            bundle = archive.read(obb.TARGET)
            self.assertTrue(bundle.startswith(self.old_bundle))
            for block in obb.PEM.findall(self.certificate.read_bytes()):
                self.assertIn(block.replace(b'\r\n', b'\n'), bundle)

    def test_second_check_does_not_rewrite_or_duplicate_certificates(self):
        self.prepare()
        first = self.source.read_bytes()
        modified = self.source.stat().st_mtime_ns
        result = self.prepare()
        self.assertFalse(result['changed'])
        self.assertEqual(first, self.source.read_bytes())
        self.assertEqual(modified, self.source.stat().st_mtime_ns)
        self.assertEqual(1, len(list(self.root.glob('*.zoffline-original-*'))))

    def test_wrong_game_version_is_rejected_before_writing(self):
        with self.assertRaisesRegex(ValueError, '\u7248\u672c'):
            obb.prepare(self.source, self.certificate, self.state, '999')
        self.assertEqual(self.original, self.source.read_bytes())

    def test_invalid_zip_does_not_replace_source(self):
        self.source.write_bytes(b'incomplete game resource')
        with self.assertRaises(zipfile.BadZipFile):
            self.prepare()
        self.assertEqual(b'incomplete game resource', self.source.read_bytes())

    def test_low_space_does_not_mutate_source(self):
        with patch.object(obb.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 1})()):
            with self.assertRaisesRegex(ValueError, '\u7a7a\u95f4'):
                self.prepare()
        self.assertEqual(self.original, self.source.read_bytes())
        self.assertFalse(list(self.root.glob('*.zoffline-original-*')))

    def test_corrupted_asset_fails_crc_and_leaves_original_in_place(self):
        with zipfile.ZipFile(self.source) as archive:
            info = archive.getinfo('dataES/assets/Worlds/\u4e16\u754c.bin')
            start = info.header_offset
        content = bytearray(self.source.read_bytes())
        name_size = int.from_bytes(content[start + 26:start + 28], 'little')
        extra_size = int.from_bytes(content[start + 28:start + 30], 'little')
        content[start + 30 + name_size + extra_size + 5] ^= 32
        self.source.write_bytes(content)
        before = self.source.read_bytes()
        with self.assertRaises(Exception):
            self.prepare()
        self.assertEqual(before, self.source.read_bytes())
        self.assertFalse(list(self.root.glob('*.zoffline-original-*')))

    def test_failure_after_original_rename_restores_original(self):
        replace = os.replace

        def fail_stage(source, destination):
            if str(source).endswith('.zoffline-patch.tmp'):
                raise OSError('simulated failed final rename')
            return replace(source, destination)

        with patch.object(obb.os, 'replace', side_effect=fail_stage):
            with self.assertRaisesRegex(OSError, 'simulated'):
                self.prepare()
        self.assertEqual(self.original, self.source.read_bytes())
        self.assertTrue(self.prepare()['changed'])

    def test_recovery_after_process_death_restores_missing_original_then_patches(self):
        self.state.mkdir()
        backup = self.source.with_name(self.source.name + '.zoffline-original-deadbeef')
        os.replace(self.source, backup)
        record = {'state': 'prepared', 'path': str(self.source), 'backup': str(backup),
                  'original_sha256': obb._hash(backup), 'patched_sha256': '0' * 64}
        (self.state / '168455.json').write_text(json.dumps(record))
        self.assertTrue(self.prepare()['changed'])
        with zipfile.ZipFile(self.source) as archive:
            self.assertIn(b'-----BEGIN CERTIFICATE-----', archive.read(obb.TARGET))

    def test_external_update_during_build_is_not_overwritten(self):
        build = obb._build
        replacement = b'game updater installed a different OBB'

        def update_after_build(*args):
            result = build(*args)
            self.source.write_bytes(replacement)
            return result

        with patch.object(obb, '_build', side_effect=update_after_build):
            with self.assertRaisesRegex(ValueError, '\u5df2\u66f4\u65b0'):
                self.prepare()
        self.assertEqual(replacement, self.source.read_bytes())

    def test_archive_without_room_fails_without_touching_the_original(self):
        with zipfile.ZipFile(self.source, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(obb.TARGET, self.old_bundle)
        before = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, '\u7a7a\u95f4'):
            self.prepare()
        self.assertEqual(before, self.source.read_bytes())

    def test_duplicate_zip_names_are_not_rewritten(self):
        with zipfile.ZipFile(self.source, 'a') as archive:
            archive.writestr('dataES/duplicate.bin', b'1')
            with self.assertWarns(UserWarning):
                archive.writestr('dataES/duplicate.bin', b'2')
        before = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, '\u91cd\u590d'):
            self.prepare()
        self.assertEqual(before, self.source.read_bytes())


if __name__ == '__main__':
    unittest.main()
