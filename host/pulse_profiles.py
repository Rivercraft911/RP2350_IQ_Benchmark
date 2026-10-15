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


def qpsk_lut(span, beta, profile=None):
    if profile == L8_PROFILE:
        if span != 8 or beta is not None:
            raise ValueError("L8 optimized pulse is not a Kaiser profile")
        return SimpleNamespace(L=8, sps=4, coef=L8_COEFFICIENTS,
                               taps=L8_COEFFICIENTS / 256,
                               table=iqlut.build_table(L8_COEFFICIENTS, 8, 4))
    return iqlut.Lut(0.20, 4, span, beta, 1.0)
