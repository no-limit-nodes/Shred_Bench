#!/usr/bin/env python3
"""
sidebyside.py — compare arrival times of two raw Solana shred feeds.

Both feeds must land on the same host, so a single monotonic clock times
both and no cross-host clock sync is required.

    python3 sidebyside.py --a 20000 --b 20001 --seconds 600

Feed A is the baseline (your existing Jito feed).
Positive delta means feed B arrived LATER than feed A.
"""

import argparse
import selectors
import socket
import struct
import time

# slot (u64 LE) at offset 65, index (u32 LE) at offset 73
HEADER = struct.Struct("<QI")
MIN_LEN = 77
MAX_SHRED = 1228


def shred_key(pkt):
    if len(pkt) < MIN_LEN:
        return None
    return HEADER.unpack_from(pkt, 65)


def percentile(sorted_xs, p):
    if not sorted_xs:
        return float("nan")
    i = int(round((p / 100.0) * (len(sorted_xs) - 1)))
    return sorted_xs[min(i, len(sorted_xs) - 1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", type=int, required=True, help="baseline feed port")
    ap.add_argument("--b", type=int, required=True, help="candidate feed port")
    ap.add_argument("--seconds", type=int, default=600)
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()

    sel = selectors.DefaultSelector()
    for name, port in (("A", args.a), ("B", args.b)):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Ask for 64 MiB. The kernel silently clamps this to net.core.rmem_max.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 << 20)
        actual = s.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        print(f"feed {name}: port {port}, rcvbuf {actual // 1024} KiB")
        s.setblocking(False)
        s.bind((args.bind, port))
        sel.register(s, selectors.EVENT_READ, name)

    seen = {"A": {}, "B": {}}
    total = {"A": 0, "B": 0}
    oversize = {"A": 0, "B": 0}
    runt = {"A": 0, "B": 0}

    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        for key, _ in sel.select(timeout=0.5):
            name = key.data
            sock = key.fileobj
            while True:
                try:
                    pkt = sock.recv(2048)
                except BlockingIOError:
                    break
                now = time.monotonic()
                total[name] += 1
                if len(pkt) > MAX_SHRED:
                    oversize[name] += 1
                k = shred_key(pkt)
                if k is None:
                    runt[name] += 1
                elif k not in seen[name]:
                    seen[name][k] = now

    a, b = seen["A"], seen["B"]
    both = a.keys() & b.keys()
    deltas = sorted((b[k] - a[k]) * 1000.0 for k in both)

    print(f"\nwindow            {args.seconds}s")
    for n in ("A", "B"):
        print(f"feed {n}            {total[n]:>9} datagrams  "
              f"{len(seen[n]):>9} unique  "
              f"{oversize[n]} oversize  {runt[n]} short")
    print(f"shreds on both    {len(both):>9}")
    print(f"only on A         {len(a.keys() - b.keys()):>9}")
    print(f"only on B         {len(b.keys() - a.keys()):>9}")
    if deltas:
        print(f"\nB minus A (ms, negative = B is faster)")
        for p in (1, 10, 50, 90, 99):
            print(f"  p{p:<3}            {percentile(deltas, p):>+9.2f}")


if __name__ == "__main__":
    main()
