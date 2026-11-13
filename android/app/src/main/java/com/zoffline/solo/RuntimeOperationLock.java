package com.zoffline.solo;

import java.io.Closeable;
import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.channels.FileLock;
import java.nio.channels.OverlappingFileLockException;

/** Serializes resource preparation and the server across their separate processes. */
final class RuntimeOperationLock implements Closeable {
    private final RandomAccessFile file;
    private final FileLock lock;

    private RuntimeOperationLock(RandomAccessFile file, FileLock lock) {
        this.file = file;
        this.lock = lock;
    }

    static RuntimeOperationLock acquire(File directory) throws IOException {
        RandomAccessFile file = new RandomAccessFile(new File(directory, "runtime-operation.lock"), "rw");
        try {
            FileLock lock = file.getChannel().tryLock();
            if (lock == null) throw new IOException("请先停止本机服务，或等待证书检查完成");
            return new RuntimeOperationLock(file, lock);
        } catch (IOException | OverlappingFileLockException error) {
            file.close();
            throw new IOException("请先停止本机服务，或等待证书检查完成", error);
        }
    }

    @Override public void close() throws IOException {
        try { lock.release(); } finally { file.close(); }
    }
}
