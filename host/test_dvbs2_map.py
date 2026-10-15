"""Check both actual portable mapper selectors, canonical FEC and complete PL streams.

Native execution proves the packed-carry arithmetic and ring boundaries, including
all partial fills and linear output. Arm instruction scheduling needs device checks.
"""
import ctypes as ct
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PTR = ct.POINTER(ct.c_uint32)
U32 = 0xFFFFFFFF
CANARY = 0x9AD4C781


class Stream(ct.Structure):
    _fields_ = [("out", PTR)] + [(n, ct.c_uint32) for n in ("mask", "n", "acc_i", "acc_q", "fill")]


class Mapper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="yapogee-map-")
        temp = Path(cls.temp.name)
        src = ROOT / "firmware/src"
        wrapper = '#include ' + json.dumps(str(src / "dvbs2.c")) + '''
#include "dvbs2_codes.h"
unsigned test_codes(void) { return N_DVBS2_CODES; }
void test_init(unsigned i, bool pilots) { dvbs2_init(&DVBS2_CODES[i], pilots); }
void test_map(const uint32_t *f, const uint32_t *sw, const uint32_t *fl, unsigned n, symstream_t *s) {
    map_words(f, sw, fl, n, s, r0);
}
'''
        (temp / "map.c").write_text(wrapper)
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        "-I" + str(src), str(temp / "map.c"), "-o", str(temp / "map.so")], check=True)
        cls.lib = ct.CDLL(str(temp / "map.so"))
        cls.lib.dvbs2_map_set_kernel.argtypes = [ct.c_uint]
        cls.lib.dvbs2_map_set_kernel.restype = ct.c_bool
        cls.lib.dvbs2_map_get_kernel.restype = ct.c_uint
        assert cls.lib.dvbs2_map_get_kernel() == 0
        cls.lib.test_init.argtypes = [ct.c_uint, ct.c_bool]
        cls.lib.test_map.argtypes = [PTR, PTR, PTR, ct.c_uint, ct.POINTER(Stream)]
        cls.lib.dvbs2_frame_begin_into.argtypes = [PTR, PTR]
        cls.lib.dvbs2_frame_ldpc_into.argtypes = [PTR]
        cls.lib.dvbs2_frame_finish_from.argtypes = [PTR, ct.POINTER(Stream)]
        cls.rng = np.random.default_rng(0xC001CAFE)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def words(self, size):
        return self.rng.integers(0, 1 << 32, size, dtype=np.uint32)

    def streams(self, size, fill, cursor, *, linear=False):
        guarded = [self.words(size + 2)]
        guarded[0][0] = guarded[0][-1] = CANARY
        guarded.append(guarded[0].copy())
        arrays = [a[1:-1] for a in guarded]
        ci, cq = (int(v) for v in self.rng.integers(1 << fill, size=2))
        states = [Stream(a.ctypes.data_as(PTR), U32 if linear else size - 1, cursor, ci, cq, fill)
                  for a in arrays]
        return guarded, states

    def equal(self, arrays, states):
        np.testing.assert_array_equal(*arrays)
        for array in arrays:
            self.assertEqual(int(array[0]), CANARY)
            self.assertEqual(int(array[-1]), CANARY)
        a, b = states
        self.assertEqual((a.n, a.acc_i, a.acc_q, a.fill), (b.n, b.acc_i, b.acc_q, b.fill))

    def test_selection_rejection_and_init_lifetime(self):
        for kernel in (0, 1):
            self.assertTrue(self.lib.dvbs2_map_set_kernel(kernel))
            self.lib.test_init(5, True)
            self.assertEqual(self.lib.dvbs2_map_get_kernel(), kernel)
            for bad in (2, 3, U32):
                self.assertFalse(self.lib.dvbs2_map_set_kernel(bad))
                self.assertEqual(self.lib.dvbs2_map_get_kernel(), kernel)

    def test_word_runs_all_fills_wraps_and_linear_output(self):
        self.lib.test_init(5, True)
        for fill in range(16):
            for size, linear in ((8, False), (256, False), (8192, False), (8192, True)):
                for count in (0, 1, 2, 3, 89, 90, 91, 221):
                    cursors = (0, 7) if linear else (size - 1, size + 7, 0xFFFFFFC0)
                    for cursor in cursors:
                        arrays, states = self.streams(size, fill, cursor, linear=linear)
                        inputs = [self.words(count + 1) for _ in range(3)]
                        for kernel, state in enumerate(states):
                            self.lib.dvbs2_map_set_kernel(kernel)
                            self.lib.test_map(*(a.ctypes.data_as(PTR) for a in inputs), count, ct.byref(state))
                        self.equal(arrays, states)

    def test_all_codes_pilots_canonical_fec_and_partial_plframes(self):
        self.assertEqual(self.lib.test_codes(), 21)
        for code in range(self.lib.test_codes()):
            for pilots in (False, True):
                self.lib.test_init(code, pilots)
                bb = self.words(2027)
                fec = [np.zeros(2027, np.uint32) for _ in range(2)]
                for kernel, frame in enumerate(fec):
                    self.lib.dvbs2_map_set_kernel(kernel)
                    self.lib.dvbs2_frame_begin_into(bb.ctypes.data_as(PTR), frame.ctypes.data_as(PTR))
                    self.lib.dvbs2_frame_ldpc_into(frame.ctypes.data_as(PTR))
                np.testing.assert_array_equal(*fec)
                for fill in range(16):
                    for size, linear in ((32, False), (8192, False), (8192, True)):
                        arrays, states = self.streams(size, fill, 0 if linear else size - 3, linear=linear)
                        for index in range(3):
                            frame = fec[0] if index == 0 else self.words(2027)
                            for kernel, state in enumerate(states):
                                self.lib.dvbs2_map_set_kernel(kernel)
                                self.lib.dvbs2_frame_finish_from(frame.ctypes.data_as(PTR), ct.byref(state))
                            self.equal(arrays, states)


if __name__ == "__main__":
    unittest.main()
