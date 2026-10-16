"""Execute the actual N5 command against native resource/output/time shims.

Real portable source packing, table generation, pointer rings and CRCs are linked.
Synthetic time and DMA outcomes test orchestration and cleanup; they provide no
M33 timing, arbitration, GPIO phase, or device throughput evidence.
"""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'firmware/src'

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
#include "iq4quad.h"
#include "iq5hist.h"
#define GIT_REV "native-shim"
#define SEED 0x1234abcdu
static uint32_t *ring_storage;
#define ring_buf (*(uint32_t (*)[N_BLOCKS * BLOCK_WORDS])ring_storage)
static _Alignas(8192) uint32_t in_buf[IN_WORDS];
static uint32_t cap_buf[CAP_WORDS_MAX];
enum { NUM_DMA_CHANNELS=16, NUM_PIO_STATE_MACHINES=4, clk_sys=0 };
typedef struct { unsigned index; } pio_t;
typedef pio_t *PIO;
static pio_t ports[]={{0},{1},{2}};
#define pio0 (&ports[0])
#define pio1 (&ports[1])
#define pio2 (&ports[2])
static unsigned dma_claims, sm_claims[3], initial_dma, initial_sm[3];
static unsigned writers=4, rows=4, sample_bits=14, schedule=3, priority=7, dac_clock;
static unsigned starts, stops, captures, faults, tick, selections, restores;
static bool running, selector_failed;
static uint32_t fault_bits;
static const char *mode;
static bool is(const char *value) { return !strcmp(mode,value); }
static bool dma_channel_is_claimed(unsigned ch) { return dma_claims & (1u<<ch); }
static bool pio_sm_is_claimed(PIO pio,unsigned sm) { return sm_claims[pio->index] & (1u<<sm); }
static uint32_t clock_get_hz(int clock) { assert(clock==clk_sys);return is("clock")?256000000u:128000000u; }
static uint32_t time_us_32(void) { tick+=is("pollgap")?257:64;return tick; }
static void tight_loop_contents(void) {}
static uint32_t cycles(void) { static uint32_t count;return count+=40; }
unsigned dual_dma_get_writers(void) { return writers; }
unsigned dual_dma_get_row_words(void) { return rows; }
unsigned dual_dma_get_sample_bits(void) { return sample_bits; }
unsigned dual_dma_get_schedule(void) { return schedule; }
unsigned dual_dma_get_bus_priority(void) { return priority; }
unsigned dual_dma_get_dac_clock(void) { return dac_clock; }
bool dual_dma_set_writers(unsigned v) { selections++;if(is("owned"))return false;writers=v;return true; }
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
int dual_dma_dac_clock_sm(void) { return 0; }
bool dual_dma_start_quad5_rows(const uint32_t *const desc[4],unsigned log2,unsigned cpw) {
    assert(log2==11 && (cpw==1||cpw==2));
    assert(writers==4 && rows==2 && sample_bits==8 && schedule==0 && dac_clock);
    for(unsigned stream=0;stream<4;stream++)assert(desc[stream]==in_buf+2048*stream);
    starts++;running=true;dma_claims|=255;sm_claims[2]|=15;sm_claims[0]|=1;
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
    dma_claims|=256;sm_claims[1]|=1;
    for(unsigned j=0;j<count;j++)data[j]=0x10004000u^j;
    if(is("pointermutate"))in_buf[0]^=4;
    return true;
}
void dual_dma_stop(void) {
    assert(starts && !stops);stops++;running=false;
    dma_claims=initial_dma;memcpy(sm_claims,initial_sm,sizeof sm_claims);
    if(is("leak"))dma_claims|=1;
}
uint32_t dual_dma_capture_count(void) { assert(stops);return is("shortcapture")?4095:4096; }
uint32_t dual_dma_capture_errors(void) { assert(stops);return is("caperror")?DUAL_DMA_ERR_CAPTURE_DMA:0; }
static bool test_build5(uint32_t *table,unsigned capacity) {
    if(is("tablefail"))return false;
    bool ok=iq5hist_build(table,capacity);if(is("tablecrc"))table[0]^=1;return ok;
}
static bool test_pointers(uint32_t *const ptrs[4],unsigned words,const uint32_t *i,const uint32_t *q,
                          unsigned axis_words,uint32_t base,unsigned row_words) {
    if(is("pointerfail"))return false;
    return iq4quad_build(ptrs,words,i,q,axis_words,base,row_words);
}
static void restore_prbs(uint32_t *data,size_t count,uint32_t *seed) {
    assert(data==in_buf && count==IN_WORDS && *seed==SEED);restores++;prbs_fill(data,count,seed);
}
#define iq5hist_build test_build5
#define iq4quad_build test_pointers
#define prbs_fill restore_prbs
'''

MAIN = r'''
#undef prbs_fill
int main(int argc,char **argv) {
    if(argc!=7)return 2;
    mode=argv[1];
    unsigned bits=(unsigned)strtoul(argv[2],NULL,0);
    int cpw=atoi(argv[3]),invert=atoi(argv[4]),prio=atoi(argv[5]),ms=atoi(argv[6]);
    void *allocation=malloc(sizeof ring_buf+131071u);assert(allocation);
    ring_storage=(uint32_t *)(((uintptr_t)allocation+131071u)&~(uintptr_t)131071u);
    uint32_t seed=SEED;prbs_fill(in_buf,IN_WORDS,&seed);
    uint32_t original_crc=crc32_update(0,in_buf,sizeof in_buf);
    dma_claims=1u<<15;sm_claims[0]=2;sm_claims[1]=4;
    if(is("claimsbusy"))dma_claims=255;
    if(is("sm2busy"))sm_claims[2]=1;
    if(is("sm0full"))sm_claims[0]=15;
    if(is("sm1full"))sm_claims[1]=15;
    initial_dma=dma_claims;memcpy(initial_sm,sm_claims,sizeof sm_claims);
    (void)bits;bench5quad8(cpw,ms,invert,prio);
    bool unchanged=original_crc==crc32_update(0,in_buf,sizeof in_buf);
    bool selected=writers==4&&rows==4&&sample_bits==14&&schedule==3&&priority==7&&!dac_clock;
    bool released=dma_claims==initial_dma&&!memcmp(sm_claims,initial_sm,sizeof sm_claims);
    assert(!running);
    if(restores)assert(unchanged);
    printf("@native {\"starts\":%u,\"stops\":%u,\"captures\":%u,\"restores\":%u,"
           "\"selections\":%u,\"unchanged\":%s,\"selected\":%s,\"released\":%s}\n",
           starts,stops,captures,restores,selections,unchanged?"true":"false",
           selected?"true":"false",released?"true":"false");
    free(allocation);return 0;
}
'''


class Bench5Quad(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='bench5quad-native-')
        directory = Path(cls.temp.name)
        source = (SRC / 'bench5quad.c').read_text()
        # Retain the complete production enums, claim snapshot and command body.
        body = source[source.index('enum {'):]
        native = directory / 'bench.c'
        native.write_text(FIXTURE + body + MAIN)
        cls.executable = directory / 'bench'
        subprocess.run(['cc', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I'+str(SRC),
                        str(native), *(str(SRC/name) for name in
                                       ('iqgen.c', 'iq8.c', 'iq4hist.c', 'iq5hist.c', 'iq4quad.c')),
                        '-o', str(cls.executable)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_case(self, mode='positive', bits=8, cpw=1, invert=0, priority=13, ms=100):
        output = subprocess.check_output([str(self.executable), mode, str(bits), str(cpw),
                                          str(invert), str(priority), str(ms)], text=True)
        lines = output.splitlines()
        result = [json.loads(line[1:]) for line in lines if line.startswith('@{')]
        native = [json.loads(line[8:]) for line in lines if line.startswith('@native ')]
        self.assertEqual(len(result), 1)
        self.assertEqual(len(native), 1)
        return result[0], native[0], lines

    def test_positive_formats_rates_priorities_and_inversion(self):
        for bits in (8,):
            for cpw in (1, 2):
                for invert in (0, 1):
                    for priority in (0, 13):
                        with self.subTest(bits=bits, cpw=cpw, invert=invert, priority=priority):
                            r, n, lines = self.run_case(bits=bits, cpw=cpw, invert=invert, priority=priority)
                            self.assertEqual(r['errors'], 0)
                            self.assertEqual(r['cmd'], 'bench5quad8')
                            self.assertEqual((r['sample_bits'], r['row_words']), (8, 2))
                            self.assertEqual((r['sps'], r['row_padding_bytes']), (5, 3))
                            self.assertEqual(r['table_crc'], 0xa299c7c1)
                            self.assertEqual(r['table_bytes'], 131072)
                            self.assertEqual((r['cpw'], r['invert'], r['bus_priority']), (cpw, invert, priority))
                            self.assertEqual(r['sym_rate'], 12800000/cpw)
                            self.assertEqual((r['period_symbols'], r['pointer_words'], r['pointer_ring_words']),
                                             (4096, 8192, 2048))
                            self.assertEqual(r['input_crc'], 3034056686)
                            self.assertEqual(r['pointer_crc'], r['pointer_after_crc'])
                            self.assertEqual(r['pointer_bases'], [r['pointer_base']+8192*k for k in range(4)])
                            self.assertTrue(r['restored'] and r['selectors_restored'] and r['resources_released'])
                            self.assertGreaterEqual(r['elapsed_us'], 100000)
                            self.assertLessEqual(r['max_poll_gap_us'], 256)
                            self.assertEqual((n['starts'], n['stops'], n['captures'], n['restores']), (1, 1, 1, 1))
                            self.assertTrue(n['unchanged'] and n['selected'] and n['released'])
                            captures = [line.split()[2:] for line in lines if line.startswith('@cap ')]
                            words = [int(word, 16) for row in captures for word in row]
                            self.assertEqual(len(words), 4096)
                            data = b''.join(word.to_bytes(4, 'little') for word in words)
                            self.assertEqual(r['cap_crc'], zlib.crc32(data))
                            self.assertIn('@capend', lines)

    def test_capture_and_output_failures_stop_and_restore(self):
        for mode, mask in [('capturefail', 1<<17), ('shortcapture', 1<<17),
                           ('outputfault', 1), ('stopfault', 2), ('caperror', 8),
                           ('startfail', 1<<19), ('pointermutate', 1<<16), ('pollgap', 1<<20)]:
            with self.subTest(mode=mode):
                r, n, _ = self.run_case(mode)
                self.assertTrue(r['errors'] & mask)
                self.assertEqual((n['starts'], n['stops'], n['restores']), (1, 1, 1))
                self.assertTrue(n['unchanged'] and n['selected'] and n['released'])
                self.assertTrue(r['restored'] and r['selectors_restored'] and r['resources_released'])
                if mode in ('outputfault', 'pollgap', 'startfail'):
                    self.assertLess(r['elapsed_us'], 100000)
                    self.assertEqual(n['captures'], 0)

    def test_setup_failures_release_and_restore_without_output_stop(self):
        for mode, mask in [('selectfail', 1<<19), ('tablefail', 1<<18),
                           ('tablecrc', 1<<18), ('pointerfail', 1<<16)]:
            with self.subTest(mode=mode):
                r, n, _ = self.run_case(mode)
                self.assertTrue(r['errors'] & mask)
                self.assertEqual((n['starts'], n['stops'], n['captures'], n['restores']), (0, 0, 0, 1))
                self.assertTrue(n['unchanged'] and n['selected'] and n['released'])

    def test_failed_cleanup_does_not_restore_borrowed_source(self):
        for mode, key in [('leak', 'resources_released'), ('restorefail', 'selectors_restored')]:
            with self.subTest(mode=mode):
                r, n, _ = self.run_case(mode)
                self.assertTrue(r['errors'] & (1<<19))
                self.assertFalse(r[key])
                self.assertFalse(r['restored'])
                self.assertEqual(n['restores'], 0)

    def test_admission_rejects_invalid_values_busy_claims_and_owned_output(self):
        cases = [dict(mode=mode) for mode in ('claimsbusy', 'sm2busy', 'sm0full', 'sm1full', 'clock', 'owned')]
        cases += [dict(cpw=0), dict(cpw=3), dict(ms=99), dict(ms=60001), dict(invert=2),
                  dict(priority=-1), dict(priority=16), dict(cpw=4)]
        for options in cases:
            with self.subTest(options=options):
                r, n, lines = self.run_case(**options)
                self.assertIn('error', r)
                self.assertEqual((n['starts'], n['stops'], n['captures'], n['restores']), (0, 0, 0, 0))
                self.assertTrue(n['unchanged'] and n['selected'] and n['released'])
                self.assertFalse(any(line.startswith('@cap ') for line in lines))


if __name__ == '__main__':
    unittest.main()
