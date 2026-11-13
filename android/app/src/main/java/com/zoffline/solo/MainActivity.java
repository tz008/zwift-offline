package com.zoffline.solo;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ComponentName;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.net.VpnService;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.Message;
import android.os.Messenger;
import android.os.RemoteException;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;
import java.io.File;
import java.io.FileInputStream;
import java.io.OutputStream;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;
import java.util.concurrent.atomic.AtomicBoolean;
import org.json.JSONObject;

public final class MainActivity extends Activity {
    private static final int VPN_REQUEST = 20;
    private static final int BACKUP_REQUEST = 21;
    private static final int INK = Color.rgb(25, 47, 44);
    private static final int TEAL = Color.rgb(0, 121, 107);
    private final Handler main = new Handler(Looper.getMainLooper());
    private Messenger service;
    private boolean bound;
    private boolean active;
    private boolean ready;
    private boolean statusKnown;
    private boolean startPending;
    private static final AtomicBoolean exporting = new AtomicBoolean();
    private String lastState = "stopped";
    private TextView statusTitle;
    private TextView statusDetail;
    private TextView connectionStatus;
    private TextView logText;
    private Button start;
    private Button stop;
    private Button launch;
    private Button settings;
    private Button backup;
    private final Messenger replies = new Messenger(new Handler(Looper.getMainLooper(), message -> {
        if (message.what == OfflineVpnService.MSG_STATUS) {
            try { render(new JSONObject(message.getData().getString("status", "{}"))); }
            catch (Exception ignored) {}
        }
        return true;
    }));
    private final ServiceConnection connection = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            service = new Messenger(binder);
            poll.run();
        }
        @Override public void onServiceDisconnected(ComponentName name) {
            service = null;
            active = false;
            ready = false;
            statusKnown = false;
            startPending = false;
            if (!"error".equals(lastState)) {
                statusTitle.setText("服务已停止");
                statusDetail.setText("可以再次启动；玩家存档已保留。");
            }
            updateButtons();
        }
    };

    @Override public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(dp(24), dp(20), dp(24), dp(28));
        content.setBackgroundColor(Color.rgb(244, 246, 245));
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.addView(content);
        scroll.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                    insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets;
        });
        setContentView(scroll);
        TextView eyebrow = label("LOCAL RIDE  /  本机单机", 12, TEAL);
        eyebrow.setLetterSpacing(0.12f);
        content.addView(eyebrow);
        TextView title = label("zoffline", 38, INK);
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        content.addView(title);
        addText(content, "手机上的离线骑行服务", 16, Color.DKGRAY, 3, 24);

        LinearLayout card = card();
        statusTitle = label("尚未启动", 22, INK);
        statusTitle.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        card.addView(statusTitle);
        statusDetail = label("启动后可在这台手机上打开 Zwift 骑行。", 14, Color.DKGRAY);
        add(card, statusDetail, 10, 0);
        connectionStatus = label("等待游戏连接", 13, TEAL);
        add(card, connectionStatus, 16, 0);
        content.addView(card);

        start = button("启动本机服务", true, this::startRide);
        add(content, start, 18, 0);
        launch = button("打开 Zwift", false, () -> {
            Intent intent = getPackageManager().getLaunchIntentForPackage(BuildConfig.ZWIFT_PACKAGE);
            if (intent == null) toast("请先安装 Zwift 安卓客户端");
            else startActivity(intent);
        });
        add(content, launch, 8, 0);
        settings = button("地图与幽灵骑手设置", false,
                () -> startActivity(new Intent(this, SettingsActivity.class)));
        add(content, settings, 8, 0);
        stop = button("停止服务", false, () -> new AlertDialog.Builder(this)
                .setTitle("停止本机服务？").setMessage("请先在 Zwift 中保存并退出骑行，再停止服务。")
                .setNegativeButton("继续运行", null).setPositiveButton("停止服务", (dialog, which) -> {
                    stop.setEnabled(false);
                    startService(new Intent(this, OfflineVpnService.class).setAction(OfflineVpnService.ACTION_STOP));
                }).show());
        add(content, stop, 8, 0);

        LinearLayout preparation = card();
        addText(preparation, "第一次使用", 17, INK, 0, 8);
        addText(preparation, "1. 安装 Zwift，并准备好所需游戏资源。\n"
                + "2. 完成适用于当前 Zwift 版本的证书补丁。\n"
                + "3. 启动服务、允许 VPN，然后打开 Zwift。\n"
                + "单机模式可用任意邮箱和密码登录；账号与存档仅在本机。",
                14, Color.DKGRAY, 0, 12);
        Button help = button("查看证书补丁说明", false, this::showHelp);
        preparation.addView(help);
        add(content, preparation, 24, 0);

        backup = button("导出玩家存档", false, this::chooseBackup);
        add(content, backup, 14, 0);
        addText(content, "更新同一签名的 APK 会保留进度；卸载应用会删除本机存档。", 12, Color.GRAY, 8, 22);

        TextView logTitle = label("运行日志", 16, INK);
        logTitle.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        content.addView(logTitle);
        logText = label("启动后在这里显示服务日志。", 11, Color.DKGRAY);
        logText.setTypeface(Typeface.MONOSPACE);
        logText.setTextIsSelectable(true);
        add(content, logText, 10, 18);
        addText(content, "v" + BuildConfig.VERSION_NAME + " · ARM64 / x86_64 · Android 10+", 11, Color.GRAY, 0, 0);
        TextView licenses = label("开源许可", 12, TEAL);
        licenses.setOnClickListener(view -> new AlertDialog.Builder(this).setTitle("开源许可")
                .setMessage("zoffline：AGPL-3.0\nhev-socks5-tunnel：MIT\n"
                        + "Python 与第三方依赖遵循各自许可证。\n\n完整许可证随 APK 资源附带；"
                        + "分发修改版时请同时提供对应源码。")
                .setPositiveButton("知道了", null).show());
        add(content, licenses, 8, 0);
        updateButtons();
    }

    private void startRide() {
        if (exporting.get() || !statusKnown || active) return;
        if (!zwiftInstalled()) { toast("请先安装 Zwift 安卓客户端"); return; }
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 22);
            return;
        }
        prepareVpn();
    }

    private void prepareVpn() {
        Intent preparation = VpnService.prepare(this);
        if (preparation != null) startActivityForResult(preparation, VPN_REQUEST);
        else startServer();
    }

    @Override public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == 22) prepareVpn();
    }

    private void startServer() {
        if (exporting.get()) { toast("正在导出存档，请完成后再启动"); return; }
        startPending = true;
        lastState = "starting";
        active = true;
        statusTitle.setText("正在启动");
        statusDetail.setText(R.string.preparing_service);
        updateButtons();
        try {
            startForegroundService(new Intent(this, OfflineVpnService.class).setAction(OfflineVpnService.ACTION_START));
        } catch (RuntimeException error) {
            active = false;
            startPending = false;
            statusTitle.setText("启动失败");
            statusDetail.setText(error.getMessage());
            updateButtons();
        }
    }

    @Override protected void onStart() {
        super.onStart();
        statusKnown = false;
        updateButtons();
        bound = bindService(new Intent(this, OfflineVpnService.class).setAction(OfflineVpnService.ACTION_BIND),
                connection, BIND_AUTO_CREATE);
    }

    @Override protected void onStop() {
        main.removeCallbacks(poll);
        if (bound) { unbindService(connection); bound = false; }
        service = null;
        super.onStop();
    }

    private final Runnable poll = new Runnable() {
        @Override public void run() {
            main.removeCallbacks(this);
            if (service != null) {
                Message request = Message.obtain(null, OfflineVpnService.MSG_STATUS);
                request.replyTo = replies;
                try { service.send(request); } catch (RemoteException ignored) {}
            }
            if (bound) main.postDelayed(this, 1000);
        }
    };

    private void render(JSONObject status) {
        String state = status.optString("state", "stopped");
        if (startPending && "stopped".equals(state)) return;
        startPending = false;
        statusKnown = true;
        if ("stopped".equals(state) && "error".equals(lastState)) {
            active = false;
            ready = false;
            updateButtons();
            return;
        }
        lastState = state;
        ready = "ready".equals(state);
        active = ready || "starting".equals(state) || "stopping".equals(state) || status.optBoolean("terminating");
        String title = switch (state) {
            case "starting" -> "正在启动";
            case "ready" -> "本机服务运行中";
            case "stopping" -> "正在停止";
            case "error" -> "启动或运行失败";
            default -> "服务已停止";
        };
        statusTitle.setText(title);
        statusDetail.setText(status.optString("message", ""));
        if (status.optBoolean("game_login")) {
            connectionStatus.setText(R.string.game_login_received);
        } else if (status.optBoolean("client_seen")) {
            connectionStatus.setText("已收到游戏请求，等待登录完成");
        } else {
            connectionStatus.setText(ready ? "等待 Zwift 连接 · 尚未确认游戏证书配置" : "等待游戏连接");
        }
        String logs = status.optString("logs");
        if (!logs.isEmpty()) logText.setText(logs);
        updateButtons();
    }

    private void updateButtons() {
        if (start == null) return;
        start.setEnabled(statusKnown && !active && !exporting.get());
        stop.setEnabled(active && !"stopping".equals(lastState));
        launch.setEnabled(ready);
        settings.setEnabled(ready);
        backup.setEnabled(statusKnown && !active && !exporting.get());
    }

    private boolean zwiftInstalled() {
        try { getPackageManager().getApplicationInfo(BuildConfig.ZWIFT_PACKAGE, 0); return true; }
        catch (PackageManager.NameNotFoundException ignored) { return false; }
    }

    private void showHelp() {
        new AlertDialog.Builder(this).setTitle("Zwift 证书准备")
                .setMessage("本 APK 提供本地服务和 VPN。Zwift 还需要接受本地服务器证书。\n\n"
                        + "可以先尝试 ZofflineObb v1.5。它更新到 API 36，但 Android 17 上仍需实际验证。"
                        + "手机端补丁失败时，可参考项目 issue #505，通过电脑和 ADB 处理 OBB。\n\n"
                        + "补丁适用于对应的 Zwift 版本；游戏重新安装或更新后需要重新检查。"
                        + "系统 VPN 授权或安装用户 CA 都不能单独证明补丁成功。")
                .setPositiveButton("新版补丁工具", (dialog, which) -> openUrl(
                        "https://github.com/fatsbrown/ZofflineObbAndroid/releases/latest"))
                .setNeutralButton("电脑补丁说明", (dialog, which) -> openUrl(
                        "https://github.com/zoffline/zwift-offline/issues/505"))
                .setNegativeButton("返回", null).show();
    }

    private void openUrl(String url) {
        try { startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url))); }
        catch (RuntimeException ignored) { toast("未找到浏览器"); }
    }

    private void chooseBackup() {
        if (!statusKnown || active || exporting.get()) return;
        if (!new File(getFilesDir(), "storage").isDirectory()) { toast("还没有玩家存档"); return; }
        Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT).setType("application/zip")
                .addCategory(Intent.CATEGORY_OPENABLE).putExtra(Intent.EXTRA_TITLE, "zoffline-storage.zip");
        startActivityForResult(intent, BACKUP_REQUEST);
    }

    @Override protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == VPN_REQUEST) {
            if (resultCode == RESULT_OK) startServer();
            else toast("未获得 VPN 授权，服务尚未启动");
        } else if (requestCode == BACKUP_REQUEST && resultCode == RESULT_OK && data != null && data.getData() != null) {
            if (active || !exporting.compareAndSet(false, true)) { toast("请先停止服务再导出存档"); return; }
            Uri destination = data.getData();
            updateButtons();
            new Thread(() -> {
                try (OutputStream stream = getContentResolver().openOutputStream(destination);
                     ZipOutputStream zip = new ZipOutputStream(stream)) {
                    zipDirectory(new File(getFilesDir(), "storage"), "storage/", zip);
                    runOnUiThread(() -> toast("存档已导出，请妥善保存"));
                } catch (Exception error) {
                    runOnUiThread(() -> toast("导出失败：" + error.getMessage()));
                } finally { exporting.set(false); runOnUiThread(this::updateButtons); }
            }, "zoffline-backup").start();
        }
    }

    private static void zipDirectory(File folder, String prefix, ZipOutputStream zip) throws Exception {
        File[] files = folder.listFiles();
        if (files == null) return;
        byte[] buffer = new byte[32768];
        for (File file : files) {
            if (file.isDirectory()) zipDirectory(file, prefix + file.getName() + "/", zip);
            else {
                zip.putNextEntry(new ZipEntry(prefix + file.getName()));
                try (FileInputStream input = new FileInputStream(file)) {
                    int read;
                    while ((read = input.read(buffer)) != -1) zip.write(buffer, 0, read);
                }
                zip.closeEntry();
            }
        }
    }

    private void toast(String text) { Toast.makeText(this, text, Toast.LENGTH_LONG).show(); }
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private TextView label(String text, int size, int color) {
        TextView view = new TextView(this);
        view.setText(text); view.setTextSize(size); view.setTextColor(color);
        view.setLineSpacing(dp(3), 1);
        return view;
    }
    private LinearLayout card() {
        LinearLayout view = new LinearLayout(this);
        view.setOrientation(LinearLayout.VERTICAL);
        view.setPadding(dp(20), dp(20), dp(20), dp(20));
        GradientDrawable background = new GradientDrawable();
        background.setColor(Color.WHITE); background.setCornerRadius(dp(20));
        view.setBackground(background);
        return view;
    }
    private Button button(String title, boolean primary, Runnable action) {
        Button view = new Button(this);
        view.setText(title); view.setTextSize(15); view.setAllCaps(false); view.setMinHeight(dp(52));
        view.setTextColor(primary ? Color.WHITE : TEAL);
        view.setBackgroundTintList(android.content.res.ColorStateList.valueOf(primary ? TEAL : Color.WHITE));
        view.setOnClickListener(button -> action.run());
        return view;
    }
    private void add(LinearLayout parent, View child, int top, int bottom) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.setMargins(0, dp(top), 0, dp(bottom));
        parent.addView(child, params);
    }
    private void addText(LinearLayout parent, String text, int size, int color, int top, int bottom) {
        add(parent, label(text, size, color), top, bottom);
    }
}
