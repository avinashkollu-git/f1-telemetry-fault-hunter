"""Apply the data-quality checks that make sense to real public F1 telemetry.

What this can find: gaps and irregular sampling in the broadcast feed, values outside the
documented range, frozen channels. What it cannot do: say anything about faults in a real car.
The public feed is ~4 Hz, already processed, and has no CRCs, error frames or raw bus timing.
Every finding below is a property of the feed as delivered, reported as measured.

Writes results/real_results.json. Skips cleanly if data/real/ has not been fetched.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from fh.detectors import equal_runs

GAP_S = 1.0           # a silence longer than this in a ~4 Hz feed is a gap
STUCK_SAMPLES = 12    # ~3 s of identical RPM while the car is moving
MAX_DECEL_KMH_S = 300 # ~8.5 g; F1 cars brake at ~5-6 g, so faster changes are not physical


def on_track_mask(car, laps):
    """True for samples inside a timed lap of that driver (excludes garage and grid time)."""
    m = np.zeros(len(car), bool)
    for drv, lp in laps.groupby("Driver"):
        t = car.SessionTime.to_numpy()
        is_drv = (car.Driver == drv).to_numpy()
        for a, b in zip(lp.LapStartTime, lp.LapEndTime):
            m |= is_drv & (t >= a) & (t <= b)
    return m


def check_driver(d):
    t = d.SessionTime.to_numpy()
    dt = np.diff(t)
    track = d.on_track.to_numpy()
    both = track[1:] & track[:-1]  # interval fully on track
    gaps = np.flatnonzero((dt > GAP_S) & both)
    stuck = [(t[a], t[b - 1], b - a, int(d.RPM.iloc[a])) for a, b in equal_runs(d.RPM.to_numpy())
             if b - a >= STUCK_SAMPLES and d.Speed.iloc[a:b].min() > 50 and track[a:b].all()]
    dv = np.diff(d.Speed.to_numpy()) / np.where(dt > 0, dt, np.nan)
    fast = np.flatnonzero((np.abs(dv) > MAX_DECEL_KMH_S) & both)
    on = d[track]
    return dict(
        samples=len(d), on_track_samples=int(track.sum()),
        dt_on_track_s=dict(zip(["p1", "median", "p95", "p99", "max"],
                               np.round(np.percentile(dt[both], [1, 50, 95, 99, 100]), 3).tolist())),
        rate_hz_on_track=round(1 / float(np.median(dt[both])), 2),
        duplicate_or_backwards_timestamps=int((dt <= 0).sum()),
        gaps_over_1s_on_track=[dict(t=round(float(t[i]), 2), gap_s=round(float(dt[i]), 3)) for i in gaps],
        throttle_above_100=int((on.Throttle > 100).sum()),
        throttle_values_above_100=sorted(on.Throttle[on.Throttle > 100].unique().tolist()),
        brake_values=sorted(on.Brake.unique().tolist()),
        drs_codes=sorted(on.DRS.unique().tolist()),
        rpm_above_15000=int((on.RPM > 15000).sum()),
        gear_outside_0_8=int((~on.nGear.between(0, 8)).sum()),
        stuck_rpm_runs=[dict(t_start=round(a, 2), t_end=round(b, 2), samples=n, rpm=r) for a, b, n, r in stuck],
        speed_change_over_300kmh_per_s=[dict(t=round(float(t[i + 1]), 2), kmh_per_s=round(float(dv[i]), 1),
                                             dt_s=round(float(dt[i]), 3)) for i in fast],
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", default="data/real")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()
    real, out = Path(args.real), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if not (real / "car_data.csv").exists():
        msg = f"no {real}/car_data.csv (run `make real` with network access); real-data checks skipped"
        print(msg)
        (out / "real_results.json").write_text(json.dumps(dict(skipped=msg), indent=2) + "\n")
        return

    car = pd.read_csv(real / "car_data.csv")
    laps = pd.read_csv(real / "laps.csv")
    car["on_track"] = on_track_mask(car, laps)
    # Every driver's samples share the exact same timestamps: the feed sends all cars in one packet.
    same_clock = car.groupby("Driver").SessionTime.apply(lambda s: tuple(s.round(3))).nunique() == 1
    res = dict(session=(real / "session.txt").read_text().strip(), drivers={},
               all_drivers_share_timestamps=bool(same_clock))
    for drv, d in car.groupby("Driver"):
        res["drivers"][drv] = check_driver(d.reset_index(drop=True))
    # Because all cars share one clock, a gap is a gap in the feed itself (common mode), not in a car.
    res["gap_instants_any_driver"] = len({g["t"] for r in res["drivers"].values() for g in r["gaps_over_1s_on_track"]})
    (out / "real_results.json").write_text(json.dumps(res, indent=2) + "\n")

    print(res["session"])
    print(f"distinct gap instants (>1 s, on track): {res['gap_instants_any_driver']}")
    print(f"all drivers share timestamps: {same_clock}")
    for drv, r in res["drivers"].items():
        q = r["dt_on_track_s"]
        print(f"{drv}: {r['on_track_samples']}/{r['samples']} samples on track, ~{r['rate_hz_on_track']} Hz, "
              f"dt median {q['median']} s p95 {q['p95']} s max {q['max']} s, "
              f"gaps>1s {len(r['gaps_over_1s_on_track'])}, throttle>100 {r['throttle_above_100']} "
              f"(values {r['throttle_values_above_100']}), brake values {r['brake_values']}, "
              f"stuck rpm runs {len(r['stuck_rpm_runs'])}, speed jumps>300km/h/s {len(r['speed_change_over_300kmh_per_s'])}")


if __name__ == "__main__":
    main()
