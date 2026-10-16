"""C9 compact BCH table checks against independent polynomial and BB/BCH references.

Portable C runs here. The companion test_bch_fast_asm.py interprets each actual
assembled compact leaf; neither supplies Cortex-M33 timing measurements.
"""
from __future__ import annotations

import ctypes as ct
import random
import unittest

import numpy as np

import test_c7_bch as c7
from test_c7_bch import FE_WORDS, GUARD, U32, guarded, pack_bits, polynomial_step, ref


class C9BCH(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        c7.C7BCH.setUpClass.__func__(cls)
        cls.lib.dvbs2_bch_table4_bytes.restype = U32
        cls.lib.dvbs2_bch_table4_init.argtypes = [ct.c_void_p, U32]
        cls.lib.dvbs2_bch_table4_init.restype = ct.c_bool

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    setup_code = c7.C7BCH.setup_code
    assert_leaf = c7.C7BCH.assert_leaf

    def compact(self, degree):
        size = self.lib.dvbs2_bch_table4_bytes()
        self.assertEqual(size, 1024 * degree // 8)
        storage, pointer = guarded([0xA5A5A5A5] * (size // 4))
        self.assertTrue(self.lib.dvbs2_bch_table4_init(pointer, size))
        self.assertEqual((storage[0], storage[-1]), (GUARD, GUARD))
        return storage, pointer

    def test_table_extent_and_rejection_without_writes(self):
        for ci in (5, 6, 9):
            degree, _, _ = self.setup_code(ci)
            size = self.lib.dvbs2_bch_table4_bytes()
            storage, pointer = guarded([0xA5A5A5A5] * (size // 4))
            before = bytes(storage)
            for address, available in ((None, size), (pointer, size - 1),
                                       (ct.byref(storage, 5), size)):
                self.assertFalse(self.lib.dvbs2_bch_table4_init(address, available))
                self.assertEqual(bytes(storage), before)
            self.compact(degree)
            self.assertTrue(self.lib.dvbs2_bch_copy(None, 0, 3))
        for ci in range(11, 21):
            self.setup_code(ci)
            self.assertEqual(self.lib.dvbs2_bch_table4_bytes(), 0)
            self.assertFalse(self.lib.dvbs2_bch_table4_init(None, 0))
            self.assertFalse(self.lib.dvbs2_bch_copy(None, 0, 3))

    def test_every_compact_row_and_arbitrary_continuations(self):
        rng = random.Random(0xC9BC)
        for ci in (5, 6, 9):
            degree, _, _ = self.setup_code(ci)
            generator = ref.bch_generator(False, degree // 16)
            storage, table = self.compact(degree)
            before = bytes(storage)
            for plane, shift in enumerate((24, 16, 8, 0)):
                for index in range(256):
                    want = polynomial_step([index << shift], [0] * 6, generator, degree)
                    start = (plane * 256 + index) * (degree // 32)
                    self.assertEqual(list(table[start:start + degree // 32]), want[:degree // 32])
                    self.assert_leaf(3, [index << shift], [0] * 6, table, want)
            for count in (0, 1, 2, 7, 128, 1682):
                words = [rng.getrandbits(32) for _ in range(count)]
                parity = [rng.getrandbits(32) for _ in range(degree // 32)] + [0] * (6 - degree // 32)
                want = polynomial_step(words, parity, generator, degree)
                self.assert_leaf(3, words, parity, table, want)
                cursor = 0
                while cursor < count:
                    piece = words[cursor:cursor + rng.choice((1, 7, 31, 128))]
                    parity = self.assert_leaf(3, piece, parity, table,
                                              polynomial_step(piece, parity, generator, degree))
                    cursor += len(piece)
                self.assertEqual(parity, want)
            self.assertEqual(bytes(storage), before)

    def test_all_normal_rate_frontends_and_final_bytes(self):
        rng = random.Random(0xC9FEC)
        for ci, rate in enumerate(ref.RATES):
            degree, _, _ = self.setup_code(ci)
            storage, table = self.compact(degree)
            before = bytes(storage)
            code = ref.code(rate)
            for prepared in (0, 1):
                words = [rng.getrandbits(32) for _ in range((code.kbch + 31) // 32)]
                source, source_pointer = guarded(words)
                saved = bytes(source)
                bits = np.unpackbits(np.asarray(words, dtype=">u4").view(np.uint8))[:code.kbch]
                encoded = ref.bch_encode(ref.bb_scramble(bits), rate)
                frame, frame_pointer = guarded([0xA5A5A5A5] * FE_WORDS)
                parity, parity_pointer = guarded([0] * 6)
                self.assertEqual(self.lib.c7_frontend(source_pointer, frame_pointer, parity_pointer,
                                                      0, 3, table, (7, 128)[prepared], prepared), 1)
                self.assertEqual(list(frame)[1:-1], pack_bits(encoded, FE_WORDS))
                self.assertEqual(list(parity)[1:-1], pack_bits(encoded[code.kbch:], 6))
                self.assertEqual((frame[0], frame[-1], parity[0], parity[-1]), (GUARD,) * 4)
                self.assertEqual(bytes(source), saved)
            self.assertEqual(bytes(storage), before)

    def test_used_table_bit_corruption_changes_parity(self):
        for ci in (5, 6, 9):
            degree, _, _ = self.setup_code(ci)
            storage, table = self.compact(degree)
            generator = ref.bch_generator(False, degree // 16)
            words = [1]
            want = polynomial_step(words, [0] * 6, generator, degree)
            self.assert_leaf(3, words, [0] * 6, table, want)
            table[(768 + 1) * (degree // 32)] ^= 1
            source, source_pointer = guarded(words)
            remainder, remainder_pointer = guarded([0] * 6)
            self.assertEqual(self.lib.c7_leaf(3, source_pointer, remainder_pointer, table, 1), 1)
            self.assertNotEqual(list(remainder)[1:-1], want)
            self.assertEqual((storage[0], storage[-1], source[0], source[-1],
                              remainder[0], remainder[-1]), (GUARD,) * 6)


if __name__ == "__main__":
    unittest.main()
