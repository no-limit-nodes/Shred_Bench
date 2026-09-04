#!/usr/bin/env python3
"""
shredbench — compare two or more raw Solana shred feeds on one host.

Every feed is delivered to the same machine, so a single monotonic clock
times all of them and no cross-host clock synchronisation is involved.
That is the only way to compare feeds honestly without a hardware clock:
measure them where they land, not where they left.

    ./shredbench.py --feed jito=20000 --feed frankfurt=20001 --seconds 900

Reports, per feed: throughput, coverage of the union of all shreds seen,
first-arrival win rate, and arrival delta percentiles against a baseline.

MIT licensed. https://github.com/nolimitnodes/shredbench
"""

import argparse
import json
import os
import re
import selectors
import signal
import socket
import struct
import sys
import time

# --- shred wire format -------------------------------------------------
# solana-foundation/specs, p2p/shred.md. Integers are little-endian.
#   0x00  64B  ed25519 signature
#   0x40   1B  variant
#   0x41   8B  u64  slot
#   0x49   4B  u32  shred_index
#   0x4d   2B  u16  shred_version
#   0x4f   4B  u32  fec_set_index
COMMON_HEADER_LEN = 0x53          # 83 bytes
SLOT_INDEX = struct.Struct("<QI")  # slot @ 0x41, index @ 0x49
SLOT_OFFSET = 0x41
SHRED_SZ_MAX = 1228

LEGACY_CODE = 0x5a
LEGACY_DATA = 0xa5


def classify(variant):
    """Return 'data', 'coding' or None for an unrecognised variant byte."""
    if variant == LEGACY_DATA:
        return "data"
    if variant == LEGACY_CODE:
        return "coding"
    high = variant >> 4
    if high == 0x8:
        return "data"       # merkle data
    if high == 0x4:
        return "coding"     # merkle coding
    return None


def parse(pkt):
    """(slot, index, kind) or None if this is not a plausible shred."""
    if len(pkt) < COMMON_HEADER_LEN:
        return None
    kind = classify(pkt[0x40])
    if kind is None:
        return None
    slot, index = SLOT_INDEX.unpack_from(pkt, SLOT_OFFSET)
    return slot, index, kind


# --- kernel socket drop counters ---------------------------------------

def socket_inode(sock):
    try:
        return os.readlink(f"/proc/self/fd/{sock.fileno()}")[8:-1]
    except OSError:
        return None


def socket_drops(inode):
    """Kernel-side drop count for one UDP socket, or None if unavailable."""
    if inode is None:
        return None
    try:
        with open("/proc/net/udp") as fh:
            next(fh)
            for line in fh:
                f = line.split()
                if len(f) >= 13 and f[9] == inode:
                    return int(f[-1])
    except (OSError, ValueError, StopIteration):
        return None
    return None


# --- stats --------------------------------------------------------------

def percentile(xs, p):
    if not xs:
        return float("nan")
    i = int(round((p / 100.0) * (len(xs) - 1)))
    return xs[min(i, len(xs) - 1)]


class Feed:
    def __init__(self, name, port):
        self.name = name
        self.port = port
        self.first = {}       # (slot, index) -> monotonic arrival
        self.datagrams = 0
        self.duplicates = 0
        self.unparseable = 0
        self.oversize = 0
        self.kinds = {"data": 0, "coding": 0}
        self.sock = None
        self.inode = None
        self.rcvbuf = 0
        self.drops_start = None


def build_feeds(specs, bind, rcvbuf_request):
    feeds = []
    for spec in specs:
        if "=" not in spec:
            sys.exit(f"bad --feed {spec!r}, expected name=port")
        name, _, port = spec.partition("=")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            sys.exit(f"bad feed name {name!r}")
        f = Feed(name, int(port))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf_request)
        # The kernel returns twice what it accepted, and silently clamps the
        # request to net.core.rmem_max. Report what we actually got.
        f.rcvbuf = s.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        s.setblocking(False)
        try:
            s.bind((bind, f.port))
        except OSError as e:
            sys.exit(f"cannot bind {bind}:{f.port} for feed {name}: {e}")
        f.sock = s
        f.inode = socket_inode(s)
        f.drops_start = socket_drops(f.inode)
        feeds.append(f)
    return feeds


def capture(feeds, seconds):
    sel = selectors.DefaultSelector()
    for f in feeds:
        sel.register(f.sock, selectors.EVENT_READ, f)

    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("now", True))

    started = time.monotonic()
    deadline = started + seconds
    while not stop["now"] and time.monotonic() < deadline:
        for key, _ in sel.select(timeout=0.5):
            f = key.data
            sock = key.fileobj
            while True:
                try:
                    pkt = sock.recv(2048)
                except BlockingIOError:
                    break
                except OSError:
                    break
                now = time.monotonic()
                f.datagrams += 1
                if len(pkt) > SHRED_SZ_MAX:
                    f.oversize += 1
                got = parse(pkt)
                if got is None:
                    f.unparseable += 1
                    continue
                slot, index, kind = got
                f.kinds[kind] += 1
                k = (slot, index)
                if k in f.first:
                    f.duplicates += 1
                else:
                    f.first[k] = now
    return time.monotonic() - started


def analyse(feeds, elapsed, baseline_name):
    union = set()
    for f in feeds:
        union |= f.first.keys()

    # Which feed delivered each shred first. Ties split nothing — strict min.
    wins = {f.name: 0 for f in feeds}
    for k in union:
        best, best_t = None, None
        for f in feeds:
            t = f.first.get(k)
            if t is not None and (best_t is None or t < best_t):
                best, best_t = f.name, t
        if best is not None:
            wins[best] += 1

    baseline = next((f for f in feeds if f.name == baseline_name), feeds[0])

    report = {
        "window_seconds": round(elapsed, 2),
        "union_shreds": len(union),
        "baseline": baseline.name,
        "feeds": [],
        "deltas_vs_baseline_ms": {},
    }

    for f in feeds:
        drops_end = socket_drops(f.inode)
        drops = None
        if f.drops_start is not None and drops_end is not None:
            drops = drops_end - f.drops_start
        report["feeds"].append({
            "name": f.name,
            "port": f.port,
            "rcvbuf_bytes": f.rcvbuf,
            "datagrams": f.datagrams,
            "datagrams_per_sec": round(f.datagrams / elapsed, 1) if elapsed else 0,
            "unique_shreds": len(f.first),
            "data_shreds": f.kinds["data"],
            "coding_shreds": f.kinds["coding"],
            "duplicates": f.duplicates,
            "unparseable": f.unparseable,
            "oversize": f.oversize,
            "kernel_drops": drops,
            "coverage_pct": round(100.0 * len(f.first) / len(union), 3) if union else 0.0,
            "missing_vs_union": len(union) - len(f.first),
            "first_arrivals": wins[f.name],
            "win_rate_pct": round(100.0 * wins[f.name] / len(union), 3) if union else 0.0,
        })

    for f in feeds:
        if f is baseline:
            continue
        both = f.first.keys() & baseline.first.keys()
        deltas = sorted((f.first[k] - baseline.first[k]) * 1000.0 for k in both)
        report["deltas_vs_baseline_ms"][f.name] = {
            "compared_shreds": len(both),
            "p1": round(percentile(deltas, 1), 3),
            "p10": round(percentile(deltas, 10), 3),
            "p50": round(percentile(deltas, 50), 3),
            "p90": round(percentile(deltas, 90), 3),
            "p99": round(percentile(deltas, 99), 3),
            "only_on_baseline": len(baseline.first.keys() - f.first.keys()),
            "only_on_this_feed": len(f.first.keys() - baseline.first.keys()),
        }

    return report


def render(report):
    out = []
    out.append(f"\nwindow {report['window_seconds']}s   "
               f"union {report['union_shreds']} shreds   "
               f"baseline '{report['baseline']}'\n")

    head = (f"{'feed':<14}{'pkts/s':>9}{'unique':>10}{'cover%':>9}"
            f"{'win%':>8}{'missing':>9}{'drops':>8}{'bad':>6}")
    out.append(head)
    out.append("-" * len(head))
    for f in report["feeds"]:
        drops = "-" if f["kernel_drops"] is None else str(f["kernel_drops"])
        out.append(f"{f['name']:<14}{f['datagrams_per_sec']:>9}"
                   f"{f['unique_shreds']:>10}{f['coverage_pct']:>9.2f}"
                   f"{f['win_rate_pct']:>8.2f}{f['missing_vs_union']:>9}"
                   f"{drops:>8}{f['unparseable'] + f['oversize']:>6}")

    if report["deltas_vs_baseline_ms"]:
        out.append(f"\narrival delta vs '{report['baseline']}' "
                   f"(ms, negative = faster than baseline)\n")
        head2 = (f"{'feed':<14}{'p1':>9}{'p10':>9}{'p50':>9}{'p90':>9}"
                 f"{'p99':>9}{'only-base':>11}{'only-this':>11}")
        out.append(head2)
        out.append("-" * len(head2))
        for name, d in report["deltas_vs_baseline_ms"].items():
            out.append(f"{name:<14}{d['p1']:>+9.2f}{d['p10']:>+9.2f}"
                       f"{d['p50']:>+9.2f}{d['p90']:>+9.2f}{d['p99']:>+9.2f}"
                       f"{d['only_on_baseline']:>11}{d['only_on_this_feed']:>11}")

    warn = [f for f in report["feeds"] if f["rcvbuf_bytes"] < 16 << 20]
    if warn:
        out.append(f"\nnote: receive buffer is {warn[0]['rcvbuf_bytes'] // 1024} KiB. "
                   "If drops are non-zero you are measuring your own socket, "
                   "not the network. Raise net.core.rmem_max and re-run.")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(
        description="Compare raw Solana shred feeds arriving on one host.")
    ap.add_argument("--feed", action="append", required=True, metavar="NAME=PORT",
                    help="repeatable, e.g. --feed jito=20000 --feed frankfurt=20001")
    ap.add_argument("--seconds", type=int, default=900)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--baseline", default=None,
                    help="feed name to measure others against (default: first)")
    ap.add_argument("--rcvbuf", type=int, default=64 << 20,
                    help="SO_RCVBUF request in bytes (kernel clamps to rmem_max)")
    ap.add_argument("--json", metavar="PATH", help="also write the report as JSON")
    args = ap.parse_args()

    feeds = build_feeds(args.feed, args.bind, args.rcvbuf)
    for f in feeds:
        print(f"feed {f.name:<14} port {f.port:<6} rcvbuf {f.rcvbuf // 1024} KiB")
    print(f"capturing for {args.seconds}s (ctrl-c to stop early)...")

    elapsed = capture(feeds, args.seconds)
    report = analyse(feeds, elapsed, args.baseline or feeds[0].name)

    sys.stdout.write(render(report))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"json written to {args.json}")


if __name__ == "__main__":
    main()
