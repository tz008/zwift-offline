package com.zoffline.solo;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.VpnService;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.Message;
import android.os.Messenger;
import android.os.ParcelFileDescriptor;
import android.os.PowerManager;
import android.os.RemoteException;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import com.zoffline.solo.net.LocalSocksProxy;
import hev.htproxy.TProxyService;
import java.io.File;
import java.io.FileWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.json.JSONObject;

/** Owns Python and the VPN in a separate process; never terminates the UI process. */
public final class OfflineVpnService extends VpnService {
    static final String ACTION_START = "com.zoffline.solo.START";
    static final String ACTION_STOP = "com.zoffline.solo.STOP";
    static final String ACTION_BIND = "com.zoffline.solo.BIND";
    static final int MSG_STATUS = 1;
    private static final int NOTIFICATION_ID = 41;
    private static final String CHANNEL = "local-ride";
    private static volatile boolean processRetiring;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final Messenger messenger = new Messenger(new Handler(Looper.getMainLooper(), message -> {
        if (message.what == MSG_STATUS && message.replyTo != null) {
            Message reply = Message.obtain(null, MSG_STATUS);
            Bundle bundle = new Bundle();
            bundle.putString("status", status().toString());
            reply.setData(bundle);
            try { message.replyTo.send(reply); } catch (RemoteException ignored) {}
        }
        return true;
    }));
    private final CountDownLatch backendFinished = new CountDownLatch(1);
    private final CountDownLatch startupFinished = new CountDownLatch(1);
    private final Object resourcesLock = new Object();
    private volatile String state = "stopped";
    private volatile String detail = "尚未启动";
    private volatile boolean stopping;
    private volatile boolean everStarted;
    private volatile PyObject runtime;
    private volatile LocalSocksProxy proxy;
    private volatile ParcelFileDescriptor tunnel;
    private PowerManager.WakeLock wakeLock;
    private RuntimeOperationLock operationLock;

    @Override public void onCreate() {
        super.onCreate();
        // A binding can recreate the Service in the short interval between
        // stopSelf and process exit. Never reuse that process's interpreter.
        if (processRetiring) {
            state = "stopping";
            detail = "正在结束本次服务，请稍候";
            stopping = true;
            everStarted = true;
        }
        NotificationManager manager = getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel(CHANNEL, "本机骑行服务",
                NotificationManager.IMPORTANCE_LOW));
    }

    @Override public IBinder onBind(Intent intent) {
        return ACTION_BIND.equals(intent.getAction()) ? messenger.getBinder() : super.onBind(intent);
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) { stopSelf(); return START_NOT_STICKY; }
        if (ACTION_STOP.equals(intent.getAction())) {
            requestStop("服务已停止", false);
        } else if (ACTION_START.equals(intent.getAction()) && !everStarted) {
            everStarted = true;
            state = "starting";
            detail = "正在准备本机服务";
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(NOTIFICATION_ID, notification(detail), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
            } else {
                startForeground(NOTIFICATION_ID, notification(detail));
            }
            wakeLock = getSystemService(PowerManager.class).newWakeLock(
                    PowerManager.PARTIAL_WAKE_LOCK, "zoffline:local-ride");
            wakeLock.acquire(12 * 60 * 60 * 1000L);
            new Thread(this::startSession, "zoffline-start").start();
        }
        return START_NOT_STICKY;
    }

    private Notification notification(String text) {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 1,
                new Intent(this, OfflineVpnService.class).setAction(ACTION_STOP),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_ride).setContentTitle("zoffline 本机单机")
                .setContentText(text).setContentIntent(open).setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "停止服务", stop).build()).build();
    }

    private void startSession() {
        try {
            getPackageManager().getApplicationInfo(BuildConfig.ZWIFT_PACKAGE, 0);
            synchronized (resourcesLock) {
                if (stopping) return;
                operationLock = RuntimeOperationLock.acquire(getFilesDir());
            }
            File resources = RuntimeAssets.install(this);
            if (stopping) return;
            if (!Python.isStarted()) Python.start(new AndroidPlatform(this));
            runtime = Python.getInstance().getModule("android_runtime");
            if (stopping) { runtime.callAttr("stop"); return; }
            new Thread(() -> {
                try {
                    runtime.callAttr("run", resources.getAbsolutePath(),
                            new File(getFilesDir(), "storage").getAbsolutePath(), logsDirectory().getAbsolutePath());
                } catch (Throwable error) {
                    if (!stopping) requestStop("服务启动失败：" + readable(error), true);
                } finally { backendFinished.countDown(); }
            }, "zoffline-python").start();

            long deadline = android.os.SystemClock.elapsedRealtime() + 90000;
            while (!stopping) {
                JSONObject backend = backendStatus();
                if ("error".equals(backend.optString("state"))) throw new IllegalStateException(backend.optString("message"));
                if ("ready".equals(backend.optString("state"))) break;
                if (backendFinished.getCount() == 0) throw new IllegalStateException("服务提前退出");
                if (android.os.SystemClock.elapsedRealtime() > deadline) throw new IllegalStateException("服务启动超时，请查看日志");
                Thread.sleep(100);
            }
            if (stopping) return;
            synchronized (resourcesLock) {
            if (stopping) return;
            proxy = new LocalSocksProxy();
            proxy.start();
            Builder builder = new Builder().setSession("zoffline 本机单机")
                    .setMtu(1500).addAddress("198.18.0.3", 32)
                    .addAddress("fdfe:dcba:9876::3", 128)
                    .addRoute("0.0.0.0", 0).addRoute("::", 0)
                    .addDnsServer("198.18.0.2").addAllowedApplication(BuildConfig.ZWIFT_PACKAGE);
            builder.setMetered(false);
            tunnel = builder.establish();
            if (tunnel == null) throw new IllegalStateException("VPN 授权已取消，请重新启动并授权");
            File config = new File(getFilesDir(), "tunnel.yml");
            String yaml = "tunnel:\n  name: zoffline\n  mtu: 1500\n  multi-queue: false\n"
                    + "  ipv4: 198.18.0.3\n  ipv6: 'fdfe:dcba:9876::3'\n  icmp: 'off'\n"
                    + "socks5:\n  address: 127.0.0.1\n  port: 1080\n  udp: 'udp'\n"
                    + "misc:\n  connect-timeout: 5000\n  tcp-read-write-timeout: 300000\n"
                    + "  udp-read-write-timeout: 60000\n  log-level: warn\n  log-file: '"
                    + new File(logsDirectory(), "tunnel.log").getAbsolutePath() + "'\n";
            Files.write(config.toPath(), yaml.getBytes(StandardCharsets.UTF_8));
            if (!TProxyService.TProxyStartService(config.getAbsolutePath(), tunnel.getFd())) {
                throw new IllegalStateException("本机流量转发启动失败");
            }
            }
            Thread.sleep(250);
            if (!TProxyService.TProxyIsRunning()) throw new IllegalStateException("本机流量转发提前退出");
            synchronized (this) {
                if (stopping) return;
                state = "ready";
                detail = "本机服务与 VPN 已就绪，可以打开 Zwift";
            }
            log(detail);
            getSystemService(NotificationManager.class).notify(NOTIFICATION_ID, notification("运行中 · 等待 Zwift 连接"));
            main.postDelayed(monitor, 2000);
        } catch (Throwable error) {
            requestStop("启动失败：" + readable(error), true);
        } finally {
            if (runtime == null) backendFinished.countDown();
            startupFinished.countDown();
        }
    }

    private final Runnable monitor = new Runnable() {
        @Override public void run() {
            if (stopping || !"ready".equals(state)) return;
            if (!TProxyService.TProxyIsRunning() || proxy == null || !proxy.isRunning()
                    || backendFinished.getCount() == 0) {
                requestStop("服务意外退出，请查看日志后重新启动", true);
                return;
            }
            main.postDelayed(this, 2000);
        }
    };

    private synchronized void requestStop(String message, boolean error) {
        if (stopping) return;
        stopping = true;
        processRetiring = true;
        state = "stopping";
        detail = message;
        main.removeCallbacks(monitor);
        log(message);
        // A native or Python shutdown may stall. Only the dedicated service process
        // is ended, after a bounded opportunity to finish writes and close sockets.
        main.postDelayed(() -> android.os.Process.killProcess(android.os.Process.myPid()), 6500);
        new Thread(() -> {
            try {
                synchronized (resourcesLock) {
                    try {
                        if (TProxyService.TProxyIsRunning()) TProxyService.TProxyStopService();
                    } catch (Throwable failure) { log("停止转发：" + readable(failure)); }
                    try {
                        if (tunnel != null) tunnel.close();
                    } catch (Exception failure) { log("关闭 VPN：" + readable(failure)); }
                    finally { tunnel = null; }
                    try {
                        if (proxy != null) proxy.close();
                    } finally { proxy = null; }
                }
                if (runtime != null) runtime.callAttr("stop");
                backendFinished.await(4, TimeUnit.SECONDS);
            } catch (Throwable failure) { log("停止过程：" + readable(failure)); }
            finally {
                if (startupFinished.getCount() == 0 && backendFinished.getCount() == 0 && operationLock != null) {
                    try { operationLock.close(); } catch (Exception failure) { log("释放运行锁：" + readable(failure)); }
                    operationLock = null;
                }
                state = error ? "error" : "stopping";
                detail = message;
                main.post(() -> {
                    if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
                    stopForeground(STOP_FOREGROUND_REMOVE);
                    stopSelf();
                    main.postDelayed(() -> android.os.Process.killProcess(android.os.Process.myPid()), 700);
                });
            }
        }, "zoffline-stop").start();
    }

    private JSONObject backendStatus() {
        try { return runtime == null ? new JSONObject() : new JSONObject(runtime.callAttr("status").toString()); }
        catch (Exception error) { return new JSONObject(); }
    }

    private JSONObject status() {
        JSONObject result = new JSONObject();
        try {
            JSONObject backend = backendStatus();
            result.put("state", state).put("message", detail)
                    .put("terminating", stopping)
                    .put("client_seen", backend.optBoolean("client_seen"))
                    .put("game_login", backend.optBoolean("game_login"))
                    .put("logs", tail(new File(logsDirectory(), "android-server.log"), 7000)
                            + tail(new File(logsDirectory(), "android.log"), 1500));
            if ("ready".equals(state)) {
                long[] stats = TProxyService.TProxyGetStats();
                if (stats != null && stats.length == 4) result.put("bytes", stats[1] + stats[3]);
            }
        } catch (Exception ignored) {}
        return result;
    }

    private File logsDirectory() {
        File directory = new File(getFilesDir(), "logs");
        if (!directory.isDirectory()) directory.mkdirs();
        return directory;
    }

    private static String tail(File file, int maximum) {
        try (java.io.RandomAccessFile input = new java.io.RandomAccessFile(file, "r")) {
            input.seek(Math.max(0, input.length() - maximum));
            byte[] bytes = new byte[(int) Math.min(maximum, input.length())];
            input.readFully(bytes);
            return new String(bytes, StandardCharsets.UTF_8);
        } catch (Exception ignored) { return ""; }
    }

    private synchronized void log(String line) {
        android.util.Log.i("zoffline", line);
        try (FileWriter output = new FileWriter(new File(logsDirectory(), "android.log"), true)) {
            output.write(new java.util.Date() + " " + line + "\n");
        } catch (Exception ignored) {}
    }

    private static String readable(Throwable error) {
        String message = error.getMessage();
        return message == null || message.trim().isEmpty() ? error.getClass().getSimpleName() : message;
    }

    @Override public void onRevoke() { requestStop("VPN 已被系统或其他 VPN 关闭", false); }
    @Override public void onDestroy() {
        if (everStarted && !stopping) requestStop("服务已结束", false);
        super.onDestroy();
    }
}
