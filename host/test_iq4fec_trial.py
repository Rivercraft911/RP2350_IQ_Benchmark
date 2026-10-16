"""B3 runner dispatch and strict live GCS/FEC/FIR validation; never opens USB."""
import copy
import functools
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zlib

import numpy as np

import iq4fec_trial as trial


@functools.lru_cache(maxsize=8)
def capture_fixture(invert=0, symbol=3 * trial.FRAME_SYMBOLS - 300, bus_phase=3):
    """A real scalar FEC/rotated PLFRAME/FIR stream, spanning a frame boundary."""
    lo, hi = symbol - 40, symbol + 40
    base, reference = trial.reference_window(lo - 256, hi + 256)
    start = symbol * trial.BUS_PER_SYMBOL + bus_phase - base
    bus = reference[start + trial.STRIDE * np.arange(trial.CAP_WORDS)].astype('<u4')
    words = bus | ((((bus >> 14) & 1) ^ invert) << 28)
    meta, lines = trial.capture_lines(words)
    meta.update(cap_symbol_lo=lo, cap_symbol_hi=hi, cap_margin_symbols=256)
    return meta, words, lines


def fixture(cpw=2, invert=0, writers=2, schedule=0, clock=128000000):
    issued = math.ceil(clock / (8 * cpw) * 3 / 16) + 16
    granted = issued + 8192
    frames = math.ceil(granted * 16 / trial.FRAME_SYMBOLS)
    groups = frames * trial.FRAME_SYMBOLS // 16
    size = frames * ((trial.BB_BITS - 80) // 8)
    packets = (size + 187) // 188
    accepted = (packets + 6) // 7 + 24
    crc_fields = b''.join(trial.dual_spi.pv.message(k, trial.dual_spi.pv.pattern_payload(k))[-4:]
                          for k in range(16))
    stats = dict.fromkeys(('lost', 'bad_hdr', 'bad_crc', 'bad_sync', 'null_packets', 'short_msgs',
                          'long_msgs', 'overflows', 'gcs_underflows'), 0)
    stats.update(ok=accepted, bb_mode='gcs', bbframes=frames, runtime_bbframes=frames - 2,
                 runtime_messages=accepted - 24, ts_packets=0, gcs_packets=packets, gcs_bytes=size,
                 bb_crc=trial.dual_spi.first_crcs(gcs=True, rate='5/6'),
                 crc_chain=zlib.crc32((crc_fields * ((accepted + 15) // 16))[:4 * accepted]))
    meta, words, lines = capture_fixture(invert)
    r = dict(cmd='bench4fec', git='native-test-image', clk_hz=clock, cpw=cpw, ms=3000,
             invert=invert, source=6, code_index=8, s2_code='normal 5/6', modcod=15,
             kbch=trial.BB_BITS, pilots=True, L=7, sps=4, row_profile=trial.L7_PROFILE,
             row_symbols=1, row_words=2, writers=writers, dma_schedule=schedule,
             pointer_pio=0 if writers == 4 else 2,
             pointer_routing='alternating_dma' if writers == 4 else 'direct',
             frame_symbols=trial.FRAME_SYMBOLS, table_crc=trial.TABLE_CRC,
             restored=True, selectors_restored=True, errors=0, pointer_errors=0, output_errors=0,
             txstalls_i=0, txstalls_q=0, null_trigger_mask=0, bus_priority_mask=13,
             elapsed_us=3000000, sym_rate=clock / (8 * cpw), encoder_frames=frames,
             encoder_runtime_frames=frames - 2, input_published_groups=groups,
             input_issue_words=[issued, issued], granted_words=granted,
             published_words=groups, converted_groups=groups, min_credit_runway_words=8192,
             max_poll_gap_us=192, max_poll_interval_us=256, pv_spi=stats,
             **trial.live8.prefill_reference(), **meta)
    return r, words, lines


class Iq4fecTrial(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Memoize the immutable real reference; firmware conversion is never the oracle.
        cls.references = mock.patch.object(trial, 'reference_window',
                                           functools.lru_cache(maxsize=16)(trial.reference_window))
        cls.references.start()

    @classmethod
    def tearDownClass(cls):
        cls.references.stop()

    def validate(self, r, lines, cpw=2, invert=0, writers=2, schedule=0, clock=128000000):
        return trial.validate_run(r, lines, cpw, 3000, invert, 'native-test-image',
                                  writers=writers, dma_schedule=schedule, clock_hz=clock)

    def test_topology_schedule_divider_clock_matrix_uses_same_independent_samples(self):
        for cpw in (2, 4):
            for writers in (2, 4):
                for schedule in range(4):
                    for clock in (128000000, 256000000):
                        r, _, lines = fixture(cpw, writers=writers, schedule=schedule, clock=clock)
                        result = self.validate(r, lines, cpw, writers=writers, schedule=schedule, clock=clock)
                        self.assertEqual(result['faults'], [], (cpw, writers, schedule, clock, result))
                        self.assertEqual(result['classification'], 'digital_live_clean')
                        self.assertFalse(result['negative_control_detected'])
                        self.assertEqual(result['cap_checked_bus_words'], 4096)
                        self.assertEqual(result['cap_clock_checked'], 4096)

    def test_row_order_writer_parity_and_gpio_flag_are_observed_across_a_frame(self):
        for phase in range(trial.BUS_PER_SYMBOL * 2):
            r, _, _ = fixture(writers=4)
            meta, _, lines = capture_fixture(bus_phase=phase)
            r.update(meta)
            result = self.validate(r, lines, writers=4)
            self.assertEqual(result['faults'], [], (phase, result))
            self.assertEqual(result['cap_global_bus_offset'],
                             (3 * trial.FRAME_SYMBOLS - 300) * trial.BUS_PER_SYMBOL + phase)
        r, words, _ = fixture(writers=4)
        damaged = words.copy()
        # Swap observations on opposite row parity; keep transfer CRC valid.
        damaged[-2:] = damaged[-2:][::-1]
        meta, lines = trial.capture_lines(damaged)
        result = self.validate(dict(r, **meta), lines, writers=4)
        self.assertTrue(result['faults'])

    def test_identity_and_configuration_fail_closed(self):
        r, _, lines = fixture(writers=4, schedule=3)
        for change in (dict(cmd='dualptr4'), dict(git='other-image'), dict(writers=2),
                       dict(dma_schedule=0), dict(pointer_pio=2), dict(pointer_routing='direct'),
                       dict(clk_hz=256000000), dict(row_symbols=2), dict(row_words=4),
                       dict(table_crc=0), dict(prefill_crc=0), dict(source=5),
                       dict(code_index=9), dict(restored=False), dict(selectors_restored=False)):
            result = self.validate(dict(r, **change), lines, writers=4, schedule=3)
            self.assertTrue(result['faults'], change)
        for key in ('writers', 'dma_schedule', 'pointer_pio', 'pointer_routing', 'git'):
            changed = dict(r); del changed[key]
            self.assertTrue(self.validate(changed, lines, writers=4, schedule=3)['faults'], key)
        self.assertTrue(self.validate(r, lines)['faults'])
        self.assertTrue(self.validate(r, lines, writers=3)['faults'])

    def test_every_last_sample_bit_and_gpio28_are_checked(self):
        r, words, _ = fixture(writers=4)
        for bit in (0, 13, 14, 15, 28):
            damaged = words.copy(); damaged[-1] ^= 1 << bit
            meta, lines = trial.capture_lines(damaged)
            result = self.validate(dict(r, **meta), lines, writers=4)
            self.assertTrue(result['cap_transport_ok'])
            self.assertTrue(result['faults'], bit)
            self.assertEqual(result['cap_mismatches'], 0 if bit == 28 else 1)
            self.assertFalse(result['negative_control_detected'])

    def test_inverted_clock_control_needs_correct_data_and_all4096_clock_errors(self):
        for writers in (2, 4):
            r, words, lines = fixture(invert=1, writers=writers)
            result = self.validate(r, lines, invert=1, writers=writers)
            self.assertEqual(result['faults'], [], result)
            self.assertEqual(result['classification'], 'inverted_clock_detected')
            self.assertTrue(result['negative_control_detected'])
            self.assertEqual(result['cap_mismatches'], 0)
            self.assertEqual(result['cap_clock_mismatches'], 4096)
            self.assertTrue(self.validate(r, lines, writers=writers)['faults'])
            for bit in (0, 28):
                damaged = words.copy(); damaged[-1] ^= 1 << bit
                meta, lines = trial.capture_lines(damaged)
                result = self.validate(dict(r, **meta), lines, invert=1, writers=writers)
                self.assertTrue(result['faults'])
                self.assertFalse(result['negative_control_detected'])

    def test_capture_transport_and_search_metadata_fail_closed(self):
        r, _, lines = fixture()
        for damaged in (lines[:-1], [lines[1], lines[0]] + lines[2:]):
            self.assertTrue(self.validate(r, damaged)['faults'])
        for change in (dict(cap_crc=0), dict(cap_words=0), dict(cap_stride=1),
                       dict(cap_format='bus16'), dict(cap_symbol_hi=-500),
                       dict(cap_symbol_lo=None), dict(cap_symbol_hi=float('inf'))):
            self.assertTrue(self.validate(dict(r, **change), lines)['faults'], change)

    def test_fault_counters_finite_credits_stop_and_runtime_accounting_are_required(self):
        r, _, lines = fixture()
        for change in (dict(error='start rejected'), dict(errors=1), dict(pointer_errors=16),
                       dict(output_errors=1), dict(txstalls_i=1), dict(txstalls_q=1),
                       dict(null_trigger_mask=48), dict(elapsed_us=790000),
                       dict(encoder_frames=2), dict(encoder_runtime_frames=0),
                       dict(input_published_groups=0), dict(input_issue_words=[0, 0]),
                       dict(input_issue_words=[r['granted_words'] + 1, r['granted_words']]),
                       dict(min_credit_runway_words=0), dict(max_poll_gap_us=257),
                       dict(published_words=r['converted_groups'] + 1)):
            self.assertTrue(self.validate(dict(r, **change), lines)['faults'], change)
        for key in ('elapsed_us', 'input_issue_words', 'max_poll_gap_us', 'pv_spi'):
            for value in (None, 'bad', [], float('nan')):
                self.assertTrue(self.validate(dict(r, **{key: value}), lines)['faults'], (key, value))
        for value in (float('nan'), float('inf'), -float('inf')):
            self.assertTrue(self.validate(dict(r, sym_rate=value), lines)['faults'])

    def test_gcs_bbframes_and_spi_crc_provenance_are_required(self):
        r, _, lines = fixture()
        for key, value in (('lost', 1), ('bad_crc', 1), ('gcs_underflows', 1), ('drain_timeouts', 1),
                           ('runtime_messages', 0), ('runtime_bbframes', 0), ('bbframes', 0),
                           ('bb_crc', [0] * 4), ('crc_chain', 0), ('gcs_bytes', 0)):
            changed = copy.deepcopy(r); changed['pv_spi'][key] = value
            self.assertTrue(self.validate(changed, lines)['faults'], key)

    def run_cli(self, extra=(), failed=False, clock_error=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build, out = root / 'build', root / 'out'
            build.mkdir()
            for name in ('iqbench.uf2', 'iqbench.elf', 'memmap_iq.ld', 'git_rev.h'):
                (build / name).write_bytes(b'native runner fixture')
            (build / 'CMakeCache.txt').write_text('IQ4HIST_EXPERIMENT:BOOL=ON\n')
            board = mock.Mock()
            def cmd(line, **_kwargs):
                return ({'error': 'unsupported clock'} if clock_error and line.startswith('clock ')
                        else {'bus_priority_mask': 13} if line.startswith('bench4fec ') else {}), []
            board.cmd.side_effect = cmd
            argv = ['iq4fec_trial.py', '--out', str(out), '--build', str(build), '--prios', '13',
                    '--expected-git', 'native-test-image', *extra]
            result = dict(faults=['injected failure'] if failed else [],
                          classification='failed' if failed else 'digital_live_clean',
                          negative_control_detected=False)
            with mock.patch.object(sys, 'argv', argv), mock.patch.object(trial.iq, 'Board', return_value=board), \
                    mock.patch.object(trial, 'validate_run', return_value=result) as validate, \
                    mock.patch('builtins.print'):
                if failed or clock_error:
                    with self.assertRaises(SystemExit):
                        trial.main()
                else:
                    trial.main()
            manifest = json.loads((out / 'manifest.json').read_text())
            runs = [json.loads(line) for line in (out / 'runs.jsonl').read_text().splitlines()]
            snapshots = sorted(path.name for path in out.glob('*.txt'))
            board.s.close.assert_called_once()
            commands = [call.args[0] for call in board.cmd.call_args_list]
            self.assertEqual(commands[-2:], ['ldpcpack 0', 'bchkernel 0'])
            return commands, validate.call_args_list, manifest, runs, snapshots

    def test_cli_defaults_to_two_writers_and_archives_unique_control_labels(self):
        commands, validations, manifest, runs, snapshots = self.run_cli()
        self.assertIn('clock 128000', commands)
        self.assertIn('bench4fec 2 3000 0 13 2 0', commands)
        self.assertIn('bench4fec 2 3000 1 13 2 0', commands)
        self.assertEqual(len(validations), 2)
        self.assertTrue(all(call.kwargs == dict(writers=2, dma_schedule=0, clock_hz=128000000)
                            for call in validations))
        self.assertEqual(manifest['writers'], [2])
        self.assertEqual(manifest['dma_schedules'], [0])
        self.assertEqual(manifest['clocks_khz'], [128000])
        self.assertIn('host/iq4fec_trial.py', manifest['sha256'])
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(len([r for r in runs if r['label'].startswith('bench4fec-')]), 2)

    def test_cli_dispatches_clock_writer_schedule_matrix_and_retains_identity(self):
        commands, validations, manifest, _runs, snapshots = self.run_cli([
            '--writers', '2', '4', '--schedules', '0', '1', '2', '3', '--clocks', '128000', '256000',
            '--cpws', '2', '4', '--inverts', '0', '--ms', '60000'])
        self.assertEqual(len(validations), 32)
        self.assertEqual(len(snapshots), 32)
        for clock in (128000, 256000):
            self.assertIn(f'clock {clock}', commands)
            for writers in (2, 4):
                for schedule in range(4):
                    for cpw in (2, 4):
                        self.assertIn(f'bench4fec {cpw} 60000 0 13 {writers} {schedule}', commands)
        self.assertEqual({call.kwargs['clock_hz'] for call in validations}, {128000000, 256000000})
        self.assertEqual(manifest['writers'], [2, 4])
        self.assertEqual(manifest['dma_schedules'], [0, 1, 2, 3])
        self.assertEqual(manifest['clocks_khz'], [128000, 256000])

    def test_cli_failures_exit_nonzero_after_restoring_selectors_and_closing(self):
        commands, _, _, _, _ = self.run_cli(failed=True)
        self.assertTrue(any(command.startswith('bench4fec ') for command in commands))
        commands, validations, _, _, _ = self.run_cli(clock_error=True)
        self.assertFalse(validations)
        self.assertFalse(any(command.startswith('bench4fec ') for command in commands))


if __name__ == '__main__':
    unittest.main()
