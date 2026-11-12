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
        settings = load_settings(self.directory)
        self.mode = tk.StringVar(value=settings['mode'])
        self.host_ip = tk.StringVar(value=settings['host_ip'])
        self.join_ip = tk.StringVar(value=settings['join_ip'])
        self.ride = tk.BooleanVar(value=settings['ride'])
        self.status = tk.StringVar(value='选择运行方式后启动。')
        self.installation = tk.StringVar(value='正在检查 Zwift 安装……')
        self.adapter = tk.StringVar(value='正在读取本机网卡……')
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

    def start(self):
        if self.busy or self.checking:
            return
        mode = self.mode.get()
        try:
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
