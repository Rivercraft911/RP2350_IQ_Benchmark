"""Decode the actual RAW_PAIRS program at every supported integer slot width.

Odd widths retain a uniform word period with floor(cpw/2) low clocks and the
remaining clocks high. This proves digital schedule and values, not pad timing.
"""
from __future__ import annotations

import ctypes as ct
import json
from pathlib import Path
import re
import subprocess
import unittest

import numpy as np

import test_iqout_raw as raw


class IntegerSlots(raw.RawPairs):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        source = (raw.ROOT / "firmware/src/output/iqout.c").read_text()
        capture = re.search(r"prog_cap\[0\] = .*?prog_cap\[2\] = .*?;", source, re.S)
        assert capture, "capture program missing"
        temp = Path(cls.tmp.name)
        wrapper = (
            "#include " + json.dumps(str(raw.sdk_header())) + "\n"
            "void capture_program(uint16_t *prog_cap, int cpw) {\n" +
            capture[0] + "\n}\n"
        )
        (temp / "capture.c").write_text(wrapper)
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-shared", "-fPIC", "-I" + str(temp), str(temp / "capture.c"),
                        "-o", str(temp / "capture.so")], check=True)
        cls.capture_lib = ct.CDLL(str(temp / "capture.so"))
        cls.capture_lib.capture_program.argtypes = [ct.POINTER(ct.c_uint16), ct.c_int]

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
                self.assertEqual([(op >> 10) & 7 for op in code],
                                 [1, 5, 5, 0, 4, 1, 5, 0, 4])
                self.assertEqual([(op >> 8) & 3 for op in code],
                                 [low - 1, 0, high - 2, low - 1, high - 1,
                                  low - 1, high - 1, low - 1, high - 1])
                self.assertEqual(sum(1 + ((op >> 8) & 3) for op in code), 4 * cpw)
                bus, clock = self.trace(code, words, len(expected) * cpw)
                np.testing.assert_array_equal(bus, np.repeat(expected, cpw))
                slot = np.asarray([0] * low + [1] * high, dtype=np.uint8)
                np.testing.assert_array_equal(clock, np.tile(slot, len(expected)))
                rising = np.flatnonzero(np.diff(clock.astype(int), prepend=0) == 1)
                np.testing.assert_array_equal(rising, np.arange(len(expected)) * cpw + low)
                np.testing.assert_array_equal(bus[rising], expected)
                self.assertFalse(np.any(bus & 0x8000))

    def test_raw_timing_range_and_pin_assignment(self):
        self.assertEqual([cpw for cpw in range(-2, 35) if self.lib.raw_valid(cpw)],
                         list(range(3, 33)))
        config = (raw.ROOT / "firmware/src/app/config.h").read_text()
        self.assertRegex(config, r"#define\s+PIN_D0\s+0\b")
        self.assertRegex(config, r"#define\s+PIN_CLKIO\s+16\b")

    def test_even_program_opcodes_unchanged(self):
        for cpw in (4, 6, 8):
            half = cpw // 2
            # Exact previous instruction words: OUT14, OUT NULL2, OUT X16,
            # OUT14, OUT NULL2, MOV PINS X, NOP, OUT14, OUT NULL2.
            opcodes = [0x600E, 0x6062, 0x6030, 0x600E, 0x6062,
                       0xA001, 0xA042, 0x600E, 0x6062]
            side = [1, 5, 5, 0, 4, 1, 5, 0, 4]
            delay = [half - 1, 0, half - 2] + [half - 1] * 6
            expected = [op | (s << 10) | (d << 8)
                        for op, s, d in zip(opcodes, side, delay)]
            self.assertEqual(self.program(cpw), expected)

    def test_capture_samples_keep_uniform_word_period(self):
        for cpw in range(3, 33):
            code = (ct.c_uint16 * 3)()
            self.capture_lib.capture_program(code, cpw)
            self.assertEqual([op >> 13 for op in code], [2, 2, 4])
            self.assertEqual([op & 31 for op in code[:2]], [16, 16])
            self.assertEqual([(op >> 8) & 31 for op in code], [cpw - 1, cpw - 2, 0])
            clock, samples = 0, []
            for n in range(60):
                op = code[n % 3]
                if op >> 13 == 2:
                    samples.append(clock)
                clock += 1 + ((op >> 8) & 31)
            self.assertEqual(samples, [cpw * n for n in range(40)])

    def test_wide_periods_use_exact_quarter_dividers(self):
        rng = np.random.default_rng(7281)
        i, q = rng.integers(0, 65536, (2, 1024), dtype=np.uint32)
        expected = np.stack([(i & 0x3fff) | 0x4000, q & 0x3fff], axis=1).ravel()
        bus, _ = self.trace(self.program(4), self.pair_words(i, q), len(expected) * 4)
        for cpw in range(3, 9):
            self.assertEqual(self.lib.raw_divider(cpw), 256)
        for cpw in range(9, 33):
            with self.subTest(cpw=cpw):
                self.assertEqual(self.program(cpw), self.program(4))
                divider = self.lib.raw_divider(cpw)
                self.assertEqual(divider, cpw * 64)
                # Every possible fractional phase gives exactly cpw system
                # clocks per four PIO ticks; there is no word-period jitter.
                phases = np.arange(256, dtype=np.int64)[:, None]
                ends = (phases + np.arange(65) * divider) // 256
                periods = np.diff(ends, axis=1).reshape(256, 16, 4).sum(axis=2)
                np.testing.assert_array_equal(periods, np.full_like(periods, cpw))
                for phase in (0, 63, 127, 191, 255):
                    ticks = (phase + np.arange(len(bus) + 1) * divider) // 256
                    physical = np.repeat(bus, np.diff(ticks))
                    np.testing.assert_array_equal(physical, np.repeat(expected, cpw))
        source = (raw.ROOT / "firmware/src/output/iqout.c").read_text()
        self.assertIn("const uint32_t divider = raw ? raw_divider(cpw) : 256u;", source)
        self.assertIn("sm_config_set_clkdiv_int_frac8(&c, divider >> 8, divider & 255u);", source)


if __name__ == "__main__":
    unittest.main()
