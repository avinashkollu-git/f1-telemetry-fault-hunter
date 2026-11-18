"""Run every detector on the decoded synthetic log, group faults by signature, search for the
heartbeat-loss trigger, and score everything against the injected ground truth.

Writes results/events.csv, results/signatures.csv, results/synth_results.json.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from fh import detectors, rootcause
from fh.can import load_signals

# Detector event type -> injected fault type it should find.
TRUTH_TYPE = {"CRC_CLUSTER": "EMI_BURST"}
MATCH_TOL = 0.25  # s: a detection counts if it overlaps the injected window +- this much
STATE = ["rpm", "gear", "speed", "water_temp", "lap_distance", "throttle", "brake_front"]
TRENDING = ["soc", "batt_temp"]


def score(events, truth):
    """Event-level precision/recall per fault type. A detected event matches an injected one of the
    same type (and same CAN ID, when the injected fault has one) if their time windows overlap."""
    out = {}
    for ttype in sorted(set(truth.type) - {"BITFLIP"}):
        det = events[events.type.map(lambda x: TRUTH_TYPE.get(x, x)) == ttype]
        tru = truth[truth.type == ttype]

        def hits(b_rows, row):
            same_id = (b_rows.can_id == row.can_id) | (b_rows.can_id == "") | (row.can_id == "")
            overlap = (b_rows.t_start <= row.t_end + MATCH_TOL) & (b_rows.t_end >= row.t_start - MATCH_TOL)
            return bool((same_id & overlap).any())

        tp_det = sum(hits(tru, r) for r in det.itertuples())
        tp_tru = sum(hits(det, r) for r in tru.itertuples())
        out[ttype] = dict(injected=len(tru), detected=len(det), matched=tp_tru,
                          precision=round(tp_det / len(det), 3) if len(det) else None,
                          recall=round(tp_tru / len(tru), 3) if len(tru) else None)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synth", default="data/synth")
    ap.add_argument("--decoded", default="results/decoded")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()
    synth, dec_dir, out = Path(args.synth), Path(args.decoded), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # can_id as text: "200" would otherwise be read as the integer 200.
    decoded = pd.read_csv(dec_dir / "decoded.csv", dtype={"can_id": str})
    anomalies = pd.read_csv(dec_dir / "anomalies.csv", dtype={"can_id": str, "detail": str})
    truth = pd.read_csv(synth / "ground_truth.csv", dtype={"can_id": str}, keep_default_na=False)
    truth_meta = json.loads((synth / "truth.json").read_text())
    stats = json.loads((dec_dir / "decode_stats.json").read_text())
    sigs = load_signals()
    hb_period = next(s["period_ms"] for s in sigs if s["signal"] == "hb_counter") / 1000
    t_end = float(decoded.t.max())

    # 1. Detect and group.
    events = detectors.run_all(decoded, anomalies, sigs, hb_period, t_end)
    events.to_csv(out / "events.csv", index=False)
    sig_table = detectors.signatures(events)
    sig_table.to_csv(out / "signatures.csv", index=False)

    # 2. Frame-level CRC check: every corrupted frame should be flagged, and nothing else.
    flips = truth[truth.type == "BITFLIP"]
    crc = anomalies[anomalies.type == "CRC_ERROR"]
    key = lambda df, col: set(zip(df[col].round(6), df.can_id))
    crc_hit = len(key(flips, "t_start") & key(crc, "t"))
    crc_stats = dict(injected_bitflips=len(flips), crc_errors=len(crc), matched=crc_hit,
                     false_crc_errors=len(crc) - crc_hit)

    # 3. Root cause of heartbeat loss. Grid = nominal heartbeat slots (first heartbeat + k * period).
    hb_t = decoded[decoded.signal == "hb_counter"].t
    grid = np.arange(hb_t.min(), t_end, hb_period)
    X = rootcause.state_on_grid(decoded, STATE + TRENDING, grid)
    hb_events = events[events.type == "HB_LOSS"]
    y = rootcause.label_inside(X.t.to_numpy(), hb_events)
    X.assign(hb_loss=y).to_csv(out / "hb_grid.csv", index=False)
    means = rootcause.single_feature_table(X[STATE + TRENDING], y)

    # Ground truth for scoring: the true trigger state at every real heartbeat slot.
    slots = pd.read_csv(synth / "hb_slots.csv")
    Xs = rootcause.state_on_grid(decoded, STATE + TRENDING, slots.t.to_numpy())
    truth_trig = slots.set_index("t").loc[Xs.t, "trigger"].to_numpy().astype(bool)
    p_lab, r_lab, _ = rootcause.prf(rootcause.label_inside(Xs.t.to_numpy(), hb_events), truth_trig)

    def search(features):
        terms, history, alternatives = rootcause.find_rule(X[features], y)
        p, r, f1 = rootcause.prf(rootcause.predict(Xs[features], terms), truth_trig)
        return dict(features=features, found_rule=rootcause.rule_text(terms), search_steps=history,
                    alternatives=alternatives,
                    rule_vs_true_trigger=dict(precision=round(p, 3), recall=round(r, 3), f1=round(f1, 3)))

    # Main search: the vehicle-state signals. Second search adds signals that just trend over the
    # stint (battery temperature, SoC) to show how a time proxy can masquerade as a cause.
    main_search, confound_search = search(STATE), search(STATE + TRENDING)

    res = dict(
        decode=stats,
        generator=truth_meta,
        detection=score(events, truth),
        crc=crc_stats,
        signatures=sig_table.to_dict("records"),
        root_cause=dict(
            injected_rule=truth_meta["trigger_rule"],
            grid_points=len(X), fault_points=int(y.sum()),
            hb_loss_label_vs_true_trigger=dict(precision=round(p_lab, 3), recall=round(r_lab, 3)),
            feature_means=means.to_dict("index"),
            state_features=main_search,
            with_trending_signals=confound_search,
        ),
    )
    (out / "synth_results.json").write_text(json.dumps(res, indent=2, default=float) + "\n")

    # Console summary.
    print(f"frames {stats['frames']}, decode {stats['frames_per_second']} frames/s")
    print(f"CRC: {crc_hit}/{len(flips)} corrupted frames flagged, {crc_stats['false_crc_errors']} false")
    print(f"\n{'fault':<14}{'inj':>5}{'det':>5}{'prec':>7}{'recall':>8}")
    for k, v in res["detection"].items():
        print(f"{k:<14}{v['injected']:>5}{v['detected']:>5}{v['precision'] or 0:>7.2f}{v['recall'] or 0:>8.2f}")
    print("\nsignatures:\n" + sig_table.to_string(index=False))
    print("\nheartbeat loss, feature means (fault vs normal):\n" + means.to_string())
    print(f"\ninjected rule: {truth_meta['trigger_rule']}")
    for name, srch in [("state features", main_search), ("+ trending signals", confound_search)]:
        print(f"\nrule search over {name}:")
        for h in srch["search_steps"]:
            print(f"  + {h['rule']:<62} P={h['precision']:.3f} R={h['recall']:.3f} F1={h['f1']:.3f}")
        for alt in srch["alternatives"]:
            print(f"    alternative: {alt['rule']:<52} F1={alt['f1']:.3f}")
        rv = srch["rule_vs_true_trigger"]
        print(f"  found: {srch['found_rule']}  ->  vs true trigger P={rv['precision']:.3f} R={rv['recall']:.3f}")


if __name__ == "__main__":
    main()
