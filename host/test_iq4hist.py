"""Exhaustive native four-level L7/N4 tables and continuous QPSK/8PSK mapping.

Compares all 4^7 rows to a scalar integer FIR; checks direct mathematical levels,
PRBS packing, existing PL mappers and partial groups across VCM boundaries.
The optional --spectrum audit evaluates the exact iid four-level ensemble PSD.
No Pico, serial port, shared firmware build or RF hardware is accessed.
"""
from __future__ import annotations

import argparse
import ctypes as ct
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zlib

import numpy as np

from pulse_profiles import (FOUR_LEVELS_Q30, L7_COEFFICIENTS, L7_PROFILE,
                            iq4hist_prbs, iq4hist_rows)
import test_dvbs2_8psk as mapper
import fwsrc

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware/src"
L, N, ROWS, ROW_WORDS = 7, 4, 4 ** 7, 2
CANARY = 0x7ad4c781
PTR = ct.POINTER(ct.c_uint32)


def scalar_rows():
    """Plain scalar FIR, without the vectorized table model's matrix operations."""
    result = np.empty((ROWS, ROW_WORDS), dtype="<u4")
    for key in range(ROWS):
        samples = []
        for phase in range(N):
            total = sum(int(L7_COEFFICIENTS[age * N + phase]) *
                        int(FOUR_LEVELS_Q30[key // (4 ** age) % 4]) for age in range(L))
            samples.append((total + (1 << 37)) // (1 << 38) & 0x3fff)
        result[key] = [samples[0] | samples[1] << 16, samples[2] | samples[3] << 16]
    return result


def unpack_rows(rows):
    slots = np.stack([rows & 0x3fff, rows >> 16], axis=-1).reshape(ROWS, N)
    return (slots.astype(np.int64) ^ 0x2000) - 0x2000


def level_codes(points):
    result = []
    for coord in (points.real, points.imag):
        a = (coord < 0).astype(np.int64)
        b = a ^ (abs(coord) < .7).astype(np.int64)
        code = a | (b << 1)
        np.testing.assert_allclose(FOUR_LEVELS_Q30[code] / 2 ** 30, coord, atol=5e-10, rtol=0)
        result.append(code)
    return np.asarray(result)


def waveform(codes, rows, block):
    """History-window traversal across arbitrary publication boundaries."""
    result = []
    for axis, before in enumerate((0, 2)):
        key = sum(before << (2 * age) for age in range(L))
        out = []
        for start in range(0, codes.shape[1], block):
            for code in codes[axis, start:start + block]:
                key = ((key << 2) | int(code)) & (ROWS - 1)
                out.extend(rows[key])
        result.append(np.asarray(out, dtype="<u4"))
    return np.stack(result, axis=-1).ravel()


def scalar_waveform(codes):
    """Direct convolution, with no history-key construction or table lookup."""
    result = []
    for axis, before in enumerate((0, 2)):
        symbols = np.concatenate([np.full(L - 1, FOUR_LEVELS_Q30[before]),
                                  FOUR_LEVELS_Q30[codes[axis]]])
        phases = []
        for phase in range(N):
            sums = np.convolve(symbols, L7_COEFFICIENTS[phase::N])[L - 1:][:codes.shape[1]]
            phases.append((sums + (1 << 37)) >> 38)
        samples = np.stack(phases, axis=-1).ravel() & 0x3fff
        result.append((samples[::2] | samples[1::2] << 16).astype("<u4"))
    return np.stack(result, axis=-1).ravel()


def mapper_check(directory, rows):
    bridge = directory / "mapper.c"
    bridge.write_text('#include "dvbs2_codes.h"\n'
                      'const dvbs2_code_t *test_code(unsigned i){return &DVBS2_CODES[i];}\n')
    library = directory / "mapper.so"
    subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                    *fwsrc.INCLUDES, *(str(fwsrc.path(name)) for name in
                    ("iq4hist.c", "dvbs2.c", "dvbs2_8psk.c", "dvbs2_qpsk8.c", "iqgen.c")),
                    str(bridge), "-o", str(library)], check=True)
    lib = ct.CDLL(str(library))
    lib.test_code.argtypes = [ct.c_uint]; lib.test_code.restype = ct.c_void_p
    lib.dvbs2_8psk_arena_words.argtypes = [ct.c_void_p, ct.c_bool]
    lib.dvbs2_8psk_arena_words.restype = ct.c_uint32
    lib.dvbs2_8psk_init.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_bool, ct.c_uint32, PTR, ct.c_uint32]
    lib.dvbs2_8psk_init.restype = ct.c_bool
    lib.dvbs2_8psk_frame.argtypes = [ct.c_void_p, PTR, ct.POINTER(mapper.Stream)]
    lib.dvbs2_qpsk8_append.argtypes = [ct.POINTER(mapper.Stream), PTR, ct.c_uint32, ct.c_uint32, ct.c_uint]
    lib.dvbs2_qpsk8_append.restype = ct.c_bool
    lib.iq4hist_pack_group.argtypes = [ct.POINTER(mapper.Input), PTR, PTR]
    lib.iq4hist_coefficients.restype = ct.POINTER(ct.c_int32)
    np.testing.assert_array_equal(np.ctypeslib.as_array(lib.iq4hist_coefficients(), shape=(28,)),
                                  L7_COEFFICIENTS)
    memory = (mapper.Input * 10000)()
    stream = mapper.Stream(out=memory, mask=0xffffffff)
    rng = np.random.default_rng(20261015)
    points, boundaries = [], []
    configs = [("8psk", False, True), ("qpsk", False, True),
               ("8psk", True, False), ("qpsk", True, True)]
    for mode, short, pilots in configs:
        fec = rng.integers(0, 2, 16200 if short else 64800, dtype=np.uint8)
        if mode == "8psk":
            code = lib.test_code(8 + (11 if short else 0))
            count = lib.dvbs2_8psk_arena_words(code, pilots)
            arena = (ct.c_uint32 * count)(); cfg = (ct.c_uint64 * 32)()
            assert lib.dvbs2_8psk_init(cfg, code, pilots, 0, arena, count)
            words = mapper.frame_words(fec)
            lib.dvbs2_8psk_frame(cfg, words.ctypes.data_as(PTR), ct.byref(stream))
            expected = mapper.scalar_frame(fec, "5/6", short, pilots)
        else:
            i, q = mapper.ref.plframe(fec, "5/6", short=short, pilots=pilots)
            expected = mapper.ref.symbols(i, q) * np.exp(1j * np.pi / 8)
            count, tail = divmod(len(i), 16)
            data = []
            for start in range(0, len(i), 16):
                data.append(sum(int(x) << j for j, x in enumerate(i[start:start + 16])) |
                            sum(int(x) << (j + 16) for j, x in enumerate(q[start:start + 16])))
            words = np.asarray(data, dtype=np.uint32)
            assert lib.dvbs2_qpsk8_append(ct.byref(stream), words.ctypes.data_as(PTR), count,
                                          int(words[-1]) if tail else 0, tail)
        points.append(expected)
        boundaries.append(sum(len(p) for p in points))
        assert stream.n * 16 + stream.fill == boundaries[-1]
    points = np.concatenate(points)
    codes = level_codes(points[:stream.n * 16])
    converted = np.empty((stream.n, 2), np.uint32)
    for group in range(stream.n):
        lib.iq4hist_pack_group(ct.byref(memory[group]), converted[group:].ctypes.data_as(PTR),
                              ct.cast(converted[group:].ctypes.data + 4, PTR))
    shifts = 2 * np.arange(16, dtype=np.uint32)
    want = (codes.reshape(2, -1, 16).astype(np.uint32) << shifts).sum(axis=-1, dtype=np.uint32).T
    np.testing.assert_array_equal(converted, want)
    scalar = scalar_waveform(codes)
    for block in (1, 7, 16, 257, codes.shape[1]):
        np.testing.assert_array_equal(waveform(codes, rows, block), scalar)
    return dict(frames=len(configs), frame_boundary_symbols=boundaries,
                partial_fills=[b % 16 for b in boundaries], checked_symbols=codes.shape[1],
                waveform_crc=zlib.crc32(scalar.tobytes()))


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
    linear = np.zeros((2, L * N))
    for axis, weight in enumerate(((np.cos(np.pi / 8) + np.sin(np.pi / 8)) / 2,
                                   (np.cos(np.pi / 8) - np.sin(np.pi / 8)) / 2)):
        linear[axis] = weight * L7_COEFFICIENTS / 256
    reference = receiver_audit(linear)
    selected = json.loads((ROOT / "results/pulse-design/2026-10-15/selected.json").read_text())
    frozen = next(p for p in selected["profiles"] if p["id"] == L7_PROFILE)
    for model, key in (("digital", "digital"), ("held", "with_dac_hold")):
        assert abs(reference[model]["isi_plus_nonlinear_distortion_percent"] -
                   frozen[key]["isi"]["isi_percent"]) < 1e-7
    if require_held:
        assert result["held"]["continuous_certificate"]
    assert not result["digital"]["continuous_certificate"]
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spectrum", action="store_true")
    args = parser.parse_args()
    selected = json.loads((ROOT / "results/pulse-design/2026-10-15/selected.json").read_text())
    profile = next(p for p in selected["profiles"] if p["id"] == L7_PROFILE)
    np.testing.assert_array_equal(profile["coefficients_q8"], L7_COEFFICIENTS)
    assert hashlib.sha256(L7_COEFFICIENTS.astype("<i4").tobytes()).hexdigest() == profile["coefficient_bytes_sha256"]
    with tempfile.TemporaryDirectory(prefix="iq4hist-native-") as temp:
        directory = Path(temp); exe = directory / "iq4hist_test"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", *fwsrc.INCLUDES,
                        str(SRC / "output/iq4hist.c"), str(SRC / "cpu_shaping/iq8.c"),
                        str(ROOT / "host/native/iq4hist_test.c"), "-o", str(exe)], check=True)
        rows = np.frombuffer(subprocess.check_output([str(exe)]), dtype="<u4").reshape(ROWS, ROW_WORDS)
        np.testing.assert_array_equal(rows, scalar_rows())
        np.testing.assert_array_equal(rows, iq4hist_rows())
        assert rows.nbytes == 131072 and not np.any(rows & np.uint32(0xc000c000))
        samples = unpack_rows(rows)
        levels = np.array([np.cos(np.pi / 8), -np.sin(np.pi / 8),
                           np.sin(np.pi / 8), -np.cos(np.pi / 8)])
        keys = np.arange(ROWS, dtype=np.int64)[:, None]
        codes = (keys >> (2 * np.arange(L))) & 3
        ideal = np.einsum("ra,ap->rp", levels[codes], L7_COEFFICIENTS.reshape(L, N) / 256)
        error = float(abs(samples - ideal).max())
        phase_l1 = abs(L7_COEFFICIENTS.reshape(L, N)).sum(axis=0) / 256
        bound = .5 + float(phase_l1.max()) / 2 ** 31
        assert error <= bound and abs(samples).max() < 8192
        for seed in (0, 1, 0x1234abcd, 0xffffffff):
            groups = 137
            actual = np.frombuffer(subprocess.check_output([str(exe), "pack", str(seed), str(groups)]),
                                   dtype="<u4").reshape(groups, 2)
            codes = iq4hist_prbs(seed, groups * 16)
            expected = (codes.reshape(2, groups, 16).astype(np.uint32) <<
                        (2 * np.arange(16, dtype=np.uint32))).sum(axis=-1, dtype=np.uint32).T
            np.testing.assert_array_equal(actual, expected)
            for block in (1, 7, 16, 257):
                np.testing.assert_array_equal(waveform(codes, rows, block), scalar_waveform(codes))
        mappings = mapper_check(directory, rows)
    result = dict(profile=L7_PROFILE, rows=ROWS, scalar_samples=ROWS * N, table_bytes=rows.nbytes,
                  table_crc=zlib.crc32(rows.tobytes()), exact_axis_peak=int(abs(samples).max()),
                  max_axis_error_lsb=error, frozen_tap_error_bound_lsb=bound,
                  level_q30=list(map(int, FOUR_LEVELS_Q30)), continuous_mapping=mappings)
    if args.spectrum:
        result["exact_iid_four_level_psd"] = ensemble_spectrum(samples)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
