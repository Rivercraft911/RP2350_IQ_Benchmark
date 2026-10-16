"""Actual wide-ingress close/credit/reset C and PIO words, without device access.

The independent decoder checks the emitted RX and sender instructions, shift
ordering, truncated/extra beats and synchronizer latency skew. Native register shims
exercise delayed retirement, exact terminal PC, CRC publication and credits.
Neither model represents DMA arbitration, pad timing or M33 execution time.
"""
from pathlib import Path
import json
import subprocess
import tempfile
import zlib

from cm5.pv_spi_tx import message, pattern_payload
from test_dual_faults import function_body
import fwsrc

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "pvproto.h"
typedef unsigned uint;
typedef struct { uint32_t dbg_padout,dbg_padoe; } pio_mock_t;
typedef pio_mock_t *PIO;
#define PV_SLOTS 24
#define PV_EMU_MSGS 16
#define PV_EMU_WORDS (PV_EMU_MSGS * PV_MSG_WORDS)
#define IN_WORDS 8192
#define CAP_WORDS_MAX 14336
#define SRAM_HI /* device: section(".sram_hi") */
#define DMA_CH0_CTRL_TRIG_BUSY_BITS 0x04000000u
#define DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS 0x80000000u
enum { pio_pins=0,pio_x=1,pio_y=2,pio_null=3,pio_isr=6,pio_osr=7 };
static uint32_t in_buf[IN_WORDS];
static uint16_t pio_encode_wait_gpio(bool high,uint p) { return 0x2000u|(high<<7)|p; }
static uint16_t pio_encode_in(uint src,uint n) { return 0x4000u|(src<<5)|(n&31); }
static uint16_t pio_encode_out(uint dest,uint n) { return 0x6000u|(dest<<5)|(n&31); }
static uint16_t pio_encode_pull(bool iff,bool block) { return 0x8080u|(iff<<6)|(block<<5); }
static uint16_t pio_encode_sideset(uint n,uint v) { return v<<(13-n); }
static uint16_t pio_encode_delay(uint n) { return n<<8; }
static uint16_t pio_encode_set(uint dest,uint v) { return 0xe000u|(dest<<5)|v; }
static uint16_t pio_encode_mov(uint dest,uint src) { return 0xa000u|(dest<<5)|src; }
static uint16_t pio_encode_jmp_x_dec(uint dest) { return 0x0040u|dest; }
static uint16_t pio_encode_jmp(uint dest) { return dest; }
static uint16_t pio_encode_irq_wait(bool rel,uint n) { return 0xc020u|(rel<<4)|n; }
static uint16_t pio_encode_irq_set(bool rel,uint n) { return 0xc000u|(rel<<4)|n; }
typedef struct { uint32_t transfer_count,ctrl_trig; } dma_channel_hw_t;
static dma_channel_hw_t rx;
static uint32_t now,retire_at,pc_at,cs_at,pc,fifo,residue,cycle_clock;
static uint32_t xreg,isrreg,shift_count;
static unsigned credits,rearms,aborts,resets,restarts,crc_reads,barriers;
static bool cs,irq1,enabled,interrupts;
static uint32_t cycles(void) { return ++cycle_clock; }
static uint32_t time_us_32(void) {
    ++now;
    if(retire_at&&now==retire_at)rx.transfer_count=rx.ctrl_trig=0;
    if(pc_at&&now==pc_at)pc=16+5;
    if(cs_at&&now==cs_at)cs=false;
    return now;
}
static dma_channel_hw_t *dma_channel_hw_addr(uint ch) { assert(ch==2);return &rx; }
static uint pio_sm_get_pc(PIO p,uint sm) { (void)p;assert(sm==1);return pc; }
static bool pio_interrupt_get(PIO p,uint n) { (void)p;assert(n==1);return irq1; }
static uint pio_sm_get_rx_fifo_level(PIO p,uint sm) { (void)p;assert(sm==1);return fifo; }
static bool gpio_get(uint pin) { assert(pin==26);return cs; }
static void dma_channel_abort(uint ch) { assert(ch==2);++aborts;rx=(dma_channel_hw_t){0}; }
static void pio_sm_set_enabled(PIO p,uint sm,bool on) { (void)p;assert(sm<4);if(sm==1)enabled=on; }
static void pio_sm_clear_fifos(PIO p,uint sm) { (void)p;assert(sm<4);if(sm==1){fifo=0;++resets;} }
static void pio_sm_restart(PIO p,uint sm) { (void)p;assert(sm==1);shift_count=0;++restarts; }
static void pio_sm_exec(PIO p,uint sm,uint16_t instruction) {
    (void)p;assert(sm==1&&!enabled);
    const uint op=instruction>>13,dest=(instruction>>5)&7,arg=instruction&31;
    if(op==7) { assert(dest==pio_x);xreg=arg; }
    else if(op==2) { assert(dest==pio_x&&arg==5);isrreg=(isrreg<<5)|(xreg&31);shift_count+=5;assert(shift_count<32); }
    else if(op==5) {
        if(dest==pio_x) { assert(arg==pio_isr);xreg=isrreg; }
        else { assert(dest==pio_isr&&arg==pio_null);isrreg=0; }
    } else { assert(op==0);pc=arg; }
}
static void pio_interrupt_clear(PIO p,uint n) { (void)p;if(n==1)irq1=false;else assert(n==0); }
static void dma_sniffer_set_data_accumulator(uint32_t seed) { assert(seed==0xffffffffu);residue=0; }
static uint32_t dma_sniffer_get_data_accumulator(void) {
    assert(!rx.transfer_count&&!(rx.ctrl_trig&DMA_CH0_CTRL_TRIG_BUSY_BITS));++crc_reads;return residue;
}
static void dma_channel_transfer_to_buffer_now(uint ch,uint32_t *dest,uint n) {
    assert(ch==2&&dest>=in_buf&&dest<in_buf+IN_WORDS&&n==PV_MSG_WORDS);
    ++rearms;rx.transfer_count=n;rx.ctrl_trig=DMA_CH0_CTRL_TRIG_BUSY_BITS;
}
static void pio_sm_put(PIO p,uint sm,uint32_t token) { (void)p;assert(sm==3&&!token);++credits; }
static void __dmb(void) { ++barriers; }
static void __dsb(void) {}
static uint32_t save_and_disable_interrupts(void) { uint32_t prev=interrupts;interrupts=false;return prev; }
static void restore_interrupts(uint32_t saved) { interrupts=saved; }
'''

RELEASE_FIXTURE = r'''
#define DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB 0
#define DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS 15
#define DMA_CH0_CTRL_TRIG_EN_BITS 256
typedef struct { const uint16_t *instructions;uint length;int origin; } pio_program_t;
static struct { struct {uint32_t al1_ctrl;} ch[16];uint32_t sniff_ctrl,sniff_data; } dma_mock;
#define dma_hw (&dma_mock)
static uint dma_claimed,sm_claimed,removed;
static bool irq_enabled,handler_removed;
enum { pis_interrupt0=0 };
static void irq_set_enabled(uint irq,bool on) { assert(irq==7);irq_enabled=on; }
static void pio_set_irq0_source_enabled(PIO p,uint irq,bool on) { (void)p;assert(irq==0&&!on); }
static void pio_set_sm_mask_enabled(PIO p,uint mask,bool on) { (void)p;assert(mask==sm_claimed&&!on); }
static void irq_clear(uint irq) { assert(irq==7); }
static void irq_remove_handler(uint irq,void(*handler)(void)) { assert(irq==7&&handler);handler_removed=true; }
static void hw_write_masked(uint32_t *reg,uint32_t value,uint32_t mask) { *reg=(*reg&~mask)|(value&mask); }
static void dma_channel_cleanup(uint ch) {
    assert(dma_claimed&(1u<<ch));
    for(uint n=0;n<16;++n)if(dma_claimed&(1u<<n))assert(dma_hw->ch[n].al1_ctrl==n);
}
static void dma_channel_unclaim(uint ch) { assert(dma_claimed&(1u<<ch));dma_claimed&=~(1u<<ch); }
static void pio_sm_set_pindirs_with_mask(PIO p,uint sm,uint32_t bits,uint32_t mask) {
    assert(sm_claimed&(1u<<sm));p->dbg_padoe=(p->dbg_padoe&~mask)|(bits&mask);
}
static void pio_sm_set_pins_with_mask(PIO p,uint sm,uint32_t bits,uint32_t mask) {
    assert(sm_claimed&(1u<<sm));p->dbg_padout=(p->dbg_padout&~mask)|(bits&mask);
}
static void pio_sm_unclaim(PIO p,uint sm) { (void)p;assert(sm_claimed&(1u<<sm));sm_claimed&=~(1u<<sm); }
static void pio_remove_program(PIO p,const pio_program_t *program,uint offset) {
    (void)p;assert(program->origin==-1&&program->length>=5&&offset<32);++removed;
}
'''

CHECKS = r'''
static w_state_t s;
static uint32_t emu[PV_EMU_WORDS];
static void init(uint32_t remaining,uint32_t ctrl,uint32_t rxpc) {
    memset(&s,0,sizeof s);rx=(dma_channel_hw_t){remaining,ctrl};
    s.ch_rx=2;s.sm_rx=1;s.sm_gate=3;s.off_rx=16;s.beats=2664;
    s.emu_base=emu;s.source_bytes=PV_MSG_BYTES;s.emulate=true;
    s.armed=s.admitting=true;w_active=&s;
    now=retire_at=pc_at=cs_at=fifo=cycle_clock=0;pc=rxpc;
    credits=rearms=aborts=resets=restarts=crc_reads=barriers=0;
    irq1=false;cs=interrupts=enabled=true;residue=0x2144df1cu;
}
static void accepted(void) {
    assert(s.head==1&&s.slot_crc[0]==1&&crc_reads==1&&!aborts);
    assert(s.started==1&&!s.errors&&s.armed&&credits==1&&rearms==1);
    assert(resets==1&&restarts==2&&xreg==s.beats-1&&!shift_count&&!isrreg&&enabled);
}
int main(void) {
    assert(w_pins[6]==26);
    init(0,0,21);w_cs_isr();accepted();
    for(unsigned busy=0;busy<2;++busy) {
        init(busy,busy?DMA_CH0_CTRL_TRIG_BUSY_BITS:0,20);retire_at=pc_at=3;
        w_cs_isr();accepted();
    }
    for(unsigned remaining=1;remaining<=PV_MSG_WORDS;++remaining) {
        init(remaining,DMA_CH0_CTRL_TRIG_BUSY_BITS,18);w_cs_isr();
        assert(!s.head&&!crc_reads&&s.short_msgs==1&&s.errors==W_ERR_SHORT&&s.drain_timeouts==1&&aborts==1);
        assert(s.armed&&credits==1&&rearms==1);
    }
    // Last word full but a missing falling clock/terminal instruction cannot pass.
    for(unsigned offset=0;offset<5;++offset) {
        init(0,0,16+offset);w_cs_isr();assert(!s.head&&s.short_msgs==1&&!crc_reads);
    }
    init(0,DMA_CH0_CTRL_TRIG_BUSY_BITS,21);w_cs_isr();assert(s.short_msgs==1&&!crc_reads);
    init(0,DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS,21);w_cs_isr();
    assert(s.errors==(W_ERR_SHORT|W_ERR_DMA)&&!s.head);
    for(unsigned offset=5;offset<=7;++offset) {
        init(0,0,16+offset);irq1=offset==5;w_cs_isr();assert(s.long_msgs==1&&!s.head&&!crc_reads);
    }
    init(0,0,21);fifo=1;w_cs_isr();assert(s.long_msgs==1&&!s.head);
    init(0,0,21);cs=false;w_cs_isr();assert(s.late_closes==1&&!s.head&&(s.errors&W_ERR_LATE));
    init(1,DMA_CH0_CTRL_TRIG_BUSY_BITS,20);retire_at=pc_at=cs_at=3;
    w_cs_isr();assert(s.late_closes==1&&!s.head&&!crc_reads);
    init(0,0,21);residue=0;w_cs_isr();assert(s.head==1&&s.slot_crc[0]==0);
    init(0,0,21);s.armed=false;w_cs_isr();assert(s.overflows==1&&(s.errors&W_ERR_OVERFLOW));
    init(0,0,21);s.admitting=false;w_cs_isr();assert(s.head==1&&!s.armed&&!credits&&!rearms);
    init(0,0,21);w_arm(&s);w_arm(&s);assert(!credits&&!rearms);
    s.armed=false;s.head=PV_SLOTS;w_arm(&s);assert(!credits);
    s.tail++;w_arm(&s);w_arm(&s);assert(credits==1&&rearms==1);
    // Reset loads every supported beat count and removes partial ISR history.
    for(unsigned width=1;width<=4;width*=4) {
        init(1,0,18);s.beats=PV_MSG_BYTES*8/width;isrreg=0xffffffffu;shift_count=31;
        w_rx_reset(&s);assert(xreg==s.beats-1&&!isrreg&&!shift_count&&pc==16&&enabled);
    }
    printf("{\"programs\":[");
    for(unsigned width=1;width<=4;width*=4)for(unsigned half=3;half<=16;++half) {
        w_build(&s,width,half);
        printf("%s{\"width\":%u,\"half\":%u,\"rx\":[",width==1&&half==3?"":",",width,half);
        for(unsigned n=0;n<W_RX_LEN;++n)printf("%s%u",n?",":"",s.prog_rx[n]);
        printf("],\"gate\":[");for(unsigned n=0;n<W_GATE_LEN;++n)printf("%s%u",n?",":"",s.prog_gate[n]);
        printf("],\"tx\":[");for(unsigned n=0;n<W_TX_LEN;++n)printf("%s%u",n?",":"",s.prog_tx[n]);printf("]}");
    }
    printf("],\"consume\":[");
    for(unsigned fault=0;fault<2;++fault) {
        init(0,0,21);pv_init(16);s.armed=true;
        for(unsigned m=0;m<16;++m)pv_pattern_message((uint8_t *)(emu+m*PV_MSG_WORDS),m,16);
        if(fault)((uint8_t *)emu)[20]^=1;
        for(unsigned m=0;m<16;++m) {
            memcpy(w_slot(m),emu+m*PV_MSG_WORDS,PV_MSG_BYTES);
            s.slot_crc[m]=pv_crc32(0,w_slot(m),PV_MSG_BYTES)==0x2144df1cu;
        }
        s.head=16;w_consume(&s);assert(s.tail==16&&s.consumed==16&&!s.pattern_mismatches);
        assert(s.validated_bytes==(16-fault)*PV_PAYLOAD);
        printf("%s{\"ok\":%u,\"bad_crc\":%u,\"crc_chain\":%u,\"payload_crc\":%u}",fault?",":"",
               pv_stats.ok,pv_stats.bad_crc,pv_stats.crc_chain,s.payload_crc);
    }
    puts("]}");
    init(0,0,21);pv_init(16);pv_pattern_message((uint8_t *)emu,0,16);
    memcpy(w_slot(0),emu,PV_MSG_BYTES);((uint8_t *)w_slot(0))[100]^=1;
    s.slot_crc[0]=1;s.head=1;w_consume(&s);assert(s.pattern_mismatches==1);
    // Release restores PIO latches while an SM is still owned, and breaks
    // every chain before the first cleanup. Cover external and emulator claims.
    for(unsigned emulate=0;emulate<2;++emulate) {
        pio_mock_t port={.dbg_padout=0xa5a5a5a5u,.dbg_padoe=0x5a5a5a5au};
        init(0,0,21);s.pio=&port;s.emulate=emulate;s.sm_tx=emulate?2:-1;
        s.ch_tx=emulate?4:-1;s.ch_ctl=emulate?5:-1;s.irq_num=7;s.irq_added=true;s.programs_added=true;
        s.pio_out=0x00120000u;s.pio_oe=0x04400000u;s.sniff_ctrl=31;s.sniff_data=99;
        dma_claimed=(1u<<2)|(emulate?((1u<<4)|(1u<<5)):0u);
        sm_claimed=(1u<<1)|(1u<<3)|(emulate?1u<<2:0u);removed=0;
        for(unsigned n=0;n<16;++n)dma_hw->ch[n].al1_ctrl=256|15;
        irq_enabled=true;handler_removed=false;w_release(&s);
        assert(!dma_claimed&&!sm_claimed&&!irq_enabled&&handler_removed&&!w_active);
        assert(removed==2+emulate&&dma_hw->sniff_ctrl==31&&dma_hw->sniff_data==99);
        assert((port.dbg_padout&W_PIN_MASK)==s.pio_out&&(port.dbg_padoe&W_PIN_MASK)==s.pio_oe);
        assert((port.dbg_padout&~W_PIN_MASK)==(0xa5a5a5a5u&~W_PIN_MASK));
        assert((port.dbg_padoe&~W_PIN_MASK)==(0x5a5a5a5au&~W_PIN_MASK));
    }
}
'''


def native():
    source = (ROOT / "firmware/src/ingress/pvwide.c").read_text()
    declarations = source[source.index("enum {"):source.index("static w_claims_t w_claims")]
    functions = [("static uint32_t *", "w_slot", "uint32_t n"),
                 ("static void", "w_build", "w_state_t *s, int width, int half"),
                 ("static void", "w_rx_reset", "w_state_t *s"),
                 ("static void", "w_arm", "w_state_t *s"),
                 ("static bool", "w_close", "w_state_t *s"),
                 ("static void", "w_cs_isr", "void"),
                 ("static void", "w_consume", "w_state_t *s")]
    functions.append(("static void", "w_release", "w_state_t *s"))
    code = FIXTURE + declarations + RELEASE_FIXTURE + "\n".join(
        f"{ret} {name}({args}) {{\n{function_body(source, name)}\n}}" for ret, name, args in functions) + CHECKS
    with tempfile.TemporaryDirectory(prefix="pvwide-native-") as directory:
        directory = Path(directory)
        path, exe = directory / "test.c", directory / "test"
        path.write_text(code)
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        *fwsrc.INCLUDES, str(path), str(ROOT / "firmware/src/ingress/pvproto.c"),
                        "-o", str(exe)], check=True)
        return json.loads(subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout)


def receive(code, width, physical, data_skew=0):
    """Decode actual RX opcodes, two-cycle synchronized inputs, one instruction/cycle."""
    pc, x, isr, fill, irq, words = 0, 1332 * 8 // width - 1, 0, 0, False, []
    # Two-cycle paths, with an optional +/-one-cycle data/clock skew stress.
    for tick in range(len(physical)):
        clock, data, cs = physical[max(0, tick - 2)]
        data = physical[max(0, tick - 2 - data_skew)][1]
        instruction = code[pc]
        op, src, arg = instruction >> 13, (instruction >> 5) & 7, instruction & 31
        following = (pc + 1) % len(code)
        if op == 1:
            pin = clock if arg == 21 else cs if arg == 26 else None
            assert pin is not None and src & 3 == 0
            if pin != bool(instruction & 0x80):
                continue
        elif op == 2:
            assert src == 0 and arg == width
            isr, fill = ((isr << width) | data) & 0xffffffff, fill + width
            if fill == 32:
                words.append(isr.to_bytes(4, "big"))  # DMA BSWAP then little-endian SRAM
                isr = fill = 0
        elif op == 0:
            condition = (instruction >> 5) & 7
            if condition == 2:
                if x:
                    following = arg
                x = (x - 1) & 0xffffffff
            else:
                assert condition == 0
                following = arg
        elif op == 6:
            assert instruction == 0xc001
            irq = True
        else:
            raise AssertionError(hex(instruction))
        pc = following
    return b"".join(words), fill, pc, irq


def sender(program, frame, beats=None):
    """Actual OUT/JMP clock and setup instructions; enough FIFO bandwidth assumed."""
    width, half, code = program["width"], program["half"], program["tx"]
    beats = len(frame) * 8 // width if beats is None else beats
    assert code[:3] == [0x2096, 0xe000, 0xa022]
    assert code[5] == 0xe001
    assert code[6:] == [0xe03f, 0x0f47, 0xe03f, 0x0f49, 0xe03f, 0x0f4b]
    assert code[3] == 0x6000 | (half - 1) << 8 | width
    assert code[4] == 0x1043 | (half - 1) << 8
    physical = [(False, 0, True)] * 8 + [(False, 0, False)] * 2
    values = [(byte >> shift) & ((1 << width) - 1)
              for byte in frame for shift in range(8 - width, -1, -width)]
    for value in (values + [0] * max(0, beats - len(values)))[:beats]:
        # OUT on low, delayed JMP on high; x counter supplies exactly beats iterations.
        physical.extend([(False, value, False)] * half)
        physical.extend([(True, value, False)] * half)
    physical.extend([(False, 0, True)] * 16)
    return physical


def check_gate(code):
    """Decode actual gate words with delayed CPU IRQ service and single credits."""
    for latency in (0, 128, 2560, 16384, 128000):
        pc, tokens, ready, irq, waiting = 0, 1, False, False, False
        cs, end, pending, started, closed = True, None, None, 0, 0
        synchronizer = [True, True]
        for tick in range(700000):
            if end is not None and tick == end:
                cs, end = True, None
            if end is None and started < 3 and ready:
                assert started == closed  # no next CS before prior slot closes
                started += 1
                cs, end = False, tick + 6000
            synced = synchronizer.pop(0)
            synchronizer.append(cs)
            instruction = code[pc]
            ready = bool(instruction & 0x1000)  # side-set also applies while stalled
            op, following = instruction >> 13, (pc + 1) % len(code)
            if op == 4:
                assert instruction & 0xff == 0xa0
                if tokens:
                    tokens -= 1
                else:
                    following = pc
            elif op == 1:
                assert instruction & 31 == 26
                if synced != bool(instruction & 0x80):
                    following = pc
            elif op == 6:
                assert instruction == 0xc020
                if not waiting:
                    assert pending is None
                    irq, waiting, pending = True, True, tick + latency
                if irq:
                    following = pc
                else:
                    waiting = False
            else:
                raise AssertionError(hex(instruction))
            pc = following
            if pending is not None and tick >= pending:
                assert not ready and not tokens and cs
                closed += 1
                tokens = 1
                irq, pending = False, None
            if closed == 3:
                break
        assert closed == 3, latency


def main():
    result = native()
    cases = 0
    for program in result["programs"]:
        width, half = program["width"], program["half"]
        assert program["gate"] == [0x80a0, 0x209a, 0x301a, 0x209a, 0xc020]
        assert program["rx"] == [0x201a, 0x2095, 0x4000 | width, 0x2015, 0x0041, 0x2095, 0xc001, 0x0007]
        # Every bit value, 32-bit boundaries and +/-one-cycle data synchronization skew.
        frame = message(7, pattern_payload(7))
        for skew in (-1, 0, 1):
            received, fill, pc, irq = receive(program["rx"], width, sender(program, frame), skew)
            assert received == frame and fill == 0 and pc == 5 and not irq, (width, half, skew)
            assert zlib.crc32(received) == 0x2144df1c
            cases += 1
        if half in (3, 5, 16):
            total = len(frame) * 8 // width
            for cut in (1, 2, 7, 8, 31, 32, total - 8 // width, total - 1):
                data, fill, pc, irq = receive(program["rx"], width, sender(program, frame, cut))
                assert len(data) < len(frame) and pc < 5 and not irq, (width, half, cut, pc)
                cases += 1
            data, fill, pc, irq = receive(program["rx"], width, sender(program, frame, total + 1))
            assert data == frame and irq and pc == 7, (width, half, pc)
            cases += 1
    check_gate(result["programs"][0]["gate"])
    for fault, got in enumerate(result["consume"]):
        frames = [message(n, pattern_payload(n)) for n in range(fault, 16)]
        expected = dict(ok=16-fault, bad_crc=fault,
                        crc_chain=zlib.crc32(b"".join(m[-4:] for m in frames)),
                        payload_crc=zlib.crc32(b"".join(m[12:-4] for m in frames)))
        assert got == expected
    print(f"pvwide native: PASS (production close/reset/credit/consume; {cases} PIO wire cases; CM5/zlib reference)")


if __name__ == "__main__":
    main()
