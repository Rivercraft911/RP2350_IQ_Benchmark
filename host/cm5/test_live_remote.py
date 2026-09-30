"""Test runner configuration without touching either board."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from live_remote import configure_capture, native_summary, validate_options


def options(**changes):
    values = dict(label='test-run', seconds=120, threads=3, sender_nice=0,
                  sender_cpus=None, capture_cpus=None, profile_sender=False,
                  native_spi=True, udp_mode='threaded', transfer='duplex',
                  allocator='dma_heap_cached', encoder_input='dmabuf')
    values.update(changes)
    return SimpleNamespace(**values)


class RunnerChecks(unittest.TestCase):
    def test_native_uses_spi_without_udp(self):
        original = {'udp_destination': '127.0.0.1:1234', 'bitrate': 4000000}
        config = configure_capture(original, options(), '/test/session')
        self.assertIsNone(config['udp_destination'])
        self.assertEqual(config['spi']['hz'], 20000000)
        self.assertEqual(config['bitrate'], 4000000)
        self.assertEqual(original['udp_destination'], '127.0.0.1:1234')

    def test_python_clears_existing_spi(self):
        config = configure_capture({'spi': {'hz': 20000000}},
                                   options(native_spi=False), '/test/session')
        self.assertNotIn('spi', config)
        self.assertEqual(config['udp_destination'], '127.0.0.1:1234')

    def test_native_rejects_unused_sender_options(self):
        validate_options(options())
        for change in ({'sender_cpus': '1'}, {'sender_nice': -5},
                       {'profile_sender': True}, {'transfer': 'tx-only'},
                       {'udp_mode': 'direct'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_options(options(**change))

    def test_rejects_invalid_paths_and_cpu_lists(self):
        for change in ({'label': '../escape'}, {'capture_cpus': '0;shutdown'},
                       {'seconds': 0}, {'threads': 0}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_options(options(**change))

    def test_summary_preserves_raw_health(self):
        final = {'outputs': {'transport': {'spi': {'crc_chain': 0x12345678, 'messages': 2}}},
                 'failed': False, 'signal': 0}
        before = deepcopy(final)
        summary = native_summary(final, None, 0)
        self.assertEqual(summary['crc_chain'], '0x12345678')
        self.assertEqual(summary['status'], 'complete')
        self.assertEqual(final, before)
        final['signal'] = 15
        self.assertEqual(native_summary(final, None, 0)['status'], 'error')
        self.assertIsNone(native_summary(None, None, 0))


if __name__ == '__main__':
    unittest.main()
