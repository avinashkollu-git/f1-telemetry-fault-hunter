"""Root-cause helper: which vehicle state explains when a fault happens?

Approach: sample the vehicle state on a fixed time grid, label each grid point "fault active"
or not, then greedily search for the AND-rule of simple thresholds (feature >= x, feature <= x,
feature == k) that best separates the two (by F1). A hand-rolled, depth-limited rule search,
kept small so every step can be explained in an interview.
"""
import numpy as np
import pandas as pd


def state_on_grid(decoded, features, grid):
    """Latest decoded value of each feature at each grid time (zero-order hold, like a scope)."""
    out = pd.DataFrame({"t": grid})
    for f in features:
        s = decoded[decoded.signal == f][["t", "value"]].rename(columns={"value": f})
        out = pd.merge_asof(out, s.sort_values("t"), on="t", direction="backward")
    return out.dropna().reset_index(drop=True)


def label_inside(grid, events):
    y = np.zeros(len(grid), bool)
    for a, b in zip(events.t_start, events.t_end):
        y |= (grid >= a - 1e-9) & (grid <= b + 1e-9)
    return y


def prf(pred, y):
    tp = int((pred & y).sum())
    p = tp / max(int(pred.sum()), 1)
    r = tp / max(int(y.sum()), 1)
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def _candidates(x, x_pos):
    """Thresholds to try: percentiles of the feature, plus the edges of the positive class
    (the tightest threshold that keeps every fault sample)."""
    thr = np.unique(np.concatenate([np.percentile(x, np.arange(1, 100)), [x_pos.min(), x_pos.max()]]))
    ops = [(">=", v) for v in thr] + [("<=", v) for v in thr]
    if len(np.unique(x)) <= 10:  # small integer set (gear): try equality first, so it wins ties
        ops = [("==", v) for v in np.unique(x)] + ops
    return ops


def _apply(x, op, v):
    return x >= v if op == ">=" else x <= v if op == "<=" else x == v


def find_rule(X, y, max_terms=3, beam=5, min_gain=0.01):
    """Beam search for the AND-rule with the best F1. Plain greedy (beam=1) got stuck here: its
    best first term was a speed threshold, after which battery SoC (which just trends down over
    the stint) looked like the best second term. Keeping the 5 best partial rules avoids that.
    Returns (terms, history, alternatives): terms is a list of (feature, op, threshold), history
    has precision/recall/F1 after each term of the winning rule, alternatives are the other rules
    left in the final beam. Near-ties there are competing hypotheses, not noise."""
    cols = {f: X[f].to_numpy() for f in X.columns}
    beams = [((), np.ones(len(y), bool), 0.0)]
    best = beams[0]
    for _ in range(max_terms):
        cand = []
        for terms, mask, f1_prev in beams:
            used = {t[0] for t in terms}
            for f, x in cols.items():
                if f in used or not (mask & y).any():
                    continue
                top = None  # best threshold for this feature, given the terms so far
                for op, v in _candidates(x, x[mask & y]):
                    m = mask & _apply(x, op, v)
                    f1 = prf(m, y)[2]
                    if top is None or f1 > top[2]:
                        top = (terms + ((f, op, float(v)),), m, f1)
                if top[2] - f1_prev >= min_gain:
                    cand.append(top)
        if not cand:
            break
        cand.sort(key=lambda c: -c[2])
        beams, seen = [], set()
        for c in cand:
            key = frozenset(t[0] for t in c[0])  # same features in another order = same rule
            if key not in seen:
                seen.add(key)
                beams.append(c)
            if len(beams) == beam:
                break
        if beams[0][2] > best[2]:
            best = beams[0]
    alternatives = [dict(rule=rule_text(c[0]), f1=round(c[2], 3)) for c in beams if c is not best]
    terms = list(best[0])
    history = []
    for i in range(1, len(terms) + 1):
        p, r, f1 = prf(predict(X, terms[:i]), y)
        history.append(dict(rule=rule_text(terms[:i]), precision=p, recall=r, f1=f1))
    return terms, history, alternatives


def predict(X, terms):
    m = np.ones(len(X), bool)
    for f, op, v in terms:
        m &= _apply(X[f].to_numpy(), op, v)
    return m


def rule_text(terms):
    return " and ".join(f"{f} {op} {v:g}" for f, op, v in terms)


def single_feature_table(X, y):
    """Mean of each feature when the fault is active vs not: the first thing to eyeball."""
    return pd.DataFrame({"fault_active_mean": X[y].mean(), "normal_mean": X[~y].mean()}).round(1)
