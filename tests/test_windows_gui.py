"""GUI interaction regressions using a hidden Tk window and no system changes."""

from pathlib import Path
from contextlib import closing
import json
import sqlite3
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import windows_gui as gui
from windows_bot_config import bot_config_path, save_bot_config


class WindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.destroy_root)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.background = patch.object(gui.LauncherWindow, '_background').start()
        self.addCleanup(patch.stopall)
        self.window = gui.LauncherWindow(self.root, self.directory)
        self.window.session = Mock()

    def destroy_root(self):
        self.root.update_idletasks()
        self.root.destroy()

    def test_host_only_start_has_no_ride_and_freezes_mode_selection(self):
        self.window.mode.set('host')
        self.window.host_ip.set('192.168.1.100')
        self.window.ride.set(False)
        self.window._mode_changed()
        self.assertEqual(self.window.start_button['text'], '启动服务器')
        self.window.start()
        options = self.window.session.start.call_args.args[0]
        self.assertEqual(options, {'mode': 'host', 'server_ip': '192.168.1.100', 'ride': False})
        self.assertTrue(self.window.busy)
        self.assertTrue(self.window.start_button.instate(['disabled']))
        self.assertTrue(all(widget.instate(['disabled']) for widget in self.window.editable))
        self.window.emit('stopped', None)
        self.window._drain()
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.start_button.instate(['disabled']))

    def test_join_start_uses_server_address_and_forces_riding(self):
        self.window.mode.set('join')
        self.window.join_ip.set('192.168.1.20')
        self.window.ride.set(False)
        self.window.start()
        self.assertEqual(self.window.session.start.call_args.args[0],
                         {'mode': 'join', 'server_ip': '192.168.1.20', 'ride': True})

    def test_invalid_join_address_stays_idle_with_visible_error(self):
        self.window.mode.set('join')
        self.window.join_ip.set('https://localhost')
        with patch.object(gui.messagebox, 'showerror') as error:
            self.window.start()
        self.window.session.start.assert_not_called()
        self.assertFalse(self.window.busy)
        error.assert_called_once()

    def test_connection_check_disables_start_until_result(self):
        self.window.join_ip.set('192.168.1.10')
        self.window.check_connection()
        self.assertTrue(self.window.start_button.instate(['disabled']))
        self.assertTrue(self.window.checking)
        self.window.emit('probe-error', '连接失败')
        self.window._drain()
        self.assertFalse(self.window.checking)
        self.assertFalse(self.window.start_button.instate(['disabled']))
        self.assertEqual(self.window.status.get(), '连接失败')

    def test_close_waits_for_cleanup_and_restore_error_keeps_window_open(self):
        self.window._set_busy(True)
        with patch.object(gui.messagebox, 'askyesno', return_value=True):
            self.window.close()
        self.window.session.stop.assert_called_once()
        self.assertTrue(self.window.closing)
        self.assertTrue(self.root.winfo_exists())
        with patch.object(gui.messagebox, 'showerror') as error:
            self.window.emit('error', '配置还原未确认')
            self.window.emit('stopped', '配置还原未确认')
            self.window._drain()
        self.assertFalse(self.window.closing)
        self.assertTrue(self.root.winfo_exists())
        error.assert_called_once()

    def test_session_finishing_inside_exit_confirmation_does_not_trap_window(self):
        self.window._set_busy(True)

        def finishes_while_asking():
            self.window.emit('stopped', None)
            self.window._drain()
            return True

        with patch.object(self.window, '_confirm_stop', side_effect=finishes_while_asking), \
                patch.object(self.root, 'destroy') as destroy:
            self.window.close()
        destroy.assert_called_once()
        self.window.session.stop.assert_not_called()
        self.assertFalse(self.window.closing)

    def select_host(self, ride=False):
        self.window.mode.set('host')
        self.window.host_ip.set('192.168.1.100')
        self.window.ride.set(ride)
        self.window._mode_changed()

    def player_database(self, rows):
        path = self.directory / 'storage' / 'zwift-offline.db'
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection:
            connection.execute('CREATE TABLE user (player_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, username TEXT)')
            connection.executemany('INSERT INTO user VALUES (?, ?, ?, ?)', rows)
            connection.commit()
        return path

    def test_host_player_picker_shows_names_and_distinguishes_duplicate_accounts(self):
        self.player_database(((47, '张三', '', 'first@example.com'), (2, '张三', '', 'second@example.com')))
        save_bot_config(self.directory, {'enabled': True}, player_id=47)
        self.select_host()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        self.assertIsInstance(dialog.player_field, gui.ttk.Combobox)
        self.assertEqual(tuple(dialog.player_field['values']), ('张三 · ID 2', '张三 · ID 47'))
        self.assertEqual(dialog.player_id.get(), '张三 · ID 47')
        self.assertIn('first@example.com', dialog.player_hint.get())
        self.assertIn('跟随 张三（ID 47）', self.window.bot_summary.get())
        for index, player_id, username in ((0, 2, 'second@example.com'), (1, 47, 'first@example.com')):
            with self.subTest(player_id=player_id):
                dialog.player_field.current(index)
                self.assertIn(username, dialog.player_hint.get())
                dialog.save()
                self.assertEqual(json.loads(bot_config_path(self.directory).read_text())['player_id'], player_id)
                self.window.configure_bots()
                dialog = self.window.bot_dialog
                self.assertEqual(dialog.player_id.get(), '张三 · ID %d' % player_id)
        dialog.cancel()

    def test_player_labels_use_explicit_mapping_even_when_name_contains_id_text(self):
        self.player_database(((47, '张三 · ID 999', '', 'rider@example.com'),))
        self.select_host()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.player_field.current(0)
        dialog.save()
        self.assertEqual(json.loads(bot_config_path(self.directory).read_text())['player_id'], 47)

    def test_host_picker_keeps_manual_unknown_ids_and_invalid_drafts_on_refresh(self):
        self.player_database(((47, '张三', '', 'rider@example.com'),))
        save_bot_config(self.directory, {'enabled': True}, player_id=47)
        original = bot_config_path(self.directory).read_bytes()
        self.select_host()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        for draft in ('73', '', '不是玩家 · ID 47', '47.5'):
            with self.subTest(draft=draft):
                dialog.player_id.set(draft)
                dialog.refresh_players()
                self.assertEqual(dialog.player_id.get(), draft)
        dialog.save()
        self.assertTrue(dialog.winfo_exists())
        self.assertIn('玩家 ID', dialog.error.get())
        self.assertEqual(bot_config_path(self.directory).read_bytes(), original)
        dialog.player_id.set('73')
        dialog.save()
        self.assertEqual(json.loads(bot_config_path(self.directory).read_text())['player_id'], 73)
        self.assertIn('跟随玩家 ID 73', self.window.bot_summary.get())

    def test_refresh_and_dropdown_show_registered_players_and_rename_without_losing_draft(self):
        path = self.player_database(((47, '旧名字', '', 'first@example.com'),))
        save_bot_config(self.directory, {'enabled': True}, player_id=47)
        self.select_host()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.variables['riders'].set('12')
        dialog.names_text.insert('1.0', '未保存的机器人名称')
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("UPDATE user SET first_name='新名字', username='renamed@example.com' WHERE player_id=47")
            connection.execute("INSERT INTO user VALUES (73, '新玩家', '', 'new@example.com')")
            connection.commit()
        dialog.player_refresh.invoke()
        self.assertEqual(dialog.player_id.get(), '新名字 · ID 47')
        self.assertIn('renamed@example.com', dialog.player_hint.get())
        self.assertIn('新玩家 · ID 73', dialog.player_field['values'])
        self.assertEqual(dialog.variables['riders'].get(), '12')
        self.assertEqual(dialog.names_text.get('1.0', 'end-1c'), '未保存的机器人名称')
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("UPDATE user SET last_name='下拉刷新' WHERE player_id=47")
            connection.commit()
        self.root.tk.call(dialog.player_field.cget('postcommand'))
        self.assertEqual(dialog.player_id.get(), '新名字 下拉刷新 · ID 47')
        dialog.reset_defaults()
        self.assertEqual(dialog.player_id.get(), '新名字 下拉刷新 · ID 47')
        dialog.cancel()
        self.assertEqual(json.loads(bot_config_path(self.directory).read_text())['player_id'], 47)

    def test_refresh_missing_target_keeps_its_numeric_id_without_choosing_another_player(self):
        path = self.player_database(((47, '旧玩家', '', 'old@example.com'), (73, '其他玩家', '', 'other@example.com')))
        save_bot_config(self.directory, {'enabled': True}, player_id=47)
        self.select_host()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        with closing(sqlite3.connect(path)) as connection:
            connection.execute('DELETE FROM user WHERE player_id=47')
            connection.commit()
        dialog.refresh_players()
        self.assertEqual(dialog.player_id.get(), '47')
        self.assertEqual(tuple(dialog.player_field['values']), ('其他玩家 · ID 73',))
        dialog.save()
        self.assertEqual(json.loads(bot_config_path(self.directory).read_text())['player_id'], 47)

    def test_failed_player_refresh_preserves_choices_and_allows_saving_selected_account(self):
        path = self.player_database(((47, '张三', '', 'rider@example.com'),))
        save_bot_config(self.directory, {'enabled': True}, player_id=47)
        self.select_host()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        choices = dict(dialog.player_choices)
        values = tuple(dialog.player_field['values'])
        path.write_bytes(b'broken database')
        dialog.refresh_players()
        self.assertEqual(dialog.player_choices, choices)
        self.assertEqual(tuple(dialog.player_field['values']), values)
        self.assertEqual(dialog.player_id.get(), '张三 · ID 47')
        self.assertIn('rider@example.com', dialog.player_hint.get())
        self.assertIn('无法读取', dialog.player_list_error.get())
        dialog.save()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(json.loads(bot_config_path(self.directory).read_text())['player_id'], 47)
        self.assertIn('跟随玩家 ID 47', self.window.bot_summary.get())
        self.assertEqual(path.read_bytes(), b'broken database')

    def test_initial_player_list_failure_and_empty_server_allow_manual_target(self):
        self.select_host()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        self.assertIn('注册或登录', dialog.player_hint.get())
        self.assertEqual(tuple(dialog.player_field['values']), ())
        self.assertFalse((self.directory / 'storage').exists())
        dialog.cancel()
        path = self.directory / 'storage' / 'zwift-offline.db'
        path.parent.mkdir()
        path.write_bytes(b'broken database')
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        self.assertIn('无法读取', dialog.player_list_error.get())
        dialog.player_id.set('73')
        dialog.refresh_players()
        self.assertEqual(dialog.player_id.get(), '73')
        dialog.save()
        self.assertEqual(json.loads(bot_config_path(self.directory).read_text())['player_id'], 73)

    def test_named_selection_remains_live_during_server_session(self):
        self.player_database(((47, '张三', '', 'rider@example.com'),))
        self.run_host_session()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.player_field.current(0)
        dialog.variables['enabled'].set(True)
        dialog.save()
        self.assertIn('约 1 秒内更新', self.window.status.get())
        self.assertIn('跟随 张三（ID 47）', self.window.bot_summary.get())
        self.assertEqual(json.loads(bot_config_path(self.directory).read_text())['player_id'], 47)

    def test_other_modes_never_query_server_player_list(self):
        with patch.object(gui, 'list_bot_players', side_effect=AssertionError('no player picker in this mode')):
            self.window.configure_bots()
            self.assertIsNone(self.window.bot_dialog.player_field)
            self.window.bot_dialog.cancel()
            self.window.mode.set('join')
            self.window._mode_changed()
            self.window.configure_bots()

    def test_bot_configuration_entry_is_available_for_local_servers_only(self):
        self.assertTrue(self.window.single_frame.grid_info())
        self.assertTrue(self.window.bot_frame.grid_info())
        self.window.configure_bots()
        self.assertIsNone(self.window.bot_dialog.player_field)
        self.window.bot_dialog.cancel()
        for ride in (True, False):
            with self.subTest(ride=ride):
                self.select_host(ride)
                self.assertFalse(self.window.single_frame.grid_info())
                self.assertTrue(self.window.bot_frame.grid_info())
                self.assertFalse(self.window.bot_button.instate(['disabled']))
                self.window.configure_bots()
                self.assertEqual(self.window.bot_dialog.mode, 'host')
                self.assertEqual(self.window.bot_dialog.player_id.get(), '1')
                self.assertIsNotNone(self.window.bot_dialog.player_field)
                self.window.bot_dialog.cancel()
        self.window.mode.set('join')
        self.window._mode_changed()
        previous = self.window.bot_dialog
        self.assertFalse(self.window.bot_frame.grid_info())
        self.assertTrue(self.window.bot_button.instate(['disabled']))
        self.window.configure_bots()
        self.assertIs(self.window.bot_dialog, previous)
        self.assertFalse(self.window.bot_dialog.winfo_exists())

    def test_host_bot_settings_save_remote_target_and_reopen_without_solo_rebinding(self):
        profile = self.directory / 'storage' / '9' / 'profile.bin'
        profile.parent.mkdir(parents=True)
        profile.write_bytes(b'solo profile')
        self.select_host()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.variables['enabled'].set(True)
        dialog.variables['riders'].set('12')
        dialog.player_id.set('47')
        dialog.save()
        saved = json.loads(bot_config_path(self.directory).read_text())
        self.assertEqual(saved['player_id'], 47)
        self.assertEqual(saved['riders'], 12)
        self.assertIn('12 人', self.window.bot_summary.get())
        self.assertIn('跟随玩家 ID 47', self.window.bot_summary.get())
        self.assertNotIn('单机', self.window.status.get())
        self.window.configure_bots()
        reopened = self.window.bot_dialog
        self.assertEqual(reopened.player_id.get(), '47')
        self.assertIn('下次服务器启动', reopened.save_hint.get())
        reopened.cancel()
        self.window.start()
        self.window.session.start.assert_called_once()
        self.assertFalse(self.window.session.start.call_args.args[0]['ride'])
        self.assertEqual(json.loads(bot_config_path(self.directory).read_text())['player_id'], 47)

    def test_host_reset_defaults_keeps_draft_target_and_cancel_keeps_saved_target(self):
        save_bot_config(self.directory, {'enabled': True, 'riders': 12}, player_id=47)
        original = bot_config_path(self.directory).read_bytes()
        self.select_host()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.player_id.set('73')
        dialog.reset_defaults()
        self.assertEqual(dialog.player_id.get(), '73')
        self.assertEqual(bot_config_path(self.directory).read_bytes(), original)
        dialog.cancel()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        self.assertEqual(dialog.player_id.get(), '47')
        dialog.reset_defaults()
        dialog.save()
        saved = json.loads(bot_config_path(self.directory).read_text())
        self.assertEqual(saved['player_id'], 47)
        self.assertFalse(saved['enabled'])

    def test_invalid_host_target_keeps_file_and_selects_target_field(self):
        save_bot_config(self.directory, {'enabled': True, 'riders': 8}, player_id=47)
        original = bot_config_path(self.directory).read_bytes()
        self.select_host()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        for value in ('', '0', '-1', '47.5', 'True', '9223372036854775808'):
            with self.subTest(value=value):
                dialog.player_id.set(value)
                dialog.tabs.select(dialog.advanced_tab)
                dialog.save()
                self.assertTrue(dialog.winfo_exists())
                self.assertIn('玩家 ID', dialog.error.get())
                self.assertEqual(dialog.player_id.get(), value)
                self.assertEqual(dialog.tabs.select(), str(dialog.basic_tab))
                self.assertEqual(bot_config_path(self.directory).read_bytes(), original)
        dialog.cancel()

    def test_host_summary_updates_when_switching_from_solo_binding_warning(self):
        profile = self.directory / 'storage' / '9' / 'profile.bin'
        profile.parent.mkdir(parents=True)
        profile.write_bytes(b'solo profile')
        save_bot_config(self.directory, {'enabled': True, 'riders': 12}, player_id=47)
        self.window._refresh_bot_summary()
        self.assertIn('请打开配置并保存', self.window.bot_summary.get())
        self.select_host()
        self.assertIn('12 人', self.window.bot_summary.get())
        self.assertIn('跟随玩家 ID 47', self.window.bot_summary.get())
        self.assertNotIn('单机', self.window.bot_summary.get())

    def test_open_and_cancel_default_bot_page_does_not_write_config(self):
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        self.assertEqual(dialog.variables['riders'].get(), '8')
        self.assertFalse(dialog.variables['enabled'].get())
        dialog.variables['riders'].set('12')
        dialog.cancel()
        self.assertFalse(bot_config_path(self.directory).exists())

    def test_save_bot_page_updates_summary_and_reopens_with_saved_values(self):
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.variables['enabled'].set(True)
        dialog.variables['riders'].set('12')
        dialog.variables['scatter'].set(False)
        dialog.variables['row_gap'].set('2.4')
        dialog.save()
        self.assertFalse(dialog.winfo_exists())
        self.assertIn('12 人', self.window.bot_summary.get())
        self.assertIn('整齐列队', self.window.bot_summary.get())
        self.window.configure_bots()
        reopened = self.window.bot_dialog
        self.assertEqual(reopened.variables['riders'].get(), '12')
        self.assertEqual(reopened.variables['row_gap'].get(), '2.4')
        self.assertTrue(reopened.variables['enabled'].get())
        reopened.cancel()
        self.window.start()
        self.window.session.start.assert_called_once()
        self.assertTrue(json.loads(bot_config_path(self.directory).read_text())['enabled'])

    def test_invalid_bot_input_keeps_page_open_and_file_unchanged(self):
        save_bot_config(self.directory, {'enabled': True, 'riders': 8})
        original = bot_config_path(self.directory).read_bytes()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.variables['riders'].set('31')
        dialog.save()
        self.assertTrue(dialog.winfo_exists())
        self.assertIn('1–30', dialog.error.get())
        self.assertEqual(bot_config_path(self.directory).read_bytes(), original)
        dialog.cancel()

    def test_restore_defaults_is_only_a_draft_until_saved(self):
        save_bot_config(self.directory, {'enabled': True, 'riders': 15})
        original = bot_config_path(self.directory).read_bytes()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.reset_defaults()
        self.assertFalse(dialog.variables['enabled'].get())
        self.assertEqual(dialog.variables['riders'].get(), '8')
        self.assertEqual(bot_config_path(self.directory).read_bytes(), original)
        dialog.cancel()
        self.assertEqual(bot_config_path(self.directory).read_bytes(), original)

    def test_bot_controls_follow_formation_and_rotation_choices(self):
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        self.assertTrue(dialog.fields['row_gap'].instate(['disabled']))
        self.assertFalse(dialog.fields['scatter_width'].instate(['disabled']))
        self.assertTrue(dialog.fields['shuffle_interval'].instate(['disabled']))
        dialog.variables['scatter'].set(False)
        dialog.variables['rotate'].set(False)
        dialog._update_fields()
        self.assertFalse(dialog.fields['row_gap'].instate(['disabled']))
        self.assertTrue(dialog.fields['scatter_width'].instate(['disabled']))
        self.assertFalse(dialog.fields['shuffle_interval'].instate(['disabled']))
        self.assertTrue(dialog.fields['pull_interval'].instate(['disabled']))
        dialog.cancel()

    def test_bot_page_cannot_be_opened_during_startup(self):
        self.window._set_busy(True)
        self.assertTrue(self.window.bot_button.instate(['disabled']))
        self.window.configure_bots()
        self.assertIsNone(self.window.bot_dialog)
        self.window._set_busy(False)
        self.assertFalse(self.window.bot_button.instate(['disabled']))

    def test_host_bot_page_cannot_be_opened_during_startup(self):
        self.select_host()
        self.window._set_busy(True)
        self.window.configure_bots()
        self.assertTrue(self.window.bot_button.instate(['disabled']))
        self.assertIsNone(self.window.bot_dialog)
        self.window._set_busy(False)
        self.assertFalse(self.window.bot_button.instate(['disabled']))

    def test_corrupt_bot_file_shows_error_and_prevents_single_start(self):
        path = bot_config_path(self.directory)
        path.parent.mkdir(parents=True)
        path.write_text('broken', encoding='utf-8')
        with patch.object(gui.messagebox, 'showerror') as error:
            self.window.configure_bots()
            self.window.start()
        self.assertEqual(error.call_count, 2)
        self.assertIsNone(self.window.bot_dialog)
        self.window.session.start.assert_not_called()
        self.assertFalse(self.window.busy)
        self.assertEqual(path.read_text(encoding='utf-8'), 'broken')

    def test_corrupt_bot_file_blocks_host_but_does_not_affect_join(self):
        path = bot_config_path(self.directory)
        path.parent.mkdir(parents=True)
        path.write_text('broken', encoding='utf-8')
        self.select_host()
        with patch.object(gui.messagebox, 'showerror') as error:
            self.window.configure_bots()
            self.window.start()
        self.assertEqual(error.call_count, 2)
        self.assertIsNone(self.window.bot_dialog)
        self.window.session.start.assert_not_called()
        self.assertFalse(self.window.busy)
        self.window.mode.set('join')
        self.window.join_ip.set('192.168.1.20')
        self.window._mode_changed()
        self.window.start()
        self.window.session.start.assert_called_once()
        self.assertEqual(path.read_text(encoding='utf-8'), 'broken')

    def test_invalid_saved_target_blocks_host_start_and_can_be_repaired_in_editor(self):
        path = bot_config_path(self.directory)
        path.parent.mkdir(parents=True)
        self.select_host()
        for value in (True, 47.5, 0, -1, None, '', 9223372036854775808):
            with self.subTest(value=value):
                path.write_text(json.dumps({'enabled': True, 'player_id': value}), encoding='utf-8')
                self.window._refresh_bot_summary()
                self.assertIn('修正跟随玩家 ID', self.window.bot_summary.get())
                with patch.object(gui.messagebox, 'showerror') as error:
                    self.window.start()
                self.window.session.start.assert_not_called()
                self.assertFalse(self.window.busy)
                self.assertIn('玩家 ID', error.call_args.args[1])
                original = path.read_bytes()
                self.window.configure_bots()
                dialog = self.window.bot_dialog
                dialog.save()
                self.assertTrue(dialog.winfo_exists())
                self.assertIn('玩家 ID', dialog.error.get())
                self.assertEqual(path.read_bytes(), original)
                dialog.cancel()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.player_id.set('47')
        dialog.save()
        self.window.start()
        self.window.session.start.assert_called_once()
        self.assertEqual(json.loads(path.read_text())['player_id'], 47)

    def test_start_waits_until_bot_editor_is_saved_or_cancelled(self):
        self.window.configure_bots()
        self.window.start()
        self.window.session.start.assert_not_called()
        self.assertFalse(self.window.busy)
        self.window.bot_dialog.cancel()

    def test_disabled_parameters_keep_saved_values_instead_of_invalid_drafts(self):
        save_bot_config(self.directory, {'scatter': False, 'row_gap': 2.4,
                                        'pull_interval': 90, 'shuffle_interval': 35})
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.variables['row_gap'].set('')
        dialog.variables['shuffle_interval'].set('')
        dialog.variables['pull_interval'].set('')
        dialog.variables['scatter'].set(True)
        dialog.variables['rotate'].set(False)
        dialog.save()
        self.assertFalse(dialog.winfo_exists())
        saved = json.loads(bot_config_path(self.directory).read_text())
        self.assertEqual(saved['row_gap'], 2.4)
        self.assertEqual(saved['shuffle_interval'], 35)
        self.assertEqual(saved['pull_interval'], 90)

    def test_validation_error_selects_the_page_with_the_invalid_active_field(self):
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.variables['lateral_spacing'].set('bad')
        dialog.tabs.select(dialog.basic_tab)
        dialog.save()
        self.assertTrue(dialog.winfo_exists())
        self.assertIn('横向间距', dialog.error.get())
        self.assertEqual(dialog.tabs.select(), str(dialog.advanced_tab))
        dialog.cancel()

    def test_existing_manual_values_do_not_block_start_and_are_shown_in_editor(self):
        path = bot_config_path(self.directory)
        path.parent.mkdir(parents=True)
        path.write_text('{"enabled":false,"shuffle_interval":0,"scatter_width":8}', encoding='utf-8')
        original = path.read_bytes()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        self.assertEqual(dialog.variables['shuffle_interval'].get(), '0.0')
        self.assertEqual(dialog.variables['scatter_width'].get(), '8.0')
        dialog.cancel()
        self.assertEqual(path.read_bytes(), original)
        self.window.start()
        self.window.session.start.assert_called_once()

    def test_mismatched_player_binding_prompts_save_instead_of_claiming_active_bots(self):
        profile = self.directory / 'storage' / '47' / 'profile.bin'
        profile.parent.mkdir(parents=True)
        profile.write_bytes(b'profile')
        path = bot_config_path(self.directory)
        path.write_text('{"enabled":true,"riders":8,"player_id":1}', encoding='utf-8')
        self.window._refresh_bot_summary()
        self.assertIn('请打开配置并保存', self.window.bot_summary.get())
        self.window.configure_bots()
        self.window.bot_dialog.save()
        self.assertEqual(json.loads(path.read_text())['player_id'], 47)
        self.assertIn('8 人', self.window.bot_summary.get())

    def test_restore_defaults_resets_inactive_parameters_when_saved(self):
        save_bot_config(self.directory, {'scatter': False, 'row_gap': 2.4, 'shuffle_interval': 35})
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.reset_defaults()
        dialog.save()
        saved = json.loads(bot_config_path(self.directory).read_text())
        self.assertEqual(saved['row_gap'], 1.8)
        self.assertEqual(saved['shuffle_interval'], 25)

    def test_existing_unsafe_scatter_combination_can_be_disabled(self):
        path = bot_config_path(self.directory)
        path.parent.mkdir(parents=True)
        path.write_text('{"enabled":true,"riders":30,"scatter_width":2,"lateral_spacing":3}',
                        encoding='utf-8')
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.variables['enabled'].set(False)
        dialog.save()
        self.assertFalse(dialog.winfo_exists())
        saved = json.loads(path.read_text())
        self.assertFalse(saved['enabled'])
        self.assertEqual(saved['scatter_width'], 2)
        self.assertEqual(saved['lateral_spacing'], 3)

    def test_custom_names_save_reopen_and_cancel_preserve_line_order(self):
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.names_text.insert('1.0', '  小明  \n\nTeam  Rider')
        dialog.save()
        self.assertFalse(dialog.winfo_exists())
        saved = json.loads(bot_config_path(self.directory).read_text())
        self.assertEqual(saved['custom_names'], ['小明', '', 'Team  Rider'])
        original = bot_config_path(self.directory).read_bytes()
        self.window.configure_bots()
        reopened = self.window.bot_dialog
        self.assertEqual(reopened.names_text.get('1.0', 'end-1c'), '小明\n\nTeam  Rider')
        reopened.names_text.delete('1.0', 'end')
        reopened.names_text.insert('1.0', 'Discarded')
        reopened.cancel()
        self.assertEqual(bot_config_path(self.directory).read_bytes(), original)

    def test_invalid_custom_name_selects_names_tab_and_keeps_file(self):
        save_bot_config(self.directory, {'custom_names': ['Original']})
        original = bot_config_path(self.directory).read_bytes()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.names_text.delete('1.0', 'end')
        dialog.names_text.insert('1.0', 'Valid\n' + '长' * 41)
        dialog.tabs.select(dialog.basic_tab)
        dialog.save()
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(dialog.tabs.select(), str(dialog.names_tab))
        self.assertIn('第 2 行', dialog.error.get())
        self.assertEqual(dialog.names_text.index('insert'), '2.0')
        self.assertTrue(dialog.names_text.tag_ranges('invalid-name'))
        self.assertEqual(bot_config_path(self.directory).read_bytes(), original)
        dialog.cancel()

    def test_clearing_names_restores_random_and_reset_defaults_only_edits_draft(self):
        save_bot_config(self.directory, {'custom_names': ['Original']})
        original = bot_config_path(self.directory).read_bytes()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.reset_defaults()
        self.assertEqual(dialog.names_text.get('1.0', 'end-1c'), '')
        dialog.cancel()
        self.assertEqual(bot_config_path(self.directory).read_bytes(), original)
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.names_text.delete('1.0', 'end')
        dialog.save()
        self.assertEqual(json.loads(bot_config_path(self.directory).read_text())['custom_names'], [])

    def test_reducing_rider_count_keeps_unused_custom_names_in_editor(self):
        save_bot_config(self.directory, {'riders': 3, 'custom_names': ['One', '', 'Three']})
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.variables['riders'].set('1')
        dialog.save()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        self.assertEqual(dialog.variables['riders'].get(), '1')
        self.assertEqual(dialog.names_text.get('1.0', 'end-1c'), 'One\n\nThree')
        dialog.cancel()

    def test_pasting_thirty_names_with_a_terminating_newline_is_accepted(self):
        names = ['Rider %d' % index for index in range(1, 31)]
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.names_text.insert('1.0', '\n'.join(names) + '\n')
        dialog.save()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(json.loads(bot_config_path(self.directory).read_text())['custom_names'], names)

    def run_solo_session(self):
        self.window._set_busy(True)
        self.window.emit('running', {'mode': 'single', 'server_ip': '127.0.0.1'})
        self.window._drain()

    def test_running_solo_session_allows_bot_editor_while_other_controls_stay_frozen(self):
        self.run_solo_session()
        self.assertTrue(self.window.running)
        self.assertFalse(self.window.bot_button.instate(['disabled']))
        self.assertTrue(all(widget.instate(['disabled']) for widget in self.window.editable
                            if widget is not self.window.bot_button))
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        self.assertIn('当前骑行', dialog.save_hint.get())
        self.assertIn('当前骑行', dialog.enabled_hint.get())
        dialog.variables['enabled'].set(True)
        dialog.variables['riders'].set('5')
        dialog.save()
        self.assertIn('约 1 秒内更新', self.window.status.get())
        self.assertTrue(self.window.busy)

    def test_server_stops_while_editor_is_open_and_save_becomes_next_start_configuration(self):
        self.run_solo_session()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        self.window.emit('stopping', None)
        self.window._drain()
        self.assertFalse(self.window.running)
        self.assertTrue(self.window.bot_button.instate(['disabled']))
        self.assertIn('下次单机启动', dialog.save_hint.get())
        self.window.emit('stopped', None)
        self.window._drain()
        dialog.variables['riders'].set('7')
        dialog.save()
        self.assertIn('下次单机启动', self.window.status.get())
        self.assertEqual(json.loads(bot_config_path(self.directory).read_text())['riders'], 7)
        self.assertFalse(self.window.busy)

    def test_stop_request_disables_bot_editor_and_late_running_event_cannot_reopen_it(self):
        self.run_solo_session()
        with patch.object(self.window, '_confirm_stop', return_value=True):
            self.window.stop()
        self.window.emit('running', {'mode': 'single', 'server_ip': '127.0.0.1'})
        self.window._drain()
        self.assertFalse(self.window.running)
        self.assertTrue(self.window.stopping)
        self.window.configure_bots()
        self.assertIsNone(self.window.bot_dialog)
        self.window.session.stop.assert_called_once()

    def run_host_session(self, ride=False):
        self.select_host(ride)
        self.window._set_busy(True)
        self.window.emit('running', {'mode': 'host', 'server_ip': '192.168.1.100', 'ride': ride})
        self.window._drain()

    def test_running_host_allows_live_target_edits_while_other_controls_stay_frozen(self):
        self.run_host_session()
        self.assertTrue(self.window.running)
        self.assertFalse(self.window.bot_button.instate(['disabled']))
        self.assertTrue(all(widget.instate(['disabled']) for widget in self.window.editable
                            if widget is not self.window.bot_button))
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        self.assertIn('服务器运行中', dialog.save_hint.get())
        self.assertNotIn('单机', dialog.save_hint.get())
        dialog.variables['enabled'].set(True)
        dialog.variables['riders'].set('5')
        dialog.player_id.set('47')
        dialog.save()
        self.assertIn('约 1 秒内更新', self.window.status.get())
        self.assertIn('跟随玩家 ID 47', self.window.bot_summary.get())
        self.assertEqual(json.loads(bot_config_path(self.directory).read_text())['player_id'], 47)
        self.assertTrue(self.window.busy)

    def test_host_stops_while_editor_is_open_and_draft_is_kept_for_next_start(self):
        self.run_host_session()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.player_id.set('73')
        dialog.variables['riders'].set('7')
        self.window.emit('stopping', None)
        self.window._drain()
        self.assertTrue(dialog.winfo_exists())
        self.assertTrue(self.window.bot_button.instate(['disabled']))
        self.assertIn('下次服务器启动', dialog.save_hint.get())
        self.window.emit('stopped', None)
        self.window._drain()
        dialog.save()
        self.assertIn('下次服务器启动', self.window.status.get())
        saved = json.loads(bot_config_path(self.directory).read_text())
        self.assertEqual((saved['player_id'], saved['riders']), (73, 7))

    def test_host_stop_request_disables_editor_and_ignores_late_running_event(self):
        self.run_host_session()
        with patch.object(self.window, '_confirm_stop', return_value=True):
            self.window.stop()
        self.window.emit('running', {'mode': 'host', 'server_ip': '192.168.1.100'})
        self.window._drain()
        self.assertFalse(self.window.running)
        self.assertTrue(self.window.stopping)
        self.assertTrue(self.window.bot_button.instate(['disabled']))
        self.window.configure_bots()
        self.assertIsNone(self.window.bot_dialog)
        self.window.session.stop.assert_called_once()

    def test_join_session_never_enables_local_bot_editor(self):
        self.window.mode.set('join')
        self.window._mode_changed()
        self.window._set_busy(True)
        self.window.emit('running', {'mode': 'join', 'server_ip': '192.168.1.20'})
        self.window._drain()
        self.assertFalse(self.window.bot_frame.grid_info())
        self.assertTrue(self.window.bot_button.instate(['disabled']))
        self.window.configure_bots()
        self.assertIsNone(self.window.bot_dialog)

    def test_failure_during_editing_keeps_draft_and_switches_save_message(self):
        self.run_solo_session()
        self.window.configure_bots()
        dialog = self.window.bot_dialog
        dialog.names_text.insert('1.0', 'Saved after failure')
        with patch.object(gui.messagebox, 'showerror'):
            self.window.emit('error', 'server disconnected')
            self.window.emit('stopped', 'server disconnected')
            self.window._drain()
        self.assertTrue(dialog.winfo_exists())
        dialog.save()
        self.assertIn('下次单机启动', self.window.status.get())
        self.assertEqual(json.loads(bot_config_path(self.directory).read_text())['custom_names'],
                         ['Saved after failure'])


if __name__ == '__main__':
    unittest.main()
