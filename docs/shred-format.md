# Solana shred wire format — the parts you need

Everything here is from the [Solana Foundation shred specification](https://github.com/solana-foundation/specs/blob/main/p2p/shred.md).
This is a working subset: enough to identify, classify and count shreds without
a `solana-ledger` dependency. It is not a substitute for the spec.

**Integer byte order is little-endian throughout.**

## Common header (83 bytes, `0x53`)

Every shred — data or coding, legacy or merkle — starts with this.

| Offset | Size | Type | Field |
|---|---|---|---|
| `0x00` | 64 B | ed25519 | `signature` |
| `0x40` | 1 B | `u8` | `variant` |
| `0x41` | 8 B | `u64` | `slot` |
| `0x49` | 4 B | `u32` | `shred_index` |
| `0x4d` | 2 B | `u16` | `shred_version` |
| `0x4f` | 4 B | `u32` | `fec_set_index` |

`(slot, shred_index)` uniquely identifies a shred and is **stable across
providers** — the same shred forwarded by two vendors carries the same pair.
That property is what makes provider comparison possible at all, and it is the
join key `shredbench` uses.

```python
import struct
HEADER = struct.Struct("<QI")            # slot, index
slot, index = HEADER.unpack_from(pkt, 0x41)
```

## The variant byte

The variant at `0x40` packs two 4-bit fields: shred type and authentication.

| High nibble | Low nibble | Type | Auth |
|---|---|---|---|
| `0x5` | `0xa` | coding | legacy |
| `0xa` | `0x5` | data | legacy |
| `0x4` | any | coding | merkle |
| `0x8` | any | data | merkle |

Under merkle authentication the low nibble carries the merkle tree height, so
match on the **high nibble only** for merkle shreds:

```python
def shred_type(variant):
    if variant == 0xa5: return "data"
    if variant == 0x5a: return "coding"
    if variant >> 4 == 0x8: return "data"
    if variant >> 4 == 0x4: return "coding"
    return None
```

Anything else is not a shred. Count it and alarm on it — it means something other
than your provider is reaching that port.

## Sizes

`SHRED_SZ_MAX` is **1228** bytes.

| Auth | Data shred | Coding shred |
|---|---|---|
| Legacy | 1228 | 1228 |
| Merkle | **1203** | 1228 |

**A datagram larger than 1228 bytes is not a shred.** `shredbench` counts these
under `bad`.

## FEC sets and coding overhead

Shreds are grouped into forward-error-correction sets. In current practice
senders emit **32 data shreds + 32 coding shreds** per set. The coding shreds are
Reed-Solomon parity computed over the data shreds, which means any 32 of the 64
reconstruct the whole set.

> [SIMD-0317](https://github.com/solana-foundation/solana-improvement-documents/blob/main/proposals/0317-enforce-32-data-shreds.md)
> would make receivers *enforce* 32+32 and fix the set payload at 31,840 bytes.
> As of this writing it is still in **Review** status and not activated on
> mainnet — so treat 32+32 as current sender behaviour, not a protocol
> guarantee.

The bandwidth consequence is worth internalising:

```
  64 shreds × 1228 bytes         =  78,592 B on the wire
+ 64 datagrams × 28 B IP/UDP     =   1,792 B headers
                                   ─────────
                                    80,384 B transmitted
                                    31,840 B of block payload

              wire : payload  =  2.52 : 1
```

**Roughly 2.5 bytes of network per byte of block data, and half your traffic is
parity.** Budget for it, and check whether your provider meters bandwidth —
at these volumes a per-gigabyte rate can exceed the subscription itself.

## Header layouts past the common header

For reference, if you go on to reconstruct entries:

**Data shred header** (v2), starting at `0x53`:

| Offset | Size | Type | Field |
|---|---|---|---|
| `0x53` | 2 B | `u16` | `parent_offset` |
| `0x55` | 1 B | `u8` | `data_flags` |
| `0x56` | 2 B | `u16` | `size` |
| `0x58` | — | — | payload begins |

**Coding shred header**, starting at `0x53`:

| Offset | Size | Type | Field |
|---|---|---|---|
| `0x53` | 2 B | `u16` | `num_data_shreds` |
| `0x55` | 2 B | `u16` | `num_coding_shreds` |
| `0x57` | 2 B | `u16` | `position` |
| `0x59` | — | — | Reed-Solomon payload begins |

## What the shred layer does not give you

Worth stating explicitly, because it is the most common misunderstanding:

- No transaction logs
- No balance changes
- No error status — you cannot tell whether a transaction succeeded
- No compute-unit usage
- No ordering guarantee — arrival order is **not** ledger order
- No commitment levels
- No fork marking — you will reconstruct entries from slots that never become
  canonical, and nothing in the packet says so

If you need any of the above, you need a post-execution feed such as Yellowstone
gRPC alongside the shred feed. See
[ShredStream vs Yellowstone gRPC](REPLACE-WITH-MEDIUM-URL) for how the two fit
together, and the dual-path architecture that most teams end up running.

## Further reading

- [Solana ShredStream guide](https://nolimitnodes.com/blog/solana-shredstream-guide) — Turbine fanout, the deshredding loop, fork tracking
- [solana-foundation/specs — p2p/shred.md](https://github.com/solana-foundation/specs/blob/main/p2p/shred.md) — the authoritative format
- [`docs/tuning.md`](tuning.md) — why your receiver drops packets and how to stop it
