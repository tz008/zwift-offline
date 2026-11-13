package com.zoffline.solo;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/** Owns permission guidance and reports OBB readiness separately from actual game login. */
public final class CertificatePatchActivity extends Activity {
    static final String FOR_START = "prepare-for-start";
    static final String MANUALLY_CONFIGURED = "manually-configured";
    private static final int STORAGE_SETTINGS = 31;
    private static final int INSTALL_SETTINGS = 32;
    private static final int STORAGE_PERMISSION = 33;
    private static final int NOTIFICATION_PERMISSION = 34;
    private final Handler main = new Handler(Looper.getMainLooper());
    private TextView status;
    private Button check;
    private Button done;
    private boolean jobRequested;
    private boolean pendingStart;
    private boolean fromStart;

    @Override public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        fromStart = getIntent().getBooleanExtra(FOR_START, false);
        if (savedInstanceState != null) jobRequested = savedInstanceState.getBoolean("job-requested");
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        int padding = Math.round(22 * getResources().getDisplayMetrics().density);
        content.setPadding(padding, padding, padding, padding);
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
        addText(content, "Zwift 证书准备", 25);
        try {
            PackageInfo info = getPackageManager().getPackageInfo(BuildConfig.ZWIFT_PACKAGE, 0);
            addText(content, "当前游戏版本：" + info.versionName, 15);
        } catch (Exception ignored) { addText(content, "请先安装 Zwift 安卓客户端", 15); }
        addText(content, "先保存并退出 Zwift，并等待游戏更新和资源下载完成。\n\n"
                + "这里会检查当前版本、追加离线证书、校验游戏文件并保留原始资源包。"
                + "首次使用需要在系统设置中允许文件访问和安装来源，以访问游戏 OBB。"
                + "授权完成后，zoffline 会重新打开以应用文件权限。", 15);
        status = addText(content, "准备检查当前版本的游戏资源", 17);
        check = addButton(content, "检查并修复证书", this::begin);
        done = addButton(content, fromStart ? "准备完成，继续启动服务" : "准备完成，返回", () -> {
            if (!CertificatePatchService.currentVersionPrepared(this)) {
                done.setEnabled(false);
                status.setText("Zwift 版本或资源已变化，请重新检查。");
                return;
            }
            setResult(RESULT_OK);
            finish();
        });
        done.setEnabled(false);
        addText(content, "补丁不会清除游戏数据，也不会修改已解压到 Zwift 私有目录中的旧证书。"
                + "如果在打补丁前已打开当前版本，且仍提示证书错误，请先备份，再到系统设置中清除 Zwift 的应用数据以重新解压。"
                + "仅清缓存无效。资源包就绪不代表游戏已登录。", 14);
        addButton(content, "打开 Zwift 应用设置", () -> openSettings(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, 0,
                BuildConfig.ZWIFT_PACKAGE));
        if (fromStart) {
            addButton(content, "已自行配置，继续启动", () -> {
                if (CertificatePatchService.isBusy() || pendingStart) return;
                new AlertDialog.Builder(this)
                    .setTitle("使用已有证书配置")
                    .setMessage("仅在你已通过其他方式完成当前 Zwift 的证书配置时使用。服务就绪后仍需以实际游戏登录为准。")
                    .setNegativeButton("返回检查", null).setPositiveButton("继续启动", (dialog, which) -> {
                        if (CertificatePatchService.isBusy()) return;
                        setResult(RESULT_OK, new Intent().putExtra(MANUALLY_CONFIGURED, true));
                        finish();
                    }).show();
            });
        }
        addButton(content, "返回", this::finish);
        if (fromStart && savedInstanceState == null && !CertificatePatchService.isBusy()) main.post(this::begin);
    }

    private void begin() {
        if (CertificatePatchService.isBusy() || pendingStart) return;
        jobRequested = false;
        done.setEnabled(false);
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTIFICATION_PERMISSION);
            return;
        }
        continuePermissions();
    }

    private void continuePermissions() {
        if (CertificatePatchService.isBusy()) return;
        if (Build.VERSION.SDK_INT >= 30) {
            if (!Environment.isExternalStorageManager()) {
                status.setText("请允许 zoffline 访问所有文件，用于读取和修补游戏资源。");
                openSettings(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, STORAGE_SETTINGS, getPackageName());
                return;
            }
        } else if (checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.READ_EXTERNAL_STORAGE,
                    Manifest.permission.WRITE_EXTERNAL_STORAGE}, STORAGE_PERMISSION);
            return;
        }
        if (!getPackageManager().canRequestPackageInstalls()) {
            status.setText("请允许此来源，以获得系统对游戏 OBB 的文件访问权限。本操作不会安装其他应用。");
            openSettings(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, INSTALL_SETTINGS, getPackageName());
            return;
        }
        try {
            pendingStart = true;
            jobRequested = true;
            CertificatePatchService.state = "starting";
            CertificatePatchService.message = "正在启动证书检查…";
            check.setEnabled(false);
            done.setEnabled(false);
            status.setText("正在启动证书检查…");
            startForegroundService(new Intent(this, CertificatePatchService.class));
        } catch (RuntimeException error) {
            pendingStart = false;
            jobRequested = false;
            check.setEnabled(true);
            status.setText("无法启动检查：" + error.getMessage());
        }
    }

    private void openSettings(String action, int request, String targetPackage) {
        try {
            Intent intent = new Intent(action, Uri.parse("package:" + targetPackage));
            if (request == 0) startActivity(intent);
            else startActivityForResult(intent, request);
        } catch (RuntimeException error) {
            status.setText("无法打开系统设置，请在系统应用设置中授予相应权限后重试。");
        }
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == STORAGE_SETTINGS || request == INSTALL_SETTINGS) {
            boolean granted = request == STORAGE_SETTINGS
                    ? Build.VERSION.SDK_INT < 30 || Environment.isExternalStorageManager()
                    : getPackageManager().canRequestPackageInstalls();
            if (granted) {
                if (getPackageManager().canRequestPackageInstalls()) restartForStorageAccess();
                else continuePermissions();
            }
            else status.setText("尚未获得文件访问所需的权限。允许后点击“检查并修复证书”重试。");
        }
    }

    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(request, permissions, results);
        if (request == NOTIFICATION_PERMISSION) continuePermissions();
        else if (request == STORAGE_PERMISSION && results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            if (getPackageManager().canRequestPackageInstalls()) restartForStorageAccess();
            else continuePermissions();
        } else status.setText("文件访问权限未授予，请允许后重试。");
    }

    private void restartForStorageAccess() {
        startActivity(new Intent(this, StorageAccessRestartActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK)
                .putExtra(StorageAccessRestartActivity.OLD_PROCESS, android.os.Process.myPid())
                .putExtra(FOR_START, fromStart));
    }

    @Override protected void onStart() { super.onStart(); poll.run(); }
    @Override protected void onStop() { main.removeCallbacks(poll); super.onStop(); }
    @Override protected void onSaveInstanceState(Bundle saved) {
        saved.putBoolean("job-requested", jobRequested);
        super.onSaveInstanceState(saved);
    }

    private final Runnable poll = new Runnable() {
        @Override public void run() {
            boolean busy = CertificatePatchService.isBusy();
            if (pendingStart && !"starting".equals(CertificatePatchService.state)) pendingStart = false;
            if (busy) {
                pendingStart = false;
                jobRequested = true;
            }
            if (jobRequested && !pendingStart) {
                status.setText(CertificatePatchService.message);
                check.setEnabled(!busy);
                done.setEnabled(!busy && "ready".equals(CertificatePatchService.state)
                        && CertificatePatchService.currentVersionPrepared(CertificatePatchActivity.this));
            }
            main.postDelayed(this, 500);
        }
    };

    private TextView addText(LinearLayout parent, String text, int size) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextSize(size);
        view.setTextColor(Color.rgb(25, 47, 44));
        view.setPadding(0, 12, 0, 18);
        parent.addView(view);
        return view;
    }

    private Button addButton(LinearLayout parent, String text, Runnable action) {
        Button button = new Button(this);
        button.setText(text);
        button.setAllCaps(false);
        button.setOnClickListener(view -> action.run());
        parent.addView(button);
        return button;
    }
}
