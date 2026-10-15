"""Isolated two-binary-plane 8PSK waveform test. Compiles/runs natively in a temp directory.

Verifies all 8 constellation angles, block histories, exact quantized reference, PAIRS/IQ flags,
and error against independent floating-point complex convolution. No Pico or RF hardware calls.
"""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference"))
import gen_coeffs as g
import iqlut as m

GROUPS, L, N = 137, 12, 4
AQ30, BQ30 = 701455651, 290552444


def reference(seed: int):
    phases = m.xorshift32(seed, GROUPS * 16) & 7
    points = np.exp(1j * (np.pi / 8 + phases * np.pi / 4))
    assert len(np.unique(phases)) == 8
    # Recover the two signs independently from the mathematical constellation, not C's map.
    sign_a_i = np.sign(points.real).astype(np.int64)
    sign_a_q = np.sign(points.imag).astype(np.int64)
    sign_b_i = sign_a_i * np.where(np.abs(points.real) > 0.7, 1, -1)
    sign_b_q = sign_a_q * np.where(np.abs(points.imag) > 0.7, 1, -1)
    axis_signs = [(sign_a_i, sign_b_i, 1, 1), (sign_a_q, sign_b_q, 1, -1)]
    lut = m.Lut(g.ALPHA, N, L, g.KAISER_BETA, g.HEADROOM_DB)
    ca = (lut.coef * AQ30 + (1 << 29)) >> 30
    cb = (lut.coef * BQ30 + (1 << 29)) >> 30
    axes = []
    for sa, sb, pa, pb in axis_signs:
        outputs = []
        for signs, before, coef in [(sa, pa, ca), (sb, pb, cb)]:
            padded = np.concatenate([np.full(L - 1, before), signs])
            win = np.lib.stride_tricks.sliding_window_view(padded, L)[:, ::-1]
            exact = win @ coef.reshape(L, N)
            outputs.append(((exact + (1 << (m.FRAC_BITS - 1))) >> m.FRAC_BITS).ravel())
        axes.append(outputs[0] + outputs[1])
    want = m.to_layout(m.pack(*axes), m.LAYOUT_PAIRS)
    # Direct complex convolution includes the same prehistory, but no plane/coef quantization.
    before = np.exp(1j * np.pi / 8)
    syms = np.concatenate([np.full(L - 1, before), points])
    impulses = np.zeros(len(syms) * N, dtype=complex)
    impulses[::N] = syms
    direct = np.convolve(impulses, lut.taps)[(L - 1) * N:][:len(points) * N]
    error = axes[0] + 1j * axes[1] - direct
    # Per axis: two rounded planes <=1 LSB, base/plane coefficient rounding <3L/512 LSB.
    bound = 1 + 3 * L / (2 ** (m.FRAC_BITS + 1)) + 1e-5
    max_axis_error = max(np.max(np.abs(error.real)), np.max(np.abs(error.imag)))
    assert max_axis_error <= bound, (max_axis_error, bound)
    assert max(np.max(np.abs(axes[0])), np.max(np.abs(axes[1]))) < 8192
    return want, float(max_axis_error), bound


def main():
    with tempfile.TemporaryDirectory(prefix="iq8-native-") as build:
        exe = Path(build) / "iq8_test"
        src = ROOT / "firmware" / "src"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{src}",
                        str(src / "iq8.c"), str(ROOT / "host/native/iq8_test.c"), "-o", str(exe)], check=True)
        for seed in [0x1234ABCD, 1, 0xFFFFFFFF]:
            want, err, bound = reference(seed)
            for block in [1, 7, 64, GROUPS]:
                got = np.frombuffer(subprocess.check_output([str(exe), str(seed), str(block)]), dtype="<u4")
                np.testing.assert_array_equal(got, want)
                print(json.dumps(dict(seed=seed, groups=GROUPS, block=block,
                                      crc=zlib.crc32(got.tobytes()), max_axis_error_lsb=err,
                                      error_bound_lsb=bound, table_bytes=65536)))


if __name__ == "__main__":
    main()
