#!/usr/bin/env python3
"""Instrumented UDP client (Phase 2 / Phase 3 updated).

Splits the payload into fixed-size datagrams with an 8-byte header
(seq uint32, total uint32), paces sending to a fixed rate below the
bottleneck (so observed loss comes from netem, not sender bursts), then
sends the END marker (seq=0xFFFFFFFF) several times since a single
marker can itself be lost.

Timing semantics:
client_time_s measures the sender transmission loop duration (from the
first datagram sendto through the final END marker sendto). Unlike TCP
client_time_s (which measures round-trip connect, transfer, and server app
ACK), UDP is unidirectional and does not measure server reception time.

Usage: udp_client.py <server_ip> [port] [bytes] [rate_bps]
"""
import socket
import struct
import sys
import time

HDR = struct.Struct("!II")  # seq, total
END_SEQ = 0xFFFFFFFF
END_REPEAT = 10

ip = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) > 2 else 50002
nbytes = int(sys.argv[3]) if len(sys.argv) > 3 else 2_000_000
rate_bps = int(sys.argv[4]) if len(sys.argv) > 4 else 8_000_000
PAYLOAD = 1200

n_pkts = (nbytes + PAYLOAD - 1) // PAYLOAD
wire_bits = (PAYLOAD + HDR.size + 28) * 8  # payload + header + IP/UDP overhead
interval = wire_bits / rate_bps

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
chunk = b"Y" * PAYLOAD
t0 = time.perf_counter()
next_t = t0
sent = 0
for seq in range(n_pkts):
    n = min(PAYLOAD, nbytes - sent)
    s.sendto(HDR.pack(seq, n_pkts) + chunk[:n], (ip, port))
    sent += n
    next_t += interval
    delay = next_t - time.perf_counter()
    if delay > 0:
        time.sleep(delay)

for _ in range(END_REPEAT):
    s.sendto(HDR.pack(END_SEQ, n_pkts), (ip, port))
client_time = time.perf_counter() - t0
s.close()

print(f"UDP_CLIENT datagrams={n_pkts} sent={sent} rate_bps={rate_bps} client_time_s={client_time:.6f}", flush=True)
print(f"SENDER proto=udp client_time_s={client_time:.6f} packets_sent={n_pkts} bytes_sent={sent}", flush=True)
