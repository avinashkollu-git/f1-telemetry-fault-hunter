"""Generate a synthetic CAN bus log of a car lapping a made-up circuit, with
intermittent faults injected at known times (the ground truth).

Output (in --out, default data/synth):
  can_log.txt       one line per frame: "<t_s> <ID hex> <DLC> <data hex> <CRC-15 hex>",
                    or "<t_s> ERR" for an error frame seen on the bus
  ground_truth.csv  every injected fault: type, can_id, signal, t_start, t_end, note
  hb_slots.csv      every scheduled radio heartbeat slot and whether the hidden
                    trigger condition was true there (used to score the root-cause helper)
  truth.json        seed, trigger rule, fault counts
"""
import argparse
import csv
import json
from pathlib import Path

import numpy as np

from fh.can import encode_raw, frame_crc, load_signals, pack

TRACK_LEN = 5200.0  # m
# (distance into lap in m, apex speed km/h) for a made-up circuit with one long main straight.
CORNERS = [(600, 95), (1150, 185), (1550, 245), (2600, 85), (3000, 160), (3400, 120), (4500, 200)]
GEAR_TOP = np.array([100, 135, 165, 195, 225, 260, 295, 360.0])  # km/h where each gear reaches 12000 rpm

# The hidden intermittent fault: the radio heartbeat is lost only when all three hold.
# It lives here, not in the analysis code, so the analysis has to rediscover it.
TRIGGER_TEXT = "gear == 8 and rpm >= 11000 and water_temp >= 100"


def hb_trigger(gear, rpm, water_temp):
    return (gear == 8) & (rpm >= 11000) & (water_temp >= 100)


def simulate_drive(laps, rng):
    """Very simple lap-time simulation on a 1 m grid: accelerate (with drag) as hard as
    possible, brake at 45 m/s^2 to make each corner's apex speed. Returns arrays over distance."""
    n = int(laps * TRACK_LEN)
    s = np.arange(n, dtype=float)
    cap = np.full(n, 400 / 3.6)
    for lap in range(laps):
        for d, apex in CORNERS:
            i = int(lap * TRACK_LEN + d)
            cap[max(0, i - 40):i + 40] = apex / 3.6
    # Top speed varies lap to lap (tow, wind, DRS), so the trigger does not fire every lap.
    vtop = (rng.uniform(340, 356, laps) / 3.6)[(s // TRACK_LEN).astype(int)]
    v = np.empty(n)
    v[0] = 200 / 3.6
    for i in range(1, n):  # forward pass: traction/drag limited acceleration
        a = 14.0 * max(0.0, 1 - (v[i - 1] / vtop[i]) ** 2)
        v[i] = min(cap[i], np.sqrt(v[i - 1] ** 2 + 2 * a))
    for i in range(n - 2, -1, -1):  # backward pass: braking limit
        v[i] = min(v[i], np.sqrt(v[i + 1] ** 2 + 2 * 45.0))
    t = np.concatenate([[0.0], np.cumsum(2.0 / (v[1:] + v[:-1]))])
    accel = np.gradient(v, t)
    on_cap = (v >= cap - 0.05) & (cap < 400 / 3.6)
    throttle = np.where(accel < -2, 0.0, np.where(on_cap, 40.0, 100.0))
    brake = np.where(accel < -2, 2 + 120 * np.minimum(1, -accel / 45), 2.0)
    # Battery: deploy on throttle, harvest under braking.
    dt = np.diff(t, prepend=0.0)
    soc = np.clip(60 + np.cumsum(dt * (-0.08 * throttle / 100 + 0.3 * (brake > 10))), 20, 95)
    # Coolant: first-order lag (tau 20 s) towards a target set by load, a per-lap offset (running
    # in dirty air behind another car runs hotter) and a slow warming trend. The per-lap offset
    # matters: with only the trend, coolant temperature rose with time, so anything else that
    # trends over a stint (battery temperature, SoC) explained the fault just as well.
    lap_offset = rng.uniform(-6, 6, laps)[(s // TRACK_LEN).astype(int)]
    water = np.empty(n)
    water[0] = 90.0
    target = 80 + 18 * throttle / 100 + lap_offset + 0.02 * t
    for i in range(1, n):
        water[i] = water[i - 1] + (target[i] - water[i - 1]) * dt[i] / 20.0
    # Engine mode changes lap to lap (+-3% rpm at the same speed), so rpm is not just a function
    # of speed; otherwise "speed >= X" would explain the fault exactly as well as rpm does.
    mode = rng.uniform(0.97, 1.03, laps)[(s // TRACK_LEN).astype(int)]
    return dict(t=t, s=s, v=v * 3.6, throttle=throttle, brake=brake, soc=soc, water=water, mode=mode)


def gear_rpm(speed, mode=1.0):
    gear = np.clip(np.searchsorted(GEAR_TOP, speed) + 1, 1, 8)
    hi = GEAR_TOP[gear - 1]
    lo = np.where(gear == 1, 0.0, GEAR_TOP[np.maximum(gear - 2, 0)])
    # 9000-12000 rpm band in each gear; first gear starts from 4000 rpm.
    base = np.where(gear == 1, 4000.0, 9000.0)
    rpm = (base + (12000 - base) * (speed - lo) / (hi - lo)) * mode
    return gear, rpm


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--laps", type=int, default=7)
    ap.add_argument("--out", default="data/synth")
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    drive = simulate_drive(args.laps, rng)
    t_end = drive["t"][-1]

    def at(tq, key):
        return np.interp(tq, drive["t"], drive[key])

    sigs = load_signals()
    msgs = {}
    for sig in sigs:
        msgs.setdefault(sig["msg_id"], []).append(sig)

    # Physical values for every frame of every message (noise added where real sensors are noisy).
    frames = {}
    for k, (mid, msig) in enumerate(sorted(msgs.items())):
        p = msig[0]["period_ms"] / 1000
        tq = 0.0007 * k + np.arange(0.01, t_end - 0.05, p)  # small offset per ID so frames interleave
        tq = tq + rng.normal(0, 20e-6, len(tq))  # normal clock jitter, ~20 us
        speed = at(tq, "v")
        gear, rpm = gear_rpm(speed, at(tq, "mode"))
        water = at(tq, "water")
        vals = {
            "rpm": rpm + rng.normal(0, 15, len(tq)),
            "throttle": at(tq, "throttle"),
            "water_temp": water + rng.normal(0, 0.3, len(tq)),
            "brake_front": at(tq, "brake") + rng.normal(0, 0.3, len(tq)),
            "brake_rear": 0.6 * at(tq, "brake") + rng.normal(0, 0.3, len(tq)),
            "gear": gear,
            "speed": speed + rng.normal(0, 0.5, len(tq)),
            "soc": at(tq, "soc"),
            "batt_temp": 30 + 0.02 * tq,
            "lap_distance": at(tq, "s") % TRACK_LEN,
            "hb_counter": np.arange(len(tq)) % 256,
            "link_quality": 95 + rng.integers(-3, 4, len(tq)),
        }
        raws = {s["signal"]: np.array([encode_raw(s, x) for x in vals[s["signal"]]]) for s in msig}
        frames[mid] = dict(t=tq, raw=raws, sigs=msig, period=p,
                           protected=np.zeros(len(tq), bool),
                           trigger=hb_trigger(gear, rpm, water))

    truth = []  # rows of ground_truth.csv

    def add(kind, mid, sig, t0, t1, note=""):
        truth.append(dict(type=kind, can_id=f"{mid:03X}" if mid is not None else "",
                          signal=sig, t_start=round(t0, 6), t_end=round(t1, 6), note=note))

    # Non-overlapping 10 s slots for the windowed faults, so they don't mask each other.
    slots = list(rng.permutation(np.arange(15, t_end - 15, 10.0)))

    def window(length):
        t0 = slots.pop() + rng.uniform(0, 10 - length)
        return t0, t0 + length

    sig_by_name = {s["signal"]: s for s in sigs}

    # --- value faults (applied to raw values, frames still transmitted with a valid CRC) ---
    for name, length in [("rpm", 1.5), ("brake_front", 2.0), ("speed", 1.2)]:
        f = frames[sig_by_name[name]["msg_id"]]
        t0, t1 = window(length)
        idx = np.where((f["t"] >= t0) & (f["t"] <= t1))[0]
        f["raw"][name][idx] = f["raw"][name][idx[0]]  # sensor freezes at its last good reading
        f["protected"][idx] = True
        add("STUCK", sig_by_name[name]["msg_id"], name, f["t"][idx[0]], f["t"][idx[-1]])

    def pick_frames(f, count):
        """Random frames that no other fault touches, at least 1 s apart."""
        chosen = []
        while len(chosen) < count:
            i = int(rng.integers(5, len(f["t"]) - 5))
            if not f["protected"][i - 2:i + 3].any() and all(abs(f["t"][i] - f["t"][j]) > 1 for j in chosen):
                chosen.append(i)
        f["protected"][chosen] = True
        return chosen

    for name, count in [("soc", 4), ("water_temp", 3)]:
        sig = sig_by_name[name]
        f = frames[sig["msg_id"]]
        for i in pick_frames(f, count):
            f["raw"][name][i] = 0xFF  # all-ones raw value: a classic open-circuit/sensor-fail pattern
            add("OUT_OF_RANGE", sig["msg_id"], name, f["t"][i], f["t"][i])

    for name, jump, count in [("rpm", 6000, 5), ("speed", 60, 3)]:
        sig = sig_by_name[name]
        f = frames[sig["msg_id"]]
        for i in pick_frames(f, count):
            phys = f["raw"][name][i] * sig["scale"] + sig["offset"]
            # Spike in whichever direction stays inside the valid range, so only the rate check can catch it.
            new = phys - jump if phys + jump > sig["max"] else phys + jump
            f["raw"][name][i] = encode_raw(sig, new)
            add("RATE_SPIKE", sig["msg_id"], name, f["t"][i], f["t"][i])

    # --- bus / timing fault windows ---
    jitter_w = [window(1.0) for _ in range(4)]
    for w in jitter_w:
        add("JITTER", 0x0C0, "", *w, "ENGINE ECU frames delayed by random 0-4.5 ms")
    busoff_w = []
    for _ in range(2):
        t0, _unused = window(0.2)
        busoff_w.append((t0, t0 + 0.15))
        add("BUS_OFF", 0x0D0, "", t0 - 32 * 0.00025, t0 + 0.15, "32 error frames then BRAKE ECU silent 150 ms")
    emi_w = [window(0.3) for _ in range(3)]
    for w in emi_w:
        add("EMI_BURST", None, "", *w, "bit-flip probability 0.15 per frame")

    def inside(t, wins):
        return any(a <= t <= b for a, b in wins)

    # --- frame-level faults while writing the log ---
    lines = []  # (t, text)
    for t0, _ in busoff_w:
        lines += [(t0 - (32 - j) * 0.00025, "ERR") for j in range(32)]

    hb_slots = []
    for mid, f in sorted(frames.items()):
        dlc = f["sigs"][0]["dlc"]
        for k, t in enumerate(f["t"]):
            if mid == 0x3F0:
                hb_slots.append((t, int(f["trigger"][k])))
                if f["trigger"][k]:
                    continue  # the hidden fault: heartbeat never reaches the bus
            if mid == 0x0D0 and inside(t, busoff_w):
                continue
            if not f["protected"][k] and rng.random() < 0.001:
                add("DROP", mid, "", t, t)
                continue
            if mid == 0x0C0 and inside(t, jitter_w):
                t += rng.uniform(0, 4.5e-3)  # overloaded ECU: random 0-4.5 ms send latency
            data = pack(f["sigs"], [int(f["raw"][s["signal"]][k]) for s in f["sigs"]], dlc)
            crc = frame_crc(mid, dlc, data)
            in_emi = inside(t, emi_w)
            if not f["protected"][k] and rng.random() < (0.15 if in_emi else 0.0003):
                bad = bytearray(data)
                # Distinct bits: flipping the same bit twice cancels out and leaves a valid frame.
                for bit in rng.choice(8 * dlc, size=int(rng.integers(1, 4)) if in_emi else 1, replace=False):
                    bad[bit // 8] ^= 0x80 >> (bit % 8)
                # Corrupted frame on the wire (CRC was computed by the sender over the good data),
                # receivers answer with an error frame, sender retransmits.
                lines.append((t, f"{mid:03X} {dlc} {bytes(bad).hex().upper()} {crc:04X}"))
                lines.append((t + 0.00010, "ERR"))
                add("BITFLIP", mid, "", t, t, "emi" if in_emi else "isolated")
                t += 0.00015
            lines.append((t, f"{mid:03X} {dlc} {data.hex().upper()} {crc:04X}"))

    # HB_LOSS ground truth = each run of consecutive triggered heartbeat slots.
    run = []
    for t, trig in sorted(hb_slots) + [(None, 0)]:
        if trig:
            run.append(t)
        elif run:
            add("HB_LOSS", 0x3F0, "", run[0], run[-1], f"{len(run)} heartbeats lost")
            run = []

    lines.sort(key=lambda x: x[0])
    with open(out / "can_log.txt", "w") as fh:
        for t, text in lines:
            fh.write(f"{t:.6f} {text}\n")
    with open(out / "ground_truth.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["type", "can_id", "signal", "t_start", "t_end", "note"])
        w.writeheader()
        w.writerows(sorted(truth, key=lambda r: r["t_start"]))
    with open(out / "hb_slots.csv", "w") as fh:
        fh.write("t,trigger\n")
        fh.writelines(f"{t:.6f},{trig}\n" for t, trig in sorted(hb_slots))
    counts = {}
    for r in truth:
        counts[r["type"]] = counts.get(r["type"], 0) + 1
    n_frames = sum(1 for _, x in lines if x != "ERR")
    summary = dict(seed=args.seed, laps=args.laps, duration_s=round(t_end, 2), log_lines=len(lines),
                   frames=n_frames, trigger_rule=TRIGGER_TEXT, injected=counts)
    (out / "truth.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
