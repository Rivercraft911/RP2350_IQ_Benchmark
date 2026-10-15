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

    def test_gcs_bbframes(self):
        stream = b"".join(spi.pv.pattern_packet(k % spi.PACKET_PERIOD) for k in range(350))
        for index in range(len(stream) // spi.DATA_BYTES):
            data = stream[index * spi.DATA_BYTES:(index + 1) * spi.DATA_BYTES]
            expected = spi.ref.bbframe(np.unpackbits(np.frombuffer(data, np.uint8)), "2/3",
                                       ro=0.20, sync=0xB9)
            np.testing.assert_array_equal(spi.bbframe(index, gcs=True), expected)
        for index in (0, 1, 31, 10528, 123456):
            np.testing.assert_array_equal(spi.bbframe(index, gcs=True), spi.bbframe(index + 10528, gcs=True))

    def check_capture_and_corruption(self, rows, span, beta):
        cursor = 123 * spi.FRAME_SYMBOLS + 1000
        first, bus = spi.reference_window(cursor, span=span, beta=beta)
        offset = (cursor - first * spi.FRAME_SYMBOLS) * 8 + 13
        got = np.asarray(bus[offset + np.arange(8192) * 17], dtype="<u2").view("<u4").copy()
        def validate(words):
            lines = ["@cap " + str(i) + " " + " ".join(f"{v:08x}" for v in words[i:i + 16])
                     for i in range(0, len(words), 16)]
            result = dict(cap_words=4096, cap_crc=zlib.crc32(words.tobytes()), cap_stride=17, row_symbols=rows,
                          divider256=256, cap_divider256=256, cap_symbol_cursor=cursor)
            return trial.validate_capture(result, lines, 5)
        good = validate(got)
        self.assertTrue(good["cap_aligned"])
        self.assertEqual(good["cap_mismatches"], 0)
        got[300] ^= 1
        self.assertEqual(validate(got)["cap_mismatches"], 1)

    def test_capture_and_corruption(self):
        for profile in ((2, 12, 1.0), (4, 9, 0.8)):
            with self.subTest(profile=profile):
                self.check_capture_and_corruption(*profile)

    def test_startup_is_not_runtime(self):
        result = {"pv_spi": dict(ok=25, ts_packets=172, bbframes=6,
                                 runtime_bbframes=0, runtime_messages=0,
                                 bb_crc=spi.first_crcs())}
        self.assertIn("no runtime SPI arrivals and BB construction", spi.packet_faults(result))

    def test_receive_closure_faults(self):
        crc_fields = b"".join(spi.pv.message(k, spi.pv.pattern_payload(k))[-4:]
                               for k in range(16))
        stats = dict.fromkeys(("lost", "bad_hdr", "bad_crc", "bad_sync", "null_packets",
                               "short_msgs", "long_msgs", "overflows"), 0)
        stats.update(ok=32, ts_packets=224, runtime_bbframes=6, runtime_messages=8,
                     crc_chain=zlib.crc32(crc_fields * 2), bb_crc=spi.first_crcs(), deferred_msgs=3)
        self.assertEqual(spi.packet_faults({"pv_spi": stats}), [])
        for field in ("drain_timeouts", "late_closes"):
            self.assertIn(f"pv_spi.{field}", spi.packet_faults({"pv_spi": dict(stats, **{field: 1})}))

    def test_gcs_counters_and_reference_faults(self):
        crc_fields = b"".join(spi.pv.message(k, spi.pv.pattern_payload(k))[-4:] for k in range(16))
        stats = dict.fromkeys(("lost", "bad_hdr", "bad_crc", "bad_sync", "null_packets", "ts_packets",
                               "short_msgs", "long_msgs", "overflows", "gcs_underflows"), 0)
        size = 6 * spi.DATA_BYTES
        stats.update(ok=32, gcs_packets=(size + 187) // 188, gcs_bytes=size, bbframes=6, bb_mode="gcs",
                     runtime_bbframes=2, runtime_messages=8, crc_chain=zlib.crc32(crc_fields * 2),
                     bb_crc=spi.first_crcs(gcs=True))
        self.assertEqual(spi.packet_faults({"source": 6, "pv_spi": stats}), [])
        changes = {"gcs_underflows": 1, "bb_mode": "ts", "gcs_bytes": size + 1,
                   "gcs_packets": 1, "ts_packets": 1, "bb_crc": spi.first_crcs()}
        for name, value in changes.items():
            with self.subTest(field=name):
                self.assertTrue(spi.packet_faults({"source": 6, "pv_spi": dict(stats, **{name: value})}))


if __name__ == "__main__":
    unittest.main()
