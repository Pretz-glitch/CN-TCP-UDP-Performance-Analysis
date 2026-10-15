# Performance Analysis of TCP and UDP Under Emulated Network Conditions

Course: Computer Networks | Platform: Linux (Kubuntu) | Language: Python 3

## Status
- [x] Phase 0: Prerequisites verified (see below)
- [x] Phase 1: Topology + bare transfers (see below)
- [x] Phase 2: Real instrumentation (see below)
- [ ] Phase 2: Real instrumentation
- [ ] Phase 3: Automation + pilot
- [ ] Phase 4: Full experiment sweeps (E1–E3)
- [ ] Phase 5: Analysis and graphs
- [ ] Phase 6: Report
- [ ] Phase 7: Slides + demo

## Prerequisites (Phase 0 results)
- OS: Ubuntu 24.04.5 LTS, kernel 7.0.0-34-generic, bare metal
- `ip netns` create/delete: OK (passwordless sudo available)
- `sch_netem` kernel module: OK (loads via modprobe, delay/loss/rate verified with ping)
- 3-node topology (client–router–server) with IP forwarding: OK
- Netem calibration: 50ms+50ms per-direction delay => ~100.4ms RTT; 5% loss => 1/20 drops
- TCP socket transfer across impaired 3-node topo: OK
- `ethtool -K ... tso off gso off gro off` on veth: OK
- `tcpdump` capture inside netns: OK
- Python 3.12, system `matplotlib` 3.6.3 + `numpy` 1.26.4: OK
- REQUIRED installs: `tshark` 4.2.2 / `wireshark`, `python3-pandas` 2.1.4, `python3-pip` 24.0 — INSTALLED 2026-10-01
- Note: no `wireshark` group created (dumpcap not setuid); captures run via `sudo tshark`,
  which is required anyway for `ip netns exec`. Retransmission fields
  (`tcp.analysis.retransmission`, `fast_retransmission`, etc.) confirmed present in tshark.

## Phase 1 results (topology + bare transfers)
- `setup/topology.sh` (idempotent), `setup/reset.sh`, `setup/netem.sh <delay_ms> <loss_pct> [rate_mbit]` — all working, committed `c944b21`.
- Baseline ping (no impairment): ~0.08ms RTT, 0% loss, bidirectional.
- Netem calibration (`netem.sh 50 1`): 100.5ms mean RTT (design: 50+50ms), 1% loss over 100 pings — exact match.
- Bare TCP: 1,000,000/1,000,000 bytes at baseline and under 50ms/1% impairment.
- Bare UDP: 834/834 datagrams (1 MB) at baseline; 84/84 after full teardown→rebuild.
- No measurement instrumentation yet (timing, CSV, sequencing, pacing) — that is Phase 2.

## Phase 2 results (instrumentation)
- TCP: server records `t_first`/`t_last`, byte count, sends 1-byte app ACK; client
  times connect→ACK. Both print `RESULT`/`SENDER` key=value lines (unbuffered).
- UDP: 1200B payload + 8B header (seq, total); paced below bottleneck (default
  8 Mbit/s); 16 MB `SO_RCVBUF` request (kernel grants 4 MB via rmem_max — plenty:
  max in-flight is ~200 KB); 10x END marker + 3 s idle-timeout safety net.
  Two-stage timeout: 30 s startup window for the first datagram (time to start
  the client manually), then 3 s idle timeout once running; no data in 30 s is
  a loud failure (exit 1, no RESULT line) so the driver can flag the run.
- Bug found by measurement: original client `close()`d before reading the ACK while
  the server waited for close before ACKing — a close/ACK deadlock masked by
  `timeout` kills. Fixed with `shutdown(SHUT_WR)` after sending. Lesson: every
  earlier "success" was a mercy kill; always check server-side output, not just client exit.
- Manual runs under `netem.sh 10 1` (20 ms RTT, 1% loss), 2 MB payload:
  - TCP: transfer 2.570 s, client 2.612 s, goodput 6.226 Mbps (Mathis predicts ~8.9)
  - UDP: 1667 sent / 1659 received (0.48% obs), goodput 7.734 Mbps, 0 dups, 0 ooo
- Single-run UDP loss is noisy (8 vs ~17 expected at 1%); sweeps average it out in Phase 4.

## CSV schema (one row per run, assembled by the Phase 3 driver)
`protocol,delay_ms,loss_pct,trial,payload_bytes,transfer_time_s,client_time_s,goodput_mbps,bytes_delivered,packets_sent,packets_received,loss_pct_obs,duplicates,out_of_order`
- Server stdout carries the `RESULT` line (server-side fields); client stdout carries
  the `SENDER` line (`client_time_s` for TCP, `packets_sent` for UDP). Empty = n/a.
- Example merged row:
  `tcp,10,1,1,2000000,2.570001,2.612103,6.226,2000000,,,,,`
  `udp,10,1,1,2000000,2.058791,,7.734,1990400,1667,1659,0.480,0,0`

## Layout
- `client/` — tcp_client.py, udp_client.py
- `server/` — tcp_server.py, udp_server.py
- `setup/` — topology.sh, netem.sh, reset.sh
- `experiments/` — run_experiment.py
- `results/` — raw_results.csv, processed_results.csv
- `analysis/` — graphs.py
- `captures/` — *.pcapng

## How to reproduce
```bash
./setup/topology.sh            # build client -> router -> server testbed
./setup/netem.sh 50 1          # ~100ms RTT, 1% loss client->server (0 0 = baseline)
# TCP: server first, then client (run server in one shell, client in another)
sudo ip netns exec server python3 server/tcp_server.py 50001
sudo ip netns exec client python3 client/tcp_client.py 10.0.2.1 50001 1000000
# UDP: same pattern (optional 4th client arg = pacing rate in bit/s)
sudo ip netns exec server python3 server/udp_server.py 50002
sudo ip netns exec client python3 client/udp_client.py 10.0.2.1 50002 2000000 8000000
./setup/reset.sh               # tear everything down
```
(Full experiment automation arrives in Phase 3.)
