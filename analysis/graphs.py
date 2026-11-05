#!/usr/bin/env python3
"""Build the five required Phase 5 graphs from results/raw_results.csv.

Graphs:
  1. Baseline goodput, TCP vs UDP
  2. Transfer time vs added RTT (both protocols)
  3. Goodput vs loss (both protocols, 95% CI, with Mathis overlay)
  4. TCP retransmissions vs loss
  5. UDP observed loss vs configured loss (diagonal reference)

Each point is the mean of the 5 trials for that condition; error bars are the
95% confidence interval (1.96 * sample SD / sqrt(n)).

Usage:
    python3 analysis/graphs.py
    python3 analysis/graphs.py --csv results/raw_results.csv --outdir graphs
"""

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean, stdev

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

TCP = "#1f77b4"
UDP = "#d95f02"
MATHIS = "#555555"
MARKERS = {"tcp": "o", "udp": "s"}
LABELS = {"tcp": "TCP", "udp": "UDP"}

# Mathis model inputs: 1448 B is the observed TCP payload per segment and
# 20 ms is the added RTT of the E3 sweep. Documented in the report.
MSS_BYTES = 1448
RTT_S = 0.020

plt.rcParams.update({
    "figure.dpi": 200,
    "savefig.dpi": 200,
    "savefig.bbox": "tight",
    "font.size": 11,
    "axes.titlesize": 12,
    "axes.labelsize": 11,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "legend.frameon": False,
})


def load(path: Path) -> dict:
    groups = defaultdict(list)
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            key = (r["protocol"], int(r["delay_ms"]), float(r["loss_pct"]),
                   int(r["payload_bytes"]))
            groups[key].append(r)
    return groups


def stat(rows: list, field: str) -> tuple[float, float, int]:
    vals = [float(r[field]) for r in rows if r[field] not in ("", None)]
    if not vals:
        return float("nan"), float("nan"), 0
    mu = mean(vals)
    sd = stdev(vals) if len(vals) > 1 else 0.0
    return mu, 1.96 * sd / math.sqrt(len(vals)), len(vals)


def series(groups: dict, proto: str, points: list[tuple[int, float, int]],
           field: str) -> tuple[list, list]:
    means, errs = [], []
    for delay, loss, payload in points:
        m, ci, _ = stat(groups[(proto, delay, loss, payload)], field)
        means.append(m)
        errs.append(ci)
    return means, errs


def save(fig, outdir: Path, name: str) -> None:
    path = outdir / name
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}")


def graph_baseline(groups, outdir):
    fig, ax = plt.subplots(figsize=(6.4, 4.6))
    xs = [0, 1]
    vals, errs = [], []
    for proto in ("tcp", "udp"):
        m, ci, _ = stat(groups[(proto, 0, 0.0, 2_000_000)], "goodput_mbps")
        vals.append(m)
        errs.append(ci)
    bars = ax.bar(xs, vals, width=0.55, color=[TCP, UDP],
                  yerr=errs, capsize=4, error_kw={"elinewidth": 1.2})
    for bar, v in zip(bars, vals):
        ax.text(bar.get_x() + bar.get_width() / 2, v + 0.15, f"{v:.2f}",
                ha="center", va="bottom", fontsize=10)
    ax.set_xticks(xs, ["TCP", "UDP"])
    ax.set_ylabel("Goodput (Mbit/s)")
    ax.set_ylim(0, 11)
    ax.set_title("Baseline goodput: TCP vs UDP\n(0 ms added RTT, 0% loss, 2 MB)")
    fig.text(0.5, -0.02, "bars = mean of 5 trials; whiskers = 95% CI",
             ha="center", va="top", fontsize=8, color="#555")
    save(fig, outdir, "01_baseline_goodput.png")


def graph_rtt(groups, outdir):
    fig, ax = plt.subplots(figsize=(7.2, 4.8))
    # 0 ms point comes from E1; 50/100/200 ms from E2 (delay 25/50/100).
    points = [(0, 0.0, 2_000_000), (25, 0.0, 2_000_000),
              (50, 0.0, 2_000_000), (100, 0.0, 2_000_000)]
    rtts = [0, 50, 100, 200]
    for proto in ("tcp", "udp"):
        m, e = series(groups, proto, points, "transfer_time_s")
        ax.errorbar(rtts, m, yerr=e, marker=MARKERS[proto], color=TCP if proto == "tcp" else UDP,
                    label=LABELS[proto], capsize=4, linewidth=1.8, markersize=6)
    ax.set_xlabel("Added round-trip time (ms)")
    ax.set_ylabel("Transfer time (s)")
    ax.set_xticks(rtts)
    ax.set_title("Transfer time vs added RTT\n(0% loss, 2 MB, 10 Mbit/s bottleneck)")
    ax.legend(loc="upper left")
    ax.text(0.99, 0.02, "UDP is open-loop paced at 8 Mbit/s, so RTT does not affect it",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=8, color="#555")
    save(fig, outdir, "02_transfer_time_vs_rtt.png")


def graph_loss(groups, outdir):
    fig, ax = plt.subplots(figsize=(7.4, 5.0))
    losses = [0.0, 0.5, 1.0, 2.0, 5.0]
    points = [(10, p, 10_000_000) for p in losses]
    for proto in ("tcp", "udp"):
        m, e = series(groups, proto, points, "goodput_mbps")
        ax.errorbar(losses, m, yerr=e, marker=MARKERS[proto],
                    color=TCP if proto == "tcp" else UDP,
                    label=f"{LABELS[proto]} (measured)", capsize=4,
                    linewidth=1.8, markersize=6)

    # Mathis: BW = (MSS/RTT) * 1.22/sqrt(p), plotted only where p > 0.
    p = np.linspace(0.005, 0.05, 200)
    mathis = (MSS_BYTES * 8 / RTT_S) * 1.22 / np.sqrt(p) / 1e6
    ax.plot(p * 100, mathis, "--", color=MATHIS, linewidth=1.5,
            label=r"Mathis: $(MSS/RTT)\cdot 1.22/\sqrt{p}$")

    ax.set_xlabel("Configured packet loss (%)")
    ax.set_ylabel("Goodput (Mbit/s)")
    ax.set_title("Goodput vs packet loss\n(20 ms RTT, 10 MB payload, 10 Mbit/s bottleneck)")
    ax.set_ylim(0, 11)
    ax.legend(loc="upper right")
    ax.text(0.99, 0.02,
            "Mathis ignores RTO backoff and the 10 Mbit/s cap; measured TCP falls faster",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=8, color="#555")
    save(fig, outdir, "03_goodput_vs_loss.png")


def graph_retrans(groups, outdir):
    fig, ax = plt.subplots(figsize=(7.2, 4.8))
    losses = [0.0, 0.5, 1.0, 2.0, 5.0]
    points = [(10, p, 10_000_000) for p in losses]
    m, e = series(groups, "tcp", points, "retrans_tshark")
    ax.errorbar(losses, m, yerr=e, marker="o", color=TCP, capsize=4,
                linewidth=1.8, markersize=6)
    for x, y in zip(losses, m):
        ax.annotate(f"{y:.0f}", (x, y), textcoords="offset points", xytext=(0, 8),
                    ha="center", fontsize=9)
    ax.set_xlabel("Configured packet loss (%)")
    ax.set_ylabel("TCP retransmissions (segments)")
    ax.set_title("TCP retransmissions vs packet loss\n(20 ms RTT, 10 MB payload; mean of 5 trials)")
    ax.set_ylim(bottom=0)
    ax.text(0.99, 0.02, "counted from the capture (tcp.analysis.retransmission); "
                        "matches the kernel counter",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=8, color="#555")
    save(fig, outdir, "04_tcp_retransmissions_vs_loss.png")


def graph_udp_loss(groups, outdir):
    fig, ax = plt.subplots(figsize=(6.4, 5.2))
    losses = [0.0, 0.5, 1.0, 2.0, 5.0]
    points = [(10, p, 10_000_000) for p in losses]
    m, e = series(groups, "udp", points, "loss_pct_obs")
    ax.errorbar(losses, m, yerr=e, marker="s", color=UDP, capsize=4,
                linewidth=1.8, markersize=6, label="Measured (5 trials)")
    ax.plot([0, 5.3], [0, 5.3], "--", color=MATHIS, linewidth=1.3, label="y = x (ideal)")
    ax.set_xlabel("Configured loss (%)")
    ax.set_ylabel("Observed loss (%)")
    ax.set_title("UDP observed vs configured loss\n(20 ms RTT, 10 MB payload)")
    ax.set_xlim(0, 5.4)
    ax.set_ylim(0, 5.4)
    ax.set_aspect("equal", adjustable="box")
    ax.legend(loc="upper left")
    ax.text(0.98, 0.04, "observed loss tracks the configured value within sampling noise",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=8, color="#555")
    save(fig, outdir, "05_udp_observed_vs_configured_loss.png")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="results/raw_results.csv")
    ap.add_argument("--outdir", default="graphs")
    args = ap.parse_args()

    groups = load(Path(args.csv))
    if not groups:
        print(f"no data in {args.csv}")
        return 1
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"building graphs from {args.csv} -> {outdir}/")
    graph_baseline(groups, outdir)
    graph_rtt(groups, outdir)
    graph_loss(groups, outdir)
    graph_retrans(groups, outdir)
    graph_udp_loss(groups, outdir)

    # Print the exact numbers plotted, so the report can quote them.
    print("\nplotted means (95% CI):")
    for proto in ("tcp", "udp"):
        b, bci, n = stat(groups[(proto, 0, 0.0, 2_000_000)], "goodput_mbps")
        print(f"  baseline {proto}: {b:.3f} ± {bci:.3f} Mbps (n={n})")
    for loss in (0.0, 0.5, 1.0, 2.0, 5.0):
        t, tci, _ = stat(groups[("tcp", 10, loss, 10_000_000)], "goodput_mbps")
        u, uci, _ = stat(groups[("udp", 10, loss, 10_000_000)], "goodput_mbps")
        rt, _, _ = stat(groups[("tcp", 10, loss, 10_000_000)], "retrans_tshark")
        ul, ulci, _ = stat(groups[("udp", 10, loss, 10_000_000)], "loss_pct_obs")
        print(f"  loss {loss:>3}%: TCP {t:.3f}±{tci:.3f} Mbps, {rt:.0f} retrans | "
              f"UDP {u:.3f}±{uci:.3f} Mbps, obs {ul:.3f}±{ulci:.3f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
