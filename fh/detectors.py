"""Fault detectors over the decoder's output.

Every detector returns a DataFrame of events with the same columns (EVENT_COLS), so they can be
concatenated, grouped by signature and scored against the ground truth in one place.
"""
import numpy as np
import pandas as pd

EVENT_COLS = ["type", "can_id", "signal", "t_start", "t_end", "count", "detail"]


def merge_times(times, max_gap):
    """Group sorted timestamps into runs where consecutive points are <= max_gap apart.
    Returns a list of (start, end, count)."""
    runs = []
    for t in np.sort(np.asarray(times, dtype=float)):
        if runs and t - runs[-1][1] <= max_gap:
            runs[-1][1] = t
            runs[-1][2] += 1
        else:
            runs.append([t, t, 1])
    return [tuple(r) for r in runs]


def equal_runs(values):
    """(start, end) index pairs, end exclusive, for each run of identical consecutive values."""
    change = np.flatnonzero(np.diff(values) != 0) + 1
    return list(zip(np.concatenate([[0], change]), np.concatenate([change, [len(values)]])))


def _events(rows):
    return pd.DataFrame(rows, columns=EVENT_COLS)


def series(decoded, signal):
    d = decoded[decoded.signal == signal]
    return d.t.to_numpy(), d.raw.to_numpy(), d.value.to_numpy(), d.can_id.iloc[0] if len(d) else ""


def stuck_at(decoded, sigs, min_run=40):
    """A noisy sensor that repeats the exact same raw value min_run times in a row is frozen.
    Only applied to signals marked noisy: throttle at 100% or gear 8 legitimately sit still."""
    rows = []
    for sig in (s for s in sigs if s["noisy"]):
        t, raw, _, cid = series(decoded, sig["signal"])
        if len(raw) == 0:
            continue
        for a, b in equal_runs(raw):
            if b - a >= min_run:
                rows.append(["STUCK", cid, sig["signal"], t[a], t[b - 1], b - a, f"raw={raw[a]} x{b - a}"])
    return _events(rows)


def out_of_range(decoded, sigs, merge_gap=0.1):
    rows = []
    for sig in sigs:
        t, _, val, cid = series(decoded, sig["signal"])
        bad = (val < sig["min"]) | (val > sig["max"])
        for a, b, n in merge_times(t[bad], merge_gap):
            v = val[bad][(t[bad] >= a) & (t[bad] <= b)]
            rows.append(["OUT_OF_RANGE", cid, sig["signal"], a, b, n,
                         f"value {v[0]:g} outside [{sig['min']:g}, {sig['max']:g}]"])
    return _events(rows)


def rate_of_change(decoded, sigs, merge_gap=0.1):
    """|dvalue/dt| above what the physics allows (max_rate in the signal table).
    dt is floored at the nominal period: timestamps are bus arrival times, and a frame delayed by
    jitter followed by an on-time one would otherwise look like a huge rate over a tiny dt."""
    rows = []
    for sig in (s for s in sigs if s["max_rate"]):
        t, _, val, cid = series(decoded, sig["signal"])
        rate = np.abs(np.diff(val) / np.maximum(np.diff(t), sig["period_ms"] / 1000))
        hit = np.flatnonzero(rate > sig["max_rate"]) + 1
        for a, b, n in merge_times(t[hit], merge_gap):
            in_event = rate[hit - 1][(t[hit] >= a) & (t[hit] <= b)]
            rows.append(["RATE_SPIKE", cid, sig["signal"], a, b, n,
                         f"max {in_event.max():.0f}/s > {sig['max_rate']:g}/s"])
    return _events(rows)


def missing_heartbeat(decoded, period, t_log_end, signal="hb_counter", timeout_periods=2.5):
    """Heartbeat silent for longer than timeout_periods. The event spans the silence, and the
    counter jump across it says how many heartbeats were really lost."""
    t, raw, _, cid = series(decoded, signal)
    rows = []
    for i in np.flatnonzero(np.diff(t) > timeout_periods * period):
        lost = (int(raw[i + 1]) - int(raw[i]) - 1) % 256
        rows.append(["HB_LOSS", cid, "", t[i] + period, t[i + 1] - period, lost, f"{lost} heartbeats lost"])
    if len(t) and t_log_end - t[-1] > timeout_periods * period:  # still silent when the log ends
        rows.append(["HB_LOSS", cid, "", t[-1] + period, t_log_end, 0, "silent until end of log"])
    return _events(rows)


def crc_clusters(anomalies, max_gap=0.1, min_count=4):
    """Isolated CRC errors happen; several close together point at a shared cause (EMI, a loose
    connector, a bad termination), which is worth its own fault entry."""
    t = anomalies[anomalies.type == "CRC_ERROR"].t
    rows = [["CRC_CLUSTER", "", "", a, b, n, f"{n} CRC errors"]
            for a, b, n in merge_times(t, max_gap) if n >= min_count]
    return _events(rows)


def bus_off(anomalies, max_gap=0.005, min_errors=16, silence_periods=5):
    """A burst of error frames followed by one ECU going silent = that ECU went bus-off
    (transmit error counter > 255) and stopped transmitting until it recovered."""
    errs = anomalies[anomalies.type == "ERROR_FRAME"].t
    gaps = anomalies[anomalies.type == "GAP"]
    rows = []
    for a, b, n in merge_times(errs, max_gap):
        if n < min_errors:
            continue
        # The silent ECU shows up as a long GAP that closes shortly after the burst.
        g = gaps[(gaps.t > b) & (gaps.t < b + 1.0) & (gaps.detail.astype(int) >= silence_periods)]
        cid = g.can_id.iloc[0] if len(g) else ""
        end = g.t.iloc[0] if len(g) else b
        rows.append(["BUS_OFF", cid, "", a, end, n, f"{n} error frames, then {cid or 'no ID'} silent"])
    return _events(rows)


def dropped_frames(anomalies, max_missing=2):
    """Short gaps (1-2 frames missing) on a periodic ID. Longer silences belong to bus-off or
    heartbeat loss and are reported by those detectors instead."""
    g = anomalies[anomalies.type == "GAP"]
    g = g[g.detail.astype(int) <= max_missing]
    return _events([["DROP", r.can_id, "", r.t, r.t, int(r.detail), f"{r.detail} frame(s) missing"]
                    for r in g.itertuples()])


def timing_jitter(anomalies, merge_gap=0.5):
    rows = []
    j = anomalies[anomalies.type == "JITTER"]
    for cid, grp in j.groupby("can_id"):
        rows += [["JITTER", cid, "", a, b, n, f"{n} late/early frames"] for a, b, n in merge_times(grp.t, merge_gap)]
    return _events(rows)


def run_all(decoded, anomalies, sigs, hb_period, t_log_end):
    parts = [stuck_at(decoded, sigs), out_of_range(decoded, sigs), rate_of_change(decoded, sigs),
             missing_heartbeat(decoded, hb_period, t_log_end), crc_clusters(anomalies),
             bus_off(anomalies), dropped_frames(anomalies), timing_jitter(anomalies)]
    ev = pd.concat([p for p in parts if len(p)], ignore_index=True)
    # When a frozen sensor comes back, its value jumps to the real one and trips the rate check.
    # That spike is a symptom of the STUCK fault, not a second fault, so drop it (like keeping only
    # the first failure in a cascade when triaging a regression).
    stuck = ev[ev.type == "STUCK"]
    cascade = ev.apply(lambda r: r.type == "RATE_SPIKE" and bool(
        ((stuck.signal == r.signal) & (r.t_start > stuck.t_end) & (r.t_start <= stuck.t_end + 0.1)).any()), axis=1)
    # A short heartbeat loss (2 missing) also looks like a 2-frame drop; report it once, as HB_LOSS.
    hb = ev[ev.type == "HB_LOSS"]
    dup = ev.apply(lambda r: r.type == "DROP" and bool(
        ((hb.can_id == r.can_id) & (r.t_start >= hb.t_start) & (r.t_start <= hb.t_end + 2 * hb_period)).any()), axis=1)
    return ev[~cascade & ~dup].sort_values("t_start", ignore_index=True)


def signatures(events):
    """Bucket events like a chip-verification regression triages failures: same fault type on the
    same ID/signal = same signature, so 112 log lines become a handful of things to debug."""
    sig = events.type + " " + events.can_id.astype(str).replace("", "-") + " " + events.signal.astype(str)
    return (events.assign(signature=sig.str.strip())
            .groupby("signature")
            .agg(events=("type", "size"), first_t=("t_start", "min"), last_t=("t_end", "max"))
            .sort_values("events", ascending=False)
            .reset_index())
