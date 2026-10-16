"""Independent C3 GCS/FEC/code-ring references and strict cadence-trial validation.

No serial port is opened. The portable existing GCS adapter checks source/header
semantics; scalar Python BCH/LDPC/complex symbols supply final-ring CRCs.
"""
from __future__ import annotations

import copy
import contextlib
import ctypes as ct
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zlib

import numpy as np

import c3_fec_trial as trial


def fixture(code=5, frames=72, slow=0):
    counts = [(frames + 1) // 2, frames // 2]
    result = dict(cmd="c3fec", git="native-test", clk_hz=128000000, code_index=code,
                  s2_code="normal " + trial.RATES[code], ms=100, slow_us=slow,
                  source="memory_gcs", source_period_bytes=256, ring_groups=4096,
                  pilots=True, gold=0, frame_symbols=trial.FRAME_SYMBOLS,
                  frame_cycles=177552, cpw=1, frames_target=72, published_frames=frames,
                  consumed_frames=frames if not slow else max(0, frames - 2),
                  late_frames=int(bool(slow)), max_lateness_us=5000 if slow else 0,
                  max_service_gap_us=90, errors=int(bool(slow)),
                  elapsed_us=101300 if not slow else 11000,
                  core=[dict(frames=n, busy_cycles=300000*n, max_frame_cycles=300000 if n else 0,
                             max_slice_cycles=6000,
                             stage_cycles=dict(gcs=1000*n, bch=100000*n, ldpc=150000*n,
                                               map=40000*n, delay=0)) for n in counts])
    result.update(trial.ring_reference(code, frames, 4096))
    return result


class C3Reference(unittest.TestCase):
    def test_gcs_source_matches_actual_portable_adapter(self):
        root = Path(__file__).resolve().parents[1]
        packet = ct.CFUNCTYPE(ct.c_void_p)
        with tempfile.TemporaryDirectory(prefix="c3-gcs-native-") as directory:
            library = Path(directory) / "pv.so"
            subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                            str(root / "firmware/src/pvproto.c"), "-o", str(library)], check=True)
            lib = ct.CDLL(str(library))
            lib.pv_init.argtypes = [ct.c_uint32]
            lib.pv_gcs_bbframe.argtypes = [ct.c_void_p, ct.c_uint32, ct.c_uint8, ct.c_uint8, packet]
            lib.pv_gcs_bbframe.restype = ct.c_bool
            for code, rate in trial.RATES.items():
                lib.pv_init(16)
                kbch = trial.ref.code(rate, False).kbch
                memory = (ct.c_uint8 * (kbch // 8))()
                buffer = (ct.c_uint8 * 188)()
                cursor = 0

                def next_packet():
                    nonlocal cursor
                    buffer[:] = [trial.SOURCE[(cursor + j) % 256] for j in range(188)]
                    cursor += 188
                    return ct.addressof(buffer)

                callback = packet(next_packet)
                for index in range(7):
                    self.assertTrue(lib.pv_gcs_bbframe(memory, kbch, 0x72, 0xB9, callback))
                    expected = np.packbits(trial.gcs_bbframe(index, code)).tobytes()
                    self.assertEqual(bytes(memory), expected, (code, index))
                    self.assertEqual(bytes(memory)[:2], b"\x72\x00")
                    self.assertEqual(bytes(memory)[6:9], b"\xb9\x00\x00")

    def test_packed_codes_chronological_and_little_endian(self):
        codes = np.array([[0, 1, 2, 3] * 4, [3, 2, 1, 0] * 4], np.uint32)
        packed = trial.pack_codes(codes)
        self.assertEqual(packed.tolist(), [[0xE4E4E4E4], [0x1B1B1B1B]])
        self.assertEqual(packed.tobytes(), bytes([0xE4] * 4 + [0x1B] * 4))
        for bad in (np.zeros((2, 15)), np.zeros((1, 16)), np.full((2, 16), 4)):
            with self.assertRaises(ValueError):
                trial.pack_codes(bad)

    def test_ring_wrap_and_partial_frame_carry_against_full_stream(self):
        for frames in (0, 1, 2, 3, 8, 9):
            full = (np.concatenate([trial.frame_codes(index, 5) for index in range(frames)], axis=1)
                    if frames else np.zeros((2, 0), np.uint32))
            groups = frames * trial.FRAME_SYMBOLS // 16
            packed = trial.pack_codes(full[:, :groups * 16])
            for capacity in (1, 2, 4096, 16384):
                # Scalar stores start at absolute group 0; the tested implementation
                # computes only the retained suffix directly at its global phase.
                rings = np.zeros((2, capacity), dtype="<u4")
                for index in range(groups):
                    rings[:, index % capacity] = packed[:, index]
                actual = trial.ring_reference(5, frames, capacity)
                self.assertEqual(actual["code_crc"], [zlib.crc32(axis.tobytes()) for axis in rings])
                self.assertEqual(actual["published_groups"], groups)
                self.assertEqual(actual["pending_fill"], full.shape[1] % 16)
                self.assertEqual(actual["pending_codes"],
                                 [sum(int(v) << (2*j) for j, v in enumerate(axis[groups*16:]))
                                  for axis in full])

    def test_all_requested_rate_full_pipeline_crc(self):
        expected = {5: [1531943157, 3986744385], 8: [2356957565, 1808985931],
                    6: [2937561118, 1824749921]}
        for code, crc in expected.items():
            with self.subTest(code=code):
                actual = trial.ring_reference(code, 3, 4096)
                self.assertEqual(actual["code_crc"], crc)
                self.assertEqual(actual["published_groups"], 4161)
                self.assertEqual(actual["pending_fill"], 6)

    def test_random_access_source_and_frame_invariant(self):
        for code, rate in trial.RATES.items():
            count = (trial.ref.code(rate, False).kbch - 80) // 8
            for index in (0, 1, 2, 1245, 43210):
                bits = trial.gcs_bbframe(index, code)
                self.assertEqual(len(bits), trial.ref.code(rate, False).kbch)
                data = np.packbits(bits).tobytes()[10:]
                self.assertEqual(data, bytes((29 * (index * count + j) + 7) & 255 for j in range(count)))
        with self.assertRaises(ValueError):
            trial.gcs_bbframe(-1, 5)

    def test_invalid_ring_metadata(self):
        for value in (0, 3, -1, 65537, True, 4096.0):
            with self.assertRaises(ValueError):
                trial.ring_reference(5, 3, value)
        for frames in (-1, True, 1.0):
            with self.assertRaises(ValueError):
                trial.ring_reference(5, frames, 4096)


class C3Protocol(unittest.TestCase):
    def test_positive_and_delayed_control(self):
        for code in trial.RATES:
            self.assertEqual(trial.run_faults(fixture(code), code, 100, 0), [])
        delayed = fixture(frames=2, slow=5000)
        self.assertEqual(trial.run_faults(delayed, 5, 100, 5000), [])
        # A healthy-looking control is a failure: the deliberate overload must
        # be caught by the device's deadline monitor.
        delayed.update(late_frames=0, max_lateness_us=0, errors=0)
        self.assertTrue(any("control" in x for x in trial.run_faults(delayed, 5, 100, 5000)))

    def test_crc_corruption_and_frame_padding_rejected(self):
        good = fixture()
        bad = copy.deepcopy(good)
        bad["code_crc"][0] ^= 1
        self.assertTrue(any("code_crc" in x for x in trial.run_faults(bad, 5, 100, 0)))
        bad = copy.deepcopy(good)
        bad["published_groups"] = good["published_frames"] * ((trial.FRAME_SYMBOLS + 15) // 16)
        self.assertTrue(any("published_groups" in x for x in trial.run_faults(bad, 5, 100, 0)))

    def test_late_or_incomplete_positive_rejected(self):
        for changes in (dict(late_frames=1, max_lateness_us=1), dict(errors=1),
                        dict(consumed_frames=71), dict(published_frames=71),
                        dict(frames_target=73), dict(max_service_gap_us=129)):
            bad = fixture()
            bad.update(changes)
            self.assertTrue(trial.run_faults(bad, 5, 100, 0), changes)

    def test_metadata_and_nonfinite_accounting_rejected(self):
        good = fixture()
        for key in ("published_frames", "consumed_frames", "elapsed_us", "code_crc", "core",
                    "source", "source_period_bytes", "ring_groups", "frames_target", "git"):
            bad = copy.deepcopy(good)
            bad.pop(key)
            self.assertTrue(trial.run_faults(bad, 5, 100, 0), key)
        for value in (None, True, -1, float("nan"), float("inf"), "72"):
            bad = copy.deepcopy(good)
            bad["published_frames"] = value
            self.assertTrue(trial.run_faults(bad, 5, 100, 0), value)
        for value in (float("nan"), float("inf"), 1.2, "busy"):
            bad = copy.deepcopy(good)
            bad["core"][0]["busy"] = value
            self.assertTrue(trial.run_faults(bad, 5, 100, 0), value)
        bad = copy.deepcopy(good)
        bad["core"][0]["stage_cycles"]["map"] = bad["core"][0]["busy_cycles"] + 1
        self.assertTrue(any("stages" in x for x in trial.run_faults(bad, 5, 100, 0)))
        bad = copy.deepcopy(good)
        bad["core"][0]["frames"] += 1
        self.assertTrue(any("even/odd" in x for x in trial.run_faults(bad, 5, 100, 0)))

    def test_clock_ratio_and_target(self):
        good = fixture(frames=144)
        good.update(clk_hz=256000000, frames_target=144, consumed_frames=144, published_frames=144)
        self.assertEqual(trial.run_faults(good, 5, 100, 0), [])
        good["frame_cycles"] *= 2
        self.assertTrue(trial.run_faults(good, 5, 100, 0))
        good = fixture()
        good.update(clk_hz=256000000, cpw=2, frame_cycles=355104)
        self.assertEqual(trial.run_faults(good, 5, 100, 0, cpw=2), [])

    def test_no_arguments_never_open_serial(self):
        with mock.patch.object(trial.iq, "Board", side_effect=AssertionError("serial accessed")):
            with mock.patch.object(sys, "argv", ["c3_fec_trial.py"]):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as result:
                        trial.main()
                self.assertEqual(result.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
