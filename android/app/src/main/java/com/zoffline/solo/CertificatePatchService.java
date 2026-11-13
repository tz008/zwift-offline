package com.zoffline.solo;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.content.pm.ServiceInfo;
import android.os.Environment;
import android.os.IBinder;
import android.os.PowerManager;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import java.io.File;
import org.json.JSONObject;

/** A foreground job in the UI process; keeps patching independent of the VPN process. */
public final class CertificatePatchService extends Service {
    private static final String CHANNEL = "certificate-preparation";
    private static final int NOTIFICATION_ID = 42;
    private static volatile boolean busy;
    static volatile String state = "idle";
    static volatile String message = "准备检查当前版本的游戏资源";
    private PowerManager.WakeLock wakeLock;

    static boolean isBusy() { return busy; }

    static boolean currentVersionPrepared(Context context) {
        try {
            PackageInfo info = context.getPackageManager().getPackageInfo(BuildConfig.ZWIFT_PACKAGE, 0);
            SharedPreferences saved = context.getSharedPreferences("certificate-preparation", MODE_PRIVATE);
            File obb = obb(info.getLongVersionCode());
            return saved.getLong("version", -1) == info.getLongVersionCode()
                    && saved.getLong("updated", -1) == info.lastUpdateTime
                    && saved.getLong("app-updated", -1) == context.getPackageManager()
                            .getPackageInfo(context.getPackageName(), 0).lastUpdateTime
                    && obb.isFile() && obb.canRead()
                    && obb.length() == saved.getLong("size", -1)
                    && obb.lastModified() == saved.getLong("modified", -1);
        } catch (Exception ignored) { return false; }
    }

    private static File obb(long version) {
        return new File(Environment.getExternalStorageDirectory(), "Android/obb/"
                + BuildConfig.ZWIFT_PACKAGE + "/main." + version + "." + BuildConfig.ZWIFT_PACKAGE + ".obb");
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) { stopSelf(startId); return START_NOT_STICKY; }
        synchronized (CertificatePatchService.class) {
            if (busy) return START_NOT_STICKY;
            busy = true;
            state = "running";
            message = "正在检查游戏证书";
        }
        try {
            getSystemService(NotificationManager.class).createNotificationChannel(
                    new NotificationChannel(CHANNEL, "Zwift 证书准备", NotificationManager.IMPORTANCE_LOW));
            startForeground(NOTIFICATION_ID, notification(message), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
            wakeLock = getSystemService(PowerManager.class).newWakeLock(
                    PowerManager.PARTIAL_WAKE_LOCK, "zoffline:certificate-preparation");
            wakeLock.acquire(30 * 60 * 1000L);
            new Thread(() -> prepare(startId), "zoffline-certificate").start();
        } catch (Exception error) {
            state = "error";
            message = readable(error);
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
            stopForeground(STOP_FOREGROUND_REMOVE);
            busy = false;
            stopSelf();
        }
        return START_NOT_STICKY;
    }

    private void prepare(int startId) {
        try (RuntimeOperationLock ignored = RuntimeOperationLock.acquire(getFilesDir())) {
            if (!getSharedPreferences("certificate-preparation", MODE_PRIVATE).edit().clear().commit()) {
                throw new IllegalStateException("无法保存检查状态，请检查应用存储空间");
            }
            PackageInfo before = getPackageManager().getPackageInfo(BuildConfig.ZWIFT_PACKAGE, 0);
            long version = before.getLongVersionCode();
            File resources = RuntimeAssets.install(this);
            if (!Python.isStarted()) Python.start(new AndroidPlatform(this));
            JSONObject result = new JSONObject(Python.getInstance().getModule("android_obb_patch").callAttr(
                    "prepare", obb(version).getAbsolutePath(),
                    new File(resources, "ssl/cert-zwift-com.pem").getAbsolutePath(),
                    new File(getFilesDir(), "certificate-patches").getAbsolutePath(), Long.toString(version), this).toString());
            PackageInfo after = getPackageManager().getPackageInfo(BuildConfig.ZWIFT_PACKAGE, 0);
            if (version != after.getLongVersionCode() || before.lastUpdateTime != after.lastUpdateTime) {
                throw new IllegalStateException("检查期间 Zwift 已更新，请重新检查当前版本");
            }
            if (!"ready".equals(result.optString("state"))) throw new IllegalStateException("证书检查未完成");
            File file = obb(version);
            boolean stored = getSharedPreferences("certificate-preparation", MODE_PRIVATE).edit()
                    .putLong("version", version).putLong("updated", after.lastUpdateTime)
                    .putLong("app-updated", getPackageManager().getPackageInfo(getPackageName(), 0).lastUpdateTime)
                    .putLong("size", file.length()).putLong("modified", file.lastModified()).commit();
            if (!stored) throw new IllegalStateException("无法保存检查结果，请检查应用存储空间");
            message = result.optString("message");
            state = "ready";
        } catch (Throwable error) {
            message = readable(error);
            state = "error";
        } finally {
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
            busy = false;
        }
    }

    /** Called from the embedded Python patcher; only plain progress text crosses this interface. */
    public void report(String text) {
        message = text;
        getSystemService(NotificationManager.class).notify(NOTIFICATION_ID, notification(text));
    }

    private Notification notification(String text) {
        PendingIntent open = PendingIntent.getActivity(this, 42,
                new Intent(this, CertificatePatchActivity.class), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL).setSmallIcon(R.drawable.ic_ride)
                .setContentTitle("Zwift 证书准备").setContentText(text).setOngoing(true).setContentIntent(open).build();
    }

    private static String readable(Throwable error) {
        String value = error.getMessage();
        return value == null || value.trim().isEmpty() ? error.getClass().getSimpleName() : value;
    }
}
