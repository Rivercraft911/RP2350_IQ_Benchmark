"""Execute the B9 command with real FEC and finite-input hardware shims.

Synthetic time checks publication, ring credit, failure drain and resource
restoration. It does not model RP2350 arbitration, IRQ latency or throughput.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import c3_fec_trial as reference
import fwsrc

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware/src"

TX_HEADER = r'''
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "pvproto.h"
#include "iqgen.h"
enum { IN_WORDS=8192, N_BLOCKS=8, BLOCK_WORDS=4096, CAP_WORDS_MAX=14336 };
#define SEED 0x1234ABCDu
typedef struct { uint32_t before; uint32_t words[N_BLOCKS*BLOCK_WORDS]; uint32_t after; } ring_storage_t;
typedef struct { uint32_t before; _Alignas(16384) uint32_t words[IN_WORDS]; uint32_t after; } input_storage_t;
typedef struct { uint32_t before; uint32_t words[CAP_WORDS_MAX]; uint32_t after; } capture_storage_t;
extern ring_storage_t *test_ring_ptr;
#define test_ring (*test_ring_ptr)
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
#include <stdlib.h>
#include <string.h>
#include "tx.h"
#include "hardware/pio.h"
#include "hardware/dma.h"
#include "dual_dma.h"
#include "iq4prod_dma.h"

static unsigned test_core, test_hz=128000000u, pump_active, worker_active, worker_joins;
static unsigned step_us, run_cpw, run_slow;
static uint64_t test_now, next_worker, worker_ticks, output_begin;
static const char *mode;
static bool is(const char *value) { return !strcmp(mode,value); }

// Preserve all actual GCS, BCH, LDPC and packed mapping logic.
#define c3_job_step real_c3_job_step
#include "C3_SOURCE"
#undef c3_job_step
static uint64_t reusable, published;
static bool output_running, producer_running, producer_prepared;
static unsigned map_steps, credit_wait_checks, blocked_map_attempts;
bool c3_job_step(c3_job_t *job, const uint8_t *pattern) {
    if (output_running && job->stage==C3_MAP) {
        const uint64_t end=((uint64_t)(job->index+1u)*C3_FRAME_SYMBOLS+15u)/16u;
        assert(end-reusable<=C3_RING_GROUPS);
        ++map_steps;
    }
    const bool ok=real_c3_job_step(job,pattern);
    const unsigned duration=1;
    if(test_core) { worker_ticks+=(uint64_t)duration*(test_hz/1000000u); step_us+=duration; }
    else test_now+=duration;
    return ok;
}

#include "bench_b9.c"

ring_storage_t *test_ring_ptr;
input_storage_t test_input = {0};
capture_storage_t test_capture = {0};
pio_t mock_pios[]={{0},{1},{2}};
static unsigned dma_claims, sm_claims[3], initial_dma, initial_sm[3];
static unsigned writers=2, rows=2, sample_bits=14, schedule=3, priority=7, dac_clock;
static unsigned starts, stops, prepares, producer_starts, producer_stops, captures;
static uint32_t output_errors, fifo[4];
static iq4prod_progress_t progress;

static void pump(void) {
    if(!worker_active || pump_active) return;
    pump_active=1;
    while(next_worker<=test_now && !live.stop) {
        if(live.core[1].status!=RUN) { next_worker=test_now+1; break; }
        if(live.core[1].job.stage==C3_MAP && !map_credit(&live.core[1].job))++blocked_map_attempts;
        test_core=1;step_us=0;work(1);test_core=0;
        next_worker+=step_us?step_us:1u;
    }
    pump_active=0;
}
uint32_t clock_get_hz(unsigned clock) { assert(clock==clk_sys); return is("badclock")?150000000u:test_hz; }
uint64_t time_us_64(void) { if(!pump_active) { ++test_now;pump(); } return test_now; }
uint32_t time_us_32(void) { return (uint32_t)time_us_64(); }
uint32_t cycles(void) {
    if(!test_core && !pump_active) { ++test_now;pump(); }
    return (uint32_t)(test_core?worker_ticks:test_now*(test_hz/1000000u));
}
void enable_cyccnt(void) {}
void busy_wait_us_32(uint32_t duration) {
    if(test_core) { worker_ticks+=(uint64_t)duration*(test_hz/1000000u);step_us+=duration; }
    else test_now+=duration;
}
void tight_loop_contents(void) { ++test_now;pump(); }
void tx_worker_start(void (*fn)(uint32_t),uint32_t arg) {
    assert(fn==core1 && arg==0 && !worker_active);worker_active=1;next_worker=test_now;
}
void tx_worker_wait(void) { assert(worker_active && live.stop);worker_active=0;++worker_joins; }
bool dma_channel_is_claimed(unsigned channel) { return !!(dma_claims&(1u<<channel)); }
bool pio_sm_is_claimed(PIO pio,unsigned sm) { return !!(sm_claims[pio->index]&(1u<<sm)); }
unsigned dual_dma_get_writers(void) { return writers; }
unsigned dual_dma_get_row_words(void) { return rows; }
unsigned dual_dma_get_sample_bits(void) { return sample_bits; }
unsigned dual_dma_get_schedule(void) { return schedule; }
unsigned dual_dma_get_bus_priority(void) { return priority; }
unsigned dual_dma_get_dac_clock(void) { return dac_clock; }
bool dual_dma_set_writers(unsigned value) { writers=value;return true; }
bool dual_dma_set_row_words(unsigned value) { rows=value;return true; }
bool dual_dma_set_sample_bits(unsigned value) { sample_bits=value;return true; }
bool dual_dma_set_schedule(unsigned value) { schedule=value;return true; }
bool dual_dma_set_bus_priority(unsigned value) { priority=value;return true; }
bool dual_dma_set_dac_clock(unsigned value) { dac_clock=value;return true; }
const char *dual_dma_last_error(void) { return "injected native output failure"; }
int dual_dma_dac_clock_pio(void) { return 0; }
int dual_dma_dac_clock_sm(void) { return 3; }
bool dual_dma_start_quad_fifo(const volatile uint32_t *const rx[4],const unsigned dreq[4],unsigned cpw) {
    assert(producer_running && cpw==run_cpw && writers==4 && rows==1 && sample_bits==8 && !schedule && dac_clock);
    for(unsigned s=0;s<4;s++)assert(rx[s]==fifo+s && dreq[s]==s+20);
    ++starts;output_running=true;output_begin=test_now;
    dma_claims|=0xff0;sm_claims[2]|=15;sm_claims[0]|=8;
    return !is("startfail");
}
dual_dma_faults_t dual_dma_faults(void) {
    if(is("outputfault") && output_running && test_now-output_begin>1000)output_errors|=DUAL_DMA_ERR_OUTPUT_DMA;
    return (dual_dma_faults_t){.errors=output_errors,.running=output_running&&!output_errors};
}
bool dual_dma_capture_pins(uint32_t *data,uint32_t count) {
    assert(data>=cap_buf+256 && data+count<=cap_buf+CAP_WORDS_MAX && count==4096 && output_running);
    ++captures;
    if(is("capturefail"))return false;
    dma_claims|=1u<<12;sm_claims[1]|=2;
    for(unsigned j=0;j<count;j++)data[j]=0x10004000u^j;
    return true;
}
bool dual_dma_capture_done(void) { return captures!=0; }
void dual_dma_stop(void) {
    ++stops;output_running=false;
    dma_claims=initial_dma|15;sm_claims[0]=initial_sm[0]|7;sm_claims[1]=initial_sm[1]|1;sm_claims[2]=initial_sm[2];
}
uint32_t dual_dma_capture_count(void) { return captures&&!is("capturefail")?4096:0; }
uint32_t dual_dma_capture_errors(void) { return 0; }
bool dual_dma_capture_fifo_only(void) { return false; }

// Input reads can advance only through the paired published prefix. Completion
// is conservative at 512-word seams; independent readers retire at different times.
static bool prepare_input(const uint32_t *i,const uint32_t *q,const uint32_t *table,
                          uint32_t previous_i,uint32_t previous_q,uint64_t words) {
    assert(i==in_buf && q==in_buf+4096 && table==ring_buf && !previous_i && !previous_q);
    assert(words==2u*C3_FRAME_SYMBOLS/16u);
    ++prepares;producer_prepared=true;published=words;
    dma_claims|=15;sm_claims[0]|=7;sm_claims[1]|=1;
    return !is("preparefail");
}
bool iq4prod_dma_prepare(const uint32_t *i,const uint32_t *q,const uint32_t *table,
                        uint32_t previous_i,uint32_t previous_q,bool finite,uint64_t words) {
    assert(finite);return prepare_input(i,q,table,previous_i,previous_q,words);
}
bool iq4prod_dma_prepare_irq(const uint32_t *i,const uint32_t *q,const uint32_t *table,
                           uint32_t previous_i,uint32_t previous_q,uint64_t words) {
    return prepare_input(i,q,table,previous_i,previous_q,words);
}
bool iq4prod_dma_start(void) { ++producer_starts;producer_running=true;return !is("producerstartfail"); }
iq4prod_progress_t iq4prod_dma_progress(void) {
    if(producer_running && output_running) {
        const uint64_t desired=(test_now-output_begin)/run_cpw;
        uint64_t low=UINT64_MAX;
        for(unsigned s=0;s<4;s++) {
            const uint64_t delay=7u*s;
            uint64_t issued=desired>delay?desired-delay:0;
            if(issued>=published) {
                issued=published;progress.errors|=IQ4PROD_ERR_SOURCE_EMPTY;progress.txstall_mask|=1u<<s;
            }
            const uint64_t done=issued/512u*512u;
            progress.input_words[s]=done;
            progress.granted_input_words[s]=issued+512u<published?issued+512u:published;
            assert(progress.granted_input_words[s]<=published);
            if(done<low)low=done;
        }
        reusable=low;progress.reusable_words=low;progress.published_words=published;
        ++credit_wait_checks;
    }
    progress.running=producer_running&&!progress.errors;return progress;
}
iq4prod_irq_poll_t iq4prod_dma_poll_irq(void) {
    const iq4prod_progress_t p=iq4prod_dma_progress();
    return (iq4prod_irq_poll_t){.reusable_words=(uint32_t)p.reusable_words,
        .published_words=(uint32_t)p.published_words,.errors=p.errors};
}
iq4prod_irq_poll_t iq4prod_dma_poll_light(void) { return iq4prod_dma_poll_irq(); } /* model: IRQ cursors advance either way */
iq4prod_progress_t iq4prod_dma_publish(uint64_t words) {
    iq4prod_dma_progress();
    assert(words>=published && words-reusable<=4096);
    published=words;progress.published_words=words;
    return progress;
}
const volatile uint32_t *iq4prod_dma_rx_fifo(unsigned s) { assert(s<4);return fifo+s; }
unsigned iq4prod_dma_rx_dreq(unsigned s) { assert(s<4);return s+20; }
int iq4prod_dma_pio_index(unsigned s) { assert(s<4);return s==3; }
int iq4prod_dma_sm(unsigned s) { assert(s<4);return s==3?0:(int)s; }
const char *iq4prod_dma_last_error(void) { return "injected native producer failure"; }
void iq4prod_dma_stop(void) {
    assert(!output_running);++producer_stops;producer_running=false;
    dma_claims=initial_dma;memcpy(sm_claims,initial_sm,sizeof sm_claims);
}

int main(int argc,char **argv) {
    if(argc!=7)return 2;
    mode=argv[1];run_cpw=(unsigned)atoi(argv[2]);run_slow=(unsigned)atoi(argv[3]);
    const int options=atoi(argv[4]),invert=atoi(argv[5]),prio=atoi(argv[6]);
    void *allocation=malloc(sizeof(ring_storage_t)+65535u);assert(allocation);
    uintptr_t words=((uintptr_t)allocation+4u+65535u)&~(uintptr_t)65535u;
    test_ring_ptr=(ring_storage_t *)(words-4u);
    const uint32_t guard=0xC39A75B1u;
    test_ring.before=test_ring.after=test_input.before=test_input.after=test_capture.before=test_capture.after=guard;
    memset(in_buf,0xA5,sizeof in_buf);memset(cap_buf,0x59,sizeof cap_buf);
    if(is("claimsbusy"))dma_claims=15;
    if(is("sm0busy"))sm_claims[0]=1;
    if(is("sm1busy"))sm_claims[1]=7;
    if(is("sm2busy"))sm_claims[2]=1;
    initial_dma=dma_claims;memcpy(initial_sm,sm_claims,sizeof sm_claims);
    pv_init(16);assert(dvbs2_bch_set_kernel(S2_BCH_T8_ARM));assert(dvbs2_frontend_set_bank(1));
    bench_b9((int)run_cpw,100,invert,prio,(int)run_slow,options);
    assert(test_ring.before==guard && test_ring.after==guard && test_input.before==guard && test_input.after==guard);
    assert(test_capture.before==guard && test_capture.after==guard);
    assert(dvbs2_bch_get_kernel()==S2_BCH_T8_ARM && dvbs2_frontend_get_bank()==1);
    assert(!worker_active && !output_running && !producer_running);
    assert(dma_claims==initial_dma && !memcmp(sm_claims,initial_sm,sizeof sm_claims));
    assert(writers==2 && rows==2 && sample_bits==14 && schedule==3 && priority==7 && !dac_clock);
    printf("@meta {\"guards_ok\":true,\"selectors_restored\":true,\"released\":true,\"worker_joins\":%u,"
           "\"starts\":%u,\"stops\":%u,\"map_steps\":%u,\"credit_checks\":%u,\"blocked_map_attempts\":%u,\"captures\":%u,\"prepared\":%s}\n",
           worker_joins,starts,stops,map_steps,credit_wait_checks,blocked_map_attempts,captures,producer_prepared?"true":"false");
    free(allocation);
    return 0;
}
'''


class B9Bench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="b9-command-native-")
        directory = Path(cls.temporary.name)
        (directory / "hardware").mkdir()
        (directory / "pico").mkdir()
        (directory / "hardware/clocks.h").write_text(
            '#pragma once\n#include <stdint.h>\nenum {clk_sys};\nuint32_t clock_get_hz(unsigned clock);\n')
        (directory / "hardware/sync.h").write_text(
            '#pragma once\n#define __dmb() ((void)0)\n#define __sev() ((void)0)\n#define __wfe() ((void)0)\n')
        (directory / "hardware/pio.h").write_text(
            '#pragma once\n#include <stdbool.h>\ntypedef struct { unsigned index; } pio_t;\n'
            'typedef pio_t *PIO;\nextern pio_t mock_pios[3];\n'
            '#define pio0 (&mock_pios[0])\n#define pio1 (&mock_pios[1])\n#define pio2 (&mock_pios[2])\n'
            'enum { NUM_PIO_STATE_MACHINES=4 };\nbool pio_sm_is_claimed(PIO pio,unsigned sm);\n')
        (directory / "hardware/dma.h").write_text(
            '#pragma once\n#include <stdbool.h>\nenum { NUM_DMA_CHANNELS=16 };\n'
            'bool dma_channel_is_claimed(unsigned channel);\n')
        (directory / "pico/stdlib.h").write_text(
            '#pragma once\n#include <stdint.h>\nuint64_t time_us_64(void);\nuint32_t time_us_32(void);\n'
            'void busy_wait_us_32(uint32_t us);\nvoid tight_loop_contents(void);\n')
        (directory / "git_rev.h").write_text('#define GIT_REV "native-b9-command"\n')
        (directory / "tx.h").write_text(TX_HEADER)
        shutil.copyfile(SRC / "bench/bench_b9.c", directory / "bench_b9.c")
        (directory / "harness.c").write_text(HARNESS.replace("C3_SOURCE", str(SRC / "dvbs2/c3_fec.c")))
        cls.executable = directory / "bench"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-DIQ4HIST_EXPERIMENT=1",
                        "-I" + str(directory), *fwsrc.INCLUDES, str(directory / "harness.c"),
                        *(str(fwsrc.path(name)) for name in ("dvbs2.c", "dvbs2_8psk.c", "pvproto.c", "iq4hist.c", "iq8.c", "iqgen.c")),
                        "-o", str(cls.executable)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_command(self, mode="positive", cpw=1, slow=0, options=251, invert=0, priority=13):
        process = subprocess.run([str(self.executable), mode, str(cpw), str(slow), str(options), str(invert), str(priority)],
                                 capture_output=True, text=True, check=True, timeout=30)
        records = [json.loads(line[1:]) for line in process.stdout.splitlines() if line.startswith("@{")]
        metadata = [json.loads(line[6:]) for line in process.stdout.splitlines() if line.startswith("@meta ")]
        self.assertEqual(len(records), 1, process.stdout)
        self.assertEqual(len(metadata), 1, process.stdout)
        self.assertTrue(metadata[0]["guards_ok"] and metadata[0]["released"] and metadata[0]["selectors_restored"])
        return records[0], metadata[0]

    def test_complete_pipeline_command_and_credit(self):
        result, meta = self.run_command()
        self.assertEqual(result["errors"], 0, result)
        self.assertEqual(result["published_groups"], result["published_frames"] * 22194 // 16)
        expected = reference.ring_reference(5, result["published_frames"], 4096)
        self.assertEqual(result["code_crc"], expected["code_crc"])
        self.assertEqual(result["pending_fill"], expected["pending_fill"])
        self.assertGreater(meta["map_steps"], 10)
        self.assertGreater(meta["credit_checks"], 10)
        self.assertGreater(meta["blocked_map_attempts"], 0)
        self.assertEqual((meta["worker_joins"], meta["starts"], meta["stops"], meta["captures"]), (1, 1, 1, 1))

    def test_same_image_option_clock_priority_and_inversion_controls(self):
        for options, cpw, invert, priority in ((0, 2, 0, 0), (251, 2, 0, 13), (251, 1, 1, 0)):
            with self.subTest(options=options, cpw=cpw, invert=invert, priority=priority):
                result, meta = self.run_command(options=options, cpw=cpw, invert=invert, priority=priority)
                self.assertEqual(result["errors"], 0, result)
                self.assertEqual((result["options"], result["cpw"], result["invert"], result["bus_priority"]),
                                 (options, cpw, invert, priority))
                self.assertEqual(result["code_crc"], reference.ring_reference(5, result["published_frames"], 4096)["code_crc"])
                self.assertGreater(meta["map_steps"], 0)

    def test_slow_worker_underruns_then_drains_exact_final_frames(self):
        result, meta = self.run_command(slow=5000)
        self.assertTrue(result["source_empty"], result)
        self.assertNotEqual(result["pointer_errors"], 0)
        self.assertEqual(result["stream_frames"], 2)
        self.assertEqual(result["published_frames"], 4)
        self.assertEqual(result["code_crc"], reference.ring_reference(5, 4, 4096)["code_crc"])
        self.assertEqual(meta["worker_joins"], 1)
        self.assertEqual(meta["captures"], 1)

    def test_setup_capture_and_output_faults_release_everything(self):
        for mode in ("preparefail", "producerstartfail", "startfail", "capturefail", "outputfault"):
            with self.subTest(mode=mode):
                result, meta = self.run_command(mode=mode)
                self.assertNotEqual(result["errors"], 0, result)
                self.assertEqual(meta["worker_joins"], 1)
                self.assertEqual(result["code_crc"], reference.ring_reference(5, result["published_frames"], 4096)["code_crc"])

    def test_invalid_arguments_and_claims_do_not_start_workers(self):
        cases = [dict(cpw=4), dict(options=-1), dict(options=31), dict(options=1275),
                 dict(invert=2), dict(priority=16), dict(slow=10001)]
        cases += [dict(mode=mode) for mode in ("badclock", "claimsbusy", "sm0busy", "sm1busy", "sm2busy")]
        for case in cases:
            with self.subTest(case=case):
                result, meta = self.run_command(**case)
                self.assertIn("error", result)
                self.assertEqual(meta["worker_joins"], 0)
                self.assertEqual(meta["starts"], 0)
                self.assertFalse(meta["prepared"])


if __name__ == "__main__":
    unittest.main()
