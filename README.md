# shredbench

[![selftest](https://github.com/nolimitnodes/shredbench/actions/workflows/selftest.yml/badge.svg)](https://github.com/nolimitnodes/shredbench/actions/workflows/selftest.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Compare two or more raw Solana shred feeds on one host, honestly.

Every published shred-latency benchmark in this market was run by the company
that won it. This is the tool for running your own — on your machine, in your
region, against your actual providers.

No dependencies. Python 3.8+. MIT.

```
$ ./shredbench.py --feed provider-a=20000 --feed provider-b=20001 --seconds 3600

window 3600.0s   union 2841503 shreds   baseline 'provider-a'

feed             pkts/s    unique   cover%    win%  missing   drops   bad
-------------------------------------------------------------------------
provider-a       1581.2   2839912    99.94   61.30     1591       0     0
provider-b       1578.9   2836604    99.83   38.70     4899       0     0

arrival delta vs 'provider-a' (ms, negative = faster than baseline)

feed                 p1      p10      p50      p90      p99  only-base  only-this
---------------------------------------------------------------------------------
provider-b        -1.84    -0.42    +0.31    +1.90    +4.77       4899       1591
```

> The block above shows the **shape** of the output, not a published result.
> Generating your own is the entire point of the tool.

---

## Contents

| Path | What it is |
|---|---|
| `shredbench.py` | The benchmark harness |
| `fakefeed.py` | Synthetic shred generator with configurable lag and loss |
| `selftest.py` | Asserts the harness recovers a known lag and loss rate. Runs in CI. |
| `examples/shred_peek.py` | 20 lines: bind a socket, print slot/index/type |
| `examples/sidebyside.py` | Stripped-down two-feed comparison for a migration runbook |
| [`docs/shred-format.md`](docs/shred-format.md) | Wire format, variant bytes, FEC sets, bandwidth maths |
| [`docs/tuning.md`](docs/tuning.md) | Why your receiver drops packets and how to stop it |

## Why one host

The obvious way to benchmark two feeds is to run each on the machine nearest it
and compare timestamps. This does not work — you are comparing two clocks, and
NTP drift on ordinary hardware exceeds the millisecond differences you are trying
to detect. At this scale, clock skew is not a correction you apply, it is the
entire signal.

shredbench sidesteps it: both feeds are delivered to the **same box** on
different ports, and one `CLOCK_MONOTONIC` reading times every packet. No sync,
no skew correction, no trust required.

The cost of that design is that it measures delivery to *one* location. That is
usually what you want — you care about the feed reaching the box that executes,
not a feed averaged over someone's global footprint.

## Install

```bash
git clone https://github.com/nolimitnodes/shredbench
cd shredbench
chmod +x shredbench.py fakefeed.py selftest.py
```

## Use

Point each provider at a different UDP port on the same host, open those ports to
each provider's source address only, then:

```bash
./shredbench.py \
  --feed provider-a=20000 \
  --feed provider-b=20001 \
  --feed provider-c=20002 \
  --baseline provider-a \
  --seconds 3600 \
  --json run-$(date +%F).json
```

| Flag | Default | Meaning |
|---|---|---|
| `--feed NAME=PORT` | required | Repeatable. One per provider. |
| `--seconds N` | `900` | Capture window. Ctrl-C stops early and still reports. |
| `--baseline NAME` | first feed | Which feed the deltas are measured against. |
| `--bind ADDR` | `0.0.0.0` | Listen address. |
| `--rcvbuf BYTES` | `67108864` | `SO_RCVBUF` request. The kernel clamps this. |
| `--json PATH` | — | Write the full report as JSON. |

## What it measures

| Metric | Meaning |
|---|---|
| `cover%` | Share of all shreds seen (union across feeds) that this feed delivered |
| `win%` | Share of shreds where this feed arrived **first**. The metric that matters. |
| `missing` | Shreds another feed delivered and this one did not |
| `drops` | Kernel-side socket drops, from `/proc/net/udp` |
| `bad` | Unparseable or oversize datagrams |
| `p1…p99` | Arrival delta against the baseline feed, in milliseconds |

**Read `cover%` and `drops` before you read the latency numbers.** A feed that
wins on p50 while dropping 2% of shreds is not faster, it is incomplete. And if
`drops` is non-zero you are measuring your own socket rather than the network —
see [`docs/tuning.md`](docs/tuning.md), then run again.

`win%` is the headline. Median delta describes the typical shred; win rate tells
you how often a feed actually got there first, which is closer to what determines
whether you see an opportunity before someone else.

## Before you trust a run

1. **Raise the receive buffer.** The kernel clamps `SO_RCVBUF` to
   `net.core.rmem_max` and reports back *double* what it accepted. shredbench
   prints what it actually got — if that number is small, fix it:
   ```bash
   sudo sysctl -w net.core.rmem_max=134217728
   ```
2. **Run through real congestion.** A quiet Sunday tells you nothing about the
   Friday you actually trade. An hour is a minimum; a full day is better.
3. **Run more than once.** One window is an anecdote; three across different days
   is a finding.
4. **Check `drops` is zero on every feed.** Otherwise you benchmarked your host.
5. **Change nothing else.** Same host, same NIC, same kernel, same window.

## Self-test

`selftest.py` runs a synthetic feed with a known lag and loss rate and asserts
that shredbench recovers both. It runs on every push via GitHub Actions:

```bash
$ python3 selftest.py
  ok    union is close to slots x 64   (union=24026 expected~25600)
  ok    feed a saw everything   (coverage=100.0%)
  ok    feed b coverage recovers injected 3% loss   (coverage=97.012% expected~97.0%)
  ok    p50 delta recovers injected 0.6 ms lag   (p50=0.731ms expected~0.6ms)
  ok    faster feed wins the majority of first arrivals   (win_rate=99.9%)
  ok    no kernel drops on either feed   (drops=[0, 0])
  ok    no unparseable or oversize datagrams
  ok    data and coding shreds classified evenly   (a: 12015d/12011c)

all 8 checks passed
```

`fakefeed.py` is usable on its own:

```bash
./fakefeed.py --to fast=20010:0:0.0 --to slow=20011:0.6:0.03 --slots 500
./shredbench.py --feed a=20010 --feed b=20011 --seconds 30 --bind 127.0.0.1
```

## How shreds are identified

Only the common header — no `solana-ledger` dependency, no decoding, no FEC
reconstruction:

| Offset | Size | Field |
|---|---|---|
| `0x00` | 64 B | ed25519 signature |
| `0x40` | 1 B | variant |
| `0x41` | 8 B | `u64` slot |
| `0x49` | 4 B | `u32` shred index |

`(slot, index)` is stable across providers, which is what makes the comparison
possible. Full detail, including the variant byte and FEC bandwidth maths, is in
[`docs/shred-format.md`](docs/shred-format.md).

Start with `examples/shred_peek.py` if you just want to confirm a feed is alive.

## What this cannot tell you

- **Arrival, not usefulness.** Shreds still have to be reconstructed and decoded.
  If one provider ships raw UDP and another ships decoded transactions, this
  compares packets, not time-to-signal.
- **One destination.** By design. Your Frankfurt result says nothing about Tokyo.
- **Loss upstream of every feed.** If all providers missed a shred it is not in
  the union and nothing here knows it existed.
- **Reliability over time.** A day of measurement tells you about a day.

## Further reading

Written by the NoLimitNodes engineering team:

- [ShredStream vs Yellowstone gRPC: Where the Milliseconds Actually Go](REPLACE-WITH-MEDIUM-URL) — which layer to tap, and the work you inherit going down one
- [Solana ShredStream Guide: Shreds, Turbine, Forks, and Bots](https://nolimitnodes.com/blog/solana-shredstream-guide) — protocol walkthrough
- [Yellowstone gRPC vs WebSockets](https://nolimitnodes.com/blog/yellowstone-grpc-vs-websockets-real-time-solana-data) — the layer above
- [How to Evaluate a Solana Data-Stream Provider](https://nolimitnodes.com/blog/how-to-evaluate-solana-data-stream-provider) — the checklist this tool operationalises
- [Solana VPS vs VDS vs Bare Metal](https://nolimitnodes.com/blog/solana-vps-vds-bare-metal-trading-bot) — what to run the receiver on

Upstream reference: [solana-foundation/specs — p2p/shred.md](https://github.com/solana-foundation/specs/blob/main/p2p/shred.md)

## Invitation

If you sell shred delivery and think this measures you unfairly, open an issue.
If you think it measures you fairly, run it and publish your numbers. A market
where several providers publish comparable results from the same tool is better
for buyers than one where each publishes a benchmark they won.

## Licence

MIT. Built by [NoLimitNodes](https://nolimitnodes.com) — bare-metal Solana
[RPC](https://nolimitnodes.com/products/rpc-nodes),
[Yellowstone gRPC](https://nolimitnodes.com/products/grpc-nodes),
[Enhanced Streams](https://nolimitnodes.com/products/datastreams) and
[ShredStream](https://nolimitnodes.com/products/shredstream) on flat monthly
pricing.

We sell one of the feeds you might point this at — which is exactly why the tool
is open, the methodology is written down, and the code is short enough to read in
ten minutes. Do not take our word for anything; run it.
