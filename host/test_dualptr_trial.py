"""Strict live-pointer host checks against real independent TS/GCS samples; no USB."""
import copy
import functools
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zlib

import numpy as np

import dual_spi as spi
import dualptr_trial as trial


def capture_lines(values):
    return ['@cap '+str(k)+' '+' '.join(f'{int(v):08x}' for v in values[k:k+16])
            for k in range(0, len(values), 16)]


@functools.lru_cache(maxsize=10)
def spi_stats(source, code_index=8):
    rate = spi.QPSK_RATES[code_index]
    crc_fields = b''.join(spi.pv.message(k, spi.pv.pattern_payload(k))[-4:] for k in range(16))
    stats = dict.fromkeys(('lost', 'bad_hdr', 'bad_crc', 'bad_sync', 'null_packets',
                          'short_msgs', 'long_msgs', 'overflows', 'gcs_underflows'), 0)
    stats.update(ok=32, runtime_bbframes=2, runtime_messages=8, bbframes=6,
                 crc_chain=zlib.crc32(crc_fields*2),
                 bb_crc=spi.first_crcs(gcs=source == 6, rate=rate))
    size = 6*((spi.qpsk_config(code_index)['kbch']-80)//8)
    stats.update(bb_mode='gcs' if source == 6 else 'ts', ts_packets=224 if source == 5 else 0,
                 gcs_packets=(size+187)//188 if source == 6 else 0,
                 gcs_bytes=size if source == 6 else 0)
    return stats


def fixture(source=6, cpw=1, fault=0, clock_mode=1, clock=128000000,
            code_index=8, bch_kernel=2, bch_bank=1, writers=2):
    cursor = 123*spi.FRAME_SYMBOLS+1000
    first, bus = spi.reference_window(cursor, span=8, beta=None, gcs=source == 6,
                                      rate=spi.QPSK_RATES[code_index], profile=trial.L8_PROFILE)
    offset = (cursor-first*spi.FRAME_SYMBOLS)*8+13
    bus = bus[offset+17*np.arange(trial.CAP_WORDS)].astype('<u4')
    values = bus | ((((bus >> 14) & 1) ^ (clock_mode == 2)) << 28)
    elapsed = 790000 if fault else 3000000
    end = 786432 if fault else int(clock*3/(128*cpw))+8
    reusable, published = end-8, end-8+trial.INPUT_WORDS
    granted = end if fault else published//512*512
    budget, deadline = trial.poll_limits(clock)
    r = dict(cmd='dualptr', git='frozen-test-image', source=source, cpw=cpw, divider256=cpw*256,
             ms=3000, fault=fault, sps=4, L=8, row_symbols=4, writers=2,
             row_profile=trial.L8_PROFILE, filter_beta=None, schedule=0, bus_priority_mask=0,
             **spi.qpsk_config(code_index), bch_kernel=bch_kernel, bch_bank=bch_bank, map_kernel=1,
             ldpc_pack=1, frame_parallel=False, descriptor_gen_busy=0.0, sample_math_busy=0.0,
             in_words=8192, table_bytes=65536, table_crc=3433922394, restored=True,
             selectors_restored=True, poll_interval_us=128, max_poll_interval_us=256,
             read_beyond_grants=False, pointer_other_pio_flags=0, cap_stride=17,
             cap_divider256=cpw*256, cap_fifo_only=False, cap_format='pins32', dac_clock_pio=2,
             dac_clock_sm=2,
             dac_clock=trial.DAC_CLOCK_NAMES['ddr' if clock_mode == 1 else 'inverted'],
             clk_hz=clock, elapsed_us=elapsed, sym_rate=clock/(8*cpw),
             core0_work_busy=0.486, supervision_busy=0.034, split_bch_busy=0.452,
             core1_work_busy=0.82, required_credit_runway_words=2*budget,
             source_cursor_deadline_us=deadline, min_credit_runway_words=0 if fault else 6500,
             max_poll_gap_us=200, table_base=0x20040000, input_base=0x20008000,
             input_issue_words=[end, end], reusable_words=reusable,
             published_words=published, granted_words=granted, pointer_sm_mask=3,
             pointer_txstall_mask=3 if fault else 0, pointer_rxstall_mask=3,
             null_trigger_mask=48 if fault else 0,
             pointer_errors=24 if fault else 0, output_errors=3 if fault else 0,
             errors=trial.ERR_POINTER|3 if fault else 0,
             txstalls_i=bool(fault), txstalls_q=bool(fault),
             s2_runtime_frames=100, split_bch_frames=100,
             grants_frozen=bool(fault), frozen_granted_words=granted if fault else 0,
             capture_started=not fault, capture_count=0 if fault else trial.CAP_WORDS,
             cap_words=0 if fault else trial.CAP_WORDS,
             cap_symbol_cursor=cursor, cap_crc=0 if fault else zlib.crc32(values.tobytes()),
             pv_spi=copy.deepcopy(spi_stats(source, code_index)))
    if writers == 4:
        r.update(cmd='dualptr4', writers=4, dac_clock_pio=0,
                 pointer_pio=0, pointer_routing='alternating_dma')
    return r, values, [] if fault else capture_lines(values)


class DualptrTrial(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The real independent references are immutable; reuse them across mutations.
        cls.references = mock.patch('dual_spi.reference_window',
                                    functools.lru_cache(maxsize=12)(spi.reference_window))
        cls.references.start()

    @classmethod
    def tearDownClass(cls):
        cls.references.stop()

    def validate(self, r, lines, source=6, cpw=1, fault=0, clock_mode=1,
                 code_index=8, bch_kernel=2, bch_bank=1, writers=2):
        return trial.validate_run(r, lines, cpw, 3000, source, fault, clock_mode,
                                  128000000, 'frozen-test-image', code_index, bch_kernel, bch_bank,
                                  writers=writers)

    def test_high_rates_require_exact_fec_configuration(self):
        for code_index in (9, 10):
            for source in (5, 6):
                for bch_kernel in (2, 3):
                    r, _, lines = fixture(source, code_index=code_index, bch_kernel=bch_kernel)
                    result = self.validate(r, lines, source, code_index=code_index, bch_kernel=bch_kernel)
                    self.assertEqual(result['classification'], 'digital_live_clean', result)
                    self.assertEqual(result['cap_checked_bus_words'], 4096)
                    for field, value in (('code_index', 8), ('s2_code', 'normal 5/6'),
                                         ('kbch', 53840), ('bch_kernel', 5-bch_kernel),
                                         ('bch_bank', 0), ('ldpc_pack', 0)):
                        changed = dict(r, **{field: value})
                        self.assertTrue(self.validate(changed, lines, source, code_index=code_index,
                                                      bch_kernel=bch_kernel)['faults'], field)
                    control = dict(r, cmd='dualstream', writers=4)
                    result = trial.validate_control(control, lines, 1, 3000, source, 128000000,
                                                    'frozen-test-image', code_index, bch_kernel, 1, 1)
                    self.assertEqual(result['classification'], 'cpu_descriptor_control_clean', result)
                    for field, value in (('code_index', 8), ('s2_code', 'normal 5/6'), ('kbch', 53840),
                                         ('bch_kernel', 5-bch_kernel), ('bch_bank', 0), ('ldpc_pack', 0)):
                        result = trial.validate_control(dict(control, **{field: value}), lines, 1, 3000,
                                                        source, 128000000, 'frozen-test-image',
                                                        code_index, bch_kernel, 1, 1)
                        self.assertTrue(result['faults'], field)

    def test_pigeon_vision_2_3_at_8msym_positive_and_negative_controls(self):
        # Normal QPSK 2/3 (code 5) is the stored pvflight profile; cpw 2 is 8 Msym/s.
        for source in (5, 6):
            for fault, clock_mode, classification in (
                    (0, 1, 'digital_live_clean'),
                    (0, 2, 'inverted_clock_detected'),
                    (1, 1, 'credit_starvation_detected')):
                r, _, lines = fixture(source, 2, fault, clock_mode, code_index=5)
                result = self.validate(r, lines, source, 2, fault, clock_mode, code_index=5)
                self.assertEqual(result['faults'], [], (source, fault, clock_mode))
                self.assertEqual(result['classification'], classification)
                if not fault:
                    self.assertEqual(result['cap_checked_bus_words'], 4096)
                    self.assertEqual(result['cap_clock_checked'], 4096)
            r, _, lines = fixture(source, 2, code_index=5)
            for field, value in (('code_index', 8), ('s2_code', 'normal 5/6'), ('kbch', 53840),
                                 ('sym_rate', 16e6), ('cpw', 1), ('divider256', 256)):
                self.assertTrue(self.validate(dict(r, **{field: value}), lines, source, 2,
                                              code_index=5)['faults'], field)
            # A 5/6 capture must not pass as 2/3, and vice versa.
            other, _, other_lines = fixture(source, 2, code_index=8)
            self.assertTrue(self.validate(dict(other, **spi.qpsk_config(5)), other_lines, source, 2,
                                          code_index=5)['faults'])

    def test_runner_sends_pigeon_vision_profile_with_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            build, out = base/'build', base/'results'
            build.mkdir()
            for name in ('iqbench.uf2', 'iqbench.elf'):
                (build/name).write_bytes(b'native runner fixture')
            board = mock.Mock()
            board.cmd.return_value = ({}, [])
            argv = ['dualptr_trial.py', '--out', str(out), '--build', str(build),
                    '--sources', '5', '--cpws', '2', '--ms', '60000', '--control',
                    '--code-index', '5', '--priority', '13']
            with mock.patch.object(sys, 'argv', argv), mock.patch.object(trial.iq, 'Board', return_value=board), \
                    mock.patch.object(trial, 'validate_clock'), \
                    mock.patch.object(trial, 'validate_run', return_value={'faults': []}) as positive, \
                    mock.patch.object(trial, 'validate_control', return_value={'faults': []}), \
                    mock.patch('builtins.print'):
                trial.main()
            commands = [call.args[0] for call in board.cmd.call_args_list]
            for command in ('dualprio 13', 'dualclock 1', 'dualptr 2 60000 5 0 5',
                            'dualstream 2 60000 2 5 4096 0 0 0 4 5'):
                self.assertIn(command, commands)
            self.assertEqual(positive.call_args.kwargs['priority'], 13)
            self.assertEqual(positive.call_args.args[-3:], (5, 2, 1))
            manifest = json.loads((out/'manifest.json').read_text())
            self.assertEqual((manifest['code_index'], manifest['priority'], manifest['cpws']), (5, 13, [2]))
            self.assertIn('2/3', manifest['scope'])

    def test_fused_ts_adapter_needs_its_reported_kernel_and_leaf_placement(self):
        for code_index in (5, 10):
            for pv_kernel, leaf in ((1, 0x20050000), (2, 0), (3, 0x20050000)):
                r, _, lines = fixture(5, code_index=code_index)
                r.update(pv_kernel=pv_kernel, pv_leaf_base=leaf)
                if pv_kernel == 3:
                    r.update(ingress_core=0, core0_ingress_busy=0.17, ingress_steps=1000,
                             ingress_max_step_cyc=5000, ingress_step_packets=4,
                             core0_work_busy=r['core0_work_busy'] + 0.17)
                    r['pv_spi']['ts_busy'] = 0.0
                    for change in (dict(ingress_core=1), dict(core0_ingress_busy=0.0), dict(ingress_steps=0),
                                   dict(ingress_step_packets=8), dict(core0_work_busy=0.486),
                                   dict(core0_ingress_busy=float('nan'))):
                        self.assertTrue(trial.validate_run(dict(r, **change), lines, 1, 3000, 5, 0, 1, 128000000,
                                                           'frozen-test-image', code_index, 2, 1,
                                                           pv_kernel=3)['faults'], change)
                    busy_core1 = copy.deepcopy(r); busy_core1['pv_spi']['ts_busy'] = 0.1
                    self.assertTrue(trial.validate_run(busy_core1, lines, 1, 3000, 5, 0, 1, 128000000,
                                                       'frozen-test-image', code_index, 2, 1, pv_kernel=3)['faults'])
                else:
                    # Core-0 ingress fields must not appear for the core-1 adapters.
                    self.assertTrue(trial.validate_run(dict(r, core0_ingress_busy=0.1), lines, 1, 3000, 5, 0, 1,
                                                       128000000, 'frozen-test-image', code_index, 2, 1,
                                                       pv_kernel=pv_kernel)['faults'])
                result = trial.validate_run(r, lines, 1, 3000, 5, 0, 1, 128000000, 'frozen-test-image',
                                            code_index, 2, 1, pv_kernel=pv_kernel)
                self.assertEqual(result['faults'], [], (code_index, pv_kernel))
                self.assertEqual(result['classification'], 'digital_live_clean')
                sram = pv_kernel != 2
                for change in (dict(pv_kernel=0), dict(pv_kernel=pv_kernel % 3 + 1),
                               dict(pv_leaf_base=0x20040000 if sram else 0x20050000),
                               dict(pv_leaf_base=leaf + 2 if sram else 4),
                               dict(pv_leaf_base=0x20008000 if sram else 0x20010000)):
                    changed = dict(r, **change)
                    self.assertTrue(trial.validate_run(changed, lines, 1, 3000, 5, 0, 1, 128000000,
                                                       'frozen-test-image', code_index, 2, 1,
                                                       pv_kernel=pv_kernel)['faults'], change)
                missing = dict(r); del missing['pv_kernel']
                self.assertTrue(trial.validate_run(missing, lines, 1, 3000, 5, 0, 1, 128000000,
                                                   'frozen-test-image', code_index, 2, 1,
                                                   pv_kernel=pv_kernel)['faults'])
        # The default needs no new fields (older images), but a reported adapter must match.
        r, _, lines = fixture(5)
        self.assertEqual(self.validate(r, lines, 5)['faults'], [])
        self.assertTrue(self.validate(dict(r, pv_kernel=1, pv_leaf_base=0x20050000), lines, 5)['faults'])
        # GCS has no TS adapter.
        r, _, lines = fixture(6)
        self.assertTrue(trial.validate_run(dict(r, pv_kernel=1, pv_leaf_base=0x20050000), lines, 1, 3000, 6,
                                           0, 1, 128000000, 'frozen-test-image', 8, 2, 1,
                                           pv_kernel=1)['faults'])

    def test_runner_appends_ts_adapter_only_when_selected(self):
        for pv_kernel, sources in ((1, ['5']), (2, ['5']), (3, ['5']), (1, ['5', '6']), (3, ['6'])):
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                build, out = base/'build', base/'results'
                build.mkdir()
                for name in ('iqbench.uf2', 'iqbench.elf'):
                    (build/name).write_bytes(b'native runner fixture')
                board = mock.Mock()
                board.cmd.return_value = ({}, [])
                argv = ['dualptr_trial.py', '--out', str(out), '--build', str(build), '--sources', *sources,
                        '--cpws', '1', '--ms', '60000', '--code-index', '10', '--bch-kernel', '3',
                        '--priority', '13', '--pv-kernel', str(pv_kernel)]
                with mock.patch.object(sys, 'argv', argv), mock.patch.object(trial.iq, 'Board', return_value=board), \
                        mock.patch.object(trial, 'validate_clock'), \
                        mock.patch.object(trial, 'validate_run', return_value={'faults': []}) as positive, \
                        mock.patch('builtins.print'), mock.patch('sys.stderr'):
                    if sources != ['5']:
                        with self.assertRaises(SystemExit):
                            trial.main()
                        board.cmd.assert_not_called()
                        continue
                    trial.main()
                commands = [call.args[0] for call in board.cmd.call_args_list]
                self.assertIn(f'dualptr 1 60000 5 0 10 {pv_kernel}', commands)
                self.assertEqual(positive.call_args.kwargs['pv_kernel'], pv_kernel)
                self.assertEqual(json.loads((out/'manifest.json').read_text())['pv_kernel'], pv_kernel)
                self.assertTrue((out/f'dualptr-128000-1-5-code10-pvk{pv_kernel}-clock1-fault0-0.txt').exists())

    def test_both_live_sources_both_rates_and_all4096_snapshots(self):
        for source in (5, 6):
            for cpw in (1, 2):
                r, _, lines = fixture(source, cpw)
                result = self.validate(r, lines, source, cpw)
                self.assertEqual(result['faults'], [])
                self.assertEqual(result['classification'], 'digital_live_clean')
                self.assertEqual(result['cap_checked_bus_words'], 4096)
                self.assertEqual(result['cap_clock_checked'], 4096)
                self.assertTrue(result['cap_clock_ok'])
                self.assertFalse(result['negative_control_detected'])

    def test_last_waveform_sample_corruption_has_intact_transport_but_fails(self):
        r, values, _ = fixture()
        values = values.copy(); values[-1] ^= 1
        r['cap_crc'] = zlib.crc32(values.tobytes())
        result = self.validate(r, capture_lines(values))
        self.assertTrue(result['cap_transport_ok'])
        self.assertEqual(result['cap_mismatches'], 1)
        self.assertEqual(result['cap_first_bad'], 4095)
        self.assertTrue(result['faults'])

    def test_four_writers_both_sources_rates_positive_and_negative_controls(self):
        for source in (5, 6):
            for cpw in (1, 2):
                for fault, clock_mode, classification in (
                        (0, 1, 'digital_live_clean'),
                        (0, 2, 'inverted_clock_detected'),
                        (1, 1, 'credit_starvation_detected')):
                    r, _, lines = fixture(source, cpw, fault, clock_mode, writers=4)
                    result = self.validate(r, lines, source, cpw, fault, clock_mode, writers=4)
                    self.assertEqual(result['faults'], [], (source, cpw, fault, clock_mode))
                    self.assertEqual(result['classification'], classification)
                    self.assertEqual(result['negative_control_detected'], bool(fault or clock_mode == 2))
                    if not fault:
                        self.assertEqual(result['cap_checked_bus_words'], 4096)
                        self.assertEqual(result['cap_clock_checked'], 4096)

    def test_four_writer_command_build_and_topology_fail_closed(self):
        r, _, lines = fixture(writers=4)
        for change in (dict(cmd='dualptr'), dict(writers=2), dict(git='other-image'),
                       dict(dac_clock_pio=2), dict(pointer_pio=2),
                       dict(pointer_routing='shared_fifo'), dict(pointer_sm_mask=15),
                       dict(dac_clock_sm=0), dict(dac_clock_sm=4)):
            self.assertTrue(self.validate(dict(r, **change), lines, writers=4)['faults'], change)
        for field in ('pointer_pio', 'pointer_routing'):
            changed = dict(r); del changed[field]
            self.assertTrue(self.validate(changed, lines, writers=4)['faults'], field)
        self.assertTrue(self.validate(r, lines)['faults'])
        self.assertTrue(self.validate(r, lines, writers=3)['faults'])
        legacy, _, legacy_lines = fixture()
        self.assertTrue(self.validate(legacy, legacy_lines, writers=4)['faults'])

    def test_runner_defaults_to_dualptr_and_selects_four_writer_command(self):
        for writers in (2, 4):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                build, source, out = root/'build', root/'firmware', root/'out'
                build.mkdir(); (source/'src').mkdir(parents=True)
                for name in ('iqbench.uf2', 'iqbench.elf'):
                    (build/name).write_bytes(b'test image')
                (source/'CMakeLists.txt').write_text('# test source\n')
                board = mock.Mock()
                board.cmd.return_value = ({}, [])
                args = ['dualptr_trial.py', '--out', str(out), '--build', str(build),
                        '--source', str(source), '--control']
                if writers == 4:
                    args += ['--writers', '4']
                with mock.patch('sys.argv', args), mock.patch.object(trial.iq, 'Board', return_value=board), \
                        mock.patch.object(trial, 'validate_clock'), \
                        mock.patch.object(trial, 'validate_control', return_value=dict(faults=[])), \
                        mock.patch.object(trial, 'validate_run', return_value=dict(faults=[])) as validate, \
                        mock.patch.object(trial.subprocess, 'check_output', return_value='frozen-test-image\n'), \
                        mock.patch('builtins.print'):
                    trial.main()
                pointer_cmd = 'dualptr4' if writers == 4 else 'dualptr'
                commands = [call.args[0] for call in board.cmd.call_args_list]
                self.assertIn(f'{pointer_cmd} 1 3000 6 0 8', commands)  # C2 appends the code index
                self.assertIn('dualstream 1 3000 2 6 4096 0 0 0 4 8', commands)
                self.assertEqual(validate.call_args.kwargs['writers'], writers)
                manifest = json.loads((out/'manifest.json').read_text())
                self.assertEqual(manifest['writers'], writers)
                self.assertIn('host/dualptr_trial.py', manifest['sha256'])
                self.assertTrue((out/f'{pointer_cmd}-128000-1-6-code8-clock1-fault0-0.txt').exists())
                board.s.close.assert_called_once()

    def test_inverted_clock_needs_correct_data_and_every_clock_snapshot_bad(self):
        r, values, lines = fixture(clock_mode=2)
        result = self.validate(r, lines, clock_mode=2)
        self.assertEqual(result['faults'], [])
        self.assertEqual(result['classification'], 'inverted_clock_detected')
        self.assertEqual(result['cap_mismatches'], 0)
        self.assertEqual(result['cap_clock_mismatches'], 4096)
        self.assertTrue(result['negative_control_detected'])
        self.assertTrue(self.validate(r, lines)['faults'])
        for bit in (0, 28):
            damaged = values.copy(); damaged[-1] ^= 1 << bit
            changed = dict(r, cap_crc=zlib.crc32(damaged.tobytes()))
            result = self.validate(changed, capture_lines(damaged), clock_mode=2)
            self.assertTrue(result['faults'])
            self.assertFalse(result['negative_control_detected'])

    def test_finite_grant_control_needs_source_empty_and_terminal_null(self):
        for stalled in (False, True):
            r, _, lines = fixture(fault=1)
            r.update(pointer_errors=24 if stalled else 8, pointer_txstall_mask=3 if stalled else 0,
                     output_errors=3 if stalled else 0,
                     errors=trial.ERR_POINTER | (3 if stalled else 0))
            result = self.validate(r, lines, fault=1)
            self.assertEqual(result['faults'], [])
            self.assertEqual(result['classification'], 'credit_starvation_detected')
            self.assertTrue(result['negative_control_detected'])
        for change in (dict(pointer_errors=16), dict(pointer_errors=40), dict(output_errors=4),
                       dict(errors=trial.ERR_POINTER|4), dict(null_trigger_mask=0),
                       dict(grants_frozen=False), dict(frozen_granted_words=0),
                       dict(elapsed_us=3000000), dict(cap_words=16), dict(error='parse error')):
            changed = dict(r, **change)
            result = self.validate(changed, lines, fault=1)
            self.assertTrue(result['faults'], change)
            self.assertFalse(result['negative_control_detected'], change)

    def test_configuration_cleanup_ownership_and_faults_fail_closed(self):
        r, _, lines = fixture()
        changes = [dict(source=5), dict(frame_parallel=True), dict(writers=4), dict(ldpc_pack=0),
                   dict(git='other-image'), dict(clk_hz=150000000), dict(filter_beta=0.8),
                   dict(table_base=0x20040020), dict(input_base=0x20040000),
                   dict(restored=False), dict(selectors_restored=False),
                   dict(descriptor_gen_busy=0.1), dict(read_beyond_grants=True),
                   dict(pointer_other_pio_flags=1), dict(pointer_sm_mask=1), dict(pointer_rxstall_mask=4),
                   dict(dac_clock_pio=0), dict(dac_clock_sm=0), dict(dac_clock_sm=4),
                   dict(errors=1), dict(pointer_errors=8), dict(output_errors=1), dict(txstalls_i=1),
                   dict(null_trigger_mask=16), dict(underruns=1), dict(own_errors=1),
                   dict(published_words=r['reusable_words']+8193),
                   dict(reusable_words=r['input_issue_words'][0]+1),
                   dict(input_issue_words=[r['granted_words']+1, r['granted_words']]),
                   dict(granted_words=r['published_words']+512), dict(input_issue_words=[0]),
                   dict(required_credit_runway_words=0), dict(source_cursor_deadline_us=0),
                   dict(min_credit_runway_words=1), dict(max_poll_gap_us=257),
                   dict(s2_runtime_frames=0), dict(split_bch_frames=0),
                   dict(capture_started=False), dict(capture_count=1), dict(cap_format='bus16x2')]
        for change in changes:
            changed = dict(r, **change)
            self.assertTrue(self.validate(changed, lines)['faults'], change)
        changed = dict(r); del changed['pointer_errors']
        self.assertTrue(self.validate(changed, lines)['faults'])

    def test_spi_acceptance_and_bbframe_provenance_are_required(self):
        for source in (5, 6):
            r, _, lines = fixture(source)
            for field, value in (('lost', 1), ('drain_timeouts', 1), ('runtime_bbframes', 0),
                                 ('runtime_messages', 0), ('bb_crc', [0]*4), ('crc_chain', 0)):
                changed = copy.deepcopy(r); changed['pv_spi'][field] = value
                self.assertTrue(self.validate(changed, lines, source)['faults'], field)

    def test_truncated_reordered_crc_and_clock_parity_controls(self):
        r, values, lines = fixture()
        for damaged in (lines[:-1], [lines[1], lines[0]]+lines[2:]):
            self.assertTrue(self.validate(r, damaged)['faults'])
        self.assertTrue(self.validate(dict(r, cap_crc=r['cap_crc']^1), lines)['faults'])
        values = values.copy(); values[-1] ^= 1 << 28
        changed = dict(r, cap_crc=zlib.crc32(values.tobytes()))
        result = self.validate(changed, capture_lines(values))
        self.assertEqual(result['cap_mismatches'], 0)
        self.assertEqual(result['cap_clock_mismatches'], 1)
        self.assertTrue(result['faults'])

    def test_nonfinite_rate_and_load_never_pass(self):
        r, _, lines = fixture()
        for key in ('sym_rate', 'core0_work_busy', 'core1_work_busy', 'supervision_busy', 'split_bch_busy'):
            for value in (float('nan'), float('inf'), -float('inf')):
                self.assertTrue(self.validate(dict(r, **{key: value}), lines)['faults'], (key, value))
        for change in (dict(supervision_busy=0.50), dict(split_bch_busy=0.50), dict(core1_work_busy=1.1)):
            self.assertTrue(self.validate(dict(r, **change), lines)['faults'], change)

    def test_existing_four_writer_control_has_same_waveform_and_clock_requirements(self):
        r, values, lines = fixture()
        r.update(cmd='dualstream', writers=4)
        result = trial.validate_control(r, lines, 1, 3000, 6, 128000000, 'frozen-test-image')
        self.assertEqual(result['faults'], [])
        self.assertEqual(result['classification'], 'cpu_descriptor_control_clean')
        for change in (dict(writers=2), dict(git='other-image'), dict(elapsed_us=1), dict(errors=1)):
            self.assertTrue(trial.validate_control(dict(r, **change), lines, 1, 3000, 6,
                                                   128000000, 'frozen-test-image')['faults'])
        values = values.copy(); values[-1] ^= 1 << 28
        changed = dict(r, cap_crc=zlib.crc32(values.tobytes()))
        self.assertTrue(trial.validate_control(changed, capture_lines(values), 1, 3000, 6,
                                               128000000, 'frozen-test-image')['faults'])

    def test_runner_sends_and_archives_selected_high_rate_and_kernel(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            build, out = base/'build', base/'results'
            build.mkdir()
            for name in ('iqbench.uf2', 'iqbench.elf'):
                (build/name).write_bytes(b'native runner fixture')
            board = mock.Mock()
            board.cmd.return_value = ({}, [])
            argv = ['dualptr_trial.py', '--out', str(out), '--build', str(build),
                    '--sources', '5', '6', '--cpws', '1', '--ms', '60000', '--control',
                    '--code-index', '10', '--bch-kernel', '3', '--bch-bank', '1']
            with mock.patch.object(sys, 'argv', argv), mock.patch.object(trial.iq, 'Board', return_value=board), \
                    mock.patch.object(trial, 'validate_clock'), \
                    mock.patch.object(trial, 'validate_run', return_value={'faults': []}) as positive, \
                    mock.patch.object(trial, 'validate_control', return_value={'faults': []}) as control, \
                    mock.patch('builtins.print'):
                trial.main()
            commands = [call.args[0] for call in board.cmd.call_args_list]
            for command in ('bchkernel 3', 'ldpcpack 1', 'bchbank 1', 'dualclock 1',
                            'dualptr 1 60000 5 0 10', 'dualptr 1 60000 6 0 10',
                            'dualstream 1 60000 2 5 4096 0 0 0 4 10',
                            'dualstream 1 60000 2 6 4096 0 0 0 4 10'):
                self.assertIn(command, commands)
            self.assertEqual(positive.call_count, 2)
            self.assertEqual(control.call_count, 2)
            self.assertTrue(all(call.args[-3:] == (10, 3, 1) for call in positive.call_args_list))
            self.assertTrue(all(call.args[-4:] == (10, 3, 1, 1) for call in control.call_args_list))
            manifest = json.loads((out/'manifest.json').read_text())
            self.assertEqual({key: manifest[key] for key in ('code_index', 'bch_kernel', 'ldpc_pack', 'bch_bank')},
                             dict(code_index=10, bch_kernel=3, ldpc_pack=1, bch_bank=1))
            board.s.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
