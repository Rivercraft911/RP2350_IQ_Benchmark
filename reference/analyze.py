"""Filter-design and correctness analysis for the LUT pulse shaper.

Run from the repo root:  python3 reference/analyze.py
Writes results/reference/filter_sweep.json and results/plots/filter_sweep (PNG/SVG/PDF).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import iqlut as m  # noqa: E402
from plotstyle import BLUE, RED, WIDTH, dots, plt, save_figure  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "reference"
PLOTS = ROOT / "results" / "plots"

ALPHA = 0.20          # PigeonVision DVB-S2 profile roll-off
RS = 8e6              # symbol rate, symbol/s
NWORDS = 2048         # 32768 symbols per axis
SEED = 0x1234ABCD


# EN 302 307-1 Table A.1, alpha = 0.20: (f / fN, upper dB, lower dB). Beyond 1.7 fN the
# upper limit stays at -40 dB.
ETSI_MASK = [(0.2, .25, -.40), (0.4, .25, -.40), (0.89, .15, -1.10), (0.94, -.50, None),
             (1.0, -2.0, -4.0), (1.11, -8.0, -11.0), (1.23, -16.0, None), (1.4, -24.0, None),
             (1.5, -35.0, None), (1.7, -40.0, None)]


def etsi_mask_margins(lut):
    """Worst margin (dB; negative = violation) of the tap response with ZOH against the template,
    relative to 0 Hz: at the listed points, and for the far sidelobes from 1.7 to 6 fN."""
    fs, fn, n = RS * lut.sps, RS / 2, np.arange(len(lut.taps))

    def db(f):
        h = np.sum(lut.taps * np.exp(-2j * np.pi * f / fs * n)) * np.sinc(f / fs)
        return 20 * np.log10(abs(h))
    ref = db(0.0)
    rel = {r: db(r * fn) - ref for r, _, _ in ETSI_MASK}
    points = min([up - rel[r] for r, up, _ in ETSI_MASK] + [rel[r] - lo for r, _, lo in ETSI_MASK if lo])
    far = -40.0 - max(db(f) - ref for f in np.linspace(1.7 * fn, 6 * fn, 2000))
    return points, far


def symbols_and_words():
    w = m.xorshift32(SEED, NWORDS)
    ib, qb = m.unpack_bits(w)
    return w, (1 - 2.0 * ib) + 1j * (1 - 2.0 * qb)


def check_lut_vs_convolution(words, cases):
    """Max |LUT - float convolution| in LSB; bound = 0.5 + L * 2^-(F+1)."""
    ib, qb = m.unpack_bits(words)
    rows = []
    for sps, L, beta in cases:
        lut = m.Lut(ALPHA, sps, L, beta)
        err = max(np.max(np.abs(m.lut_axis(b, lut) - m.conv_axis(b, lut))) for b in (ib, qb))
        bound = 0.5 + L * 2.0 ** -(m.FRAC_BITS + 1)
        rows.append(dict(sps=sps, L=L, kaiser_beta=beta, max_err_lsb=float(err),
                         bound_lsb=bound, ok=bool(err <= bound)))
    return rows


def metrics(lut: m.Lut, words, sym, tune=0):
    ib, qb = m.unpack_bits(words)
    y = m.lut_axis(ib, lut) + 1j * m.lut_axis(qb, lut)
    evm = m.evm_matched(y, sym, lut.sps, ALPHA, lut.L * lut.sps)
    fs = RS * lut.sps
    f, db = m.analog_spectrum(y, fs, tune)
    bw = RS * (1 + ALPHA)                          # occupied width, Hz
    main = m.band_power(f, db, -bw / 2, bw / 2)
    adj = max(m.band_power(f, db, bw / 2, 1.5 * bw), m.band_power(f, db, -1.5 * bw, -bw / 2))
    # images: everything beyond the first Nyquist zone
    img = (np.abs(f) > fs / 2)
    fd, pd = m.psd(y)
    pd_db = 10 * np.log10(pd / pd.max())
    oob_digital = pd_db[np.abs(fd) * fs > 0.6 * bw].max() if lut.sps > 1 else np.nan
    return dict(
        sps=lut.sps, L=lut.L, kaiser_beta=lut.kaiser_beta,
        evm_db=20 * np.log10(evm),
        aclr_db=10 * np.log10(adj / main),
        worst_image_dbc=float(db[img].max()),
        oob_digital_peak_db=float(oob_digital),
        droop_edge_db=float(20 * np.log10(np.sinc(bw / 2 / fs))),
        table_bytes_per_axis=int(2**lut.L * lut.sps * 2),
    )


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    PLOTS.mkdir(parents=True, exist_ok=True)
    words, sym = symbols_and_words()

    checks = check_lut_vs_convolution(words, [(2, 6, 0), (2, 10, 0), (4, 10, 0), (4, 12, 4)])
    for c in checks:
        print(f"LUT vs conv  sps={c['sps']} L={c['L']:2d} beta={c['kaiser_beta']}: "
              f"max err {c['max_err_lsb']:.3f} LSB (bound {c['bound_lsb']:.3f})")
    assert all(c["ok"] for c in checks), "LUT disagrees with reference convolution"

    sweep = [metrics(m.Lut(ALPHA, sps, L, beta), words, sym)
             for sps in (2, 4) for beta in (0.0, 3.0, 6.0) for L in range(4, 17, 2)]
    print(f"\n{'sps':>3} {'beta':>4} {'L':>3} {'EVM dB':>7} {'ACLR dB':>8} "
          f"{'image dBc':>9} {'table B':>8}")
    for r in sweep:
        print(f"{r['sps']:>3} {r['kaiser_beta']:>4.0f} {r['L']:>3} {r['evm_db']:>7.1f} "
              f"{r['aclr_db']:>8.1f} {r['worst_image_dbc']:>9.1f} {r['table_bytes_per_axis']:>8}")

    mask = []
    for L, beta in ((10, 0.0), (10, 2.0), (10, 3.0), (12, 0.0), (12, 1.0)):
        points, far = etsi_mask_margins(m.Lut(ALPHA, 4, L, beta))
        mask.append(dict(sps=4, L=L, kaiser_beta=beta, points_margin_db=round(points, 2),
                         far_sidelobe_margin_db=round(far, 2)))
        print(f"ETSI mask  sps=4 L={L:2d} beta={beta}: points {points:+.2f} dB, far sidelobes {far:+.2f} dB")

    meta = dict(alpha=ALPHA, symbol_rate=RS, seed=SEED, nwords=NWORDS,
                frac_bits=m.FRAC_BITS, full_scale=m.FULL_SCALE, afe_filter_tune=0,
                afe_filter_model="SLOS789C p.6 typical points, log-f interpolation",
                aclr_definition="adjacent channel width Rs(1+a) at offset Rs(1+a), analog model",
                image_definition="max PSD beyond fs/2 after ZOH sinc and AFE filter, dBc to peak")
    (OUT / "filter_sweep.json").write_text(json.dumps(
        dict(meta=meta, lut_vs_convolution=checks, etsi_mask=mask, sweep=sweep), indent=1))
    plot_sweep(sweep)


def plot_sweep(sweep):
    fig, ax = plt.subplots(1, 2, figsize=(WIDTH, 2.7))
    for sps, col, marker, ls in ((4, BLUE, "o", "-"), (2, RED, "s", "--")):
        r = [x for x in sweep if x["sps"] == sps and x["kaiser_beta"] == 0]
        L = [x["L"] for x in r]
        for a, key in zip(ax, ("evm_db", "aclr_db")):
            dots(a, L, [x[key] for x in r], col, f"{sps} samples/symbol", marker=marker, linestyle=ls)
    ax[0].set_title("(a) Digital TX EVM")
    ax[1].set_title("(b) Adjacent-channel leakage")
    ax[0].set_ylabel("EVM (dB)")
    ax[1].set_ylabel("Adjacent / main channel power (dB)")
    for a in ax:
        a.set_xlabel("Filter span (symbols)")
        a.set_xticks([4, 8, 12, 16])
        a.margins(y=0.1)
    ax[0].legend(loc="upper right")
    ax[1].legend(loc="lower left")
    save_figure(fig, PLOTS / "filter_sweep.png", "Model: 8 Msym/s, RRC α = 0.20, rectangular truncation, AFE filter tune 0.\n"
                "EVM: digital shaper + ideal matched receiver. Leakage: zero-order hold + typical AFE filter magnitude.")


if __name__ == "__main__":
    main()
