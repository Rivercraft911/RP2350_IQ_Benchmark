"""Strict B5 GPIO/reference/metadata/CLI tests; every board is synthetic."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zlib

import numpy as np

import iq4quad_trial as trial


def capture_lines(values):
    return ['@cap '+str(k)+' '+' '.join(f'{int(v):08x}' for v in values[k:k+16])
            for k in range(0, len(values), 16)]


def fixture(cpw=1, invert=0, bits=8, priority=13, offset=None):
    source, bus = trial.reference_source(bits)
    if offset is None:
        offset = len(bus)-67
    values = bus[(offset+17*np.arange(trial.CAP_WORDS)) % len(bus)].astype('<u4')
    values |= ((((values >> 14) & 1) ^ invert) << 28)
    elapsed, cycles = 1000000, 3000000
    pointer_crc = zlib.crc32(trial.reference_pointers(bits).tobytes())
    r = dict(cmd='bench4quad8' if bits == 8 else 'bench4quad', git='frozen-test-image',
             clk_hz=trial.CLOCK_HZ, source='cyclic_8psk', cpw=cpw, divider256=256*cpw,
             ms=1000, invert=invert, sps=4, L=7, row_profile=trial.L7_PROFILE,
             row_symbols=1, row_words=1 if bits == 8 else 2, sample_bits=bits, low_bits=0,
             writers=4, chains_per_axis=2, pointer_source='sram',
             period_symbols=trial.PERIOD_SYMBOLS, seed=trial.SEED, in_words=trial.INPUT_WORDS,
             input_words=2*trial.INPUT_WORDS, input_base=trial.POINTER_BASE,
             pointer_base=trial.POINTER_BASE, source_base=0x20040000,
             pointer_words=trial.POINTER_WORDS, pointer_ring_words=trial.POINTER_RING_WORDS,
             pointer_bases=[trial.POINTER_BASE+4*trial.POINTER_RING_WORDS*j for j in range(4)],
             pointer_crc=pointer_crc, pointer_after_crc=pointer_crc,
             previous_words=[int(source[trial.INPUT_WORDS-1]), int(source[-1])],
             input_crc=zlib.crc32(source.tobytes()),
             ring_base=trial.TABLE_BASE, table_base=trial.TABLE_BASE, ring_bytes=131072,
             table_bytes=65536 if bits == 8 else 131072, table_crc=trial.table_crc(bits),
             bus_priority=priority, errors=0, pointer_errors=0, output_errors=0,
             txstalls_i=0, txstalls_q=0, restored=True, selectors_restored=True,
             resources_released=True, core1_idle=True, sample_math_busy=0.0,
             descriptor_gen_busy=0.0, idle_wait='spin', reason='complete',
             dac_clock_pio=0, dac_clock_sm=0,
             elapsed_us=elapsed, sym_rate=trial.CLOCK_HZ/(8*cpw),
             supervision_cycles=cycles, supervision_busy=cycles/(elapsed*trial.CLOCK_HZ/1e6),
             polls=elapsed//trial.POLL_US, poll_interval_us=trial.POLL_US,
             max_poll_interval_us=trial.MAX_POLL_US, max_poll_gap_us=129,
             cap_format='pins32', cap_words=trial.CAP_WORDS, capture_count=trial.CAP_WORDS,
             cap_crc=zlib.crc32(values.tobytes()), cap_stride=17,
             cap_divider256=256*cpw, cap_fifo_only=False)
    return r, values, capture_lines(values)


class QuadRowTrial(unittest.TestCase):
    def test_rates_widths_priorities_clock_polarities_and_ring_wrap(self):
        for bits in (8, 14):
            for cpw in (1, 2, 4):
                for priority in (0, 13):
                    for invert in (0, 1):
                        r, values, lines = fixture(cpw, invert, bits, priority)
                        checked = trial.validate_run(r, lines, cpw, 1000, invert,
                                                     expected_git='frozen-test-image', bits=bits, priority=priority)
                        self.assertEqual(checked['faults'], [])
                        self.assertEqual(checked['cap_checked_bus_words'], 4096)
                        self.assertEqual(checked['cap_clock_checked'], 4096)
                        self.assertEqual(checked['cap_clock_mismatches'], 4096*invert)
                        self.assertEqual(checked['classification'],
                                         'clock_inversion_detected' if invert else 'digital_quad_clean')
                        if bits == 8:
                            self.assertTrue(np.all((values & 63) == 0))

    def test_scalar_fir_period_and_deinterleaved_pointer_wrap(self):
        codes = trial.reference_codes()
        source, bus = trial.reference_source()
        self.assertEqual(len(source), 512)
        self.assertEqual(len(bus), 32768)
        for bits in (8, 14):
            pointers = trial.reference_pointers(bits).reshape(4, trial.POINTER_RING_WORDS)
            stride = 4 if bits == 8 else 8
            for axis in range(2):
                # Check every deinterleaved row; use scalar wrap and bit positions.
                for parity in (0, 1):
                    expected = [trial.TABLE_BASE+stride*sum(int(codes[axis][(symbol-age) % 4096]) << (2*age)
                                                         for age in range(7))
                                for symbol in range(parity, 4096, 2)]
                    np.testing.assert_array_equal(pointers[2*parity+axis], expected)
            # Frozen geometric PRBS descriptors assert the public stream order.
            # Both first and next entries depend on cyclic prehistory.
            first = ([0x2000551c, 0x2000fc48, 0x20005478, 0x2000f120] if bits == 8 else
                     [0x2000aa38, 0x2001f890, 0x2000a8f0, 0x2001e240])
            second = ([0x200051e8, 0x2000c48c, 0x200047ac, 0x20001234] if bits == 8 else
                      [0x2000a3d0, 0x20018918, 0x20008f58, 0x20002468])
            np.testing.assert_array_equal(pointers[:, 0], first)
            np.testing.assert_array_equal(pointers[:, 1], second)
            self.assertEqual(zlib.crc32(pointers.tobytes()),
                             4057852319 if bits == 8 else 2212114013)
        shifts = 2*np.arange(16, dtype=np.uint32)
        for axis in range(2):
            unpacked = ((source[axis*256:(axis+1)*256, None] >> shifts) & 3).ravel()
            np.testing.assert_array_equal(unpacked, codes[axis])
        # First row really uses the final six symbols, not reset history.
        self.assertNotEqual(int(trial.reference_pointers()[0]),
                            trial.TABLE_BASE+4*int(codes[0][0]))

    def test_capture_rejects_even_odd_writer_swap_phase_and_axis_order(self):
        for bits in (8, 14):
            r, _, _ = fixture(bits=bits)
            _, bus = trial.reference_source(bits)
            rows = bus.reshape(4096, 4, 2)
            wrong = []
            switched = rows.copy()
            switched[0::2] = rows[1::2]
            switched[1::2] = rows[0::2]
            wrong.append(switched.ravel())
            wrong.append(rows[:, ::-1].ravel())
            swapped = rows[:, :, ::-1].copy()
            swapped[:, :, 0] |= 0x4000
            swapped[:, :, 1] &= 0x3fff
            wrong.append(swapped.ravel())
            # One missed odd row on I only destroys the fixed I/Q row ordering.
            skipped = rows.copy()
            skipped[1::2, :, 0] = np.roll(rows[1::2, :, 0], -1, axis=0)
            wrong.append(skipped.ravel())
            for other in wrong:
                values = other[(-67+17*np.arange(4096)) % len(other)].astype('<u4')
                values |= ((values >> 14) & 1) << 28
                changed = dict(r, cap_crc=zlib.crc32(values.tobytes()))
                self.assertTrue(trial.validate_run(changed, capture_lines(values), 1, 1000, 0, bits=bits)['faults'])

    def test_every_bus_flag_low_spare_and_clock_bit_is_checked(self):
        r, values, _ = fixture()
        for bit in (0, 5, 6, 13, 14, 15, 28):
            changed = values.copy()
            changed[-1] ^= 1 << bit
            metadata = dict(r, cap_crc=zlib.crc32(changed.tobytes()))
            checked = trial.validate_run(metadata, capture_lines(changed), 1, 1000, 0)
            self.assertTrue(checked['faults'], bit)
            self.assertEqual(checked['cap_checked_bus_words'], 4096)
            if bit == 28:
                self.assertEqual(checked['cap_mismatches'], 0)
                self.assertIn('DDR clock phase/control', checked['faults'])
            else:
                self.assertEqual(checked['cap_mismatches'], 1)
                self.assertEqual(checked['cap_first_bad'], 4095)
        values |= 0x08010000
        r['cap_crc'] = zlib.crc32(values.tobytes())
        self.assertEqual(trial.validate_run(r, capture_lines(values), 1, 1000, 0)['faults'], [])

    def test_inverted_clock_is_an_explicit_negative_control(self):
        r, values, lines = fixture(invert=1)
        self.assertEqual(trial.validate_run(r, lines, 1, 1000, 1)['classification'], 'clock_inversion_detected')
        self.assertTrue(trial.validate_run(dict(r, invert=0), lines, 1, 1000, 0)['faults'])
        values[-1] ^= 1 << 28
        r['cap_crc'] = zlib.crc32(values.tobytes())
        self.assertTrue(trial.validate_run(r, capture_lines(values), 1, 1000, 1)['faults'])

    def test_transport_is_ordered_complete_and_crc_checked(self):
        r, _, lines = fixture()
        for bad in (lines[:-1], [lines[1], lines[0]]+lines[2:], lines+[lines[-1]]):
            self.assertTrue(trial.validate_run(r, bad, 1, 1000, 0)['faults'])
        for change in (dict(cap_crc=r['cap_crc']^1), dict(cap_error='capture terminator missing')):
            self.assertFalse(trial.validate_run(dict(r, **change), lines, 1, 1000, 0)['cap_transport_ok'])

    def test_metadata_source_rings_cleanup_and_resource_ownership_fail_closed(self):
        r, _, lines = fixture()
        changes = [dict(clk_hz=256000000), dict(cmd='bench4live8'), dict(writers=2),
                   dict(chains_per_axis=1), dict(pointer_source='pio'), dict(period_symbols=65536),
                   dict(sample_bits=14), dict(low_bits=32), dict(row_words=2),
                   dict(in_words=4096), dict(input_words=8192), dict(input_base=0x20030000),
                   dict(pointer_base=0x20030000), dict(pointer_words=4096), dict(pointer_ring_words=4096),
                   dict(pointer_bases=list(reversed(r['pointer_bases']))),
                   dict(pointer_crc=0), dict(pointer_after_crc=0), dict(input_crc=0),
                   dict(previous_words=[0, 0]), dict(source_base=0x20020000),
                   dict(ring_base=0x20020000), dict(table_base=0x20000004),
                   dict(ring_bytes=65536), dict(table_bytes=131072), dict(table_crc=0),
                   dict(bus_priority=0), dict(errors=1), dict(pointer_errors=1), dict(output_errors=1),
                   dict(txstalls_i=1), dict(txstalls_q=1), dict(restored=False),
                   dict(selectors_restored=False), dict(resources_released=False), dict(core1_idle=False),
                   dict(sample_math_busy=0.1), dict(descriptor_gen_busy=0.1), dict(dac_clock_pio=2),
                   dict(dac_clock_sm=-1), dict(dac_clock_sm=4), dict(reason='writer TX stall'),
                   dict(cap_format='pairs'), dict(capture_count=4095), dict(cap_stride=16),
                   dict(cap_divider256=512), dict(cap_fifo_only=True), dict(max_poll_gap_us=257),
                   dict(supervision_cycles=-1), dict(supervision_busy=0), dict(elapsed_us=999999),
                   dict(polls=0), dict(sym_rate=8000000), dict(git='different-image')]
        for change in changes:
            checked = trial.validate_run(dict(r, **change), lines, 1, 1000, 0,
                                         expected_git='frozen-test-image')
            self.assertTrue(checked['faults'], change)
        for key in r:
            changed = dict(r)
            del changed[key]
            self.assertTrue(trial.validate_run(changed, lines, 1, 1000, 0,
                                              expected_git='frozen-test-image')['faults'], key)
        for key in ('sym_rate', 'supervision_busy'):
            for value in (float('nan'), float('inf'), -float('inf')):
                self.assertTrue(trial.validate_run(dict(r, **{key:value}), lines, 1, 1000, 0)['faults'])

    def build_fixture(self, directory):
        build = directory/'build'
        build.mkdir()
        for name in ('iqbench.uf2', 'iqbench.elf', 'memmap_iq.ld', 'git_rev.h'):
            (build/name).write_text('synthetic '+name+'\n')
        (build/'CMakeCache.txt').write_text('IQ4HIST_EXPERIMENT:BOOL=ON\n')
        (build/'build.ninja').write_text('PICO_STACK_SIZE=0x1000\n')
        return build

    def test_cli_sends_only_info_and_requested_quad_commands_and_preserves_evidence(self):
        for bits in (8, 14):
            with tempfile.TemporaryDirectory(prefix='b5-runner-') as tmp:
                directory = Path(tmp)
                build = self.build_fixture(directory)
                board = Mock()

                def command(text, **kwargs):
                    if text == 'info':
                        return dict(cmd='info', clk_hz=trial.CLOCK_HZ, git='frozen-test-image'), []
                    name, cpw, ms, invert, priority = text.split()
                    self.assertEqual(name, 'bench4quad8' if bits == 8 else 'bench4quad')
                    self.assertEqual(ms, '1000')
                    result, _, lines = fixture(int(cpw), int(invert), bits, int(priority))
                    return result, lines

                board.cmd.side_effect = command
                out = directory/'results'
                with patch.object(trial.iq, 'Board', return_value=board) as constructor, \
                     patch.object(trial.subprocess, 'check_output', return_value='frozen-host-head\n'), \
                     contextlib.redirect_stdout(io.StringIO()):
                    trial.main(['--out', str(out), '--build', str(build), '--bits', str(bits),
                                '--cpws', '2', '1', '--priorities', '0', '13', '--inverts', '0', '1',
                                '--expected-git', 'frozen-test-image', '--port', 'synthetic-only'])
                constructor.assert_called_once_with('synthetic-only')
                board.s.close.assert_called_once_with()
                self.assertEqual(board.cmd.call_count, 9)
                self.assertEqual(board.cmd.call_args_list[0].args, ('info',))
                runs = [json.loads(line) for line in (out/'runs.jsonl').read_text().splitlines()]
                self.assertEqual(len(runs), 8)
                self.assertTrue(all(not run['faults'] for run in runs))
                manifest = json.loads((out/'manifest.json').read_text())
                self.assertEqual(manifest['clock_khz'], 128000)
                self.assertEqual(manifest['head'], 'frozen-host-head')
                self.assertIn('host/iq4quad_trial.py', manifest['sha256'])
                self.assertEqual(len(list(out.glob('capture-*.txt'))), 8)
                self.assertEqual((out/'iqbench.uf2').read_bytes(), (build/'iqbench.uf2').read_bytes())
                # An existing evidence directory must never be reopened/overwritten.
                with patch.object(trial.iq, 'Board') as unopened, self.assertRaises(FileExistsError):
                    trial.main(['--out', str(out), '--build', str(build)])
                unopened.assert_not_called()

    def test_cli_closes_mock_board_and_rejects_non_sys128_before_any_run(self):
        with tempfile.TemporaryDirectory(prefix='b5-runner-') as tmp:
            directory = Path(tmp)
            build = self.build_fixture(directory)
            board = Mock()
            board.cmd.return_value = (dict(cmd='info', clk_hz=256000000), [])
            with patch.object(trial.iq, 'Board', return_value=board), \
                 patch.object(trial.subprocess, 'check_output', return_value='frozen-host-head\n'), \
                 self.assertRaises(ValueError):
                trial.main(['--out', str(directory/'results'), '--build', str(build)])
            board.cmd.assert_called_once_with('info')
            board.s.close.assert_called_once_with()
        with patch.object(trial.iq, 'Board') as unopened, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            trial.main(['--out', '/unused', '--ms', '99'])
        unopened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
