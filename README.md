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
- Note: no `wireshark` group created (dumpcap not setuid); captures run via `sudo dumpcap`
  (see defect 10), which is required anyway for `ip netns exec`. Retransmission fields
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
  - Background `dumpcap` packet captures on sender interface `client:c-r`, with a drain delay and a
    single-SIGINT stop that waits for the writer to fully exit (see defect 10).
  - Client/server process lifecycle with stdout `RESULT` and `SENDER` line aggregation.
  - Cross-validates TCP retransmissions between `tshark` (`tcp.analysis.retransmission || tcp.analysis.fast_retransmission`) and kernel SNMP counters (`nstat -az TcpRetransSegs`).
  - Graceful timeout handling, automated cleanup, and `--dry-run` validation.
- Pilot re-executed from a clean slate (namespaces deleted, results/captures wiped) after all fixes;
  every number below is from that final run:
  - **Baseline (0ms, 0%)**: TCP 9.570 Mbps goodput (0 retrans); UDP 7.771 Mbps (0% loss, 0 ooo/dups).
  - **Added Delay (50ms RTT, 0%)**: TCP 9.183 Mbps; UDP 7.771 Mbps.
  - **Added Loss (20ms RTT, 1% loss, 5 MB)**:
    - TCP: 4.591 Mbps goodput, 46 retransmissions by both `tshark` and the kernel SNMP counter.
    - UDP: 7.689 Mbps goodput, 1.032% observed loss, 0 out-of-order, 0 duplicates.
  - All 6 `.pcapng` traces captured to `captures/` (gitignored — regenerate with the runner).
  - Merged run records written to `results/raw_results.csv` (17 columns, no malformed rows).
- `--set all` plans **90 runs**: E1 baseline (10) + E2 delay sweep 0/25/50/100 ms (30) + E3 loss sweep 0/0.5/1/2/5% (50).

### Phase 3 verification: defects found and fixed
A line-by-line audit plus independent replay of the committed captures found ten real problems. The
first seven were in the delivered runner; three more surfaced only when re-running from scratch and
checking the captures against independent ground truth.
1. **Timeout budget was too small for E3.** The budget divided by the UDP pacing rate for both protocols. Measured E3 worst case (10 MB, 5% loss, 20 ms RTT) = **44.9 s against a 45 s budget**. Replaced with a per-protocol model: Mathis approximation (`BW ≈ MSS/RTT × 1/√p`) for TCP, pacing time for UDP, `max(60, 3×est + 60)`. New budget for that case is **161 s** (3.6× headroom).
2. **Timeout kills orphaned processes.** `proc.kill()` kills only `sudo`; the root-owned python child kept running inside the namespace (confirmed empirically: the child survived `SIGKILL` to sudo and would corrupt later trials). Now every child starts with `start_new_session=True` and is signalled as a process group via `sudo kill -s SIGKILL -- -PGID`. Verified: forced-timeout run leaves zero processes/sockets behind.
   - Sub-note: `kill -SIGKILL -PGID` silently fails (negative PID parsed as options); the `-s SIG … --` form is required.
3. **CSV schema corruption.** The old appender inspected the existing header and used `extrasaction="ignore"`, so appending to the 16-column legacy CSV **silently dropped `added_rtt_ms` from every new row**. Now a header mismatch triggers an in-place migration (with `.bak`), backfilling `added_rtt_ms = 2 × delay_ms`. `results/raw_results.csv` was regenerated under the new schema.
4. **Counter failures reported as zero.** `nstat`/`tshark` errors returned `0`, indistinguishable from "no retransmissions". They now return `None` and the cell is left empty with a warning.
5. **Capture-start race.** A fixed `sleep(0.5)` after launching the capture could truncate the start of a run (SYN/first segments). Now the runner polls until the capture file exists and is non-empty (10 s limit).
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
10. **The capture tail was still being lost — and `tcpdump` cannot be stopped on this machine.** After
    fixing the double SIGINT, a worst-case E3 trial captured only **9,965,136 of 10,000,000** payload
    bytes: contiguous from the start, ending 34,864 bytes early, so the last retransmissions were
    missing. A controlled 100-packet test isolated the cause:

    | capture tool | packets captured when stopped |
    |---|---|
    | `tshark` (pipes from dumpcap) | truncates the pipe on shutdown |
    | `dumpcap`, read immediately | 85–100 / 100 |
    | `tcpdump` | 90 / 100 |
    | `tcpdump -U` | 100 / 100 **only because it never stopped** |

    `tcpdump` was in fact **unkillable**: this distro ships an enforcing AppArmor profile for
    `/usr/bin/tcpdump` that denies signal receipt even to root (`apparmor="DENIED" … signal=int`), so
    the process kept running and the "complete" file was an artifact of reading a live capture. The
    final design uses `dumpcap` (unconfined, writes pcapng natively), **waits 1.5 s for its buffer to
    drain**, sends one SIGINT to the dumpcap pid, and waits for that pid to exit before the file is
    touched. Control result with the final design: **100/100 packets, three runs**. Real-run
    verification: payload coverage exact on every capture (2 MB, 5 MB and 10 MB at 5% loss).

### Independent validation performed on the final artifacts
- **TCP payload accounting**: merged client byte intervals (`tcp.seq_raw`, `tcp.len`) cover the payload
  **exactly** in every capture — 2,000,000 / 2,000,000 / 5,000,000 bytes in the pilot, and **10,000,000**
  in the worst-case 10 MB @ 5% trial. No gaps; retransmitted bytes account for the remainder.
- **Capture completeness**: capture duration tracks client duration to sub-millisecond (e.g. 8.755327 s
  vs 8.755423 s; 45.066820 s vs 45.066942 s). The final pilot's 1%-loss capture contains all 46
  retransmissions.
- **TCP retransmissions, two ways**: Wireshark flags and the kernel counter agree in the final pilot
  (**46 = 46** at 1% loss; **370 = 370** at 5% loss). An independent duplicate-`(seq,len)` count matches
  the Wireshark flag count exactly. The kernel counter was previously seen to differ by **±1** in roughly
  1 of 6 trials; that is a kernel accounting artifact, so the capture-based count is treated as
  authoritative and any divergence is printed as `[WARN]` rather than hidden.
- **UDP loss, two ways**: the capture shows exactly **4167** data datagrams transmitted while the server
  counted **4124** received → 43 lost (1.032%), against ~41.7 expected at 1%; the capture also contains
  the single partial final chunk (UDP length 816) plus 10 END markers = 4177 client frames.
- **Netem delay mapping**: ping RTT measured 20.5 / 50.5 / 200.6 ms for configured delays of 10 / 25 /
  100 ms, confirming `added_rtt_ms = 2 × delay_ms` is honest.
- **Cleanup**: after the full E1 suite, the pilot, a forced timeout, and the 10 MB worst case, zero
  orphan capture/client/server processes remain and no listeners survive in either namespace
  (`TIME-WAIT` only). This check is now mandatory because AppArmor can make a capture process
  unkillable if the wrong tool is used (defect 10).
- At 5% loss a 10 MB TCP transfer measured 1.777 Mbps, below the Mathis estimate of 2.61 Mbps —
  expected, since Mathis ignores RTO backoff.

## Phase 4 results (full sweep)
- **90/90 runs successful, 0 failed, 0 warnings**; 18 conditions × 5 trials × 2 protocols.
  E1 baseline (0 ms, 0%), E2 RTT 0/50/100/200 ms at 0% loss, E3 loss 0/0.5/1/2/5% at 20 ms RTT
  (10 MB payload, 10 Mbit/s bottleneck). TCP and UDP used identical machine, payload and measurement path.
- Raw records: `results/raw_results.csv` (90 rows). Processed means/SD/95% CI:
  `results/summary.csv` (generated by `analysis/summarize.py`).
- All 90 captures in `captures/` (609 MB, gitignored).

| Set | Condition | TCP goodput | UDP goodput | Notes |
|---|---|---|---|---|
| E1 | 0 ms RTT, 0% loss | 9.572 Mbps | 7.771 Mbps | 0 retrans, 0% UDP loss |
| E2 | 50 ms RTT | 9.182 Mbps | 7.771 Mbps | TCP xfer 1.743 s |
| E2 | 100 ms RTT | 8.427 Mbps | 7.771 Mbps | TCP xfer 1.899 s |
| E2 | 200 ms RTT | 6.932 Mbps | 7.771 Mbps | TCP xfer 2.308 s |
| E3 | 0% loss | 9.555 Mbps | 7.768 Mbps | 0 retrans; UDP 0.00% obs |
| E3 | 0.5% loss | 8.376 Mbps | 7.727 Mbps | 30 retrans; UDP 0.53% obs |
| E3 | 1% loss | 5.674 Mbps | 7.691 Mbps | 65 retrans; UDP 0.99% obs |
| E3 | 2% loss | 3.507 Mbps | 7.618 Mbps | 140 retrans; UDP 1.93% obs |
| E3 | 5% loss | 1.746 Mbps | 7.387 Mbps | 371 retrans; UDP 4.90% obs |

Key observations: UDP throughput is flat in RTT and loss because it is open-loop paced at 8 Mbit/s;
its measured loss tracks the configured value to within sampling noise. TCP throughput degrades with
both RTT (congestion window takes longer to grow) and loss (retransmissions and RTO backoff), and
retransmissions rise monotonically with loss. At 5% loss TCP delivers 1.75 Mbps, below the Mathis
estimate (~3 Mbps with `1.22/√p`) because Mathis ignores RTO backoff.

The dataset on this branch was regenerated end-to-end on 2026-11-04 (a clean 90-run sweep) and
revalidated; the table above and all Phase 5 graphs use that run.

### Phase 4 verification (independent of the runner)
`analysis/validate_results.py` hard-codes the roadmap plan rather than reusing the runner's plan. It
confirms: exactly 90 rows/18 conditions/5 trials per protocol, 17-column schema, no retransmissions at
0% loss, UDP observed loss within 4σ of configured at every point, monotonic expected trends, and for
all 90 captures: filename/coverage match, capture duration ≥ client duration, exact TCP byte coverage
and exact UDP datagram/END-marker counts. Result: **PASS, 0 warnings**. No orphan processes remained.

## Phase 5 results (analysis + graphs)
`analysis/graphs.py` builds the five required graphs from `results/raw_results.csv`; every point is the
mean of 5 trials and every error bar is the 95% confidence interval (`1.96·SD/√n`). Outputs are in
`graphs/` at 200 dpi.

| # | File | What it shows | Result |
|---|---|---|---|
| 1 | `01_baseline_goodput.png` | Baseline goodput, TCP vs UDP | TCP **9.57** vs UDP **7.77 Mbit/s** at 0 ms / 0% |
| 2 | `02_transfer_time_vs_rtt.png` | Transfer time vs added RTT | TCP **1.672 → 2.308 s** from 0 → 200 ms; UDP flat at **2.059 s** |
| 3 | `03_goodput_vs_loss.png` | Goodput vs loss, with Mathis overlay | TCP **9.56 → 1.75 Mbit/s** from 0 → 5%; UDP **7.77 → 7.39** |
| 4 | `04_tcp_retransmissions_vs_loss.png` | TCP retransmissions vs loss | **0 → 30 → 65 → 140 → 371** segments |
| 5 | `05_udp_observed_vs_configured_loss.png` | UDP observed vs configured loss | **0.53 / 0.99 / 1.93 / 4.90%** against 0.5 / 1 / 2 / 5% |

- The Mathis curve is plotted with MSS = 1448 B and RTT = 20 ms (the E3 added RTT):
  `BW = (MSS/RTT) · 1.22/√p`. Measured TCP lies below it and diverges as loss rises, because Mathis
  assumes only fast retransmit and ignores RTO backoff and the 10 Mbit/s bottleneck.
- Processed numbers for the report are in `results/summary.csv` (18 conditions, mean/SD/95% CI).
- `analysis/validate_results.py` re-checks the raw CSV and all 90 captures against the roadmap spec
  independently of the runner.

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
  `tcp,10,20,1.0,1,5000000,8.713279,8.755423,4.591,5000000,,,,,,46,46`
  `udp,10,20,1.0,1,5000000,5.148870,5.150697,7.689,4948400,4167,4124,1.032,0,0,,`

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
