"""Regression checks against the recorded two-camera 60-second run."""
import json
from pathlib import Path
import tempfile
import unittest

from analyze_live import analyze

EVIDENCE = Path(__file__).resolve().parents[2] / 'results/cm5-spi/current-cached-t3-priority-60s'


class EvidenceChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.report = json.loads((EVIDENCE / 'report.json').read_text())
        self.pico = json.loads((EVIDENCE / 'pico.json').read_text())
        self.frames = (EVIDENCE / 'frames.jsonl').read_text()

    def result(self, camera_only=False):
        (self.directory / 'report.json').write_text(json.dumps(self.report))
        (self.directory / 'pico.json').write_text(json.dumps(self.pico))
        (self.directory / 'frames.jsonl').write_text(self.frames)
        return analyze(self.directory, camera_only=camera_only)

    def test_observed_run(self):
        result = self.result()
        self.assertEqual(result['failures'], [])
        self.assertGreaterEqual(result['observed_session_duration_s'], 60)
        self.assertAlmostEqual(result['measured_transport_mbps'], 9.001597, places=5)
        self.assertLess(result['cameras'][0]['steady_encoded_fps'], 29)
        self.assertGreater(result['cameras'][0]['delivered_intervals_over_50ms'], 50)

    def test_clean_exit_does_not_hide_sigterm(self):
        self.report['health'][-1]['signal'] = 15
        self.assertIn('process/session failure', self.result()['failures'])

    def test_shortened_capture(self):
        self.report['health'][-1]['timestamp_ns'] = self.report['manifest']['clock_origin_ns'] + 30_000_000_000
        self.assertIn('capture shorter than requested duration', self.result()['failures'])

    def test_harness_error(self):
        self.report['error'] = 'capture ignored encoder_threads'
        self.assertIn('process/session failure', self.result()['failures'])

    def test_crc_mismatch(self):
        self.pico['pv']['crc_chain'] ^= 1
        self.assertIn('CRC chain mismatch', self.result()['failures'])

    def test_udp_overload(self):
        self.report['sender']['udp']['queue_overflows'] = 1
        self.assertIn('queue_overflows', self.result()['failures'])

    def test_unread_kernel_datagram(self):
        self.report['sender']['udp']['kernel_pending_on_close'] = True
        self.assertIn('unread UDP datagrams at shutdown', self.result()['failures'])

    def test_native_counts_without_udp(self):
        self.report['sender']['implementation'] = 'native'
        del self.report['sender']['udp']
        self.assertEqual(self.result()['failures'], [])
        self.report['sender']['pending_payload_bytes'] = 1316
        self.assertIn('pending_payload_bytes', self.result()['failures'])
        self.report['sender']['crc_chain'] = '0x00000000'
        self.assertIn('CRC chain mismatch', self.result()['failures'])

    def test_lost_video_before_spi(self):
        self.report['health'][-1]['outputs']['transport']['dropped_packets'] = 1
        self.assertIn('capture transport failed or dropped packets', self.result()['failures'])

    def test_lost_recording(self):
        self.report['health'][-1]['outputs']['recorders']['A']['written_packets'] -= 1
        self.assertIn('A recorder failed or lost packets', self.result()['failures'])

    def test_selftest_is_not_external_input(self):
        self.pico['pv']['selftest'] = 1
        self.assertIn('Pico mode, message type or coefficient verification differs', self.result()['failures'])

    def test_missing_process_exit(self):
        self.report['capture_exit'] = None
        self.assertIn('process/session failure', self.result()['failures'])

    def test_count_mismatches(self):
        for key in ('messages', 'ts_packets', 'payload_bytes'):
            with self.subTest(key=key):
                self.report['sender'][key] += 1
                self.assertTrue(self.result()['failures'])
                self.report['sender'][key] -= 1

    def test_unmonitored_udp(self):
        self.report['sender']['udp']['kernel_drop_monitor'] = False
        self.assertIn('UDP receive buffer or drop monitor unavailable', self.result()['failures'])

    def test_camera_only_control_has_no_pico_claim(self):
        self.report['sink_exit'] = 0
        self.report['sink'] = {'bytes': self.report['health'][-1]['outputs']['transport']['wire_bytes']}
        result = self.result(camera_only=True)
        self.assertEqual(result['failures'], [])
        self.assertIsNone(result['messages'])
        self.assertIsNone(result['pico_crc_chain'])

    def test_camera_only_udp_loss(self):
        self.report['sink_exit'] = 0
        self.report['sink'] = {'bytes': 0}
        self.assertIn('capture/UDP sink byte count mismatch', self.result(camera_only=True)['failures'])

    def test_untimed_frame_drop(self):
        frame = json.loads(self.frames.splitlines()[0])
        frame.update(camera_id='A', pts_us=None, status='dropped', drop_reason='camera_sequence_gap')
        self.frames += json.dumps(frame) + '\n'
        result = self.result()
        self.assertEqual(result['cameras'][0]['untimed_drops'], {'camera_sequence_gap': 1})


if __name__ == '__main__':
    unittest.main()
