#!/usr/bin/env python3
"""Bare UDP server (Phase 1): receive datagrams until idle timeout, report counts.
Usage: udp_server.py [port]
No sequencing yet — that comes in Phase 2.
"""
import socket
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 50002

srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
srv.bind(("0.0.0.0", PORT))
srv.settimeout(5.0)
count, total = 0, 0
try:
    while True:
        data, _ = srv.recvfrom(65535)
        count += 1
        total += len(data)
except socket.timeout:
    pass
srv.close()
print(f"UDP_SERVER_DONE datagrams={count} bytes={total}")
