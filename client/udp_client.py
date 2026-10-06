#!/usr/bin/env python3
"""Bare UDP client (Phase 1): send N bytes as fixed-size datagrams, no pacing.
Usage: udp_client.py <server_ip> [port] [bytes]
"""
import socket
import sys

ip = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) > 2 else 50002
nbytes = int(sys.argv[3]) if len(sys.argv) > 3 else 1_000_000
DGRAM = 1200

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
payload = b"Y" * DGRAM
sent, count = 0, 0
while sent < nbytes:
    n = min(DGRAM, nbytes - sent)
    s.sendto(payload[:n], (ip, port))
    sent += n
    count += 1
s.close()
print(f"UDP_CLIENT_DONE datagrams={count} sent={sent}")
