"""Run the production c3pair command with native encoders and a pthread core1.

Pico synchronization/clock APIs are mocked; cycle values only exercise command
accounting. Every physical code-ring word and reported CRC is independently
reconstructed with scalar Python BCH/LDPC/PL generation. No hardware is opened.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zlib

import numpy as np

import c3_fec_trial as reference
import fwsrc

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware/src"
RATES = {5: "2/3", 6: "3/4", 8: "5/6", 9: "8/9", 10: "9/10"}
RING = 2048
GUARD = 0xC7A53D19

TX_HEADER = r'''
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "pvproto.h"
enum { IN_WORDS=8192, N_BLOCKS=8, BLOCK_WORDS=4096, CAP_WORDS_MAX=14336 };
#define SEED 0x1234ABCDu
#define __not_in_flash_func(name) name
typedef struct { uint32_t before; _Alignas(16) uint32_t words[N_BLOCKS*BLOCK_WORDS]; uint32_t after; } ring_storage_t;
typedef struct { uint32_t before; _Alignas(16) uint32_t words[IN_WORDS]; uint32_t after; } input_storage_t;
typedef struct { uint32_t before; _Alignas(16) uint32_t words[CAP_WORDS_MAX]; uint32_t after; } capture_storage_t;
extern ring_storage_t test_ring;
extern input_storage_t test_input;
extern capture_storage_t test_capture;
#define ring_buf (test_ring.words)
#define in_buf (test_input.words)
#define cap_buf (test_capture.words)
uint32_t cycles(void);
void enable_cyccnt(void);
void tx_worker_start(void (*fn)(uint32_t), uint32_t arg);
void tx_worker_wait(void);
void prbs_fill(uint32_t *out, size_t count, uint32_t *state);
'''

HARNESS = r'''
#include <assert.h>
#include <pthread.h>
#include <sched.h>
#include <stdlib.h>
#include "tx.h"
static uint32_t pair_crc32(uint32_t crc,const void *p,size_t count);
#define pv_crc32 pair_crc32
#include "bench_c3_pair.c"
#undef pv_crc32

ring_storage_t test_ring;
input_storage_t test_input;
capture_storage_t test_capture;
static pthread_t worker;
static void (*worker_fn)(uint32_t);
static uint32_t worker_arg;
static unsigned worker_active, worker_joins;
static _Thread_local uint32_t ticks;
static uint32_t measured_rings[2][2*RING];
static unsigned crc_calls, prbs_calls;

static uint32_t pair_crc32(uint32_t crc,const void *p,size_t count) {
    assert(crc_calls<2 && count==sizeof measured_rings[0]);
    memcpy(measured_rings[crc_calls++],p,count);
    return pv_crc32(crc,p,count);
}
void prbs_fill(uint32_t *out,size_t count,uint32_t *state) {
    assert(out==in_buf && count==IN_WORDS);
    ++prbs_calls;
    while (count--) { *state^=*state<<13; *state^=*state>>17; *state^=*state<<5; *out++=*state; }
}

uint32_t clock_get_hz(unsigned clock) { assert(clock==clk_sys); return 128000000u; }
uint32_t cycles(void) { return ticks+=128u; }
void enable_cyccnt(void) {}
void mock_wfe(void) { sched_yield(); }
uint32_t save_and_disable_interrupts(void) { return 0; }
void restore_interrupts(uint32_t state) { assert(!state); }
static void *thread_main(void *unused) { (void)unused; worker_fn(worker_arg); return NULL; }
void tx_worker_start(void (*fn)(uint32_t), uint32_t arg) {
    assert(!worker_active);
    worker_fn=fn; worker_arg=arg; worker_active=1;
    assert(!pthread_create(&worker,NULL,thread_main,NULL));
}
void tx_worker_wait(void) {
    assert(worker_active);
    assert(!pthread_join(worker,NULL));
    worker_active=0; ++worker_joins;
}
void dvbs2_transpose32(uint32_t *matrix) {
    dvbs2_transpose_fn fn=dvbs2_transpose_copy(NULL,0);
    assert(fn); fn(matrix);
}

int main(int argc,char **argv) {
    if (argc!=5) return 2;
    const int code=atoi(argv[1]), layout=atoi(argv[2]), who=atoi(argv[3]), options=atoi(argv[4]);
    const uint32_t guard=0xC7A53D19u;
    memset(&test_ring,0xA5,sizeof test_ring);
    memset(&test_input,0xA5,sizeof test_input);
    memset(&test_capture,0xA5,sizeof test_capture);
    test_ring.before=test_ring.after=test_input.before=test_input.after=
        test_capture.before=test_capture.after=guard;
    assert(dvbs2_bch_set_kernel(S2_BCH_T8_ARM));
    assert(dvbs2_frontend_set_bank(1));
    bench_c3_pair(code,2,layout,who,options);
    assert(test_ring.before==guard && test_ring.after==guard);
    assert(test_input.before==guard && test_input.after==guard);
    assert(test_capture.before==guard && test_capture.after==guard);
    assert(dvbs2_bch_get_kernel()==S2_BCH_T8_ARM && dvbs2_frontend_get_bank()==1);
    assert(!worker_active);
    assert(crc_calls==2 ? prbs_calls==1 : prbs_calls==0);
    printf("@meta {\"guards_ok\":true,\"selectors_restored\":true,\"joins\":%u,"
           "\"ring_start\":%lu,\"input_start\":%lu,\"capture_start\":%lu,\"rings\":[",
           worker_joins,(unsigned long)(uintptr_t)ring_buf,(unsigned long)(uintptr_t)in_buf,
           (unsigned long)(uintptr_t)cap_buf);
    for (unsigned job=0;job<2;++job) {
        printf("%s[",job ? "," : "");
        const uint32_t *r=measured_rings[job];
        for (unsigned k=0;k<2*RING;++k) printf("%s%lu",k ? "," : "",(unsigned long)r[k]);
        printf("]");
    }
    puts("]}");
    return 0;
}
'''


def independent_rings(code_index):
    rate = RATES[code_index]
    code = reference.ref.code(rate)
    levels = np.asarray(reference.FOUR_LEVELS_Q30, np.float64) / 2**30
    rings = np.zeros((2, 2, RING), dtype="<u4")
    count = (code.kbch - 80) // 8
    for index in range(2):
        start = index * count % len(reference.SOURCE)
        data = (reference.SOURCE * ((start + count + 255) // 256))[start:start + count]
        bb = reference.ref.bbframe(np.unpackbits(np.frombuffer(data, np.uint8)), rate,
                                   short=False, ro=0.20, sync=0xB9)
        points = reference.scalar_frame(reference.ref.fecframe(bb, rate), rate, False, True, 0)
        codes = np.asarray([np.abs(axis[:, None] - levels).argmin(axis=1)
                            for axis in (points.real, points.imag)], np.uint32)
        prefix = index * reference.FRAME_SYMBOLS % 16
        padded = np.pad(codes, ((0, 0), (prefix, 0)))
        groups = padded.shape[1] // 16
        words = reference.pack_codes(padded[:, :groups * 16])
        # c3pair leaves each first boundary word deferred and its last partial
        # word pending. No publication joins these independent job rings.
        logical = index * reference.FRAME_SYMBOLS // 16
        rings[index, :, (logical + np.arange(1, groups)) & (RING - 1)] = words[:, 1:].T
    return rings


class C7Pair(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="c7-pair-native-")
        directory = Path(cls.temporary.name)
        (directory / "hardware").mkdir()
        (directory / "pico").mkdir()
        (directory / "hardware/clocks.h").write_text(
            '#pragma once\n#include <stdint.h>\nenum {clk_sys};\nuint32_t clock_get_hz(unsigned clock);\n')
        (directory / "hardware/sync.h").write_text(
            '#pragma once\n#include <stdint.h>\n#include <stdatomic.h>\n'
            'void mock_wfe(void);\nuint32_t save_and_disable_interrupts(void);\n'
            'void restore_interrupts(uint32_t state);\n'
            '#define __dmb() atomic_thread_fence(memory_order_seq_cst)\n'
            '#define __sev() ((void)0)\n#define __wfe() mock_wfe()\n')
        (directory / "pico/stdlib.h").write_text('#pragma once\n')
        (directory / "git_rev.h").write_text('#define GIT_REV "native-c7-pair"\n')
        (directory / "tx.h").write_text(TX_HEADER)
        shutil.copyfile(SRC / "bench/bench_c3_pair.c", directory / "bench_c3_pair.c")
        (directory / "harness.c").write_text(HARNESS)
        cls.executable = directory / "bench"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
                        "-I" + str(directory), *fwsrc.INCLUDES, str(directory / "harness.c"),
                        *(str(fwsrc.path(name)) for name in ("c3_fec.c", "dvbs2.c", "dvbs2_8psk.c", "pvproto.c")),
                        "-o", str(cls.executable)], check=True)
        cls.gold = {code: independent_rings(code) for code in RATES}

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_command(self, code=5, layout=0, who=0, options=31):
        result = subprocess.run([str(self.executable), str(code), str(layout), str(who), str(options)],
                                capture_output=True, text=True, check=True, timeout=20)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2, result.stdout[:2000])
        self.assertTrue(lines[0].startswith("@{"))
        self.assertTrue(lines[1].startswith("@meta "))
        return json.loads(lines[0][1:]), json.loads(lines[1][6:])

    def assert_command(self, code, layout, who, options):
        result, meta = self.run_command(code, layout, who, options)
        self.assertNotIn("error", result)
        for field, value in dict(cmd="c3pair", git="native-c7-pair", clk_hz=128000000, code_index=code,
                                 s2_code="normal " + RATES[code], pairs=2, layout=layout,
                                 who=who, hammer=0, options=options).items():
            self.assertEqual(result[field], value)
        self.assertTrue(meta["guards_ok"] and meta["selectors_restored"])
        self.assertEqual(meta["joins"], int(bool(who)))
        self.assertEqual(result["ring_a"], meta["input_start"])
        if layout != 1:
            self.assertEqual(result["ring_b"], meta["input_start"] + 4 * 2 * RING)
        else:
            self.assertLessEqual(meta["capture_start"], result["ring_b"])
            self.assertLessEqual(result["ring_b"] + 4 * 2 * RING, meta["capture_start"] + 57344)
        job_base = meta["capture_start"] if layout in (1, 4) else meta["ring_start"]
        job_extent = 57344 if layout in (1, 4) else 98304
        self.assertLessEqual(job_base, result["job_b"])
        self.assertLess(result["job_b"], job_base + job_extent)
        rings = np.asarray(meta["rings"], dtype="<u4").reshape(2, 2, RING)
        np.testing.assert_array_equal(rings, self.gold[code])
        for index, name in enumerate(("crc_a", "crc_b")):
            self.assertEqual(result[name], zlib.crc32(self.gold[code][index].tobytes()))
        self.assertEqual(result["table3_bytes"], 5120 * (reference.ref.code(RATES[code]).t * 2)
                         if options & 1024 else 0)
        self.assertEqual(result["table3"], meta["ring_start"] if options & 1024 else 0)
        self.assertEqual([bool(x) for x in result["bch_leaf"]], [bool(options & (128 | 1024))] * 2)
        self.assertEqual([bool(x) for x in result["transpose_leaf"]], [bool(options & 512)] * 2)
        self.assertEqual(result["shared_state"], meta["ring_start"] + 128416)
        self.assertLessEqual(result["shared_bytes"], 128)
        self.assertLessEqual(result["shared_state"] + result["shared_bytes"], meta["ring_start"] + 128544)
        if layout == 4:
            self.assertLessEqual(result["wide_bytes"], 57344)
            self.assertEqual(result["gold_a"], meta["ring_start"] + 122880)
            self.assertEqual(result["gold_b"], result["gold_a"])
            self.assertLessEqual(result["table3"] + result["table3_bytes"], result["gold_a"])
            self.assertEqual(result["gold_a"] + 5536, result["shared_state"])
            self.assertEqual(result["place_a"], meta["ring_start"] + 128544)
            self.assertLessEqual(meta["capture_start"], result["place_b"])
            self.assertLessEqual(result["place_b"] + 1024, meta["capture_start"] + result["wide_bytes"])
        else:
            self.assertEqual(result["wide_bytes"], 0)
        self.assertEqual(len(result["jobs"]), 2)
        for index, job in enumerate(result["jobs"]):
            self.assertEqual(job["core"], int(bool(index and who)))
            self.assertEqual(job["runs"], 2)
            self.assertGreater(job["avg_cycles"], 0)
            self.assertGreaterEqual(job["max_cycles"], job["avg_cycles"])
            self.assertTrue(all(value > 0 for value in job["avg_stage"].values()))

    def test_layouts_masks_and_real_worker(self):
        for code in RATES:
            for layout in range(5):
                for who in (0, 2):
                    for options in ((91, 219, 475, 731, 763, 1023) if layout == 3 else
                                    (31, 155, 411, 667, 699, 1023)):
                        with self.subTest(code=code, layout=layout, who=who, options=options):
                            self.assert_command(code, layout, who, options)

    def test_wide_table_same_layout_off_on(self):
        for code in RATES:
            for who in (0, 2):
                with self.subTest(code=code, who=who):
                    for baseline in (27, 155, reference.C8_BEST_BASE, 667, 763, 767):
                        self.assert_command(code, 4, who, baseline)
                        self.assert_command(code, 4, who, baseline | 1024)

    def test_private_transpose_with_and_without_c5(self):
        for code in (5, 8, 6):
            for options in (512, 544, 539, 571, 1787):
                with self.subTest(code=code, options=options):
                    self.assert_command(code, 4, 2, options)

    def test_invalid_wide_requests_are_rejected(self):
        for layout, who, options in ((0, 0, 1275), (1, 2, 1275), (2, 0, 1275),
                                     (3, 0, 1275), (4, 0, reference.C8_ALL),
                                     (4, 3, 251), (5, 0, 251), (3, 0, 27),
                                     (0, 0, reference.C8_ALL + 1), (4, 0, -1)):
            with self.subTest(layout=layout, who=who, options=options):
                result, meta = self.run_command(5, layout, who, options)
                self.assertIn("error", result)
                self.assertEqual(meta["joins"], 0)
                self.assertTrue(meta["guards_ok"] and meta["selectors_restored"])


if __name__ == "__main__":
    unittest.main()
