"""CAN 2.0A frame helpers shared by the generator and the Python tests.

The C++ decoder has its own implementation of the same CRC (decoder/can.cpp);
the tests check that the two agree, so a bug in one shows up as a mismatch.
"""
import csv
from pathlib import Path

CRC15_POLY = 0x4599  # x^15 + x^14 + x^10 + x^8 + x^7 + x^4 + x^3 + 1 (Bosch CAN spec)
SIGNALS_CSV = Path(__file__).resolve().parent.parent / "config" / "signals.csv"


def crc15(bits):
    """Bitwise CRC-15/CAN over a sequence of 0/1 ints (init 0, no final XOR)."""
    crc = 0
    for b in bits:
        crc_next = b ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if crc_next:
            crc ^= CRC15_POLY
    return crc


def bytes_to_bits(data):
    return [(byte >> (7 - i)) & 1 for byte in data for i in range(8)]


def frame_bits(can_id, dlc, data):
    """Bits covered by the CRC in a CAN 2.0A data frame, before bit stuffing:
    SOF(0), 11-bit ID, RTR(0), IDE(0), r0(0), 4-bit DLC, data bytes. MSB first."""
    bits = [0]
    bits += [(can_id >> (10 - i)) & 1 for i in range(11)]
    bits += [0, 0, 0]
    bits += [(dlc >> (3 - i)) & 1 for i in range(4)]
    bits += bytes_to_bits(data[:dlc])
    return bits


def frame_crc(can_id, dlc, data):
    return crc15(frame_bits(can_id, dlc, data))


def load_signals(path=SIGNALS_CSV):
    """Read the DBC-like signal table. Returns a list of dicts with typed fields."""
    out = []
    with open(path) as f:
        for row in csv.DictReader(f):
            out.append({
                "msg_id": int(row["msg_id"], 16),
                "msg_name": row["msg_name"],
                "period_ms": float(row["period_ms"]),
                "dlc": int(row["dlc"]),
                "signal": row["signal"],
                "start_byte": int(row["start_byte"]),
                "num_bytes": int(row["num_bytes"]),
                "scale": float(row["scale"]),
                "offset": float(row["offset"]),
                "min": float(row["min"]),
                "max": float(row["max"]),
                "max_rate": float(row["max_rate"]) if row["max_rate"] else None,
                "noisy": row["noisy"] == "1",
            })
    return out


def encode_raw(sig, value):
    """Physical value -> unsigned raw integer, clamped to the field width."""
    raw = round((value - sig["offset"]) / sig["scale"])
    return max(0, min(raw, (1 << (8 * sig["num_bytes"])) - 1))


def pack(sigs, raws, dlc):
    """Place raw values little-endian (Intel byte order) into a data field."""
    data = bytearray(dlc)
    for sig, raw in zip(sigs, raws):
        for i in range(sig["num_bytes"]):
            data[sig["start_byte"] + i] = (raw >> (8 * i)) & 0xFF
    return bytes(data)
