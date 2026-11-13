"""Persist desktop bot settings in the existing server format, in isolation."""

import ast
import builtins
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import windows_bot_config as config


class BotPlayerListTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='zoffline 玩家 # % ')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / 'storage' / 'zwift-offline.db'

    def database(self, rows=()):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('CREATE TABLE user (player_id, first_name, last_name, username, pass_hash)')
            connection.executemany('INSERT INTO user VALUES (?, ?, ?, ?, ?)',
                                   [tuple(row) + ('private password hash',) for row in rows])
            connection.commit()

    def test_missing_storage_or_database_returns_empty_without_creating_files(self):
        self.assertEqual(config.list_bot_players(self.directory), [])
        self.assertFalse(self.path.parent.exists())
        self.path.parent.mkdir()
        self.assertEqual(config.list_bot_players(self.directory), [])
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_missing_user_table_and_empty_accounts_return_empty_without_changes(self):
        self.path.parent.mkdir()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('CREATE TABLE version (version INTEGER)')
            connection.commit()
        original = self.path.read_bytes()
        self.assertEqual(config.list_bot_players(self.directory), [])
        self.assertEqual(self.path.read_bytes(), original)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('CREATE TABLE user (player_id, first_name, last_name, username)')
            connection.commit()
        original = self.path.read_bytes()
        self.assertEqual(config.list_bot_players(self.directory), [])
        self.assertEqual(self.path.read_bytes(), original)

    def test_players_are_sorted_cleaned_and_never_include_passwords(self):
        self.database(((47, ' 张\t 三 ', ' 骑手\n ', ' first@example.com '),
                       (2, '张 三', '骑手', 'second@example.com'),
                       (10, '  ', None, ' username '), (11, '', '', ''),
                       (0, 'Invalid', '', 'zero'), (-1, 'Invalid', '', 'negative'),
                       (3.5, 'Invalid', '', 'fraction'), (None, 'Invalid', '', 'missing'),
                       ('9223372036854775808', 'Invalid', '', 'overflow')))
        original = self.path.read_bytes()
        self.assertEqual(config.list_bot_players(self.directory), [
            {'player_id': 2, 'name': '张 三 骑手', 'username': 'second@example.com'},
            {'player_id': 10, 'name': 'username', 'username': 'username'},
            {'player_id': 11, 'name': '未命名玩家', 'username': ''},
            {'player_id': 47, 'name': '张 三 骑手', 'username': 'first@example.com'},
        ])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_query_is_read_only_skips_credentials_and_always_closes_connection(self):
        self.database(((47, '张三', '', 'rider@example.com'),))
        connect = sqlite3.connect
        opened = []
        reads = []

        def readonly_connect(database_uri, **kwargs):
            self.assertTrue(database_uri.endswith('?mode=ro'))
            self.assertTrue(kwargs['uri'])
            self.assertLessEqual(kwargs['timeout'], 0.2)
            connection = connect(database_uri, **kwargs)
            opened.append(connection)

            def authorize(action, table, column, *unused):
                if action == sqlite3.SQLITE_READ:
                    reads.append((table, column))
                    if column == 'pass_hash':
                        return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            return connection

        with patch.object(config.sqlite3, 'connect', side_effect=readonly_connect):
            self.assertEqual(config.list_bot_players(self.directory)[0]['player_id'], 47)
        self.assertNotIn(('user', 'pass_hash'), reads)
        self.assertTrue(opened)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute('SELECT 1')

    def test_corrupt_database_reports_error_without_changes(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b'not a sqlite database')
        original = self.path.read_bytes()
        with self.assertRaisesRegex(config.BotConfigError, '玩家列表'):
            config.list_bot_players(self.directory)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_locked_database_fails_promptly_without_changing_data(self):
        self.database(((47, '张三', '', 'rider@example.com'),))
        original = self.path.read_bytes()
        with closing(sqlite3.connect(self.path)) as writer:
            writer.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            with self.assertRaisesRegex(config.BotConfigError, 'locked'):
                config.list_bot_players(self.directory)
            self.assertLess(time.monotonic() - started, 1.0)
            writer.rollback()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(config.list_bot_players(self.directory)[0]['player_id'], 47)

    def test_incompatible_schema_is_reported_instead_of_silently_hiding_accounts(self):
        self.path.parent.mkdir()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('CREATE TABLE user (player_id INTEGER)')
            connection.commit()
        original = self.path.read_bytes()
        with self.assertRaisesRegex(config.BotConfigError, '玩家列表'):
            config.list_bot_players(self.directory)
        self.assertEqual(self.path.read_bytes(), original)


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

    def test_lan_player_id_accepts_positive_int64_integers_and_decimal_strings(self):
        for value, expected in ((47, 47), (' 0047 ', 47), (1, 1),
                                (9223372036854775807, 9223372036854775807),
                                ('9223372036854775807', 9223372036854775807)):
            with self.subTest(value=value):
                self.assertEqual(config.validate_player_id(value), expected)

    def test_invalid_lan_player_ids_identify_the_field_without_writing(self):
        self.write({'enabled': True, 'riders': 8, 'player_id': 47})
        original = self.path.read_bytes()
        invalid = (None, True, False, 47.0, 0, -1, 9223372036854775808,
                   '', ' ', '0', '-1', '+47', '47.0', '1e2', 'NaN',
                   '9223372036854775808', '9' * 4500, [], {})
        for value in invalid:
            with self.subTest(value=repr(value)[:50]), self.assertRaises(config.BotConfigError) as error:
                config.validate_player_id(value)
            self.assertEqual(error.exception.field, 'player_id')
            if value is not None:  # None retains the existing solo-save API.
                with self.assertRaises(config.BotConfigError) as save_error:
                    config.save_bot_config(self.directory, {'riders': 12}, player_id=value)
                self.assertEqual(save_error.exception.field, 'player_id')
            self.assertEqual(self.path.read_bytes(), original)

    def test_explicit_lan_target_avoids_solo_profile_and_is_consumed_by_manager(self):
        self.write({'enabled': True, 'riders': 8, 'player_id': 9, 'custom': {'备注': '保留'}})
        with patch.object(config, 'solo_player_id', side_effect=AssertionError('LAN must use its chosen target')):
            saved = config.save_bot_config(self.directory, {'riders': 12}, player_id='47')
        self.assertEqual(saved['player_id'], 47)
        self.assertEqual(saved['custom'], {'备注': '保留'})
        self.assertEqual(config.load_bot_config(self.directory)['player_id'], 47)
        manager = self.manager()
        manager.load()
        self.assertEqual(list(manager.blobs), [47])
        self.assertEqual(len(manager.blobs[47].riders), 12)

    def test_solo_save_still_selects_current_profile_after_explicit_lan_save(self):
        profile = self.path.parent / '9' / 'profile.bin'
        profile.parent.mkdir(parents=True)
        profile.write_bytes(b'existing profile')
        config.save_bot_config(self.directory, {'enabled': True}, player_id=47)
        saved = config.save_bot_config(self.directory, {'riders': 12})
        self.assertEqual(saved['player_id'], 9)

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
        hidden = {'scatter_gap': 4, 'row_gap': 0, 'columns': -2, 'tail_gap': 0,
                  'lead_gap': 0, 'lead_lateral': -1, 'max_lead': 0}
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

    def test_custom_names_keep_blank_positions_and_internal_spaces_in_ascii_json(self):
        saved = config.save_bot_config(self.directory, {
            'custom_names': ['  小明  ', '', '  Team  Rider ', '骑行😄']})
        self.assertEqual(saved['custom_names'], ['小明', '', 'Team  Rider', '骑行😄'])
        self.assertEqual(config.load_bot_config(self.directory)['custom_names'], saved['custom_names'])
        self.assertTrue(self.path.read_bytes().isascii())
        config.save_bot_config(self.directory, {'custom_names': [' ', '', '  ']})
        self.assertEqual(config.load_bot_config(self.directory)['custom_names'], [])

    def test_invalid_custom_names_identify_the_line_without_changing_file(self):
        self.write({'custom_names': ['Original']})
        original = self.path.read_bytes()
        for value, line in ((['Valid', 'x' * 41], 2), (['Name'] * 31, 31),
                            (['Valid', None], 2), (['Valid', 'Bad\tName'], 2),
                            (['Valid', '\ud800'], 2), (['Valid', 'Bad\u2028Name'], 2),
                            ('single string', None), ({'name': 'value'}, None)):
            with self.subTest(value=repr(value)), self.assertRaises(config.BotConfigError) as error:
                config.save_bot_config(self.directory, {'custom_names': value})
            self.assertEqual(error.exception.field, 'custom_names')
            self.assertEqual(error.exception.line, line)
            self.assertEqual(self.path.read_bytes(), original)
        # Limits count Unicode code points, including non-BMP characters.
        saved = config.save_bot_config(self.directory, {'custom_names': ['😄' * 40]})
        self.assertEqual(saved['custom_names'], ['😄' * 40])

    def test_real_profiles_use_whole_custom_names_and_keep_blank_entry_random(self):
        config.save_bot_config(self.directory, {'enabled': True, 'riders': 3,
                                                'custom_names': ['小明', '', 'Team  Rider']})
        manager = self.manager()
        manager.names_provider = lambda: [{'first_name': 'Random', 'last_name': 'Rider',
                                          'ride_jersey': 1234}]
        manager.load()
        blob = manager.blobs[1]
        for rider, expected in zip(blob.riders, (('', '小明'), ('Random', 'Rider'), ('', 'Team  Rider'))):
            profile = type(rider.profile).FromString(rider.profile.SerializeToString())
            self.assertEqual((profile.first_name, profile.last_name), expected)
            self.assertEqual(profile.ride_jersey, 1234)
        manager.send_update = lambda *_: None
        for index, rider in enumerate(blob.riders):
            rider.gap = 0 if index == 0 else 10
        self.assertIn('Rider 小明 ', manager.chat_command(1, 'bot wave'))

    def test_custom_name_order_survives_roster_count_changes_and_new_ride(self):
        expected = ['小明', '', 'Team Rider']
        config.save_bot_config(self.directory, {'enabled': True, 'riders': 3, 'custom_names': expected})
        manager = self.manager()
        manager.load()
        module = sys.modules['solo_blob']
        manager.chat_command(1, 'new')
        self.assertEqual(manager.blobs[1].riders[0].profile.last_name, '小明')
        config.save_bot_config(self.directory, {'riders': 1})
        manager.load()
        self.assertEqual(len(manager.blobs[1].riders), 1)
        self.assertEqual(manager.blobs[1].riders[0].profile.last_name, '小明')
        self.assertEqual(config.load_bot_config(self.directory)['custom_names'], expected)
        config.save_bot_config(self.directory, {'riders': 3})
        manager.load()
        blob = manager.blobs[1]
        self.assertEqual(blob.riders[2].profile.last_name, 'Team Rider')
        blob.trail.reset()
        for x, road_time in ((1000, 100000), (2000, 110000)):
            state = module.udp_node_msgs_pb2.PlayerState(id=1, x=x, z=100, roadTime=road_time,
                roadPosition=10000000, speed=36000000, power=150)
            blob.on_state(state)
        with patch.object(blob, 'reroll', wraps=blob.reroll) as reroll:
            blob.update(module.TICK_SECONDS)
        reroll.assert_called_once_with()
        self.assertEqual(blob.riders[0].profile.last_name, '小明')
        self.assertEqual(blob.riders[2].profile.last_name, 'Team Rider')

    def test_no_custom_names_preserves_original_random_profile_names(self):
        config.save_bot_config(self.directory, {'enabled': True, 'riders': 2})
        manager = self.manager()
        manager.names_provider = lambda: [{'first_name': 'Random', 'last_name': 'Name'}]
        manager.load()
        self.assertEqual([(r.profile.first_name, r.profile.last_name) for r in manager.blobs[1].riders],
                         [('Random', 'Name'), ('Random', 'Name')])
        config.save_bot_config(self.directory, {'custom_names': ['Custom']})
        manager.load()
        self.assertEqual(manager.blobs[1].riders[0].profile.last_name, 'Custom')
        config.save_bot_config(self.directory, {'custom_names': []})
        manager.load()
        self.assertEqual(manager.blobs[1].riders[0].profile.first_name, 'Random')


if __name__ == '__main__':
    unittest.main()
