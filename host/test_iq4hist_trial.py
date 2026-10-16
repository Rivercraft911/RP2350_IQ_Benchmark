"""Synthetic full GPIO capture and strict B1 runner validation; no hardware access."""
import unittest
import zlib

import numpy as np

import iq4hist_trial as trial


def capture_lines(values):
    return ['@cap '+str(k)+' '+' '.join(f'{int(v):08x}' for v in values[k:k+16])
            for k in range(0, len(values), 16)]


def fixture(cpw=2, invert=0):
    source, bus = trial.reference_source()
    # Cross physical source wrap while the odd stride exercises both axes/phases.
    values = bus[(len(bus)-67+17*np.arange(trial.CAP_WORDS)) % len(bus)].astype('<u4')
    values |= ((((values >> 14) & 1) ^ invert) << 28)
    elapsed, cycles = 1000000, 3000000
    reads = int(trial.CLOCK_HZ/(8*cpw*16))
    r = dict(cmd='bench4live', git='frozen-test-image', clk_hz=trial.CLOCK_HZ,
             source='cyclic_8psk', cpw=cpw, divider256=256*cpw, ms=1000, invert=invert,
             sps=4, L=7, row_profile=trial.L7_PROFILE, row_symbols=1, row_words=2,
             writers=2, seed=trial.SEED, in_words=4096, input_words=8192,
             ring_base=0x20000000, table_base=0x20000000, ring_bytes=131072,
             table_bytes=131072, table_crc=trial.table_crc(), input_base=0x20020000,
             previous_words=[int(source[4095]), int(source[-1])],
             input_crc=zlib.crc32(source.tobytes()), input_after_crc=zlib.crc32(source.tobytes()),
             errors=0, pointer_errors=0, output_errors=0, txstalls_i=0, txstalls_q=0,
             pointer_txstall_mask=0, pointer_rxstall_mask=3,
             restored=True, selectors_restored=True, core1_idle=True,
             idle_wait='spin', reason='complete', polls=elapsed//128,
             sample_math_busy=0.0, descriptor_gen_busy=0.0,
             poll_interval_us=128, max_poll_interval_us=256, max_poll_gap_us=129,
             elapsed_us=elapsed, sym_rate=trial.CLOCK_HZ/(8*cpw),
             supervision_cycles=cycles, supervision_busy=cycles/(elapsed*trial.CLOCK_HZ/1e6),
             start_input_issue_words=[8, 8], input_issue_words=[8+reads, 8+reads],
             cap_format='pins32', cap_words=4096, capture_count=4096,
             cap_crc=zlib.crc32(values.tobytes()), cap_stride=17,
             cap_divider256=256*cpw, cap_fifo_only=False)
    return r, values, capture_lines(values)


class FourHistoryTrial(unittest.TestCase):
    def test_both_rates_clock_phases_and_cyclic_wrap(self):
        for cpw in (2, 4):
            for invert in (0, 1):
                r, _, lines = fixture(cpw, invert)
                result = trial.validate_run(r, lines, cpw, 1000, invert,
                                            expected_git='frozen-test-image')
                self.assertEqual(result['faults'], [])
                self.assertEqual(result['cap_checked_bus_words'], 4096)
                self.assertEqual(result['cap_clock_checked'], 4096)
                self.assertEqual(result['cap_clock_mismatches'], 4096*invert)
                self.assertEqual(result['classification'], 'clock_inversion_detected' if invert else 'digital_live_clean')

    def test_late_bus_flag_and_spare_corruptions_with_valid_transport_crc(self):
        for bit in (0, 13, 14, 15):
            r, values, _ = fixture()
            values[-1] ^= 1 << bit
            r['cap_crc'] = zlib.crc32(values.tobytes())
            result = trial.validate_run(r, capture_lines(values), 2, 1000, 0)
            self.assertEqual(result['cap_checked_bus_words'], 4096)
            self.assertEqual(result['cap_mismatches'], 1)
            self.assertEqual(result['cap_first_bad'], 4095)
            self.assertTrue(result['faults'])

    def test_clock_control_requires_every_snapshot(self):
        for invert in (0, 1):
            r, values, _ = fixture(invert=invert)
            values[-1] ^= 1 << 28
            r['cap_crc'] = zlib.crc32(values.tobytes())
            result = trial.validate_run(r, capture_lines(values), 2, 1000, invert)
            self.assertEqual(result['cap_mismatches'], 0)
            self.assertIn('DDR clock phase/control', result['faults'])
        r, _, lines = fixture(invert=1)
        r['invert'] = 0
        self.assertTrue(trial.validate_run(r, lines, 2, 1000, 0)['faults'])

    def test_unrelated_gpio_bits_do_not_change_waveform_validation(self):
        r, values, _ = fixture()
        values |= 0x08010000
        r['cap_crc'] = zlib.crc32(values.tobytes())
        self.assertEqual(trial.validate_run(r, capture_lines(values), 2, 1000, 0)['faults'], [])

    def test_transport_truncation_reordering_and_crc_fail_closed(self):
        r, _, lines = fixture()
        for bad in (lines[:-1], [lines[1], lines[0]]+lines[2:], lines+[lines[-1]]):
            self.assertTrue(trial.validate_run(r, bad, 2, 1000, 0)['faults'])
        r['cap_crc'] ^= 1
        self.assertFalse(trial.validate_run(r, lines, 2, 1000, 0)['cap_transport_ok'])

    def test_metadata_faults_source_ownership_and_cleanup_are_required(self):
        r, _, lines = fixture()
        changes = [dict(table_base=0x20000008), dict(input_base=0x20008000), dict(table_crc=0),
                   dict(input_crc=0), dict(input_after_crc=0), dict(previous_words=[0, 0]),
                   dict(errors=1), dict(pointer_errors=1), dict(output_errors=1), dict(txstalls_q=1),
                   dict(pointer_txstall_mask=1), dict(pointer_rxstall_mask=16),
                   dict(restored=False), dict(selectors_restored=False), dict(core1_idle=False),
                   dict(sample_math_busy=0.1), dict(descriptor_gen_busy=0.1),
                   dict(cap_format='pairs'), dict(capture_count=4095), dict(cap_stride=16),
                   dict(cap_divider256=256), dict(cap_fifo_only=True), dict(max_poll_gap_us=257),
                   dict(supervision_cycles=-1), dict(supervision_busy=0), dict(elapsed_us=999999),
                   dict(sym_rate=16000000), dict(start_input_issue_words=[0]),
                   dict(input_issue_words=[0, 0]), dict(git='different-image')]
        for change in changes:
            changed = dict(r, **change)
            self.assertTrue(trial.validate_run(changed, lines, 2, 1000, 0,
                                              expected_git='frozen-test-image')['faults'], change)

    def test_missing_fields_and_nonfinite_fractions_never_pass(self):
        r, _, lines = fixture()
        for key in ('clk_hz', 'cap_crc', 'input_crc', 'elapsed_us', 'input_issue_words'):
            changed = dict(r)
            del changed[key]
            self.assertTrue(trial.validate_run(changed, lines, 2, 1000, 0)['faults'], key)
        for key in ('sym_rate', 'supervision_busy'):
            for value in (float('nan'), float('inf'), -float('inf')):
                self.assertTrue(trial.validate_run(dict(r, **{key:value}), lines, 2, 1000, 0)['faults'])

    def test_clock_response_and_frozen_firmware_identity(self):
        r = dict(cmd='info', clk_hz=128000000, git='frozen-test-image')
        trial.validate_clock(r, 'frozen-test-image')
        clean = dict(r, git='0123456789')
        trial.validate_clock(clean, '0123456789abcdef0123456789abcdef01234567')
        with self.assertRaises(ValueError):
            trial.validate_clock(dict(clean, git='0123456789-dirty'), '0123456789abcdef0123456789abcdef01234567')
        for change in (dict(cmd='clock'), dict(clk_hz=150000000), dict(git='different-image'), dict(error='failed')):
            with self.assertRaises(ValueError):
                trial.validate_clock(dict(r, **change), 'frozen-test-image')


if __name__ == '__main__':
    unittest.main()
