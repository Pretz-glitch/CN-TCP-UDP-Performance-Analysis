#!/usr/bin/env python3
"""Automated Experiment Runner (Phase 3).

Orchestrates full experimental sweeps across network conditions, capturing
packet traces with tshark, collecting application metrics (goodput, transfer
time, packet loss, sequence anomalies), extracting TCP retransmissions, and
appending standardized rows to results/raw_results.csv.

Usage:
    python3 experiments/run_experiment.py --set pilot
    python3 experiments/run_experiment.py --set E1
    python3 experiments/run_experiment.py --set E2
    python3 experiments/run_experiment.py --set E3
    python3 experiments/run_experiment.py --set all
    python3 experiments/run_experiment.py --dry-run --set pilot
"""
import argparse
import csv
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETUP_DIR = PROJECT_ROOT / "setup"
SERVER_DIR = PROJECT_ROOT / "server"
CLIENT_DIR = PROJECT_ROOT / "client"
RESULTS_DIR = PROJECT_ROOT / "results"
CAPTURES_DIR = PROJECT_ROOT / "captures"

CLIENT_IP = "10.0.1.1"
SERVER_IP = "10.0.2.1"

CSV_FIELDS = [
    "protocol",
    "delay_ms",
    "added_rtt_ms",
    "loss_pct",
    "trial",
    "payload_bytes",
    "transfer_time_s",
    "client_time_s",
    "goodput_mbps",
    "bytes_delivered",
    "packets_sent",
    "packets_received",
    "loss_pct_obs",
    "duplicates",
    "out_of_order",
    "retrans_tshark",
    "retrans_kernel",
]


@dataclass
class ExperimentConfig:
    protocol: str       # 'tcp' or 'udp'
    delay_ms: int       # per-direction delay (added RTT is 2 * delay_ms)
    loss_pct: float     # client->server packet loss percentage
    trial: int          # trial index (1-based)
    payload_bytes: int  # payload size in bytes
    rate_bps: int       # UDP pacing rate in bps (default: 8,000,000)
    rate_mbit: int      # netem bottleneck rate in Mbit/s (default: 10)
    port: int           # server port


def get_prefix() -> List[str]:
    """Return ['sudo'] if not running as root."""
    return [] if os.geteuid() == 0 else ["sudo"]


def run_command(cmd: List[str], check: bool = True, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    """Run a command synchronously."""
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=check,
        timeout=timeout,
    )


def check_topology() -> bool:
    """Check if required network namespaces exist."""
    prefix = get_prefix()
    res = subprocess.run(prefix + ["ip", "netns", "list"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        return False
    ns_list = [line.split()[0] for line in res.stdout.splitlines() if line.strip()]
    return all(ns in ns_list for ns in ["client", "router", "server"])


def ensure_topology() -> None:
    """Ensure testbed namespaces and links are up."""
    if not check_topology():
        print("[*] Topology not detected or incomplete. Initializing via setup/topology.sh...")
        topo_script = SETUP_DIR / "topology.sh"
        subprocess.run(["bash", str(topo_script)], check=True)
        time.sleep(0.5)


def apply_netem(delay_ms: int, loss_pct: float, rate_mbit: int = 10) -> None:
    """Apply delay, loss, and rate limiting using setup/netem.sh."""
    netem_script = SETUP_DIR / "netem.sh"
    cmd = ["bash", str(netem_script), str(delay_ms), str(loss_pct), str(rate_mbit)]
    run_command(cmd, check=True)


def get_kernel_tcp_retrans() -> Optional[int]:
    """Read the cumulative TcpRetransSegs counter from the client namespace.

    Returns None (not 0) when the counter cannot be read, so a missing tool or a
    failed command is never silently reported as "zero retransmissions".
    """
    prefix = get_prefix()
    cmd = prefix + ["ip", "netns", "exec", "client", "nstat", "-az", "TcpRetransSegs"]
    try:
        res = run_command(cmd, check=False, timeout=10)
        if res.returncode != 0:
            print(f"  [WARN] nstat failed (rc={res.returncode}): {res.stderr.strip()[:200]}")
            return None
        for line in res.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] == "TcpRetransSegs":
                return int(parts[1])
        print("  [WARN] TcpRetransSegs not present in nstat output")
    except Exception as exc:
        print(f"  [WARN] kernel retransmission counter unavailable: {exc}")
    return None


def count_tshark_retrans(pcap_path: Path) -> Optional[int]:
    """Count sender-side TCP retransmissions in a pcap.

    Filtered to CLIENT_IP so the capture's server->client direction (ACKs and
    the occasional server retransmission) cannot inflate the sender's count.
    Returns None if the capture is missing/unreadable rather than 0.
    """
    if not pcap_path.exists() or pcap_path.stat().st_size == 0:
        print(f"  [WARN] capture missing or empty: {pcap_path.name}")
        return None
    prefix = get_prefix()
    cmd = prefix + [
        "tshark", "-r", str(pcap_path),
        "-Y", f"(tcp.analysis.retransmission || tcp.analysis.fast_retransmission) && ip.src == {CLIENT_IP}",
        "-T", "fields", "-e", "frame.number"
    ]
    try:
        res = run_command(cmd, check=False, timeout=120)
        if res.returncode != 0:
            print(f"  [WARN] tshark failed (rc={res.returncode}): {res.stderr.strip()[:200]}")
            return None
        return len([f for f in res.stdout.splitlines() if f.strip()])
    except Exception as exc:
        print(f"  [WARN] tshark retransmission count unavailable: {exc}")
    return None


def estimate_timeout(cfg: "ExperimentConfig") -> float:
    """Generous per-trial timeout budget in seconds.

    The previous version divided by the UDP pacing rate for BOTH protocols, which
    underestimated TCP badly: the E3 worst case (10 MB @ 5% loss, 20 ms RTT)
    measured 44.9 s against a computed budget of exactly 45 s. TCP throughput
    here is modelled with the Mathis approximation (BW ~ MSS/RTT * 1/sqrt(p)),
    which ignores RTO backoff, hence the wide safety factor on top.
    """
    rtt_s = max(0.001, (2 * cfg.delay_ms) / 1000.0 + 0.002)
    if cfg.protocol == "udp":
        est = (cfg.payload_bytes * 8) / max(1, cfg.rate_bps)  # pacing is deterministic
    else:
        p = max(cfg.loss_pct / 100.0, 1e-4)
        bw_bps = (1460 * 8 / rtt_s) / math.sqrt(p)            # Mathis approximation
        bw_bps = min(bw_bps, cfg.rate_mbit * 1_000_000)       # cannot beat the bottleneck
        est = (cfg.payload_bytes * 8) / max(1.0, bw_bps)
    return max(60.0, est * 3.0 + 60.0)


def kill_tree(proc: Optional[subprocess.Popen], sig: int = signal.SIGKILL, grace: float = 5.0) -> None:
    """Signal the whole process group of a sudo-launched command.

    `proc.kill()` only kills `sudo`; the root-owned python child keeps running
    inside the namespace (verified empirically) and can pollute later trials.
    Processes are started with start_new_session=True so proc.pid is the pgid,
    and the signal is sent through sudo so root-owned members are reachable.
    """
    if proc is None or proc.poll() is not None:
        return
    prefix = get_prefix()
    # NOTE: the negative pid must follow `--`; `kill -SIGKILL -123` is silently
    # parsed as options and leaves the group alive (verified on this box).
    subprocess.run(prefix + ["kill", "-s", sig.name, "--", f"-{proc.pid}"], check=False)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        subprocess.run(prefix + ["kill", "-s", "SIGKILL", "--", f"-{proc.pid}"], check=False)
        try:
            proc.wait(timeout=2)
        except Exception:
            pass


def parse_kv_line(line: str) -> Dict[str, str]:
    """Parse key=value pairs from RESULT or SENDER lines."""
    matches = re.findall(r'(\w+)=([^\s]*)', line)
    return {k: v for k, v in matches}


def extract_metric(sender: Dict[str, str], result: Dict[str, str], key: str) -> str:
    """Extract metric value, distinguishing a valid zero ('0', '0.000') from missing ('')."""
    val = sender.get(key)
    if val is not None and val != "":
        return val
    val = result.get(key)
    if val is not None and val != "":
        return val
    return ""


def run_single_trial(
    cfg: ExperimentConfig,
    captures_dir: Path,
    enable_capture: bool = True,
    dry_run: bool = False,
) -> Optional[Dict[str, str]]:
    """Execute a single trial under configured conditions."""
    pcap_name = f"{cfg.protocol}_d{cfg.delay_ms}ms_l{cfg.loss_pct}pct_trial{cfg.trial}.pcapng"
    pcap_path = captures_dir / pcap_name

    print(f"\n--- [{cfg.protocol.upper()}] Trial {cfg.trial}: delay={cfg.delay_ms}ms (RTT~{2 * cfg.delay_ms}ms), "
          f"loss={cfg.loss_pct}%, payload={cfg.payload_bytes}B ---")

    if dry_run:
        print(f"  [DRY-RUN] Netem: {SETUP_DIR}/netem.sh {cfg.delay_ms} {cfg.loss_pct} {cfg.rate_mbit}")
        print(f"  [DRY-RUN] Capture: {pcap_path.name} on client:c-r")
        print(f"  [DRY-RUN] Server: {cfg.protocol}_server.py port={cfg.port}")
        print(f"  [DRY-RUN] Client: {cfg.protocol}_client.py 10.0.2.1 {cfg.port} {cfg.payload_bytes}")
        return {
            "protocol": cfg.protocol,
            "delay_ms": str(cfg.delay_ms),
            "added_rtt_ms": str(2 * cfg.delay_ms),
            "loss_pct": str(cfg.loss_pct),
            "trial": str(cfg.trial),
            "payload_bytes": str(cfg.payload_bytes),
            "transfer_time_s": "1.000000",
            "client_time_s": "1.000000",
            "goodput_mbps": "8.000",
            "bytes_delivered": str(cfg.payload_bytes),
            "packets_sent": "100",
            "packets_received": "100",
            "loss_pct_obs": "0.000",
            "duplicates": "0",
            "out_of_order": "0",
            "retrans_tshark": "0",
            "retrans_kernel": "0",
        }

    # 1. Apply netem impairment
    apply_netem(cfg.delay_ms, cfg.loss_pct, cfg.rate_mbit)

    # 2. Kernel retransmission counter before run
    retrans_before = get_kernel_tcp_retrans() if cfg.protocol == "tcp" else None

    # 3. Start packet capture (staged in /tmp to avoid root-mount permissions)
    tshark_proc = None
    tmp_pcap = Path("/tmp") / pcap_name
    prefix = get_prefix()
    if enable_capture:
        captures_dir.mkdir(parents=True, exist_ok=True)
        if tmp_pcap.exists():
            subprocess.run(prefix + ["rm", "-f", str(tmp_pcap)], check=False)
        if pcap_path.exists():
            pcap_path.unlink(missing_ok=True)
        tshark_cmd = prefix + [
            "ip", "netns", "exec", "client",
            "tshark", "-i", "c-r", "-w", str(tmp_pcap), "-q"
        ]
        tshark_proc = subprocess.Popen(
            tshark_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Wait until tshark has actually created the file instead of guessing a
        # fixed sleep: a slow attach would silently truncate the start of the run
        # (the SYN and first segments).
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if tmp_pcap.exists() and tmp_pcap.stat().st_size > 0:
                break
            if tshark_proc.poll() is not None:
                print("  [WARN] tshark exited during startup; capture disabled for this run")
                tshark_proc = None
                break
            time.sleep(0.05)
        else:
            print("  [WARN] tshark did not create the capture file within 10s")

    # 4. Start server
    server_script = SERVER_DIR / f"{cfg.protocol}_server.py"
    server_cmd = prefix + [
        "ip", "netns", "exec", "server",
        sys.executable, str(server_script), str(cfg.port)
    ]
    server_proc = subprocess.Popen(
        server_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    time.sleep(0.2)  # Allow server to bind and listen

    # 5. Start client
    client_script = CLIENT_DIR / f"{cfg.protocol}_client.py"
    client_cmd = prefix + [
        "ip", "netns", "exec", "client",
        sys.executable, str(client_script), SERVER_IP, str(cfg.port), str(cfg.payload_bytes)
    ]
    if cfg.protocol == "udp":
        client_cmd.append(str(cfg.rate_bps))

    # Per-protocol timeout budget (see estimate_timeout docstring).
    timeout = estimate_timeout(cfg)
    print(f"  [i] timeout budget: {timeout:.0f}s")

    sender_data: Dict[str, str] = {}
    result_data: Dict[str, str] = {}
    client_proc = None

    try:
        client_proc = subprocess.Popen(
            client_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        c_stdout, c_stderr = client_proc.communicate(timeout=timeout)
        s_stdout, s_stderr = server_proc.communicate(timeout=timeout)

        # Parse SENDER line from client
        for line in c_stdout.splitlines():
            if line.startswith("SENDER"):
                sender_data = parse_kv_line(line)
                break

        # Parse RESULT line from server
        for line in s_stdout.splitlines():
            if line.startswith("RESULT"):
                result_data = parse_kv_line(line)
                break

        if not result_data:
            print(f"  [ERROR] Server produced no RESULT line. Stderr:\n{s_stderr}")
            return None

    except subprocess.TimeoutExpired as exc:
        print(f"  [TIMEOUT] Run exceeded {timeout:.0f}s: {exc}")
        # Kill the whole process group, not just sudo: otherwise the root-owned
        # python child survives inside the namespace and corrupts later trials.
        kill_tree(client_proc)
        kill_tree(server_proc)
        return None
    finally:
        # Stop tshark with SIGINT so it flushes the pcap, then force if needed.
        if tshark_proc and tshark_proc.poll() is None:
            kill_tree(tshark_proc, sig=signal.SIGINT, grace=8.0)
        # Any lingering server (e.g. after a parse failure) must not hold the port.
        kill_tree(server_proc, sig=signal.SIGTERM, grace=3.0)

        # Move staged capture from /tmp to captures/
        if enable_capture and tmp_pcap.exists():
            try:
                subprocess.run(prefix + ["chmod", "666", str(tmp_pcap)], check=False)
                shutil.copyfile(str(tmp_pcap), str(pcap_path))
                subprocess.run(prefix + ["rm", "-f", str(tmp_pcap)], check=False)
                os.chmod(pcap_path, 0o666)
            except Exception as e:
                print(f"  [WARN] Failed to move capture from {tmp_pcap} to {pcap_path}: {e}")

    # 6. Retransmission metrics
    retrans_kernel_count = ""
    retrans_tshark_count = ""
    if cfg.protocol == "tcp":
        retrans_after = get_kernel_tcp_retrans()
        if retrans_before is not None and retrans_after is not None:
            retrans_kernel_count = str(max(0, retrans_after - retrans_before))
        if enable_capture:
            tshark_count = count_tshark_retrans(pcap_path)
            if tshark_count is not None:
                retrans_tshark_count = str(tshark_count)

    # 7. Merge into standard CSV row
    row: Dict[str, str] = {
        "protocol": cfg.protocol,
        "delay_ms": str(cfg.delay_ms),
        "added_rtt_ms": str(2 * cfg.delay_ms),
        "loss_pct": str(cfg.loss_pct),
        "trial": str(cfg.trial),
        "payload_bytes": str(cfg.payload_bytes),
        "transfer_time_s": result_data.get("transfer_time_s", ""),
        "client_time_s": extract_metric(sender_data, result_data, "client_time_s"),
        "goodput_mbps": result_data.get("goodput_mbps", ""),
        "bytes_delivered": result_data.get("bytes_delivered", ""),
        "packets_sent": extract_metric(sender_data, result_data, "packets_sent"),
        "packets_received": result_data.get("packets_received", ""),
        "loss_pct_obs": result_data.get("loss_pct_obs", ""),
        "duplicates": result_data.get("duplicates", ""),
        "out_of_order": result_data.get("out_of_order", ""),
        "retrans_tshark": retrans_tshark_count,
        "retrans_kernel": retrans_kernel_count,
    }

    print(f"  => transfer={row['transfer_time_s']}s client={row['client_time_s']}s "
          f"goodput={row['goodput_mbps']}Mbps delivered={row['bytes_delivered']}B", end="")
    if cfg.protocol == "tcp":
        print(f" retrans(tshark={row['retrans_tshark']}, kernel={row['retrans_kernel']})")
    else:
        print(f" loss_obs={row['loss_pct_obs']}% ooo={row['out_of_order']} dups={row['duplicates']}")

    # Small pause to allow socket states and network queues to drain
    time.sleep(0.8)
    return row


def migrate_csv(csv_path: Path) -> int:
    """Rewrite an existing CSV so its header exactly matches CSV_FIELDS.

    Older runs used a shorter header (no added_rtt_ms). Appending with the old
    header silently DROPPED the new column, so the file no longer matched the
    documented schema. This migrates in place (with a .bak backup), backfilling
    added_rtt_ms from delay_ms where it is derivable. Returns rows migrated.
    """
    backup = csv_path.with_suffix(csv_path.suffix + ".bak")
    shutil.copyfile(csv_path, backup)
    with open(csv_path, mode="r", newline="") as f:
        rows = list(csv.DictReader(f))
    with open(csv_path, mode="w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for old in rows:
            new = {k: old.get(k, "") for k in CSV_FIELDS}
            if not new["added_rtt_ms"] and new["delay_ms"]:
                try:
                    new["added_rtt_ms"] = str(2 * int(float(new["delay_ms"])))
                except ValueError:
                    pass
            writer.writerow(new)
    print(f"  [WARN] CSV header did not match schema; migrated {len(rows)} rows "
          f"(backup: {backup.name})")
    return len(rows)


def append_csv_row(csv_path: Path, row: Dict[str, str]) -> None:
    """Append one row, guaranteeing the file's header matches CSV_FIELDS.

    A schema mismatch is migrated rather than silently truncated: previously
    extrasaction='ignore' meant a legacy 16-column file would drop added_rtt_ms
    from every new row, corrupting the dataset without any error.
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    exists = csv_path.exists() and csv_path.stat().st_size > 0

    if exists:
        with open(csv_path, mode="r", newline="") as f:
            existing_header = next(csv.reader(f), None)
        if existing_header != CSV_FIELDS:
            migrate_csv(csv_path)

    with open(csv_path, mode="a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def build_experiment_plan(
    exp_set: str,
    trials_override: Optional[int],
    protocols: List[str],
    payload_override: Optional[int],
    rate_bps: int,
    bottleneck_mbit: int,
) -> List[ExperimentConfig]:
    """Generate the full list of experiment configurations to run."""
    plan: List[ExperimentConfig] = []

    def get_port(proto: str) -> int:
        return 50001 if proto == "tcp" else 50002

    if exp_set == "pilot":
        # Fast pilot: 1 trial each across baseline, delay, and loss
        conditions = [
            (0, 0.0, payload_override or 2_000_000),      # baseline
            (25, 0.0, payload_override or 2_000_000),     # 50ms RTT
            (10, 1.0, payload_override or 5_000_000),     # 20ms RTT, 1% loss
        ]
        num_trials = trials_override or 1
        for delay, loss, payload in conditions:
            for trial in range(1, num_trials + 1):
                for proto in protocols:
                    plan.append(ExperimentConfig(
                        protocol=proto,
                        delay_ms=delay,
                        loss_pct=loss,
                        trial=trial,
                        payload_bytes=payload,
                        rate_bps=rate_bps,
                        rate_mbit=bottleneck_mbit,
                        port=get_port(proto),
                    ))

    elif exp_set in ("E1", "all"):
        # E1: Baseline (0 delay, 0% loss)
        num_trials = trials_override or 5
        payload = payload_override or 2_000_000
        for trial in range(1, num_trials + 1):
            for proto in protocols:
                plan.append(ExperimentConfig(
                    protocol=proto,
                    delay_ms=0,
                    loss_pct=0.0,
                    trial=trial,
                    payload_bytes=payload,
                    rate_bps=rate_bps,
                    rate_mbit=bottleneck_mbit,
                    port=get_port(proto),
                ))

    if exp_set in ("E2", "all"):
        # E2: Delay sweep (RTT: 0, 50, 100, 200 ms -> delay: 0, 25, 50, 100 ms)
        num_trials = trials_override or 5
        payload = payload_override or 2_000_000
        delays = [0, 25, 50, 100]
        for delay in delays:
            # Skip delay=0 if already in E1 when running 'all'
            if exp_set == "all" and delay == 0:
                continue
            for trial in range(1, num_trials + 1):
                for proto in protocols:
                    plan.append(ExperimentConfig(
                        protocol=proto,
                        delay_ms=delay,
                        loss_pct=0.0,
                        trial=trial,
                        payload_bytes=payload,
                        rate_bps=rate_bps,
                        rate_mbit=bottleneck_mbit,
                        port=get_port(proto),
                    ))

    if exp_set in ("E3", "all"):
        # E3: Loss sweep (0, 0.5, 1, 2, 5% loss at 20ms RTT -> 10ms per direction)
        # Recommended: 10 MB payload for stable loss statistics
        num_trials = trials_override or 5
        payload = payload_override or 10_000_000
        loss_rates = [0.0, 0.5, 1.0, 2.0, 5.0]
        for loss in loss_rates:
            for trial in range(1, num_trials + 1):
                for proto in protocols:
                    plan.append(ExperimentConfig(
                        protocol=proto,
                        delay_ms=10,
                        loss_pct=loss,
                        trial=trial,
                        payload_bytes=payload,
                        rate_bps=rate_bps,
                        rate_mbit=bottleneck_mbit,
                        port=get_port(proto),
                    ))

    return plan


def main():
    parser = argparse.ArgumentParser(description="Automated Experiment Runner for TCP vs UDP Analysis")
    parser.add_argument("--set", choices=["pilot", "E1", "E2", "E3", "all"], default="pilot",
                        help="Experiment suite to run (default: pilot)")
    parser.add_argument("--trials", type=int, default=None,
                        help="Override number of trials per condition")
    parser.add_argument("--protocols", nargs="+", choices=["tcp", "udp"], default=["tcp", "udp"],
                        help="Protocols to include (default: tcp udp)")
    parser.add_argument("--payload-bytes", type=int, default=None,
                        help="Override payload size in bytes")
    parser.add_argument("--rate-bps", type=int, default=8_000_000,
                        help="UDP pacing rate in bit/s (default: 8000000 = 8 Mbit/s)")
    parser.add_argument("--bottleneck-mbit", type=int, default=10,
                        help="Router bottleneck rate in Mbit/s (default: 10)")
    parser.add_argument("--output", type=str, default=str(RESULTS_DIR / "raw_results.csv"),
                        help="Path to results CSV (default: results/raw_results.csv)")
    parser.add_argument("--captures-dir", type=str, default=str(CAPTURES_DIR),
                        help="Directory to save .pcapng traces (default: captures)")
    parser.add_argument("--no-capture", action="store_true",
                        help="Disable tshark packet capture")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print experiment plan without executing")

    args = parser.parse_args()

    csv_path = Path(args.output).resolve()
    captures_path = Path(args.captures_dir).resolve()

    plan = build_experiment_plan(
        exp_set=args.set,
        trials_override=args.trials,
        protocols=args.protocols,
        payload_override=args.payload_bytes,
        rate_bps=args.rate_bps,
        bottleneck_mbit=args.bottleneck_mbit,
    )

    print("=" * 70)
    print(f" EXPERIMENT SUITE: {args.set.upper()} ({len(plan)} total runs)")
    print(f" Protocols: {', '.join(args.protocols)} | Capture: {not args.no_capture}")
    print(f" Output CSV: {csv_path}")
    print(f" Captures Dir: {captures_path}")
    print("=" * 70)

    if not args.dry_run:
        ensure_topology()

    successful_runs = 0
    failed_runs = 0

    try:
        for idx, cfg in enumerate(plan, 1):
            print(f"\nProgress: Run {idx}/{len(plan)}")
            row = run_single_trial(
                cfg=cfg,
                captures_dir=captures_path,
                enable_capture=not args.no_capture,
                dry_run=args.dry_run,
            )
            if row:
                if not args.dry_run:
                    append_csv_row(csv_path, row)
                successful_runs += 1
            else:
                failed_runs += 1

    finally:
        if not args.dry_run:
            print("\n[*] Resetting router netem to unshaped baseline...")
            try:
                apply_netem(0, 0, args.bottleneck_mbit)
            except Exception:
                pass

    print("\n" + "=" * 70)
    print(f" EXPERIMENT COMPLETE: {successful_runs} successful, {failed_runs} failed.")
    if not args.dry_run:
        print(f" Results written to: {csv_path}")
    else:
        print(f" [DRY-RUN] Verification complete. No CSV files or captures were modified.")
    print("=" * 70)


if __name__ == "__main__":
    main()
