"""Run the production B8 command with real FEC/ingress and virtual peripherals.

Virtual fixed-cost slices test scheduling and ownership only, never M33 throughput,
DMA arbitration, or GPIO timing. The final packed rings are checked against the
independent Python GCS/scalar FEC/complex PLFRAME reference.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zlib

import numpy as np
import b8_lean8_trial as trial

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware/src"

TX_HEADER = r'''
#pragma once
#include <stddef.h>
#include "config.h"
#include "iqgen.h"
#include "pvspi.h"
#define SEED 0x1234ABCDu
extern uint32_t *ring_storage;
#define ring_buf (*(uint32_t (*)[N_BLOCKS*BLOCK_WORDS])ring_storage)
typedef struct { uint32_t before; _Alignas(16384) uint32_t words[IN_WORDS]; uint32_t after; } input_storage_t;
extern input_storage_t test_input;
#define in_buf (test_input.words)
extern uint32_t cap_buf[CAP_WORDS_MAX];
extern iq_cfg_t cfg;
uint32_t cycles(void);
void enable_cyccnt(void);
void tx_worker_start(void (*fn)(uint32_t), uint32_t);
void tx_worker_wait(void);
const char *tx_shaper(const char *, int, int, const iq_kernel_info_t **, bool *);
'''

HARNESS = r'''
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "tx.h"
#define IQ_ON_DEVICE 1
#include "iq4ptr.h"
#undef IQ_ON_DEVICE
#include "dual_dma.h"
#include "link.h"
static unsigned test_core, pump_active, worker_active, joins, step_us, overlapping, out_of_order;
static unsigned run_cpw, starts, stops, prepares, pointer_stops, captures, restores;
static unsigned writers=4, rows=4, bits=14, schedule=3, priority=7, clock_mode;
static uint64_t tick, next_worker, worker_ticks, output_epoch;
static bool running, pointer_running, spi_active, selector_failed, injected;
static const char *mode;
static bool is(const char *s) { return !strcmp(mode,s); }
static void charge_time(unsigned duration) {
    if(test_core) { worker_ticks+=(uint64_t)duration*128u;step_us+=duration; }
    else tick+=duration;
}
#define c3_job_step real_c3_job_step
#include "C3_SOURCE"
#undef c3_job_step
bool c3_job_step(c3_job_t *job,const uint8_t *pattern) {
    bool ok=real_c3_job_step(job,pattern);charge_time(test_core?1:6);return ok;
}
#define b8_ingress_step real_b8_ingress_step
#include "INGRESS_SOURCE"
#undef b8_ingress_step
b8_ingress_result_t b8_ingress_step(b8_ingress_t *s,c3_job_t *j,pv_packet_fn next,uint32_t max) {
    b8_ingress_result_t result=real_b8_ingress_step(s,j,next,max);
    unsigned duration=1;
    if(worker_active && is("slice") && !injected) { duration=200;injected=true; }
    charge_time(duration);return result;
}
static void mock_build_tables(const iq_cfg_t *);
#define iq_build_tables mock_build_tables
#include "b8_lean8.c"
#undef iq_build_tables

uint32_t *ring_storage;
input_storage_t test_input;
uint32_t cap_buf[CAP_WORDS_MAX],link_ring[LINK_RING_WORDS];
static uint32_t table_i[TABLE_WORDS],table_q[TABLE_WORDS];
iq_cfg_t cfg={.ti=table_i,.tq=table_q};
pvspi_stats_t pvspi;
static uint32_t message[PV_MSG_WORDS], message_number, packet_number=7;
static iq4ptr_progress_t progress;
static uint32_t fifo[2];

static void pump(void) {
    if(!worker_active || pump_active)return;
    pump_active=1;
    while(next_worker<=tick && !live.stop) {
        if(live.core[1].status!=RUN) { next_worker=tick+1;break; }
        if(live.core[0].status==RUN)overlapping++;
        test_core=1;step_us=0;work(1);test_core=0;
        if(live.core[1].status==READY && live.core[0].status==RUN && live.core[0].job.index<live.core[1].job.index)out_of_order++;
        next_worker+=step_us?step_us:1;
    }
    pump_active=0;
}
uint64_t time_us_64(void) { if(!pump_active) { ++tick;pump(); }return tick; }
uint32_t time_us_32(void) {
    if(worker_active && tick-output_epoch>1000 && is("gap") && !injected) { tick+=200;injected=true; }
    return (uint32_t)time_us_64();
}
void tight_loop_contents(void) { (void)time_us_64(); }
void busy_wait_us_32(uint32_t us) { tick+=us;pump(); }
uint32_t cycles(void) { return (uint32_t)(test_core?worker_ticks:tick*128u); }
void enable_cyccnt(void) {}
uint32_t clock_get_hz(unsigned clock) { assert(clock==clk_sys);return is("clockbad")?150000000u:128000000u; }
bool dma_channel_is_claimed(unsigned channel) { return is("busy") && channel<5; }
bool pio_sm_is_claimed(PIO pio,unsigned sm) { return is("smbusy") && pio->index==1 && sm==0; }
void tx_worker_start(void (*fn)(uint32_t),uint32_t arg) {
    assert(fn==core1 && arg==0 && !worker_active);worker_active=1;next_worker=tick;
}
void tx_worker_wait(void) {
    assert(worker_active && live.stop && live.core[0].status!=RUN && live.core[1].status!=RUN);
    worker_active=0;joins++;
}
const char *tx_shaper(const char *kernel,int sps,int span,const iq_kernel_info_t **unused,bool *ok) {
    assert(!strcmp(kernel,"lut_asm_p") && sps==4 && span==12);(void)unused;*ok=!is("tablesbad");return NULL;
}
static void mock_build_tables(const iq_cfg_t *config) { assert(config==&cfg);restores++; }
void pvspi_start(PIO pio,uint32_t *slots,bool selftest,uint32_t *emu,int half) {
    assert(pio==pio1 && slots==link_ring && selftest && emu==cfg.tq && half==2);
    memset(&pvspi,0,sizeof pvspi);spi_active=true;message_number=0;packet_number=7;
}
void pvspi_pause(void) { assert(spi_active); }
void pvspi_stop(void) { assert(spi_active && !worker_active);spi_active=false; }
uint8_t *pvspi_next_packet(void) {
    assert(spi_active && !test_core);
    if(is("spiwait"))return NULL;
    if(packet_number==7) {
        if(message_number)pvspi.tail++;
        pv_pattern_message((uint8_t *)message,message_number%PV_EMU_MSGS,PV_EMU_MSGS);
        assert(pv_accept((uint8_t *)message,1)==7);
        message_number++;pvspi.head++;packet_number=0;
    }
    return (uint8_t *)message+12+PV_TS*packet_number++;
}
unsigned dual_dma_get_writers(void) { return writers; }
unsigned dual_dma_get_row_words(void) { return rows; }
unsigned dual_dma_get_sample_bits(void) { return bits; }
unsigned dual_dma_get_schedule(void) { return schedule; }
unsigned dual_dma_get_bus_priority(void) { return priority; }
unsigned dual_dma_get_dac_clock(void) { return clock_mode; }
bool dual_dma_set_writers(unsigned v) { writers=v;return true; }
bool dual_dma_set_row_words(unsigned v) { rows=v;return true; }
bool dual_dma_set_sample_bits(unsigned v) {
    if(is("selectfail") && !selector_failed) { selector_failed=true;return false; }
    bits=v;return true;
}
bool dual_dma_set_schedule(unsigned v) { schedule=v;return true; }
bool dual_dma_set_bus_priority(unsigned v) { priority=v;return true; }
bool dual_dma_set_dac_clock(unsigned v) { clock_mode=v;return true; }
const char *dual_dma_last_error(void) { return "native injected output failure"; }
bool dual_dma_start_axis_fifo(const volatile uint32_t *i,const volatile uint32_t *q,unsigned di,unsigned dq,unsigned cpw) {
    assert(i==fifo && q==fifo+1 && di==20 && dq==21 && pointer_running);
    assert(writers==2 && rows==1 && bits==8 && !schedule && clock_mode);
    run_cpw=cpw;starts++;running=true;output_epoch=tick;return !is("outputfail");
}
dual_dma_faults_t dual_dma_faults(void) { return (dual_dma_faults_t){.running=running}; }
void dual_dma_stop(void) { assert(starts && !stops);running=false;stops++; }
bool dual_dma_capture_pins(uint32_t *data,uint32_t words) {
    assert(data==cap_buf && words==4096 && running);captures++;
    for(unsigned n=0;n<words;n++)data[n]=0x10004000u^n;
    return true;
}
uint32_t dual_dma_capture_count(void) { assert(stops);return 4096; }
uint32_t dual_dma_capture_errors(void) { assert(stops);return 0; }
bool iq4ptr_prepare_live8(PIO pio,const uint32_t *i,const uint32_t *q,unsigned log2,const uint32_t *table,
                         uint32_t pi,uint32_t pq,uint64_t published) {
    assert(pio==pio2 && i==in_buf && q==in_buf+4096 && log2==12 && table==ring_buf && !pi && pq==0xaaaaaaaau);
    assert(published==2774);prepares++;progress.published_words=published;
    progress.granted_words=published/512*512;return !is("pointerfail");
}
bool iq4ptr_start(void) { pointer_running=true;progress.running=true;return true; }
iq4ptr_progress_t iq4ptr_progress(void) {
    if(pointer_running && starts) {
        uint64_t wanted=(tick-output_epoch)/(run_cpw?run_cpw:2);
        if(wanted>=progress.granted_words) {
            wanted=progress.granted_words;progress.errors|=IQ4PTR_ERR_SOURCE_EMPTY;progress.null_trigger_mask=3;
        }
        progress.input_words[0]=progress.input_words[1]=wanted;
        progress.reusable_words=wanted>8?wanted-8:0;
    }
    return progress;
}
iq4ptr_progress_t iq4ptr_publish(uint64_t published) {
    iq4ptr_progress();
    assert(published>=progress.published_words && published-progress.reusable_words<=4096);
    progress.published_words=published;progress.granted_words=published/512*512;return progress;
}
const volatile uint32_t *iq4ptr_rx_fifo(unsigned axis) { assert(axis<2);return fifo+axis; }
unsigned iq4ptr_rx_dreq(unsigned axis) { assert(axis<2);return axis+20; }
const char *iq4ptr_last_error(void) { return "native injected pointer failure"; }
void iq4ptr_stop(void) { assert(prepares && !pointer_stops && !running);iq4ptr_progress();pointer_running=false;progress.running=false;pointer_stops++; }

int main(int argc,char **argv) {
    if(argc!=8)return 2;
    mode=argv[1];int code=atoi(argv[2]),cpw=atoi(argv[3]),options=atoi(argv[4]);
    const uint32_t guard=0xc39a75b1u;
    void *allocation=malloc(N_BLOCKS*BLOCK_WORDS*4u+131080u);assert(allocation);
    ring_storage=(uint32_t *)(((uintptr_t)allocation+131075u)&~(uintptr_t)131071u);
    ring_storage[-1]=ring_storage[N_BLOCKS*BLOCK_WORDS]=guard;
    test_input.before=test_input.after=guard;
    uint32_t seed=SEED;prbs_fill(in_buf,IN_WORDS,&seed);
    uint32_t original=crc32_update(0,in_buf,sizeof in_buf);
    dvbs2_bch_set_kernel(S2_BCH_T8_ARM);dvbs2_frontend_set_bank(1);
    bench_b8_lean8(code,100,cpw,atoi(argv[5]),atoi(argv[6]),options,atoi(argv[7]));
    assert(ring_storage[-1]==guard && ring_storage[N_BLOCKS*BLOCK_WORDS]==guard);
    assert(test_input.before==guard && test_input.after==guard);
    assert(!running && !pointer_running && !worker_active && !spi_active);
    assert(dvbs2_bch_get_kernel()==S2_BCH_T8_ARM && dvbs2_frontend_get_bank()==1);
    bool selected=writers==4 && rows==4 && bits==14 && schedule==3 && priority==7 && !clock_mode;
    printf("@meta {\"guards_ok\":true,\"selected\":%s,\"input_restored\":%s,\"joins\":%u,\"starts\":%u,\"stops\":%u,\"prepares\":%u,\"pointer_stops\":%u,\"captures\":%u,\"restores\":%u,\"overlapping\":%u,\"out_of_order\":%u}\n",
           selected?"true":"false",crc32_update(0,in_buf,sizeof in_buf)==original?"true":"false",joins,starts,stops,prepares,pointer_stops,captures,restores,overlapping,out_of_order);
    free(allocation);return 0;
}
'''


@functools.lru_cache(maxsize=64)
def ring_crc(code: int, frames: int) -> list[int]:
    """Reconstruct only the final physical ring, using scalar Python FEC and PL."""
    first = max(0, frames - 5)
    codes = np.concatenate([trial.geometric_codes(trial.frame_points(k, code))
                            for k in range(first, frames)], axis=1)
    first_symbol = first * trial.FRAME_SYMBOLS
    end_word = frames * trial.FRAME_SYMBOLS // 16
    rings = np.zeros((2, 4096), dtype="<u4")
    start_word = max((first_symbol + 15) // 16, end_word - 4096)
    for word in range(start_word, end_word):
        offset = word * 16 - first_symbol
        rings[:, word & 4095] = np.sum(codes[:, offset:offset + 16].astype(np.uint64) <<
                                             (2 * np.arange(16, dtype=np.uint64)), axis=1)
    return [zlib.crc32(axis.tobytes()) for axis in rings]


class B8Bench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="b8-command-native-")
        directory = Path(cls.temp.name)
        (directory / "hardware").mkdir()
        (directory / "pico").mkdir()
        headers = {
            "hardware/clocks.h": '#pragma once\n#include <stdint.h>\nenum {clk_sys};\nuint32_t clock_get_hz(unsigned);\n',
            "hardware/dma.h": '#pragma once\n#include <stdbool.h>\nenum {NUM_DMA_CHANNELS=16};\nbool dma_channel_is_claimed(unsigned);\n',
            "hardware/pio.h": '#pragma once\n#include <stdbool.h>\ntypedef struct {unsigned index;} pio_t;\ntypedef pio_t *PIO;\nstatic pio_t ports[]={{0},{1},{2}};\n#define pio0 (&ports[0])\n#define pio1 (&ports[1])\n#define pio2 (&ports[2])\nbool pio_sm_is_claimed(PIO,unsigned);\n',
            "hardware/sync.h": '#pragma once\n#define __dmb() ((void)0)\n#define __sev() ((void)0)\n#define __wfe() ((void)0)\n',
            "pico/stdlib.h": '#pragma once\n#include <stdint.h>\nuint32_t time_us_32(void);\nuint64_t time_us_64(void);\nvoid tight_loop_contents(void);\nvoid busy_wait_us_32(uint32_t);\n',
            "git_rev.h": '#define GIT_REV "native-b8-command"\n',
            "tx.h": TX_HEADER,
        }
        for path, text in headers.items():
            (directory / path).write_text(text)
        shutil.copyfile(SRC / "b8_lean8.c", directory / "b8_lean8.c")
        (directory / "harness.c").write_text(HARNESS.replace("C3_SOURCE", str(SRC / "c3_fec.c"))
                                             .replace("INGRESS_SOURCE", str(SRC / "b8_ingress.c")))
        cls.exe = directory / "bench"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        "-DIQ4HIST_EXPERIMENT=1", "-I" + str(directory), "-I" + str(SRC),
                        str(directory / "harness.c"),
                        *(str(SRC / file) for file in ("dvbs2.c", "dvbs2_8psk.c", "pvproto.c", "iqgen.c", "iq8.c", "iq4hist.c")),
                        "-o", str(cls.exe)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_case(self, mode="positive", code=8, cpw=2, options=27, invert=0, priority=13, starve=0):
        output = subprocess.check_output([str(self.exe), mode, str(code), str(cpw), str(options),
                                          str(invert), str(priority), str(starve)], text=True, timeout=30)
        lines = output.splitlines()
        result = [json.loads(line[1:]) for line in lines if line.startswith("@{")]
        meta = [json.loads(line[6:]) for line in lines if line.startswith("@meta ")]
        self.assertEqual(len(result), 1)
        self.assertEqual(len(meta), 1)
        self.assertTrue(meta[0]["guards_ok"] and meta[0]["selected"] and meta[0]["input_restored"])
        return result[0], meta[0], lines

    def test_real_command_options_rates_priorities_and_clock_control(self):
        for code in (5, 8, 6):
            for options in (0, 27):
                with self.subTest(code=code, options=options):
                    r, n, lines = self.run_case(code=code, options=options, priority=0 if options else 13,
                                                invert=bool(options))
                    self.assertEqual(r["errors"], 0, r)
                    self.assertGreater(r["published_frames"], 5)
                    self.assertEqual(r["published_groups"], r["published_frames"] * 22194 // 16)
                    self.assertEqual(r["code_crc"], ring_crc(code, r["published_frames"]))
                    self.assertEqual((n["joins"], n["starts"], n["stops"], n["pointer_stops"], n["captures"]), (1, 1, 1, 1, 1))
                    self.assertGreater(n["overlapping"], 0)
                    self.assertGreater(n["out_of_order"], 0)
                    self.assertLessEqual(r["max_service_gap_us"], 128)
                    words = [int(word, 16) for line in lines if line.startswith("@cap ") for word in line.split()[2:]]
                    self.assertEqual(len(words), 4096)
                    self.assertEqual(r["cap_crc"], zlib.crc32(np.asarray(words, dtype="<u4").tobytes()))

    def test_starvation_gap_and_slice_controls(self):
        r, n, _ = self.run_case(starve=1)
        self.assertTrue(r["pointer_errors"] & 64)
        self.assertEqual(r["published_frames"], 2)
        self.assertEqual(r["null_trigger_mask"], 3)
        self.assertLess(r["elapsed_us"], 100000)
        self.assertEqual(n["joins"], 1)
        for mode, flag in (("gap", 1 << 18), ("slice", 1 << 19)):
            r, n, _ = self.run_case(mode)
            self.assertTrue(r["errors"] & flag, r)
            self.assertEqual(n["joins"], 1)

    def test_setup_faults_and_admission_restore(self):
        for mode in ("selectfail", "pointerfail", "outputfail", "spiwait"):
            with self.subTest(mode=mode):
                r, n, _ = self.run_case(mode)
                self.assertNotEqual(r["errors"], 0)
                self.assertEqual(n["joins"], 0)
                self.assertEqual(n["starts"], n["stops"])
        for mode in ("busy", "smbusy", "clockbad", "tablesbad"):
            r, n, _ = self.run_case(mode)
            self.assertIn("error", r)
            self.assertEqual(n["restores"], 0)


if __name__ == "__main__":
    unittest.main()
