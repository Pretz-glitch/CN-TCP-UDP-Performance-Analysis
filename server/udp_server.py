#!/usr/bin/env python3
"""Instrumented UDP server (Phase 2).

Enlarged SO_RCVBUF; records every sequence number; computes missing
(loss), duplicates and out-of-order counts. Ends on idle timeout
(safety net in case END markers are lost).

Usage: udp_server.py [port]
"""
import socket
import struct
import sys
import time

HDR = struct.Struct("!II")  # seq, total
END_SEQ = 0xFFFFFFFF
RCVBUF = 16 * 1024 * 1024
STARTUP_TIMEOUT = 30.0  # wait this long for the FIRST datagram (time to start the client)
IDLE_TIMEOUT = 3.0      # once started, finish after this long with no datagrams

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 50002

srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
actual_buf = srv.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
srv.bind(("0.0.0.0", PORT))
srv.settimeout(STARTUP_TIMEOUT)  # generous window until the first datagram

seen = {}  # seq -> payload length (first arrival wins)
duplicates = 0
out_of_order = 0
max_seq = -1
expected = None
t_first = None
t_last = None
started = False

while True:
    try:
        data, _ = srv.recvfrom(65535)
    except socket.timeout:
        if not started:
            # Client never showed up: fail loudly, no RESULT line (driver
            # treats a missing RESULT + nonzero exit as a failed run).
            print("UDP_SERVER_ERROR no datagrams within startup window", flush=True)
            srv.close()
            sys.exit(1)
        break  # idle timeout after start: experiment is over
    if not started:
        started = True
        srv.settimeout(IDLE_TIMEOUT)  # now the idle timeout governs the end
    now = time.perf_counter()
    if len(data) < HDR.size:
        continue
    seq, total = HDR.unpack(data[:HDR.size])
    if seq == END_SEQ:
        expected = total
        continue
    if t_first is None:
        t_first = now
    t_last = now
    if seq in seen:
        duplicates += 1
    else:
        if seq < max_seq:
            out_of_order += 1
        max_seq = max(max_seq, seq)
        seen[seq] = len(data) - HDR.size

srv.close()

if expected is None:  # all END markers lost; fall back to highest seq seen
    expected = max_seq + 1 if max_seq >= 0 else 0
received = len(seen)
missing = max(0, expected - received)
loss_pct = (100.0 * missing / expected) if expected > 0 else 0.0
delivered = sum(seen.values())
transfer = (t_last - t_first) if (t_first is not None and t_last != t_first) else 0.0
goodput = (8.0 * delivered / transfer / 1e6) if transfer > 0 else 0.0

print(f"UDP_SERVER expected={expected} received={received} missing={missing} "
      f"loss_pct={loss_pct:.2f} dups={duplicates} ooo={out_of_order} "
      f"transfer_time_s={transfer:.6f} goodput_mbps={goodput:.3f} rcvbuf={actual_buf}",
      flush=True)
print(f"RESULT proto=udp transfer_time_s={transfer:.6f} client_time_s= "
      f"bytes_delivered={delivered} goodput_mbps={goodput:.3f} "
      f"packets_sent= packets_received={received} loss_pct_obs={loss_pct:.3f} "
      f"duplicates={duplicates} out_of_order={out_of_order}",
      flush=True)
