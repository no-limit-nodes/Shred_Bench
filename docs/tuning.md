# Tuning a host to receive shreds without losing them

If `shredbench` reports non-zero `drops`, **you are measuring your own socket,
not the network.** Fix that before you draw any conclusion about a provider.

UDP has no backpressure. A shred provider forwards at line rate and never asks
whether you are keeping up. If you are not, packets are discarded — at the NIC,
in the kernel, or at the socket — and *nothing raises an error*. Your `recv()`
loop keeps returning packets. They are just not all the packets.

## Where packets die

```
wire → [1] NIC ring buffer
     → [2] kernel softirq backlog
     → [3] socket receive buffer      ← usually this one
     → [4] your application's queue
```

Diagnose in reverse order of likelihood — start at [3].

### [3] Socket receive buffer

```bash
# per-socket drops; last column is 'drops'. 0x4E20 = port 20000
cat /proc/net/udp | awk 'NR==1 || $2 ~ /:4E20$/'

# system-wide
cat /proc/net/snmp | grep -A1 '^Udp:'
```

Fields are `InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors
InCsumErrors IgnoredMulti MemErrors`. **`RcvbufErrors` rising means your receive
buffer overflowed.**

`netstat -su` prints the same as *"receive buffer errors"*; `ss -ulmn` shows
per-socket memory with a `d<n>` drop count in `skmem`.

### [2] Kernel backlog

```bash
# column 2 = dropped, column 3 = time_squeeze, one row per CPU
cat /proc/net/softnet_stat
```

Non-zero column 2 means packets were discarded before reaching any socket
(`net.core.netdev_max_backlog` filled). Column 3 rising means softirq processing
ran out of budget. Both indicate a host under general load.

### [1] NIC

```bash
ethtool -S eth0 | grep -Ei 'drop|miss|err|fifo|nobuf'
```

`rx_missed_errors` or `rx_no_buffer_count` climbing means the card filled its ring
before the kernel drained it. Raise it: `ethtool -G eth0 rx 4096`.

### [4] Your code

All counters zero and still missing shreds? The loss is inside your process — a
bounded channel dropping when full, a parser blocking the read loop, work done
between `recv()` calls. Only your own instrumentation sees this.

## The buffer is a time budget, not a size

The only useful way to think about `SO_RCVBUF`:

```
headroom_ms = rcvbuf_bytes / feed_bytes_per_sec × 1000
```

Measure your feed rate rather than guessing:

```bash
sudo timeout 60 tcpdump -ni eth0 -q "udp dst port 20000" -w /tmp/s.pcap
capinfos -d /tmp/s.pcap        # bytes ÷ 60 = bytes/sec
```

At a feed rate around 6.8 MB/s:

| Buffer | Headroom |
|---|---|
| 212,992 B (common Linux default) | **~31 ms** |
| 8 MiB | ~1.2 s |
| 128 MiB | ~19.7 s |

**Thirty-one milliseconds is the default.** One GC pause, one page fault, one
slow log flush blows through it, and every packet arriving during the stall is
gone silently.

## Two gotchas that waste an afternoon

**The kernel returns double what you set.** Ask for 1 MB, `getsockopt` reports
2,000,000. It adds an equal allowance for bookkeeping. Expected.

**The kernel silently clamps to `net.core.rmem_max`.** Ask for 64 MiB where
`rmem_max` is 4 MiB and you get 8 MiB back, with no error:

```python
>>> s.setsockopt(SOL_SOCKET, SO_RCVBUF, 64 << 20)   # ask for 64 MiB
>>> s.getsockopt(SOL_SOCKET, SO_RCVBUF)
8388608                                             # got 8 MiB. no error.
```

**Always read back what you got and log it at startup.** `shredbench` prints it
on every run for exactly this reason.

## Fixing it

```bash
sudo sysctl -w net.core.rmem_max=134217728
sudo sysctl -w net.core.netdev_max_backlog=10000

# persist
cat <<'EOF' | sudo tee /etc/sysctl.d/99-shreds.conf
net.core.rmem_max = 134217728
net.core.netdev_max_backlog = 10000
EOF
```

A bigger buffer buys time, not throughput. If your consumer is slower than the
feed on average, no buffer size saves you — it only changes how long you take to
fall over.

## When the buffer is huge and you still drop

The read path is too slow. Architecture, not sysctls:

- **Read on a thread that does nothing else.** `recv()` into a preallocated ring
  buffer. No parsing, no per-packet allocation, no logging, no locks held across
  a read.
- **Bounded queue, deliberate drops.** When full you will lose data; the only
  question is whether you choose which. Drop the *oldest* — stale shreds are
  worthless — and count it.
- **Measure queue age, not depth.** Depth says how much work waits. Age says how
  stale the front is, which decides whether it is worth doing.
- **`recvmmsg` batches syscalls** and helps meaningfully at these rates.

## Bursty loss vs scattered loss

Reed-Solomon recovers any FEC set from any 32 of its 64 shreds — a lot of
tolerance. But buffer overflow loss is **bursty**, not uniform: a stall discards
everything arriving during it, easily more than 32 consecutive shreds from one
set.

- **Scattered loss** → FEC absorbs it. You may never notice.
- **Bursty loss** → whole FEC sets die. You lose entire slots.

**Losing whole slots is the signature of a stall, not a bad network.** Check your
buffer and consumer before you email your provider.

Measure loss *after* FEC recovery. Pre-recovery gaps are expected and mostly
meaningless.

## Monitor these permanently

A UDP feed gives you no session, so every signal is local.

| Metric | Source | Alert when |
|---|---|---|
| **Packet age** | your receiver | time since last accepted datagram exceeds threshold |
| Receive rate | your receiver | datagrams/sec outside expected band |
| Socket drops | `/proc/net/udp`, last column | any increase |
| Buffer errors | `/proc/net/snmp` `RcvbufErrors` | any increase |
| Backlog drops | `/proc/net/softnet_stat` col 2 | any increase |
| Queue age | your ring buffer | front-of-queue age exceeds usefulness |
| Post-FEC gaps | your reconstructor | missing shreds after recovery |

**Packet age is the one that catches an outage.** Everything else diagnoses; that
one pages. If you build only one, build that.

## Five-minute checklist

1. `cat /proc/net/udp` — drop column moving for your port?
2. `cat /proc/net/snmp | grep -A1 Udp:` — `RcvbufErrors` climbing?
3. `cat /proc/net/softnet_stat` — column 2 non-zero?
4. `ethtool -S eth0 | grep -i drop` — NIC missing frames?
5. Log your actual `SO_RCVBUF` — is it what you asked for?
6. `sudo sysctl -w net.core.rmem_max=134217728`, re-run.
7. Still dropping? It is your consumer. Move parsing off the read thread.

## Further reading

- [`docs/shred-format.md`](shred-format.md) — the wire format, FEC sets, bandwidth maths
- [Solana ShredStream guide](https://nolimitnodes.com/blog/solana-shredstream-guide) — Turbine, deshredding, fork tracking
- [ShredStream product docs](https://nolimitnodes.com/products/shredstream) — destination verification and setup
