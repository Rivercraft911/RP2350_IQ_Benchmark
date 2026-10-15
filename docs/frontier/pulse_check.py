#!/usr/bin/env python3
"""Screening LP: can a symmetric FIR of span L symbols at N samples/symbol pass the
EN 302 307-1 Annex A (alpha = 0.20) template, and at what receive ISI?

Purpose: decide which filter spans are usable for full-history LUT shaping (table size
grows as M^L). This is an exploratory screen, NOT a pulse design:
  * Mask knots: EN 302 307-1 V1.4.1 Table A.1, alpha = 0.20 column
    (docs/sources/en_30230701v010401p.txt, lines ~2248-2275). Upper/lower boundaries
    are linear in dB between knots, relative to the 0 Hz response, as in the repo's
    reference/analyze.py ETSI_MASK and results/waveforms/2026-10-15/pulse-design.md.
  * Response models follow pulse-design.md: digital FIR only, FIR x ideal DAC hold
    sinc(f/fs), or both. Upper mask over f_n in [0, 6] (N4) / [0, 2] (N2); lower over
    [0, 1.11]; far region [1.7, f_max] gets an optional extra reserve.
  * Constraints are imposed on a finite frequency grid plus every knot. A grid is a
    relaxation of the continuous constraint, so INFEASIBLE here implies infeasible for
    the continuous mask; FEASIBLE here is only indicative (no between-grid certificate,
    no coefficient or LUT rounding).
  * Objective: minimise the L1 norm of ideal-RRC (alpha 0.20) receive sidelobes with the
    main sample fixed to 1 (an LP). The repo's optimiser minimised the L2 norm, so ISI
    values are comparable in magnitude but not identical.
Requires NumPy + SciPy (linprog/HiGHS). Writes pulse_check.json next to this file.
"""
import json
import pathlib
import sys

import numpy as np
from scipy.optimize import linprog

ALPHA = 0.20
UP = [(0.0, 0.25), (0.2, 0.25), (0.4, 0.25), (0.89, 0.15), (0.94, -0.5), (1.0, -2.0),
      (1.11, -8.0), (1.23, -16.0), (1.4, -24.0), (1.5, -35.0), (1.7, -40.0)]
LO = [(0.0, -0.25), (0.2, -0.4), (0.4, -0.4), (0.89, -1.1), (1.0, -4.0), (1.11, -11.0)]


def upper_db(f, far_reserve):
    if f >= 1.7:
        return -40.0 - far_reserve
    xs, ys = zip(*UP)
    return float(np.interp(f, xs, ys))


def lower_db(f):
    if f > 1.11:
        return None
    xs, ys = zip(*LO)
    return float(np.interp(f, xs, ys))


def rrc(t, a=ALPHA):
    t = np.asarray(t, dtype=float)
    out = np.empty_like(t)
    z = np.isclose(t, 0.0)
    s = np.isclose(np.abs(t), 1 / (4 * a))
    g = ~(z | s)
    tg = t[g]
    out[g] = (np.sin(np.pi * tg * (1 - a)) + 4 * a * tg * np.cos(np.pi * tg * (1 + a))) / (
        np.pi * tg * (1 - (4 * a * tg) ** 2))
    out[z] = 1 - a + 4 * a / np.pi
    out[s] = a / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * a)) +
                              (1 - 2 / np.pi) * np.cos(np.pi / (4 * a)))
    return out


def held_rrc(t, n_sps, pts=64):
    """RRC convolved with a centred rectangular hold of width 1/N symbols (unit area)."""
    tau = (np.arange(pts) + 0.5) / pts / n_sps - 0.5 / n_sps
    return np.mean(rrc(np.asarray(t)[:, None] - tau[None, :]), axis=1)


def solve(n_sps, span, model, margin_db=0.0, far_reserve_db=0.0, isi_lags=60, grid=3000):
    m_taps = n_sps * span
    k = m_taps // 2                      # free half-taps (even length, symmetric)
    off = (np.arange(k) + 0.5) / n_sps    # tap offsets from centre, symbols
    f_max = 6.0 if n_sps >= 4 else 2.0
    knots = [x for x, _ in UP] + [x for x, _ in LO] + [1.7, f_max]
    f = np.unique(np.concatenate([np.linspace(0, f_max, grid), knots]))
    # amplitude A(f_n) = 2 sum x_j cos(pi f_n (j+0.5)/N)
    basis = 2 * np.cos(np.pi * np.outer(f, (np.arange(k) + 0.5)) / n_sps)
    a0 = basis[0]                         # A(0) row
    hold = np.sinc(f / (2 * n_sps))
    models = {"digital": [np.ones_like(f)], "hold": [hold], "both": [np.ones_like(f), hold]}[model]
    rows, rhs = [], []
    for w in models:
        for i, fi in enumerate(f):
            u = 10 ** ((upper_db(fi, far_reserve_db) - (margin_db if fi < 1.7 else 0)) / 20)
            rows.append(w[i] * basis[i] - u * a0); rhs.append(0.0)
            rows.append(-w[i] * basis[i] - u * a0); rhs.append(0.0)
            lo = lower_db(fi)
            if lo is not None:
                l = 10 ** ((lo + margin_db) / 20)
                rows.append(-w[i] * basis[i] + l * a0); rhs.append(0.0)
    rows.append(-a0); rhs.append(-1e-3)   # A(0) > 0
    # receive samples with hold (physical model) at symbol lags 0..isi_lags
    lags = np.arange(isi_lags + 1)
    r = np.stack([held_rrc(lags - o, n_sps) + held_rrc(lags + o, n_sps) for o in off], axis=1)
    nv = k + isi_lags
    a_ub = np.zeros((len(rows) + 2 * isi_lags, nv))
    a_ub[:len(rows), :k] = np.array(rows)
    b_ub = np.zeros(len(rows) + 2 * isi_lags)
    b_ub[:len(rows)] = rhs
    for q in range(isi_lags):            # |y_q| <= e_q
        a_ub[len(rows) + 2 * q, :k] = r[q + 1]; a_ub[len(rows) + 2 * q, k + q] = -1
        a_ub[len(rows) + 2 * q + 1, :k] = -r[q + 1]; a_ub[len(rows) + 2 * q + 1, k + q] = -1
    a_eq = np.zeros((1, nv)); a_eq[0, :k] = r[0]
    c = np.concatenate([np.zeros(k), np.ones(isi_lags)])
    bounds = [(None, None)] * k + [(0, None)] * isi_lags
    res = linprog(c, A_ub=a_ub, b_ub=b_ub, A_eq=a_eq, b_eq=[1.0], bounds=bounds, method="highs")
    out = dict(N=n_sps, L=span, model=model, margin_db=margin_db, far_reserve_db=far_reserve_db,
               grid_points=int(len(f)), status=res.status, feasible=bool(res.status == 0))
    if res.status == 0:
        y = (r[1:] * res.x[:k][None, :]).sum(axis=1)   # avoid spurious Accelerate BLAS FP flags
        out["isi_rms_pct_hold_rx"] = float(100 * np.sqrt(2 * np.sum(y ** 2)))
    return out


def main():
    cases = []
    for n_sps in (4, 2):
        for span in (4, 5, 6, 7, 8, 9):
            for model in ("hold", "both"):
                for margin, far in ((0.0, 0.0), (0.1, 2.0)):
                    cases.append(solve(n_sps, span, model, margin, far))
    # repo cross-checks (pulse-design.md): expected outcomes for margin 0.1 dB / far 2 dB
    expect = [((4, 8, "both"), True), ((4, 7, "hold"), True), ((4, 7, "both"), False),
              ((2, 8, "hold"), True), ((2, 8, "both"), False)]
    checks = []
    for (n_sps, span, model), want in expect:
        got = next(c for c in cases if (c["N"], c["L"], c["model"], c["margin_db"], c["far_reserve_db"])
                   == (n_sps, span, model, 0.1, 2.0))["feasible"]
        checks.append(dict(N=n_sps, L=span, model=model, expected_feasible=want, got=got, ok=got == want))
    doc = dict(tool="pulse_check.py", numpy=np.__version__, scipy=__import__("scipy").__version__,
               python=sys.version.split()[0], cases=cases, repo_crosschecks=checks)
    path = pathlib.Path(__file__).with_suffix(".json")
    path.write_text(json.dumps(doc, indent=1))
    for c in cases:
        isi = f"{c['isi_rms_pct_hold_rx']:.2f}%" if c["feasible"] else "-"
        print(f"N{c['N']} L{c['L']} {c['model']:5s} m{c['margin_db']:.1f} far{c['far_reserve_db']:.0f}: "
              f"{'feasible' if c['feasible'] else 'INFEASIBLE':10s} ISI {isi}")
    print("repo cross-checks:", checks)


if __name__ == "__main__":
    main()
