"""Model the actual SDK-encoded RAW_PAIRS PIO and native benchmark CRC adapter.

This validates instruction timing and bus values, including signed padding bits.
It does not execute a Pico, model DMA contention, or establish analog timing margins.
"""
import ctypes as ct
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import zlib

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def function(source, name):
    match = re.search(r"(?:static\s+)?uint32_t\s+" + name + r"\([^)]*\)\s*\{", source)
    assert match, name
    depth, end = 1, match.end()
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start():end]


def sdk_header():
    candidates = [Path(os.environ["PICO_SDK_PATH"])] if "PICO_SDK_PATH" in os.environ else []
    cache = ROOT / "firmware/build-limits/CMakeCache.txt"
    if cache.exists():
        match = re.search(r"^PICO_SDK_PATH:[^=]+=(.+)$", cache.read_text(), re.M)
        if match:
            candidates.append(Path(match[1]))
    candidates += sorted((Path.home() / ".pico-sdk/sdk").glob("*"), reverse=True)
    for candidate in candidates:
        header = candidate / "src/rp2_common/hardware_pio/include/hardware/pio_instructions.h"
        if header.exists():
            return header
    raise unittest.SkipTest("Pico SDK PIO instruction header unavailable; set PICO_SDK_PATH")


class RawPairs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        header = sdk_header()
        cls.tmp = tempfile.TemporaryDirectory(prefix="yapogee-raw-pio-")
        temp = Path(cls.tmp.name)
        # PIO encoders are pure C; only the SDK's platform typedef/assert macros need a stub.
        (temp / "pico.h").write_text("#include <stdint.h>\n#include <stdbool.h>\n#include <assert.h>\n"
                                     "typedef unsigned int uint;\n"
                                     "#define valid_params_if(group, condition) assert(condition)\n"
                                     "#define PARAM_ASSERTIONS_ENABLED(group) 1\n")
        source = (ROOT / "firmware/src/output/iqout.c").read_text()
        match = re.search(r"if \(layout == IQ_LAYOUT_RAW_PAIRS\) \{(.*?)#define SIDE\(v\).*?"
                          r"const uint16_t p\[\] = \{(.*?)\};",
                          source, re.S)
        assert match, "RAW_PAIRS program missing"
        valid = re.search(r"hard_assert\(layout != IQ_LAYOUT_RAW_PAIRS \|\| (.*?)\);", source)
        assert valid, "RAW_PAIRS timing validation missing"
        crc = function((ROOT / "firmware/src/cpu_shaping/iqgen.c").read_text(), "crc32_update")
        packed = function((ROOT / "firmware/src/bench/bench8quad.c").read_text(), "packed_crc")
        wrapper = ("#include <stddef.h>\n#include <string.h>\n#include " + json.dumps(str(header)) + "\n"
                   "#define SIDE(v) pio_encode_sideset(3, (v))\n"
                   "unsigned raw_program(uint16_t *out, int cpw) {" + match[1] +
                   "const uint16_t p[] = {" + match[2] + "};\n"
                   "memcpy(out, p, sizeof p); return sizeof p/sizeof p[0]; }\n"
                   "int raw_valid(int cpw) { return " + valid[1] + "; }\n" +
                   function(source, "raw_divider").replace("static ", "", 1) + "\n" +
                   "#define OUT_WORDS 2048\n" + crc + "\n" + packed + "\n"
                   "uint32_t test_packed_crc(uint32_t crc, const uint32_t *p) { return packed_crc(crc,p); }\n")
        (temp / "test.c").write_text(wrapper)
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        "-I" + str(temp), str(temp / "test.c"), "-o", str(temp / "test.so")], check=True)
        cls.lib = ct.CDLL(str(temp / "test.so"))
        cls.lib.raw_program.argtypes = [ct.POINTER(ct.c_uint16), ct.c_int]
        cls.lib.raw_program.restype = ct.c_uint
        cls.lib.raw_valid.argtypes = [ct.c_int]
        cls.lib.raw_valid.restype = ct.c_int
        cls.lib.raw_divider.argtypes = [ct.c_int]
        cls.lib.raw_divider.restype = ct.c_uint32
        cls.lib.test_packed_crc.argtypes = [ct.c_uint32, ct.POINTER(ct.c_uint32)]
        cls.lib.test_packed_crc.restype = ct.c_uint32

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def pair_words(i, q):
        return np.stack([i[::2] | i[1::2] << 16, q[::2] | q[1::2] << 16], axis=1).ravel().astype("<u4")

    def program(self, cpw):
        code = (ct.c_uint16 * 32)()
        count = self.lib.raw_program(code, cpw)
        return list(code[:count])

    def trace(self, code, words, clocks):
        fifo = iter(int(w) for w in words)
        pc, x, osr, count, data, side, delay = 0, 0, 0, 32, 0, 0, 0
        bus, clock = [], []
        for _ in range(clocks):
            if delay:
                delay -= 1
            else:
                op = code[pc]
                pc = (pc + 1) % len(code)
                side, delay = (op >> 10) & 7, (op >> 8) & 3
                if op >> 13 == 3:  # OUT, shift right, autopull threshold32
                    if count == 32:
                        osr, count = next(fifo), 0
                    bits, dest = op & 31 or 32, (op >> 5) & 7
                    value = osr & ((1 << bits) - 1)
                    osr >>= bits
                    count += bits
                    if dest == 0:
                        data = value & 0x3FFF
                    elif dest == 1:
                        x = value
                    else:
                        self.assertEqual(dest, 3)  # NULL discards signed padding
                else:
                    self.assertEqual(op >> 13, 5)  # MOV
                    dest, source = (op >> 5) & 7, op & 31
                    if dest == 0:
                        self.assertEqual(source, 1)  # PINS <- X, OUT_COUNT14
                        data = x & 0x3FFF
                    else:
                        self.assertEqual((dest, source), (2, 2))  # NOP = MOV Y,Y
            bus.append(data | ((side & 3) << 14))
            clock.append(side >> 2)
        return np.asarray(bus, dtype=np.uint16), np.asarray(clock, dtype=np.uint8)

    def test_all_signed_halfwords_and_clock_schedule(self):
        i = np.arange(65536, dtype=np.uint32)
        q = (i * 40503 + 91) & 0xFFFF
        words = self.pair_words(i, q)
        expected = np.stack([(i & 0x3FFF) | 0x4000, q & 0x3FFF], axis=1).ravel()
        for cpw in range(3, 9):
            with self.subTest(cpw=cpw):
                code = self.program(cpw)
                self.assertEqual(len(code), 9)
                low, high = cpw // 2, cpw - cpw // 2
                # Decode the SDK's actual opcodes, with mandatory 3-bit side-set.
                self.assertEqual([(op >> 10) & 7 for op in code], [1, 5, 5, 0, 4, 1, 5, 0, 4])
                self.assertEqual([(op >> 8) & 3 for op in code],
                                 [low - 1, 0, high - 2, low - 1, high - 1,
                                  low - 1, high - 1, low - 1, high - 1])
                self.assertEqual(sum(1 + ((op >> 8) & 3) for op in code), 4 * cpw)
                bus, clock = self.trace(code, words, len(i) * 2 * cpw)
                np.testing.assert_array_equal(bus, np.repeat(expected, cpw))
                edges = np.asarray([0] * low + [1] * high, dtype=np.uint8)
                np.testing.assert_array_equal(clock, np.tile(edges, len(expected)))
                rising = np.flatnonzero(np.diff(clock.astype(int), prepend=0) == 1)
                np.testing.assert_array_equal(rising, np.arange(len(expected)) * cpw + low)
                np.testing.assert_array_equal(bus[rising], expected)
                self.assertFalse(np.any(bus & 0x8000))

    def test_raw_timing_range_and_pin_assignment(self):
        self.assertEqual([cpw for cpw in range(-2, 35) if self.lib.raw_valid(cpw)], list(range(3, 33)))
        config = (ROOT / "firmware/src/app/config.h").read_text()
        self.assertRegex(config, r"#define\s+PIN_D0\s+0\b")
        self.assertRegex(config, r"#define\s+PIN_CLKIO\s+16\b")

    def test_packed_crc_uses_iq_order_without_sign_padding_or_flag(self):
        rng = np.random.default_rng(1234)
        i, q = rng.integers(0, 65536, (2, 8192), dtype=np.uint32)
        words = self.pair_words(i, q)
        crc = 0
        for start in range(0, len(words), 2048):
            crc = self.lib.test_packed_crc(crc, words[start:].ctypes.data_as(ct.POINTER(ct.c_uint32)))
        expected = ((i & 0x3FFF) | (q & 0x3FFF) << 16).astype("<u4")
        self.assertEqual(crc, zlib.crc32(expected.tobytes()))


if __name__ == "__main__":
    unittest.main()
