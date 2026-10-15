"""Exercise actual SPI close/IRQ code with delayed DMA retirement and CS boundaries.

Register events are injected by a synthetic microsecond clock. The actual CS-gate
opcodes also run in an independent cycle model, including delayed CPU service,
one-token admission, backpressure and pause. Hardware arbitration is not modeled.
"""
from pathlib import Path
import json
import subprocess
import tempfile

from test_dual_faults import function_body

ROOT = Path(__file__).resolve().parents[1]

ENCODERS = r'''
// The native fixture needs only these public PIO encoding operations. Literal
// opcode assertions below independently check the five generated gate words.
typedef unsigned uint;
enum { pio_pins=0, pio_x=1, pio_y=2 };
static uint16_t prog_rx[4], prog_cs[5], prog_emu[10];
static uint8_t len_emu;
static unsigned pio_encode_wait_gpio(bool high,unsigned pin) { return 0x2000u | (high<<7) | pin; }
static unsigned pio_encode_in(unsigned source,unsigned count) { return 0x4000u | source<<5 | (count&31); }
static unsigned pio_encode_out(unsigned dest,unsigned count) { return 0x6000u | dest<<5 | (count&31); }
static unsigned pio_encode_pull(bool empty,bool block) { return 0x8080u | empty<<6 | block<<5; }
static unsigned pio_encode_sideset(unsigned count,unsigned value) { return value<<(13-count); }
static unsigned pio_encode_delay(unsigned cycles) { return cycles<<8; }
static unsigned pio_encode_set(unsigned dest,unsigned value) { return 0xe000u | dest<<5 | value; }
static unsigned pio_encode_mov(unsigned dest,unsigned source) { return 0xa000u | dest<<5 | source; }
static unsigned pio_encode_jmp_x_dec(unsigned target) { return 0x0040u | target; }
static unsigned pio_encode_irq_wait(bool relative,unsigned irq) { return 0xc020u | relative<<4 | irq; }
'''

FIXTURE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "pvspi.h"
#define PIN_CS 19
#define PIN_READY 22
#define PIN_SCK 17
#define DMA_CH0_CTRL_TRIG_BUSY_BITS 0x04000000u
#define DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS 0x80000000u
enum { RX_DRAIN_US = 3 };
typedef struct { volatile uint32_t transfer_count, ctrl_trig; } dma_channel_hw_t;
pvspi_stats_t pvspi;
static PIO pio;
static unsigned sm_rx, sm_cs;
static int ch_rx;
static bool armed, admitting, locked, ready, cs, active, overridden;
static int8_t slot_crc[PV_SLOTS];
static uint32_t memory[PV_SLOTS * PV_MSG_WORDS], *slot_mem = memory;
static dma_channel_hw_t rx;
static uint32_t now, retire_at, retired_fifo, cs_fall_at, fifo, pc, residue;
static unsigned aborts, resets, rearms, crc_reads, releases, credits, clears;
static uint32_t time_us_32(void) {
    now++;
    if (retire_at && now == retire_at) { rx.transfer_count = 0; rx.ctrl_trig = 0; fifo = retired_fifo; }
    if (cs_fall_at && now == cs_fall_at) cs = false;
    return now;
}
static dma_channel_hw_t *dma_channel_hw_addr(int channel) { (void)channel; return &rx; }
static unsigned pio_sm_get_rx_fifo_level(PIO p, unsigned sm) { (void)p; (void)sm; return fifo; }
static unsigned pio_sm_get_pc(PIO p, unsigned sm) { (void)p; (void)sm; return pc; }
static bool gpio_get(unsigned pin) { assert(pin == PIN_CS); return cs; }
enum { GPIO_OVERRIDE_LOW = 2 };
static void gpio_set_outover(unsigned pin, unsigned value) {
    assert(locked && pin == PIN_READY && value == GPIO_OVERRIDE_LOW);
    overridden = true; ready = false;
}
static void dma_channel_abort(int channel) {
    (void)channel; aborts++; rx.transfer_count = rx.ctrl_trig = 0;
}
static void dma_sniffer_set_data_accumulator(uint32_t value) {
    assert(value == 0xffffffffu); residue = 0;
}
static uint32_t dma_sniffer_get_data_accumulator(void) {
    assert(!rx.transfer_count && !(rx.ctrl_trig & DMA_CH0_CTRL_TRIG_BUSY_BITS));
    crc_reads++; return residue;
}
static void dma_channel_transfer_to_buffer_now(int channel, uint32_t *dest, uint32_t count) {
    (void)channel; assert(dest >= memory && dest < memory + PV_SLOTS * PV_MSG_WORDS);
    rearms++; rx.transfer_count = count; rx.ctrl_trig = DMA_CH0_CTRL_TRIG_BUSY_BITS;
}
static void pio_sm_put(PIO p, unsigned sm, uint32_t token) {
    (void)p; assert(locked && sm == sm_cs && !token && armed && admitting);
    assert(resets == 1 && rearms == 1); credits++;
}
static void pio_interrupt_clear(PIO p, unsigned irq) {
    (void)p; assert(irq == 0 && locked && resets == 1);
    assert(!admitting || credits == 1); clears++; ready = admitting && armed;
}
static void *lock;
static uint32_t spin_lock_blocking(void *l) { (void)l; assert(!locked); locked = true; return 0; }
static void spin_unlock(void *l, uint32_t saved) { (void)l; (void)saved; assert(locked); locked = false; }
static void rx_reset(void) { resets++; fifo = 0; }
static void __dmb(void) { releases++; }
static void __dsb(void) { assert(clears == 1); }
'''

CHECKS = r'''
static void init(uint32_t remaining, uint32_t ctrl, uint32_t depth) {
    memset(&pvspi, 0, sizeof pvspi); memset(slot_crc, -1, sizeof slot_crc);
    rx.transfer_count = remaining; rx.ctrl_trig = ctrl;
    fifo = depth; pc = 23; residue = 0x2144df1cu;
    now = retire_at = retired_fifo = cs_fall_at = 0;
    aborts = resets = rearms = crc_reads = releases = credits = clears = 0;
    armed = admitting = cs = active = true; ready = locked = overridden = false;
}
static void accepted(bool deferred) {
    assert(pvspi.head == 1 && slot_crc[0] == 1 && crc_reads == 1 && releases == 2);
    assert(pvspi.deferred_msgs == (unsigned)deferred && !pvspi.short_msgs);
    assert(!pvspi.long_msgs && !pvspi.drain_timeouts && !aborts);
    assert(resets == 1 && rearms == 1 && credits == 1 && clears == 1 && armed && ready && !locked);
    assert(!pvspi.errors);
}
int main(void) {
    build(2);
    init(0, 0, 0); cs_isr(); accepted(false); assert(now == 0);
    // The count may still include a FIFO word, or may already be zero while its write retires.
    for (unsigned pending = 0; pending < 2; pending++) {
        init(pending, DMA_CH0_CTRL_TRIG_BUSY_BITS, pending);
        retire_at = 3; cs_isr(); accepted(true);
    }
    // A genuinely truncated transfer cannot become valid just by waiting.
    for (unsigned remaining = 1; remaining <= PV_MSG_WORDS; remaining++) {
        init(remaining, DMA_CH0_CTRL_TRIG_BUSY_BITS, 0); cs_isr();
        assert(!pvspi.head && !crc_reads && pvspi.short_msgs == 1 && aborts == 1);
        assert(pvspi.drain_timeouts == 1 && now == RX_DRAIN_US + 1);
        assert(pvspi.last_short_words == PV_MSG_WORDS - remaining);
        assert(!pvspi.last_short_fifo && pvspi.last_short_pc == pc && pvspi.last_short_cs);
        assert(resets == 1 && rearms == 1 && armed && ready);
        assert(pvspi.errors == PV_SPI_ERR_SHORT);
    }
    init(0, DMA_CH0_CTRL_TRIG_BUSY_BITS, 0); cs_isr();
    assert(pvspi.short_msgs == 1 && pvspi.drain_timeouts == 1 && !crc_reads);
    assert(pvspi.last_short_ctrl & DMA_CH0_CTRL_TRIG_BUSY_BITS);
    for (unsigned depth = 1; depth <= 8; depth++) {
        init(0, 0, depth); cs_isr();
        assert(pvspi.long_msgs == 1 && !pvspi.short_msgs && !crc_reads && aborts == 1);
        assert(pvspi.errors == PV_SPI_ERR_LONG);
    }
    init(1, DMA_CH0_CTRL_TRIG_BUSY_BITS, 2); retire_at = 3; retired_fifo = 1; cs_isr();
    assert(pvspi.long_msgs == 1 && !pvspi.head && !crc_reads && !pvspi.deferred_msgs);
    init(1, DMA_CH0_CTRL_TRIG_BUSY_BITS, 0); now = UINT32_MAX - 1; cs_isr();
    assert(pvspi.short_msgs == 1 && pvspi.drain_timeouts == 1 && now == 2);
    init(0, DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS, 0); cs_isr();
    assert(pvspi.short_msgs == 1 && !pvspi.head && !crc_reads);
    assert(pvspi.errors == (PV_SPI_ERR_SHORT | PV_SPI_ERR_DMA));
    init(0, 0, 0); cs = false; cs_isr();
    assert(pvspi.late_closes == 1 && pvspi.short_msgs == 1 && !pvspi.head && !crc_reads);
    assert(pvspi.errors == (PV_SPI_ERR_SHORT | PV_SPI_ERR_LATE));
    init(1, DMA_CH0_CTRL_TRIG_BUSY_BITS, 1); retire_at = cs_fall_at = 3; cs_isr();
    assert(pvspi.late_closes == 1 && pvspi.short_msgs == 1 && !pvspi.head && !crc_reads);
    // CRC failure still reaches pv_accept as an explicitly bad residue, never as valid data.
    init(0, 0, 0); residue = 0; cs_isr(); assert(pvspi.head == 1 && slot_crc[0] == 0);
    init(0, 0, 0); ready = true; pvspi_pause();
    assert(!ready && overridden && !admitting && armed && !locked);
    cs_isr(); assert(!ready && !armed && !credits && !rearms);
    init(0, 0, 0); armed = false; cs_isr();
    assert(pvspi.overflows == 1 && !pvspi.head && !crc_reads);
    assert(pvspi.errors == PV_SPI_ERR_OVERFLOW);
    // An already armed transfer cannot receive a duplicate credit from a consumer.
    init(0, 0, 0); locked = true; arm_locked(); arm_locked();
    assert(!credits && !rearms); locked = false;
    puts("SPI close: PASS (late retirement, true shorts, long FIFO, BUSY/AHB, late CS, CRC, READY)");
    printf("{\"gate\":[%u,%u,%u,%u,%u],\"emu\":[",prog_cs[0],prog_cs[1],prog_cs[2],prog_cs[3],prog_cs[4]);
    for(unsigned i=0;i<len_emu;i++) printf("%s%u",i?",":"",prog_emu[i]);
    puts("]}");
}
'''


class Gate:
    """Independent PIO decoder: mandatory one-bit side-set, PULL/WAIT/IRQ."""
    def __init__(self, code, hardware=True):
        self.code, self.hardware = code, hardware
        self.pc = self.tokens = 0
        self.ready = not hardware
        self.irq = self.waiting = self.override_low = False

    @property
    def pin(self):
        return self.ready and not self.override_low

    def step(self, cs):
        instruction = self.code[self.pc]
        if self.hardware:
            # Side-set executes on the first cycle, including stalled instructions.
            self.ready = bool(instruction & 0x1000)
        opcode = instruction >> 13
        if opcode == 4:  # unconditional PULL BLOCK
            assert instruction & 0xff == 0xa0
            if not self.tokens:
                return
            self.tokens -= 1
        elif opcode == 1:  # WAIT GPIO
            assert instruction & 31 == 19 and (instruction >> 5) & 3 == 0
            if bool(instruction & 0x80) != cs:
                return
        elif opcode == 6:  # IRQ0, optional WAIT
            assert instruction & 0x1f == 0
            if instruction & 0x20:
                if not self.waiting:
                    self.irq = self.waiting = True
                if self.irq:
                    return
                self.waiting = False
            else:
                self.irq = True
        else:
            raise AssertionError(hex(instruction))
        self.pc = (self.pc + 1) % len(self.code)


def gate_stress(programs, latency, *, legacy=False):
    code = [0x2013, 0x2093, 0xc000] if legacy else programs["gate"]
    gate = Gate(code, hardware=not legacy)
    gate.tokens = 1
    # Actual generated master: MOV X/Y then10,656 OUT/JMP bit pairs. Its CS-high
    # tail is SET X31,32 JMP[15],SET X31,32 JMP[15],WAIT READY before SET CS0.
    emu = programs["emu"]
    bit_cycles = 2 + ((emu[3] >> 8) & 15) + ((emu[4] >> 8) & 15)
    message_cycles = 2 + 1332 * 8 * bit_cycles
    gap = 1 + 32 * (1 + ((emu[7] >> 8) & 15)) + 1 + 32 * (1 + ((emu[9] >> 8) & 15)) + 1
    assert bit_cycles == 4 and gap == 1027
    cs, synchronizer, end, earliest = True, [True, True], None, 0
    pending, started, closed, unclosed, overlaps, late = None, 0, 0, False, 0, 0
    for tick in range(8 * (message_cycles + gap + latency + 16)):
        if end is not None and tick == end:
            cs, end, earliest = True, None, tick + gap
        if end is None and started < 6 and tick >= earliest and gate.pin:
            overlaps += unclosed
            unclosed, cs, end = True, False, tick + message_cycles
            started += 1
        previous_irq = gate.irq
        gate.step(synchronizer.pop(0))
        synchronizer.append(cs)
        if gate.irq and not previous_irq and pending is None:
            pending = tick + latency
        if pending is not None and tick >= pending:
            late += not cs
            unclosed = False
            # CPU has retired RX, reset its bit history and armed its next buffer.
            if legacy:
                gate.ready = True
            else:
                assert gate.tokens == 0 and not gate.pin
                gate.tokens += 1
            gate.irq, pending = False, None
            closed += 1
        if closed == 6:
            return overlaps, late
    raise AssertionError(("gate did not complete", latency, started, closed))


def check_gate(programs):
    assert programs["gate"] == [0x80a0, 0x2093, 0x3013, 0x2093, 0xc020]
    for latency in (0, 128, 2560, 16384, 128000):  # 0/1/20/128/1000us at128MHz
        assert gate_stress(programs, latency) == (0, 0), latency
    assert gate_stress(programs, 2560, legacy=True)[0] > 0
    assert gate_stress(programs, 2560, legacy=True)[1] > 0
    gate = Gate(programs["gate"])
    for _ in range(100):
        gate.step(True)
        assert not gate.pin  # no room/credit: indefinite safe backpressure
    gate.tokens = 1
    for _ in range(4):
        gate.step(True)
    assert gate.pin
    gate.step(False)
    gate.step(False)
    assert not gate.pin  # CS fall consumes the grant before the end IRQ
    for _ in range(10):
        gate.step(True)
    assert gate.irq and not gate.pin
    gate.irq = False
    for _ in range(100):
        gate.step(True)
        assert not gate.pin  # CPU closed RX but no new buffer/room token
    # Pause forces OUTOVER low even with a token queued or READY already high.
    for phase in range(6):
        gate = Gate(programs["gate"])
        gate.tokens = 1
        for _ in range(phase):
            gate.step(True)
        gate.override_low = True
        for _ in range(100):
            gate.step(True)
            assert not gate.pin
        gate.tokens = 0  # stop disables the SM and clears the token FIFO
        gate = Gate(programs["gate"])  # fresh initialized side0/PULL state
        gate.step(True)
        assert not gate.pin and not gate.tokens
    print("SPI gate: PASS (actual5 opcodes; IRQ delay0..1000us; old20us overlap; credit/pause)")


def main():
    source = (ROOT / "firmware/src/pvspi.c").read_text()
    functions = [("static void", "build", "int emu_half"),
                 ("static uint32_t *", "slot", "uint32_t n"),
                 ("static void", "arm_locked", "void"),
                 ("static bool", "close_rx", "bool late"),
                 ("static void", "cs_isr", "void"),
                 ("void", "pvspi_pause", "void")]
    code = FIXTURE + ENCODERS + "\n".join(f"{result} {name}({args}) {{\n{function_body(source, name)}\n}}"
                                for result, name, args in functions) + CHECKS
    with tempfile.TemporaryDirectory(prefix="pvspi-close-") as directory:
        directory = Path(directory)
        (directory / "hardware").mkdir()
        (directory / "hardware/pio.h").write_text("typedef struct pio_hw *PIO;\n")
        test, executable = directory / "close.c", directory / "close"
        test.write_text(code)
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        f"-I{directory}", f"-I{ROOT / 'firmware/src'}", str(test),
                        "-o", str(executable)], check=True)
        result = subprocess.run([str(executable)], check=True, capture_output=True, text=True)
        lines = result.stdout.splitlines()
        print(lines[0])
        check_gate(json.loads(lines[1]))


if __name__ == "__main__":
    main()
