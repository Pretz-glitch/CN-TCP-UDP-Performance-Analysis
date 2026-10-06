#!/usr/bin/env python3
"""Bare TCP client (Phase 1): connect, send N bytes, close.
Usage: tcp_client.py <server_ip> [port] [bytes]
"""
import socket
import sys

ip = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) > 2 else 50001
nbytes = int(sys.argv[3]) if len(sys.argv) > 3 else 1_000_000
CHUNK = 65536

payload = b"X" * CHUNK
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.connect((ip, port))
sent = 0
while sent < nbytes:
    n = min(CHUNK, nbytes - sent)
    s.sendall(payload[:n])
    sent += n
s.close()
print(f"TCP_CLIENT_DONE sent={sent}")
