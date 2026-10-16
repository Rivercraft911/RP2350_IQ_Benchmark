"""Independent QPSK symbol-history and waveform checks for descriptor-only kernels.

Builds in a temporary directory. Descriptor addresses are numerical uint32 values, never
host pointers. Resolving their offsets into model LUTs must reproduce the existing waveform.
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
import fwsrc  # noqa: E402  firmware/src/<area>/ layout

WORDS, L, N = 137, 12, 4


def descriptors(words: np.ndarray, base_i: int, base_q: int) -> tuple[np.ndarray, np.ndarray]:
    # Advance one symbol at a time, independently of the C/ASM's 32-bit window extraction.
    outputs = []
    for bits, base in zip(m.unpack_bits(words), (base_i, base_q)):
        history = 0
        addresses = []
        for bit in bits:
            history = (history >> 1) | (int(bit) << (L - 1))
            addresses.append((base + history * N * 2) & 0xffffffff)
        outputs.append(np.asarray(addresses, dtype="<u4"))
    return outputs[0], outputs[1]


def pair_descriptors(words: np.ndarray, base_i: int, base_q: int):
    outputs = []
    for bits, base in zip(m.unpack_bits(words), (base_i, base_q)):
        history, first = 0, 0
        addresses = []
        for pos, bit in enumerate(bits):
            history = (history >> 1) | (int(bit) << (L - 1))
            if not pos % 2:
                first = history
            else:
                key = first | int(bit) << L
                assert (key >> 1) == history
                addresses.append((base + key * 16) & 0xffffffff)
        outputs.append(np.asarray(addresses, dtype="<u4"))
    return outputs[0], outputs[1]


def feedback_model(words: np.ndarray, base_i: int, base_q: int, paired=False):
    # Model the optional RP2350 POP-feedback configuration, not host MMIO execution.
    # A POP returns the lane-0 address while lane 1 advances its original full32 window.
    def ror(x, n):
        return ((x >> n) | (x << (32 - n))) & 0xffffffff
    prev = 0
    outputs = [[], []]
    for word in words:
        cur = int(word)
        windows = [(prev & 0xffff) | ((cur << 16) & 0xffffffff),
                   (prev >> 16) | (cur & 0xffff0000)]
        for axis, base in enumerate((base_i, base_q)):
            window = windows[axis]
            for _ in range(8 if paired else 16):
                address = ror(window, 1) & 0x1fff0 if paired else ror(window, 2) & 0x7ff8
                outputs[axis].append((base + address) & 0xffffffff)
                window = ror(window, 2 if paired else 1)
        prev = cur
    return tuple(np.asarray(out, dtype="<u4") for out in outputs)


def waveform(di: np.ndarray, dq: np.ndarray, base_i: int, base_q: int, lut: m.Lut):
    ii = ((di.astype(np.uint64) - np.uint64(base_i)) & np.uint64(0xffffffff)) // 8
    qi = ((dq.astype(np.uint64) - np.uint64(base_q)) & np.uint64(0xffffffff)) // 8
    assert ii.max() < 1 << L and qi.max() < 1 << L
    i, q = lut.words(True)[ii], lut.words(False)[qi]
    return np.stack([i[:, 0], q[:, 0], i[:, 1], q[:, 1]], axis=1).ravel()


def pair_waveform(di: np.ndarray, dq: np.ndarray, base_i: int, base_q: int, lut: m.Lut):
    ii = ((di.astype(np.uint64) - np.uint64(base_i)) & np.uint64(0xffffffff)) // 16
    qi = ((dq.astype(np.uint64) - np.uint64(base_q)) & np.uint64(0xffffffff)) // 16
    assert ii.max() < 1 << (L + 1) and qi.max() < 1 << (L + 1)
    keys = np.arange(1 << (L + 1))
    raw = lut.words(False)
    paired = np.concatenate([raw[keys & ((1 << L) - 1)], raw[keys >> 1]], axis=1)
    i, q = paired[ii] | np.uint32(0x40004000), paired[qi]
    return np.stack([i, q], axis=-1).ravel()


def main():
    lut = m.Lut(g.ALPHA, N, L, g.KAISER_BETA, g.HEADROOM_DB)
    with tempfile.TemporaryDirectory(prefix="iqdesc-native-") as build:
        exe = Path(build) / "iqdesc_test"
        src = ROOT / "firmware/src"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", *fwsrc.INCLUDES,
                        str(src / "output/iqdesc.c"), str(ROOT / "host/native/iqdesc_test.c"),
                        "-o", str(exe)], check=True)
        for seed in (0, 1, 0x1234abcd, 0xffffffff):
            words = m.xorshift32(seed, WORDS)
            expected_wave = m.to_layout(m.generate(words, lut), m.LAYOUT_PAIRS)
            for bi, bq in ((0x20040000, 0x20048000), (0xfffffff8, 0x10)):
                di, dq = descriptors(words, bi, bq)
                pi, pq = feedback_model(words, bi, bq)
                np.testing.assert_array_equal(pi, di)
                np.testing.assert_array_equal(pq, dq)
                for block in (1, 7, 64, WORDS):
                    got = np.frombuffer(subprocess.check_output(
                        [str(exe), str(seed), str(block), str(bi), str(bq)]), dtype="<u4")
                    gi, gq = got.reshape(2, WORDS * 16)
                    np.testing.assert_array_equal(gi, di)
                    np.testing.assert_array_equal(gq, dq)
                    np.testing.assert_array_equal(waveform(gi, gq, bi, bq, lut), expected_wave)
                    print(json.dumps(dict(seed=seed, block=block, base_i=bi, base_q=bq,
                                          crc_i=zlib.crc32(gi.tobytes()), crc_q=zlib.crc32(gq.tobytes()),
                                          waveform_crc=zlib.crc32(expected_wave.tobytes()))))
            for bi, bq in ((0x20040000, 0x20048000), (0xfffffff8, 0x10), (0x20040000, 0x20040000)):
                di, dq = pair_descriptors(words, bi, bq)
                pi, pq = feedback_model(words, bi, bq, paired=True)
                np.testing.assert_array_equal(pi, di)
                np.testing.assert_array_equal(pq, dq)
                for block in (1, 7, 64, WORDS):
                    got = np.frombuffer(subprocess.check_output(
                        [str(exe), str(seed), str(block), str(bi), str(bq), "1"]), dtype="<u4")
                    gi, gq = got.reshape(2, WORDS * 8)
                    np.testing.assert_array_equal(gi, di)
                    np.testing.assert_array_equal(gq, dq)
                    np.testing.assert_array_equal(pair_waveform(gi, gq, bi, bq, lut), expected_wave)
                    print(json.dumps(dict(paired=True, seed=seed, block=block, base_i=bi, base_q=bq,
                                          crc_i=zlib.crc32(gi.tobytes()), crc_q=zlib.crc32(gq.tobytes()),
                                          waveform_crc=zlib.crc32(expected_wave.tobytes()))))


if __name__ == "__main__":
    main()
