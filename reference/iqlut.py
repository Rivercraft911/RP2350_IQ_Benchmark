"""QPSK root-raised-cosine lookup-table waveform: reference model for the RP2350 firmware.

Conventions shared bit-for-bit with firmware/src/iqgen.c:

* Input word (uint32): I bits in [15:0], Q bits in [31:16]; symbol j of the word uses bit j
  (I) and bit 16+j (Q). Bit b maps to amplitude s = 1 - 2b (DVB-S2 sign convention).
* LUT index h for symbol n: bit m of h holds the bit of symbol n-(L-1)+m, so bit L-1 is the
  newest symbol (age 0) and bit 0 the oldest (age L-1). History before the first word is 0.
* Output sample p (0..sps-1) of symbol n:  y = sum_{a=0}^{L-1} s[n-a] * g[a*sps + p].
* Tables are built from integer coefficients c = round(g * scale * 2^F) and exact integer sums,
  T = (sum + 2^(F-1)) >> F (floor shift), so C and Python produce identical tables.
* Output slot (16 bits): bits 13:0 = 14-bit two's complement sample, bit 14 = IQ_FLAG
  (1 on I words), bit 15 = 0. One 32-bit word per complex sample: I slot | Q slot << 16.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

FULL_SCALE = 2**13 - 1  # 14-bit two's complement peak
FRAC_BITS = 8           # extra fractional bits in integer coefficients
IQ_FLAG = 1 << 14


# ---------------------------------------------------------------- filter design

def rrc(t: np.ndarray, alpha: float) -> np.ndarray:
    """Root-raised-cosine impulse response, t in symbol periods, unit-energy per symbol."""
    t = np.asarray(t, dtype=float)
    h = np.empty_like(t)
    at = 4 * alpha * t
    zero = np.isclose(t, 0)
    sing = np.isclose(np.abs(at), 1)
    reg = ~(zero | sing)
    tr = t[reg]
    h[reg] = (np.sin(np.pi * tr * (1 - alpha)) + at[reg] * np.cos(np.pi * tr * (1 + alpha))) / (
        np.pi * tr * (1 - at[reg] ** 2))
    h[zero] = 1 - alpha + 4 * alpha / np.pi
    h[sing] = alpha / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                                    + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
    return h


def design_taps(alpha: float, sps: int, span: int, kaiser_beta: float = 0.0) -> np.ndarray:
    """Symmetric RRC truncated to span*sps taps (even length; centre between taps)."""
    n = span * sps
    k = np.arange(n)
    g = rrc((k - (n - 1) / 2) / sps, alpha)
    if kaiser_beta:
        g *= np.kaiser(n, kaiser_beta)
    return g


@dataclass
class Lut:
    alpha: float
    sps: int
    L: int
    kaiser_beta: float = 0.0
    headroom_db: float = 1.0
    taps: np.ndarray = field(init=False, repr=False)    # real-valued, output-scaled (LSB)
    coef: np.ndarray = field(init=False, repr=False)    # int64, scaled by 2^FRAC_BITS
    table: np.ndarray = field(init=False, repr=False)   # int16 [2^L, sps]

    def __post_init__(self):
        g = design_taps(self.alpha, self.sps, self.L, self.kaiser_beta)
        # Exact worst case over all histories is sum |g| per phase: no sequence can clip.
        worst = np.abs(g.reshape(self.L, self.sps)).sum(axis=0).max()
        scale = FULL_SCALE * 10 ** (-self.headroom_db / 20) / worst
        self.taps = g * scale
        self.coef = np.round(self.taps * 2**FRAC_BITS).astype(np.int64)
        self.table = build_table(self.coef, self.L, self.sps)

    def words(self, flag: bool) -> np.ndarray:
        """Firmware table layout: uint32 [2^L, sps/2]; each word = phase 2k | phase 2k+1 << 16."""
        slots = (self.table.astype(np.int64) & 0x3FFF) | (IQ_FLAG if flag else 0)
        slots = slots.astype(np.uint32)
        return slots[:, 0::2] | (slots[:, 1::2] << 16)


def build_table(coef: np.ndarray, L: int, sps: int) -> np.ndarray:
    """T[h, p] = floor((sum_a s_a(h) c[a*sps+p] + 2^(F-1)) / 2^F), s_a = 1 - 2*bit(L-1-a)."""
    h = np.arange(2**L)[:, None]
    ages = np.arange(L)[None, :]
    s = 1 - 2 * ((h >> (L - 1 - ages)) & 1)                     # [2^L, L]
    acc = s @ coef.reshape(L, sps)                              # exact int64
    return ((acc + (1 << (FRAC_BITS - 1))) >> FRAC_BITS).astype(np.int16)


# ---------------------------------------------------------------- deterministic input

def xorshift32(seed: int, n: int) -> np.ndarray:
    """Marsaglia xorshift32 (13, 17, 5); identical to firmware prbs_fill()."""
    out = np.empty(n, dtype=np.uint32)
    x = seed & 0xFFFFFFFF
    for i in range(n):
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        out[i] = x
    return out


def unpack_bits(words: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Input words -> per-symbol I and Q bits (uint8), 16 symbols per word."""
    j = np.arange(16, dtype=np.uint32)
    ib = ((words[:, None] >> j) & 1).astype(np.uint8).ravel()
    qb = ((words[:, None] >> (j + 16)) & 1).astype(np.uint8).ravel()
    return ib, qb


# ---------------------------------------------------------------- generators

def lut_axis(bits: np.ndarray, lut: Lut) -> np.ndarray:
    """LUT generator for one axis: int16 [N*sps]."""
    padded = np.concatenate([np.zeros(lut.L - 1, np.int64), bits.astype(np.int64)])
    win = np.lib.stride_tricks.sliding_window_view(padded, lut.L)   # [N, L], oldest first
    idx = win @ (1 << np.arange(lut.L))                             # bit m = oldest + m
    return lut.table[idx].ravel()


def conv_axis(bits: np.ndarray, lut: Lut) -> np.ndarray:
    """Straightforward reference: zero-stuff the symbols and convolve with the float taps."""
    s = 1.0 - 2.0 * np.concatenate([np.zeros(lut.L - 1), bits])
    u = np.zeros(len(s) * lut.sps)
    u[::lut.sps] = s
    y = np.convolve(u, lut.taps)
    start = (lut.L - 1) * lut.sps
    return y[start:start + len(bits) * lut.sps]


def pack(i16: np.ndarray, q16: np.ndarray) -> np.ndarray:
    """int16 I/Q streams -> uint32 bus words as the PIO consumes them (I slot first)."""
    i = (i16.astype(np.int64) & 0x3FFF) | IQ_FLAG
    q = q16.astype(np.int64) & 0x3FFF
    return (i | (q << 16)).astype(np.uint32)


def unpack(words: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Bus words -> (I, Q, I-flag, Q-flag); 14-bit sign extension."""
    w = words.astype(np.int64)
    sx = lambda v: ((v & 0x3FFF) ^ 0x2000) - 0x2000
    return sx(w), sx(w >> 16), (w >> 14) & 1, (w >> 30) & 1


def generate(words: np.ndarray, lut: Lut) -> np.ndarray:
    """Full model: input words -> one word per complex sample (PACKED layout, bus order)."""
    ib, qb = unpack_bits(words)
    return pack(lut_axis(ib, lut), lut_axis(qb, lut))


LAYOUT_PACKED, LAYOUT_PAIRS = 0, 1


def to_layout(words: np.ndarray, layout: int) -> np.ndarray:
    """PACKED sample words -> firmware buffer layout. PAIRS: [I0|I1] [Q0|Q1] per sample pair;
    the PIO restores bus order I0 Q0 I1 Q1."""
    if layout == LAYOUT_PACKED:
        return words
    i, q = words & 0xFFFF, words >> 16
    ip = i[0::2] | (i[1::2] << 16)
    qp = q[0::2] | (q[1::2] << 16)
    return np.stack([ip, qp], axis=1).ravel().astype(np.uint32)


# ---------------------------------------------------------------- metrics

def evm_matched(y: np.ndarray, symbols: np.ndarray, sps: int, alpha: float, tx_len: int,
                rx_span: int = 64, trim: int = 128) -> float:
    """RMS EVM (fraction) after an ideal long RRC matched filter and complex LS gain.

    y[n*sps + p] = (upsampled symbols * g)[n*sps + p], so symbol k peaks at k*sps + c_tx + c_rx,
    with centres c = (len - 1)/2. Both filters have even length, so the sum is an integer.
    """
    rx = design_taps(alpha, sps, rx_span)
    d2 = (tx_len - 1) + (len(rx) - 1)
    assert d2 % 2 == 0, "cascade centre must fall on a sample"
    z = np.convolve(y, rx)[d2 // 2::sps][:len(symbols)]
    ref, zs = symbols[trim:-trim], z[trim:-trim]
    gain = np.vdot(ref, zs) / np.vdot(ref, ref)
    err = zs / gain - ref
    return float(np.sqrt(np.mean(np.abs(err) ** 2) / np.mean(np.abs(ref) ** 2)))


def psd(y: np.ndarray, nfft: int = 4096) -> tuple[np.ndarray, np.ndarray]:
    """Welch PSD (Hann, 50 % overlap) of a complex sequence; f in cycles/sample, fftshifted."""
    from scipy.signal import welch
    f, p = welch(y, fs=1.0, window="hann", nperseg=nfft, return_onesided=False,
                 scaling="density")
    return np.fft.fftshift(f), np.fft.fftshift(p)


# AFE7071 SLOS789C integrated baseband filter, typical attenuation (dB, positive) vs MHz.
# Tunes 0 and 8: the p.6 table (1/18/42/58 and 1/18/42/65 dB). Tune 4 is not tabulated; it is
# read from Figure 36 (p.13) at +-1 dB reading accuracy. Figure 36 draws tune 0 about 2-3 dB
# lower than the table at 20 MHz, so these are typical curves for one part, not bounds.
AFE_FILTER_TYPICAL = {
    0: ([10, 20, 40, 55], [1, 18, 42, 58]),
    4: ([3, 4, 5, 6, 7, 8, 9, 10, 12.5, 15, 17.5, 20], [0.3, 1.5, 4, 8, 13, 18, 22, 26, 34, 40, 46, 50]),
    8: ([2.5, 5, 10, 20], [1, 18, 42, 65]),
}


def afe_filter_db(f_mhz: np.ndarray, tune: int) -> np.ndarray:
    """Attenuation (dB, positive) interpolated linearly in log-frequency between the typical
    points; ~f^4 toward 0 dB below the first point, 80 dB/decade (4th order) beyond the last.
    A screening model only: not a guaranteed response, and magnitude only (the datasheet gives
    2 degrees RMS phase deviation from linear)."""
    fp, ap = map(np.asarray, AFE_FILTER_TYPICAL[tune])
    f = np.maximum(np.abs(np.asarray(f_mhz, float)), 1e-6)
    a = np.interp(np.log10(f), np.log10(fp), ap, left=0.0)
    beyond = f > fp[-1]
    a[beyond] = ap[-1] + 80 * np.log10(f[beyond] / fp[-1])
    a[f < fp[0]] = ap[0] * (f[f < fp[0]] / fp[0]) ** 4   # smooth toward 0 dB
    return a


def analog_spectrum(y: np.ndarray, fs_hz: float, tune: int, span_fs: float = 3.0,
                    nfft: int = 4096) -> tuple[np.ndarray, np.ndarray]:
    """Continuous-time PSD model at the AFE modulator input: periodic digital PSD x ZOH sinc^2
    x interpolated AFE baseband filter. Returns (f_hz, dB relative to in-band peak)."""
    fd, pd = psd(y, nfft)
    f = np.linspace(-span_fs * fs_hz, span_fs * fs_hz, int(2 * span_fs * nfft) + 1)
    p = np.interp(((f / fs_hz + 0.5) % 1.0) - 0.5, fd, pd, period=1.0)
    p = p * np.sinc(f / fs_hz) ** 2 * 10 ** (-afe_filter_db(f / 1e6, tune) / 10)
    db = 10 * np.log10(np.maximum(p, 1e-30))
    return f, db - db.max()


def band_power(f: np.ndarray, db: np.ndarray, lo: float, hi: float) -> float:
    m = (f >= lo) & (f <= hi)
    return float(np.trapezoid(10 ** (db[m] / 10), f[m]))
