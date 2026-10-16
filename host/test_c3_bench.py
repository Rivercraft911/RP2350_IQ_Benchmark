"""Production C3 command with virtual time, real encoder stages, and no hardware.

The unchanged benchmark source is compiled beside mocked Pico/worker headers.
A deterministic event pump runs core 1's actual work() slices while the real
command schedules, publishes and checks deadlines. Synthetic slice costs only
exercise scheduler behavior; none of these times estimate an RP2350 measurement.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zlib

import c3_fec_trial as trial

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware/src"

TX_HEADER = r'''
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "pvproto.h"
enum { IN_WORDS=8192, N_BLOCKS=8, BLOCK_WORDS=4096 };
#define SEED 0x1234ABCDu
typedef struct { uint32_t before; _Alignas(16) uint32_t words[N_BLOCKS*BLOCK_WORDS]; uint32_t after; } ring_storage_t;
typedef struct { uint32_t before; _Alignas(16) uint32_t words[IN_WORDS]; uint32_t after; } input_storage_t;
extern ring_storage_t test_ring;
extern input_storage_t test_input;
#define ring_buf (test_ring.words)
#define in_buf (test_input.words)
uint32_t cycles(void);
void enable_cyccnt(void);
void tx_worker_start(void (*fn)(uint32_t), uint32_t arg);
void tx_worker_wait(void);
void prbs_fill(uint32_t *out, size_t count, uint32_t *state);
'''

HARNESS = r'''
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include "tx.h"

static unsigned test_core, test_hz=128000000u, pump_active, worker_active, worker_joins;
static unsigned inject_service_gap, injected;
static uint64_t test_now, next_worker, worker_ticks;
static unsigned step_us;

// Preserve all production c3_job_step logic; only its modeled time is wrapped.
#define c3_job_step real_c3_job_step
#include "C3_SOURCE"
#undef c3_job_step
bool c3_job_step(c3_job_t *job, const uint8_t *pattern) {
    const bool ok=real_c3_job_step(job,pattern);
    unsigned duration=1;
    if (!test_core && inject_service_gap && !injected) { duration=200; injected=1; }
    if (test_core) { worker_ticks+=(uint64_t)duration*(test_hz/1000000u); step_us+=duration; }
    else test_now+=duration;
    return ok;
}

#include "bench_c3_fec.c"

ring_storage_t test_ring;
input_storage_t test_input;

uint32_t clock_get_hz(unsigned clock) { assert(clock==clk_sys); return test_hz; }
uint32_t cycles(void) {
    return (uint32_t)(test_core ? worker_ticks : test_now*(test_hz/1000000u));
}
void enable_cyccnt(void) {}

static void pump(void) {
    if (!worker_active || pump_active) return;
    pump_active=1;
    while (next_worker<=test_now && !live.stop) {
        if (live.core[1].status!=RUN) { next_worker=test_now+1; break; }
        test_core=1;
        step_us=0;
        work(1);
        test_core=0;
        next_worker+=step_us ? step_us : 1u;
    }
    pump_active=0;
}

uint64_t time_us_64(void) {
    if (!pump_active) { ++test_now; pump(); }
    return test_now;
}
void busy_wait_us_32(uint32_t duration) {
    if (test_core) { worker_ticks+=(uint64_t)duration*(test_hz/1000000u); step_us+=duration; }
    else test_now+=duration;
}
void tx_worker_start(void (*fn)(uint32_t), uint32_t arg) {
    assert(fn==core1 && arg==0 && !worker_active);
    worker_active=1;
    next_worker=test_now;
}
void tx_worker_wait(void) {
    assert(worker_active && live.stop);
    assert(live.core[0].status!=RUN && live.core[1].status!=RUN);
    worker_active=0;
    ++worker_joins;
}
void prbs_fill(uint32_t *out, size_t count, uint32_t *state) {
    while (count--) { *state^=*state<<13; *state^=*state>>17; *state^=*state<<5; *out++=*state; }
}

int main(int argc, char **argv) {
    if (argc!=6) return 2;
    const int code=atoi(argv[1]), slow=atoi(argv[2]), cpw=atoi(argv[3]);
    test_hz=(unsigned)strtoul(argv[4],NULL,10);
    inject_service_gap=(unsigned)atoi(argv[5]);
    const uint32_t guard=0xC39A75B1u;
    test_ring.before=test_ring.after=test_input.before=test_input.after=guard;
    for (unsigned j=0;j<N_BLOCKS*BLOCK_WORDS;++j) ring_buf[j]=0x57EAD219u;
    memset(in_buf,0xA5,sizeof in_buf);
    assert(dvbs2_bch_set_kernel(S2_BCH_T8_ARM));
    assert(dvbs2_frontend_set_bank(1));
    assert(dvbs2_ldpc_set_pack(1));
    assert(dvbs2_map_set_kernel(1));
    bench_c3_fec(code,100,slow,cpw,128);
    assert(test_ring.before==guard && test_ring.after==guard);
    assert(test_input.before==guard && test_input.after==guard);
    for (unsigned j=0;j<16384;++j) assert(ring_buf[j]==0x57EAD219u);
    assert(dvbs2_bch_get_kernel()==S2_BCH_T8_ARM);
    assert(dvbs2_frontend_get_bank()==1);
    assert(dvbs2_ldpc_get_pack()==1 && dvbs2_map_get_kernel()==1);
    assert(!worker_active && worker_joins==1);
    printf("@meta {\"input_crc\":%lu,\"selectors_restored\":true,\"guards_ok\":true,\"worker_joins\":%u}\n",
           (unsigned long)pv_crc32(0,in_buf,sizeof in_buf),worker_joins);
    return 0;
}
'''


class C3Bench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="c3-command-native-")
        directory = Path(cls.temporary.name)
        (directory / "hardware").mkdir()
        (directory / "pico").mkdir()
        (directory / "hardware/clocks.h").write_text(
            '#pragma once\n#include <stdint.h>\nenum {clk_sys};\nuint32_t clock_get_hz(unsigned clock);\n')
        (directory / "hardware/sync.h").write_text(
            '#pragma once\n#define __dmb() ((void)0)\n#define __sev() ((void)0)\n#define __wfe() ((void)0)\n')
        (directory / "pico/stdlib.h").write_text(
            '#pragma once\n#include <stdint.h>\nuint64_t time_us_64(void);\nvoid busy_wait_us_32(uint32_t us);\n')
        (directory / "git_rev.h").write_text('#define GIT_REV "native-c3-command"\n')
        (directory / "tx.h").write_text(TX_HEADER)
        # Copy byte-for-byte so quote includes resolve to mocks, while scheduling,
        # JSON formatting, bounds and publication remain the production source.
        shutil.copyfile(SRC / "bench_c3_fec.c", directory / "bench_c3_fec.c")
        (directory / "harness.c").write_text(HARNESS.replace("C3_SOURCE", str(SRC / "c3_fec.c")))
        cls.executable = directory / "bench"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        "-I" + str(directory), "-I" + str(SRC), str(directory / "harness.c"),
                        str(SRC / "dvbs2.c"), str(SRC / "dvbs2_8psk.c"), str(SRC / "pvproto.c"),
                        "-o", str(cls.executable)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_command(self, code=5, slow=0, cpw=1, hz=128000000, gap=0):
        process = subprocess.run([str(self.executable), str(code), str(slow), str(cpw), str(hz), str(gap)],
                                 capture_output=True, text=True, check=True, timeout=30)
        lines = process.stdout.splitlines()
        self.assertEqual(len(lines), 2, process.stdout)
        self.assertTrue(lines[0].startswith("@{"), process.stdout)
        self.assertTrue(lines[1].startswith("@meta "), process.stdout)
        result, meta = json.loads(lines[0][1:]), json.loads(lines[1][6:])
        expected = trial.iq.m.xorshift32(0x1234ABCD, 8192).astype("<u4")
        self.assertEqual(meta, dict(input_crc=zlib.crc32(expected.tobytes()), selectors_restored=True,
                                    guards_ok=True, worker_joins=1))
        return result

    def test_real_encoder_command_positive_all_requested_rates(self):
        for code in trial.RATES:
            with self.subTest(code=code):
                result = self.run_command(code)
                self.assertEqual(trial.run_faults(result, code, 100, 0), [], result)
                self.assertEqual(result["core"][0]["frames"], 36)
                self.assertEqual(result["core"][1]["frames"], 36)
                self.assertGreater(result["published_groups"], 4096)

    def test_symbol_clock_ratio_and_slow_rate_control(self):
        for cpw, hz in ((2, 128000000), (2, 256000000), (4, 256000000)):
            with self.subTest(cpw=cpw, hz=hz):
                result = self.run_command(cpw=cpw, hz=hz)
                self.assertEqual(trial.run_faults(result, 5, 100, 0, cpw=cpw), [], result)
                self.assertEqual(result["frame_cycles"], 177552 * cpw)

    def test_slow_negative_stops_releases_drains_and_publishes(self):
        result = self.run_command(slow=5000)
        self.assertEqual(trial.run_faults(result, 5, 100, 5000), [], result)
        self.assertEqual(result["late_frames"], 1)
        self.assertEqual(result["consumed_frames"], 0)
        self.assertEqual(result["published_frames"], 2)
        self.assertEqual([core["frames"] for core in result["core"]], [1, 1])
        self.assertTrue(any("cadence" in x for x in trial.run_faults(result, 5, 100, 0)))

    def test_service_gap_fault_detected_and_active_frames_drained(self):
        result = self.run_command(gap=1)
        self.assertTrue(result["errors"] & 4, result)
        self.assertGreater(result["max_service_gap_us"], 128)
        self.assertEqual(result["late_frames"], 0)
        self.assertEqual(result["published_frames"], 2)
        expected = trial.ring_reference(5, result["published_frames"], 4096)
        self.assertEqual(result["code_crc"], expected["code_crc"])
        self.assertTrue(trial.run_faults(result, 5, 100, 0))


if __name__ == "__main__":
    unittest.main()
