"""Why the AFE7071 needs 4 samples/symbol: DAC -> ZOH -> AFE filter -> ideal receiver model.

Run from the repo root:  python3 reference/analyze_sps.py
Writes results/reference/sps_analysis.json and results/plots/why_4_samples_per_symbol.png.

The AFE7071 has no interpolation: the rate on its bus is the DAC rate f_s = N R_s. A DAC's output
spectrum repeats at every multiple of f_s (images), shaped by the zero-order hold
|sinc(f/f_s)|, and the only filter before the modulator is the integrated 4th-order low-pass.
The first image's inner edge is at f_s - B, with B = R_s (1 + a) / 2 the signal half-width.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import iqlut as m  # noqa: E402
from plotstyle import AMBER, CYAN, MUTED, ROSE, glow, plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ALPHA, RS = 0.20, 8e6
U = 32                     # simulation oversampling of the DAC rate (continuous-time proxy)


def eq_taps(N: int, L: int, tune: int | None, window: float = 4.0) -> np.ndarray:
    """RRC taps, optionally pre-equalised for ZOH and AFE filter droop over the signal band:
    G(f) = RRC(f) / (sinc(f/f_s) H(f)), by frequency sampling, truncated to L*N taps."""
    n = L * N
    if tune is None:
        return m.design_taps(ALPHA, N, L)
    nf = 8192
    f = np.fft.fftfreq(nf) * N * RS                       # Hz at the DAC rate
    x = np.abs(f) / RS                                     # in symbol rates
    lo, hi = (1 - ALPHA) / 2, (1 + ALPHA) / 2
    P = np.where(x <= lo, 1.0, np.where(x <= hi, np.sqrt(0.5 * (1 + np.cos(np.pi / ALPHA * (x - lo)))), 0.0))
    comp = np.sinc(f / (N * RS)) * 10 ** (-m.afe_filter_db(f / 1e6, tune) / 20)
    G = P / np.maximum(comp, 1e-3)
    g = np.real(np.fft.ifft(G))
    k = np.arange(n) - (n - 1) / 2
    g = np.real(np.fft.ifft(G * np.exp(-2j * np.pi * np.fft.fftfreq(nf) * (n - 1) / 2)))[:n]
    return g * np.kaiser(n, window)


def simulate(N: int, L: int, tune: int, eq: bool, fscale: float = 1.0, nsym: int = 16384, seed=7):
    """fscale stretches the actual filter in frequency (corner error) while the pre-equaliser
    stays designed for the nominal curve."""
    from scipy.signal import fftconvolve, welch
    rng = np.random.default_rng(seed)
    sym = (1 - 2.0 * rng.integers(0, 2, nsym)) + 1j * (1 - 2.0 * rng.integers(0, 2, nsym))
    g = eq_taps(N, L, tune if eq else None)
    u = np.zeros(nsym * N, complex)
    u[::N] = sym
    y = np.convolve(u, g)                                   # DAC samples (full precision)
    rms_backoff = 20 * np.log10(np.sqrt(np.mean(np.abs(y.real) ** 2)) / np.abs(g.reshape(L, N)).sum(0).max())
    z = np.repeat(y, U)                                     # zero-order hold at U x f_s
    fs_sim = N * RS * U
    Z = np.fft.fft(z)
    f = np.fft.fftfreq(len(z), 1 / fs_sim)
    Z *= 10 ** (-m.afe_filter_db(f / fscale / 1e6, tune) / 20)   # AFE filter (magnitude)
    out = np.fft.ifft(Z)
    fw, pw = welch(out, fs=fs_sim, nperseg=16384, return_onesided=False)
    fw, pw = np.fft.fftshift(fw), np.fft.fftshift(pw)
    db = 10 * np.log10(pw / pw[np.abs(fw) < 0.4 * RS].max())
    bw = RS * (1 + ALPHA)
    img = np.abs(fw) >= N * RS - bw / 2
    band = lambda lo, hi: np.trapezoid(pw[(fw >= lo) & (fw <= hi)], fw[(fw >= lo) & (fw <= hi)])
    aclr = 10 * np.log10(max(band(bw / 2, 1.5 * bw), band(-1.5 * bw, -bw / 2)) / band(-bw / 2, bw / 2))
    # Ideal RRC matched receiver at the simulation rate; symbol phase refined to 1/64 sample
    # by FFT fractional delay around the best integer delay.
    rx = m.design_taps(ALPHA, N * U, 32)
    r = fftconvolve(out, rx)
    sps_sim = N * U
    base = (len(g) - 1) * U // 2 + (len(rx) - 1) // 2 - sps_sim

    def evm_at(rr, d):
        zs = rr[base + d::sps_sim][:nsym]
        ref, zs = sym[64:len(zs) - 64], zs[64:-64]
        gain = np.vdot(ref, zs) / np.vdot(ref, ref)
        return np.sqrt(np.mean(np.abs(zs / gain - ref) ** 2) / 2)

    d0 = min(range(0, 2 * sps_sim), key=lambda d: evm_at(r, d))
    R, fr = np.fft.fft(r), np.fft.fftfreq(len(r))
    best = min(evm_at(np.fft.ifft(R * np.exp(2j * np.pi * fr * t)), d0) for t in np.linspace(-1, 1, 129))
    return dict(N=N, L=L, tune=tune, eq=eq, filter_freq_scale=fscale, f_dac_msps=N * RS / 1e6,
                bus_mwords=2 * N * RS / 1e6, worst_image_dbc=float(db[img].max()), aclr_db=float(aclr),
                evm_db=float(20 * np.log10(best)), rms_backoff_db=float(rms_backoff)), (fw, db)


CASES = [("(a) 2 sps, filter tune 0", 2, 12, 0, False, None),
         ("(b) 2 sps, tune 4 + digital pre-equalisation", 2, 12, 4, True, None),
         ("(c) 4 sps, tune 0 (current design)", 4, 10, 0, False, None)]


def main():
    res, spectra = [], []
    def show(r):
        print(f"{r['name']:<52} image {r['worst_image_dbc']:6.1f} dBc  ACLR {r['aclr_db']:6.1f} dB  "
              f"EVM {r['evm_db']:6.1f} dB  rms {r['rms_backoff_db']:5.1f} dBFS")
    for name, N, L, tune, eq, col in CASES:
        r, sp = simulate(N, L, tune, eq)
        r["name"] = name
        res.append(r)
        spectra.append(sp)
        show(r)
    sens = []                                               # corner error vs a fixed equaliser
    for fsc in (0.9, 1.1):
        r, _ = simulate(2, 12, 4, True, fscale=fsc)
        r["name"] = f"    (b) with the real tune-4 corner x{fsc}"
        sens.append(r)
        show(r)
    (ROOT / "results" / "reference").mkdir(parents=True, exist_ok=True)
    (ROOT / "results" / "reference" / "sps_analysis.json").write_text(json.dumps(dict(
        alpha=ALPHA, symbol_rate=RS, filter_model="AFE_FILTER_TYPICAL in reference/iqlut.py",
        note="typical curves; magnitude-only filter; image = max PSD beyond f_s - B; rms = I-axis rms "
             "relative to the LUT's no-clip full scale", cases=res, corner_sensitivity=sens), indent=1))

    colors = [ROSE, AMBER, CYAN]
    fig, axes = plt.subplots(len(CASES), 1, figsize=(7.5, 7), sharex=True)
    for ax, (name, N, L, tune, eq, _), r, (fw, db), col in zip(axes, CASES, res, spectra, colors):
        f = np.linspace(-72e6, 72e6, 4000)
        ax.fill_between(fw / 1e6, db, -120, color=col, alpha=0.07, lw=0)
        glow(ax, fw / 1e6, db, col, lw=1)
        ax.plot(f / 1e6, -m.afe_filter_db(f / 1e6, tune), color=MUTED, lw=0.9, ls=(0, (4, 3)))
        ax.set_ylim(-100, 6)
        ax.set_ylabel("dB")
        ax.set_title(f"N = {N}  tune {tune}" + ("  + EQ" if eq else ""), loc="left")
        ax.set_title(f"images {r['worst_image_dbc']:.0f} dBc", loc="right", color=col, fontsize=9)
    axes[-1].set_xlabel("MHz")
    axes[-1].set_xlim(-72, 72)
    fig.tight_layout(h_pad=1.5)
    fig.savefig(ROOT / "results" / "plots" / "why_4_samples_per_symbol.png")

if __name__ == "__main__":
    main()
