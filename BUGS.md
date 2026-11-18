# Bugs hit while building this

These are real problems from this build, each with what I saw, the root cause and the fix. Each
one is a small example of the "reproduce, isolate, fix, prevent" loop the project is about.

## 1. FastF1 returned no car data

- **Seen:** the first real-data fetch crashed with `ValueError: No objects to concatenate`. FastF1
  logged `Failed to authenticate with Formula 1 account ... Car telemetry data is unavailable!`.
- **Isolated:** `curl` on `https://livetiming.formula1.com/static/2024/Index.json` returns
  **HTTP 401**. The F1 live-timing archive that FastF1 reads now needs a signed-in F1 account
  (FastF1 3.9.2 has an `f1account` module for this). Without credentials, `session.car_data` is
  just empty. No exception is raised.
- **Fix:** if FastF1 returns no car data, `fetch_fastf1.py` falls back to the public OpenF1 API,
  which serves the same car channels without login. Responses are cached on disk.
- **Prevent:** check for empty data explicitly. A library that "continues without authentication"
  fails silently, and the crash showed up three lines later.

## 2. "Corrupted" frames that weren't corrupted (bit flips cancelling)

- **Seen:** on seed 2, the CRC check flagged 71 of 72 injected bit-flip frames. In the same run a
  1A0 battery frame got a JITTER flag with `dt 0.150 ms`, and the 1A0 message only runs at 100 ms.
- **Isolated:** both pointed at the same frame, 473.512 s, inside an EMI burst. The "corrupted"
  copy was byte-for-byte identical to the good frame, so its CRC was valid. The decoder then
  accepted it, and the retransmission 0.15 ms later looked like a frame arriving far too early.
- **Root cause:** the generator flipped 1-3 *random* bits per EMI-hit frame with replacement.
  Flipping the same bit twice puts it back.
- **Fix:** pick distinct bits (`rng.choice(..., replace=False)`).
- **Prevent:** the scorer compares injected bit flips to CRC errors frame by frame, which is how this
  surfaced. One root cause explained two different symptoms, a missed CRC and a false jitter.

## 3. Timing jitter that the jitter check couldn't see

- **Seen:** the first jitter model delayed every ENGINE frame in a 1 s window by 2.5-4.5 ms.
  The decoder flagged only 8 frames across 4 windows.
- **Root cause:** a receiver only sees inter-arrival time, which is the *difference* between two
  consecutive delays. Two delays drawn from 2.5-4.5 ms differ by at most 2 ms, under the 2.5 ms
  (25 % of period) threshold. Only the start and end of each window stood out.
- **Fix:** model an overloaded ECU with independent 0-4.5 ms latency per frame, which gives about
  20 flags per window. The limitation is in the README: a constant added latency is invisible
  without a reference clock.

## 4. Rate-of-change false positives from other faults (cascades)

- **Seen (a):** RATE_SPIKE had 9 detections for 8 injected. The extra one was 20 ms after a stuck
  `speed` window ended. When the sensor unfroze, the value jumped from the frozen reading to the
  real one.
- **Seen (b):** on seed 2, an rpm RATE_SPIKE appeared inside a jitter window. A delayed frame
  followed by an on-time one gives a short `dt`, and an ordinary upshift rpm drop divided by that
  short `dt` crossed the limit.
- **Fix (a):** a rate spike within 100 ms after a STUCK event on the same signal counts as a
  symptom of that STUCK event, not a new fault. This is the same idea as keeping only the first
  failure of a cascade when triaging a regression.
- **Fix (b):** floor `dt` at the nominal period. Timestamps are *bus arrival* times. The sensor was
  still sampled on its schedule.

## 5. A short heartbeat loss also counted as a dropped frame

- **Seen:** DROP precision 0.99, with one detection that matched no injected drop.
- **Root cause:** a 2-heartbeat loss leaves a 150 ms gap. That is over the 125 ms heartbeat timeout
  (HB_LOSS) and also within "1-2 frames missing" (DROP), so it was reported twice.
- **Fix:** a DROP on the heartbeat ID inside an HB_LOSS event is dropped. A related limit remains:
  a *single* lost heartbeat is indistinguishable from a random drop, which is why seed 1 misses one
  HB_LOSS.

## 6. Rate-spike detail text showed the same number for every event

- **Seen:** every rpm RATE_SPIKE said `max 614855/s`.
- **Root cause:** the detail used `rate[hit - 1].max()`, the max over *all* hits of that signal,
  not the hits in that event.
- **Fix:** take the max within each merged event.

## 7. The root-cause search found the wrong rule, twice

- **Seen (a):** plain greedy search returned `speed >= 339.7 and soc <= 51`, with recall 0.66
  against the true trigger.
- **Root cause (a):** greedy is short-sighted. Its first pick (speed) made `gear` and `rpm`
  redundant, so the best second term it could find was SoC, which simply falls over the stint
  just as coolant temperature rose.
- **Fix (a):** beam search keeps the 5 best partial rules (de-duplicated by feature set). It also
  reports the runners-up, because near-ties are competing hypotheses.
- **Seen (b):** even with beam search, `batt_temp >= 34` / `soc <= 52.5` tied with or beat
  `water_temp >= 100`. In the first version coolant temperature was a linear ramp in time, so every
  signal that trends with time looked just like it.
- **Fix (b):** coolant now follows load with a first-order lag plus a per-lap offset (running in
  dirty air), so it isn't monotonic in time. It was also caught on review that rpm was an exact
  function of speed, which made `speed >= X` as good as `gear and rpm`. A ±3 % per-lap
  engine-mode factor fixes that.
- **Kept on purpose:** with SoC and battery temperature included, a time proxy still slightly wins
  on seed 42. That is a real property of observational data, so the README shows it instead of
  tuning it away.

## 8. Test bugs

- **Byte literal:** the end-to-end test built an unknown-ID frame with `b'\\x00'` (escaped twice
  when the file was written). That is 4 bytes (`\`, `x`, `0`, `0`), so the CRC was computed over
  `0x5C` and the decoder correctly reported a CRC error instead of UNKNOWN_ID. Fixed with
  `bytes(1)`. The decoder was right and the test was wrong.
- **Stuck run off by one:** the stuck-sensor test froze rpm at its current value. The next real
  sample happened to round to the same value, so the run was 61, not 60. The detector was right:
  it can't know where the freeze really ended. The test now freezes at a value outside the noise
  band.

## 9. Model tuning: loss windows too long

- **Seen:** the first circuit had a 1.9 km main straight, and heartbeat losses lasted up to 10 s.
  That is not "intermittent".
- **Fix:** move the last corner so the main straight is 1.3 km and vary top speed per lap. Losses
  now last 0.05-5 s (seeds 42, 1, 2) and only start once the coolant is hot.
