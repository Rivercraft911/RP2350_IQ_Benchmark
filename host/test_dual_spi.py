"""Check random-access TS framing against the sequential independent builder."""
import unittest
import zlib

import numpy as np

import dual_spi as spi
import dual_trial as trial


class SpiReference(unittest.TestCase):
    def test_bbframes(self):
        packets = np.frombuffer(b"".join(spi.pv.pattern_packet(k % spi.PACKET_PERIOD)
                                       for k in range(350)), np.uint8)
        sequential = spi.ref.ts_bbframes(packets, "2/3", ro=0.20)
        for index, expected in enumerate(sequential):
            np.testing.assert_array_equal(spi.bbframe(index), expected)
        for index in (1, 31, 10528, 123456):
            np.testing.assert_array_equal(spi.bbframe(index), spi.bbframe(index + 10528))

    def test_capture_and_corruption(self):
        cursor = 123 * spi.FRAME_SYMBOLS + 1000
        first, bus = spi.reference_window(cursor)
        offset = (cursor - first * spi.FRAME_SYMBOLS) * 8 + 13
        got = np.asarray(bus[offset + np.arange(8192) * 17], dtype="<u2").view("<u4").copy()
        def validate(words):
            lines = ["@cap " + str(i) + " " + " ".join(f"{v:08x}" for v in words[i:i + 16])
                     for i in range(0, len(words), 16)]
            result = dict(cap_words=4096, cap_crc=zlib.crc32(words.tobytes()), cap_stride=17,
                          divider256=256, cap_divider256=256, cap_symbol_cursor=cursor)
            return trial.validate_capture(result, lines, 5)
        good = validate(got)
        self.assertTrue(good["cap_aligned"])
        self.assertEqual(good["cap_mismatches"], 0)
        got[300] ^= 1
        self.assertEqual(validate(got)["cap_mismatches"], 1)

    def test_startup_is_not_runtime(self):
        result = {"pv_spi": dict(ok=25, ts_packets=172, bbframes=6,
                                 runtime_bbframes=0, runtime_messages=0,
                                 bb_crc=spi.first_crcs())}
        self.assertIn("no runtime SPI arrivals and BB construction", spi.packet_faults(result))


if __name__ == "__main__":
    unittest.main()
