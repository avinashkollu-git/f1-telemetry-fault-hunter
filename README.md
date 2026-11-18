# F1 Telemetry Fault Hunter

A small test-and-debug toolchain for vehicle network data. It is built around one question:
**when a fault only shows up now and then, how do you find out what triggers it?**

- A seeded generator writes a realistic **CAN 2.0A bus log** (6 ECUs, 12 signals, correct
  11-bit IDs, DLC and CRC-15) of a car lapping a made-up circuit. It injects faults at known
  times, including a hidden one: the radio heartbeat drops out **only** in 8th gear above
  11 000 rpm with coolant at or above 100 °C.
- A **C++17 decoder** checks every frame's CRC-15, decodes signals from a small DBC-like table and
  flags protocol and timing anomalies.
- **Python fault-analysis tools** detect stuck sensors, out-of-range values, impossible rates of
  change, missing heartbeats, CRC error clusters, bus-off events and dropped frames. They group the
  events by signature, like failures in a chip-verification regression. Then a **root-cause rule
  search** tries to recover the hidden trigger, and is scored against the injected ground truth.
- The same data-quality checks run on **real public F1 telemetry** (2024 British GP qualifying,
  3 cars). Findings there are reported as measured.

This is a personal portfolio project. It has no connection with any F1 team, Formula 1 or the FIA,
and the synthetic car, circuit and faults are invented. It does not find faults in a real car.

## Pipeline

```
                    config/signals.csv  (DBC-like signal table, shared by both sides)
                              |
  scripts/gen_can.py  --------+-------->  data/synth/can_log.txt      "0.010003 0C0 4 4425C87C 00E2"
  (seeded lap sim +                       data/synth/ground_truth.csv  every injected fault
   fault injection)                       data/synth/hb_slots.csv      true trigger state per heartbeat slot
                                                    |
                                                    v
                                   build/can_decode   (C++17, single-threaded)
                                   - parse + validate each line
                                   - recompute CRC-15, drop bad frames
                                   - decode signals (scale/offset)
                                   - per-ID timing: GAP / JITTER
                                                    |
                         results/decoded/decoded.csv, anomalies.csv, decode_stats.json
                                                    |
                                                    v
                                   scripts/analyze.py  (fh/detectors.py, fh/rootcause.py)
                                   - 8 detectors -> events
                                   - group by signature
                                   - rule search: which vehicle state explains heartbeat loss?
                                   - score vs ground truth (precision / recall)
                                                    |
  scripts/fetch_fastf1.py  -> data/real/  ->  scripts/real_checks.py
  (FastF1, else OpenF1;                       (sample-rate jitter, gaps, out-of-range,
   cached in data/cache)                       frozen channels on the public feed)
                                                    |
                                                    v
                                   scripts/report.py -> reports/REPORT.md + plots
```

## Run it

Needs `make`, a C++17 compiler, Python 3.12 and [uv](https://docs.astral.sh/uv/) (it falls back to
`python3 -m venv` + pip without uv). Everything runs single-threaded at low priority (`nice`, and
`taskpolicy -b` on macOS), with a 60 s cool-down between the heavy steps.

```sh
make all              # venv, data, build, test, analyze, report  (~1 min 40 s here, incl. cool-downs)

make data             # synthetic log (seed 42) + real session download (cached in data/cache)
make build            # build/can_decode and build/test_can
make test             # 18 C++ checks + 5 Python tests
make analyze          # detectors, signatures, root cause, real-data checks -> results/
make report           # reports/REPORT.md + PNG plots
make seeds SEEDS="1 2"  # same analysis on other seeds
make clean            # keeps data/cache so nothing is downloaded again
make analyze COOLDOWN=0   # skip the cool-down (CI does this)
```

Real data: FastF1 is tried first. Since 2026 the F1 live-timing archive returns HTTP 401 without a
signed-in F1 account, so FastF1 only gets car data if `F1_USERNAME` / `F1_PASSWORD` are set.
Otherwise the script falls back to the public [OpenF1](https://openf1.org) API, which serves the
same live-timing car channels. See [BUGS.md](BUGS.md#1-fastf1-returned-no-car-data).

## Results (real numbers from `make all`, seed 42)

Machine: Apple M4 laptop, background priority. All numbers are from `results/` and
[reports/REPORT.md](reports/REPORT.md).

**Log:** 7 laps, 546.7 s of driving, **157 926 frames** plus 151 error frames.

**Decoder:** **164 018 frames/s** single-threaded (0.96 s for the log, including writing ~365k CSV
rows). Across runs this ranged from about 137k to 346k frames/s, because the process runs at
background priority.

**CRC-15:** 87 / 87 corrupted frames flagged, 0 false CRC errors. The CRC code is checked against the
published CRC-15/CAN check value (`"123456789"` → `0x059E`), against the receiver residue property,
and against every single-bit flip of a frame. The Python encoder and the C++ decoder must also agree.

**Detectors vs injected faults** (event level; a detection counts if it overlaps the injected
window ±0.25 s):

| fault | injected | detected | precision | recall |
|---|---|---|---|---|
| dropped frame | 151 | 151 | 1.00 | 1.00 |
| heartbeat loss (hidden trigger) | 9 | 9 | 1.00 | 1.00 |
| CRC error cluster (EMI burst) | 3 | 3 | 1.00 | 1.00 |
| bus-off (error-frame burst + ECU silent) | 2 | 2 | 1.00 | 1.00 |
| timing jitter window | 4 | 4 | 1.00 | 1.00 |
| stuck sensor | 3 | 3 | 1.00 | 1.00 |
| out-of-range value | 7 | 7 | 1.00 | 1.00 |
| rate-of-change spike | 8 | 8 | 1.00 | 1.00 |

These detectors were written knowing the fault model, so perfect scores here show the pipeline is
consistent end to end. They do not show it would generalise to an unknown fault model. Two other
seeds were not perfect (see below).

**Signatures:** 187 events collapse into 17 signatures, e.g. `DROP 0D0` ×61, `HB_LOSS 3F0` ×9,
`STUCK 0C0 rpm` ×1. That is the debug to-do list.

**Root cause of the heartbeat loss.** The heartbeat is sampled on a 50 ms grid (10 933 points,
498 with heartbeat lost). A beam search then looks for the AND-rule of thresholds that best explains
the loss:

| | rule | vs true trigger |
|---|---|---|
| injected (hidden) | `gear == 8 and rpm >= 11000 and water_temp >= 100` | |
| found, vehicle-state signals | `gear == 8 and rpm >= 11079 and water_temp >= 100` | precision **0.978**, recall **0.892** |
| found, + SoC & battery temp | `speed >= 336 and batt_temp >= 36 and rpm >= 11011` | precision 0.998, recall 0.915 |

- With the vehicle-state signals it recovers **all three conditions**. The rpm threshold comes out
  79 rpm high and recall is 0.89. Most of the misses sit at the 100 °C boundary: coolant
  temperature is sent with 1 °C resolution, so the data can't tell 99.6 °C from 100.4 °C
  ([plot](reports/hb_root_cause.png)).
- If you add signals that just trend up or down over the stint (battery temperature, SoC), a
  time proxy (`batt_temp >= 36`) scores slightly *better* than the real cause, because in this run
  the hot laps happen to come late. Correlation found the right *place*, not the right *reason*.
  The runner-up list shows the competing hypotheses, and an engineer would separate them with a
  test (heat the coolant with a cold battery). This is the most useful lesson in the project.

**Other seeds** (`make seeds`):

| seed | detection misses | rule found (state signals) | P / R vs true trigger |
|---|---|---|---|
| 1 | 1 heartbeat loss missed: a single lost heartbeat 18 ms before the log ends, too short to tell from a drop | `gear == 8 and rpm >= 11012 and water_temp >= 101` | 0.986 / 0.938 |
| 2 | none | `rpm >= 10980 and speed >= 331.5 and water_temp >= 101` | 0.910 / 0.851 |

Seed 2 found `speed >= 331.5` instead of `gear == 8`. In this data they are equivalent: above
295 km/h the car is always in 8th.

**Real telemetry** (OpenF1, 2024 Silverstone qualifying, RUS / HAM / NOR, on-track samples only):

| check | finding |
|---|---|
| sample rate | ~4.17 Hz; interval median 0.24 s, p95 0.40 s, max 1.36 s |
| gaps > 1 s | 15-16 per car, 17 distinct instants |
| common mode | all three cars carry **identical timestamps**, so every gap is in the feed, not in a car |
| out of range | `Throttle` = 104 in 939-1 784 samples per car; `Brake` takes values {0, 100, 104} |
| stuck rpm (≥ 12 samples while moving) | none |
| speed change > 300 km/h/s | none |

The 104 values are outside the 0-100 range the channel suggests. I report them as out-of-range and
don't guess what they mean. None of this is a car fault. It describes the public feed: low rate,
already processed, no raw bus.

## Layout

```
config/signals.csv      message/signal table (ID, period, DLC, byte position, scale, offset, range, max rate)
fh/can.py               CRC-15, frame bits, encode/pack (Python side)
scripts/gen_can.py      lap simulation + CAN log + fault injection + ground truth
decoder/                can.hpp/can.cpp (CRC, parser, table, timing), main.cpp (CLI)
fh/detectors.py         8 detectors, cascade de-duplication, signature grouping
fh/rootcause.py         state-on-grid, beam rule search, precision/recall
scripts/analyze.py      runs it all on synthetic data and scores vs ground truth
scripts/fetch_fastf1.py real session download (FastF1, OpenF1 fallback)
scripts/real_checks.py  data-quality checks on the real feed
scripts/report.py       reports/REPORT.md + plots
tests/                  test_can.cpp (tiny C++ runner), test_python.py (plain asserts + C++/Python cross-check)
```

## Limitations

- **Synthetic bus.** The log contains frame contents, not bits on a wire. There is no bit
  stuffing, no arbitration or bus load model, and no ACK. Frame timestamps are scheduled, not
  computed from frame length. The error-frame and bus-off behaviour is a scripted pattern, not a
  simulation of the transmit/receive error counters.
- **Decoder scope.** Standard 11-bit IDs only (no 29-bit extended or CAN FD). Signals must be
  byte-aligned and little-endian, so this is a tiny subset of DBC.
- **Seeing CRC errors at all.** A normal CAN controller drops a frame with a bad CRC and never shows
  it to software. A log that contains corrupted frames needs a bus analyser or a controller in a
  special mode. The generator assumes that kind of capture.
- **Jitter.** The check only sees changes in delay between consecutive frames. A constant extra
  latency is invisible without a reference clock (see BUGS.md #3).
- **Detector tuning.** The thresholds (stuck run of 40 samples, 2.5-period heartbeat timeout, etc.)
  were set with the injected fault model in mind. A single lost heartbeat cannot be told apart from
  a dropped frame.
- **Root cause.** The rule search finds correlation in observational data and nothing more.
  Measurement resolution limits how exactly it can place a threshold, and confounders that trend
  over time can win (shown above). It handles AND-rules of up to 3 thresholds, not OR-ed causes.
- **Real data.** One session, three cars, public ~4 Hz feed. It can only show data-quality issues
  in the feed. The FastF1 path needs an F1 account; without one the data comes from OpenF1.
- **Lap model.** Invented circuit, simple point-mass physics, made-up gear and rpm map. It only
  exists to give the signals realistic, correlated behaviour.
