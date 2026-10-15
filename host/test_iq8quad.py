"""Independent native checks for the isolated two-SPS, four-symbol 8PSK shaper.

Checks both complete signed tables, pre-emphasis coefficients, two-plane PRBS
packing, every interpolation window, all-history headroom and partitioned output
against direct integer and floating complex convolution. Does not execute Arm,
access a Pico, establish DMA throughput or qualify analog reconstruction.
"""
from __future__ import annotations
import ctypes
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zlib
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference"))
import iqlut as m

L, N, K, ROWS, GROUPS = 9, 2, 4, 4096, 4096
AQ30, BQ30 = 701455651, 290552444
PTR = ctypes.POINTER(ctypes.c_uint32)
CANARY = 0x9ad4c781

class Input(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint32)]

class Tables(ctypes.Structure):
    _fields_ = [("a", PTR), ("b", PTR)]

def coefficients():
    taps = m.design_taps(.20, N, L, 1.16)
    taps = np.convolve(taps, [-.0455, 1.091, -.0455])[1:-1]
    scale = 8191 * 10 ** (-1 / 20) / abs(taps.reshape(L, N)).sum(axis=0).max()
    taps *= scale
    return taps, np.round(taps * 256).astype(np.int64)

def input_reference(seed):
    phase = m.xorshift32(seed, GROUPS * 16) & 7
    points = np.exp(1j * (np.pi / 8 + phase * np.pi / 4))
    bits = []
    for coordinate in (points.real, points.imag):
        sign = (coordinate < 0).astype(np.uint32)
        bits.extend([sign, sign ^ (abs(coordinate) < .7).astype(np.uint32)])
    shift = np.arange(16, dtype=np.uint32)
    packed = np.zeros((GROUPS, 2), np.uint32)
    for p, plane in enumerate(bits):
        values = (plane.reshape(-1, 16) << shift).sum(axis=1, dtype=np.uint32)
        packed[:, p % 2] |= values << (16 * (p // 2))
    return points, bits, packed

def direct_reference(bits, taps, coef):
    results = []
    before_bits = [0, 0, 0, 1]
    for ax in range(2):
        y = np.zeros(len(bits[0]) * N, np.int64)
        for p in (2 * ax, 2 * ax + 1):
            cp = (coef * (AQ30 if p % 2 == 0 else BQ30) + (1 << 29)) >> 30
            s = 1 - 2 * np.concatenate([np.full(L - 1, before_bits[p]), bits[p]]).astype(np.int64)
            win = np.lib.stride_tricks.sliding_window_view(s, L)[:, ::-1]
            y += ((win @ cp.reshape(L, N) + 128) >> 8).ravel()
        results.append(y)
    # PAIRS signed16 layout, with neither masking nor IQ flag.
    i, q = [v.astype(np.uint16).astype(np.uint32) for v in results]
    words = np.stack([i[::2] | i[1::2] << 16,
                      q[::2] | q[1::2] << 16], axis=1).ravel()
    complex_before = np.exp(1j * np.pi / 8)
    a = (np.cos(np.pi / 8) + np.sin(np.pi / 8)) / 2
    b = (np.cos(np.pi / 8) - np.sin(np.pi / 8)) / 2
    syms = a * (1 - 2 * bits[0].astype(int)) + b * (1 - 2 * bits[1].astype(int))
    syms = syms + 1j * (a * (1 - 2 * bits[2].astype(int)) + b * (1 - 2 * bits[3].astype(int)))
    impulses = np.zeros((len(syms) + L - 1) * N, complex)
    impulses[::N] = np.concatenate([np.full(L - 1, complex_before), syms])
    direct = np.convolve(impulses, taps)[(L - 1) * N:][:len(syms) * N]
    error = results[0] + 1j * results[1] - direct
    max_error = float(max(abs(error.real).max(), abs(error.imag).max()))
    bound = 1 + 3 * L / 512
    assert max_error <= bound, (max_error, bound)
    return words, results, max_error

def check_feedback(packed):
    last = np.array([0, 0xffff0000], np.uint32)
    comparisons = 0
    for cur in packed:
        for p in range(2):
            for ax in range(2):
                window = ((int(last[p]) & 0xffff) | (int(cur[p]) << 16)) & 0xffffffff if ax == 0 else \
                         (int(last[p]) >> 16) | (int(cur[p]) & 0xffff0000)
                initial = window
                for j in range(4):
                    rotated = ((window >> 4) | (window << 28)) & 0xffffffff
                    assert (rotated & 0xfff0) == (((initial >> (8 + 4 * j)) & 0xfff) << 4)
                    window = rotated
                    comparisons += 1
        last = cur
    return comparisons

def main():
    taps, coef = coefficients()
    with tempfile.TemporaryDirectory(prefix="iq8quad-native-") as temp:
        library = Path(temp) / "iq8quad.so"
        source = ROOT / "firmware/src"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        f"-I{source}", str(source / "iq8quad.c"), str(source / "iq8.c"), "-o", str(library)], check=True)
        lib = ctypes.CDLL(str(library))
        lib.iq8quad_coefficients.restype = ctypes.POINTER(ctypes.c_int32)
        np.testing.assert_array_equal(np.ctypeslib.as_array(lib.iq8quad_coefficients(), shape=(L*N,)), coef)
        lib.iq8quad_build.argtypes = [ctypes.POINTER(Tables)]
        lib.iq8quad_kernel_c.argtypes = [PTR, ctypes.POINTER(Input), ctypes.c_uint32,
                                        ctypes.POINTER(Input), ctypes.POINTER(Tables)]
        lib.iq8_fill.argtypes = [ctypes.POINTER(Input), ctypes.c_uint32, PTR]
        arrays = [(ctypes.c_uint32 * (ROWS * K + 2))() for _ in range(2)]
        for a in arrays: a[0] = a[-1] = CANARY
        tables = Tables(*(ctypes.cast(ctypes.byref(a, 4), PTR) for a in arrays))
        lib.iq8quad_build(ctypes.byref(tables))
        table_crcs, bounds = [], []
        key = np.arange(ROWS, dtype=np.int64)[:, None]
        for plane, a in enumerate(arrays):
            assert a[0] == a[-1] == CANARY
            cp = (coef * (AQ30 if plane == 0 else BQ30) + (1 << 29)) >> 30
            expect = []
            for j in range(K):
                signs = 1 - 2 * ((key >> (L - 1 + j - np.arange(L))) & 1)
                y = ((signs @ cp.reshape(L, N) + 128) >> 8).astype(np.uint16).astype(np.uint32)
                expect.append(y[:, 0] | y[:, 1] << 16)
            want = np.stack(expect, axis=1)
            got = np.ctypeslib.as_array(a)[1:-1].reshape(ROWS, K)
            np.testing.assert_array_equal(got, want)
            table_crcs.append(zlib.crc32(got.tobytes()))
            bounds.append(abs(cp.reshape(L,N)).sum(axis=0) / 256 + .5)
        assert table_crcs == [0xcd2d4635, 0x135bd352]
        peak_bound = float(np.sum(bounds, axis=0).max())
        assert peak_bound < 8192 and max(float(x.max()) for x in bounds) < 32768
        records = []
        for seed in (0x1234abcd, 1, 0xffffffff):
            points, bits, data = input_reference(seed)
            fill = (Input * GROUPS)(); state = ctypes.c_uint32(seed)
            lib.iq8_fill(fill, GROUPS, ctypes.byref(state))
            np.testing.assert_array_equal(np.ctypeslib.as_array(ctypes.cast(fill, PTR), shape=(GROUPS * 2,)), data.ravel())
            expected, axes, error = direct_reference(bits, taps, coef)
            for block in (1, 7, 64, GROUPS):
                result = (ctypes.c_uint32 * (len(expected) + 2))();result[0] = result[-1] = CANARY
                for start in range(0, GROUPS, block):
                    count = min(block, GROUPS - start)
                    inp = ctypes.cast(ctypes.byref(fill, start * 8), ctypes.POINTER(Input))
                    prev = ctypes.cast(ctypes.byref(fill, (start - 1) * 8), ctypes.POINTER(Input)) if start else None
                    out = ctypes.cast(ctypes.byref(result, 4 + start * 32 * 4), PTR)
                    lib.iq8quad_kernel_c(out, inp, count, prev, ctypes.byref(tables))
                lib.iq8quad_kernel_c(ctypes.cast(ctypes.byref(result, 4), PTR), None, 0, None, ctypes.byref(tables))
                assert result[0] == result[-1] == CANARY
                np.testing.assert_array_equal(np.ctypeslib.as_array(result)[1:-1], expected)
            packed = (axes[0] & 0x3fff) | (axes[1] & 0x3fff) << 16
            metadata = dict(seed=seed, symbols=GROUPS*16, packed_no_flag_crc=zlib.crc32(packed.astype('<u4').tobytes()),
                            raw_pairs_crc=zlib.crc32(expected.tobytes()),
                            i_crc=zlib.crc32(axes[0].astype('<i2').tobytes()),
                            q_crc=zlib.crc32(axes[1].astype('<i2').tobytes()), max_axis_error_lsb=error)
            if seed == 0x1234abcd:
                assert metadata['packed_no_flag_crc'] == 3803865361
                assert metadata['i_crc'] == 3417247612 and metadata['q_crc'] == 3777218023
            records.append(metadata)
        feedback = check_feedback(data)
    print(json.dumps(dict(profile="iq8quad", L=L, sps=N, symbols_per_row=K,
                          bytes_per_table=ROWS*K*4, table_crcs=table_crcs,
                          all_history_axis_peak_bound_lsb=peak_bound,
                          feedback_windows=feedback, fixtures=records)))

if __name__ == "__main__":
    main()
