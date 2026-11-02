# Performance Analysis of TCP and UDP Under Emulated Network Conditions

Course: Computer Networks | Platform: Linux (Kubuntu) | Language: Python 3

## Status
- [x] Phase 0: Prerequisites verified (see below)
- [x] Phase 1: Topology + bare transfers (see below)
- [x] Phase 2: Real instrumentation (see below)
- [x] Phase 3: Automation + pilot (see below)
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

## Phase 3 results (automation + pilot)
- Built `experiments/run_experiment.py` orchestrating end-to-end runs:
  - Automates topology verification and per-trial `netem` configuration.
  - Background `tshark` packet captures on sender interface `client:c-r` with clean SIGINT finalization.
  - Client/server process lifecycle with stdout `RESULT` and `SENDER` line aggregation.
  - Cross-validates TCP retransmissions between `tshark` (`tcp.analysis.retransmission || tcp.analysis.fast_retransmission`) and kernel SNMP counters (`nstat -az TcpRetransSegs`).
  - Graceful timeout handling, automated cleanup, and `--dry-run` validation.
- Pilot re-executed from a clean slate (namespaces deleted, results/captures wiped) after all fixes;
  every number below is from that final run:
  - **Baseline (0ms, 0%)**: TCP 9.570 Mbps goodput (0 retrans); UDP 7.772 Mbps (0% loss, 0 ooo/dups).
  - **Added Delay (50ms RTT, 0%)**: TCP 9.181 Mbps; UDP 7.772 Mbps.
  - **Added Loss (20ms RTT, 1% loss, 5 MB)**:
    - TCP: 5.336 Mbps goodput, 38 retransmissions by `tshark` and 38 by kernel SNMP.
    - UDP: 7.718 Mbps goodput, 0.648% observed loss, 0 out-of-order, 0 duplicates.
  - All 6 `.pcapng` traces captured to `captures/` (gitignored — regenerate with the runner).
  - Merged run records written to `results/raw_results.csv` (17 columns, no malformed rows).
- `--set all` plans **90 runs**: E1 baseline (10) + E2 delay sweep 0/25/50/100 ms (30) + E3 loss sweep 0/0.5/1/2/5% (50).

### Phase 3 verification: defects found and fixed
A line-by-line audit plus independent replay of the committed captures found nine real problems. The
first seven were in the delivered runner; two more surfaced only when re-running from scratch and
checking the captures against independent ground truth.
1. **Timeout budget was too small for E3.** The budget divided by the UDP pacing rate for both protocols. Measured E3 worst case (10 MB, 5% loss, 20 ms RTT) = **44.9 s against a 45 s budget**. Replaced with a per-protocol model: Mathis approximation (`BW ≈ MSS/RTT × 1/√p`) for TCP, pacing time for UDP, `max(60, 3×est + 60)`. New budget for that case is **161 s** (3.6× headroom).
2. **Timeout kills orphaned processes.** `proc.kill()` kills only `sudo`; the root-owned python child kept running inside the namespace (confirmed empirically: the child survived `SIGKILL` to sudo and would corrupt later trials). Now every child starts with `start_new_session=True` and is signalled as a process group via `sudo kill -s SIGKILL -- -PGID`. Verified: forced-timeout run leaves zero processes/sockets behind.
   - Sub-note: `kill -SIGKILL -PGID` silently fails (negative PID parsed as options); the `-s SIG … --` form is required.
3. **CSV schema corruption.** The old appender inspected the existing header and used `extrasaction="ignore"`, so appending to the 16-column legacy CSV **silently dropped `added_rtt_ms` from every new row**. Now a header mismatch triggers an in-place migration (with `.bak`), backfilling `added_rtt_ms = 2 × delay_ms`. `results/raw_results.csv` was regenerated under the new schema.
4. **Counter failures reported as zero.** `nstat`/`tshark` errors returned `0`, indistinguishable from "no retransmissions". They now return `None` and the cell is left empty with a warning.
5. **Capture-start race.** A fixed `sleep(0.5)` after launching tshark could truncate the start of a run (SYN/first segments). Now the runner polls until the capture file exists and is non-empty (10 s limit).
6. **Retransmission filter spanned both directions.** The capture on `client:c-r` sees server→client traffic too. Filter now restricts to `ip.src == 10.0.1.1`. Re-verified against the old capture: still 34 (the 21 `fast_retransmission` frames are a *subset* of the 34, not additive — no double counting).
7. **19 MB of `.pcapng` artifacts were committed** because `.gitignore` listed only `*.pcap`. Captures are now untracked and ignored; they stay on disk locally.

Found during the clean-slate redo:
8. **Captures were silently truncated — wrong retransmission counts.** Stopping tshark by signalling the
   whole process group delivered *two* SIGINTs (sudo forwards one to its child as well), so tshark
   aborted before flushing. The file ended 0.25 s before the client finished and the last
   retransmission was missing: tshark reported **33** where the kernel had **34**. Now a single SIGINT
   goes to the actual tshark pid (`ip netns exec` execs the command, so sudo's direct child is tshark).
   Verified: capture duration now matches client duration to ~0.1 ms and captured unique payload bytes
   equal the payload exactly.
9. **Server readiness was assumed, not checked.** A fixed `sleep(0.2)` could race the bind and produce
   `ECONNREFUSED`. Now the runner polls `ss -tln`/`-uln` inside the server namespace until the port is
   listening (10 s cap) and aborts the trial cleanly if it never appears.

### Independent validation performed on the final artifacts
- **TCP payload accounting**: unique client payload bytes in the 1%-loss capture = **5,000,000 exactly**;
  retransmitted bytes account for the remainder. No gaps, no truncation.
- **TCP retransmissions, two ways**: Wireshark flags match an independent duplicate-`(seq,len)` count
  (43 = 43, 41 = 41, 35 = 35 across trials). The kernel counter may differ by **±1** in ~1 of 6 trials
  (observed once, 44 vs 43); this is a kernel accounting artifact, so the capture-based count is treated
  as authoritative and mismatches are printed as `[WARN]` rather than hidden.
- **UDP loss, two ways**: the capture shows exactly **4167** data datagrams transmitted while the server
  counted **4140** received → 27 lost (0.648%), against ~41.7 expected at 1% for that payload; sent +
  10 END markers = 4177 frames, matching the capture exactly.
- **Netem delay mapping**: ping RTT measured 20.5 / 50.5 / 200.6 ms for configured delays of 10 / 25 /
  100 ms, confirming `added_rtt_ms = 2 × delay_ms` is honest.
- **Cleanup**: after a forced-timeout trial and after a full suite, zero orphan processes and no
  lingering listeners in either namespace (`TIME-WAIT` only).
- At 5% loss a 10 MB TCP transfer measured 1.827 Mbps, below the Mathis estimate of 2.61 Mbps —
  expected, since Mathis ignores RTO backoff.

## CSV schema (one row per run, assembled by the Phase 3 driver)
`protocol,delay_ms,added_rtt_ms,loss_pct,trial,payload_bytes,transfer_time_s,client_time_s,goodput_mbps,bytes_delivered,packets_sent,packets_received,loss_pct_obs,duplicates,out_of_order,retrans_tshark,retrans_kernel`
- `delay_ms`: configured one-way delay per direction in netem.
- `added_rtt_ms`: total added round-trip time ($2 \times \text{delay\_ms}$), confirmed symmetric on router interfaces `r-s` and `r-c`.
- `client_time_s`: sender-side duration.
  - TCP: round-trip connect -> data transfer -> close write -> app ACK received.
  - UDP: unidirectional transmission loop duration (first datagram -> final END marker).
- Server stdout carries the `RESULT` line; client stdout carries the `SENDER` line. Empty = n/a.
- Empty cell = not applicable (e.g. `packets_sent`/`loss_pct_obs` for TCP, `retrans_*` for UDP).
- Example merged rows (from the verified pilot):
  `tcp,10,20,1.0,1,5000000,6.299007,6.341634,6.350,5000000,,,,,,27,27`
  `udp,10,20,1.0,1,5000000,5.148808,5.150704,7.666,4934000,4167,4112,1.320,0,0,,`

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
# Manual TCP transfer
sudo ip netns exec server python3 server/tcp_server.py 50001
sudo ip netns exec client python3 client/tcp_client.py 10.0.2.1 50001 1000000
# Manual UDP transfer (optional 4th arg = pacing rate in bit/s)
sudo ip netns exec server python3 server/udp_server.py 50002
sudo ip netns exec client python3 client/udp_client.py 10.0.2.1 50002 2000000 8000000

# Automated sweeps (Phase 3)
python3 experiments/run_experiment.py --set pilot    # 6 calibration runs
python3 experiments/run_experiment.py --set E1       # Baseline sweep (5 trials)
python3 experiments/run_experiment.py --set E2       # Delay sweep (RTT 0..200ms, 5 trials)
python3 experiments/run_experiment.py --set E3       # Loss sweep (0..5%, 10MB, 5 trials)
python3 experiments/run_experiment.py --set all      # Full 90-run sweep (E1..E3)
python3 experiments/run_experiment.py --set all --dry-run   # print plan, write nothing

./setup/reset.sh               # tear everything down
```
Captures in `captures/` are generated artifacts and are gitignored (19 MB for the
pilot alone); re-run the suite to recreate them. `results/*.csv` **is** tracked —
that is the dataset. Run the suite in one go: the runner manages topology, netem,
capture, process lifecycle and CSV appends, and leaves netem unshaped at the end.
