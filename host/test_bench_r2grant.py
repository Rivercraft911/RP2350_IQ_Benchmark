"""Execute the complete R2 command against SDK/register/resource shims.

Configuration assertions and injected failures exercise production driver code;
the synthetic DMA completion supplies no device arbitration or timing evidence.
"""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware/src"

FIXTURE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define IQ4HIST_EXPERIMENT 1
#include "config.h"
#include "iqgen.h"
#include "r2grant.h"
#define GIT_REV "native-shim"
#define SEED 0x1234abcdu
static uint32_t ring_buf[N_BLOCKS * BLOCK_WORDS], in_buf[IN_WORDS];
enum { NUM_DMA_CHANNELS=16, NUM_DMA_IRQS=4, clk_sys=0, DMA_SIZE_32=2,
       DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB=0, DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS=15,
       DMA_CH0_CTRL_TRIG_EN_BITS=1u<<8, DMA_CH0_CTRL_TRIG_BUSY_BITS=1u<<9,
       DMA_CH0_CTRL_TRIG_READ_ERROR_BITS=1u<<28,
       DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS=1u<<29,
       DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS=1u<<30, PIO_FDEBUG_TXSTALL_LSB=24,
       BUSCTRL_BUS_PRIORITY_ACK_BITS=1, pio_isr=6, pio_osr=7, pio_x=1 };
typedef struct { uintptr_t read_addr, write_addr; uint32_t transfer_count,ctrl_trig,al1_ctrl;
                 uint32_t al1_transfer_count_trig; } dma_ch_t;
static struct { dma_ch_t ch[16]; uint32_t intr,abort; } hardware_dma;
#define dma_hw (&hardware_dma)
static struct { uint32_t priority,priority_ack; } hardware_bus={7,1};
#define busctrl_hw (&hardware_bus)
typedef struct { uint32_t txf[4],rxf[4],ctrl,fdebug; } pio_t;
typedef pio_t *PIO;
static pio_t hardware_pio;
#define pio0 (&hardware_pio)
typedef struct { const uint16_t *instructions; unsigned length; int origin; } pio_program_t;
typedef struct { unsigned div,wrap0,wrap1; } pio_sm_config;
typedef struct { bool read_inc,write_inc,quiet,high; unsigned dreq,chain,size; } dma_channel_config;
static dma_channel_config configs[16];
static unsigned dma_claims,sm_claims,added,configs_count,seed[4],dividers[4],starts;
static unsigned put_counts[4],execs[4],tick,claim_calls,restores;
static unsigned requested_priority;
static bool started,completed;
static const char *mode;
static bool is(const char *s) { return !strcmp(mode,s); }
static void emulate(void) {
    if(!started||completed)return;
    completed=true;pio0->fdebug=0;dma_hw->intr=0;
    for(unsigned stream=0;stream<4;stream++) {
        const unsigned input=stream*3,reload=input+1,capture=input+2;
        uint32_t *source=(uint32_t *)dma_hw->ch[input].read_addr;
        uint32_t *dest=(uint32_t *)dma_hw->ch[capture].write_addr;
        const uint32_t *counts=(const uint32_t *)dma_hw->ch[reload].read_addr;
        unsigned issued=0,batches=0;
        while(counts[batches]) { assert(counts[batches]==512);issued+=counts[batches++];assert(batches<=8); }
        assert(issued<=seed[stream]+1 && counts[batches]==0);
        for(unsigned j=0;j<issued;j++)dest[j]=source[j];
        dma_hw->ch[input].read_addr+=(uintptr_t)issued*4;
        dma_hw->ch[reload].read_addr+=(uintptr_t)(batches+1)*4;
        dma_hw->ch[capture].transfer_count-=issued;
        dma_hw->ch[input].ctrl_trig&=~DMA_CH0_CTRL_TRIG_BUSY_BITS;
        dma_hw->ch[reload].ctrl_trig&=~DMA_CH0_CTRL_TRIG_BUSY_BITS;
        if(!dma_hw->ch[capture].transfer_count)dma_hw->ch[capture].ctrl_trig&=~DMA_CH0_CTRL_TRIG_BUSY_BITS;
        else pio0->fdebug|=1u<<(PIO_FDEBUG_TXSTALL_LSB+stream);
        if(!is("nullfail"))dma_hw->intr|=1u<<input;
        if(is("dmafail")&&stream==2)dma_hw->ch[input].ctrl_trig|=DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS;
        if(is("orderfail")&&stream==1)dest[12]^=0x80;
        if(is("guardfail")&&stream==3)dest[seed[stream]+1]^=1;
    }
}
static uint32_t clock_get_hz(int c) { assert(c==clk_sys);return is("sys256")?256000000u:128000000u; }
static uint32_t time_us_32(void) {
    tick+=50;
    if(dma_hw->abort&&!is("abortfail"))dma_hw->abort=0;
    return tick;
}
static void tight_loop_contents(void) {}
static void busy_wait_us_32(uint32_t us) { tick+=us;emulate(); }
static void __dmb(void) {}
static void hw_clear_bits(uint32_t *reg,uint32_t mask) { *reg&=~mask; }
static void hw_write_masked(uint32_t *reg,uint32_t value,uint32_t mask) { *reg=(*reg&~mask)|(value&mask); }
static uint16_t pio_encode_delay(unsigned n) { assert(n==31);return n<<8; }
static uint16_t pio_encode_pull(bool iff,bool block) { assert(!iff&&block);return 0x80a0; }
static uint16_t pio_encode_push(bool iff,bool block) { assert(!iff&&block);return 0x8020; }
static uint16_t pio_encode_mov(unsigned dest,unsigned src) { assert(src==pio_osr);return 0xa000u|(dest<<5)|src; }
static uint16_t pio_encode_jmp_x_dec(unsigned dest) { return 0x0040|dest; }
static uint16_t pio_encode_jmp(unsigned dest) { return dest; }
static bool pio_can_add_program(PIO pio,const pio_program_t *program) {
    assert(pio==pio0&&program->length==5);return !is("programfail");
}
static unsigned pio_add_program(PIO pio,const pio_program_t *program) {
    assert(pio==pio0&&program->length==5&&program->origin==-1);
    const uint16_t want[]={0x9fa0,0xbfC7,0x9f20,0x1f40,0x0004};
    assert(!memcmp(want,program->instructions,sizeof want));added++;return 0;
}
static void pio_remove_program(PIO pio,const pio_program_t *program,unsigned offset) {
    assert(pio==pio0&&program->length==5&&!offset&&added==1);added--;
}
static int pio_claim_unused_sm(PIO pio,bool required) {
    assert(pio==pio0&&!required);
    if(is("smfail")&&sm_claims==3)return -1;
    for(unsigned n=0;n<4;n++)if(!(sm_claims&(1u<<n))) { sm_claims|=1u<<n;return n; }
    return -1;
}
static void pio_sm_unclaim(PIO pio,unsigned n) { assert(pio==pio0&&(sm_claims&(1u<<n)));sm_claims&=~(1u<<n); }
static pio_sm_config pio_get_default_sm_config(void) { return (pio_sm_config){0}; }
static void sm_config_set_wrap(pio_sm_config *c,unsigned lo,unsigned hi) { c->wrap0=lo;c->wrap1=hi; }
static void sm_config_set_clkdiv_int_frac8(pio_sm_config *c,unsigned i,unsigned f) { assert(!f);c->div=i; }
static void pio_sm_init(PIO pio,unsigned n,unsigned entry,const pio_sm_config *c) {
    assert(pio==pio0&&!entry&&c->wrap0==0&&c->wrap1==4);dividers[n]=c->div;
}
static void pio_sm_put(PIO pio,unsigned n,uint32_t word) { assert(pio==pio0&&!put_counts[n]++);seed[n]=word; }
static void pio_sm_exec_wait_blocking(PIO pio,unsigned n,unsigned instr) {
    assert(pio==pio0);assert(instr==(!execs[n]?0x80a0u:0xa027u));execs[n]++;
}
static bool pio_sm_is_tx_fifo_full(PIO pio,unsigned n) { assert(pio==pio0&&n<4);return !is("prefillfail"); }
static void pio_enable_sm_mask_in_sync(PIO pio,unsigned mask) { assert(pio==pio0&&mask==15);pio->ctrl=mask;started=true; }
static void pio_sm_clear_fifos(PIO pio,unsigned n) { assert(pio==pio0&&n<4); }
static unsigned pio_get_dreq(PIO pio,unsigned n,bool tx) { assert(pio==pio0&&n<4);return n+(tx?0:4); }
static int dma_claim_unused_channel(bool required) {
    assert(!required);claim_calls++;
    if(is("claimfail")&&claim_calls==5)return -1;
    for(unsigned ch=0;ch<16;ch++)if(!(dma_claims&(1u<<ch))) { dma_claims|=1u<<ch;return ch; }
    return -1;
}
static void dma_channel_unclaim(unsigned ch) { assert(dma_claims&(1u<<ch));dma_claims&=~(1u<<ch); }
static void dma_channel_cleanup(unsigned ch) { assert(ch<16); }
static void dma_irqn_set_channel_enabled(unsigned irq,unsigned ch,bool en) { assert(irq<4&&ch<16&&!en); }
static bool dma_channel_is_busy(unsigned ch) { return dma_hw->ch[ch].ctrl_trig&DMA_CH0_CTRL_TRIG_BUSY_BITS; }
static dma_channel_config dma_channel_get_default_config(unsigned ch) { return (dma_channel_config){.chain=ch}; }
static void channel_config_set_transfer_data_size(dma_channel_config *c,unsigned v) { c->size=v; }
static void channel_config_set_read_increment(dma_channel_config *c,bool v) { c->read_inc=v; }
static void channel_config_set_write_increment(dma_channel_config *c,bool v) { c->write_inc=v; }
static void channel_config_set_dreq(dma_channel_config *c,unsigned v) { c->dreq=v; }
static void channel_config_set_chain_to(dma_channel_config *c,unsigned v) { c->chain=v; }
static void channel_config_set_irq_quiet(dma_channel_config *c,bool v) { c->quiet=v; }
static void channel_config_set_high_priority(dma_channel_config *c,bool v) { c->high=v; }
static void dma_channel_configure(unsigned ch,const dma_channel_config *c,void *write,const void *read,unsigned count,bool trigger) {
    const unsigned stream=ch/3,kind=ch%3;
    assert(ch<12&&!trigger&&c->size==DMA_SIZE_32&&c->quiet&&c->high);configs_count++;
    if(kind==0) { assert(c->read_inc&&!c->write_inc&&c->dreq==stream&&c->chain==ch+1&&!count);
        assert(write==&pio0->txf[stream]&&read==in_buf+(stream&1u)*4096); }
    if(kind==1) { assert(c->read_inc&&!c->write_inc&&c->chain==ch&&count==1);
        assert(write==&dma_hw->ch[ch-1].al1_transfer_count_trig); }
    if(kind==2) { assert(!c->read_inc&&c->write_inc&&c->dreq==stream+4&&c->chain==ch&&count==seed[stream]+1);
        assert(read==&pio0->rxf[stream]&&write==ring_buf+stream*4100); }
    configs[ch]=*c;dma_hw->ch[ch].read_addr=(uintptr_t)read;dma_hw->ch[ch].write_addr=(uintptr_t)write;
    dma_hw->ch[ch].transfer_count=count;dma_hw->ch[ch].ctrl_trig=DMA_CH0_CTRL_TRIG_EN_BITS;
}
static void dma_start_channel_mask(uint32_t mask) {
    assert(mask==0xdb6&&configs_count==12);starts++;
    const unsigned p=requested_priority;
    assert(hardware_bus.priority==((p&1u)|((p&2u)<<3)|((p&4u)<<6)|((p&8u)<<9)));
    for(unsigned ch=0;ch<12;ch++)if(mask&(1u<<ch))dma_hw->ch[ch].ctrl_trig|=DMA_CH0_CTRL_TRIG_BUSY_BITS;
}
static void test_restore(uint32_t *w,size_t n,uint32_t *seed) { assert(w==in_buf&&n==IN_WORDS&&*seed==SEED);restores++;prbs_fill(w,n,seed); }
#define prbs_fill test_restore
'''

MAIN = r'''
#undef prbs_fill
int main(int argc,char **argv) {
    assert(argc==7);mode=argv[1];
    if(is("priorityfail"))hardware_bus.priority_ack=0;
    int batches=atoi(argv[2]),cpw=atoi(argv[3]),gap=atoi(argv[4]),fault=atoi(argv[5]),prio=atoi(argv[6]);
    requested_priority=(unsigned)prio;
    bench_r2grant(batches,cpw,gap,fault,prio);
    printf("@native {\"dma_claims\":%u,\"sm_claims\":%u,\"programs\":%u,\"configs\":%u,"
           "\"starts\":%u,\"restores\":%u,\"priority\":%u}\n",dma_claims,sm_claims,added,configs_count,starts,restores,hardware_bus.priority);
    return 0;
}
'''


class BenchR2Grant(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="bench-r2grant-native-")
        directory = Path(cls.temp.name)
        body = (SRC / "bench_r2grant.c").read_text().split("enum {", 1)[1]
        source = directory / "driver.c"
        source.write_text(FIXTURE + "enum {" + body + MAIN)
        cls.executable = directory / "driver"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I" + str(SRC),
                        str(source), str(SRC / "r2grant.c"), str(SRC / "iqgen.c"),
                        "-o", str(cls.executable)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_case(self, mode="positive", batches=8, cpw=1, gap=6000, fault=0, priority=13):
        output = subprocess.check_output([str(self.executable), mode, str(batches), str(cpw),
                                          str(gap), str(fault), str(priority)], text=True)
        result = [json.loads(line[1:]) for line in output.splitlines() if line.startswith("@{")]
        native = [json.loads(line[8:]) for line in output.splitlines() if line.startswith("@native ")]
        self.assertEqual((len(result), len(native)), (1, 1))
        return result[0], native[0]

    def assert_released(self, n):
        self.assertEqual((n["dma_claims"], n["sm_claims"], n["programs"], n["priority"]), (0, 0, 0, 7))

    def test_real_setup_echo_order_null_and_cleanup(self):
        from test_r2grant import source
        for batches in (1, 2, 8):
            for cpw in (1, 2, 4):
                for mode in ("positive", "sys256"):
                    r, n = self.run_case(mode, batches, cpw, priority=13 if mode == "positive" else 0)
                    words = batches * 512
                    self.assertEqual(r["errors"], 0)
                    self.assertEqual(r["issued"], [words] * 4)
                    self.assertEqual(r["captured"], [words] * 4)
                    self.assertEqual(r["null_mask"], r["input_mask"])
                    self.assertEqual(r["crc"], [zlib.crc32(b"".join(source(j & 1, k).to_bytes(4, "little")
                                                                      for k in range(words))) for j in range(4)])
                    self.assertEqual(r["crc"], r["expected_crc"])
                    self.assertTrue(r["guards_ok"] and r["released"])
                    self.assertEqual((n["configs"], n["starts"], n["restores"]), (12, 1, 1))
                    self.assert_released(n)

    def test_short_published_prefix_negative_control(self):
        r, n = self.run_case(fault=1)
        self.assertEqual(r["errors"], 4 | 32)
        self.assertEqual(r["issued"], [4096, 4096, 4096, 3584])
        self.assertEqual(r["captured"], r["issued"])
        self.assertEqual(r["txstall_mask"], 8)
        self.assertEqual(r["null_mask"], r["input_mask"])
        self.assert_released(n)

    def test_compact_priority_mask_expands_to_all_bus_register_fields(self):
        for priority in range(16):
            r, n = self.run_case(batches=1, priority=priority)
            self.assertEqual(r["errors"], 0)
            self.assertEqual(r["priority"], priority)
            self.assert_released(n)

    def test_failures_and_cleanup(self):
        for mode, mask in (("programfail", 1), ("smfail", 1), ("claimfail", 1),
                           ("prefillfail", 4), ("priorityfail", 1 | 16),
                           ("nullfail", 4 | 64), ("dmafail", 2),
                           ("orderfail", 8), ("guardfail", 8)):
            with self.subTest(mode=mode):
                r, n = self.run_case(mode)
                self.assertEqual(r["errors"] & mask, mask)
                self.assert_released(n)
        r, n = self.run_case("abortfail")
        self.assertTrue(r["errors"] & 16)
        self.assertFalse(r["released"])
        self.assertEqual((n["dma_claims"], n["sm_claims"], n["programs"], n["restores"]), (4095, 15, 1, 0))

    def test_invalid_parameters_do_not_claim_or_borrow(self):
        for args in ({"batches": 0}, {"batches": 9}, {"cpw": 3}, {"gap": 20001},
                     {"fault": 2}, {"fault": 1, "batches": 1}, {"priority": 16}):
            r, n = self.run_case(**args)
            self.assertIn("error", r)
            self.assertEqual((n["configs"], n["starts"], n["restores"]), (0, 0, 0))
            self.assert_released(n)


if __name__ == "__main__":
    unittest.main()
