"""Windows desktop launcher for local, LAN host and LAN client sessions."""

from datetime import datetime
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from zwift_installation import find_zwift_installation
from windows_launcher import runtime_directory
from windows_network import list_lan_addresses, probe_server, validate_server_ip
from windows_session import DesktopSession, load_settings, validate_options
from windows_bot_config import (BOT_DEFAULTS, BotConfigError, load_bot_config,
                                save_bot_config, solo_player_id)


class BotConfigDialog(tk.Toplevel):
    """A modal editor; only Save applies the draft to the next server start."""

    def __init__(self, parent, directory, config, on_saved):
        super().__init__(parent)
        self.directory = directory
        self.initial_config = dict(config)
        self.defaults_restored = False
        self.on_saved = on_saved
        self.title('机器人配置 · zoffline')
        self.transient(parent)
        self.resizable(False, False)
        self.protocol('WM_DELETE_WINDOW', self.cancel)
        self.bind('<Escape>', lambda _: self.cancel())
        self.variables = {}
        for key, default in BOT_DEFAULTS.items():
            if key == 'custom_names':
                continue
            variable = tk.BooleanVar if isinstance(default, bool) else tk.StringVar
            self.variables[key] = variable(self, value=config[key])
        self.error = tk.StringVar(self)
        self.fields = {}
        self._build()
        self._update_fields()
        self.update_idletasks()
        x = max(0, parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2)
        y = max(0, parent.winfo_rooty() + (parent.winfo_height() - self.winfo_reqheight()) // 2)
        self.geometry('+%d+%d' % (x, y))
        self.grab_set()

    def _build(self):
        outer = ttk.Frame(self, padding=20)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        ttk.Label(outer, text='单机跟骑机器人', font=('Microsoft YaHei UI', 16, 'bold')).grid(
            row=0, sticky='w')
        ttk.Label(outer, text='机器人围绕你跟骑，并随你的速度和路线移动。',
                  style='Subtitle.TLabel').grid(row=1, sticky='w', pady=(4, 16))
        self.tabs = ttk.Notebook(outer)
        self.tabs.grid(row=2, sticky='nsew')
        self.basic_tab = basic = ttk.Frame(self.tabs, padding=16)
        self.advanced_tab = advanced = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(basic, text='基本设置')
        self.tabs.add(advanced, text='队形与节奏')
        self.names_tab = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(self.names_tab, text='自定义名称')
        ttk.Label(self.names_tab, text='每行对应一名机器人：第 1 行是 1 号，第 2 行是 2 号，以此类推。',
                  wraplength=440).pack(anchor='w')
        ttk.Label(self.names_tab, text='空行使用随机名称；最多 30 行，每个名称最多 40 个字符。',
                  style='Subtitle.TLabel', wraplength=440).pack(anchor='w', pady=(6, 10))
        self.names_text = scrolledtext.ScrolledText(self.names_tab, height=8, width=46,
            wrap='none', font=('Microsoft YaHei UI', 10), undo=True)
        self.names_text.pack(fill='both', expand=True)
        self.names_text.insert('1.0', '\n'.join(self.initial_config['custom_names']))
        horizontal = ttk.Scrollbar(self.names_tab, orient='horizontal', command=self.names_text.xview)
        horizontal.pack(fill='x')
        self.names_text.configure(xscrollcommand=horizontal.set)
        self.names_text.tag_configure('invalid-name', background='#fde8e8')
        ttk.Label(self.names_tab, text='减少人数会保留多出的名称；换阵容也会保留名称顺序。',
                  wraplength=440, style='Subtitle.TLabel').pack(anchor='w', pady=(10, 0))
        ttk.Checkbutton(basic, text='启用跟骑机器人', variable=self.variables['enabled']).grid(
            row=0, column=0, columnspan=3, sticky='w', pady=(0, 16))
        ttk.Label(basic, text='机器人人数').grid(row=1, column=0, sticky='w')
        self.fields['riders'] = ttk.Spinbox(basic, from_=1, to=30, width=10,
                                          textvariable=self.variables['riders'])
        self.fields['riders'].grid(row=1, column=1, sticky='w', padx=(18, 10))
        ttk.Label(basic, text='人（1–30）', style='Subtitle.TLabel').grid(row=1, column=2, sticky='w')
        ttk.Label(basic, text='跟骑队形').grid(row=2, column=0, sticky='w', pady=(18, 6))
        formation = ttk.Frame(basic)
        formation.grid(row=3, column=0, columnspan=3, sticky='w', pady=(0, 16))
        for index, (value, label) in enumerate(((True, '自然散开'), (False, '整齐列队'))):
            ttk.Radiobutton(formation, text=label, variable=self.variables['scatter'], value=value,
                            command=self._update_fields).grid(row=0, column=index, padx=(0, 28))
        ttk.Checkbutton(basic, text='自动轮流领骑', variable=self.variables['rotate'],
                        command=self._update_fields).grid(row=4, column=0, columnspan=3, sticky='w', pady=5)
        ttk.Checkbutton(basic, text='机器人主动打手势', variable=self.variables['bot_gestures']).grid(
            row=5, column=0, columnspan=3, sticky='w', pady=5)
        ttk.Label(basic, text='取消勾选并保存后，下次启动将关闭机器人，其他参数继续保留。',
                  wraplength=440, style='Subtitle.TLabel').grid(
                      row=6, column=0, columnspan=3, sticky='w', pady=(16, 4))
        for row, (key, label, unit, hint) in enumerate((
                ('row_gap', '前后排间距', '米', '整齐列队 · 0.5–10'),
                ('lateral_spacing', '横向间距', '米', '两种队形 · 0.5–3'),
                ('scatter_width', '散开宽度', '米', '自然散开 · 2–6.4'),
                ('shuffle_interval', '位置交换间隔', '秒', '整齐列队 · 至少 5 秒'),
                ('pull_interval', '领骑轮换间隔', '秒', '自动轮流领骑 · 至少 5 秒'))):
            ttk.Label(advanced, text=label).grid(row=row, column=0, sticky='w', pady=10)
            self.fields[key] = ttk.Entry(advanced, width=10, textvariable=self.variables[key])
            self.fields[key].grid(row=row, column=1, padx=(18, 10))
            ttk.Label(advanced, text=unit).grid(row=row, column=2, sticky='w')
            ttk.Label(advanced, text=hint, style='Subtitle.TLabel').grid(
                row=row, column=3, sticky='w', padx=(18, 0))
        ttk.Label(advanced, text='自然散开时，横向间距不能超过散开宽度的一半。实际间距受道路宽度影响。',
                  wraplength=440, style='Subtitle.TLabel').grid(
                      row=5, column=0, columnspan=4, sticky='w', pady=(16, 4))
        ttk.Label(outer, textvariable=self.error, foreground='#b42318', wraplength=470).grid(
            row=3, sticky='w', pady=(12, 0))
        ttk.Label(outer, text='未启用的参数保留上次保存值；保存后点击“启动 Zwift”生效。',
                  wraplength=470, style='Subtitle.TLabel').grid(row=4, sticky='w', pady=(8, 14))
        actions = ttk.Frame(outer)
        actions.grid(row=5, sticky='ew')
        ttk.Button(actions, text='恢复默认', command=self.reset_defaults).pack(side='left')
        ttk.Button(actions, text='保存配置', command=self.save).pack(side='right', padx=(10, 0))
        ttk.Button(actions, text='取消', command=self.cancel).pack(side='right')

    def _update_fields(self):
        scatter = self.variables['scatter'].get()
        self.fields['row_gap'].configure(state='disabled' if scatter else 'normal')
        self.fields['scatter_width'].configure(state='normal' if scatter else 'disabled')
        self.fields['shuffle_interval'].configure(state='disabled' if scatter else 'normal')
        self.fields['pull_interval'].configure(state='normal' if self.variables['rotate'].get() else 'disabled')

    def reset_defaults(self):
        self.defaults_restored = True
        for key, value in BOT_DEFAULTS.items():
            if key == 'custom_names':
                continue
            self.variables[key].set(value)
        self.names_text.delete('1.0', 'end')
        self.names_text.tag_remove('invalid-name', '1.0', 'end')
        self.error.set('')
        self._update_fields()

    def save(self):
        try:
            self._update_fields()
            values = {key: value.get() for key, value in self.variables.items()}
            names = self.names_text.get('1.0', 'end-1c')
            values['custom_names'] = [] if names == '' else names.split('\n')
            self.names_text.tag_remove('invalid-name', '1.0', 'end')
            active = set(values)
            for key, field in self.fields.items():
                if field.instate(['disabled']):
                    values[key] = (BOT_DEFAULTS if self.defaults_restored else self.initial_config)[key]
                    active.discard(key)
            config = save_bot_config(self.directory, values, edited_fields=active)
        except BotConfigError as exc:
            self.error.set(str(exc))
            if exc.field == 'custom_names':
                self.tabs.select(self.names_tab)
                self.names_text.focus_set()
                line = exc.line or 1
                self.names_text.mark_set('insert', '%d.0' % line)
                self.names_text.see('%d.0' % line)
                self.names_text.tag_add('invalid-name', '%d.0' % line, '%d.end' % line)
            elif exc.field in self.fields:
                self.tabs.select(self.basic_tab if exc.field == 'riders' else self.advanced_tab)
                self.fields[exc.field].focus_set()
            return
        self.on_saved(config)
        self.cancel()

    def cancel(self):
        self.grab_release()
        self.destroy()


class LauncherWindow:
    def __init__(self, root, directory=None):
        self.root = root
        self.directory = runtime_directory() if directory is None else directory
        self.events = queue.SimpleQueue()
        self.session = DesktopSession(self.directory, self.emit)
        self.busy = False
        self.checking = False
        self.closing = False
        self.addresses = []
        self.bot_dialog = None
        settings = load_settings(self.directory)
        self.mode = tk.StringVar(value=settings['mode'])
        self.host_ip = tk.StringVar(value=settings['host_ip'])
        self.join_ip = tk.StringVar(value=settings['join_ip'])
        self.ride = tk.BooleanVar(value=settings['ride'])
        self.status = tk.StringVar(value='选择运行方式后启动。')
        self.installation = tk.StringVar(value='正在检查 Zwift 安装……')
        self.adapter = tk.StringVar(value='正在读取本机网卡……')
        self.bot_summary = tk.StringVar()
        self._build()
        self._mode_changed()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(50, self._drain)
        self.refresh_addresses()
        self._background(self._discover_installation, 'installation')

    def emit(self, kind, value):
        self.events.put((kind, value))

    def _build(self):
        self.root.title('zoffline · 骑行与局域网联机')
        self.root.geometry('710x630')
        self.root.minsize(650, 600)
        style = ttk.Style(self.root)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 22, 'bold'))
        style.configure('Subtitle.TLabel', foreground='#555555')
        outer = ttk.Frame(self.root, padding=22)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(8, weight=1)
        ttk.Label(outer, text='zoffline', style='Title.TLabel').grid(row=0, sticky='w')
        ttk.Label(outer, text='选择骑行方式，连接自己的 Zwift 世界。',
                  style='Subtitle.TLabel').grid(row=1, sticky='w', pady=(0, 18))
        modes = ttk.LabelFrame(outer, text='运行方式', padding=12)
        modes.grid(row=2, sticky='ew')
        self.editable = []
        for index, (value, label) in enumerate((('single', '本机单机'),
                                                ('host', '创建局域网'), ('join', '加入局域网'))):
            widget = ttk.Radiobutton(modes, text=label, value=value, variable=self.mode,
                                     command=self._mode_changed)
            widget.grid(row=0, column=index, sticky='w', padx=(0, 35))
            self.editable.append(widget)
        self.details = ttk.Frame(outer, padding=(0, 14, 0, 8))
        self.details.grid(row=3, sticky='ew')
        self.details.columnconfigure(0, weight=1)
        self.single_frame = ttk.Frame(self.details)
        ttk.Label(self.single_frame, text='在这台电脑启动服务并打开 Zwift。').pack(anchor='w')
        bots = ttk.Frame(self.single_frame)
        bots.pack(fill='x', pady=(10, 0))
        self.bot_button = ttk.Button(bots, text='机器人配置…', command=self.configure_bots)
        self.bot_button.pack(side='left')
        ttk.Label(bots, textvariable=self.bot_summary, style='Subtitle.TLabel').pack(side='left', padx=(12, 0))
        self.editable.append(self.bot_button)
        self.host_frame = ttk.Frame(self.details)
        self.host_frame.columnconfigure(1, weight=1)
        ttk.Label(self.host_frame, text='本机网卡 / IP').grid(row=0, column=0, padx=(0, 12))
        self.host_combo = ttk.Combobox(self.host_frame, textvariable=self.host_ip, width=24)
        self.host_combo.grid(row=0, column=1, sticky='ew')
        self.host_combo.bind('<<ComboboxSelected>>', lambda _: self._adapter_changed())
        self.host_ip.trace_add('write', lambda *_: self._adapter_changed())
        refresh = ttk.Button(self.host_frame, text='刷新', command=self.refresh_addresses)
        refresh.grid(row=0, column=2, padx=(8, 0))
        self.copy_button = ttk.Button(self.host_frame, text='复制地址', command=self.copy_address)
        self.copy_button.grid(row=0, column=3, padx=(8, 0))
        ttk.Label(self.host_frame, textvariable=self.adapter,
                  style='Subtitle.TLabel').grid(row=1, column=1, columnspan=3, sticky='w', pady=(5, 8))
        ride = ttk.Checkbutton(self.host_frame, text='本机也参与骑行（需安装 Zwift）', variable=self.ride,
                               command=self._mode_changed)
        ride.grid(row=2, column=0, columnspan=4, sticky='w')
        ttk.Label(self.host_frame, text='其他玩家填写此 IP 加入；Windows 网络请设为“专用”。',
                  style='Subtitle.TLabel').grid(row=3, column=0, columnspan=4, sticky='w', pady=(8, 0))
        self.join_frame = ttk.Frame(self.details)
        self.join_frame.columnconfigure(1, weight=1)
        ttk.Label(self.join_frame, text='服务器 IP').grid(row=0, column=0, padx=(0, 12))
        entry = ttk.Entry(self.join_frame, textvariable=self.join_ip, width=28)
        entry.grid(row=0, column=1, sticky='ew')
        self.check_button = ttk.Button(self.join_frame, text='检测连接', command=self.check_connection)
        self.check_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(self.join_frame, text='填写创建者显示的 IP，每位玩家使用自己的服务器账号。',
                  style='Subtitle.TLabel').grid(row=1, column=0, columnspan=3, sticky='w', pady=(8, 0))
        self.editable += [self.host_combo, refresh, ride, entry, self.check_button]
        ttk.Label(outer, textvariable=self.installation, wraplength=650,
                  style='Subtitle.TLabel').grid(row=4, sticky='w', pady=(3, 12))
        ttk.Separator(outer).grid(row=5, sticky='ew')
        controls = ttk.Frame(outer, padding=(0, 14, 0, 14))
        controls.grid(row=6, sticky='ew')
        self.start_button = ttk.Button(controls, text='启动 Zwift', command=self.start)
        self.start_button.pack(side='left', ipadx=14, ipady=4)
        self.stop_button = ttk.Button(controls, text='停止并还原配置', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=10, ipady=4)
        ttk.Button(controls, text='退出', command=self.close).pack(side='right', ipady=4)
        ttk.Label(outer, textvariable=self.status, wraplength=650).grid(row=7, sticky='w', pady=(0, 10))
        self.log = scrolledtext.ScrolledText(outer, height=9, wrap='word', state='disabled',
                                           font=('Microsoft YaHei UI', 9), relief='solid', borderwidth=1)
        self.log.grid(row=8, sticky='nsew')
        ttk.Label(outer, text='骑行期间请保留此窗口；结束时先在游戏内保存并退出。',
                  style='Subtitle.TLabel').grid(row=9, sticky='w', pady=(10, 0))

    def _mode_changed(self):
        for frame in (self.single_frame, self.host_frame, self.join_frame):
            frame.grid_remove()
        {'single': self.single_frame, 'host': self.host_frame,
         'join': self.join_frame}[self.mode.get()].grid(row=0, column=0, sticky='ew')
        label = ('启动服务器' if self.mode.get() == 'host' and not self.ride.get() else
                 '启动服务器并骑行' if self.mode.get() == 'host' else
                 '连接并启动 Zwift' if self.mode.get() == 'join' else '启动 Zwift')
        self.start_button.configure(text=label)
        if self.mode.get() == 'single':
            self._refresh_bot_summary()

    def _refresh_bot_summary(self):
        try:
            self._set_bot_summary(load_bot_config(self.directory))
        except BotConfigError:
            self.bot_summary.set('配置无法读取，请点击“机器人配置”检查。')

    def _set_bot_summary(self, config):
        if config['enabled']:
            try:
                matches_player = int(config.get('player_id') or 1) == solo_player_id(self.directory)
            except (TypeError, ValueError, OverflowError):
                matches_player = False
            if not matches_player:
                self.bot_summary.set('机器人：请打开配置并保存，以应用到当前单机玩家。')
                return
        self.bot_summary.set('机器人：%s' % (
            '%d 人 · %s' % (max(1, min(30, config['riders'])), '自然散开' if config['scatter'] else '整齐列队')
            if config['enabled'] else '未启用'))

    def configure_bots(self):
        if self.busy or self.checking or self.mode.get() != 'single':
            return
        if self.bot_dialog is not None and self.bot_dialog.winfo_exists():
            self.bot_dialog.lift()
            return
        try:
            config = load_bot_config(self.directory)
        except BotConfigError as exc:
            messagebox.showerror('机器人配置', str(exc), parent=self.root)
            return
        self.bot_dialog = BotConfigDialog(self.root, self.directory, config, self._bots_saved)

    def _bots_saved(self, config):
        self._set_bot_summary(config)
        self.status.set('机器人配置已保存，下次单机启动时生效。')

    def _adapter_changed(self):
        names = [row['name'] for row in self.addresses if row['ip'] == self.host_ip.get()]
        self.adapter.set(' / '.join(names) if names else '请选择当前联网的网卡地址，也可手动填写。')

    def _background(self, action, kind):
        def work():
            try:
                self.emit(kind, action())
            except Exception as exc:
                self.emit(kind + '-error', str(exc))
        threading.Thread(target=work, daemon=True).start()

    def _discover_installation(self):
        installation = find_zwift_installation(self.directory)
        return 'Zwift：%s' % installation if installation else '未找到 Zwift；可以仅启动局域网服务器。'

    def refresh_addresses(self):
        if not self.busy:
            self._background(list_lan_addresses, 'addresses')

    def copy_address(self):
        try:
            ip = validate_server_ip(self.host_ip.get())
        except ValueError as exc:
            messagebox.showerror('服务器地址', str(exc), parent=self.root)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(ip)
        self.status.set('已复制服务器地址：%s' % ip)

    def check_connection(self):
        if self.busy or self.checking:
            return
        try:
            ip = validate_server_ip(self.join_ip.get())
        except ValueError as exc:
            messagebox.showerror('服务器地址', str(exc), parent=self.root)
            return
        self.checking = True
        self.check_button.configure(state='disabled')
        self.start_button.configure(state='disabled')
        self.status.set('正在检测 %s……' % ip)
        self._background(lambda: probe_server(ip), 'probe')

    def _set_busy(self, value):
        self.busy = value
        for widget in self.editable:
            widget.configure(state='disabled' if value else 'normal')
        self.start_button.configure(state='disabled' if value or self.checking else 'normal')
        self.check_button.configure(state='disabled' if value or self.checking else 'normal')
        self.stop_button.configure(state='normal' if value else 'disabled')
        if not value and self.mode.get() == 'single':
            self._refresh_bot_summary()

    def start(self):
        if self.busy or self.checking:
            return
        if self.bot_dialog is not None and self.bot_dialog.winfo_exists():
            self.bot_dialog.lift()
            return
        mode = self.mode.get()
        try:
            if mode == 'single':
                load_bot_config(self.directory)
            options = validate_options({'mode': mode, 'ride': self.ride.get(),
                                        'server_ip': self.join_ip.get() if mode == 'join' else self.host_ip.get()})
            settings = {'mode': mode, 'host_ip': self.host_ip.get(),
                        'join_ip': self.join_ip.get(), 'ride': self.ride.get()}
            self._set_busy(True)
            self.status.set('正在准备启动……')
            self.session.start(options, settings)
        except Exception as exc:
            self._set_busy(False)
            messagebox.showerror('无法启动', str(exc), parent=self.root)

    def _confirm_stop(self):
        message = ('停止服务会断开所有玩家的连接。请确认大家已在游戏内保存并退出。'
                   if self.mode.get() == 'host' else '请先在游戏内保存并退出，再停止连接并还原配置。')
        return messagebox.askyesno('停止 zoffline', message + '\n\n现在停止？', parent=self.root)

    def stop(self):
        if self.busy and self._confirm_stop() and self.busy:
            self.status.set('正在停止；若仍显示 Windows 授权窗口，请完成或取消授权。')
            self.stop_button.configure(state='disabled')
            self.session.stop()

    def close(self):
        if self.closing:
            return
        if self.busy and not self._confirm_stop():
            return
        # A confirmation dialog runs a nested Tk event loop: the session may
        # finish and deliver its terminal event while that dialog is open.
        if self.busy:
            self.closing = True
            self.status.set('正在退出并恢复配置……')
            self.stop_button.configure(state='disabled')
            self.session.stop()
        else:
            self.root.destroy()

    def _append_log(self, message):
        self.log.configure(state='normal')
        self.log.insert('end', '[%s] %s\n' % (datetime.now().strftime('%H:%M:%S'), message))
        if int(self.log.index('end-1c').split('.')[0]) > 1500:
            self.log.delete('1.0', '301.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _drain(self):
        for _ in range(200):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'log':
                self._append_log(value)
            elif kind == 'status':
                self.status.set(value)
                self._append_log(value)
            elif kind == 'running':
                text = ('局域网服务器运行中：%s' % value['server_ip'] if value['mode'] == 'host' else
                        '已连接服务器：%s' % value['server_ip'] if value['mode'] == 'join' else '本机服务运行中。')
                self.status.set(text)
                self._append_log(text)
            elif kind == 'error':
                self.closing = False
                self._append_log(value)
                messagebox.showerror('zoffline', value, parent=self.root)
            elif kind == 'stopped':
                self._set_busy(False)
                self.status.set('会话已停止，请查看错误信息。' if value else '已停止，本次临时配置已还原。')
                if self.closing:
                    self.root.destroy()
                    return
            elif kind == 'addresses':
                self.addresses = value
                self.host_combo.configure(values=[row['ip'] for row in value])
                if not self.host_ip.get() and value:
                    self.host_ip.set(value[0]['ip'])
                self._adapter_changed()
            elif kind in ('installation', 'installation-error'):
                self.installation.set(value)
            elif kind in ('probe', 'probe-error'):
                self.checking = False
                self.check_button.configure(state='disabled' if self.busy else 'normal')
                self.start_button.configure(state='disabled' if self.busy else 'normal')
                self.status.set(value)
                self._append_log(value)
            elif kind.endswith('-error'):
                self._append_log(value)
                if kind == 'addresses-error':
                    self.adapter.set('无法自动读取网卡，可手动填写本机 IPv4 地址。')
        self.root.after(50, self._drain)


def run_gui():
    root = tk.Tk()
    LauncherWindow(root)
    root.mainloop()
    return 0
