"""Exhaustive N5 padded-byte table and exact held-DAC ensemble certificate.

No device access. --spectrum evaluates the B1/B4 overlap-covariance/Walsh
model with N=5 throughout, including nonlinear table rounding and all aliases
admitted by the ideal alpha0.20 RRC receiver. The default test compiles actual
firmware C and checks every sample against an independent scalar integer FIR.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import tempfile
import zlib

import numpy as np

from pulse_profiles import (FOUR_LEVELS_Q30, N5_COEFFICIENTS, N5_PROFILE,
                            N5_SCALE_DEN, iq5hist_rows, iq4hist_prbs)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware/src"
L, N, ROWS = 7, 5, 4 ** 7
TABLE_CRC = 0xa299c7c1


def scalar_samples(divisor=N5_SCALE_DEN):
    """Plain scalar FIR: no table lookup, matrix product or history shift."""
    denominator = divisor * 2 ** 32
    result = np.empty((ROWS, N), dtype=np.int64)
    for key in range(ROWS):
        for phase in range(N):
            total = 0
            for age in range(L):
                code = key // (4 ** age) % 4
                total += int(N5_COEFFICIENTS[age * N + phase]) * int(FOUR_LEVELS_Q30[code])
            result[key, phase] = (total + denominator // 2) // denominator
    return result


def unpack_rows(rows):
    slots = np.stack([(rows[:, phase // 4] >> (8 * (phase % 4))) & 255
                      for phase in range(N)], axis=1).astype(np.int64)
    return (slots ^ 128) - 128


def continuity_check(samples):
    """Table traversal versus direct scalar FIR across publication boundaries."""
    count = 137 * 16
    for seed in (0, 1, 0x1234abcd, 0xffffffff):
        codes = iq4hist_prbs(seed, count)
        for axis, before in enumerate((0, 2)):
            history = [before] * (L - 1)
            expected = []
            for code in codes[axis]:
                history.append(int(code))
                for phase in range(N):
                    total = sum(int(N5_COEFFICIENTS[age * N + phase]) *
                                int(FOUR_LEVELS_Q30[history[-age - 1]]) for age in range(L))
                    denominator = N5_SCALE_DEN * 2 ** 32
                    expected.append((total + denominator // 2) // denominator)
            for block in (1, 7, 16, 257, count):
                key = sum(before * 4 ** age for age in range(L))
                actual = []
                for start in range(0, count, block):
                    for code in codes[axis, start:start + block]:
                        key = (key * 4 + int(code)) % ROWS
                        actual.extend(samples[key])
                np.testing.assert_array_equal(actual, expected)
    return dict(seeds=4, symbols_per_seed=count, publication_blocks=[1, 7, 16, 257, count])


def ensemble_spectrum(samples, require_held=True):
    """Exact iid four-level PSD, from conditional means over overlapping histories.

    Histories separated by k symbols share L-k independent four-valued symbols.
    Marginalizing their disjoint symbols gives exact phase-pair correlations,
    without a random simulation or a linear approximation to LUT rounding.
    """
    means = samples.mean(axis=0)
    assert np.all(means == 0), means
    correlations = np.zeros(2 * L * N - 1)
    center = L * N - 1
    for lag in range(L):
        # Numeric keys: high digits are older; previous row's low digits overlap
        # current row's high digits. Average each row over its disjoint digits.
        current = samples.reshape(4 ** (L - lag), 4 ** lag, N).mean(axis=1)
        previous = samples.reshape(4 ** lag, 4 ** (L - lag), N).mean(axis=0)
        covariance = np.einsum("ip,iq->pq", current, previous) / len(current)
        for p in range(N):
            for q in range(N):
                d = N * lag + p - q
                correlations[center + d] += covariance[p, q] / N
                if lag:
                    correlations[center - d] += covariance[p, q] / N
    np.testing.assert_allclose(correlations, correlations[::-1], rtol=0, atol=1e-8)
    grid = np.unique(np.concatenate([np.linspace(0, 6, 1_048_577),
                                     np.array([0, .2, .4, .89, .94, 1, 1.11, 1.23, 1.4, 1.5, 1.7, 6])]))
    # Even time-averaged covariance makes the PSD a real cosine polynomial.
    psd = np.full(len(grid), correlations[center])
    for lag, corr in enumerate(correlations[center + 1:], 1):
        psd += 2 * corr * np.cos(np.pi * grid * lag / N)
    assert psd.min() > 0
    models = json.loads((ROOT / "results/pulse-design/2026-10-15/selected.json").read_text())["masks"]
    upper = np.asarray(models["upper_knots_fn_db"])
    lower = np.asarray(models["lower_knots_fn_db"])
    up = np.interp(grid, upper[:, 0], upper[:, 1])
    lo = np.interp(grid, lower[:, 0], lower[:, 1])
    classes = walsh_classes(samples)
    probes = np.array([0, .89, 1.49727, 2.16324, 4.38625, 6])
    transfers = np.einsum("ct,ft->cf", classes,
                         np.exp(-1j * np.pi / N * probes[:, None] * np.arange(N * L)))
    walsh_psd = (abs(transfers) ** 2).sum(axis=0) / N
    covariance_psd = np.full(len(probes), correlations[center])
    for lag, corr in enumerate(correlations[center + 1:], 1):
        covariance_psd += 2 * corr * np.cos(np.pi * probes * lag / N)
    np.testing.assert_allclose(walsh_psd, covariance_psd, rtol=1e-8, atol=1e-7)
    nonzero = classes != 0
    first = np.argmax(nonzero, axis=1)
    last = N * L - 1 - np.argmax(nonzero[:, ::-1], axis=1)
    center_time = (first + last) / 2
    time = np.arange(N * L)
    norms = abs(classes).sum(axis=1)
    derivative_norms = (abs(classes) * abs(time - center_time[:, None]) * np.pi / N).sum(axis=1)
    normalizer = np.sqrt(N * psd[0])
    max_amplitude = float(np.linalg.norm(norms) / normalizer)
    derivative = float(np.linalg.norm(derivative_norms) / normalizer)
    result = {}
    for hold in (False, True):
        response = 10 * np.log10(psd / psd[0])
        if hold:
            response += 20 * np.log10(np.sinc(grid / (2 * N)))
        far = grid >= 1.7; low = grid <= lower[-1, 0]
        upper_margin = up - response
        amplitude = np.sqrt(psd / psd[0])
        derivative_bound = derivative
        if hold:
            amplitude *= np.sinc(grid / (2 * N))
            # sinc(x)=integral[-.5,.5] exp(2*pi*i*x*t)dt gives
            # |d sinc(f/(2N))/df| <= pi/(4N).
            derivative_bound += max_amplitude * np.pi / (4 * N)
        upper_amplitude = 10 ** (up / 20)
        lower_amplitude = 10 ** (lo / 20)
        upper_slope = np.max(abs(np.diff(upper[:, 1]) / np.diff(upper[:, 0])))
        lower_slope = np.max(abs(np.diff(lower[:, 1]) / np.diff(lower[:, 0])))
        upper_derivative = upper_slope * np.log(10) / 20 * upper_amplitude.max()
        lower_derivative = lower_slope * np.log(10) / 20 * lower_amplitude.max()
        half_gap = float(np.diff(grid).max()) / 2
        upper_slack = float((upper_amplitude - amplitude).min() -
                            half_gap * (derivative_bound + upper_derivative))
        lower_slack = float((amplitude[low] - lower_amplitude[low]).min() -
                            half_gap * (derivative_bound + lower_derivative))
        result["held" if hold else "digital"] = dict(
            upper_margin_db=float(upper_margin.min()),
            upper_worst_fn=float(grid[upper_margin.argmin()]),
            lower_margin_db=float((response[low] - lo[low]).min()),
            far_margin_db=float((-40 - response[far]).min()),
            grid_points=len(grid), response_derivative_bound=derivative_bound,
            certified_upper_amplitude_slack=upper_slack,
            certified_lower_amplitude_slack=lower_slack,
            continuous_certificate=bool(upper_slack > 0 and lower_slack > 0))
    result["ideal_rrc_receiver"] = receiver_audit(classes)
    if require_held:
        assert result["held"]["continuous_certificate"]
    return result


def walsh_classes(samples):
    """Orthogonal iid sign products grouped by whole-symbol translations."""
    coefficients = samples.astype(float).copy()
    for bit in range(2 * L):
        blocks = coefficients.reshape(-1, 2, 1 << bit, N)
        a, b = blocks[:, 0].copy(), blocks[:, 1].copy()
        blocks[:, 0], blocks[:, 1] = a + b, a - b
    coefficients /= ROWS
    classes = {}
    for mask in range(1, ROWS):
        canonical, shift = mask, 0
        while not canonical & 3:
            canonical >>= 2; shift += 1
        if canonical not in classes:
            classes[canonical] = np.zeros(N * L)
        classes[canonical][N * shift:N * (shift + 1)] = coefficients[mask]
    # First two canonical masks are the A and B single-bit linear responses.
    assert list(classes)[:2] == [1, 2]
    return np.asarray(list(classes.values()))


def receiver_audit(classes):
    """Exact ensemble receiver variance, with numerical ideal-RRC integration.

    The same orthogonal history products that give the exact PSD also give
    receiver power. A small Gram matrix integrates the coherent sample aliases
    for every finite impulse response; no nonlinear term is dropped.
    """
    result = {}
    target_a = (np.cos(np.pi / 8) + np.sin(np.pi / 8)) / 2
    target_b = (np.cos(np.pi / 8) - np.sin(np.pi / 8)) / 2
    products = np.einsum("ct,cu->tu", classes, classes)
    for hold in (False, True):
        estimates = []
        for points in (16384, 65536):
            f = np.arange(points) / points - .5
            basis = np.zeros((points, N * L), complex)
            delay = (L * N - 1) / (2 * N) + (1 / (2 * N) if hold else 0)
            for alias in (-1, 0, 1):
                frequency = f + alias
                af = abs(frequency)
                receiver = np.where(af <= .4, 1, np.where(af < .6,
                    np.sqrt(np.maximum(0, .5 * (1 + np.cos(np.pi / .2 * (af - .4))))), 0))
                amplitude = receiver * (np.sinc(frequency / N) if hold else 1)
                phase_delay = delay - (1 / (2 * N) if hold else 0)
                basis += amplitude[:, None] * np.exp(2j * np.pi * frequency[:, None] *
                    (phase_delay - np.arange(N * L)[None, :] / N))
            main = basis.mean(axis=0).real
            gram = np.einsum("ft,fu->tu", basis.conj(), basis).real / points
            variance = float((products * gram).sum())
            axis_gain = float((target_a * (classes[0] * main).sum() +
                               target_b * (classes[1] * main).sum()) / .5)
            target_power = .5 * axis_gain ** 2
            evm = np.sqrt(max(0, variance - target_power) / target_power)
            estimates.append(float(100 * evm))
        result["held" if hold else "digital"] = dict(
            isi_plus_nonlinear_distortion_percent=estimates[-1],
            integration_points=[16384, 65536], refinement_delta_percentage_points=abs(estimates[1] - estimates[0]))
    return result


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
    with tempfile.TemporaryDirectory(prefix="iq5hist-native-") as temporary:
        executable = Path(temporary) / "iq5hist_test"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{SRC}",
                        str(SRC / "iq5hist.c"), str(ROOT / "host/native/iq5hist_test.c"),
                        "-o", str(executable)], check=True)
        rows = np.frombuffer(subprocess.check_output([str(executable)]), dtype="<u4").reshape(ROWS, 2)
    assert rows.nbytes == 131072 and not np.any(rows[:, 1] & 0xffffff00)
    assert zlib.crc32(rows.tobytes()) == TABLE_CRC
    np.testing.assert_array_equal(rows, iq5hist_rows())
    samples = unpack_rows(rows)
    np.testing.assert_array_equal(samples, scalar_samples())
    assert abs(samples).max() == 127 and np.all(samples.mean(axis=0) == 0)
    bus = (samples & 255) << 6
    np.testing.assert_array_equal((bus ^ 8192) - 8192, 64 * samples)
    keys = np.arange(ROWS, dtype=np.int64)[:, None]
    codes = (keys >> (2 * np.arange(L))) & 3
    levels = np.array([np.cos(np.pi / 8), -np.sin(np.pi / 8),
                       np.sin(np.pi / 8), -np.cos(np.pi / 8)])
    ideal = np.einsum("ra,ap->rp", levels[codes], N5_COEFFICIENTS.reshape(L, N)) / (4 * N5_SCALE_DEN)
    error = float(abs(samples - ideal).max())
    phase_l1 = abs(N5_COEFFICIENTS.reshape(L, N)).sum(axis=0) / (4 * N5_SCALE_DEN)
    error_bound = .5 + float(phase_l1.max()) / 2 ** 31
    assert error <= error_bound
    result = dict(profile=N5_PROFILE, rows=ROWS, scalar_samples=ROWS * N,
                  table_bytes=rows.nbytes, table_crc=TABLE_CRC, row_words=2,
                  padding_bytes_per_row=3, exact_axis_peak=int(abs(samples).max()),
                  afe_axis_peak=64 * int(abs(samples).max()), scale_denominator=N5_SCALE_DEN,
                  afe_headroom_db=float(20 * np.log10(8191 / (64 * abs(samples).max()))),
                  afe_gain_relative_to_14bit=4096 / N5_SCALE_DEN,
                  coefficient_bytes_sha256=hashlib.sha256(N5_COEFFICIENTS.astype("<i4").tobytes()).hexdigest(),
                  coefficients_q8=N5_COEFFICIENTS.tolist(),
                  max_axis_error_8bit_lsb=error, frozen_tap_error_bound_8bit_lsb=error_bound,
                  rounding="floor((Q8_times_Q30_sum + 3388*2^31)/(3388*2^32))",
                  continuous_mapping=continuity_check(samples),
                  environment=dict(python=platform.python_version(), numpy=np.__version__,
                                   compiler=subprocess.check_output(["cc", "--version"], text=True).splitlines()[0]))
    if args.spectrum:
        result["spectrum_8bit"] = add_mer(ensemble_spectrum(samples))
        result["evaluated_alternatives"] = {}
        for bits, divisors in ((8, (3386, 3387)), (6, (13704, 13705, 13706))):
            for divisor in divisors:
                candidate = scalar_samples(divisor)
                assert abs(candidate).max() <= (1 << (bits - 1)) - 1
                result["evaluated_alternatives"][f"{bits}bit_divisor{divisor}"] = add_mer(
                    ensemble_spectrum(candidate, require_held=False))
        result["scope"] = dict(alpha=.20, fn="f/(Rs/2)", upper_interval=[0, 6],
            lower_interval=[0, 1.11], far_interval=[1.7, 6],
            ensemble="iid uniform four-level coordinate histories",
            limitations=["not deterministic PLHEADER/pilot/coded-payload PSD",
                         "ideal DAC hold and ideal RRC receiver; no RF measurement",
                         "not a certificate for DAC reconstruction images"])
    print(json.dumps(result))


if __name__ == "__main__":
    main()
