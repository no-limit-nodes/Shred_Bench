#!/usr/bin/env python3
"""
fakefeed.py — synthetic shred generator for testing shredbench without
paying for two live feeds. Emits well-formed shred headers to N ports with
configurable per-feed lag and loss.

    ./fakefeed.py --to fast=20000:0:0.001 --to slow=20001:0.8:0.02 --slots 200

Each --to is NAME=PORT:LAG_MS:LOSS_FRACTION.
"""

import argparse
import random
import socket
import struct
import sys
import time

SLOT_INDEX = struct.Struct("<QI")
MERKLE_DATA = 0x84
MERKLE_CODE = 0x44


def make_shred(slot, index, kind):
    pkt = bytearray(1203 if kind == "data" else 1228)
    pkt[0x40] = MERKLE_DATA if kind == "data" else MERKLE_CODE
    SLOT_INDEX.pack_into(pkt, 0x41, slot, index)
    return bytes(pkt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", action="append", required=True,
                    metavar="NAME=PORT:LAG_MS:LOSS")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--slots", type=int, default=100)
    ap.add_argument("--slot-interval", type=float, default=0.05)
    args = ap.parse_args()

    targets = []
    for spec in args.to:
        name, _, rest = spec.partition("=")
        port, lag_ms, loss = rest.split(":")
        targets.append({
            "name": name,
            "port": int(port),
            "lag": float(lag_ms) / 1000.0,
            "loss": float(loss),
            "sock": socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
        })
        print(f"target {name} port {port} lag {lag_ms}ms loss {loss}")

    sent = {t["name"]: 0 for t in targets}
    for slot in range(args.slots):
        # SIMD-0317 shape: 32 data + 32 coding shreds per FEC set.
        shreds = [(i, "data") for i in range(32)] + \
                 [(i, "coding") for i in range(32, 64)]
        for index, kind in shreds:
            pkt = make_shred(slot, index, kind)
            for t in sorted(targets, key=lambda x: x["lag"]):
                if t["lag"]:
                    time.sleep(t["lag"])
                if random.random() >= t["loss"]:
                    t["sock"].sendto(pkt, (args.host, t["port"]))
                    sent[t["name"]] += 1
        time.sleep(args.slot_interval)

    print("sent:", sent, file=sys.stderr)


if __name__ == "__main__":
    main()
