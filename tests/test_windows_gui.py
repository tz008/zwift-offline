"""GUI interaction regressions using a hidden Tk window and no system changes."""

from pathlib import Path
import json
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

    def test_bot_configuration_entry_is_available_only_in_solo_mode(self):
        self.assertTrue(self.window.single_frame.grid_info())
        self.window.mode.set('host')
        self.window._mode_changed()
        self.assertFalse(self.window.single_frame.grid_info())
        self.window.configure_bots()
        self.assertIsNone(self.window.bot_dialog)
        self.window.mode.set('join')
        self.window._mode_changed()
        self.window.configure_bots()
        self.assertIsNone(self.window.bot_dialog)

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

    def test_bot_page_cannot_be_opened_during_running_session(self):
        self.window._set_busy(True)
        self.assertTrue(self.window.bot_button.instate(['disabled']))
        self.window.configure_bots()
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


if __name__ == '__main__':
    unittest.main()
