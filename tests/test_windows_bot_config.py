"""Persist desktop bot settings in the existing server format, in isolation."""

import ast
import builtins
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import windows_bot_config as config


class BotConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='zoffline 机器人 ')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = config.bot_config_path(self.directory)

    def write(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value), encoding='utf-8')

    def manager(self):
        root = Path(config.__file__).parent
        with patch.object(sys, 'path', [str(root / 'protobuf')] + sys.path):
            import solo_blob
        dictionary = json.loads((root / 'data' / 'game_dictionary.txt').read_text(encoding='utf-8'),
                                object_hook=lambda data: {int(key) if key.lstrip('-').isdigit() else key: value
                                                          for key, value in data.items()})
        return solo_blob.SoloBlobManager(str(self.path.parent), lambda: [], dictionary,
                                         lambda: 0, lambda: {}, lambda _: None)

    def test_no_file_uses_server_defaults_without_creating_storage(self):
        self.assertEqual(config.load_bot_config(self.directory), config.BOT_DEFAULTS)
        self.assertFalse(self.path.parent.exists())
        source = Path(config.__file__).with_name('solo_blob.py')
        tree = ast.parse(source.read_text(encoding='utf-8'))
        defaults = next(ast.literal_eval(item.value) for item in tree.body
                        if isinstance(item, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == 'DEFAULT_CONFIG'
                                for target in item.targets))
        self.assertEqual(config.BOT_DEFAULTS, {key: defaults[key] for key in config.BOT_DEFAULTS})

    def test_roundtrip_normalizes_single_player_and_keeps_advanced_keys(self):
        self.write({'enabled': True, 'riders': 5, 'player_id': 47,
                    'scatter_gap': 2.2, 'custom': {'names': ['A', 'B']}})
        values = config.load_bot_config(self.directory)
        values.update(riders='12', row_gap='2.4', scatter=False)
        result = config.save_bot_config(self.directory, values)
        self.assertEqual(result['player_id'], 1)
        self.assertEqual(result['riders'], 12)
        self.assertEqual(result['row_gap'], 2.4)
        self.assertFalse(result['scatter'])
        self.assertEqual(result['custom'], {'names': ['A', 'B']})
        self.assertEqual(result['scatter_gap'], 2.2)
        self.assertEqual(config.load_bot_config(self.directory), result)

    def test_save_merges_the_latest_unknown_keys(self):
        self.write({'enabled': True, 'riders': 8, 'manual': 'original'})
        draft = config.load_bot_config(self.directory)
        self.write({'enabled': True, 'riders': 8, 'manual': 'external edit', 'other': 7})
        draft['riders'] = 9
        result = config.save_bot_config(self.directory, draft)
        self.assertEqual(result['manual'], 'external edit')
        self.assertEqual(result['other'], 7)

    def test_disabling_keeps_previous_parameters(self):
        self.write({'enabled': True, 'riders': 14, 'pull_interval': 120, 'record': True})
        result = config.save_bot_config(self.directory, {'enabled': False})
        self.assertFalse(result['enabled'])
        self.assertEqual(result['riders'], 14)
        self.assertEqual(result['pull_interval'], 120)
        self.assertTrue(result['record'])

    def test_invalid_count_never_changes_existing_file(self):
        self.write({'enabled': True, 'riders': 8})
        original = self.path.read_bytes()
        for value in (0, 31, -1, True, 8.5, '', '8.0', 'NaN', '²', '9' * 4500):
            with self.subTest(value=repr(value)[:50]), self.assertRaises(config.BotConfigError):
                config.save_bot_config(self.directory, {'riders': value})
            self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_numbers_and_booleans_are_rejected_before_writing(self):
        for field, values in {
                'row_gap': (0, -1, 'NaN', 'inf', True, None),
                'scatter_width': (1, 6.5),
                'lateral_spacing': (0, 3.1),
                'shuffle_interval': (0, 4, float('inf')),
                'pull_interval': (4, 'bad'),
                'enabled': ('false', 1),
                'rotate': (None,),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(config.BotConfigError):
                    config.save_bot_config(self.directory, {field: value})
                self.assertFalse(self.path.exists())

    def test_corrupt_files_cannot_be_overwritten_by_save(self):
        self.path.parent.mkdir(parents=True)
        for text in ('not json', '[]', 'null', '{"row_gap": NaN}', '{"riders": []}'):
            self.path.write_text(text, encoding='utf-8')
            with self.subTest(text=text), self.assertRaises(config.BotConfigError):
                config.load_bot_config(self.directory)
            with self.assertRaises(config.BotConfigError):
                config.save_bot_config(self.directory, {'enabled': True})
            self.assertEqual(self.path.read_text(encoding='utf-8'), text)

    def test_utf8_bom_supported_but_invalid_encoding_is_reported(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"riders": 10}', encoding='utf-8-sig')
        self.assertEqual(config.load_bot_config(self.directory)['riders'], 10)
        self.path.write_bytes(b'\xff\xfe\xfd')
        with self.assertRaises(config.BotConfigError):
            config.load_bot_config(self.directory)

    def test_atomic_replace_failure_preserves_original_and_removes_temporary(self):
        self.write({'enabled': False, 'riders': 8})
        original = self.path.read_bytes()
        with patch.object(config.os, 'replace', side_effect=PermissionError('read only')), \
                self.assertRaisesRegex(config.BotConfigError, '无法保存'):
            config.save_bot_config(self.directory, {'enabled': True})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_read_permission_failure_is_visible(self):
        with patch.object(Path, 'read_text', side_effect=PermissionError('denied')), \
                self.assertRaisesRegex(config.BotConfigError, '无法读取'):
            config.load_bot_config(self.directory)

    def test_saved_settings_are_consumed_by_real_solo_blob_manager(self):
        config.save_bot_config(self.directory, {'enabled': True, 'riders': 12, 'scatter': False})
        manager = self.manager()
        manager.load()
        self.assertEqual(list(manager.blobs), [1])
        self.assertEqual(len(manager.blobs[1].riders), 12)
        self.assertFalse(manager.blobs[1].config['scatter'])
        config.save_bot_config(self.directory, {'enabled': False})
        manager.load()
        self.assertFalse(manager.blobs)

    def test_current_solo_profile_is_the_blob_anchor(self):
        profile = self.path.parent / '47' / 'profile.bin'
        profile.parent.mkdir(parents=True)
        profile.write_bytes(b'existing profile')
        self.assertEqual(config.solo_player_id(self.directory), 47)
        saved = config.save_bot_config(self.directory, {'enabled': True, 'riders': 12})
        self.assertEqual(saved['player_id'], 47)
        manager = self.manager()
        manager.load()
        self.assertEqual(list(manager.blobs), [47])
        self.assertEqual(len(manager.blobs[47].riders), 12)

    def test_profile_selection_matches_server_directory_order_and_zero_fallback(self):
        for name in ('non-numeric', '47', '9', '0'):
            profile = self.path.parent / name / 'profile.bin'
            profile.parent.mkdir(parents=True)
            profile.write_bytes(b'profile')
        with patch.object(config.os, 'listdir', return_value=['non-numeric', '47', '9', '0']):
            self.assertEqual(config.solo_player_id(self.directory), 47)
        with patch.object(config.os, 'listdir', return_value=['0', '47']):
            self.assertEqual(config.solo_player_id(self.directory), 1)
        with patch.object(config.os, 'listdir', return_value=['9', '47']):
            self.assertEqual(config.solo_player_id(self.directory), 9)

    def test_unsafe_scattered_spacing_is_rejected_before_writing(self):
        self.write({'enabled': False, 'riders': 8})
        original = self.path.read_bytes()
        with self.assertRaisesRegex(config.BotConfigError, '不能超过散开宽度的一半') as error:
            config.save_bot_config(self.directory, {'enabled': True, 'riders': 30, 'scatter': True,
                                                    'scatter_width': 2, 'lateral_spacing': 3})
        self.assertEqual(error.exception.field, 'lateral_spacing')
        self.assertEqual(self.path.read_bytes(), original)

    def test_thirty_scattered_riders_load_at_the_narrowest_supported_width(self):
        config.save_bot_config(self.directory, {'enabled': True, 'riders': 30, 'scatter': True,
                                                'scatter_width': 2, 'lateral_spacing': 1})
        manager = self.manager()
        for _ in range(12):
            manager.load()
            self.assertEqual(len(manager.blobs[1].riders), 30)
            self.assertEqual(len(manager.blobs[1].slots), 30)

    def test_saved_unicode_fields_survive_system_cp936_reads(self):
        self.write({'custom': {'备注': '骑行机器人 测试 😄'}})
        config.save_bot_config(self.directory, {'enabled': True, 'riders': 8})
        self.assertTrue(self.path.read_bytes().isascii())
        manager = self.manager()
        module = sys.modules['solo_blob']
        original_open = builtins.open

        def cp936_open(path, mode='r', *args, **kwargs):
            if Path(path) == self.path and 'b' not in mode and 'encoding' not in kwargs:
                kwargs['encoding'] = 'cp936'
            return original_open(path, mode, *args, **kwargs)

        with patch.object(module, 'open', cp936_open, create=True):
            manager.load()
        self.assertEqual(manager.blobs[1].config['custom'], {'备注': '骑行机器人 测试 😄'})

    def test_excessive_json_nesting_is_a_visible_config_error(self):
        self.path.parent.mkdir(parents=True)
        content = '{"custom":' + '[' * 2000 + '0' + ']' * 2000 + '}'
        self.path.write_text(content, encoding='utf-8')
        for action in (lambda: config.load_bot_config(self.directory),
                       lambda: config.save_bot_config(self.directory, {'enabled': True})):
            with self.assertRaises(config.BotConfigError):
                action()
            self.assertEqual(self.path.read_text(encoding='utf-8'), content)

    def test_serialization_recursion_failure_keeps_file_and_cleans_temporary(self):
        self.write({'enabled': False, 'riders': 8})
        original = self.path.read_bytes()
        with patch.object(config.json, 'dump', side_effect=RecursionError('too deep')), \
                self.assertRaisesRegex(config.BotConfigError, '无法保存'):
            config.save_bot_config(self.directory, {'enabled': True})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_supported_manual_values_are_readable_outside_editor_limits(self):
        self.write({'enabled': True, 'shuffle_interval': 0, 'scatter_width': 8, 'row_gap': 12})
        values = config.load_bot_config(self.directory)
        self.assertEqual(values['shuffle_interval'], 0)
        self.assertEqual(values['scatter_width'], 8)
        self.assertEqual(values['row_gap'], 12)
        # Changing only the on/off switch retains unrelated existing values.
        saved = config.save_bot_config(self.directory, {'enabled': False})
        self.assertEqual(saved['shuffle_interval'], 0)
        self.assertEqual(saved['scatter_width'], 8)
        self.assertEqual(saved['row_gap'], 12)

    def assert_complete_slots(self, blob, count):
        self.assertEqual(len(blob.riders), count)
        self.assertEqual(len(blob.slots), count)
        self.assertEqual({rider.slot for rider in blob.riders}, set(range(count)))

    def test_preserved_scatter_constraints_keep_all_riders_through_mode_changes(self):
        for hidden in ({'scatter_gap': 4}, {'scatter_side': 3}):
            with self.subTest(hidden=hidden):
                self.write(dict(hidden, enabled=True, riders=8))
                saved = config.save_bot_config(self.directory, {'enabled': True, 'riders': 30,
                    'scatter': True, 'scatter_width': 2, 'lateral_spacing': 1})
                for key, value in hidden.items():
                    self.assertEqual(saved[key], value)
                manager = self.manager()
                module = sys.modules['solo_blob']
                with patch.object(module.time, 'time_ns', return_value=123456):
                    with self.assertLogs('solo_blob', level='WARNING') as messages:
                        manager.load()
                    self.assertIn('using default row formation', '\n'.join(messages.output))
                    blob = manager.blobs[1]
                    self.assert_complete_slots(blob, 30)
                    for command in ('new', 'lead', 'lead', 'hammer', 'toast', 'attack'):
                        manager.chat_command(1, command)
                        self.assert_complete_slots(blob, 30)
                    for _ in range(5):
                        blob._vary_leaders()
                        blob._rotate_line()
                        self.assert_complete_slots(blob, 30)

    def test_bell_capacity_fallback_returns_to_scattering_when_front_slots_return(self):
        self.write({'enabled': True, 'riders': 22, 'scatter_gap': 4,
                    'scatter_width': 2, 'lateral_spacing': 1})
        manager = self.manager()
        module = sys.modules['solo_blob']
        with patch.object(module.time, 'time_ns', return_value=123456):
            manager.load()
        blob = manager.blobs[1]
        self.assertFalse(blob._scatter_capacity_fallback)
        self.assertLess(len(blob.scatter_tail), 22)
        self.assert_complete_slots(blob, 22)
        # Bell/F8 makes every rider sit behind the player, so front positions
        # can no longer make up for the tail pool's limited capacity.
        with self.assertLogs('solo_blob', level='WARNING'):
            manager.chat_command(1, 'lead')
        self.assertTrue(blob._scatter_capacity_fallback)
        self.assert_complete_slots(blob, 22)
        manager.chat_command(1, 'lead')
        self.assertFalse(blob._scatter_capacity_fallback)
        self.assert_complete_slots(blob, 22)

    def test_capacity_fallback_uses_safe_row_defaults_and_preserves_manual_values(self):
        hidden = {'scatter_gap': 4, 'row_gap': 0, 'columns': -2, 'tail_gap': 'unused',
                  'lead_gap': 'unused', 'lead_lateral': 'unused', 'max_lead': 'unused'}
        self.write(dict(hidden, enabled=True, riders=8))
        config.save_bot_config(self.directory, {'enabled': True, 'riders': 30,
            'scatter': True, 'scatter_width': 2, 'lateral_spacing': 1})
        manager = self.manager()
        module = sys.modules['solo_blob']
        with patch.object(module.time, 'time_ns', return_value=123456), \
                self.assertLogs('solo_blob', level='WARNING'):
            manager.load()
        blob = manager.blobs[1]
        self.assert_complete_slots(blob, 30)
        manager.chat_command(1, 'lead')
        self.assert_complete_slots(blob, 30)
        for key, value in hidden.items():
            self.assertEqual(blob.config[key], value)
            self.assertEqual(json.loads(self.path.read_text())[key], value)

    def test_scattering_resumes_after_the_sample_pool_gains_capacity(self):
        self.write({'enabled': True, 'riders': 30, 'scatter_gap': 4,
                    'scatter_width': 2, 'lateral_spacing': 1})
        manager = self.manager()
        module = sys.modules['solo_blob']
        with patch.object(module.time, 'time_ns', return_value=123456), \
                self.assertLogs('solo_blob', level='WARNING'):
            manager.load()
        blob = manager.blobs[1]
        self.assertTrue(blob._scatter_capacity_fallback)
        blob.config.update(scatter_gap=1.75, scatter_side=1, scatter_width=6.4)
        blob._set_mode(None, 0)
        self.assertFalse(blob._scatter_capacity_fallback)
        self.assert_complete_slots(blob, 30)
        blob._vary_leaders()
        blob._rotate_line()
        self.assert_complete_slots(blob, 30)


if __name__ == '__main__':
    unittest.main()
