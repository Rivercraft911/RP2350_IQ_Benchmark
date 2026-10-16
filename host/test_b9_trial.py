"""Offline independent GCS/FEC/PL/FIR and strict B9 capture checks; no USB."""
from __future__ import annotations

import copy
import contextlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zlib

import numpy as np

import b9_trial as trial
from pulse_profiles import iq4hist8_rows


def capture_fixture(symbol=0, phase=3, invert=0, code_index=5):
    cursor = symbol // 16
    first = max(0, cursor * 16 - 4096)
    reference = trial.reference_bus(first, 32768, code_index)
    offset = (symbol - first) * trial.BUS_PER_SYMBOL + phase
    words = reference[offset + trial.STRIDE * np.arange(trial.CAP_WORDS)].astype('<u4')
    words |= ((((words >> 14) & 1) ^ invert) << 28)
    metadata, lines = trial.capture_lines(words)
    metadata['cap_source_word'] = cursor
    metadata['code_index'] = code_index
    return metadata, words, lines


def run_fixture(cpw=1, ms=1000, invert=0, priority=13, slow_us=0, options=251, code_index=5, sram_hot=1):
    rate = trial.CLOCK_HZ / (8 * cpw)
    elapsed = 2800 * cpw if slow_us else ms * 1000
    accounting = elapsed + 250
    stream = 2 if slow_us else math.floor(rate * elapsed / 1e6 / trial.FRAME_SYMBOLS) + 2
    frames = stream + 2
    groups = stream * trial.FRAME_SYMBOLS // 16
    completed = groups if slow_us else math.floor(rate * elapsed / 16e6 / 512) * 512
    capture_symbol = 16 * (math.floor(rate * .0005 / 16) if slow_us else
                           math.floor(rate * ms / 32000 / 512) * 512)
    record, words, lines = capture_fixture(capture_symbol, invert=invert, code_index=code_index)
    record.update(cmd='b9', clk_hz=trial.CLOCK_HZ, cpw=cpw, ms=ms, invert=invert,
                  bus_priority=priority, slow_us=slow_us, options=options,
                  sram_hot=sram_hot,
                  source='memory_gcs', source_period_bytes=256, code_index=code_index,
                  s2_code='normal ' + trial.RATES[code_index], kbch=trial.KBCH[code_index],
                  payload_bits=trial.KBCH[code_index] - 80,
                  payload_bps=(trial.KBCH[code_index] - 80) * rate / trial.FRAME_SYMBOLS,
                  pilots=True, gold=0, frame_symbols=trial.FRAME_SYMBOLS,
                  frame_cycles=trial.FRAME_SYMBOLS * 8 * cpw, ring_groups=4096, sample_bits=8,
                  sps=4, L=7, row_profile=trial.L7_PROFILE, row_symbols=1, row_words=1,
                  writers=4, producers=4, chains_per_axis=2, pointer_source='pio',
                  table_bytes=65536, table_crc=trial.table_crc(8), previous_words=[0, 0],
                  grant_mode='finite_irq_rearm', batch_words=512, cursor_kind='completed_batch_words',
                  table_base=0x20000000, context_base=0x20010000, input_base=0x20020000,
                  context_bytes=49152, grant_irq_calls=100, grant_irq_cycles=100000,
                  grant_irq_max_cycles=1100, grant_irq_index=2,
                  supervise_cycles=10000, supervise_busy=10000 / (accounting * trial.CLOCK_HZ / 1e6),
                  source_empty=bool(slow_us), restored=True, selectors_restored=True,
                  resources_released=True, git='offline-b9', elapsed_us=elapsed,
                  accounting_elapsed_us=accounting,
                  published_frames=frames, stream_frames=stream, stream_groups=groups,
                  published_words=groups, reusable_words=completed, max_service_gap_us=64,
                  errors=(1 << 21) | (1 << 20) | 3 if slow_us else 0, pointer_errors=64 if slow_us else 0,
                  pointer_txstall_mask=1 if slow_us else 0,
                  output_errors=3 if slow_us else 0, txstalls_i=3 if slow_us else 0,
                  txstalls_q=3 if slow_us else 0, sym_rate=rate,
                  input_issue_words=[completed] * 4, completed_words=[completed] * 4,
                  granted_words=[groups if slow_us else min(groups, completed + 512)] * 4,
                  reason='SOURCE_EMPTY' if slow_us else 'complete',
                  core=[dict(frames=(frames + 1 - index) // 2,
                             busy_cycles=300000 * ((frames + 1 - index) // 2),
                             timed_busy_cycles=10000,
                             busy=(10000 if index else 20000) / (accounting * trial.CLOCK_HZ / 1e6),
                             max_frame_cycles=300000, max_slice_cycles=8000,
                             stage_cycles={key: amount * ((frames + 1 - index) // 2) for key, amount in
                                           dict(gcs=10000, bch=100000, ldpc=130000, map=60000, delay=0).items()})
                        for index in range(2)])
    record.update(trial.fec.ring_reference(code_index, frames, 4096))
    return record, words, lines


class B9Reference(unittest.TestCase):
    def test_direct_fir_matches_all_history_bits_and_frame_carry(self):
        self.assertEqual(zlib.crc32(trial.reference_bus(0, 8704).tobytes()), 0x6e67a0a0)
        table = iq4hist8_rows().astype('<u4')
        for symbol in (0, 1, 15, 16, trial.FRAME_SYMBOLS - 6,
                       trial.FRAME_SYMBOLS, 2 * trial.FRAME_SYMBOLS - 3):
            frames = (symbol + 31) // trial.FRAME_SYMBOLS + 1
            codes = np.concatenate([trial.fec.frame_codes(index, 5) for index in range(frames)], axis=1)
            expected = np.empty(32 * 8, dtype='<u2')
            for axis in range(2):
                keys = [sum((int(codes[axis, index - age]) if index >= age else 0) << (2 * age)
                            for age in range(7)) for index in range(symbol, symbol + 32)]
                samples = table[keys].view(np.uint8).reshape(-1).astype(np.uint16) * 64
                expected[axis::2] = (samples & 0x3fff) | (0x4000 if axis == 0 else 0)
            np.testing.assert_array_equal(trial.reference_bus(symbol, 32), expected)
        full = trial.reference_bus(trial.FRAME_SYMBOLS - 12, 96)
        np.testing.assert_array_equal(trial.reference_bus(trial.FRAME_SYMBOLS + 1, 32), full[13 * 8:45 * 8])

    def test_finite_capture_startup_frame_boundaries_and_large_cursor(self):
        for symbol in (0, 17, trial.FRAME_SYMBOLS - 300, 3 * trial.FRAME_SYMBOLS + 15,
                       43210 * trial.FRAME_SYMBOLS + 123):
            for invert in (0, 1):
                metadata, _, lines = capture_fixture(symbol, invert=invert)
                checked = trial.validate_capture(metadata, lines, invert)
                self.assertTrue(checked['cap_transport_ok'] and checked['cap_aligned'], checked)
                self.assertEqual(checked['cap_mismatches'], 0, checked)
                self.assertEqual(checked['cap_global_bus_offset'], symbol * 8 + 3)
                self.assertEqual(checked['cap_checked_bus_words'], trial.CAP_WORDS)
                self.assertTrue(checked['cap_clock_ok'], checked)

    def test_every_bus_bit_clock_and_late_capture_sample_is_checked(self):
        metadata, words, _ = capture_fixture(trial.FRAME_SYMBOLS - 500)
        for bit in (0, 5, 6, 13, 14, 15, 28):
            changed = words.copy()
            changed[-1] ^= 1 << bit
            transport, lines = trial.capture_lines(changed)
            checked = trial.validate_capture(dict(metadata, **transport), lines, 0)
            self.assertEqual(checked['cap_mismatches'], 0 if bit == 28 else 1, checked)
            self.assertEqual(checked['cap_clock_ok'], bit not in (14, 28), checked)
        words |= 0x08010000  # Unrelated GPIOs cannot affect bus or clock checks.
        transport, lines = trial.capture_lines(words)
        self.assertEqual(trial.validate_capture(dict(metadata, **transport), lines, 0)['cap_mismatches'], 0)

    def test_transport_and_bounds_reject_missing_stale_and_wrapped_samples(self):
        metadata, _, lines = capture_fixture(4 * trial.FRAME_SYMBOLS)
        for broken in (lines[:-1], lines[1:], [lines[1], lines[0], *lines[2:]], lines + [lines[-1]]):
            self.assertFalse(trial.validate_capture(metadata, broken, 0)['cap_transport_ok'])
        self.assertFalse(trial.validate_capture(dict(metadata, cap_crc=metadata['cap_crc'] ^ 1), lines, 0)
                         ['cap_transport_ok'])
        for change in (dict(cap_stride=16), dict(cap_format='pairs'), dict(cap_source_word=-1),
                       dict(cap_source_word=True), dict(cap_words=4095)):
            self.assertFalse(trial.validate_capture(dict(metadata, **change), lines, 0)['cap_transport_ok'])
        stale_meta, _, stale_lines = capture_fixture(4 * trial.FRAME_SYMBOLS % 65536)
        self.assertFalse(trial.validate_capture(dict(metadata, cap_crc=stale_meta['cap_crc']), stale_lines, 0)
                         ['cap_aligned'])
        base, reference, _, _ = trial.capture_window(metadata)
        offset = len(reference) - 100
        wrapped = reference[(offset + trial.STRIDE * np.arange(trial.CAP_WORDS)) % len(reference)].astype('<u4')
        wrapped |= ((wrapped >> 14) & 1) << 28
        transport, wrapped_lines = trial.capture_lines(wrapped)
        self.assertFalse(trial.validate_capture(dict(metadata, **transport), wrapped_lines, 0)['cap_aligned'])

    def test_even_odd_axis_swaps_and_per_frame_padding_are_not_independent_phases(self):
        symbol = trial.FRAME_SYMBOLS - 200
        metadata, _, _ = capture_fixture(symbol)
        first = max(0, metadata['cap_source_word'] * 16 - 4096)
        bus = trial.reference_bus(first, 32768).reshape(-1, 4, 2)
        swapped_parity = bus.copy()
        swapped_parity[0::2], swapped_parity[1::2] = bus[1::2], bus[0::2]
        swapped_axes = bus[:, :, ::-1].copy()
        swapped_axes[:, :, 0] |= 0x4000
        swapped_axes[:, :, 1] &= 0x3fff
        padded_codes = np.concatenate([np.pad(trial.fec.frame_codes(index, 5), ((0, 0), (0, 14)))
                                       for index in range(3)], axis=1)
        table = iq4hist8_rows().astype('<u4')
        padded_bus = np.empty(padded_codes.shape[1] * 8, dtype='<u2')
        indices = np.arange(padded_codes.shape[1])[:, None] - np.arange(7)
        for axis in range(2):
            history = padded_codes[axis][np.maximum(0, indices)]
            history = np.where(indices < 0, 0, history)
            keys = (history << (2 * np.arange(7, dtype=np.uint32))).sum(axis=1)
            samples = table[keys].view(np.uint8).astype(np.uint16) * 64
            padded_bus[axis::2] = (samples & 0x3fff) | (0x4000 if axis == 0 else 0)
        offset = (symbol - first) * 8 + 3
        for other in (swapped_parity.ravel(), swapped_axes.ravel(), padded_bus[first * 8:]):
            words = other[offset + trial.STRIDE * np.arange(trial.CAP_WORDS)].astype('<u4')
            words |= ((words >> 14) & 1) << 28
            transport, lines = trial.capture_lines(words)
            checked = trial.validate_capture(dict(metadata, **transport), lines, 0)
            self.assertTrue(not checked['cap_aligned'] or checked['cap_mismatches'] > 0, checked)

    def test_invalid_finite_source_arguments(self):
        for first, length in ((-1, 32), (True, 32), (0, 0), (0, 1.0)):
            with self.assertRaises(ValueError):
                trial.reference_bus(first, length)
        for code_index in (0, 4, 7, 9, True, '5', 5.0):
            with self.assertRaises(ValueError):
                trial.reference_bus(0, 32, code_index)

    def test_all_rates_preserve_unpadded_frame_seams_and_select_capture_reference(self):
        # Construct each capture across a genuine PLFRAME/FIR-history seam.
        # Validation must reject a different normal rate even though the ring,
        # frame length, clock, header cadence and transport CRC are all plausible.
        for code_index in (5, 8, 6):
            with self.subTest(code=code_index):
                self.assertEqual(trial.KBCH[code_index],
                                 trial.fec.ref.code(trial.RATES[code_index], False).kbch)
                symbol = 7 * trial.FRAME_SYMBOLS - 137
                metadata, _, lines = capture_fixture(symbol, code_index=code_index)
                checked = trial.validate_capture(metadata, lines, 0)
                self.assertEqual(checked['cap_mismatches'], 0, checked)
                self.assertEqual(checked['cap_global_bus_offset'], symbol * 8 + 3)
                full = trial.reference_bus(trial.FRAME_SYMBOLS - 12, 96, code_index)
                np.testing.assert_array_equal(trial.reference_bus(trial.FRAME_SYMBOLS + 1, 32, code_index),
                                              full[13 * 8:45 * 8])
                wrong = trial.validate_capture(metadata, lines, 0, 8 if code_index == 5 else 5)
                self.assertTrue(not wrong['cap_aligned'] or wrong['cap_mismatches'] > 0, wrong)


class B9Protocol(unittest.TestCase):
    def test_c9_masks_new_rates_and_independent_payload_metadata(self):
        for code_index, payload, mbps in ((5, 42960, 30.97053), (8, 53760, 38.75642),
                                          (6, 48328, 34.84041)):
            for options in (251, 4603, 6395, 12795):
                with self.subTest(code=code_index, options=options):
                    record, _, lines = run_fixture(code_index=code_index, options=options)
                    self.assertEqual(record['payload_bits'], payload)
                    self.assertAlmostEqual(record['payload_bps'] / 1e6, mbps, places=4)
                    checked = trial.validate_run(record, lines, 1, 1000, 0,
                                                 options=options, code_index=code_index)
                    self.assertEqual(checked['faults'], [], checked)
                    self.assertEqual(checked['classification'],
                                     'complete_8psk_' + trial.RATES[code_index].replace('/', '_') + '_clean')
            for invert, slow_us in ((1, 0), (0, 5000)):
                record, _, lines = run_fixture(code_index=code_index, options=6395,
                                               invert=invert, slow_us=slow_us)
                checked = trial.validate_run(record, lines, 1, 1000, invert, slow_us=slow_us,
                                             options=6395, code_index=code_index)
                self.assertEqual(checked['faults'], [], checked)
            record, _, lines = run_fixture(code_index=code_index)
            for change in (dict(code_index=8 if code_index == 5 else 5), dict(s2_code='normal 1/2'),
                           dict(kbch=payload), dict(payload_bits=payload + 80),
                           dict(payload_bps=record['payload_bps'] * 2), dict(payload_bps=float('nan')),
                           dict(code_crc=trial.fec.ring_reference(8 if code_index == 5 else 5,
                                                               record['published_frames'], 4096)['code_crc'])):
                checked = trial.validate_run(dict(record, **change), lines, 1, 1000, 0, code_index=code_index)
                self.assertTrue(checked['faults'], change)
            for key in ('kbch', 'payload_bits', 'payload_bps'):
                changed = dict(record)
                del changed[key]
                checked = trial.validate_run(changed, lines, 1, 1000, 0, code_index=code_index)
                self.assertEqual(bool(checked['faults']), code_index != 5, checked)

    def test_rates_priorities_options_and_negative_controls(self):
        for cpw in (1, 2):
            for priority in (0, 13):
                for options in (0, 251):
                    for invert, slow_us in ((0, 0), (1, 0), (0, 5000)):
                        record, _, lines = run_fixture(cpw, invert=invert, priority=priority,
                                                       options=options, slow_us=slow_us)
                        checked = trial.validate_run(record, lines, cpw, 1000, invert, priority,
                                                     slow_us, options, 'offline-b9')
                        self.assertEqual(checked['faults'], [], checked)
                        self.assertEqual(checked['classification'], 'source_underrun_detected' if slow_us else
                                         'clock_inversion_detected' if invert else 'complete_8psk_2_3_clean')

    def test_legacy_logs_and_explicit_hot_placement_are_distinguished(self):
        record, _, lines = run_fixture()
        for key in ('kbch', 'payload_bits', 'payload_bps', 'sram_hot'):
            del record[key]
        self.assertEqual(trial.validate_run(record, lines, 1, 1000, 0)['faults'], [])
        record, _, lines = run_fixture(sram_hot=0)
        self.assertEqual(trial.validate_run(record, lines, 1, 1000, 0, sram_hot=0)['faults'], [])
        self.assertTrue(trial.validate_run(record, lines, 1, 1000, 0, sram_hot=1)['faults'])
        del record['sram_hot']
        self.assertTrue(trial.validate_run(record, lines, 1, 1000, 0, sram_hot=0)['faults'])

    def test_frame_publication_and_finite_grants_fail_closed(self):
        record, _, lines = run_fixture()
        for change in (dict(published_groups=record['published_groups'] + 1),
                       dict(pending_fill=0), dict(code_crc=[0, 0]),
                       dict(published_words=record['published_groups']),
                       dict(stream_groups=record['stream_groups'] + 1),
                       dict(stream_frames=record['stream_frames'] + 1),
                       dict(completed_words=[record['published_words'] + 1] * 4),
                       dict(granted_words=[record['published_words'] + 1] * 4),
                       dict(reusable_words=record['reusable_words'] + 1),
                       dict(input_issue_words=[0] * 4), dict(granted_words=[0] * 3),
                       dict(cap_source_word=record['reusable_words'] + 512),
                       dict(previous_words=[0, 2]), dict(grant_mode='endless_immutable'),
                       dict(errors=1), dict(output_errors=1), dict(pointer_errors=1),
                       dict(source_empty=True), dict(max_service_gap_us=129),
                       dict(grant_irq_calls=0), dict(grant_irq_cycles=0), dict(grant_irq_index=1),
                       dict(supervise_busy=float('nan')), dict(context_bytes=65537),
                       dict(accounting_elapsed_us=record['elapsed_us'] - 1),
                       dict(restored=False), dict(sym_rate=float('nan'))):
            checked = trial.validate_run(dict(record, **change), lines, 1, 1000, 0)
            self.assertTrue(checked['faults'], change)
        for key in ('core', 'source', 'published_frames', 'published_groups', 'stream_frames',
                    'stream_groups', 'code_crc', 'granted_words', 'completed_words', 'input_issue_words',
                    'accounting_elapsed_us'):
            changed = copy.deepcopy(record)
            del changed[key]
            self.assertTrue(trial.validate_run(changed, lines, 1, 1000, 0)['faults'], key)
        for value in (None, True, -1, float('nan'), float('inf'), '72'):
            self.assertTrue(trial.validate_run(dict(record, published_frames=value), lines, 1, 1000, 0)
                            ['faults'], value)

    def test_slow_control_must_expose_underrun_and_inverted_clock_must_be_real(self):
        record, _, lines = run_fixture(slow_us=5000)
        for change in (dict(source_empty=False), dict(errors=0), dict(pointer_errors=0),
                       dict(reason='complete'), dict(elapsed_us=1000000),
                       dict(errors=record['errors'] | (1 << 18)), dict(output_errors=4),
                       dict(pointer_errors=record['pointer_errors'] | 1), dict(pointer_txstall_mask=0)):
            checked = trial.validate_run(dict(record, **change), lines, 1, 1000, 0, slow_us=5000)
            self.assertTrue(checked['faults'], change)
        record, _, lines = run_fixture(invert=0)
        checked = trial.validate_run(dict(record, invert=1), lines, 1, 1000, 1)
        self.assertIn('DDR clock phase differs from requested control', checked['faults'])

    def test_cli_is_opt_in_preserves_evidence_and_dispatches_exact_b9_arguments(self):
        with tempfile.TemporaryDirectory(prefix='b9-runner-') as directory:
            temporary = Path(directory)
            build = temporary / 'build'
            build.mkdir()
            for name in ('iqbench.uf2', 'iqbench.elf', 'memmap_iq.ld', 'git_rev.h'):
                (build / name).write_text('synthetic ' + name + '\n')
            (build / 'CMakeCache.txt').write_text('IQ4HIST_EXPERIMENT:BOOL=ON\n')
            (build / 'build.ninja').write_text('PICO_STACK_SIZE=0x1000\n')
            board = Mock()

            def command(text, **kwargs):
                if text == 'info':
                    return dict(cmd='info', clk_hz=trial.CLOCK_HZ, git='offline-b9'), []
                name, cpw, ms, invert, priority, slow_us, options, *codes = text.split()
                self.assertEqual(name, 'b9')
                self.assertLessEqual(len(codes), 2)
                record, _, lines = run_fixture(int(cpw), int(ms), int(invert), int(priority),
                                               int(slow_us), int(options), int(codes[0]) if codes else 5,
                                               int(codes[1]) if len(codes) == 2 else 1)
                return record, lines

            board.cmd.side_effect = command
            output = temporary / 'results'
            with patch.object(trial.iq, 'Board', return_value=board) as constructor, \
                 patch.object(trial.subprocess, 'check_output', return_value='offline-host\n'), \
                 contextlib.redirect_stdout(io.StringIO()):
                trial.main(['--out', str(output), '--port', 'synthetic-only', '--build', str(build),
                            '--ms', '1000', '--cpws', '1', '2', '--priorities', '0', '13',
                            '--options', '0', '251', '--expected-git', 'offline-b9'])
            constructor.assert_called_once_with('synthetic-only')
            self.assertEqual(board.cmd.call_count, 9)
            board.s.close.assert_called_once_with()
            records = [json.loads(line) for line in (output / 'runs.jsonl').read_text().splitlines()]
            self.assertEqual(len(records), 8)
            self.assertTrue(all(not record['faults'] for record in records))
            manifest = json.loads((output / 'manifest.json').read_text())
            self.assertEqual(manifest['head'], 'offline-host')
            self.assertEqual(manifest['codes'], [5])
            self.assertIn('host/b9_trial.py', manifest['sha256'])
            with patch.object(trial.iq, 'Board') as unopened, self.assertRaises(FileExistsError):
                trial.main(['--out', str(output), '--port', 'synthetic-only', '--build', str(build)])
            unopened.assert_not_called()
            board.reset_mock()
            rates_output = temporary / 'rates-results'
            with patch.object(trial.iq, 'Board', return_value=board), \
                 patch.object(trial.subprocess, 'check_output', return_value='offline-host\n'), \
                 contextlib.redirect_stdout(io.StringIO()):
                trial.main(['--out', str(rates_output), '--port', 'synthetic-only', '--build', str(build),
                            '--ms', '1000', '--cpws', '1', '--priorities', '13', '--codes', '5', '8', '6',
                            '--options', '251', '4603', '6395', '12795', '--expected-git', 'offline-b9'])
            self.assertEqual([call.args[0] for call in board.cmd.call_args_list],
                             ['info'] + [f'b9 1 1000 0 13 0 {options} {code}'
                                         for code in (5, 8, 6) for options in (251, 4603, 6395, 12795)])
            records = [json.loads(line) for line in (rates_output / 'runs.jsonl').read_text().splitlines()]
            self.assertEqual(len(records), 12)
            self.assertTrue(all(not record['faults'] for record in records))
            self.assertTrue(all(record['label'].endswith('-code' + str(record['code_index'])) for record in records))
            rates_manifest = json.loads((rates_output / 'manifest.json').read_text())
            self.assertEqual(rates_manifest['codes'], [5, 8, 6])
            self.assertEqual(rates_manifest['rates'], {'5': '2/3', '8': '5/6', '6': '3/4'})
            board.reset_mock()
            hot_output = temporary / 'hot-control-results'
            with patch.object(trial.iq, 'Board', return_value=board), \
                 patch.object(trial.subprocess, 'check_output', return_value='offline-host\n'), \
                 contextlib.redirect_stdout(io.StringIO()):
                trial.main(['--out', str(hot_output), '--port', 'synthetic-only', '--build', str(build),
                            '--ms', '1000', '--cpws', '1', '--priorities', '13', '--sram-hot', '0'])
            self.assertEqual([call.args[0] for call in board.cmd.call_args_list], ['info', 'b9 1 1000 0 13 0 251 5 0'])
            record = json.loads((hot_output / 'runs.jsonl').read_text())
            self.assertEqual(record['faults'], [])
            self.assertEqual(record['sram_hot'], 0)
        for options in ([], ['--out', '/unused'], ['--port', 'synthetic-only'],
                        ['--out', '/unused', '--port', 'synthetic-only', '--ms', '99'],
                        ['--out', '/unused', '--port', 'synthetic-only', '--options', '1024'],
                        ['--out', '/unused', '--port', 'synthetic-only', '--options', '2304'],
                        ['--out', '/unused', '--port', 'synthetic-only', '--options', '16384'],
                        ['--out', '/unused', '--port', 'synthetic-only', '--sram-hot', '2'],
                        ['--out', '/unused', '--port', 'synthetic-only', '--codes', '7']):
            with patch.object(trial.iq, 'Board') as unopened, contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit):
                trial.main(options)
            unopened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
