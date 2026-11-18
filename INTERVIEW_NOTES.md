# Interview notes

Plain-English notes for explaining this project, and the ideas behind it, out loud.

## 1. CAN in five minutes

**What it is.** CAN (Controller Area Network) is a two-wire bus (CAN_H and CAN_L) that lets a car's
ECUs (engine, gearbox, brakes, battery...) share short messages. Classic CAN runs up to 1 Mbit/s and
carries up to 8 data bytes per frame. There are no addresses. Each frame has an **identifier** that
says *what* the data is (e.g. 0x0C0 = engine data), and every node decides whether it cares.

**Frame (CAN 2.0A, standard 11-bit ID):**
`SOF | ID (11) | RTR | IDE | r0 | DLC (4) | DATA (0-8 bytes) | CRC (15) | CRC delim | ACK | EOF (7)`
The DLC (data length code) says how many data bytes follow.

**Arbitration (who gets to talk).** Any node can start sending when the bus is idle. Bits are
either *dominant* (0) or *recessive* (1), and a dominant bit always wins on the wire (wired-AND).
Each sender reads the bus while it writes its ID. If it sent a 1 but reads a 0, someone with a lower
ID is also sending, so it stops and tries again later. The **lowest ID wins**, and the winner's
frame is not damaged ("non-destructive" arbitration). That is why safety-critical messages get low
IDs. In this project ENGINE is 0x0C0 and the radio heartbeat is 0x3F0, the lowest priority.

**Bit stuffing.** After 5 identical bits in a row the sender inserts one opposite bit, and the
receiver removes it. This guarantees edges that keep every node's clock in sync. Six identical bits
in a row is therefore illegal (a *stuff error*), which error frames use on purpose. Stuffing makes
frame length depend on the data. My generator does not model it (README, Limitations).

**CRC-15.** The sender divides the bits from SOF to the end of the data (before stuffing) by the
polynomial 0x4599 and sends the 15-bit remainder. The receiver does the same division. If the
remainder differs, the frame was corrupted. CAN's CRC catches all errors of up to 5 bits anywhere
in the frame and any burst of up to 15 bits. `decoder/can.cpp: crc15()` is the textbook shift
register: shift in one bit at a time, and XOR with the polynomial when the bit falling off the top
differs from the input bit. A neat check from the tests: run the CRC over the frame bits *plus*
their CRC, and you get 0.

**Error frames.** Any node that sees a problem destroys the frame immediately by sending 6 dominant
bits (an *error flag*). That breaks the stuffing rule, so everyone sees it, and the sender
retransmits automatically. The five error types are bit error, stuff error, CRC error, form error
and ACK error. My generator writes a corrupted frame, then `ERR`, then the retransmission.

**Fault confinement and bus-off.** Each node keeps a transmit error counter (TEC) and a receive
error counter (REC). An error adds 8 to the TEC and a success takes 1 off.
- Counters ≤ 127: **error-active**, normal operation.
- Over 127: **error-passive**. The node may still send, but only flags errors quietly.
- TEC over 255: **bus-off**. The node disconnects itself so a broken node can't jam the whole bus.
  It may come back only after 128 × 11 recessive bits, and many ECUs wait longer in software.

In the log, bus-off looks like a burst of error frames and then one ID going silent, which is
exactly how `bus_off()` finds it.

## 2. How to debug an intermittent fault

The heartbeat in this project fails like a real intermittent fault. It is fine for 4 minutes, then
lost for 2 s on one straight, then fine again.

1. **Reproduce / make it observable.** You can't fix what you can't see. Log everything with
   timestamps, especially the context (vehicle state), not just the failure. Add a heartbeat or
   counter so loss is *measurable*: the counter jump in `missing_heartbeat()` tells you exactly how
   many were lost.
2. **Characterise.** When, how long, how often? Group occurrences by signature (`signatures()`).
   Fifty identical events are one bug, not fifty.
3. **Correlate.** Line up each occurrence with the vehicle state and look for what is different when
   it fails. `rootcause.py` does this with a rule search. Even a table of mean values (fault vs
   normal) already points at gear, rpm and coolant temperature.
4. **Challenge the correlation.** Look at the runner-up hypotheses. In this project, battery
   temperature explained the fault as well as coolant temperature did, only because both rose over
   the stint. Correlation tells you where to look, not why.
5. **Isolate with an experiment.** Change one variable at a time: heat the coolant on a test bench
   at low rpm, then spin to high rpm with cold coolant. If it only fails with both, you have the
   AND-condition. Swap parts (harness, connector, radio unit) to localise it physically.
6. **Fix the root cause**, not the symptom: e.g. a connector that loses contact through vibration
   at high rpm once thermal expansion loosens it. Retrying the heartbeat would only hide it.
7. **Prevent.** Add a test that reproduces the condition, keep the detector in the monitoring
   pipeline, and write it up so the next person recognises the signature.

## 3. Test and validation thinking

- **Ground truth first.** Every number in the README is measured against something known: injected
  faults, the published CRC check value, a frame built in Python and decoded in C++. A test without
  an independent reference only checks that the code agrees with itself.
- **Precision and recall.** Recall asks "did I catch the real faults?". Precision asks "are my
  alarms real?". A detector that fires constantly has perfect recall and is useless in a garage.
- **Boundary cases.** The interesting failures sit at edges: the 100 °C threshold vs 1 °C sensor
  resolution, the last heartbeat before the log ends, a 2-frame gap that is both a drop and a short
  loss.
- **Fault injection.** You only know a detector works if you have seen it catch a fault you
  planted, which is the whole point of the generator.
- **Reproducibility.** Everything is seeded. "It failed on seed 2" is a bug report someone else can
  rerun.
- **Cascades.** One root cause often creates several symptoms (a frozen sensor's release looks like
  a rate spike). Triage keeps the first cause and links the rest to it.
- **Don't overfit.** The detectors were tuned knowing the fault model. Running other seeds exposed
  three issues that seed 42 hid.

## 4. Link to chip verification

My background is RTL design verification. The ideas map almost one to one:

| chip verification | this project |
|---|---|
| constrained-random stimulus with a seed | seeded generator with random fault injection |
| scoreboard comparing DUT output to a reference model | scoring detections against `ground_truth.csv` |
| SVA assertions (properties that must always hold) | detectors: "heartbeat every 50 ms", "rpm can't change faster than X" |
| regression triage: bucket failures by signature | `signatures()`: group events by type + ID + signal |
| coverage: did we exercise the corner cases? | does the data contain hot + cold, high + low rpm, so the rule search can separate causes? |
| formal (SymbiYosys, k-induction) proves a property for all inputs | not done here; simulation/data only ever shows the cases you ran |
| debugging a waveform: find the first cycle where it goes wrong | finding the first heartbeat slot that goes missing and the state at that instant |

In open-source RISC-V core verification (OpenHW Group) the work is the same loop: reproduce a
failing test, find the first divergence, find the root cause, fix it, add a check so it can't
silently come back. A CAN bus is a protocol with a spec, and a vehicle network is a system where
many such protocols meet.

## 5. Likely questions, with answers

**1. Walk me through the project in one minute.**
I generate a realistic CAN log of a car lapping, with faults injected at known times. One of them is
hidden: the radio heartbeat drops only in 8th gear above 11 000 rpm with hot coolant. A C++ decoder
checks the CRC of every frame, decodes the signals and flags timing problems. Python detectors find
the faults and group them. A rule search then tries to rediscover the hidden trigger and gets it,
within sensor resolution: precision 0.98, recall 0.89. I also ran the same data-quality checks on
real public F1 telemetry.

**2. Why does CAN use the lowest ID as highest priority?**
Because 0 is dominant. During arbitration a node sending a 1 that reads back a 0 knows someone more
important is talking and backs off. The lower ID has its first 0 earlier, so it wins, and its frame
goes through undamaged.

**3. How does your decoder check the CRC?**
It rebuilds the exact bit sequence the CRC covers (SOF, ID, RTR, IDE, r0, DLC, data) and runs the
15-bit shift register with polynomial 0x4599. If that doesn't equal the transmitted CRC, it flags
CRC_ERROR and drops the frame, the way a real controller would. I tested it against the published
check value for CRC-15/CAN, the zero-residue property, every single-bit flip, and the Python
encoder.

**4. What is bus-off and how do you detect it in a log?**
When a node's transmit error counter passes 255 it disconnects itself to protect the bus. In a log
you see a burst of error frames and then one ID going silent for a while. My detector looks for 16
or more error frames close together followed by a long gap on one ID.

**5. Your detectors score 100 %. Why should I believe that?**
You shouldn't read much into it. I wrote them knowing the fault model, so it mainly shows the
pipeline is consistent. The more honest evidence is the other seeds. On seed 1 one heartbeat loss
was missed (a single lost heartbeat right at the end of the log), and running seed 2 before I fixed
two bugs showed a false CRC miss and a false jitter alarm.

**6. Tell me about a bug you found.**
On seed 2 the CRC check missed one corrupted frame, and separately a battery frame was flagged as
arriving 0.15 ms after the previous one. Both were the same frame. My generator flipped random bits
*with replacement*, so it once flipped the same bit twice, which left the frame unchanged and valid.
The retransmission then looked like a timing fault. One root cause, two symptoms. The fix was to
choose distinct bits.

**7. How did you find the hidden trigger, and how good is it?**
I sample the vehicle state every 50 ms (one point per heartbeat slot), label each point as
heartbeat lost or not, and run a small beam search over AND-rules of thresholds to maximise F1. It
found `gear == 8 and rpm >= 11079 and water_temp >= 100` against the injected
`gear == 8 and rpm >= 11000 and water_temp >= 100`. Against the true trigger that is precision 0.978
and recall 0.892. Most misses are at 100 °C, because coolant temperature is only sent with 1 °C
resolution.

**8. What is the difference between correlation and root cause here?**
When I let the search also use battery temperature, it picked `batt_temp >= 36` over coolant
temperature, because in that run the hot laps happened late in the stint when the battery was also
warm. The data alone can't separate them. You need an experiment that breaks the link, like heating
coolant with a cold battery.

**9. How would you debug an intermittent radio failure on a real car?**
Make it measurable first (heartbeat counters, timestamped logs including vehicle state). Collect
occurrences and group them, correlate them with state, and treat the results as hypotheses. Then
test on a bench or in a controlled run, changing one factor at a time. Swap the harness, connector
and unit to localise it physically. Fix the cause, and keep a detector and a test so it can't come
back unnoticed.

**10. What's in the real F1 data you analysed, and what did you find?**
The public feed (via OpenF1, because FastF1 now needs an F1 account for the archive) for three cars
in 2024 Silverstone qualifying runs at about 4 Hz. It has 17 gaps over 1 s while cars were on a
timed lap. All three cars share identical timestamps, so those gaps are in the feed, not in the
cars. Throttle and brake contain the value 104, outside 0-100, which I report without guessing what
it means. There were no frozen rpm channels and no physically impossible speed changes. None of that
says anything about the cars themselves.

**11. Why C++ for the decoder and Python for the analysis?**
Decoding is a tight loop over every frame that could run on a logger or in a test rig, so C++ fits.
It does about 150-350k frames/s single-threaded here, including writing CSV. Analysis is
exploratory and needs dataframes and plots, so Python fits. The interface between them is a plain
CSV, which is easy to test and to swap.

**12. What would you do next?**
Bit-level timing with bit stuffing so frame duration and bus load are real. Real error counters
instead of a scripted bus-off. 29-bit IDs and CAN FD. Bit-level DBC parsing. Replaying a real lap's
speed trace into the generator. And trying the root-cause search on OR-conditions and more
confounders, because that is where real intermittent faults get hard.
