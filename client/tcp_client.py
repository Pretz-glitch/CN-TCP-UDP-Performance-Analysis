#!/usr/bin/env python3
"""Instrumented TCP client (Phase 2).

Times from connect() until the 1-byte application ACK is received
(client-side transfer time) and prints it for the driver to merge into
the run's CSV row.

Usage: tcp_client.py <server_ip> [port] [bytes]
"""
import socket
import sys
import time

ip = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) > 2 else 50001
nbytes = int(sys.argv[3]) if len(sys.argv) > 3 else 1_000_000
CHUNK = 65536

payload = b"X" * CHUNK
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
t0 = time.perf_counter()
s.connect((ip, port))
sent = 0
while sent < nbytes:
    n = min(CHUNK, nbytes - sent)
    s.sendall(payload[:n])
    sent += n
# Signal end-of-data WITHOUT closing: FIN lets the server's recv() see b"",
# while keeping our side open to receive the application ACK.
s.shutdown(socket.SHUT_WR)
s.recv(1)  # application ACK
client_time = time.perf_counter() - t0
s.close()

print(f"TCP_CLIENT sent={sent} client_time_s={client_time:.6f}", flush=True)
print(f"SENDER proto=tcp client_time_s={client_time:.6f} bytes_sent={sent}", flush=True)
