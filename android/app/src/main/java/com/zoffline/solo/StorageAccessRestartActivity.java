package com.zoffline.solo;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.os.Process;

/** Applies installer storage mounts by opening the UI in a fresh process after permission grants. */
public final class StorageAccessRestartActivity extends Activity {
    static final String OLD_PROCESS = "storage-old-process";

    @Override public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        int previous = getIntent().getIntExtra(OLD_PROCESS, -1);
        if (previous > 0 && previous != Process.myPid()) Process.killProcess(previous);
        startActivity(new Intent(this, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK)
                .putExtra(MainActivity.RESUME_CERTIFICATE,
                        getIntent().getBooleanExtra(CertificatePatchActivity.FOR_START, false)));
        finish();
        Process.killProcess(Process.myPid());
    }
}
