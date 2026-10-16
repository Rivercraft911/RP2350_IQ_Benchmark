"""Run the actual portable GCS adapter against independent DVB-S2 bit references."""
import ctypes as ct
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import numpy as np

import dual_spi as spi


class Stats(ct.Structure):
    _fields_ = [(name, ct.c_uint32) for name in (
        "ok", "nop", "bad_hdr", "bad_crc", "bad_sync", "lost", "ts_packets", "null_packets",
        "bbframes", "gcs_packets", "gcs_bytes", "gcs_underflows", "crc_chain", "last_seq", "seq_mod")]
    _fields_ += [("have_seq", ct.c_bool), ("first_bad_at", ct.c_int32), ("first_bad_info", ct.c_uint32)]


Packet = ct.CFUNCTYPE(ct.c_void_p)


class BorrowedPackets:
    """Every callback recycles one buffer, like a released PV-SPI slot."""
    def __init__(self, limit=None):
        self.buffer = (ct.c_uint8 * 188)()
        self.count, self.limit, self.previous = 0, limit, None
        self.mutations = 0
        self.callback = Packet(self.next)

    def next(self):
        if self.previous is not None and bytes(self.buffer) != self.previous:
            self.mutations += 1
        if self.limit is not None and self.count >= self.limit:
            return None
        self.previous = spi.pv.pattern_packet(self.count % spi.PACKET_PERIOD)
        self.buffer[:] = self.previous
        self.count += 1
        return ct.addressof(self.buffer)

    def unchanged(self):
        return self.mutations == 0 and (self.previous is None or bytes(self.buffer) == self.previous)


class GcsNative(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="yapogee-gcs-")
        src = pathlib.Path(__file__).resolve().parents[1] / "firmware/src"
        library = pathlib.Path(cls.tmp.name) / "pvproto.so"
        subprocess.run([shutil.which("cc"), "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-shared", "-fPIC", str(src / "ingress/pvproto.c"), "-o", str(library)], check=True)
        cls.lib = ct.CDLL(str(library))
        cls.lib.pv_init.argtypes = [ct.c_uint32]
        cls.lib.pv_gcs_bbframe.argtypes = [ct.c_void_p, ct.c_uint32, ct.c_uint8, ct.c_uint8, Packet]
        cls.lib.pv_gcs_bbframe.restype = ct.c_bool
        cls.stats = Stats.in_dll(cls.lib, "pv_stats")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def frame(self, kbch, packets, *, matype=0x72, sync=0xB9):
        size = kbch // 8
        guarded = (ct.c_uint8 * (size + 16))(*([0xA5] * (size + 16)))
        ok = self.lib.pv_gcs_bbframe(ct.addressof(guarded) + 8, kbch, matype, sync, packets.callback)
        self.assertEqual(bytes(guarded[:8]), b"\xA5" * 8)
        self.assertEqual(bytes(guarded[-8:]), b"\xA5" * 8)
        return ok, bytes(guarded[8:-8])

    @staticmethod
    def data(cursor, size):
        stream = b"".join(spi.pv.pattern_packet(k) for k in range(spi.PACKET_PERIOD))
        start = cursor % len(stream)
        return (stream * ((start + size + len(stream) - 1) // len(stream)))[start:start + size]

    def test_all_dvb_code_headers_and_stream_boundaries(self):
        for short in (False, True):
            rates = spi.ref.RATES[:-1] if short else spi.ref.RATES
            for rate in rates:
                with self.subTest(short=short, rate=rate):
                    self.lib.pv_init(16)
                    packets = BorrowedPackets()
                    kbch = spi.ref.code(rate, short).kbch
                    size = kbch // 8 - 10
                    for index in range(6):
                        ok, actual = self.frame(kbch, packets)
                        self.assertTrue(ok)
                        data = self.data(index * size, size)
                        expected = spi.ref.bbframe(np.unpackbits(np.frombuffer(data, np.uint8)), rate,
                                                   short=short, ro=0.20, sync=0xB9)
                        self.assertEqual(actual, np.packbits(expected).tobytes())
                    self.assertTrue(packets.unchanged())
                    self.assertEqual(self.stats.bbframes, 6)
                    self.assertEqual(self.stats.gcs_bytes, 6 * size)
                    self.assertEqual(self.stats.gcs_packets, (6 * size + 187) // 188)
                    self.assertEqual(self.stats.gcs_underflows, 0)
                    self.assertEqual(self.stats.ts_packets + self.stats.null_packets, 0)

    def test_aligned_empty_and_one_byte_fields(self):
        for size, count in ((0, 3), (188, 8), (1, 390)):
            with self.subTest(data_bytes=size):
                self.lib.pv_init(16)
                packets = BorrowedPackets()
                for index in range(count):
                    ok, actual = self.frame(80 + 8 * size, packets)
                    self.assertTrue(ok)
                    self.assertEqual(actual[10:], self.data(index * size, size))
                    self.assertEqual(actual[:9], bytes([0x72, 0, 0, 0, size >> 5, size << 3 & 255,
                                                       0xB9, 0, 0]))
                self.assertTrue(packets.unchanged())
                self.assertEqual(packets.count, (size * count + 187) // 188)

    def test_underflow_clears_output_without_null_substitution(self):
        for limit in (0, 1, 7, 28):
            with self.subTest(available_packets=limit):
                self.lib.pv_init(16)
                packets = BorrowedPackets(limit)
                ok, actual = self.frame(43040, packets)
                self.assertFalse(ok)
                self.assertEqual(actual, bytes(5380))
                self.assertTrue(packets.unchanged())
                self.assertEqual(packets.count, limit)
                self.assertEqual(self.stats.gcs_underflows, 1)
                self.assertEqual(self.stats.gcs_bytes, 188 * limit)
                self.assertEqual(self.stats.bbframes, 0)
                self.assertEqual(self.stats.null_packets, 0)
        self.lib.pv_init(16)
        ok, actual = self.frame(43040, BorrowedPackets())
        self.assertTrue(ok)
        self.assertEqual(actual, np.packbits(spi.bbframe(0, gcs=True)).tobytes())

    def test_underflow_after_partial_container_carry(self):
        self.lib.pv_init(16)
        packets = BorrowedPackets(30)
        ok, actual = self.frame(43040, packets)
        self.assertTrue(ok)
        self.assertEqual(actual, np.packbits(spi.bbframe(0, gcs=True)).tobytes())
        self.assertEqual(packets.count, 29)
        ok, actual = self.frame(43040, packets)
        self.assertFalse(ok)
        self.assertEqual(actual, bytes(5380))
        self.assertTrue(packets.unchanged())
        self.assertEqual(self.stats.bbframes, 1)
        self.assertEqual(self.stats.gcs_bytes, 30 * 188)
        self.assertEqual(self.stats.gcs_packets, 30)
        self.assertEqual(self.stats.gcs_underflows, 1)

    def test_random_access_pattern_reference(self):
        self.lib.pv_init(16)
        packets = BorrowedPackets()
        for index in range(50):
            ok, actual = self.frame(43040, packets)
            self.assertTrue(ok)
            self.assertEqual(actual, np.packbits(spi.bbframe(index, gcs=True)).tobytes())
        self.assertTrue(packets.unchanged())


if __name__ == "__main__":
    unittest.main()
