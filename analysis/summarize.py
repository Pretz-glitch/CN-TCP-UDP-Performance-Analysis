#!/usr/bin/env python3
"""Turn results/raw_results.csv into results/summary.csv (processed data).

One row per (protocol, delay, loss, payload) condition, with mean, sample
standard deviation and 95% confidence interval for the metrics used by the
report and the Phase 5 graphs.

Usage:
    python3 analysis/summarize.py
"""

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean, stdev

GROUP = ("protocol", "delay_ms", "added_rtt_ms", "loss_pct", "payload_bytes")
METRICS = (
    "transfer_time_s", "client_time_s", "goodput_mbps", "bytes_delivered",
    "packets_sent", "packets_received", "loss_pct_obs", "duplicates",
    "out_of_order", "retrans_tshark", "retrans_kernel",
)
FIELDS = list(GROUP) + ["n"] + [f"{m}_{s}" for m in METRICS for s in ("mean", "sd", "ci95")]


def fmt(v: float) -> str:
    return f"{v:.6f}" if v != int(v) else str(int(v))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="results/raw_results.csv")
    ap.add_argument("--out", default="results/summary.csv")
    args = ap.parse_args()

    with open(args.csv, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("no rows to summarise")
        return 1

    groups = defaultdict(list)
    for r in rows:
        groups[tuple(r[k] for k in GROUP)].append(r)

    out_rows = []
    for key in sorted(groups, key=lambda k: (k[0], int(k[1]), float(k[3]))):
        members = groups[key]
        out = dict(zip(GROUP, key))
        out["n"] = str(len(members))
        for m in METRICS:
            vals = [float(r[m]) for r in members if r[m] not in ("", None)]
            if not vals:
                out[f"{m}_mean"] = out[f"{m}_sd"] = out[f"{m}_ci95"] = ""
                continue
            mu = mean(vals)
            sd = stdev(vals) if len(vals) > 1 else 0.0
            out[f"{m}_mean"] = fmt(mu)
            out[f"{m}_sd"] = fmt(sd)
            out[f"{m}_ci95"] = fmt(1.96 * sd / math.sqrt(len(vals)))
        out_rows.append(out)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out_rows)

    print(f"wrote {out_path} ({len(out_rows)} conditions from {len(rows)} runs)")
    print(f"{'proto':5} {'delay':>5} {'loss%':>6} {'goodput Mbps':>13} {'±95%':>7} "
          f"{'xfer s':>8} {'retrans':>8} {'obs loss%':>10}")
    for r in out_rows:
        print(f"{r['protocol']:5} {r['delay_ms']:>5} {r['loss_pct']:>6} "
              f"{float(r['goodput_mbps_mean']):>13.3f} {float(r['goodput_mbps_ci95']):>7.3f} "
              f"{float(r['transfer_time_s_mean']):>8.3f} "
              f"{(r['retrans_tshark_mean'] or '0'):>8} {(r['loss_pct_obs_mean'] or '-'):>10}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
