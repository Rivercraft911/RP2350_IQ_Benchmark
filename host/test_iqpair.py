"""Check every paired-LUT row against chronological integer FIR, then stream its samples.

The native executable runs the actual firmware builder. The reference computes both
symbols from their individual input bits and coefficients, without copying an old LUT row.
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


def fir_rows(lut: m.Lut) -> np.ndarray:
    keys = np.arange(8192, dtype=np.int64)[:, None]
    ages = np.arange(12)[None, :]
    symbols = []
    for newest_bit in (11, 12):
        signs = 1 - 2 * ((keys >> (newest_bit - ages)) & 1)
        acc = signs @ lut.coef.reshape(12, 4)
        slots = (((acc + (1 << (m.FRAC_BITS - 1))) >> m.FRAC_BITS) & 0x3fff).astype("<u4")
        symbols.append(slots[:, 0::2] | (slots[:, 1::2] << 16))
    return np.concatenate(symbols, axis=1)


def stream_axis(bits: np.ndarray, rows: np.ndarray) -> np.ndarray:
    history = 0
    outputs = []
    for n, bit in enumerate(bits):
        history = (history >> 1) | (int(bit) << 12)
        if n & 1:
            outputs.append(rows[history])
    return np.asarray(outputs, dtype="<u4").ravel()


def main():
    lut = m.Lut(g.ALPHA, 4, 12, g.KAISER_BETA, g.HEADROOM_DB)
    with tempfile.TemporaryDirectory(prefix="iqpair-native-") as build:
        exe = Path(build) / "iqpair_test"
        src = ROOT / "firmware/src"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{src}",
                        str(src / "iqgen.c"), str(src / "iqpair.c"),
                        str(ROOT / "host/native/iqpair_test.c"), "-o", str(exe)], check=True)
        rows = np.frombuffer(subprocess.check_output([str(exe)]), dtype="<u4").reshape(8192, 4)
    np.testing.assert_array_equal(rows, fir_rows(lut))
    assert not np.any(rows & np.uint32(0xc000c000)), "shared table must contain sample bits only"
    for seed in (0, 1, 0x1234abcd, 0xffffffff):
        words = m.xorshift32(seed, 137)
        i, q = (stream_axis(bits, rows) for bits in m.unpack_bits(words))
        paired = np.stack([i | np.uint32(0x40004000), q], axis=1).ravel()
        expected = m.to_layout(m.generate(words, lut), m.LAYOUT_PAIRS)
        np.testing.assert_array_equal(paired, expected)
        print(json.dumps(dict(seed=seed, symbols=len(words) * 16,
                              waveform_crc=zlib.crc32(paired.tobytes()))))
    print(json.dumps(dict(rows=len(rows), bytes=rows.nbytes,
                          paired_crc=zlib.crc32(rows.tobytes()))))


if __name__ == "__main__":
    main()
