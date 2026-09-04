#!/usr/bin/env python3
"""
selftest.py — prove shredbench measures what it claims to measure.

Runs fakefeed.py with a known lag and a known loss rate, runs shredbench
against it, and asserts the reported numbers recover the injected ones.

    python3 selftest.py

Exits non-zero on failure. Safe to run in CI; uses loopback only.
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

LAG_MS = 0.6          # feed 'b' is delayed by this much
LOSS = 0.03           # feed 'b' drops this fraction
PORT_A, PORT_B = 20910, 20911
SLOTS = 400
WINDOW = 20


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def main():
    out = Path(tempfile.mkdtemp()) / "selftest.json"

    sender = subprocess.Popen(
        [sys.executable, str(HERE / "fakefeed.py"),
         f"--to=fast={PORT_A}:0:0.0",
         f"--to=slow={PORT_B}:{LAG_MS}:{LOSS}",
         "--slots", str(SLOTS), "--slot-interval", "0.005"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    time.sleep(0.3)
    receiver = subprocess.run(
        [sys.executable, str(HERE / "shredbench.py"),
         f"--feed=a={PORT_A}", f"--feed=b={PORT_B}",
         "--seconds", str(WINDOW), "--bind", "127.0.0.1",
         "--baseline", "a", "--json", str(out)],
        capture_output=True, text=True)

    sender.terminate()
    print(receiver.stdout)
    if receiver.returncode != 0:
        fail(f"shredbench exited {receiver.returncode}\n{receiver.stderr}")
    if not out.exists():
        fail("no JSON report written")

    r = json.loads(out.read_text())
    feeds = {f["name"]: f for f in r["feeds"]}
    delta = r["deltas_vs_baseline_ms"]["b"]

    checks = []

    # The generator emits 64 shreds per slot (32 data + 32 coding).
    checks.append((
        "union is close to slots x 64",
        abs(r["union_shreds"] - SLOTS * 64) <= SLOTS * 64 * 0.2,
        f"union={r['union_shreds']} expected~{SLOTS * 64}"))

    checks.append((
        "feed a saw everything",
        feeds["a"]["coverage_pct"] >= 99.0,
        f"coverage={feeds['a']['coverage_pct']}%"))

    expected_cov = (1 - LOSS) * 100
    checks.append((
        f"feed b coverage recovers injected {LOSS:.0%} loss",
        abs(feeds["b"]["coverage_pct"] - expected_cov) < 2.0,
        f"coverage={feeds['b']['coverage_pct']}% expected~{expected_cov:.1f}%"))

    checks.append((
        f"p50 delta recovers injected {LAG_MS} ms lag",
        abs(delta["p50"] - LAG_MS) < 0.5,
        f"p50={delta['p50']}ms expected~{LAG_MS}ms"))

    checks.append((
        "faster feed wins the majority of first arrivals",
        feeds["a"]["win_rate_pct"] > 90.0,
        f"win_rate={feeds['a']['win_rate_pct']}%"))

    checks.append((
        "no kernel drops on either feed",
        all(f["kernel_drops"] in (0, None) for f in r["feeds"]),
        f"drops={[f['kernel_drops'] for f in r['feeds']]}"))

    checks.append((
        "no unparseable or oversize datagrams",
        all(f["unparseable"] == 0 and f["oversize"] == 0 for f in r["feeds"]),
        "parser rejected well-formed synthetic shreds"))

    checks.append((
        "data and coding shreds classified evenly",
        all(f["data_shreds"] > 0 and f["coding_shreds"] > 0 for f in r["feeds"]),
        f"a: {feeds['a']['data_shreds']}d/{feeds['a']['coding_shreds']}c"))

    bad = 0
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}   ({detail})")
        bad += not ok

    if bad:
        fail(f"{bad} check(s) failed")
    print(f"\nall {len(checks)} checks passed")


if __name__ == "__main__":
    main()
