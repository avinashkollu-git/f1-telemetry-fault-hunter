"""Plain-assert tests for the Python side, plus an end-to-end check that the Python encoder and
the C++ decoder agree. Run from the repo root: PYTHONPATH=. python tests/test_python.py"""
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from fh import detectors, rootcause
from fh.can import bytes_to_bits, crc15, encode_raw, frame_crc, load_signals, pack

SIGS = load_signals()
BY_NAME = {s["signal"]: s for s in SIGS}


def test_crc():
    assert crc15(bytes_to_bits(b"123456789")) == 0x059E  # reveng catalogue check value
    assert frame_crc(0x0C0, 4, bytes.fromhex("3C25C87C")) == 0x430E  # same vector as the C++ test


def test_encode_pack():
    rpm, thr = BY_NAME["rpm"], BY_NAME["throttle"]
    assert encode_raw(rpm, 9532) == 9532 and encode_raw(rpm, 1e9) == 0xFFFF  # clamps to field width
    assert encode_raw(BY_NAME["water_temp"], 95) == 135  # offset -40
    assert pack([rpm, thr], [0x253C, 200], 4) == bytes([0x3C, 0x25, 200, 0])  # little-endian


def test_decoder_end_to_end():
    """Write a tiny log with Python, decode it with C++, check every anomaly class appears."""
    rpm = [s for s in SIGS if s["msg_id"] == 0x0C0]
    lines = []
    for k, t in enumerate([0.00, 0.01, 0.02, 0.05, 0.06]):  # 0.02 -> 0.05: 2 frames missing
        data = pack(rpm, [encode_raw(s, v) for s, v in zip(rpm, [9000 + k, 50, 95])], 4)
        lines.append(f"{t:.6f} 0C0 4 {data.hex().upper()} {frame_crc(0x0C0, 4, data):04X}")
    good = bytes.fromhex("3C25C87C")
    bad = bytes([good[0] ^ 0x01]) + good[1:]  # one bit flipped after the CRC was computed
    lines += [f"0.070000 0C0 4 {bad.hex().upper()} {frame_crc(0x0C0, 4, good):04X}",
              "0.070100 ERR",
              f"0.071000 123 1 00 {frame_crc(0x123, 1, bytes(1)):04X}",  # not in the signal table
              "0.072000 0C0 4 nonsense"]
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "log.txt"
        log.write_text("\n".join(lines) + "\n")
        subprocess.run(["build/can_decode", str(log), "config/signals.csv", d], check=True, capture_output=True)
        an = pd.read_csv(Path(d) / "anomalies.csv", dtype={"can_id": str, "detail": str})
        dec = pd.read_csv(Path(d) / "decoded.csv", dtype={"can_id": str})
    types = an.type.value_counts().to_dict()
    assert types == {"GAP": 1, "CRC_ERROR": 1, "ERROR_FRAME": 1, "UNKNOWN_ID": 1, "PARSE_ERROR": 1}, types
    assert an[an.type == "GAP"].detail.iloc[0] == "2"
    assert dec[dec.signal == "rpm"].value.tolist() == [9000, 9001, 9002, 9003, 9004]
    assert dec[dec.signal == "water_temp"].value.iloc[0] == 95


def decoded_frame(signal, values, period):
    sig = BY_NAME[signal]
    return pd.DataFrame({"t": np.arange(len(values)) * period, "can_id": f"{sig['msg_id']:03X}",
                         "signal": signal, "raw": [encode_raw(sig, v) for v in values], "value": values})


def test_detectors():
    assert detectors.merge_times([0.0, 0.05, 0.5, 0.52, 3.0], 0.1) == [(0.0, 0.05, 2), (0.5, 0.52, 2), (3.0, 3.0, 1)]
    rng = np.random.default_rng(0)
    rpm = 10000 + rng.normal(0, 15, 300).round()
    rpm[100:160] = 9500       # 60 frozen samples (value far from the noise, so the run is exactly 60)
    rpm[250] += 6000          # one impossible jump
    d = decoded_frame("rpm", rpm, 0.01)
    st = detectors.stuck_at(d, SIGS)
    assert len(st) == 1 and st["count"].iloc[0] == 60
    rs = detectors.rate_of_change(d, SIGS)
    assert len(rs) == 1 and abs(rs.t_start.iloc[0] - 2.50) < 1e-9
    soc = decoded_frame("soc", [50, 50, 127.5, 50], 0.1)
    assert len(detectors.out_of_range(soc, SIGS)) == 1
    # Heartbeat at 50 ms, slots 3..5 missing; counter jumps 2 -> 6 = 3 lost.
    hb = decoded_frame("hb_counter", [0, 1, 2, 6, 7], 0.05)
    hb["t"] = [0.0, 0.05, 0.10, 0.30, 0.35]
    ev = detectors.missing_heartbeat(hb, 0.05, t_log_end=0.35)
    assert len(ev) == 1 and ev["count"].iloc[0] == 3


def test_rule_search_finds_planted_rule():
    rng = np.random.default_rng(1)
    X = pd.DataFrame({"a": rng.uniform(0, 10, 3000), "b": rng.integers(1, 9, 3000).astype(float),
                      "c": rng.uniform(0, 1, 3000)})
    y = ((X.a >= 7) & (X.b == 8)).to_numpy()
    terms, history, _ = rootcause.find_rule(X, y)
    assert {t[0] for t in terms} == {"a", "b"}, terms
    assert history[-1]["f1"] > 0.98


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)}/{len(tests)} python tests passed")
