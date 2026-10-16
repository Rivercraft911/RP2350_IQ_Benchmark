"""Check random-access TS framing against the sequential independent builder."""
import math
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zlib

import numpy as np

import dual_spi as spi
import dual_trial as trial


class SpiReference(unittest.TestCase):
    def test_bbframes(self):
        packets = np.frombuffer(b"".join(spi.pv.pattern_packet(k % spi.PACKET_PERIOD)
                                       for k in range(350)), np.uint8)
        for rate in spi.QPSK_RATES.values():
            with self.subTest(rate=rate):
                data_bytes = (spi.ref.code(rate, False).kbch - 80) // 8
                period = spi.PACKET_PERIOD * 188 // math.gcd(data_bytes, spi.PACKET_PERIOD * 188)
                sequential = spi.ref.ts_bbframes(packets, rate, ro=0.20)
                for index, expected in enumerate(sequential):
                    np.testing.assert_array_equal(spi.bbframe(index, rate=rate), expected)
                for index in (1, 31, 10528, 123456):
                    np.testing.assert_array_equal(spi.bbframe(index, rate=rate),
                                                  spi.bbframe(index + period, rate=rate))

    def test_gcs_bbframes(self):
        stream = b"".join(spi.pv.pattern_packet(k % spi.PACKET_PERIOD) for k in range(350))
        for rate in spi.QPSK_RATES.values():
            with self.subTest(rate=rate):
                data_bytes = (spi.ref.code(rate, False).kbch - 80) // 8
                period = spi.PACKET_PERIOD * 188 // math.gcd(data_bytes, spi.PACKET_PERIOD * 188)
                for index in range(len(stream) // data_bytes):
                    data = stream[index * data_bytes:(index + 1) * data_bytes]
                    expected = spi.ref.bbframe(np.unpackbits(np.frombuffer(data, np.uint8)), rate,
                                               ro=0.20, sync=0xB9)
                    np.testing.assert_array_equal(spi.bbframe(index, gcs=True, rate=rate), expected)
                for index in (0, 1, 31, 10528, 123456):
                    np.testing.assert_array_equal(spi.bbframe(index, gcs=True, rate=rate),
                                                  spi.bbframe(index + period, gcs=True, rate=rate))

    def check_capture_and_corruption(self, rows, span, beta, code_index=5, gcs=False,
                                     pilots=True, profile=None):
        symbols = spi.frame_symbols(pilots)
        cursor = 123 * symbols + 1000
        first, bus = spi.reference_window(cursor, span=span, beta=beta, rate=spi.QPSK_RATES[code_index],
                                           gcs=gcs, pilots=pilots, profile=profile)
        self.assertEqual(first, 122)
        self.assertEqual(len(bus), ((4 * symbols + 15) // 16) * 16 * 8)
        offset = (cursor - first * symbols) * 8 + 13
        got = np.asarray(bus[offset + np.arange(8192) * 17], dtype="<u2").view("<u4").copy()
        def validate(words):
            lines = ["@cap " + str(i) + " " + " ".join(f"{v:08x}" for v in words[i:i + 16])
                     for i in range(0, len(words), 16)]
            result = dict(cap_words=4096, cap_crc=zlib.crc32(words.tobytes()), cap_stride=17, row_symbols=rows,
                          divider256=256, cap_divider256=256, cap_symbol_cursor=cursor,
                          **spi.qpsk_config(code_index), row_profile=profile, pilots=pilots)
            return trial.validate_capture(result, lines, 6 if gcs else 5)
        good = validate(got)
        self.assertTrue(good["cap_aligned"])
        self.assertEqual(good["cap_mismatches"], 0)
        got[300] ^= 1
        self.assertEqual(validate(got)["cap_mismatches"], 1)

    def test_capture_and_corruption(self):
        for profile in ((2, 12, 1.0), (4, 9, 0.8)):
            with self.subTest(profile=profile):
                self.check_capture_and_corruption(*profile)

    def test_high_rate_captures_both_sources_and_pilots(self):
        self.assertEqual(spi.frame_symbols(False), 32490)
        self.assertEqual(spi.frame_symbols(True), 33282)
        for code_index in (8, 9, 10):
            for gcs in (False, True):
                for pilots in (False, True):
                    with self.subTest(code_index=code_index, gcs=gcs, pilots=pilots):
                        self.check_capture_and_corruption(4, 8, None, code_index, gcs,
                                                         pilots, trial.L8_PROFILE)

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

    def test_high_rate_packet_counters_require_matching_code_and_size(self):
        crc_fields = b"".join(spi.pv.message(k, spi.pv.pattern_payload(k))[-4:] for k in range(16))
        for code_index in (8, 9, 10):
            config = spi.qpsk_config(code_index)
            size = 6 * ((config['kbch'] - 80) // 8)
            for source in (5, 6):
                stats = dict.fromkeys(('lost', 'bad_hdr', 'bad_crc', 'bad_sync', 'null_packets',
                                       'short_msgs', 'long_msgs', 'overflows', 'gcs_underflows'), 0)
                stats.update(ok=32, runtime_bbframes=2, runtime_messages=8, bbframes=6,
                             crc_chain=zlib.crc32(crc_fields * 2),
                             bb_crc=spi.first_crcs(gcs=source == 6, rate=spi.QPSK_RATES[code_index]),
                             bb_mode='gcs' if source == 6 else 'ts', ts_packets=224 if source == 5 else 0,
                             gcs_packets=(size + 187) // 188 if source == 6 else 0,
                             gcs_bytes=size if source == 6 else 0)
                r = dict(source=source, pv_spi=stats, **config)
                self.assertEqual(spi.packet_faults(r), [])
                for field, value in (('code_index', 5), ('kbch', config['kbch'] - 8),
                                     ('s2_code', 'short 9/10')):
                    self.assertTrue(spi.packet_faults(dict(r, **{field: value})), (code_index, source, field))

    def test_runner_selects_high_rate_optimizations_and_requires_full_rate_pass(self):
        # Exercise orchestration without constructing a Board or accessing USB.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            build = base/'build'
            build.mkdir()
            for name in ('iqbench.uf2', 'iqbench.elf'):
                (build/name).write_bytes(b'native runner fixture')
            for failed_target in (False, True):
                out = base/str(failed_target)
                board = mock.Mock()
                clock_mode = {'name': 'ddr'}

                def response(command, **kwargs):
                    if command.startswith('dualclock '):
                        clock_mode['name'] = ('hstx', 'ddr', 'inverted')[int(command.split()[1])]
                    if not command.startswith('dualstream '):
                        return {}, []
                    cpw, ms, kernel, source, cap, fault, schedule, divider, rows, code = map(int, command.split()[1:])
                    return dict(row_symbols=rows, schedule=schedule, L=8, divider256=cpw*256,
                                clk_hz=128000000, source=source, bus_priority_mask=0, writers=4,
                                dac_clock=trial.DAC_CLOCK_NAMES[clock_mode['name']], cap_format='pins32',
                                **spi.qpsk_config(code), bch_kernel=3, map_kernel=1, ldpc_pack=1,
                                bch_bank=1, elapsed_us=ms*1000, cpw=cpw, fault=fault), []

                board.cmd.side_effect = response
                argv = ['dual_trial.py', 'gcs', '--out', str(out), '--build', str(build),
                        '--clocks', '128000', '--ms', '60000', '--writers', '4', '--cpws', '1',
                        '--quad-profile', '1', '--code-index', '10', '--map-kernel', '1',
                        '--bch-kernel', '3', '--ldpc-pack', '1', '--bch-bank', '1', '--dac-clock', 'ddr']
                def faults(r, capture, source):
                    return ['target underrun'] if failed_target and r.get('cpw') == 1 else []
                with mock.patch.object(sys, 'argv', argv), mock.patch.object(trial.iq, 'Board', return_value=board), \
                        mock.patch.object(trial.iq, 'run_bench', return_value={}), \
                        mock.patch.object(trial.iq, 'run_stream', return_value={}), \
                        mock.patch.object(trial.iq, 'run_txs2', return_value={}), \
                        mock.patch.object(trial, 'validate_bench', return_value={}), \
                        mock.patch.object(trial, 'validate_capture', return_value={}), \
                        mock.patch.object(trial, 'negative_control_detected', return_value=True), \
                        mock.patch.object(trial, 'stream_faults', side_effect=faults), mock.patch('builtins.print'):
                    if failed_target:
                        with self.assertRaisesRegex(RuntimeError, 'unexpected validation outcomes'):
                            trial.main()
                    else:
                        trial.main()
                commands = [call.args[0] for call in board.cmd.call_args_list]
                for command in ('bchkernel 3', 'ldpcpack 1', 'bchbank 1', 'dualclock 1',
                                'dualstream 1 60000 2 5 4096 0 0 0 4 10',
                                'dualstream 1 60000 2 6 4096 0 0 0 4 10'):
                    self.assertIn(command, commands)
                manifest = json.loads((out/'manifest.json').read_text())
                self.assertEqual({key: manifest[key] for key in ('code_index', 'bch_kernel', 'ldpc_pack', 'bch_bank')},
                                 dict(code_index=10, bch_kernel=3, ldpc_pack=1, bch_bank=1))
                board.s.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
