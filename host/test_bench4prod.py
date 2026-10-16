"""Execute B6's actual command body with native time/resource/DMA shims.

Real source generation, source packing, table building and CRCs are linked.
Synthetic DMA/time verifies orchestration and cleanup, never M33 timing,
arbitration, GPIO phase, starvation response latency or device throughput.
"""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
import zlib

import iq4prod_trial as trial
import fwsrc

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT/'firmware/src'

FIXTURE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define IQ4HIST_EXPERIMENT 1
#include "config.h"
#include "dual_dma.h"
#include "iqgen.h"
#include "iq4hist.h"
#include "iq4prod_source.h"
#define GIT_REV "native-shim"
#define SEED 0x1234abcdu
static uint32_t *ring_storage;
#define ring_buf (*(uint32_t (*)[N_BLOCKS * BLOCK_WORDS])ring_storage)
static _Alignas(16384) uint32_t in_buf[IN_WORDS];
static uint32_t cap_buf[CAP_WORDS_MAX];
enum { NUM_DMA_CHANNELS=16, NUM_PIO_STATE_MACHINES=4, clk_sys=0 };
typedef struct { unsigned index; } pio_t;
typedef pio_t *PIO;
static pio_t ports[]={{0},{1},{2}};
#define pio0 (&ports[0])
#define pio1 (&ports[1])
#define pio2 (&ports[2])
#include "iq4prod_dma.h"
static unsigned dma_claims, sm_claims[3], initial_dma, initial_sm[3];
static unsigned writers=4, rows=4, sample_bits=14, schedule=3, priority=7, dac_clock;
static unsigned starts, stops, prepares, producer_starts, producer_stops, captures, faults, tick, restores;
static unsigned producer_begin, run_cpw;
static bool running, producer_running, selector_failed, producer_live;
static uint32_t fault_bits, source_published;
static iq4prod_progress_t progress;
static const char *mode;
static bool is(const char *value) { return !strcmp(mode,value); }
static bool dma_channel_is_claimed(unsigned ch) { return dma_claims & (1u<<ch); }
static bool pio_sm_is_claimed(PIO pio,unsigned sm) { return sm_claims[pio->index] & (1u<<sm); }
static uint32_t clock_get_hz(int clock) {
    assert(clock==clk_sys);return is("clockbad")?150000000u:is("clock256")?256000000u:128000000u;
}
static uint32_t time_us_32(void) { tick+=is("pollgap")?257:64;return tick; }
static void tight_loop_contents(void) {}
static uint32_t cycles(void) { static uint32_t count;return count+=40; }
unsigned dual_dma_get_writers(void) { return writers; }
unsigned dual_dma_get_row_words(void) { return rows; }
unsigned dual_dma_get_sample_bits(void) { return sample_bits; }
unsigned dual_dma_get_schedule(void) { return schedule; }
unsigned dual_dma_get_bus_priority(void) { return priority; }
unsigned dual_dma_get_dac_clock(void) { return dac_clock; }
bool dual_dma_set_writers(unsigned v) { if(is("owned"))return false;writers=v;return true; }
bool dual_dma_set_row_words(unsigned v) { rows=v;return true; }
bool dual_dma_set_sample_bits(unsigned v) {
    if(is("selectfail")&&!selector_failed) { selector_failed=true;return false; }
    sample_bits=v;return true;
}
bool dual_dma_set_schedule(unsigned v) { schedule=v;return true; }
bool dual_dma_set_bus_priority(unsigned v) { if(is("restorefail")&&stops)return false;priority=v;return true; }
bool dual_dma_set_dac_clock(unsigned v) { dac_clock=v;return true; }
const char *dual_dma_last_error(void) { return "injected native failure"; }
int dual_dma_dac_clock_pio(void) { return 0; }
int dual_dma_dac_clock_sm(void) { return 3; }
static uint32_t fifo[4];
bool dual_dma_start_quad_fifo(const volatile uint32_t *const rx[4],const unsigned dreq[4],unsigned cpw) {
    assert(producer_running && cpw==run_cpw);
    assert(writers==4 && rows==1 && sample_bits==8 && schedule==0 && dac_clock);
    for(unsigned s=0;s<4;s++)assert(rx[s]==fifo+s && dreq[s]==s+20);
    starts++;running=true;dma_claims|=0xff0;sm_claims[2]|=15;sm_claims[0]|=8;
    return !is("startfail");
}
dual_dma_faults_t dual_dma_faults(void) {
    faults++;
    if(is("outputfault")&&faults>=3)fault_bits|=DUAL_DMA_ERR_TXSTALL_I;
    if(is("stopfault")&&stops)fault_bits|=DUAL_DMA_ERR_TXSTALL_Q;
    if(fault_bits)running=false;
    return (dual_dma_faults_t){.txstalls_i=!!(fault_bits&DUAL_DMA_ERR_TXSTALL_I),
        .txstalls_q=!!(fault_bits&DUAL_DMA_ERR_TXSTALL_Q),.errors=fault_bits,.running=running};
}
bool dual_dma_capture_pins(uint32_t *data,uint32_t count) {
    assert(data==cap_buf && count==4096 && running);captures++;
    if(is("capturefail"))return false;
    dma_claims|=1u<<12;sm_claims[1]|=2;
    for(unsigned j=0;j<count;j++)data[j]=0x10004000u^j;
    if(is("sourcemutate"))in_buf[0]^=4;
    return true;
}
void dual_dma_stop(void) {
    assert(starts && !stops);stops++;running=false;
    dma_claims=initial_dma|15;sm_claims[0]=initial_sm[0]|7;
    sm_claims[1]=initial_sm[1]|1;sm_claims[2]=initial_sm[2];
}
uint32_t dual_dma_capture_count(void) { assert(stops);return is("shortcapture")?4095:4096; }
uint32_t dual_dma_capture_errors(void) { assert(stops);return is("caperror")?DUAL_DMA_ERR_CAPTURE_DMA:0; }
bool iq4prod_dma_prepare(const uint32_t *i,const uint32_t *q,const uint32_t *table,
                        uint32_t previous_i,uint32_t previous_q,bool live,uint64_t published) {
    assert(i==in_buf && q==in_buf+4096 && table==ring_buf && published==4096);
    assert(previous_i==(live?0:in_buf[4095]) && previous_q==(live?0:in_buf[8191]));
    prepares++;producer_live=live;source_published=(uint32_t)published;
    dma_claims|=15;sm_claims[0]|=7;sm_claims[1]|=1;
    if(is("preparefail")) { progress.errors=IQ4PROD_ERR_CONFIG;return false; }
    return true;
}
bool iq4prod_dma_start(void) {
    producer_starts++;producer_running=true;producer_begin=tick;
    if(is("producerstartfail")) { progress.errors=IQ4PROD_ERR_STARTUP;return false; }
    return true;
}
iq4prod_progress_t iq4prod_dma_progress(void) {
    if(producer_running) {
        uint64_t issued=(uint64_t)(tick-producer_begin)*clock_get_hz(clk_sys)/(8*run_cpw*16*1000000ull);
        if(producer_live) {
            issued=issued/512*512;
            if(issued>=source_published) {
                issued=source_published;progress.errors|=IQ4PROD_ERR_SOURCE_EMPTY;progress.txstall_mask|=1;
            }
        }
        for(unsigned s=0;s<4;s++) {
            progress.input_words[s]=issued;
            progress.granted_input_words[s]=issued+512<source_published?issued+512:source_published;
        }
        progress.reusable_words=issued;progress.published_words=source_published;
        if(is("pointerfault")&&tick-producer_begin>256)progress.errors|=IQ4PROD_ERR_PIO;
        progress.rxstall_mask=23;
        if(producer_live) { progress.worker_polls++;progress.worker_max_gap_cycles=64;progress.worker_service_cycles+=40; }
    }
    return progress;
}
iq4prod_progress_t iq4prod_dma_publish(uint64_t published) {
    iq4prod_dma_progress();
    assert(producer_live && published>=source_published && published-progress.reusable_words<=4096);
    source_published=(uint32_t)published;progress.published_words=published;
    if(is("publishfail"))progress.errors|=IQ4PROD_ERR_OWNERSHIP;
    return progress;
}
const volatile uint32_t *iq4prod_dma_rx_fifo(unsigned stream) { assert(stream<4);return fifo+stream; }
unsigned iq4prod_dma_rx_dreq(unsigned stream) { assert(stream<4);return stream+20; }
int iq4prod_dma_pio_index(unsigned stream) { assert(stream<4);return stream==3; }
int iq4prod_dma_sm(unsigned stream) { assert(stream<4);return stream==3?0:(int)stream; }
const char *iq4prod_dma_last_error(void) { return "injected native producer failure"; }
void iq4prod_dma_stop(void) {
    assert(prepares && !producer_stops);producer_stops++;
    assert(!running);iq4prod_dma_progress();producer_running=false;
    dma_claims=initial_dma;memcpy(sm_claims,initial_sm,sizeof sm_claims);
    if(is("leak"))dma_claims|=1;
}
static bool test_build8(uint32_t *table,unsigned capacity) {
    if(is("tablefail"))return false;
    bool ok=iq4hist8_build(table,capacity);if(is("tablecrc"))table[0]^=1;return ok;
}
static void restore_prbs(uint32_t *data,size_t count,uint32_t *seed) {
    assert(data==in_buf && count==IN_WORDS && *seed==SEED);restores++;prbs_fill(data,count,seed);
}
#define iq4hist8_build test_build8
#define prbs_fill restore_prbs
'''

MAIN = r'''
#undef prbs_fill
int main(int argc,char **argv) {
    if(argc!=8)return 2;
    mode=argv[1];int cpw=atoi(argv[2]),invert=atoi(argv[3]),prio=atoi(argv[4]);
    int ms=atoi(argv[5]),live=atoi(argv[6]),starve_ms=atoi(argv[7]);run_cpw=(unsigned)cpw;
    void *allocation=malloc(sizeof ring_buf+131071u);assert(allocation);
    ring_storage=(uint32_t *)(((uintptr_t)allocation+131071u)&~(uintptr_t)131071u);
    uint32_t seed=SEED;prbs_fill(in_buf,IN_WORDS,&seed);
    uint32_t original_crc=crc32_update(0,in_buf,sizeof in_buf);
    dma_claims=1u<<15;sm_claims[1]=4;
    if(is("claimsbusy"))dma_claims=15;
    if(is("sm2busy"))sm_claims[2]=1;
    if(is("sm0busy"))sm_claims[0]=1;
    if(is("sm1busy"))sm_claims[1]=7;
    initial_dma=dma_claims;memcpy(initial_sm,sm_claims,sizeof sm_claims);
    bench4prod8(cpw,ms,invert,prio,live!=0,starve_ms);
    bool unchanged=original_crc==crc32_update(0,in_buf,sizeof in_buf);
    bool selected=writers==4&&rows==4&&sample_bits==14&&schedule==3&&priority==7&&!dac_clock;
    bool released=dma_claims==initial_dma&&!memcmp(sm_claims,initial_sm,sizeof sm_claims);
    assert(!running && !producer_running);
    if(restores)assert(unchanged);
    printf("@native {\"starts\":%u,\"stops\":%u,\"prepares\":%u,\"producer_starts\":%u,"
           "\"producer_stops\":%u,\"captures\":%u,\"restores\":%u,\"unchanged\":%s,"
           "\"selected\":%s,\"released\":%s}\n",starts,stops,prepares,producer_starts,producer_stops,
           captures,restores,unchanged?"true":"false",selected?"true":"false",released?"true":"false");
    free(allocation);return 0;
}
'''


class Bench4Prod(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='bench4prod-native-')
        directory = Path(cls.temp.name)
        (directory/'hardware').mkdir()
        (directory/'hardware/pio.h').write_text('/* PIO type supplied by the fixture. */\n')
        source = (SRC / 'bench/bench4prod.c').read_text()
        native = directory/'bench.c'
        native.write_text(FIXTURE+source[source.index('enum {'):]+MAIN)
        cls.executable = directory/'bench'
        subprocess.run(['cc', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-I'+str(directory), *fwsrc.INCLUDES, str(native),
                        *(str(fwsrc.path(name)) for name in ('iqgen.c', 'iq8.c', 'iq4hist.c', 'iq4prod_source.c')),
                        '-o', str(cls.executable)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_case(self, mode='positive', cpw=1, invert=0, priority=13, ms=100, live=False, starve_ms=0):
        output = subprocess.check_output([str(self.executable), mode, str(cpw), str(invert),
                                          str(priority), str(ms), str(int(live)), str(starve_ms)], text=True)
        lines = output.splitlines()
        result = [json.loads(line[1:]) for line in lines if line.startswith('@{')]
        native = [json.loads(line[8:]) for line in lines if line.startswith('@native ')]
        self.assertEqual(len(result), 1)
        self.assertEqual(len(native), 1)
        return result[0], native[0], lines

    def test_cyclic_live_clock_ratios_priorities_and_inversion(self):
        source, _ = trial.cyclic_source(8)
        for mode, cpw in (('positive', 1), ('positive', 2), ('clock256', 2)):
            for live in (False, True):
                for invert in (0, 1):
                    for priority in (0, 13):
                        r, n, lines = self.run_case(mode, cpw, invert, priority, live=live)
                        self.assertEqual(r['errors'], 0)
                        self.assertEqual(r['cmd'], 'bench4prod8live' if live else 'bench4prod8')
                        self.assertEqual(r['sym_rate'], r['clk_hz']/(8*cpw))
                        self.assertEqual((r['cpw'], r['invert'], r['bus_priority']), (cpw, invert, priority))
                        self.assertEqual(r['producer_pios'], [0, 0, 0, 1])
                        self.assertEqual(r['producer_sms'], [0, 1, 2, 0])
                        self.assertEqual(r['table_crc'], trial.table_crc(8))
                        if live:
                            self.assertGreater(r['runtime_gen_words'], 0)
                            self.assertEqual(r['published_words'], r['runtime_gen_words']+4096)
                            self.assertEqual(r['input_crc'], trial.live_ring_crc(4096))
                            self.assertEqual(r['input_after_crc'], trial.live_ring_crc(r['published_words']))
                            self.assertEqual(r['previous_words'], [0, 0])
                            self.assertEqual(trial.live_faults(r, r['elapsed_us'], r['clk_hz'], cpw, 100, 0), [])
                        else:
                            self.assertEqual(r['input_crc'], zlib.crc32(source.tobytes()))
                            self.assertEqual(r['input_after_crc'], r['input_crc'])
                            self.assertEqual(r['previous_words'], [int(source[4095]), int(source[-1])])
                        self.assertTrue(r['restored'] and r['selectors_restored'] and r['resources_released'])
                        self.assertEqual((n['starts'], n['stops'], n['prepares'], n['producer_starts'],
                                          n['producer_stops'], n['captures'], n['restores']), (1, 1, 1, 1, 1, 1, 1))
                        self.assertTrue(n['unchanged'] and n['selected'] and n['released'])
                        words = [int(word, 16) for line in lines if line.startswith('@cap ')
                                 for word in line.split()[2:]]
                        self.assertEqual(len(words), 4096)
                        self.assertEqual(r['cap_crc'], zlib.crc32(b''.join(w.to_bytes(4, 'little') for w in words)))
                        self.assertIn('@capend', lines)

    def test_runtime_starvation_fenced_stop_and_publication_failure(self):
        r, n, _ = self.run_case(live=True, ms=100, starve_ms=20)
        self.assertTrue(r['source_empty'])
        self.assertEqual(r['reason'], 'SOURCE_EMPTY')
        self.assertTrue(r['pointer_errors'] & trial.SOURCE_EMPTY)
        self.assertTrue(r['errors'] & (1 << 21))
        self.assertLess(r['elapsed_us'], 30000)
        self.assertEqual(r['published_words'], r['completed_words'][0])
        self.assertTrue(n['unchanged'] and n['selected'] and n['released'])
        self.assertEqual(n['captures'], 1)
        r, n, _ = self.run_case('publishfail', live=True)
        self.assertTrue(r['pointer_errors'] & (1 << 7))
        self.assertEqual(r['reason'], 'publication fault')
        self.assertEqual((n['stops'], n['producer_stops'], n['restores']), (1, 1, 1))

    def test_faults_stop_writers_before_producers_and_restore_source(self):
        cases = [('capturefail', 1<<17), ('shortcapture', 1<<17), ('outputfault', 1),
                 ('stopfault', 2), ('caperror', 8), ('startfail', 1<<19),
                 ('pointerfault', 1<<16), ('sourcemutate', 1<<19), ('pollgap', 1<<20)]
        for mode, mask in cases:
            with self.subTest(mode=mode):
                r, n, _ = self.run_case(mode)
                self.assertTrue(r['errors'] & mask)
                self.assertEqual((n['starts'], n['stops'], n['producer_stops'], n['restores']), (1, 1, 1, 1))
                self.assertTrue(n['unchanged'] and n['selected'] and n['released'])

    def test_setup_failures_and_cleanup_retention(self):
        for mode, mask in (('selectfail', 1<<19), ('tablefail', 1<<18), ('tablecrc', 1<<18),
                           ('preparefail', 1<<16), ('producerstartfail', 1<<16)):
            r, n, _ = self.run_case(mode)
            self.assertTrue(r['errors'] & mask)
            self.assertEqual((n['starts'], n['stops'], n['captures'], n['restores']), (0, 0, 0, 1))
            self.assertTrue(n['unchanged'] and n['selected'] and n['released'])
        for mode, key in (('leak', 'resources_released'), ('restorefail', 'selectors_restored')):
            r, n, _ = self.run_case(mode)
            self.assertTrue(r['errors'] & (1<<19))
            self.assertFalse(r[key])
            self.assertFalse(r['restored'])
            self.assertEqual(n['restores'], 0)

    def test_admission_rejects_before_borrowing_source(self):
        cases = [dict(mode=mode) for mode in ('claimsbusy', 'sm2busy', 'sm0busy', 'sm1busy', 'clockbad', 'owned')]
        cases += [dict(cpw=0), dict(cpw=3), dict(ms=99), dict(ms=60001), dict(invert=2),
                  dict(priority=-1), dict(priority=16), dict(starve_ms=20),
                  dict(live=True, starve_ms=9), dict(live=True, starve_ms=100), dict(mode='clock256')]
        for options in cases:
            with self.subTest(options=options):
                r, n, lines = self.run_case(**options)
                self.assertIn('error', r)
                self.assertEqual((n['starts'], n['stops'], n['prepares'], n['restores']), (0, 0, 0, 0))
                self.assertTrue(n['unchanged'] and n['selected'] and n['released'])
                self.assertFalse(any(line.startswith('@cap ') for line in lines))


if __name__ == '__main__':
    unittest.main()
