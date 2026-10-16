"""Native cyclic four-chain pointers against mathematical PRBS and scalar FIR.

Every descriptor is checked independently of the C history update. Reconstructed
even/odd row streams cover repeated wraps and both8/14-bit sample formats. PIO
slot timing and DMA arbitration belong to the separate writer model/device gate.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import zlib

import numpy as np

from pulse_profiles import FOUR_LEVELS_Q30, L7_COEFFICIENTS, iq4hist_prbs

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware/src"
L, SPS, ROWS = 7, 4, 4 ** 7


def cyclic_scalar(codes, bits):
    """Direct cyclic convolution; no lookup, row address, or rolling history."""
    denominator = 3399 << 32 if bits == 8 else 1 << 38
    expected = np.empty((2, codes.shape[1], SPS), dtype=np.int64)
    for axis in range(2):
        for symbol in range(codes.shape[1]):
            for phase in range(SPS):
                total = sum(int(L7_COEFFICIENTS[age * SPS + phase]) *
                            int(FOUR_LEVELS_Q30[codes[axis, (symbol - age) % codes.shape[1]]])
                            for age in range(L))
                expected[axis, symbol, phase] = (total + denominator // 2) // denominator
    return expected


def decode_rows(rows, bits):
    if bits == 8:
        samples = (rows[..., None] >> (8 * np.arange(SPS, dtype=np.uint32))) & 255
    else:
        samples = np.stack([rows & 0x3fff, rows >> 16], axis=-1).reshape(*rows.shape[:-1], SPS)
    return (samples.astype(np.int64) ^ (1 << (bits - 1))) - (1 << (bits - 1))


def check_case(executable, table, seed, axis_words, base, bits):
    row_words = 1 if bits == 8 else 2
    raw = np.frombuffer(subprocess.check_output(
        [str(executable), str(seed), str(axis_words), hex(base), str(row_words)]), dtype="<u4")
    ring_words, symbols = axis_words * 8, axis_words * 16
    assert len(raw) == 2 * axis_words + 4 * ring_words
    packed = raw[:2 * axis_words].reshape(2, axis_words)
    rings = raw[2 * axis_words:].reshape(4, ring_words)
    codes = iq4hist_prbs(seed, symbols)
    source_expected = np.array([[sum(int(codes[axis, word * 16 + symbol]) << (2 * symbol)
                                    for symbol in range(16)) for word in range(axis_words)]
                                for axis in range(2)], dtype="<u4")
    np.testing.assert_array_equal(packed, source_expected)
    expected_address = np.empty((2, symbols), dtype="<u4")
    for axis in range(2):
        for symbol in range(symbols):
            key = sum(int(codes[axis, (symbol - age) % symbols]) * 4 ** age for age in range(L))
            expected_address[axis, symbol] = base + key * row_words * 4
    got_address = np.empty_like(expected_address)
    for parity in range(2):
        for axis in range(2):
            np.testing.assert_array_equal(rings[2 * parity + axis], expected_address[axis, parity::2])
            got_address[axis, parity::2] = rings[2 * parity + axis]
    scalar = cyclic_scalar(codes, bits)
    for lap in range(3):
        # A chain advances once every two symbols, wrapping independently.
        n = np.arange(lap * symbols, (lap + 1) * symbols)
        address = np.stack([rings[2 * (n & 1) + axis, (n // 2) % ring_words] for axis in range(2)])
        np.testing.assert_array_equal(address, got_address)
        key = ((address.astype(np.uint64) - base) // (4 * row_words)).astype(np.int64)
        assert np.all(key < ROWS)
        np.testing.assert_array_equal(decode_rows(table[key], bits), scalar)
    # Interleave each symbol's four I/Q sample slots, including IQ_FLAG. The
    # writer timing model separately proves which writer owns each such slot.
    bus = ((scalar & 255) << 6) if bits == 8 else scalar & 0x3fff
    bus[0] |= 0x4000
    slots = np.stack([bus[0], bus[1]], axis=-1).reshape(-1)
    np.testing.assert_array_equal((slots >> 14) & 1, 1 - np.arange(len(slots)) % 2)
    assert np.all((slots & 0x8000) == 0)
    if bits == 8:
        assert np.all((slots & 63) == 0)
    return dict(seed=seed, axis_words=axis_words, symbols=symbols, bits=bits,
                table_base=base, source_crc=zlib.crc32(packed.tobytes()),
                pointer_crc=zlib.crc32(rings.tobytes()),
                slot_crc=zlib.crc32(slots.astype("<u2").tobytes()))


def main():
    fixtures = []
    with tempfile.TemporaryDirectory(prefix="iq4quad-native-") as temporary:
        executable = Path(temporary) / "iq4quad_test"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{SRC}",
                        *(str(SRC / name) for name in ("iq4hist.c", "iq8.c", "iq4quad.c")),
                        str(ROOT / "host/native/iq4quad_test.c"), "-o", str(executable)], check=True)
        for bits in (8, 14):
            table = np.frombuffer(subprocess.check_output([str(executable), f"table{bits}"]), dtype="<u4")
            if bits == 14:
                table = table.reshape(ROWS, 2)
            assert zlib.crc32(table.tobytes()) == (0x5e13245c if bits == 8 else 0x19bf3c1d)
            for axis_words in (1, 2, 16, 256):
                for seed in (0, 1, 0x1234abcd, 0xffffffff):
                    for base in (0x20000000, 0x20040004):
                        case = check_case(executable, table, seed, axis_words, base, bits)
                        if axis_words == 256 and seed == 0x1234abcd and base == 0x20000000:
                            fixtures.append(case)
    print(json.dumps(dict(cases=64, cyclic_laps=3, invalid_calls_per_case=22,
                          maximum_symbols=4096, stream_order=["I-even", "Q-even", "I-odd", "Q-odd"],
                          fixtures=fixtures)))


if __name__ == "__main__":
    main()
