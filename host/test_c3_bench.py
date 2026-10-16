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
extern uint32_t cap_buf[14336];
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
static unsigned inject_service_gap, injected, timer_reads;
static uint64_t test_now, next_worker, worker_ticks;
static unsigned step_us;
static unsigned job_calls[2][4];

// Preserve all production c3_job_step logic; only its modeled time is wrapped.
#define c3_job_step real_c3_job_step
#include "C3_SOURCE"
#undef c3_job_step
bool c3_job_step(c3_job_t *job, const uint8_t *pattern) {
    assert(job->stage<4u);
    ++job_calls[test_core][job->stage];
    const bool ok=real_c3_job_step(job,pattern);
    unsigned duration=1;
    if (!test_core && (inject_service_gap&1u) && !injected) { duration=200; injected=1; }
    if (test_core) { worker_ticks+=(uint64_t)duration*(test_hz/1000000u); step_us+=duration; }
    else test_now+=duration;
    return ok;
}

#include "bench_c3_fec.c"

ring_storage_t test_ring;
input_storage_t test_input;
uint32_t cap_buf[14336];

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
    ++timer_reads;
    if (!pump_active) { ++test_now; pump(); }
    return test_now;
}
void busy_wait_us_32(uint32_t duration) {
    if (test_core) { worker_ticks+=(uint64_t)duration*(test_hz/1000000u); step_us+=duration; }
    else test_now+=duration;
}
void tx_worker_start(void (*fn)(uint32_t), uint32_t arg) {
    assert(fn==core1 && arg==0 && !worker_active);
    test_now+=60; // modeled launch latency, never an M33 timing estimate
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
    if (argc!=7) return 2;
    const int code=atoi(argv[1]), slow=atoi(argv[2]), cpw=atoi(argv[3]);
    test_hz=(unsigned)strtoul(argv[4],NULL,10);
    inject_service_gap=(unsigned)atoi(argv[5]);
    if (inject_service_gap&2u) test_now=33554400u; // cross the 32-bit 128-MHz cycle counter
    const uint32_t guard=0xC39A75B1u;
    test_ring.before=test_ring.after=test_input.before=test_input.after=guard;
    for (unsigned j=0;j<N_BLOCKS*BLOCK_WORDS;++j) ring_buf[j]=0x57EAD219u;
    memset(in_buf,0xA5,sizeof in_buf);
    pv_init(16); // initialize the CRC table even when command admission fails
    assert(dvbs2_bch_set_kernel(S2_BCH_T8_ARM));
    assert(dvbs2_frontend_set_bank(1));
    assert(dvbs2_ldpc_set_pack(1));
    assert(dvbs2_map_set_kernel(1));
    const int options=atoi(argv[6]);
    const unsigned rejected=options<0 || options>C9_ALL || (options&C7_TABLE3) ||
        ((options&C9_BCH_TABLE4) && (options&C7_BCH_LDM));
    bench_c3_fec_options(code,100,slow,cpw,128,options);
    assert(test_ring.before==guard && test_ring.after==guard);
    assert(test_input.before==guard && test_input.after==guard);
    for (unsigned j=0;j<16384;++j) assert(ring_buf[j]==0x57EAD219u);
    assert(dvbs2_bch_get_kernel()==S2_BCH_T8_ARM);
    assert(dvbs2_frontend_get_bank()==1);
    assert(dvbs2_ldpc_get_pack()==1 && dvbs2_map_get_kernel()==1);
    assert(!worker_active && worker_joins==!rejected);
    if (!rejected) {
        // Independently derive the absolute deadline from frame symbols and
        // SYS Hz, over the entire 60-second command range and counter-wrap test.
        live.options=C9_SCHEDULE;
        for (unsigned clock=128000000u;clock<=256000000u;clock*=2u) {
            live.hz=clock;
            for (unsigned ratio=1;ratio<=4;ratio*=2u) {
                live.cpw=ratio;
                for (unsigned frame=0;frame<=86510u;++frame) {
                    const uint64_t numerator=(uint64_t)(frame+2u)*22194u*8u*ratio*1000000u;
                    assert(deadline(frame)==live.epoch+(numerator+clock-1u)/clock);
                }
            }
        }
    }
    printf("@meta {\"input_crc\":%lu,\"selectors_restored\":true,\"guards_ok\":true,\"worker_joins\":%u,\"timer_reads\":%u,"
           "\"job_calls\":[[%u,%u,%u,%u],[%u,%u,%u,%u]]}\n",
           (unsigned long)pv_crc32(0,in_buf,sizeof in_buf),worker_joins,timer_reads,
           job_calls[0][0],job_calls[0][1],job_calls[0][2],job_calls[0][3],
           job_calls[1][0],job_calls[1][1],job_calls[1][2],job_calls[1][3]);
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

    def run_command(self, code=5, slow=0, cpw=1, hz=128000000, gap=0, options=0):
        process = subprocess.run([str(self.executable), str(code), str(slow), str(cpw), str(hz), str(gap), str(options)],
                                 capture_output=True, text=True, check=True, timeout=30)
        lines = process.stdout.splitlines()
        self.assertEqual(len(lines), 2, process.stdout)
        self.assertTrue(lines[0].startswith("@{"), process.stdout)
        self.assertTrue(lines[1].startswith("@meta "), process.stdout)
        result, meta = json.loads(lines[0][1:]), json.loads(lines[1][6:])
        rejected = (options < 0 or options > trial.C9_ALL or bool(options & 1024)
                    or bool(options & 2048 and options & 256))
        expected = (trial.iq.m.xorshift32(0x1234ABCD, 8192).astype("<u4").tobytes()
                    if not rejected else b"\xA5" * 32768)
        timer_reads = meta.pop("timer_reads")
        job_calls = meta.pop("job_calls")
        self.assertEqual(meta, dict(input_crc=zlib.crc32(expected), selectors_restored=True,
                                    guards_ok=True, worker_joins=int(not rejected)))
        result["timer_reads"] = timer_reads
        result["job_calls"] = job_calls
        if not rejected:
            if result["late_frames"]:
                self.assertEqual(result["min_publish_slack_us"], 0)
            else:
                self.assertGreater(result["min_publish_slack_us"], 0)
        return result

    def test_real_encoder_command_positive_all_requested_rates(self):
        for code in trial.RATES:
            with self.subTest(code=code):
                result = self.run_command(code)
                self.assertEqual(trial.run_faults(result, code, 100, 0), [], result)
                self.assertEqual(result["core"][0]["frames"], 36)
                self.assertEqual(result["core"][1]["frames"], 36)
                self.assertGreater(result["published_groups"], 4096)

    def test_each_fec_option_and_combinations(self):
        for code in trial.RATES:
            for options in (1, 2, 4, 8, 15, 16, 27, 31, 32, 33, 59, 63, 64, 66,
                            91, 95, 96, 123, 127, 128, 155, trial.C8_BEST_BASE,
                            256, 283, 379, 384, 512, 539, 544, 635, 640, 763, 767,
                            895, trial.C8_FEC_ALL, 2048, 2299, 4096, 4347, 4603,
                            6395, 8192, 8443, 12539, 12795, 14587):
                with self.subTest(code=code, options=options):
                    result = self.run_command(code, options=options)
                    self.assertEqual(result["options"], options)
                    self.assertEqual(trial.run_faults(result, code, 100, 0, options=options), [])
        for options in (31, 32, 64, 123, trial.C8_BEST_BASE, 512, 544, trial.C8_FEC_ALL,
                        4347, 6395, 12539, 12795, 14587):
            result = self.run_command(slow=5000, options=options)
            self.assertEqual(trial.run_faults(result, 5, 100, 5000, options=options), [])

    def test_symbol_clock_ratio_and_slow_rate_control(self):
        for cpw, hz in ((2, 128000000), (2, 256000000), (4, 256000000)):
            with self.subTest(cpw=cpw, hz=hz):
                result = self.run_command(cpw=cpw, hz=hz)
                self.assertEqual(trial.run_faults(result, 5, 100, 0, cpw=cpw), [], result)
                self.assertEqual(result["frame_cycles"], 177552 * cpw)
                cached = self.run_command(cpw=cpw, hz=hz, options=trial.C8_FEC_ALL)
                self.assertEqual(trial.run_faults(cached, 5, 100, 0, cpw=cpw, options=trial.C8_FEC_ALL), [])
                for field in ("frames_target", "published_frames", "consumed_frames", "code_crc"):
                    self.assertEqual(cached[field], result[field])

    def test_table3_and_unknown_bits_rejected_before_mutation(self):
        for options in (-1, 1024, trial.C8_BEST, trial.C8_ALL, trial.C9_ALL,
                        trial.C9_ALL + 1, 2304, 6400):
            with self.subTest(options=options):
                result = self.run_command(options=options)
                self.assertIn("error", result)

    def test_slow_negative_stops_releases_drains_and_publishes(self):
        result = self.run_command(slow=5000)
        self.assertEqual(trial.run_faults(result, 5, 100, 5000), [], result)
        self.assertEqual(result["late_frames"], 1)
        self.assertEqual(result["consumed_frames"], 0)
        self.assertEqual(result["published_frames"], 2)
        self.assertEqual([core["frames"] for core in result["core"]], [1, 1])
        self.assertTrue(any("cadence" in x for x in trial.run_faults(result, 5, 100, 0)))

    def test_service_gap_fault_detected_and_active_frames_drained(self):
        for options in (0, 4347, 6395, 12539, 12795, 14587):
            with self.subTest(options=options):
                result = self.run_command(gap=1, options=options)
                self.assertTrue(result["errors"] & 4, result)
                self.assertGreater(result["max_service_gap_us"], 128)
                self.assertEqual(result["late_frames"], 0)
                self.assertEqual(result["published_frames"], 2)
                expected = trial.ring_reference(5, result["published_frames"], 4096)
                self.assertEqual(result["code_crc"], expected["code_crc"])
                self.assertTrue(trial.run_faults(result, 5, 100, 0, options=options))

    def test_fast_schedule_keeps_output_and_deadlines_across_cycle_counter_wrap(self):
        for code in trial.RATES:
            baseline = self.run_command(code, options=251)
            fast = self.run_command(code, options=4347)
            wrapped = self.run_command(code, options=4347, gap=2)
            for result in (fast, wrapped):
                self.assertEqual(trial.run_faults(result, code, 100, 0, options=4347), [])
                for field in ("frames_target", "published_frames", "consumed_frames", "code_crc"):
                    self.assertEqual(result[field], baseline[field])
            self.assertLess(fast["timer_reads"], baseline["timer_reads"])
            self.assertLess(fast["startup_us"], baseline["startup_us"])
            self.assertLess(fast["job_calls"][0][1], baseline["job_calls"][0][1])
            self.assertLess(fast["job_calls"][0][2], baseline["job_calls"][0][2])


if __name__ == "__main__":
    unittest.main()
