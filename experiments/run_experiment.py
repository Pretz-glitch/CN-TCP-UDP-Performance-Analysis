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

CSV_FIELDS = [
    "protocol",
    "delay_ms",
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


def get_kernel_tcp_retrans() -> int:
    """Read TcpRetransSegs counter from client namespace."""
    prefix = get_prefix()
    cmd = prefix + ["ip", "netns", "exec", "client", "nstat", "-az", "TcpRetransSegs"]
    try:
        res = run_command(cmd, check=False)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0] == "TcpRetransSegs":
                    return int(parts[1])
    except Exception:
        pass
    return 0


def count_tshark_retrans(pcap_path: Path) -> int:
    """Count retransmissions in pcap using tshark display filter."""
    if not pcap_path.exists() or pcap_path.stat().st_size == 0:
        return 0
    prefix = get_prefix()
    cmd = prefix + [
        "tshark", "-r", str(pcap_path),
        "-Y", "tcp.analysis.retransmission || tcp.analysis.fast_retransmission",
        "-T", "fields", "-e", "frame.number"
    ]
    try:
        res = run_command(cmd, check=False)
        if res.returncode == 0:
            frames = [f.strip() for f in res.stdout.splitlines() if f.strip()]
            return len(frames)
    except Exception:
        pass
    return 0


def parse_kv_line(line: str) -> Dict[str, str]:
    """Parse key=value pairs from RESULT or SENDER lines."""
    matches = re.findall(r'(\w+)=([^\s]*)', line)
    return {k: v for k, v in matches}


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
    retrans_before = get_kernel_tcp_retrans() if cfg.protocol == "tcp" else 0

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
        )
        time.sleep(0.5)  # Allow tshark to attach

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
    )
    time.sleep(0.2)  # Allow server to bind and listen

    # 5. Start client
    client_script = CLIENT_DIR / f"{cfg.protocol}_client.py"
    client_cmd = prefix + [
        "ip", "netns", "exec", "client",
        sys.executable, str(client_script), "10.0.2.1", str(cfg.port), str(cfg.payload_bytes)
    ]
    if cfg.protocol == "udp":
        client_cmd.append(str(cfg.rate_bps))

    # Calculate timeout based on transfer size and rate (minimum 30s)
    est_transfer_time = (cfg.payload_bytes * 8) / min(cfg.rate_bps, cfg.rate_mbit * 1_000_000)
    timeout = max(35.0, est_transfer_time * 3 + 15.0)

    sender_data: Dict[str, str] = {}
    result_data: Dict[str, str] = {}
    client_proc = None

    try:
        client_proc = subprocess.Popen(
            client_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
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
        print(f"  [TIMEOUT] Run exceeded {timeout}s: {exc}")
        if client_proc:
            client_proc.kill()
        server_proc.kill()
        return None
    finally:
        # Stop tshark
        if tshark_proc and tshark_proc.poll() is None:
            # Send SIGINT to tshark (via sudo if running unprivileged)
            subprocess.run(prefix + ["kill", "-INT", str(tshark_proc.pid)], check=False)
            try:
                tshark_proc.wait(timeout=5)
            except Exception:
                subprocess.run(prefix + ["kill", "-KILL", str(tshark_proc.pid)], check=False)
                try:
                    tshark_proc.wait(timeout=2)
                except Exception:
                    pass

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
        retrans_kernel_count = str(max(0, retrans_after - retrans_before))
        if enable_capture:
            retrans_tshark_count = str(count_tshark_retrans(pcap_path))

    # 7. Merge into standard CSV row
    row: Dict[str, str] = {
        "protocol": cfg.protocol,
        "delay_ms": str(cfg.delay_ms),
        "loss_pct": str(cfg.loss_pct),
        "trial": str(cfg.trial),
        "payload_bytes": str(cfg.payload_bytes),
        "transfer_time_s": result_data.get("transfer_time_s", ""),
        "client_time_s": sender_data.get("client_time_s", result_data.get("client_time_s", "")),
        "goodput_mbps": result_data.get("goodput_mbps", ""),
        "bytes_delivered": result_data.get("bytes_delivered", ""),
        "packets_sent": sender_data.get("packets_sent", result_data.get("packets_sent", "")),
        "packets_received": result_data.get("packets_received", ""),
        "loss_pct_obs": result_data.get("loss_pct_obs", ""),
        "duplicates": result_data.get("duplicates", ""),
        "out_of_order": result_data.get("out_of_order", ""),
        "retrans_tshark": retrans_tshark_count,
        "retrans_kernel": retrans_kernel_count,
    }

    print(f"  => transfer={row['transfer_time_s']}s goodput={row['goodput_mbps']}Mbps "
          f"delivered={row['bytes_delivered']}B", end="")
    if cfg.protocol == "tcp":
        print(f" retrans(tshark={row['retrans_tshark']}, kernel={row['retrans_kernel']})")
    else:
        print(f" loss_obs={row['loss_pct_obs']}% ooo={row['out_of_order']} dups={row['duplicates']}")

    # Small pause to allow socket states and network queues to drain
    time.sleep(0.8)
    return row


def append_csv_row(csv_path: Path, row: Dict[str, str]) -> None:
    """Append a single row to the results CSV."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists() or csv_path.stat().st_size == 0
    with open(csv_path, mode="a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
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
    print(f" Results written to: {csv_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()
