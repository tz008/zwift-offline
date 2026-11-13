package com.zoffline.solo.net;

import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

/** Small DNS responder for the Zwift names redirected by the local VPN. */
public final class DnsResponder {
    private DnsResponder() {}

    public static boolean isZwiftHost(String name) {
        String normalized = name.toLowerCase(Locale.ROOT);
        while (normalized.endsWith(".")) normalized = normalized.substring(0, normalized.length() - 1);
        return normalized.equals("zwift.com") || normalized.endsWith(".zwift.com");
    }

    /** Returns null for names which must be resolved by the upstream DNS server. */
    public static byte[] answer(byte[] query) throws IOException {
        if (query.length < 12) throw new IOException("Truncated DNS header");
        if ((query[2] & 0xf8) != 0 || unsignedShort(query, 4) != 1) {
            throw new IOException("Unsupported DNS request");
        }
        Name name = readName(query, 12, new HashSet<Integer>());
        if (name.end + 4 > query.length) throw new IOException("Truncated DNS question");
        int type = unsignedShort(query, name.end);
        int recordClass = unsignedShort(query, name.end + 2);
        if (!isZwiftHost(name.value)) return null;
        boolean ipv4Answer = recordClass == 1 && (type == 1 || type == 255);
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        DataOutputStream output = new DataOutputStream(buffer);
        output.write(query, 0, 2);
        output.writeShort(0x8080 | (unsignedShort(query, 2) & 0x0110)); // QR, RA, RD/CD.
        output.writeShort(1);
        output.writeShort(ipv4Answer ? 1 : 0);
        output.writeShort(0);
        output.writeShort(0);
        for (String label : name.value.split("\\.")) {
            byte[] encoded = label.getBytes(StandardCharsets.US_ASCII);
            output.writeByte(encoded.length);
            output.write(encoded);
        }
        output.writeByte(0);
        output.writeShort(type);
        output.writeShort(recordClass);
        if (ipv4Answer) {
            output.writeShort(0xc00c);
            output.writeShort(1);
            output.writeShort(1);
            output.writeInt(30);
            output.writeShort(4);
            output.write(new byte[]{(byte) 198, 18, 0, 1});
        }
        return buffer.toByteArray();
    }

    private static Name readName(byte[] data, int position, Set<Integer> visited) throws IOException {
        StringBuilder value = new StringBuilder();
        int end = -1;
        int total = 0;
        while (position < data.length && visited.size() < 128) {
            if (!visited.add(position)) throw new IOException("DNS compression loop");
            int size = data[position++] & 255;
            if (size == 0) return new Name(value.toString(), end < 0 ? position : end);
            if ((size & 0xc0) == 0xc0) {
                if (position >= data.length) throw new IOException("Truncated DNS compression pointer");
                int target = ((size & 0x3f) << 8) | (data[position++] & 255);
                if (end < 0) end = position;
                position = target;
                continue;
            }
            if (size > 63 || position + size > data.length || (total += size + 1) > 254) {
                throw new IOException("Invalid DNS name");
            }
            if (value.length() != 0) value.append('.');
            value.append(new String(data, position, size, StandardCharsets.US_ASCII));
            position += size;
        }
        throw new IOException("Invalid DNS question");
    }

    private static int unsignedShort(byte[] data, int offset) {
        return ((data[offset] & 255) << 8) | (data[offset + 1] & 255);
    }

    private static final class Name {
        final String value;
        final int end;
        Name(String value, int end) { this.value = value; this.end = end; }
    }
}
