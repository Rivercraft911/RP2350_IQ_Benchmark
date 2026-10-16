"""Reproduce the opt-in N5 alpha0.20 pulse's gridded convex design.

Uses the repository Annex A dB-interpolation model and an ideal DAC hold.
An odd 35-tap symmetric FIR has 18 independent coefficients; its center
coefficient has weight one, all mirrored coefficients weight two. Linear
mask inequalities and an ideal-RRC main-sample equality constrain an LP
outer approximation of the RMS receiver-sidelobe norm. Frozen integer taps
in pulse_profiles.py, rather than a platform-dependent solver rerun, define
firmware truth. test_iq5hist.py independently audits their rounded table.
No device access. Requires SciPy only for this optional design operation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform

import numpy as np
import scipy
from scipy.optimize import linprog

from pulse_profiles import N5_COEFFICIENTS

ROOT = Path(__file__).resolve().parents[1]
L, N, TAPS = 7, 5, 35


def cosine_matrix(frequencies):
    ages = np.arange((TAPS + 1) // 2) - (TAPS - 1) / 2
    weights = np.where(ages == 0, 1, 2)
    return weights * np.cos(np.pi * np.asarray(frequencies)[:, None] * ages / N)


def receiver_matrix(sidelobes=80, quadrature=768):
    nodes, weights = np.polynomial.legendre.leggauss(quadrature)
    frequencies, integration_weights = [], []
    for lo, hi in ((0, .8), (.8, 1.2)):
        frequencies.append(lo + (nodes + 1) * (hi - lo) / 2)
        integration_weights.append(weights * (hi - lo) / 2)
    f = np.concatenate(frequencies)
    weight = np.concatenate(integration_weights)
    receiver = np.ones_like(f)
    transition = f > .8
    receiver[transition] = np.sqrt(.5 * (1 + np.cos(np.pi * (f[transition] - .8) / .4)))
    receiver *= np.sinc(f / (2 * N))
    return np.einsum("kf,fj->kj", np.cos(np.pi * np.arange(sidelobes + 1)[:, None] * f) *
                     weight * receiver, cosine_matrix(f), optimize=False)


def design(margin=.1, far_extra=2., max_iterations=200):
    masks = json.loads((ROOT / "results/pulse-design/2026-10-15/selected.json").read_text())["masks"]
    upper_knots = np.asarray(masks["upper_knots_fn_db"])
    lower_knots = np.asarray(masks["lower_knots_fn_db"])
    f = np.unique(np.concatenate([np.linspace(0, 6, 6001),
        upper_knots[upper_knots[:, 0] <= 6, 0], lower_knots[:, 0]]))
    cosine = cosine_matrix(f) * np.sinc(f / (2 * N))[:, None]
    dc = cosine_matrix(np.array([0]))[0]
    upper_db = np.interp(f, upper_knots[:, 0], upper_knots[:, 1]) - margin
    upper_db[f >= 1.7] -= far_extra
    upper = 10 ** (upper_db / 20)
    low = f <= 1.11
    lower = 10 ** ((np.interp(f[low], lower_knots[:, 0], lower_knots[:, 1]) + margin) / 20)
    a = np.concatenate([cosine - upper[:, None] * dc, -cosine - upper[:, None] * dc,
                        lower[:, None] * dc - cosine[low], -dc[None, :]])
    b = np.zeros(len(a)); b[-1] = -1e-6
    receiver = receiver_matrix()
    sidelobes = np.sqrt(2) * receiver[1:]
    cuts = [row.copy() for row in sidelobes] + [-row.copy() for row in sidelobes]
    dimension = (TAPS + 1) // 2
    record = dict(N=N, L=L, alpha=.20, ideal_hold=True, symmetry="odd35_single_center",
                  requested_mask_margin_db=margin, requested_far_extra_db=far_extra,
                  upper_interval_fn=[0, 6], lower_interval_fn=[0, 1.11],
                  frequency_grid_uniform_points=6001, exact_mask_knots=True,
                  receiver_sidelobes_per_side=80, quadrature_nodes_per_interval=768,
                  main_receiver_sample=1, objective="RMS receiver sidelobes / main sample",
                  environment=dict(python=platform.python_version(), numpy=np.__version__, scipy=scipy.__version__))
    for iteration in range(max_iterations):
        spectral = np.column_stack([a, np.zeros(len(a))])
        cone = np.column_stack([np.asarray(cuts), -np.ones(len(cuts))])
        optimum = linprog(
            np.r_[np.zeros(dimension), 1.], A_ub=np.vstack([spectral, cone]),
            b_ub=np.r_[b, np.zeros(len(cuts))], A_eq=np.r_[receiver[0], 0][None, :],
            b_eq=[1.], bounds=[(None, None)] * dimension + [(0, None)], method="highs",
            options={"primal_feasibility_tolerance": 1e-9, "dual_feasibility_tolerance": 1e-9})
        if not optimum.success:
            return dict(record, success=False, reason=optimum.message, iterations=iteration + 1)
        x = optimum.x[:dimension]
        side = np.einsum("ij,j->i", sidelobes, x)
        norm = float(np.linalg.norm(side))
        gap = norm - optimum.x[-1]
        if gap < 2e-8:
            break
        cuts.append(np.einsum("i,ij->j", side / norm, sidelobes))
    else:
        return dict(record, success=False, reason="outer approximation did not converge", iterations=max_iterations)
    taps = np.r_[x, x[-2::-1]]
    scale = 8191 * 10 ** (-1 / 20) / abs(taps.reshape(L, N)).sum(axis=0).max()
    coefficients = np.rint(taps * scale * 256).astype(np.int64)
    return dict(record, success=True, iterations=iteration + 1, soc_bound_gap=float(gap),
                objective_isi_fraction=norm, float_taps=taps.tolist(), coefficients_q8=coefficients.tolist(),
                binary_axis_headroom_target_db=1,
                frozen_coefficients_equal=bool(np.array_equal(coefficients, N5_COEFFICIENTS)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--margin", type=float, default=.1)
    parser.add_argument("--far-extra", type=float, default=2.)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = design(args.margin, args.far_extra)
    text = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
