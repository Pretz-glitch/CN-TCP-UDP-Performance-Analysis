#!/usr/bin/env python3
"""Independent validation of the Phase 4 sweep.

Checks the raw CSV and the packet captures against the roadmap specification,
which is hard-coded here on purpose: reusing the runner's own plan would make
the validation self-referential and could not catch a wrong plan.

Usage:
    python3 analysis/validate_results.py
    python3 analysis/validate_results.py --no-captures     # fast, CSV only
"""

import argparse
import csv
import math
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

CSV_FIELDS = [
    "protocol", "delay_ms", "added_rtt_ms", "loss_pct", "trial",
    "payload_bytes", "transfer_time_s", "client_time_s", "goodput_mbps",
    "bytes_delivered", "packets_sent", "packets_received", "loss_pct_obs",
    "duplicates", "out_of_order", "retrans_tshark", "retrans_kernel",
]

E1 = {(0, 0.0, 2_000_000)}
E2 = {(25, 0.0, 2_000_000), (50, 0.0, 2_000_000), (100, 0.0, 2_000_000)}
E3 = {(10, p, 10_000_000) for p in (0.0, 0.5, 1.0, 2.0, 5.0)}
EXPECTED = E1 | E2 | E3
TRIALS = {1, 2, 3, 4, 5}

fails: list[str] = []
warns: list[str] = []


def fail(msg: str) -> None:
    fails.append(msg)


def warn(msg: str) -> None:
    warns.append(msg)


def sh(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def parse_rows(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != CSV_FIELDS:
            fail(f"CSV header mismatch:\n  got {reader.fieldnames}\n  exp {CSV_FIELDS}")
        return list(reader)


def check_structure(rows: list[dict]) -> None:
    print("== structure ==")
    if len(rows) != 90:
        fail(f"expected 90 rows, found {len(rows)}")
    for i, r in enumerate(rows, start=2):
        if len(r) != len(CSV_FIELDS):
            fail(f"row {i}: {len(r)} columns, expected {len(CSV_FIELDS)}")

    seen = defaultdict(list)
    for r in rows:
        key = (r["protocol"], int(r["delay_ms"]), float(r["loss_pct"]), int(r["payload_bytes"]))
        seen[key].append(int(r["trial"]))

    got = {k[1:] for k in seen}  # drop protocol: conditions are protocol-independent
    if got != EXPECTED:
        fail(f"condition set mismatch; unexpected={sorted(got - EXPECTED)} "
             f"missing={sorted(EXPECTED - got)}")

    for key, trials in sorted(seen.items()):
        if sorted(trials) != sorted(TRIALS):
            fail(f"{key}: trials {sorted(trials)}, expected {sorted(TRIALS)}")
    for proto in ("tcp", "udp"):
        n = sum(1 for r in rows if r["protocol"] == proto)
        if n != 45:
            fail(f"{proto}: {n} rows, expected 45")
    print(f"  rows={len(rows)} conditions={len(seen)} protocols balanced")


def num(r: dict, field: str) -> float:
    v = r[field]
    if v is None or v == "":
        raise ValueError(f"empty {field}")
    return float(v)


def check_metrics(rows: list[dict]) -> None:
    print("== metrics ==")
    for i, r in enumerate(rows, start=2):
        try:
            t = num(r, "transfer_time_s")
            g = num(r, "goodput_mbps")
            c = num(r, "client_time_s")
            if t <= 0 or g <= 0 or c <= 0:
                fail(f"row {i} ({r['protocol']} d{r['delay_ms']} l{r['loss_pct']} t{r['trial']}): non-positive time/goodput")
        except ValueError as exc:
            fail(f"row {i}: {exc}")
            continue
        if abs(float(r["added_rtt_ms"]) - 2 * int(r["delay_ms"])) > 1e-9:
            fail(f"row {i}: added_rtt_ms={r['added_rtt_ms']} != 2*delay")

        if r["protocol"] == "tcp":
            try:
                rt, rk = num(r, "retrans_tshark"), num(r, "retrans_kernel")
            except ValueError as exc:
                fail(f"row {i} tcp: {exc}")
                continue
            if rt != rk:
                warn(f"row {i} tcp d{r['delay_ms']} l{r['loss_pct']} t{r['trial']}: "
                     f"tshark={rt:g} kernel={rk:g} (delta {rk - rt:g})")
            if float(r["loss_pct"]) == 0.0 and rt != 0:
                fail(f"row {i}: {rt:g} retransmissions at 0% loss")
            if int(r["bytes_delivered"]) != int(r["payload_bytes"]):
                fail(f"row {i}: delivered {r['bytes_delivered']} != payload {r['payload_bytes']}")
        else:
            try:
                sent = int(r["packets_sent"])
                recv = int(r["packets_received"])
                obs = num(r, "loss_pct_obs")
                conf = num(r, "loss_pct")
            except ValueError as exc:
                fail(f"row {i} udp: {exc}")
                continue
            if sent != math.ceil(int(r["payload_bytes"]) / 1200):
                fail(f"row {i}: packets_sent={sent}, expected ceil(payload/1200)="
                     f"{math.ceil(int(r['payload_bytes']) / 1200)}")
            if recv > sent:
                fail(f"row {i}: received {recv} > sent {sent}")
            sigma = math.sqrt(max(conf / 100 * (1 - conf / 100) / sent, 0.0)) * 100
            tol = 4 * sigma + 0.02
            if abs(obs - conf) > tol:
                fail(f"row {i} udp d{r['delay_ms']} l{conf}% t{r['trial']}: "
                     f"observed {obs:.3f}% outside 4 sigma (tol {tol:.3f})")
    print(f"  checked {len(rows)} rows")


def by_condition(rows: list[dict], proto: str) -> dict:
    out = defaultdict(list)
    for r in rows:
        if r["protocol"] != proto:
            continue
        key = (int(r["delay_ms"]), float(r["loss_pct"]), int(r["payload_bytes"]))
        out[key].append(r)
    return out


def check_trends(rows: list[dict]) -> None:
    print("== trends ==")
    tcp = by_condition(rows, "tcp")
    udp = by_condition(rows, "udp")

    # E2: transfer time must grow with RTT.
    tcp_e2 = [(d, mean(num(r, "transfer_time_s") for r in tcp[(d, 0.0, 2_000_000)]))
              for d in (0, 25, 50, 100)]
    for (d1, t1), (d2, t2) in zip(tcp_e2, tcp_e2[1:]):
        if t2 <= t1:
            fail(f"TCP transfer time did not increase from delay {d1} to {d2}: {t1:.3f} -> {t2:.3f}")
    print("  TCP E2 transfer time: " + ", ".join(f"{d}ms={t:.3f}s" for d, t in tcp_e2))

    # E3: TCP goodput must fall and retransmissions rise with loss.
    losses = (0.0, 0.5, 1.0, 2.0, 5.0)
    gp = [mean(num(r, "goodput_mbps") for r in tcp[(10, p, 10_000_000)]) for p in losses]
    rt = [mean(num(r, "retrans_tshark") for r in tcp[(10, p, 10_000_000)]) for p in losses]
    for p1, g1, p2, g2 in zip(losses, gp, losses[1:], gp[1:]):
        if g2 > g1 * 1.02:
            fail(f"TCP goodput rose with loss {p1}->{p2}: {g1:.3f} -> {g2:.3f}")
    if any(b < a for a, b in zip(rt, rt[1:])):
        fail(f"TCP retransmissions not monotonic with loss: {rt}")
    print("  TCP E3 goodput: " + ", ".join(f"{p}%={g:.2f}" for p, g in zip(losses, gp)))
    print("  TCP E3 retrans: " + ", ".join(f"{p}%={r:.0f}" for p, r in zip(losses, rt)))

    # E3: UDP observed loss must track configured loss.
    ul = [mean(num(r, "loss_pct_obs") for r in udp[(10, p, 10_000_000)]) for p in losses]
    for (p1, o1), (p2, o2) in zip(zip(losses, ul), zip(losses[1:], ul[1:])):
        if o2 <= o1:
            fail(f"UDP observed loss did not increase {p1}->{p2}: {o1:.3f} -> {o2:.3f}")
    print("  UDP E3 obs loss: " + ", ".join(f"{p}%={o:.2f}" for p, o in zip(losses, ul)))


def coverage(path: Path) -> tuple[int, int, float]:
    """Return (covered_bytes, duplicate_seq_count, capture_duration)."""
    out = sh(f"tshark -r {path} -Y 'ip.src==10.0.1.1 && tcp.len>0' "
             f"-T fields -e tcp.seq_raw -e tcp.len 2>/dev/null")
    iv = []
    pairs = []
    for line in out.splitlines():
        q = line.split()
        if len(q) == 2 and q[0].isdigit():
            s, n = int(q[0]), int(q[1])
            iv.append((s, s + n))
            pairs.append((s, n))
    iv.sort()
    merged = []
    for s, e in iv:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    dup = len(pairs) - len(set(pairs))
    dur = sh(f"capinfos -u {path} 2>/dev/null | grep 'Capture duration' | "
             f"grep -oE '[0-9]+\\.[0-9]+'").strip()
    return sum(e - s for s, e in merged), dup, float(dur or 0)


def check_captures(rows: list[dict], capdir: Path) -> None:
    print("== captures ==")
    missing = []
    for r in rows:
        name = (f"{r['protocol']}_d{r['delay_ms']}ms_l{r['loss_pct']}pct_"
                f"trial{r['trial']}.pcapng")
        p = capdir / name
        if not p.exists():
            missing.append(name)
            continue
        dur = float(sh(f"capinfos -u {p} 2>/dev/null | grep 'Capture duration' | "
                       f"grep -oE '[0-9]+\\.[0-9]+'").strip() or 0)
        if dur + 0.05 < float(r["client_time_s"]):
            fail(f"{name}: capture {dur:.3f}s ends before client {r['client_time_s']}s")
        if r["protocol"] == "tcp":
            cov, dup, _ = coverage(p)
            payload = int(r["payload_bytes"])
            if cov != payload:
                fail(f"{name}: byte coverage {cov}/{payload}")
            if dup != int(r["retrans_tshark"]):
                warn(f"{name}: duplicate-(seq,len)={dup} vs tshark flags={r['retrans_tshark']}")
        else:
            lens = sh(f"tshark -r {p} -Y 'ip.src==10.0.1.1 && udp' -T fields -e udp.length 2>/dev/null")
            vals = [int(x) for x in lens.split() if x.strip().isdigit()]
            data = sum(1 for v in vals if v > 16)
            end = sum(1 for v in vals if v == 16)
            if data != int(r["packets_sent"]):
                fail(f"{name}: capture data={data} != packets_sent={r['packets_sent']}")
            if end != 10:
                fail(f"{name}: {end} END markers, expected 10")
    if missing:
        fail(f"missing {len(missing)} captures: {missing[:5]}{'...' if len(missing) > 5 else ''}")
    print(f"  checked {len(rows) - len(missing)}/{len(rows)} captures")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="results/raw_results.csv")
    ap.add_argument("--captures", default="captures")
    ap.add_argument("--no-captures", action="store_true")
    args = ap.parse_args()

    rows = parse_rows(Path(args.csv))
    check_structure(rows)
    check_metrics(rows)
    check_trends(rows)
    if not args.no_captures:
        check_captures(rows, Path(args.captures))

    print()
    for w in warns:
        print(f"WARN {w}")
    for f in fails:
        print(f"FAIL {f}")
    print(f"\nRESULT: {'PASS' if not fails else f'{len(fails)} FAILURES'}"
          f" ({len(warns)} warnings)")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
