"""Startup ordering and actual native READY admission/pause regressions.

These checks cannot establish GPIO timing or throughput; they catch accidental
relocation of admission initialization into the CS interrupt. The native check
executes the production pause/arm/IRQ functions and the generated PIO gate words.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]

def body(source, name):
    match = re.search(r'\b' + name + r'\)?\([^;]*?\)\s*\{', source)
    if not match:
        raise AssertionError(f'missing function {name}')
    begin = match.end()
    depth = 1
    for pos in range(begin, len(source)):
        depth += (source[pos] == '{') - (source[pos] == '}')
        if not depth:
            return source[begin:pos]
    raise AssertionError(f'unclosed function {name}')

class StartupOrdering(unittest.TestCase):
    def setUp(self):
        self.spi = (ROOT / 'firmware/src/pvspi.c').read_text()
        self.tx = (ROOT / 'firmware/src/tx.c').read_text()

    def test_flight_usb_has_no_reset_interfaces(self):
        cmake = (ROOT / 'firmware/CMakeLists.txt').read_text()
        definitions = re.search(r'target_compile_definitions\(pvflight PRIVATE(.*?)\)', cmake, re.S).group(1)
        self.assertIn('PICO_STDIO_USB_ENABLE_RESET_VIA_BAUD_RATE=0', definitions)
        self.assertIn('PICO_STDIO_USB_ENABLE_RESET_VIA_VENDOR_INTERFACE=0', definitions)
        self.assertIn('target_compile_definitions(iqbench PRIVATE PICO_STDIO_USB_ENABLE_RESET_VIA_BAUD_RATE=1)', cmake)
        service = body((ROOT / 'firmware/src/main.c').read_text(), 'flight_service')
        self.assertNotIn('reset_usb_boot', service)
        self.assertNotIn('bootsel', service)

    def test_admission_only_enabled_in_locked_start(self):
        start = body(self.spi, 'pvspi_start')
        enabled = start.index('admitting = true;')
        self.assertLess(start.rindex('spin_lock_blocking(lock)', 0, enabled), enabled)
        self.assertLess(enabled, start.index('arm_locked();', enabled))
        unmask = start.index('gpio_set_outover(PIN_READY, GPIO_OVERRIDE_NORMAL)', enabled)
        self.assertLess(start.index('arm_locked();', enabled), unmask)
        self.assertLess(unmask, start.index('spin_unlock(lock, s)', enabled))
        self.assertEqual(len(re.findall(r'admitting\s*=\s*true', self.spi)), 1)
        self.assertNotRegex(body(self.spi, 'cs_isr'), r'admitting\s*=')

    def test_pause_survives_consumer_and_cs_rearm(self):
        # READY is PIO-driven now. Checking for a SIO gpio_put would miss stale
        # hardware grants; run the real pause/rearm leaves and delayed-IRQ model.
        from test_pvspi_rx import main as check_ready_gate
        check_ready_gate()

    def test_external_spi_after_output_and_pause_before_stop(self):
        run = body(self.tx, 'tx_run')
        external = 'if (r->pv == 0) pvspi_start(pio2, in_buf, false, NULL, 3);'
        self.assertLess(run.index('produce(s);'), run.index('iqout_start();'))
        self.assertLess(run.index('iqout_start();'), run.index(external))
        self.assertLess(run.index('pvspi_pause();'), run.index('iqout_stop();'))
        self.assertLess(run.index('iqout_stop();'), run.index('multicore_fifo_pop_blocking();'))
        self.assertIn('run.active ? pvspi_next_packet : no_packet', body(self.tx, 'enc_frame'))

if __name__ == '__main__':
    unittest.main()
