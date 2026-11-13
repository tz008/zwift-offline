package com.zoffline.solo;

import android.content.Context;
import java.io.File;
import java.io.ByteArrayOutputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/** Extracts only the generated public runtime, outside the player's storage. */
final class RuntimeAssets {
    private RuntimeAssets() {}

    static File install(Context context) throws IOException, NoSuchAlgorithmException {
        String expected;
        try (InputStream input = context.getAssets().open("runtime/runtime.sha256")) {
            ByteArrayOutputStream text = new ByteArrayOutputStream();
            byte[] buffer = new byte[256];
            int read;
            while ((read = input.read(buffer)) != -1) text.write(buffer, 0, read);
            expected = text.toString(StandardCharsets.US_ASCII.name()).trim();
        }
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (InputStream input = context.getAssets().open("runtime/runtime.zip")) {
            byte[] buffer = new byte[32768];
            int read;
            while ((read = input.read(buffer)) != -1) digest.update(buffer, 0, read);
        }
        StringBuilder actual = new StringBuilder();
        for (byte value : digest.digest()) actual.append(String.format("%02x", value & 255));
        if (!expected.matches("[a-f0-9]{64}") || !actual.toString().equals(expected)) {
            throw new IOException("APK 服务资源校验失败，请重新安装完整 APK");
        }
        File root = new File(context.getFilesDir(), "runtime/" + expected);
        File marker = new File(root, ".complete");
        if (marker.isFile()) return root;
        if (!root.isDirectory() && !root.mkdirs()) throw new IOException("无法创建服务资源目录");
        String prefix = root.getCanonicalPath() + File.separator;
        try (ZipInputStream zip = new ZipInputStream(context.getAssets().open("runtime/runtime.zip"))) {
            ZipEntry entry;
            byte[] buffer = new byte[32768];
            while ((entry = zip.getNextEntry()) != null) {
                File destination = new File(root, entry.getName());
                if (!destination.getCanonicalPath().startsWith(prefix)) throw new IOException("非法资源路径");
                if (entry.isDirectory()) {
                    if (!destination.isDirectory() && !destination.mkdirs()) throw new IOException("无法创建目录");
                } else {
                    File parent = destination.getParentFile();
                    if (!parent.isDirectory() && !parent.mkdirs()) throw new IOException("无法创建目录");
                    try (FileOutputStream output = new FileOutputStream(destination)) {
                        int read;
                        while ((read = zip.read(buffer)) != -1) output.write(buffer, 0, read);
                    }
                }
            }
        }
        Files.write(marker.toPath(), expected.getBytes(StandardCharsets.US_ASCII));
        return root;
    }
}
