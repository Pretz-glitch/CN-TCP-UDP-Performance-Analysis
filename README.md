# Performance Analysis of TCP and UDP Under Emulated Network Conditions

Course: Computer Networks | Platform: Linux (Kubuntu) | Language: Python 3

## Status
- [x] Phase 0: Prerequisites verified (see below)
- [ ] Phase 1: Topology + bare transfers
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

## Layout
- `client/` — tcp_client.py, udp_client.py
- `server/` — tcp_server.py, udp_server.py
- `setup/` — topology.sh, netem.sh, reset.sh
- `experiments/` — run_experiment.py
- `results/` — raw_results.csv, processed_results.csv
- `analysis/` — graphs.py
- `captures/` — *.pcapng

## How to reproduce
(TBD once implementation is complete.)
