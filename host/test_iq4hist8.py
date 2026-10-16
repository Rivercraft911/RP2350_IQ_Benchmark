"""Exhaustive B4 signed-8-bit L7/N4 FIR and optional exact ensemble mask audit.

No device access. --spectrum reuses B1's exact overlap covariance, orthogonal
nonlinear receiver decomposition and continuous-frequency amplitude certificate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import zlib

import numpy as np

from pulse_profiles import (FOUR_LEVELS_Q30, L7_COEFFICIENTS, L7_PROFILE,
                            iq4hist8_rows, iq4hist_prbs, iq4hist_rows)
from test_iq4hist import ensemble_spectrum, unpack_rows

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware/src"
L, N, ROWS, SCALE_DEN = 7, 4, 4 ** 7, 3399


def scalar_rows():
    """Independent scalar integer FIR; // gives the specified floor for negatives."""
    denominator = SCALE_DEN * 2 ** 32
    result = []
    for key in range(ROWS):
        packed = 0
        for phase in range(N):
            total = 0
            history = key
            for age in range(L):
                total += int(L7_COEFFICIENTS[age * N + phase]) * int(FOUR_LEVELS_Q30[history % 4])
                history //= 4
            sample = (total + denominator // 2) // denominator
            assert -128 <= sample <= 127
            packed += (sample & 255) * 2 ** (phase * 8)
        result.append(packed)
    return np.asarray(result, dtype="<u4")


def unpack8(rows):
    values = ((rows[:, None] >> (8 * np.arange(N, dtype=np.uint32))) & 255).astype(np.int64)
    return (values ^ 128) - 128


def continuity_check(rows):
    """Cyclic source, table lookup and direct scalar FIR across arbitrary blocks."""
    counts = []
    for seed in (0, 1, 0x1234abcd, 0xffffffff):
        codes = iq4hist_prbs(seed, 137 * 16)
        expected = []
        for axis, before in enumerate((0, 2)):
            history = [before] * (L - 1)
            samples = []
            for code in codes[axis]:
                history.append(int(code))
                for phase in range(N):
                    total = sum(int(L7_COEFFICIENTS[age * N + phase]) *
                                int(FOUR_LEVELS_Q30[history[-age - 1]]) for age in range(L))
                    denominator = SCALE_DEN * 2 ** 32
                    samples.append((total + denominator // 2) // denominator)
            expected.append(samples)
        expected = np.asarray(expected)
        for block in (1, 7, 16, 257, codes.shape[1]):
            actual = []
            for axis, before in enumerate((0, 2)):
                key = sum(before * 4 ** age for age in range(L))
                packed = []
                for start in range(0, codes.shape[1], block):
                    for code in codes[axis, start:start + block]:
                        key = (key * 4 + int(code)) % ROWS
                        packed.append(rows[key])
                actual.append(unpack8(np.asarray(packed, dtype=np.uint32)).ravel())
            np.testing.assert_array_equal(actual, expected)
        counts.append(codes.shape[1])
    return dict(seeds=4, symbols_per_seed=counts, publication_blocks=[1, 7, 16, 257, 2192])


def add_mer(spectrum):
    for model in ("digital", "held"):
        receiver = spectrum["ideal_rrc_receiver"][model]
        evm = receiver["isi_plus_nonlinear_distortion_percent"]
        receiver["mer_db"] = float(-20 * np.log10(evm / 100))
    return spectrum


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spectrum", action="store_true")
    args = parser.parse_args()
    selected = json.loads((ROOT / "results/pulse-design/2026-10-15/selected.json").read_text())
    frozen = next(p for p in selected["profiles"] if p["id"] == L7_PROFILE)
    np.testing.assert_array_equal(frozen["coefficients_q8"], L7_COEFFICIENTS)
    assert hashlib.sha256(L7_COEFFICIENTS.astype("<i4").tobytes()).hexdigest() == frozen["coefficient_bytes_sha256"]
    with tempfile.TemporaryDirectory(prefix="iq4hist8-native-") as temporary:
        executable = Path(temporary) / "iq4hist8_test"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{SRC}",
                        str(SRC / "iq4hist.c"), str(ROOT / "host/native/iq4hist8_test.c"),
                        "-o", str(executable)], check=True)
        rows = np.frombuffer(subprocess.check_output([str(executable)]), dtype="<u4")
    np.testing.assert_array_equal(rows, scalar_rows())
    np.testing.assert_array_equal(rows, iq4hist8_rows())
    assert rows.nbytes == 65536 and zlib.crc32(rows.tobytes()) == 0x5e13245c
    samples = unpack8(rows)
    assert np.all(samples.mean(axis=0) == 0) and abs(samples).max() == 127
    # Signed bytes in D13..D6, with D5..D0 held zero, preserve sign and zero.
    bus = (samples & 255) << 6
    np.testing.assert_array_equal((bus ^ 8192) - 8192, 64 * samples)
    assert np.all((bus & 63) == 0)
    levels = np.array([np.cos(np.pi / 8), -np.sin(np.pi / 8),
                       np.sin(np.pi / 8), -np.cos(np.pi / 8)])
    keys = np.arange(ROWS, dtype=np.int64)[:, None]
    codes = (keys >> (2 * np.arange(L))) & 3
    ideal = np.einsum("ra,ap->rp", levels[codes], L7_COEFFICIENTS.reshape(L, N)) / (4 * SCALE_DEN)
    error = float(abs(samples - ideal).max())
    phase_l1 = abs(L7_COEFFICIENTS.reshape(L, N)).sum(axis=0) / (4 * SCALE_DEN)
    error_bound = .5 + float(phase_l1.max()) / 2 ** 31
    assert error <= error_bound
    result = dict(profile=L7_PROFILE, rows=ROWS, scalar_samples=ROWS * N,
                  table_bytes=rows.nbytes, table_crc=zlib.crc32(rows.tobytes()),
                  scale_relative_to_14bit=64 / SCALE_DEN, afe_gain_relative_to_14bit=4096 / SCALE_DEN,
                  exact_axis_peak=int(abs(samples).max()), afe_axis_peak=64 * int(abs(samples).max()),
                  max_axis_error_8bit_lsb=error, frozen_tap_error_bound_8bit_lsb=error_bound,
                  rounding="floor((Q8_times_Q30_sum + 3399*2^31)/(3399*2^32))",
                  continuous_mapping=continuity_check(rows))
    if args.spectrum:
        result["spectrum_14bit"] = add_mer(ensemble_spectrum(unpack_rows(iq4hist_rows())))
        result["spectrum_8bit"] = add_mer(ensemble_spectrum(samples))
        # These two simpler scale choices show why full scale is used. Record
        # failures as model results rather than weakening the certificate.
        sums = FOUR_LEVELS_Q30[codes] @ L7_COEFFICIENTS.reshape(L, N)
        result["rejected_plain_scales"] = {}
        for divisor in (64, 54):
            candidate = (sums + (divisor << 37)) // (divisor << 38)
            result["rejected_plain_scales"][str(divisor)] = add_mer(
                ensemble_spectrum(candidate, require_held=False))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
