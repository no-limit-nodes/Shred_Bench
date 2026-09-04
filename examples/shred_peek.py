import socket, struct

# Solana shred common header (solana-foundation/specs, p2p/shred.md).
# Little-endian throughout.
#   0x00  64B  ed25519 signature
#   0x40   1B  variant
#   0x41   8B  u64  slot
#   0x49   4B  u32  shred_index
HEADER = struct.Struct("<QI")          # slot + index, read at offset 0x41

def shred_type(variant):
    if variant == 0xa5: return "data   (legacy)"
    if variant == 0x5a: return "coding (legacy)"
    if variant >> 4 == 0x8: return "data   (merkle)"
    if variant >> 4 == 0x4: return "coding (merkle)"
    return f"unknown 0x{variant:02x}"

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
# Default is ~208 KiB, which holds about 30 ms of a live shred feed.
sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 128 << 20)
print("rcvbuf:", sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF), "bytes")
sock.bind(("0.0.0.0", 20000))

while True:
    pkt = sock.recv(2048)
    if len(pkt) < 83:                  # shorter than the common header
        continue
    slot, index = HEADER.unpack_from(pkt, 0x41)
    print(f"slot {slot}  index {index:<5} {shred_type(pkt[0x40])}  {len(pkt)} bytes")
