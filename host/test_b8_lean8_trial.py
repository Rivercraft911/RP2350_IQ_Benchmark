"""Independent B8 references and fail-closed device evidence checks; no USB access."""
from __future__ import annotations

import contextlib
import copy
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zlib

import numpy as np

import b8_lean8_trial as trial
import iq4fec_trial as previous


def fixture(code=8, cpw=2, invert=0, priority=13, options=27, starve=0):
    elapsed = 5500 if starve else 100000
    frames = 2 if starve else 40
    runtime = frames - 2
    groups = frames * trial.FRAME_SYMBOLS // 16
    issued = [groups] * 2 if starve else [50000, 50002]
    grants = groups if starve else 50176
    symbol = 1000 if starve else 2 * trial.FRAME_SYMBOLS - 300
    lo, hi = symbol - 40, symbol + 40
    base, bus = trial.reference_window(max(0, lo - 256), hi + 256, code)
    offset = symbol * trial.BUS_PER_SYMBOL + 3 - base
    samples = bus[offset + trial.STRIDE * np.arange(trial.CAP_WORDS)].astype("<u4")
    samples |= ((((samples >> 14) & 1) ^ invert) << 28)
    meta, lines = trial.capture_lines(samples)
    kbch = trial.dual_spi.ref.code(trial.RATES[code], False).kbch
    gcs_bytes = frames * (kbch - 80) // 8
    packets = (gcs_bytes + 187) // 188
    accepted = max(24, (packets + 6) // 7 + 4)
    crc_fields = b"".join(trial.dual_spi.pv.message(k, trial.dual_spi.pv.pattern_payload(k))[-4:]
                          for k in range(16))
    chain = crc_fields * (accepted // 16) + crc_fields[:4 * (accepted % 16)]
    spi = dict(bb_mode="gcs", sck_hz=32000000, bbframes=frames, runtime_bbframes=runtime,
               runtime_messages=0 if starve else accepted - 24, crc_chain=zlib.crc32(chain),
               bb_crc=trial.dual_spi.first_crcs(gcs=True, rate=trial.RATES[code]),
               ok=accepted, lost=0, bad_hdr=0, bad_crc=0, bad_sync=0, ts_packets=0,
               null_packets=0, gcs_packets=packets, gcs_bytes=gcs_bytes, gcs_underflows=0,
               short_msgs=0, long_msgs=0, overflows=0, deferred_msgs=0,
               drain_timeouts=0, late_closes=0)
    if starve:
        spi["bb_crc"] = spi["bb_crc"][:2] + [0, 0]
    cores = []
    for index in range(2):
        count = (runtime + 1 - index) // 2
        cycles = 100000 if starve and index == 0 else 0 if starve else 6000000
        cores.append(dict(frames=count, busy_cycles=cycles,
                          busy=cycles / (elapsed * trial.CLOCK_HZ / 1e6),
                          max_slice_cycles=0 if starve else 12000,
                          stage_cycles={name: count * 50000 if name != "delay" else 0 for name in trial.STAGES}))
    result = dict(cmd="b8lean8", git="native-test", clk_hz=trial.CLOCK_HZ, cpw=cpw, ms=100,
                  invert=invert, bus_priority_mask=priority, options=options, starve=starve,
                  code_index=code, s2_code="normal " + trial.RATES[code], kbch=kbch,
                  pilots=True, gold=0, source=6, row_profile=trial.L7_PROFILE, L=7, sps=4,
                  row_symbols=1, row_words=1, sample_bits=8, writers=2,
                  frame_symbols=trial.FRAME_SYMBOLS, table_crc=trial.TABLE8_CRC,
                  errors=trial.SOURCE_EMPTY | 3 if starve else 0,
                  pointer_errors=trial.SOURCE_EMPTY if starve else 0,
                  output_errors=3 if starve else 0, txstalls_i=int(bool(starve)),
                  txstalls_q=int(bool(starve)), pointer_txstall_mask=int(bool(starve)),
                  null_trigger_mask=1 if starve else 0, restored=True, selectors_restored=True,
                  elapsed_us=elapsed, prefill_frames=2, published_frames=frames,
                  code_crc=trial.ring_crcs(frames, code),
                  published_groups=groups, published_words=groups, granted_words=grants,
                  reusable_words=min(issued), input_issue_words=issued,
                  min_credit_runway_words=0 if starve else 100,
                  max_service_gap_us=90, service_gap_limit_us=128, core=cores,
                  cap_symbol_lo=lo, cap_symbol_hi=hi, cap_margin_symbols=256,
                  pv_spi=spi, reason="SOURCE_EMPTY" if starve else "complete", **meta)
    return result, samples, lines


def validate(result, lines, **kwargs):
    return trial.validate_run(result, lines, result["code_index"], result["ms"], result["cpw"],
                              result["invert"], result["bus_priority_mask"], result["options"],
                              result["starve"], "native-test", **kwargs)


class B8Reference(unittest.TestCase):
    def test_existing_independent_5_6_reference_agrees_at_start_and_frame_seams(self):
        for first, last in ((0, 2), (2, 4)):
            np.testing.assert_array_equal(trial.shaped_bus(first, last, 8),
                                          previous.shaped_bus(first, last, 8))
        codes = trial.geometric_codes(np.full(6, np.exp(1j * np.pi / 8)))
        self.assertEqual(codes.tolist(), [[0] * 6, [2] * 6])

    def test_all_rates_continuous_frame_history_and_byte_output(self):
        # These scalar references are the actual SPI pattern, rather than C3's
        # distinct 256-byte in-memory source. Different rates also change headers.
        crcs = []
        for code in trial.RATES:
            full = trial.shaped_bus(0, 3, code)
            suffix = trial.shaped_bus(1, 3, code)
            np.testing.assert_array_equal(suffix, full[trial.FRAME_SYMBOLS * 8:])
            self.assertFalse(np.any(full & 0x3f))
            self.assertTrue(np.all(full[0::2] & 0x4000))
            self.assertFalse(np.any(full[1::2] & 0x4000))
            crcs.append(zlib.crc32(full.tobytes()))
        self.assertEqual(len(set(crcs)), 3)

    def test_retained_ring_crc_matches_scalar_absolute_stores_and_rejects_frame_padding(self):
        for frames in (0, 1, 2, 3, 8):
            for code in trial.RATES:
                points = (np.concatenate([trial.frame_points(index, code) for index in range(frames)])
                          if frames else np.zeros(0, dtype=np.complex128))
                codes = trial.geometric_codes(points)
                groups = len(points) // 16
                rings = np.zeros((2, 4096), dtype="<u4")
                for axis in range(2):
                    for group in range(groups):
                        word = sum(int(value) << (2 * bit) for bit, value in
                                   enumerate(codes[axis, group * 16:(group + 1) * 16]))
                        rings[axis, group % 4096] = word
                self.assertEqual(trial.ring_crcs(frames, code),
                                 [zlib.crc32(axis.tobytes()) for axis in rings], (frames, code))
        result, _, lines = fixture()
        result["code_crc"] = [value ^ 1 for value in result["code_crc"]]
        self.assertTrue(any("ring CRC" in fault for fault in validate(result, lines)["faults"]))

    def test_all_rates_clock_polarities_priorities_and_controls(self):
        for code in trial.RATES:
            for invert in (0, 1):
                for priority in (0, 13):
                    for starve in (0, 1):
                        result, _, lines = fixture(code, invert=invert, priority=priority, starve=starve)
                        checked = validate(result, lines)
                        self.assertEqual(checked["faults"], [], checked)
                        self.assertEqual(checked["cap_checked_bus_words"], 4096)
                        self.assertEqual(checked["cap_clock_mismatches"], invert * 4096)
                        self.assertEqual(checked["classification"], "source_starvation_detected" if starve else
                                         "clock_inversion_detected" if invert else "digital_complete_8psk_clean")

    def test_corrupted_late_sample_wrong_rate_and_clock_are_rejected(self):
        result, words, lines = fixture()
        corrupt = words.copy(); corrupt[-1] ^= 64
        meta, bad_lines = trial.capture_lines(corrupt)
        checked = validate(dict(result, **meta), bad_lines)
        self.assertEqual(checked["cap_mismatches"], 1)
        self.assertTrue(checked["faults"])
        self.assertFalse(trial.validate_capture(result, lines, 5, 0)["cap_aligned"])
        self.assertFalse(trial.validate_capture(result, lines, 8, 1)["cap_clock_ok"])
        corrupt = words.copy(); corrupt[-1] ^= 1 << 28
        meta, bad_lines = trial.capture_lines(corrupt)
        self.assertTrue(validate(dict(result, **meta), bad_lines)["faults"])

    def test_transport_and_capture_cursor_metadata_fail_closed(self):
        result, words, lines = fixture()
        changes = ({"cap_words": 4095}, {"cap_crc": 0}, {"cap_stride": 1},
                   {"cap_format": "u16"}, {"cap_symbol_lo": -1}, {"cap_symbol_hi": "123"},
                   {"cap_symbol_hi": result["cap_symbol_lo"] - 1},
                   {"cap_margin_symbols": 4097}, {"cap_margin_symbols": False},
                   {"cap_symbol_hi": result["cap_symbol_lo"] + 65537})
        for change in changes:
            with self.subTest(change=change):
                checked = validate(dict(result, **change), lines)
                self.assertFalse(checked["cap_transport_ok"])
                self.assertTrue(checked["faults"])
        self.assertFalse(trial.validate_capture(result, lines[:-1], 8, 0)["cap_transport_ok"])

    def test_ownership_ordering_timing_and_target_budget_rejections(self):
        result, _, lines = fixture()
        changes = ({"published_groups": result["published_groups"] + 1},
                   {"published_words": result["published_words"] + 1},
                   {"granted_words": result["published_words"] + 1},
                   {"input_issue_words": [result["granted_words"] + 1] * 2},
                   {"input_issue_words": [100, 100]}, {"input_issue_words": [False, 0]},
                   {"max_service_gap_us": 129}, {"max_service_gap_us": 0},
                   {"elapsed_us": 200000}, {"min_credit_runway_words": 0},
                   {"own_errors": 1}, {"publish_order_errors": 1}, {"null_trigger_mask": 1},
                   {"pointer_txstall_mask": 1}, {"restored": False},
                   {"selectors_restored": False}, {"git": "another-image"})
        for change in changes:
            with self.subTest(change=change):
                self.assertTrue(validate(dict(result, **change), lines)["faults"])
        for field, value in (("frames", 0), ("busy_cycles", 0), ("busy", float("nan")),
                             ("max_slice_cycles", 16385), ("stage_cycles", {})):
            bad = copy.deepcopy(result); bad["core"][0][field] = value
            self.assertTrue(validate(bad, lines)["faults"], field)
        # A valid cadence may still miss the requested headroom. A probe can ask
        # for a different target without changing the cycle-accounting checks.
        bad = copy.deepcopy(result)
        bad["core"][0].update(busy=.75, busy_cycles=9600000)
        self.assertTrue(any("70.0%" in fault for fault in validate(bad, lines)["faults"]))
        self.assertFalse(validate(bad, lines, max_busy=1)["faults"])

    def test_starvation_requires_exhaustion_instead_of_accepting_any_failure(self):
        result, _, lines = fixture(starve=1)
        for change in ({"errors": 0}, {"pointer_errors": 0}, {"reason": "output fault"},
                       {"elapsed_us": 100000}, {"null_trigger_mask": 0},
                       {"errors": trial.SOURCE_EMPTY | 16}, {"pointer_errors": trial.SOURCE_EMPTY | 128},
                       {"published_frames": 3}):
            self.assertTrue(validate(dict(result, **change), lines)["faults"], change)
        for field, value in (("bad_crc", 1), ("bbframes", 3), ("bb_crc", [0] * 4),
                             ("gcs_underflows", 1), ("gcs_bytes", 0), ("crc_chain", 0)):
            bad = copy.deepcopy(result); bad["pv_spi"][field] = value
            self.assertTrue(validate(bad, lines)["faults"], field)
        result, _, _ = fixture(starve=1)
        result["cap_words"] = 0
        checked = validate(result, [])
        self.assertEqual(checked["faults"], [], checked)
        self.assertEqual(checked["capture_skipped"], "starvation_before_capture")
        self.assertEqual(checked["cap_checked_bus_words"], 0)

    def test_offline_default_and_explicit_self_test_never_construct_board(self):
        for argv in (["b8_lean8_trial.py"], ["b8_lean8_trial.py", "--self-test"]):
            with mock.patch.object(sys, "argv", argv), mock.patch.object(trial.iq, "Board") as board:
                with contextlib.redirect_stdout(io.StringIO()):
                    trial.main()
                board.assert_not_called()

    def test_missing_port_is_rejected_before_creating_evidence_directory(self):
        with tempfile.TemporaryDirectory(prefix="b8-runner-test-") as directory:
            target = Path(directory) / "never-created"
            argv = ["b8_lean8_trial.py", "--out", str(target)]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(trial.iq, "Board") as board:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    trial.main()
                board.assert_not_called()
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
