package com.zoffline.solo.net;

import org.junit.Test;

import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import static org.junit.Assert.*;

public class LocalSocksProxyTest {
    @Test public void mapsTcpAndPreservesResponseAfterClientHalfClose() throws Exception {
        try (ServerSocket backend = new ServerSocket(0, 4, InetAddress.getByName("127.0.0.1"));
             LocalSocksProxy proxy = new LocalSocksProxy(0, backend.getLocalPort(), 8443, 3024, 3025)) {
            CountDownLatch finished = new CountDownLatch(1);
            AtomicReference<Throwable> failure = new AtomicReference<>();
            Thread echo = new Thread(() -> {
                try (Socket peer = backend.accept()) {
                    peer.setSoTimeout(3000);
                    ByteArrayOutputStream request = new ByteArrayOutputStream();
                    int value;
                    while ((value = peer.getInputStream().read()) != -1) request.write(value);
                    peer.getOutputStream().write(request.toByteArray());
                } catch (Throwable error) { failure.set(error); }
                finally { finished.countDown(); }
            });
            echo.setDaemon(true); echo.start();
            proxy.start();
            try (Socket client = negotiate(proxy)) {
                byte[] reply = request(client, 1, "198.18.0.1", 80);
                assertEquals(0, reply[1]);
                client.getOutputStream().write("ride".getBytes(StandardCharsets.US_ASCII));
                client.shutdownOutput();
                byte[] response = new byte[4];
                new DataInputStream(client.getInputStream()).readFully(response);
                assertEquals("ride", new String(response, StandardCharsets.US_ASCII));
                assertEquals(-1, client.getInputStream().read());
            }
            assertTrue(finished.await(3, TimeUnit.SECONDS));
            assertNull(failure.get());
        }
    }

    @Test public void directZwiftDomainUsesLocalServer() throws Exception {
        try (ServerSocket backend = new ServerSocket(0, 4, InetAddress.getByName("127.0.0.1"));
             LocalSocksProxy proxy = new LocalSocksProxy(0, backend.getLocalPort(), 8443, 3024, 3025)) {
            proxy.start();
            try (Socket client = negotiate(proxy)) {
                byte[] host = "secure.zwift.com".getBytes(StandardCharsets.US_ASCII);
                DataOutputStream output = new DataOutputStream(client.getOutputStream());
                output.write(new byte[]{5, 1, 0, 3, (byte) host.length});
                output.write(host); output.writeShort(80); output.flush();
                byte[] reply = new byte[10];
                new DataInputStream(client.getInputStream()).readFully(reply);
                assertEquals(0, reply[1]);
                backend.setSoTimeout(3000);
                try (Socket peer = backend.accept()) { assertTrue(peer.isConnected()); }
            }
        }
    }

    @Test public void baseAndAdjustedTcpTelemetryPortsReachSameBackend() throws Exception {
        try (ServerSocket backend = new ServerSocket(0, 4, InetAddress.getByName("127.0.0.1"));
             LocalSocksProxy proxy = new LocalSocksProxy(0, 8080, 8443, 3024, backend.getLocalPort())) {
            backend.setSoTimeout(3000);
            proxy.start();
            for (int port : new int[]{3023, 3025}) {
                try (Socket client = negotiate(proxy)) {
                    assertEquals(0, request(client, 1, "198.18.0.1", port)[1]);
                    try (Socket peer = backend.accept()) {
                        peer.getOutputStream().write(port & 255);
                        assertEquals(port & 255, client.getInputStream().read());
                    }
                }
            }
        }
    }

    @Test public void baseAndAdjustedUdpTelemetryPortsPreserveTheirOwnVirtualSource() throws Exception {
        try (DatagramSocket backend = new DatagramSocket(new InetSocketAddress("127.0.0.1", 0));
             LocalSocksProxy proxy = new LocalSocksProxy(0, 8080, 8443, backend.getLocalPort(), 3025);
             DatagramSocket sender = new DatagramSocket(new InetSocketAddress("127.0.0.1", 0))) {
            backend.setSoTimeout(3000); sender.setSoTimeout(3000);
            proxy.start();
            try (Socket control = negotiate(proxy)) {
                byte[] reply = request(control, 3, "0.0.0.0", 0);
                int relayPort = ((reply[8] & 255) << 8) | (reply[9] & 255);
                for (int port : new int[]{3022, 3024}) {
                    byte[] wrapped = new byte[]{0, 0, 0, 1, (byte) 198, 18, 0, 1,
                            (byte) (port >> 8), (byte) port, 42};
                    sender.send(new DatagramPacket(wrapped, wrapped.length, InetAddress.getLoopbackAddress(), relayPort));
                    DatagramPacket packet = new DatagramPacket(new byte[128], 128);
                    backend.receive(packet);
                    assertEquals(1, packet.getLength()); assertEquals(42, packet.getData()[0]);
                    backend.send(new DatagramPacket(packet.getData(), packet.getLength(), packet.getSocketAddress()));
                    sender.receive(packet);
                    assertArrayEquals(wrapped, Arrays.copyOf(packet.getData(), packet.getLength()));
                }
            }
        }
    }

    @Test public void refusesUnknownSyntheticPortsAndReleasesListener() throws Exception {
        LocalSocksProxy proxy = new LocalSocksProxy(0);
        proxy.start();
        int port = proxy.getPort();
        try (Socket client = negotiate(proxy)) {
            assertEquals(2, request(client, 1, "198.18.0.1", 22)[1]);
        }
        try (Socket client = negotiate(proxy)) {
            assertEquals(2, request(client, 1, "198.18.0.2", 443)[1]);
        }
        proxy.close();
        assertFalse(proxy.isRunning());
        try (ServerSocket rebound = new ServerSocket()) {
            rebound.setReuseAddress(true);
            rebound.bind(new InetSocketAddress("127.0.0.1", port));
            assertEquals(port, rebound.getLocalPort());
        }
    }

    @Test public void udpRelayPreservesVirtualSourceAndPayload() throws Exception {
        try (DatagramSocket backend = new DatagramSocket(new InetSocketAddress("127.0.0.1", 0));
             LocalSocksProxy proxy = new LocalSocksProxy(0, 8080, 8443, backend.getLocalPort(), 3025);
             DatagramSocket sender = new DatagramSocket(new InetSocketAddress("127.0.0.1", 0))) {
            backend.setSoTimeout(3000);
            sender.setSoTimeout(3000);
            proxy.start();
            try (Socket control = negotiate(proxy)) {
                byte[] reply = request(control, 3, "0.0.0.0", 0);
                int relayPort = ((reply[8] & 255) << 8) | (reply[9] & 255);
                byte[] header = new byte[]{0, 0, 0, 1, (byte) 198, 18, 0, 1, 11, (byte) 208}; // 3024
                byte[] wrapped = Arrays.copyOf(header, header.length + 3);
                wrapped[10] = 4; wrapped[11] = 5; wrapped[12] = 6;
                sender.send(new DatagramPacket(wrapped, wrapped.length, InetAddress.getLoopbackAddress(), relayPort));
                DatagramPacket inbound = new DatagramPacket(new byte[128], 128);
                backend.receive(inbound);
                assertArrayEquals(new byte[]{4, 5, 6}, Arrays.copyOf(inbound.getData(), inbound.getLength()));
                backend.send(new DatagramPacket(inbound.getData(), inbound.getLength(), inbound.getSocketAddress()));
                DatagramPacket returned = new DatagramPacket(new byte[128], 128);
                sender.receive(returned);
                assertArrayEquals(wrapped, Arrays.copyOf(returned.getData(), returned.getLength()));
                control.close();
                // A closed association cannot forward packets or retain its backend flow.
                backend.setSoTimeout(150);
                long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
                boolean closed = false;
                while (System.nanoTime() < deadline && !closed) {
                    sender.send(new DatagramPacket(wrapped, wrapped.length, InetAddress.getLoopbackAddress(), relayPort));
                    try { backend.receive(inbound); } catch (java.net.SocketTimeoutException expected) { closed = true; }
                }
                assertTrue(closed);
            }
        }
    }

    @Test public void dnsWorksOverUdpAndTcpWithoutInternet() throws Exception {
        byte[] query = dnsQuery("secure.zwift.com", 1);
        byte[] expected = DnsResponder.answer(query);
        assertNotNull(expected);
        try (LocalSocksProxy proxy = new LocalSocksProxy(0);
             DatagramSocket sender = new DatagramSocket(new InetSocketAddress("127.0.0.1", 0))) {
            proxy.start(); sender.setSoTimeout(3000);
            try (Socket control = negotiate(proxy)) {
                byte[] reply = request(control, 3, "0.0.0.0", 0);
                int port = ((reply[8] & 255) << 8) | (reply[9] & 255);
                byte[] wrapped = new byte[10 + query.length];
                byte[] header = new byte[]{0, 0, 0, 1, (byte) 198, 18, 0, 2, 0, 53};
                System.arraycopy(header, 0, wrapped, 0, 10);
                System.arraycopy(query, 0, wrapped, 10, query.length);
                sender.send(new DatagramPacket(wrapped, wrapped.length, InetAddress.getLoopbackAddress(), port));
                DatagramPacket response = new DatagramPacket(new byte[1024], 1024);
                sender.receive(response);
                assertArrayEquals(header, Arrays.copyOf(response.getData(), 10));
                assertArrayEquals(expected, Arrays.copyOfRange(response.getData(), 10, response.getLength()));
            }
            try (Socket client = negotiate(proxy)) {
                assertEquals(0, request(client, 1, "198.18.0.2", 53)[1]);
                DataOutputStream output = new DataOutputStream(client.getOutputStream());
                output.writeShort(query.length); output.write(query); output.flush();
                DataInputStream input = new DataInputStream(client.getInputStream());
                byte[] response = new byte[input.readUnsignedShort()]; input.readFully(response);
                assertArrayEquals(expected, response);
            }
        }
    }

    @Test public void mappedDnsSuppressesIpv6AndRejectsCompressionLoops() throws Exception {
        byte[] response = DnsResponder.answer(dnsQuery("CDN.ZWIFT.COM", 28));
        assertNotNull(response);
        assertEquals(0, response[6]); assertEquals(0, response[7]);
        assertNull(DnsResponder.answer(dnsQuery("example.com", 1)));
        assertNull(DnsResponder.answer(dnsQuery("example.com", 28)));
        assertFalse(DnsResponder.isZwiftHost("evilzwift.com"));
        assertFalse(DnsResponder.isZwiftHost("secure.zwift.com.evil.org"));
        byte[] malformed = dnsQuery("a", 1);
        malformed[12] = (byte) 0xc0; malformed[13] = 12;
        try { DnsResponder.answer(malformed); fail("Compression loop accepted"); }
        catch (java.io.IOException expected) { assertTrue(expected.getMessage().contains("loop")); }
    }

    @Test public void ipv6SocksAddressIsParsedAndPrivateDestinationsAreRefused() throws Exception {
        try (LocalSocksProxy proxy = new LocalSocksProxy(0)) {
            proxy.start();
            try (Socket client = negotiate(proxy)) {
                DataOutputStream output = new DataOutputStream(client.getOutputStream());
                output.write(new byte[]{5, 1, 0, 4});
                output.write(InetAddress.getByName("::1").getAddress());
                output.writeShort(443); output.flush();
                byte[] reply = new byte[10]; new DataInputStream(client.getInputStream()).readFully(reply);
                assertEquals(5, reply[0]); assertEquals(2, reply[1]);
            }
        }
    }

    @Test public void rapidRestartDoesNotLetOldAcceptThreadCloseNewListener() throws Exception {
        try (LocalSocksProxy proxy = new LocalSocksProxy(0)) {
            for (int index = 0; index < 12; index++) {
                proxy.start();
                try (Socket client = negotiate(proxy)) { assertTrue(client.isConnected()); }
                proxy.close();
            }
            proxy.start();
            try (Socket client = negotiate(proxy)) {
                assertEquals(2, request(client, 1, "198.18.0.1", 22)[1]);
                assertTrue(proxy.isRunning());
            }
        }
    }

    private static Socket negotiate(LocalSocksProxy proxy) throws Exception {
        Socket client = new Socket("127.0.0.1", proxy.getPort());
        client.setSoTimeout(3000);
        client.getOutputStream().write(new byte[]{5, 1, 0});
        DataInputStream input = new DataInputStream(client.getInputStream());
        assertEquals(5, input.readUnsignedByte()); assertEquals(0, input.readUnsignedByte());
        return client;
    }

    private static byte[] request(Socket client, int command, String host, int port) throws Exception {
        DataOutputStream output = new DataOutputStream(client.getOutputStream());
        output.write(new byte[]{5, (byte) command, 0, 1});
        output.write(InetAddress.getByName(host).getAddress()); output.writeShort(port); output.flush();
        byte[] reply = new byte[10]; new DataInputStream(client.getInputStream()).readFully(reply);
        return reply;
    }

    private static byte[] dnsQuery(String hostname, int type) throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        DataOutputStream output = new DataOutputStream(bytes);
        output.write(new byte[]{0x12, 0x34, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0});
        for (String label : hostname.split("\\.")) {
            byte[] encoded = label.getBytes(StandardCharsets.US_ASCII);
            output.writeByte(encoded.length); output.write(encoded);
        }
        output.writeByte(0); output.writeShort(type); output.writeShort(1);
        return bytes.toByteArray();
    }
}
