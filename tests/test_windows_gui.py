"""GUI interaction regressions using a hidden Tk window and no system changes."""

from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import windows_gui as gui


class WindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.background = patch.object(gui.LauncherWindow, '_background').start()
        self.addCleanup(patch.stopall)
        self.window = gui.LauncherWindow(self.root, self.directory)
        self.window.session = Mock()

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


if __name__ == '__main__':
    unittest.main()
