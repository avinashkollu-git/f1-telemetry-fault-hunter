"""Download one real F1 session and export car telemetry and laps to CSV (data/real/).

Source 1, FastF1 (cache in data/cache). Since 2026 the F1 live-timing archive it reads from
answers 401 without a signed-in F1 account, so FastF1 needs F1_USERNAME / F1_PASSWORD set.
Source 2, used when FastF1 returns no car data: the public OpenF1 API (api.openf1.org), which
serves the same live-timing car channels. Raw JSON is cached in data/cache/openf1.

Either way the car telemetry is the public broadcast feed, sampled at roughly 4 Hz, far below
what a team logs on the car.
"""
import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

CACHE = Path("data/cache")
# 2024 British GP qualifying. OpenF1 session key and the top-3 qualifiers (Russell, Hamilton, Norris).
OPENF1_SESSION, OPENF1_DRIVERS = 9554, [63, 44, 4]


def try_fastf1(year, event, session):
    import fastf1
    fastf1.Cache.configure(cache_dir=str(CACHE))
    s = fastf1.get_session(year, event, session)
    s.load(laps=True, telemetry=True, weather=False, messages=False)
    if not s.car_data:
        return None
    car = []
    for drv, df in s.car_data.items():
        d = df[["SessionTime", "RPM", "Speed", "nGear", "Throttle", "Brake", "DRS"]].copy()
        d.insert(0, "Driver", s.get_driver(drv)["Abbreviation"])
        d["SessionTime"] = d.SessionTime.dt.total_seconds()
        car.append(d)
    laps = s.laps[["Driver", "LapNumber", "LapStartTime", "Time"]].rename(columns={"Time": "LapEndTime"})
    for c in ["LapStartTime", "LapEndTime"]:
        laps[c] = laps[c].dt.total_seconds()
    return pd.concat(car, ignore_index=True), laps, f"FastF1: {year} {event} {session}"


def openf1(endpoint, **params):
    """GET api.openf1.org/v1/<endpoint>, cached on disk so reruns don't hit the network."""
    q = urllib.parse.urlencode(params)
    path = CACHE / "openf1" / f"{endpoint}_{q.replace('&', '_').replace('=', '-')}.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"https://api.openf1.org/v1/{endpoint}?{q}", timeout=60) as r:
            path.write_bytes(r.read())
    return pd.DataFrame(json.loads(path.read_text()))


def from_openf1():
    sess = openf1("sessions", session_key=OPENF1_SESSION).iloc[0]
    t0 = pd.Timestamp(sess.date_start)
    names = openf1("drivers", session_key=OPENF1_SESSION).set_index("driver_number").name_acronym
    car, laps = [], []
    for n in OPENF1_DRIVERS:
        d = openf1("car_data", session_key=OPENF1_SESSION, driver_number=n)
        car.append(pd.DataFrame({
            "Driver": names[n],
            "SessionTime": (pd.to_datetime(d.date, format="ISO8601") - t0).dt.total_seconds(),
            "RPM": d.rpm, "Speed": d.speed, "nGear": d.n_gear, "Throttle": d.throttle,
            "Brake": d.brake, "DRS": d.drs}))
        lp = openf1("laps", session_key=OPENF1_SESSION, driver_number=n).dropna(subset=["date_start", "lap_duration"])
        start = (pd.to_datetime(lp.date_start, format="ISO8601") - t0).dt.total_seconds()
        laps.append(pd.DataFrame({"Driver": names[n], "LapNumber": lp.lap_number,
                                  "LapStartTime": start, "LapEndTime": start + lp.lap_duration}))
    title = f"OpenF1: {sess.year} {sess.location} {sess.session_name} (session {OPENF1_SESSION})"
    return pd.concat(car, ignore_index=True), pd.concat(laps, ignore_index=True), title


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--event", default="British Grand Prix")
    ap.add_argument("--session", default="Q")
    ap.add_argument("--out", default="data/real")
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    got = try_fastf1(args.year, args.event, args.session)
    if got is None:
        print("FastF1 returned no car data (F1 live-timing archive needs an F1 account); using OpenF1")
        got = from_openf1()
    car, laps, title = got
    car.to_csv(out / "car_data.csv", index=False)
    laps.to_csv(out / "laps.csv", index=False)
    (out / "session.txt").write_text(title + "\n")
    print(f"{title}: {len(car)} car samples, {car.Driver.nunique()} drivers, {len(laps)} timed laps")


if __name__ == "__main__":
    main()
