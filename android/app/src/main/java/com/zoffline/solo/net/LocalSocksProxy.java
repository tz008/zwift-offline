package com.zoffline.solo.net;

import java.io.ByteArrayInputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Semaphore;
import java.util.concurrent.atomic.AtomicBoolean;

/** Loopback SOCKS5 endpoint for hev-socks5-tunnel, with TCP and RFC 1928 UDP. */
public final class LocalSocksProxy implements AutoCloseable {
    public static final String SERVER_ADDRESS = "198.18.0.1";
    public static final String DNS_ADDRESS = "198.18.0.2";
    private static final int CONNECT_TIMEOUT = 5000;
    private static final int IDLE_TIMEOUT = 300000;
    private static final int UDP_IDLE_TIMEOUT = 60000;
    private final int requestedPort;
    private final int httpPort;
    private final int httpsPort;
    private final int udpPort;
    private final int tcpPort;
    private final Set<Socket> sockets = ConcurrentHashMap.newKeySet();
    private final Set<DatagramSocket> dnsSockets = ConcurrentHashMap.newKeySet();
    private final Set<Association> associations = ConcurrentHashMap.newKeySet();
    private final Semaphore connections = new Semaphore(128);
    private final Semaphore udpAssociations = new Semaphore(32);
    private final AtomicBoolean running = new AtomicBoolean();
    private volatile ServerSocket server;
    private volatile String lastError = "";

    public LocalSocksProxy() { this(1080); }
    public LocalSocksProxy(int port) { this(port, 8080, 8443, 3024, 3025); }

    /** Alternate backend ports are useful for isolated socket integration tests. */
    public LocalSocksProxy(int port, int httpPort, int httpsPort, int udpPort, int tcpPort) {
        this.requestedPort = port;
        this.httpPort = httpPort;
        this.httpsPort = httpsPort;
        this.udpPort = udpPort;
        this.tcpPort = tcpPort;
    }

    public synchronized void start() throws IOException {
        if (running.get()) return;
        ServerSocket listener = new ServerSocket();
        try {
            listener.setReuseAddress(true);
            listener.bind(new InetSocketAddress("127.0.0.1", requestedPort), 64);
        } catch (IOException error) {
            listener.close();
            throw error;
        }
        server = listener;
        lastError = "";
        running.set(true);
        worker("zoffline-socks-listen", () -> accept(listener));
    }

    public boolean isRunning() { return running.get(); }
    public int getPort() { return server == null ? -1 : server.getLocalPort(); }
    public String getLastError() { return lastError; }

    private void accept(ServerSocket listener) {
        while (running.get()) {
            try {
                Socket client = listener.accept();
                if (!running.get() || server != listener) { client.close(); break; }
                if (!connections.tryAcquire()) { client.close(); continue; }
                sockets.add(client);
                worker("zoffline-socks-client", () -> {
                    try (Socket ignored = client) { handle(client); }
                    catch (IOException error) {
                        if (running.get() && !(error instanceof EOFException)) lastError = error.toString();
                    } finally { sockets.remove(client); connections.release(); }
                });
            } catch (IOException error) {
                if (running.get() && server == listener && !listener.isClosed()) {
                    lastError = error.toString(); close();
                }
                break;
            }
        }
    }

    private void handle(Socket client) throws IOException {
        client.setSoTimeout(CONNECT_TIMEOUT);
        client.setTcpNoDelay(true);
        DataInputStream input = new DataInputStream(client.getInputStream());
        DataOutputStream output = new DataOutputStream(client.getOutputStream());
        if (input.readUnsignedByte() != 5) throw new IOException("SOCKS version must be 5");
        int count = input.readUnsignedByte();
        boolean noAuthentication = false;
        for (int index = 0; index < count; index++) noAuthentication |= input.readUnsignedByte() == 0;
        output.write(new byte[]{5, (byte) (noAuthentication ? 0 : 255)});
        output.flush();
        if (!noAuthentication) return;
        if (input.readUnsignedByte() != 5) throw new IOException("Invalid SOCKS request version");
        int command = input.readUnsignedByte();
        if (input.readUnsignedByte() != 0) throw new IOException("Invalid SOCKS reserved field");
        Address original = readAddress(input);
        if (command == 1) {
            if (original.port == 53) {
                try { destination(original, true); }
                catch (IOException denied) { reply(output, 2, 0); return; }
                reply(output, 0, 0);
                client.setSoTimeout(IDLE_TIMEOUT);
                handleDnsTcp(input, output, original);
                return;
            }
            InetSocketAddress destination;
            try { destination = destination(original, false); }
            catch (IOException denied) { reply(output, 2, 0); return; }
            try (Socket upstream = new Socket()) {
                sockets.add(upstream);
                try {
                    try { upstream.connect(destination, CONNECT_TIMEOUT); }
                    catch (IOException failure) { reply(output, 5, 0); return; }
                    upstream.setTcpNoDelay(true);
                    upstream.setSoTimeout(IDLE_TIMEOUT);
                    client.setSoTimeout(IDLE_TIMEOUT);
                    reply(output, 0, upstream.getLocalPort());
                    Thread down = worker("zoffline-socks-down", () -> {
                        try { copy(upstream.getInputStream(), client.getOutputStream()); }
                        catch (IOException ignored) { }
                        finally { shutdownOutput(client); }
                    });
                    try { copy(input, upstream.getOutputStream()); }
                    finally { shutdownOutput(upstream); }
                    try { down.join(IDLE_TIMEOUT); }
                    catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); }
                } finally { sockets.remove(upstream); }
            } catch (IOException failure) {
                // A second SOCKS reply must never be written into an established TLS stream.
                if (!client.isClosed()) shutdownOutput(client);
                throw failure;
            }
        } else if (command == 3) {
            if (!udpAssociations.tryAcquire()) { reply(output, 1, 0); return; }
            try {
                try (Association association = new Association(client)) {
                    associations.add(association);
                    reply(output, 0, association.relay.getLocalPort());
                    client.setSoTimeout(1000);
                    worker("zoffline-socks-udp", association::receive);
                    while (running.get() && !association.closed.get()) {
                        try { if (input.read() < 0) break; }
                        catch (SocketTimeoutException ignored) { }
                    }
                }
            } finally { udpAssociations.release(); }
        } else reply(output, 7, 0);
    }

    private void handleDnsTcp(DataInputStream input, DataOutputStream output, Address target) throws IOException {
        while (running.get()) {
            int length;
            try { length = input.readUnsignedShort(); } catch (EOFException end) { return; }
            if (length == 0 || length > 65507) throw new IOException("Invalid DNS TCP frame");
            byte[] query = new byte[length];
            input.readFully(query);
            byte[] response = DnsResponder.answer(query);
            if (response == null) response = queryUpstreamDns(query, target);
            output.writeShort(response.length);
            output.write(response);
            output.flush();
        }
    }

    private byte[] queryUpstreamDns(byte[] query, Address original) throws IOException {
        InetSocketAddress target = destination(original, true);
        try (DatagramSocket socket = new DatagramSocket()) {
            dnsSockets.add(socket);
            try {
                if (!running.get()) throw new IOException("Proxy stopped");
                socket.connect(target);
                socket.setSoTimeout(CONNECT_TIMEOUT);
                socket.send(new DatagramPacket(query, query.length));
                DatagramPacket packet = new DatagramPacket(new byte[65507], 65507);
                socket.receive(packet);
                return Arrays.copyOf(packet.getData(), packet.getLength());
            } finally { dnsSockets.remove(socket); }
        }
    }

    private InetSocketAddress destination(Address address, boolean udp) throws IOException {
        if (DNS_ADDRESS.equals(address.host)) {
            if (address.port != 53) throw new IOException("Unsupported local DNS port");
            return new InetSocketAddress("8.8.8.8", 53);
        }
        boolean local = SERVER_ADDRESS.equals(address.host) || DnsResponder.isZwiftHost(address.host);
        if (local) {
            int port;
            if (!udp && address.port == 80) port = httpPort;
            else if (!udp && address.port == 443) port = httpsPort;
            // The protocol advertises base ports; clients normally add two.
            else if (udp && (address.port == 3022 || address.port == 3024)) port = udpPort;
            else if (!udp && (address.port == 3023 || address.port == 3025)) port = tcpPort;
            else throw new IOException("Unsupported local service port " + address.port);
            return new InetSocketAddress("127.0.0.1", port);
        }
        InetAddress resolved = InetAddress.getByName(address.host);
        // Never expose other local apps or private LAN services through our loopback proxy.
        if (resolved.isAnyLocalAddress() || resolved.isLoopbackAddress() || resolved.isMulticastAddress()
                || resolved.isLinkLocalAddress() || resolved.isSiteLocalAddress()) {
            throw new IOException("Private upstream address refused");
        }
        return new InetSocketAddress(resolved, address.port);
    }

    private final class Association implements AutoCloseable {
        private final DatagramSocket relay;
        private final Socket control;
        private final Map<String, UdpFlow> flows = new ConcurrentHashMap<>();
        private final AtomicBoolean closed = new AtomicBoolean();
        private volatile InetSocketAddress clientEndpoint;

        Association(Socket control) throws IOException {
            this.control = control;
            relay = new DatagramSocket(new InetSocketAddress("127.0.0.1", 0));
            relay.setSoTimeout(1000);
        }

        void receive() {
            byte[] bytes = new byte[65507];
            long lastPacket = System.nanoTime();
            while (running.get() && !closed.get()) {
                DatagramPacket packet = new DatagramPacket(bytes, bytes.length);
                try {
                    relay.receive(packet);
                    InetSocketAddress sender = (InetSocketAddress) packet.getSocketAddress();
                    if (!sender.getAddress().isLoopbackAddress()) continue;
                    if (clientEndpoint == null) clientEndpoint = sender;
                    if (!sender.equals(clientEndpoint)) continue;
                    lastPacket = System.nanoTime();
                    DataInputStream data = new DataInputStream(new ByteArrayInputStream(bytes, 0, packet.getLength()));
                    if (data.readUnsignedShort() != 0 || data.readUnsignedByte() != 0) continue;
                    Address address = readAddress(data);
                    int headerLength = packet.getLength() - data.available();
                    byte[] header = Arrays.copyOf(bytes, headerLength);
                    byte[] payload = Arrays.copyOfRange(bytes, headerLength, packet.getLength());
                    InetSocketAddress target = destination(address, true);
                    if (address.port == 53) {
                        byte[] response = DnsResponder.answer(payload);
                        if (response != null) { sendToClient(header, response); continue; }
                    }
                    String key = address.host + ":" + address.port;
                    UdpFlow flow = flows.get(key);
                    if (flow == null) {
                        if (flows.size() >= 32) continue;
                        flow = new UdpFlow(key, target, header);
                        flows.put(key, flow);
                        worker("zoffline-socks-udp-flow", flow::receive);
                    }
                    flow.upstream.send(new DatagramPacket(payload, payload.length));
                } catch (SocketTimeoutException timeout) {
                    if ((System.nanoTime() - lastPacket) / 1000000 > UDP_IDLE_TIMEOUT) break;
                } catch (IOException ignored) {
                    if (relay.isClosed()) break;
                }
            }
            close();
        }

        void sendToClient(byte[] header, byte[] data) throws IOException {
            InetSocketAddress client = clientEndpoint;
            if (client == null || header.length + data.length > 65507) return;
            byte[] result = new byte[header.length + data.length];
            System.arraycopy(header, 0, result, 0, header.length);
            System.arraycopy(data, 0, result, header.length, data.length);
            relay.send(new DatagramPacket(result, result.length, client));
        }

        @Override public void close() {
            if (!closed.compareAndSet(false, true)) return;
            relay.close();
            for (UdpFlow flow : flows.values()) flow.upstream.close();
            flows.clear();
            associations.remove(this);
            closeSocket(control);
        }

        private final class UdpFlow {
            final String key;
            final byte[] header;
            final DatagramSocket upstream;
            UdpFlow(String key, InetSocketAddress target, byte[] header) throws IOException {
                this.key = key;
                this.header = header;
                upstream = new DatagramSocket();
                upstream.connect(target);
                upstream.setSoTimeout(UDP_IDLE_TIMEOUT);
            }
            void receive() {
                byte[] bytes = new byte[65507];
                try {
                    while (running.get() && !closed.get()) {
                        DatagramPacket packet = new DatagramPacket(bytes, bytes.length);
                        upstream.receive(packet);
                        sendToClient(header, Arrays.copyOf(bytes, packet.getLength()));
                    }
                } catch (IOException ignored) { }
                finally { upstream.close(); flows.remove(key, this); }
            }
        }
    }

    private static Address readAddress(DataInputStream input) throws IOException {
        int type = input.readUnsignedByte();
        String host;
        if (type == 1 || type == 4) {
            byte[] address = new byte[type == 1 ? 4 : 16];
            input.readFully(address);
            host = InetAddress.getByAddress(address).getHostAddress();
        } else if (type == 3) {
            int length = input.readUnsignedByte();
            if (length == 0) throw new IOException("Empty SOCKS domain");
            byte[] name = new byte[length];
            input.readFully(name);
            host = new String(name, StandardCharsets.US_ASCII);
        } else throw new IOException("Unsupported SOCKS address type");
        return new Address(host, input.readUnsignedShort());
    }

    private static void reply(DataOutputStream output, int status, int port) throws IOException {
        output.write(new byte[]{5, (byte) status, 0, 1, 127, 0, 0, 1});
        output.writeShort(port);
        output.flush();
    }

    private static void copy(InputStream input, OutputStream output) throws IOException {
        byte[] buffer = new byte[16384];
        int count;
        while ((count = input.read(buffer)) != -1) { output.write(buffer, 0, count); output.flush(); }
    }

    private static void shutdownOutput(Socket socket) {
        try { socket.shutdownOutput(); } catch (IOException ignored) { }
    }

    private static void closeSocket(Socket socket) {
        try { socket.close(); } catch (IOException ignored) { }
    }

    private static Thread worker(String name, Runnable runnable) {
        Thread thread = new Thread(runnable, name);
        thread.setDaemon(true);
        thread.start();
        return thread;
    }

    @Override public synchronized void close() {
        running.set(false);
        if (server != null) try { server.close(); } catch (IOException ignored) { }
        for (Association association : associations) association.close();
        for (Socket socket : sockets) closeSocket(socket);
        for (DatagramSocket socket : dnsSockets) socket.close();
        dnsSockets.clear();
        sockets.clear();
    }

    private static final class Address {
        final String host;
        final int port;
        Address(String host, int port) { this.host = host; this.port = port; }
    }
}
