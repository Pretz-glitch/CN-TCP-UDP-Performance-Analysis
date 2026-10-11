#!/usr/bin/env python3
"""Instrumented TCP server (Phase 2).

Records t_first_byte / t_last_byte, byte count, sends a 1-byte application
ACK, and prints a machine-parseable RESULT line (schema shared with the
Phase 3 driver; see README "CSV schema").

Usage: tcp_server.py [port]
"""
import socket
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 50001
CHUNK = 65536

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", PORT))
srv.listen(1)
conn, _ = srv.accept()

total = 0
t_first = None
t_last = None
while True:
    data = conn.recv(CHUNK)
    if not data:
        break
    now = time.perf_counter()
    if t_first is None:
        t_first = now
    t_last = now
    total += len(data)

transfer = (t_last - t_first) if (t_first is not None and t_last != t_first) else 0.0
goodput = (8.0 * total / transfer / 1e6) if transfer > 0 else 0.0

conn.sendall(b"\x00")  # 1-byte application ACK
conn.close()
srv.close()

print(f"TCP_SERVER transfer_time_s={transfer:.6f} bytes={total} goodput_mbps={goodput:.3f}", flush=True)
print(f"RESULT proto=tcp transfer_time_s={transfer:.6f} client_time_s= "
      f"bytes_delivered={total} goodput_mbps={goodput:.3f} "
      f"packets_sent= packets_received= loss_pct_obs= duplicates= out_of_order=",
      flush=True)
