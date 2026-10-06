#!/usr/bin/env python3
"""Bare TCP server (Phase 1): listen, receive until close, report byte count.
Usage: tcp_server.py [port]   (binds 0.0.0.0; run inside server netns)
No timing, no CSV yet — that comes in Phase 2.
"""
import socket
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 50001
CHUNK = 65536

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", PORT))
srv.listen(1)
conn, addr = srv.accept()
total = 0
while True:
    data = conn.recv(CHUNK)
    if not data:
        break
    total += len(data)
conn.close()
srv.close()
print(f"TCP_SERVER_DONE bytes={total}")
