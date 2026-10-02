"""Exercise the compiled flight USB service against a bounded CDC transport model.

The mock follows TinyUSB's full-packet auto-flush and IN-completion behavior.
It checks short replies and a host that stops reading; it does not verify USB
interrupt timing, enumeration, or waveform continuity on the board.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define PICO_ERROR_TIMEOUT -1
static uint64_t now;
static char fifo[64], pending[64], received[512], expected[512];
static unsigned queued, in_flight, received_count, expected_count;
static unsigned reads, writes, flushes, snapshots, stops;
static bool irq_disabled, zlp;
static const char *input = "status\n";

static uint32_t save_and_disable_interrupts(void) {
    assert(!irq_disabled);
    irq_disabled = true;
    return 0;
}
static void restore_interrupts(uint32_t ignored) {
    (void)ignored;
    assert(irq_disabled);
    irq_disabled = false;
}
static uint64_t time_us_64(void) { return now; }
static unsigned tud_cdc_write_available(void) { return sizeof fifo - queued; }
static unsigned tud_cdc_write_flush(void) {
    ++flushes;
    if (in_flight || zlp || !queued) return 0;
    in_flight = queued;
    memcpy(pending, fifo, queued);
    queued = 0;
    return in_flight;
}
static unsigned tud_cdc_write(const char *data, unsigned count) {
    assert(irq_disabled);
    ++writes;
    assert(count <= sizeof fifo - queued);
    memcpy(fifo + queued, data, count);
    queued += count;
    // TinyUSB automatically starts only full packets from this API.
    if (queued == sizeof fifo) tud_cdc_write_flush();
    return count;
}
static int getchar_timeout_us(unsigned timeout) {
    assert(timeout == 0);
    ++reads;
    return *input ? (unsigned char)*input++ : PICO_ERROR_TIMEOUT;
}
static void tx_request_stop(void) { ++stops; }
static int tx_status_json(char *buffer, unsigned size) {
    ++snapshots;
    int count = snprintf(buffer, size,
        "@{\"cmd\":\"status\",\"active\":1,\"elapsed_ms\":0,\"blocks_out\":0,"
        "\"underruns\":0,\"own_errors\":0,\"txstalls\":0,\"msgs_ok\":0,"
        "\"bad_crc\":0,\"lost\":0,\"crc_chain\":0,\"null_packets\":0,\"ready\":1}\n");
    assert(count > 128 && count < (int)sizeof expected && count % 64);
    memcpy(expected, buffer, count);
    expected_count = count;
    return count;
}

/* FLIGHT_SERVICE */

static void service_once(void) {
    reads = writes = flushes = 0;
    flight_service();
    assert(!irq_disabled && reads <= 16 && writes <= 1 && flushes <= 2);
    now += 1000;
}
static void complete_transfers(void) {
    if (in_flight) {
        unsigned completed = in_flight;
        memcpy(received + received_count, pending, in_flight);
        received_count += in_flight;
        in_flight = 0;
        // TinyUSB's IN completion flushes queued data, or sends a ZLP.
        if (!tud_cdc_write_flush() && completed == sizeof fifo) zlp = true;
    }
    if (zlp) {
        zlp = false;
        tud_cdc_write_flush();
    }
}
int main(int argc, char **argv) {
    assert(argc == 2);
    if (!strcmp(argv[1], "tail")) {
        // The reader completes packets before the next 1 ms service call.
        for (unsigned i = 0; i < 100; ++i) {
            service_once();
            complete_transfers();
        }
        assert(snapshots == 1 && received_count == expected_count);
        assert(!queued && !in_flight && !zlp);
        assert(!memcmp(received, expected, expected_count));
        assert(received[received_count - 1] == '\n');
    } else {
        assert(!strcmp(argv[1], "backpressure"));
        // No IN completion: fill the pending packet and FIFO, retaining the tail.
        for (unsigned i = 0; i < 20; ++i) service_once();
        assert(snapshots == 1 && in_flight == 64 && queued == 64);
        input = "stop\n";
        service_once();
        assert(stops == 1 && !*input && !received_count);
        for (unsigned i = 0; i < 5; ++i) service_once();
        assert(stops == 1 && in_flight == 64 && queued == 64);
    }
    return 0;
}
'''


class FlightService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        main = (ROOT / "firmware/src/main.c").read_text()
        begin = main.index("static void flight_service(void) {")
        end = main.index("\nstatic const char *flight_start", begin)
        source = HARNESS.replace("/* FLIGHT_SERVICE */", main[begin:end])
        build = tempfile.TemporaryDirectory(prefix="pvflight-usb-test-")
        cls.addClassCleanup(build.cleanup)
        path = Path(build.name)
        (path / "flight_service_test.c").write_text(source)
        cls.exe = path / "flight_service_test"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        str(path / "flight_service_test.c"), "-o", str(cls.exe)], check=True)

    def test_status_short_tail_reaches_host(self):
        subprocess.run([str(self.exe), "tail"], check=True, timeout=2)

    def test_stop_remains_bounded_under_host_backpressure(self):
        subprocess.run([str(self.exe), "backpressure"], check=True, timeout=2)


if __name__ == "__main__":
    unittest.main()
