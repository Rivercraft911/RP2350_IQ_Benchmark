"""Frozen integer pulse references for opt-in experiments."""
from pathlib import Path
from types import SimpleNamespace
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reference"))
import iqlut

L8_PROFILE = "qpsk_l8_n4_both_m010_far2"
L8_COEFFICIENTS = np.array([
    18026, -9152, -32743, -43578, -16902, 44626, 101009, 97555,
    8822, -128261, -220622, -167118, 78231, 466713, 865736, 1117887,
    1117887, 865736, 466713, 78231, -167118, -220622, -128261, 8822,
    97555, 101009, 44626, -16902, -43578, -32743, -9152, 18026,
], dtype=np.int64)

L7_PROFILE = "qpsk_l7_n4_hold_m010_far2"
L7_COEFFICIENTS = np.array([
    -40558, -43572, -14753, 39237, 100386, 94551, 13532, -127225,
    -219473, -172708, 74855, 466885, 875097, 1132555, 1132555, 875097,
    466885, 74855, -172708, -219473, -127225, 13532, 94551, 100386,
    39237, -14753, -43572, -40558,
], dtype=np.int64)
FOUR_LEVELS_Q30 = np.array([992008094, -410903207, 410903207, -992008094], dtype=np.int64)


def iq4hist_rows():
    """All L7/N4 rows: newest two-bit level low, chronological sample pairs."""
    keys = np.arange(4 ** 7, dtype=np.int64)[:, None]
    codes = (keys >> (2 * np.arange(7))) & 3
    sums = FOUR_LEVELS_Q30[codes] @ L7_COEFFICIENTS.reshape(7, 4)
    samples = (((sums + (1 << 37)) >> 38) & 0x3fff).astype("<u4")
    return samples[:, ::2] | (samples[:, 1::2] << 16)


def iq4hist8_rows():
    """B4 signed 8-bit rows; exact final rounding, full-scale divisor 3399/64."""
    keys = np.arange(4 ** 7, dtype=np.int64)[:, None]
    codes = (keys >> (2 * np.arange(7))) & 3
    sums = FOUR_LEVELS_Q30[codes] @ L7_COEFFICIENTS.reshape(7, 4)
    denominator = 3399 << 32
    samples = (((sums + denominator // 2) // denominator) & 255).astype("<u4")
    return (samples << (8 * np.arange(4, dtype=np.uint32))).sum(axis=1, dtype=np.uint32)


def iq4hist_prbs(seed, symbols):
    """Independent mathematical 8PSK labels as (I,Q) chronological level codes."""
    phases = iqlut.xorshift32(seed, symbols) & 7
    points = np.exp(1j * (np.pi / 8 + phases * np.pi / 4))
    result = []
    for coordinate in (points.real, points.imag):
        a = (coordinate < 0).astype(np.int64)
        b = a ^ (abs(coordinate) < .7).astype(np.int64)
        result.append(a | (b << 1))
    return np.asarray(result)


def qpsk_lut(span, beta, profile=None):
    if profile == L8_PROFILE:
        if span != 8 or beta is not None:
            raise ValueError("L8 optimized pulse is not a Kaiser profile")
        return SimpleNamespace(L=8, sps=4, coef=L8_COEFFICIENTS,
                               taps=L8_COEFFICIENTS / 256,
                               table=iqlut.build_table(L8_COEFFICIENTS, 8, 4))
    return iqlut.Lut(0.20, 4, span, beta, 1.0)
