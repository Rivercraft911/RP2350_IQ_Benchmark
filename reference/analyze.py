"""Filter-design and correctness analysis for the LUT pulse shaper.

Run from the repo root:  python3 reference/analyze.py
Writes results/reference/filter_sweep.json and results/plots/*.png.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import iqlut as m  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "reference"
PLOTS = ROOT / "results" / "plots"

ALPHA = 0.20          # PigeonVision DVB-S2 profile roll-off
RS = 8e6              # symbol rate, symbol/s
NWORDS = 2048         # 32768 symbols per axis
SEED = 0x1234ABCD


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

    meta = dict(alpha=ALPHA, symbol_rate=RS, seed=SEED, nwords=NWORDS,
                frac_bits=m.FRAC_BITS, full_scale=m.FULL_SCALE, afe_filter_tune=0,
                afe_filter_model="SLOS789C p.6 typical points, log-f interpolation",
                aclr_definition="adjacent channel width Rs(1+a) at offset Rs(1+a), analog model",
                image_definition="max PSD beyond fs/2 after ZOH sinc and AFE filter, dBc to peak")
    (OUT / "filter_sweep.json").write_text(json.dumps(
        dict(meta=meta, lut_vs_convolution=checks, sweep=sweep), indent=1))
    plot_sweep(sweep)
    plot_spectra(words)


def plot_sweep(sweep):
    fig, ax = plt.subplots(1, 2, figsize=(10, 4))
    for sps in (2, 4):
        for beta, ls in ((0.0, "-"), (3.0, "--"), (6.0, ":")):
            r = [x for x in sweep if x["sps"] == sps and x["kaiser_beta"] == beta]
            L = [x["L"] for x in r]
            lab = f"sps={sps}, Kaiser β={beta:g}"
            ax[0].plot(L, [x["evm_db"] for x in r], ls, marker="o", label=lab)
            ax[1].plot(L, [x["aclr_db"] for x in r], ls, marker="o", label=lab)
    ax[0].set(xlabel="LUT span L (symbols)", ylabel="TX EVM, ideal matched RX (dB)",
              title=f"Truncation + quantization EVM, α={ALPHA}")
    ax[1].set(xlabel="LUT span L (symbols)", ylabel="ACLR (dB)",
              title="Adjacent-channel ratio incl. DAC images\n(ZOH + AFE filter tune 0, typical)")
    for a in ax:
        a.grid(True, alpha=0.3)
    ax[1].legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(PLOTS / "filter_sweep.png", dpi=130)


def plot_spectra(words):
    ib, qb = m.unpack_bits(words)
    fig, ax = plt.subplots(figsize=(10, 4))
    for sps, L in ((2, 10), (4, 10)):
        lut = m.Lut(ALPHA, sps, L)
        y = m.lut_axis(ib, lut) + 1j * m.lut_axis(qb, lut)
        f, db = m.analog_spectrum(y, RS * sps, tune=0)
        ax.plot(f / 1e6, db, lw=0.8, label=f"sps={sps} (DAC {RS * sps / 1e6:.0f} MS/s), L={L}")
    ax.axvspan(-RS * (1 + ALPHA) / 2e6, RS * (1 + ALPHA) / 2e6, color="k", alpha=0.07)
    ax.set(xlim=(-70, 70), ylim=(-100, 5), xlabel="Baseband frequency (MHz)",
           ylabel="PSD rel. peak (dB)",
           title=f"Modelled AFE7071 modulator input: {RS / 1e6:g} Msym/s QPSK, α={ALPHA}, "
                 "ZOH + filter tune 0 (typical)")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(PLOTS / "analog_spectrum_sps2_vs_sps4.png", dpi=130)


if __name__ == "__main__":
    main()
