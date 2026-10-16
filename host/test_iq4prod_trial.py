"""B6 independent cyclic waveform, producer parity, metadata and CLI tests.

All boards are synthetic; no serial device is opened.
"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zlib

import numpy as np

import iq4prod_trial as trial


def capture_lines(values):
    return ['@cap '+str(k)+' '+' '.join(f'{int(v):08x}' for v in values[k:k+16])
            for k in range(0, len(values), 16)]


def fixture(cpw=1, invert=0, priority=13, offset=None, clock_hz=trial.CLOCK_HZ):
    source, bus = trial.cyclic_source(8)
    if offset is None:
        offset = len(bus)-67
    values = bus[(offset+17*np.arange(trial.CAP_WORDS)) % len(bus)].astype('<u4')
    values |= ((((values >> 14) & 1) ^ invert) << 28)
    elapsed, cycles = 1000000, 3000000
    reads = round(clock_hz/(8*cpw*16))
    r = dict(cmd='bench4prod8', git='frozen-test-image', clk_hz=clock_hz,
             source='cyclic_8psk', cpw=cpw, divider256=256*cpw, ms=1000, invert=invert,
             sps=4, L=7, row_profile=trial.L7_PROFILE, row_symbols=1, row_words=1,
             sample_bits=8, low_bits=0, writers=4, producers=4, chains_per_axis=2,
             pointer_source='pio', period_symbols=trial.PERIOD_SYMBOLS, seed=trial.SEED,
             in_words=trial.INPUT_WORDS, input_words=2*trial.INPUT_WORDS,
             input_base=trial.INPUT_BASE, input_crc=zlib.crc32(source.tobytes()),
             input_after_crc=zlib.crc32(source.tobytes()),
             previous_words=[int(source[trial.INPUT_WORDS-1]), int(source[-1])],
             ring_base=trial.TABLE_BASE, table_base=trial.TABLE_BASE, ring_bytes=131072,
             table_bytes=65536, table_crc=trial.table_crc(8), bus_priority=priority,
             errors=0, pointer_errors=0, output_errors=0, txstalls_i=0, txstalls_q=0,
             pointer_txstall_mask=0, pointer_rxstall_mask=23,
             restored=True, selectors_restored=True, resources_released=True,
             core1_idle=True, sample_math_busy=0.0, descriptor_gen_busy=0.0,
             idle_wait='spin', reason='complete', producer_pios=[0, 0, 0, 1],
             producer_sms=[0, 1, 2, 0], dac_clock_pio=0, dac_clock_sm=3,
             elapsed_us=elapsed, sym_rate=clock_hz/(8*cpw),
             supervision_cycles=cycles, supervision_busy=cycles/(elapsed*clock_hz/1e6),
             polls=elapsed//trial.POLL_US, poll_interval_us=trial.POLL_US,
             max_poll_interval_us=trial.MAX_POLL_US, max_poll_gap_us=129,
             start_input_issue_words=[8]*4, input_issue_words=[8+reads]*4,
             source_generator='iq8_fill', source_empty=False, starve_ms=0,
             grant_mode='endless_immutable', batch_words=512, cursor_kind='issued_words',
             runtime_gen_words=0, source_gen_cycles=0, source_gen_busy=0., worker_polls=0,
             worker_max_gap_cycles=0, worker_service_cycles=0,
             cap_format='pins32', cap_words=trial.CAP_WORDS, capture_count=trial.CAP_WORDS,
             cap_crc=zlib.crc32(values.tobytes()), cap_stride=17,
             cap_divider256=256*cpw, cap_fifo_only=False)
    return r, values, capture_lines(values)


def live_fixture(cpw=1, invert=0, priority=13, clock_hz=trial.CLOCK_HZ, starve_ms=0):
    r, _, _ = fixture(cpw, invert, priority, clock_hz=clock_hz)
    rate = clock_hz/(8*cpw)
    if starve_ms:
        published = int(rate*starve_ms/16000)//512*512+4096
        completed = published
        elapsed = round(published*16/rate*1e6)
        grants = [published]*4
    else:
        elapsed = 1000000
        completed = int(rate*elapsed/16e6)//512*512
        published = completed+4096
        grants = [completed+512]*4
    cursor = int(rate*(starve_ms/2 if starve_ms else 500)/16000)//512*512
    first = max(0, cursor*16-4096)
    ref = trial.live_reference_bus(first, 32768)
    offset = (cursor*16-first)*8+7
    values = ref[offset+17*np.arange(4096)].astype('<u4')
    values |= ((((values >> 14) & 1) ^ invert) << 28)
    cycles = 8*(published-4096)
    r.update(cmd='bench4prod8live', source='runtime_8psk_prbs', period_symbols=0,
             source_generator='xorshift32_packed_v1', source_empty=bool(starve_ms),
             starve_ms=starve_ms, grant_mode='finite_software_rearm', cursor_kind='completed_batch_words',
             published_words=published, reusable_words=completed, runtime_gen_words=published-4096,
             granted_words=grants, completed_words=[completed]*4, input_issue_words=[completed]*4,
             start_input_issue_words=[0]*4, input_crc=trial.live_ring_crc(4096),
             input_after_crc=trial.live_ring_crc(published), previous_words=[0, 0],
             source_gen_cycles=cycles, source_gen_busy=cycles/(elapsed*clock_hz/1e6),
             worker_polls=1000000, worker_max_gap_cycles=128, worker_service_cycles=30000000,
             cap_source_word=cursor, core1_idle=False, cap_crc=zlib.crc32(values.tobytes()),
             elapsed_us=elapsed, polls=elapsed//128,
             supervision_busy=r['supervision_cycles']/(elapsed*clock_hz/1e6))
    if starve_ms:
        r.update(errors=(1 << 21) | (1 << 16) | 3, pointer_errors=trial.SOURCE_EMPTY, output_errors=3,
                 txstalls_i=3, txstalls_q=3, pointer_txstall_mask=1, reason='SOURCE_EMPTY')
    return r, values, capture_lines(values)


class ProducerTrial(unittest.TestCase):
    def test_live_publication_capture_and_intentional_starvation(self):
        for clock_hz, cpws in ((128000000, (1, 2, 4)), (256000000, (2, 4))):
            for cpw in cpws:
                for invert in (0, 1):
                    for starve_ms in (0, 100):
                        r, _, lines = live_fixture(cpw, invert, clock_hz=clock_hz, starve_ms=starve_ms)
                        checked = trial.validate_run(r, lines, cpw, 1000, invert, live=True,
                                                     clock_hz=clock_hz, starve_ms=starve_ms)
                        self.assertEqual(checked['faults'], [], checked)
                        self.assertEqual(checked['classification'], 'source_starvation_detected' if starve_ms else
                                         'clock_inversion_detected' if invert else 'digital_live_producers_clean')
        for starve_ms in (0, 100):
            r, _, lines = live_fixture(starve_ms=starve_ms)
            changes = [dict(published_words=r['published_words']+512), dict(granted_words=[r['published_words']+512]*4),
                       dict(completed_words=[0]*4), dict(reusable_words=r['reusable_words']+1),
                       dict(source_gen_cycles=0), dict(source_gen_busy=float('nan')), dict(runtime_gen_words=0),
                       dict(input_crc=0), dict(input_after_crc=0), dict(previous_words=[1, 0]),
                       dict(source_empty=not bool(starve_ms)), dict(grant_mode='endless_immutable'),
                       dict(cursor_kind='issued_words'), dict(worker_polls=0), dict(worker_service_cycles=0),
                       dict(cap_source_word=r['reusable_words']+4096)]
            if starve_ms:
                changes += [dict(pointer_errors=trial.SOURCE_EMPTY | (1 << 2)), dict(output_errors=4),
                            dict(errors=r['errors'] | (1 << 20)), dict(pointer_txstall_mask=0),
                            dict(elapsed_us=1000000)]
            for change in changes:
                self.assertTrue(trial.validate_run(dict(r, **change), lines, 1, 1000, 0,
                                                   live=True, starve_ms=starve_ms)['faults'], change)

    def test_live_jump_packed_constellation_and_finite_fir_history(self):
        values = trial.iq.m.xorshift32(trial.SEED, 40000)
        for steps in (0, 1, 2, 31, 32, 1023, 32768, 40000):
            self.assertEqual(trial.xorshift_jump(trial.SEED, steps),
                             trial.SEED if steps == 0 else int(values[steps-1]))
        for first in (0, 1, 31, 10000, 59999999):
            source = trial.live_source_words(first, 32)
            # Jump composition also checks large offsets without a host replay.
            self.assertEqual(trial.xorshift_jump(trial.SEED, 2*first+64),
                             trial.xorshift_jump(trial.xorshift_jump(trial.SEED, 2*first), 64))
            shifts = 2*np.arange(16, dtype=np.uint32)
            codes = ((source[:, :, None] >> shifts) & 3).reshape(2, -1)
            levels = np.asarray(trial.FOUR_LEVELS_Q30)/2**30
            points = levels[codes[0]]+1j*levels[codes[1]]
            angles = np.angle(points)
            self.assertTrue(np.allclose(np.abs(points), 1, atol=1e-9))
            residue = (angles-np.pi/8) % (np.pi/4)
            self.assertTrue(np.all(np.isclose(residue, 0, atol=1e-8) |
                                   np.isclose(residue, np.pi/4, atol=1e-8)))
            # Restricting a finite FIR window must preserve all history and phases.
            full = trial.live_reference_bus(first*16, 512)
            np.testing.assert_array_equal(trial.live_reference_bus(first*16+37, 383), full[37*8:420*8])
        # Frozen first direct words and zero-code startup history are not the
        # geometric cyclic source contract, and a 4096-word source replay fails.
        live = trial.live_source_words(0, 4097)
        self.assertFalse(np.array_equal(live[:, :1], live[:, 4096:4097]))
        self.assertFalse(np.array_equal(live[:, :32].ravel(), trial.cyclic_source(8)[0][:64]))

    def test_live_capture_finite_window_rejects_wrap_and_stale_source(self):
        for cursor in (0, 3072, 30000000):
            first = max(0, cursor*16-4096)
            ref = trial.live_reference_bus(first, 32768)
            offset = (cursor*16-first)*8+3
            values = ref[offset+17*np.arange(4096)].astype('<u4')
            values |= ((values >> 14) & 1) << 28
            r = dict(cap_words=4096, cap_crc=zlib.crc32(values.tobytes()), cap_source_word=cursor)
            checked = trial.validate_capture(r, capture_lines(values), 0, live=True)
            self.assertTrue(checked['cap_aligned'], checked)
            self.assertEqual(checked['cap_mismatches'], 0)
            self.assertEqual(checked['cap_global_bus_offset'], first*8+offset)
            # Beyond-window matching must not be rescued by cyclic wrapping.
            boundary = len(ref)-100
            values = ref[(boundary+17*np.arange(4096)) % len(ref)].astype('<u4')
            values |= ((values >> 14) & 1) << 28
            r['cap_crc'] = zlib.crc32(values.tobytes())
            self.assertFalse(trial.validate_capture(r, capture_lines(values), 0, live=True)['cap_aligned'])
        r, _, lines = fixture()
        r['cap_source_word'] = 30000000
        self.assertFalse(trial.validate_capture(r, lines, 0, live=True)['cap_aligned'])

    def test_rates_priorities_clock_polarities_and_cyclic_wrap(self):
        for clock_hz, cpws in ((128000000, (1, 2, 4)), (256000000, (2, 4))):
            for cpw in cpws:
                for priority in (0, 13):
                    for invert in (0, 1):
                        r, values, lines = fixture(cpw, invert, priority, clock_hz=clock_hz)
                        checked = trial.validate_run(r, lines, cpw, 1000, invert,
                                                     expected_git='frozen-test-image',
                                                     priority=priority, clock_hz=clock_hz)
                        self.assertEqual(checked['faults'], [])
                        self.assertEqual(checked['cap_checked_bus_words'], 4096)
                        self.assertEqual(checked['cap_clock_checked'], 4096)
                        self.assertEqual(checked['cap_clock_mismatches'], 4096*invert)
                        self.assertEqual(checked['classification'],
                                         'clock_inversion_detected' if invert else 'digital_cyclic_producers_clean')
                        self.assertTrue(np.all((values & 63) == 0))

    def test_complete_history_before_parity_selection_and_source_word_seams(self):
        codes = trial.reference_codes()
        pointers = trial.reference_pointers()
        self.assertEqual(pointers.shape, (4, 32768))
        for axis in range(2):
            for parity in (0, 1):
                expected = [trial.TABLE_BASE+4*sum(int(codes[axis][(symbol-age) % 65536]) << (2*age)
                                                for age in range(7))
                            for symbol in range(parity, 65536, 2)]
                np.testing.assert_array_equal(pointers[2*parity+axis], expected)
                # Seams 15/16 and source wrap both require nonlocal prehistory.
                for symbol in (parity, 14+parity, 16+parity, 65534+parity):
                    key = (int(pointers[2*parity+axis, symbol//2])-trial.TABLE_BASE)//4
                    self.assertEqual([key >> (2*age) & 3 for age in range(7)],
                                     [int(codes[axis][(symbol-age) % 65536]) for age in range(7)])
        self.assertNotEqual(int(pointers[0, 0]), trial.TABLE_BASE+4*int(codes[0][0]))
        # History over the decimated stream is a different (incorrect) result.
        decimated = [trial.TABLE_BASE+4*sum(int(codes[0][(2*n-2*age) % 65536]) << (2*age)
                                         for age in range(7)) for n in range(32768)]
        self.assertGreater(np.count_nonzero(pointers[0] != decimated), 32000)

    def test_even_odd_row_or_axis_swap_cannot_find_an_independent_offset(self):
        r, _, _ = fixture()
        _, bus = trial.cyclic_source(8)
        rows = bus.reshape(trial.PERIOD_SYMBOLS, 4, 2)
        wrong = []
        switched = rows.copy()
        switched[0::2], switched[1::2] = rows[1::2], rows[0::2]
        wrong.append(switched.ravel())
        wrong.append(rows[:, ::-1].ravel())
        swapped = rows[:, :, ::-1].copy()
        swapped[:, :, 0] |= 0x4000
        swapped[:, :, 1] &= 0x3fff
        wrong.append(swapped.ravel())
        for other in wrong:
            values = other[(-67+17*np.arange(4096)) % len(other)].astype('<u4')
            values |= ((values >> 14) & 1) << 28
            self.assertTrue(trial.validate_run(dict(r, cap_crc=zlib.crc32(values.tobytes())),
                                              capture_lines(values), 1, 1000, 0)['faults'])

    def test_every_physical_bus_flag_low_spare_and_clock_bit_is_checked(self):
        r, values, _ = fixture()
        for bit in (0, 5, 6, 13, 14, 15, 28):
            changed = values.copy()
            changed[-1] ^= 1 << bit
            checked = trial.validate_run(dict(r, cap_crc=zlib.crc32(changed.tobytes())),
                                         capture_lines(changed), 1, 1000, 0)
            self.assertTrue(checked['faults'], bit)
            self.assertEqual(checked['cap_checked_bus_words'], 4096)
            self.assertEqual(checked['cap_mismatches'], 0 if bit == 28 else 1)
        values |= 0x08010000
        self.assertEqual(trial.validate_run(dict(r, cap_crc=zlib.crc32(values.tobytes())),
                                           capture_lines(values), 1, 1000, 0)['faults'], [])

    def test_clock_inversion_is_explicit_and_transport_is_complete(self):
        r, values, lines = fixture(invert=1)
        self.assertEqual(trial.validate_run(r, lines, 1, 1000, 1)['classification'], 'clock_inversion_detected')
        self.assertTrue(trial.validate_run(dict(r, invert=0), lines, 1, 1000, 0)['faults'])
        for bad in (lines[:-1], [lines[1], lines[0]]+lines[2:], lines+[lines[-1]]):
            self.assertFalse(trial.validate_run(r, bad, 1, 1000, 1)['cap_transport_ok'])
        self.assertFalse(trial.validate_run(dict(r, cap_crc=r['cap_crc']^1), lines, 1, 1000, 1)['cap_transport_ok'])

    def test_metadata_and_distinct_peripheral_ownership_fail_closed(self):
        r, _, lines = fixture()
        changes = [dict(cmd='bench4quad8'), dict(source='stale_8psk'), dict(writers=2),
                   dict(producers=2), dict(chains_per_axis=1), dict(pointer_source='sram'),
                   dict(input_crc=0), dict(input_after_crc=0), dict(previous_words=[0, 0]),
                   dict(input_base=0x20030000), dict(table_base=0x20010000), dict(table_crc=0),
                   dict(errors=1), dict(pointer_errors=1), dict(output_errors=1), dict(txstalls_q=1),
                   dict(restored=False), dict(selectors_restored=False), dict(resources_released=False),
                   dict(producer_pios=[0, 0, 0, 0]), dict(producer_sms=[0, 0, 2, 0]),
                   dict(producer_sms=[0, 1, 4, 0]), dict(dac_clock_sm=2), dict(dac_clock_pio=1),
                   dict(input_issue_words=[100]*4), dict(input_issue_words=[1000008]*3),
                   dict(start_input_issue_words=[8, 8, 8, -1]), dict(core1_idle=False),
                   dict(sample_math_busy=.1), dict(descriptor_gen_busy=.1), dict(reason='writer TX stall'),
                   dict(cap_stride=16), dict(cap_format='pairs'), dict(cap_fifo_only=True),
                   dict(max_poll_gap_us=257), dict(polls=0), dict(elapsed_us=999999),
                   dict(sym_rate=8000000), dict(supervision_cycles=-1), dict(supervision_busy=0),
                   dict(git='different-image')]
        for change in changes:
            self.assertTrue(trial.validate_run(dict(r, **change), lines, 1, 1000, 0,
                                              expected_git='frozen-test-image')['faults'], change)
        for key in r:
            changed = dict(r)
            del changed[key]
            self.assertTrue(trial.validate_run(changed, lines, 1, 1000, 0,
                                              expected_git='frozen-test-image')['faults'], key)
        for key in ('sym_rate', 'supervision_busy'):
            for value in (float('nan'), float('inf'), -float('inf')):
                self.assertTrue(trial.validate_run(dict(r, **{key:value}), lines, 1, 1000, 0)['faults'])

    def test_cli_uses_only_info_and_requested_commands_and_preserves_evidence(self):
        with tempfile.TemporaryDirectory(prefix='b6-runner-') as tmp:
            directory = Path(tmp)
            build = directory/'build'
            build.mkdir()
            for name in ('iqbench.uf2', 'iqbench.elf', 'memmap_iq.ld', 'git_rev.h'):
                (build/name).write_text('synthetic '+name+'\n')
            (build/'CMakeCache.txt').write_text('IQ4HIST_EXPERIMENT:BOOL=ON\n')
            (build/'build.ninja').write_text('PICO_STACK_SIZE=0x1000\n')
            board = Mock()

            def command(text, **kwargs):
                if text == 'info':
                    return dict(cmd='info', clk_hz=trial.CLOCK_HZ, git='frozen-test-image'), []
                name, cpw, ms, invert, priority = text.split()
                self.assertEqual(name, 'bench4prod8')
                self.assertEqual(ms, '1000')
                result, _, lines = fixture(int(cpw), int(invert), int(priority))
                return result, lines

            board.cmd.side_effect = command
            out = directory/'results'
            with patch.object(trial.iq, 'Board', return_value=board) as constructor, \
                 patch.object(trial.subprocess, 'check_output', return_value='frozen-host-head\n'), \
                 contextlib.redirect_stdout(io.StringIO()):
                trial.main(['--out', str(out), '--build', str(build), '--ms', '1000',
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
            self.assertEqual(manifest['head'], 'frozen-host-head')
            self.assertIn('host/iq4prod_trial.py', manifest['sha256'])
            self.assertEqual(len(list(out.glob('capture-*.txt'))), 8)
            with patch.object(trial.iq, 'Board') as unopened, self.assertRaises(FileExistsError):
                trial.main(['--out', str(out), '--build', str(build)])
            unopened.assert_not_called()

    def test_cli_starvation_is_explicit_and_invalid_requests_never_open_a_board(self):
        with tempfile.TemporaryDirectory(prefix='b6-live-runner-') as tmp:
            directory = Path(tmp)
            build = directory/'build'
            build.mkdir()
            for name in ('iqbench.uf2', 'iqbench.elf', 'memmap_iq.ld', 'git_rev.h'):
                (build/name).write_text('synthetic '+name+'\n')
            (build/'CMakeCache.txt').write_text('IQ4HIST_EXPERIMENT:BOOL=ON\n')
            (build/'build.ninja').write_text('PICO_STACK_SIZE=0x1000\n')
            board = Mock()
            r, _, lines = live_fixture(starve_ms=100)
            board.cmd.side_effect = [(dict(cmd='info', clk_hz=trial.CLOCK_HZ), []), (r, lines)]
            out = directory/'results'
            with patch.object(trial.iq, 'Board', return_value=board), \
                 patch.object(trial.subprocess, 'check_output', return_value='frozen-host-head\n'), \
                 contextlib.redirect_stdout(io.StringIO()):
                trial.main(['--out', str(out), '--build', str(build), '--ms', '1000',
                            '--cpws', '1', '--priorities', '13', '--inverts', '0',
                            '--live', '--starve-ms', '100'])
            self.assertEqual(board.cmd.call_args_list[1].args, ('bench4prod8live 1 1000 0 13 100',))
            board.s.close.assert_called_once_with()
            record = json.loads((out/'runs.jsonl').read_text())
            self.assertEqual(record['classification'], 'source_starvation_detected')
        for options in (['--ms', '99'], ['--starve-ms', '100'], ['--live', '--starve-ms', '9'],
                        ['--clock-khz', '256000', '--cpws', '1']):
            with patch.object(trial.iq, 'Board') as unopened, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                trial.main(['--out', '/unused', *options])
            unopened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
