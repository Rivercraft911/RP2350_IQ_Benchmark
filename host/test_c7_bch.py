"""C7 native API checks against independent BCH and transpose references.

This executes portable fallback functions, not Thumb instructions or M33 timing.
The assembly semantics companion checks the actual copied Thumb leaf source.
"""
from __future__ import annotations

import ctypes as ct
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference/dvbs2"))
import dvbs2 as ref  # noqa: E402

MASK = 0xFFFFFFFF
GUARD = 0xC790A53D
FE_WORDS = 64800 // 32 + 2
U32 = ct.c_uint32
PTR = ct.POINTER(U32)


def guarded(values):
    """A word-aligned exact-size payload with one canary at either end."""
    storage = (U32 * (len(values) + 2))(GUARD, *values, GUARD)
    return storage, ct.cast(ct.byref(storage, 4), PTR)


def polynomial_step(words, parity, generator, degree):
    state = int.from_bytes(b"".join(int(x).to_bytes(4, "big") for x in parity[:degree // 32]), "big")
    mask = (1 << degree) - 1
    for word in words:
        for bit in range(31, -1, -1):
            feedback = (state >> (degree - 1)) ^ ((int(word) >> bit) & 1)
            state = ((state << 1) & mask) ^ (generator & mask if feedback else 0)
    return [(state >> shift) & MASK for shift in range(degree - 32, -1, -32)] + [0] * (6 - degree // 32)


def pack_bits(bits, length):
    padded = np.pad(np.asarray(bits, dtype=np.uint8), (0, (-len(bits)) % 32))
    words = np.packbits(padded).view(">u4").astype(np.uint32).tolist()
    return words + [0] * (length - len(words))


class C7BCH(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="c7-bch-native-")
        library = Path(cls.temporary.name) / "c7.so"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        f"-I{ROOT / 'firmware/src'}", str(ROOT / "host/native/c7_bch_test.c"),
                        "-o", str(library)], check=True)
        cls.lib = ct.CDLL(str(library))
        cls.lib.c7_setup.argtypes = [ct.c_uint, PTR, PTR]
        cls.lib.c7_setup.restype = ct.c_uint
        cls.lib.c7_leaf.argtypes = [ct.c_uint, PTR, PTR, ct.c_void_p, U32]
        cls.lib.c7_leaf.restype = ct.c_int
        cls.lib.c7_frontend.argtypes = [PTR, PTR, PTR, ct.c_uint, ct.c_uint, ct.c_void_p, U32, ct.c_uint]
        cls.lib.c7_frontend.restype = ct.c_int
        cls.lib.c7_frontend_pair.argtypes = [PTR, PTR, PTR, PTR, ct.c_void_p]
        cls.lib.c7_frontend_pair.restype = ct.c_int
        cls.lib.c7_serial.argtypes = [PTR, PTR]
        cls.lib.c7_transpose.argtypes = [PTR]
        cls.lib.c7_transpose.restype = ct.c_int
        cls.lib.c7_rejects_without_writes.argtypes = [ct.c_uint]
        cls.lib.c7_rejects_without_writes.restype = ct.c_int
        cls.lib.dvbs2_bch_code_bytes.argtypes = [ct.c_uint]
        cls.lib.dvbs2_bch_code_bytes.restype = U32
        cls.lib.dvbs2_bch_copy.argtypes = [ct.c_void_p, U32, ct.c_uint]
        cls.lib.dvbs2_bch_copy.restype = ct.c_void_p
        cls.lib.dvbs2_bch_table3_bytes.restype = U32
        cls.lib.dvbs2_bch_table3_init.argtypes = [ct.c_void_p, U32]
        cls.lib.dvbs2_bch_table3_init.restype = ct.c_bool
        cls.lib.dvbs2_transpose_code_bytes.restype = U32
        cls.lib.dvbs2_transpose_copy.argtypes = [ct.c_void_p, U32]
        cls.lib.dvbs2_transpose_copy.restype = ct.c_void_p

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setup_code(self, ci):
        table = (U32 * 8192)()
        generator = (U32 * 6)()
        degree = self.lib.c7_setup(ci, table, generator)
        self.assertIn(degree, (128, 160, 168, 192))
        return degree, table, generator

    def table3(self, degree):
        size = self.lib.dvbs2_bch_table3_bytes()
        self.assertEqual(size, 5120 * degree // 8)
        storage, pointer = guarded([0xA5A5A5A5] * (size // 4))
        self.assertTrue(self.lib.dvbs2_bch_table3_init(pointer, size))
        self.assertEqual(storage[0], GUARD)
        self.assertEqual(storage[-1], GUARD)
        return storage, pointer

    def assert_leaf(self, variant, words, parity, table, want):
        source, source_pointer = guarded(words)
        remainder, remainder_pointer = guarded(parity)
        saved = list(source)
        self.assertEqual(self.lib.c7_leaf(variant, source_pointer, remainder_pointer, table, len(words)), 1)
        self.assertEqual(list(remainder)[1:-1], want)
        self.assertEqual(list(source), saved)
        self.assertEqual(remainder[0], GUARD)
        self.assertEqual(remainder[-1], GUARD)
        return list(remainder)[1:-1]

    def test_native_code_copy_and_failed_admission(self):
        for ci in range(11):
            self.setup_code(ci)
            for variant in range(3):
                self.assertEqual(self.lib.dvbs2_bch_code_bytes(variant), 0)
                self.assertTrue(self.lib.dvbs2_bch_copy(None, 0, variant))
            self.assertEqual(self.lib.dvbs2_bch_code_bytes(3), 0)
            self.assertFalse(self.lib.dvbs2_bch_copy(None, 0, 3))
            self.assertEqual(self.lib.c7_rejects_without_writes(1 << 31), 1)
        self.assertEqual(self.lib.dvbs2_transpose_code_bytes(), 0)
        self.assertTrue(self.lib.dvbs2_transpose_copy(None, 0))
        for ci in range(11, 21):
            degree, _, _ = self.setup_code(ci)
            self.assertEqual(degree, 168)
            for variant in range(3):
                self.assertEqual(self.lib.dvbs2_bch_code_bytes(variant), 0)
                self.assertFalse(self.lib.dvbs2_bch_copy(None, 0, variant))
            self.assertEqual(self.lib.dvbs2_bch_table3_bytes(), 0)
            self.assertFalse(self.lib.dvbs2_bch_table3_init(None, 0))
            self.assertEqual(self.lib.c7_rejects_without_writes(0), 1)

    def test_table3_exact_extent_and_invalid_allocation(self):
        for ci in (5, 6, 9):
            degree, _, _ = self.setup_code(ci)
            size = self.lib.dvbs2_bch_table3_bytes()
            storage, pointer = guarded([0xA5A5A5A5] * (size // 4))
            saved = bytes(storage)
            for address, available in ((None, size), (pointer, size - 1),
                                       (ct.byref(storage, 5), size)):
                self.assertFalse(self.lib.dvbs2_bch_table3_init(address, available))
                self.assertEqual(bytes(storage), saved)
            self.table3(degree)

    def test_arbitrary_remainders_and_uneven_continuations(self):
        rng = random.Random(0xC7BC)
        for ci in (5, 6, 9):
            degree, table, native_generator = self.setup_code(ci)
            generator = ref.bch_generator(False, degree // 16)
            self.assertEqual(list(native_generator)[:degree // 32],
                             [(generator >> shift) & MASK for shift in range(degree - 32, -1, -32)])
            storage, wider = self.table3(degree)
            wider_saved = bytes(storage)
            for variant in range(3):
                selected = wider if variant == 2 else table
                for count in (0, 1, 2, 3, 7, 31, 128, 1345):
                    with self.subTest(degree=degree, variant=variant, count=count):
                        words = [rng.getrandbits(32) for _ in range(count)]
                        parity = [rng.getrandbits(32) for _ in range(degree // 32)] + [0] * (6 - degree // 32)
                        want = polynomial_step(words, parity, generator, degree)
                        self.assert_leaf(variant, words, parity, selected, want)
                        cursor = 0
                        while cursor < count:
                            take = rng.choice((1, 2, 7, 96, 128))
                            piece = words[cursor:cursor + take]
                            parity = self.assert_leaf(variant, piece, parity, selected,
                                                      polynomial_step(piece, parity, generator, degree))
                            cursor += len(piece)
                        self.assertEqual(parity, want)
            self.assertEqual(bytes(storage), wider_saved)

    def test_all_three_slice_indices(self):
        for ci in (5, 6, 9):
            degree, _, _ = self.setup_code(ci)
            _, table = self.table3(degree)
            generator = ref.bch_generator(False, degree // 16)
            for shift, count in ((21, 2048), (10, 2048), (0, 1024)):
                for index in range(count):
                    words = [index << shift]
                    want = polynomial_step(words, [0] * 6, generator, degree)
                    self.assert_leaf(2, words, [0] * 6, table, want)

    def test_frontend_all_normal_codes_and_tail_lengths(self):
        rng = random.Random(0xC7FEC)
        for ci, rate in enumerate(ref.RATES):
            degree, table, _ = self.setup_code(ci)
            _, wider = self.table3(degree)
            code = ref.code(rate)
            for seed in range(2):
                words = [rng.getrandbits(32) for _ in range((code.kbch + 31) // 32)]
                source, source_pointer = guarded(words)
                saved = list(source)
                bits = np.unpackbits(np.asarray(words, dtype=">u4").view(np.uint8))[:code.kbch]
                scrambled = ref.bb_scramble(bits)
                encoded = ref.bch_encode(scrambled, rate)
                want = pack_bits(encoded, FE_WORDS)
                expected_parity = pack_bits(encoded[code.kbch:], 6)
                serial = (U32 * 6)()
                scrambled_words = (U32 * len(words))(*pack_bits(scrambled, len(words)))
                self.lib.c7_serial(scrambled_words, serial)
                self.assertEqual(list(serial), expected_parity)
                for options, variant in ((0, MASK), (4, MASK), (64, MASK), (0, 0), (64, 1), (0, 2)):
                    for prepared in (0, 1):
                        with self.subTest(rate=rate, seed=seed, options=options, variant=variant, prepared=prepared):
                            frame, frame_pointer = guarded([0xA5A5A5A5] * FE_WORDS)
                            parity, parity_pointer = guarded([0] * 6)
                            selected = wider if variant == 2 else table
                            self.assertEqual(self.lib.c7_frontend(source_pointer, frame_pointer, parity_pointer,
                                                                 options, variant, selected,
                                                                 (1, 7, 128, 511)[(ci + seed) % 4], prepared), 1)
                            self.assertEqual(list(frame)[1:-1], want)
                            self.assertEqual(list(parity)[1:-1], expected_parity)
                            self.assertEqual((frame[0], frame[-1], parity[0], parity[-1]), (GUARD,) * 4)
                            self.assertEqual(list(source), saved)

    def test_negative_control_corrupt_wide_table_changes_parity(self):
        degree, _, _ = self.setup_code(5)
        storage, table = self.table3(degree)
        for k in range(1, len(storage) - 1):
            storage[k] = 0
        source = (U32 * 1)(0xD19B73A5)
        parity = (U32 * 6)()
        self.assertEqual(self.lib.c7_leaf(2, source, parity, table, 1), 1)
        want = polynomial_step([source[0]], [0] * 6, ref.bch_generator(False, 10), degree)
        self.assertNotEqual(list(parity), want)
        self.assertEqual((storage[0], storage[-1]), (GUARD, GUARD))

    def test_two_frontends_with_distinct_leaves_and_tables(self):
        rng = random.Random(0xC7D0)
        for ci in (5, 6, 8, 9, 10):
            degree, _, _ = self.setup_code(ci)
            table_storage, table = self.table3(degree)
            rate = ref.RATES[ci]
            code = ref.code(rate)
            sources, frames, wants = [], [], []
            for _ in range(2):
                words = [rng.getrandbits(32) for _ in range((code.kbch + 31) // 32)]
                sources.append(guarded(words))
                frames.append(guarded([0xA5A5A5A5] * FE_WORDS))
                bits = np.unpackbits(np.asarray(words, dtype=">u4").view(np.uint8))[:code.kbch]
                wants.append(pack_bits(ref.bch_encode(ref.bb_scramble(bits), rate), FE_WORDS))
            saved_sources = [list(source) for source, _ in sources]
            self.assertEqual(self.lib.c7_frontend_pair(sources[0][1], sources[1][1],
                                                      frames[0][1], frames[1][1], table), 1)
            for i in range(2):
                self.assertEqual(list(frames[i][0])[1:-1], wants[i])
                self.assertEqual((frames[i][0][0], frames[i][0][-1]), (GUARD, GUARD))
                self.assertEqual(list(sources[i][0]), saved_sources[i])
            self.assertEqual((table_storage[0], table_storage[-1]), (GUARD, GUARD))

    def test_transpose_all_basis_bits_and_dense_matrices(self):
        rng = random.Random(0xC732)
        fixtures = [[(1 << bit) if row == selected else 0 for row in range(32)]
                    for selected in range(32) for bit in range(32)]
        fixtures += [[rng.getrandbits(32) for _ in range(32)] for _ in range(128)]
        for words in fixtures:
            storage, pointer = guarded(words)
            want = [sum(((words[row] >> (31 - column)) & 1) << (31 - row) for row in range(32))
                    for column in range(32)]
            self.assertEqual(self.lib.c7_transpose(pointer), 1)
            self.assertEqual(list(storage)[1:-1], want)
            self.assertEqual((storage[0], storage[-1]), (GUARD, GUARD))


if __name__ == "__main__":
    unittest.main()
