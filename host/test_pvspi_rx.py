"""Exercise actual SPI close/IRQ code with delayed DMA retirement and CS boundaries.

Register events are injected by a synthetic microsecond clock. This verifies bounded
admission and fault classification, not PIO timing or the cause of a hardware short.
"""
from pathlib import Path
import subprocess
import tempfile

from test_dual_faults import function_body

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "pvspi.h"
#define PIN_CS 19
#define PIN_READY 22
#define DMA_CH0_CTRL_TRIG_BUSY_BITS 0x04000000u
#define DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS 0x80000000u
enum { RX_DRAIN_US = 3 };
typedef struct { volatile uint32_t transfer_count, ctrl_trig; } dma_channel_hw_t;
pvspi_stats_t pvspi;
static PIO pio;
static unsigned sm_rx;
static int ch_rx;
static bool armed, admitting, locked, ready, cs;
static int8_t slot_crc[PV_SLOTS];
static uint32_t memory[PV_SLOTS * PV_MSG_WORDS], *slot_mem = memory;
static dma_channel_hw_t rx;
static uint32_t now, retire_at, retired_fifo, cs_fall_at, fifo, pc, residue;
static unsigned aborts, resets, rearms, crc_reads, releases;
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
static void gpio_put(unsigned pin, bool value) { assert(locked && pin == PIN_READY); ready = value; }
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
static void pio_interrupt_clear(PIO p, unsigned irq) { (void)p; assert(irq == 0); }
static void *lock;
static uint32_t spin_lock_blocking(void *l) { (void)l; assert(!locked); locked = true; return 0; }
static void spin_unlock(void *l, uint32_t saved) { (void)l; (void)saved; assert(locked); locked = false; }
static void rx_reset(void) { resets++; fifo = 0; }
static void __dmb(void) { releases++; }
'''

CHECKS = r'''
static void init(uint32_t remaining, uint32_t ctrl, uint32_t depth) {
    memset(&pvspi, 0, sizeof pvspi); memset(slot_crc, -1, sizeof slot_crc);
    rx.transfer_count = remaining; rx.ctrl_trig = ctrl;
    fifo = depth; pc = 23; residue = 0x2144df1cu;
    now = retire_at = retired_fifo = cs_fall_at = 0;
    aborts = resets = rearms = crc_reads = releases = 0;
    armed = admitting = cs = ready = true; locked = false;
}
static void accepted(bool deferred) {
    assert(pvspi.head == 1 && slot_crc[0] == 1 && crc_reads == 1 && releases == 1);
    assert(pvspi.deferred_msgs == (unsigned)deferred && !pvspi.short_msgs);
    assert(!pvspi.long_msgs && !pvspi.drain_timeouts && !aborts);
    assert(resets == 1 && rearms == 1 && armed && ready && !locked);
}
int main(void) {
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
    }
    init(0, DMA_CH0_CTRL_TRIG_BUSY_BITS, 0); cs_isr();
    assert(pvspi.short_msgs == 1 && pvspi.drain_timeouts == 1 && !crc_reads);
    assert(pvspi.last_short_ctrl & DMA_CH0_CTRL_TRIG_BUSY_BITS);
    for (unsigned depth = 1; depth <= 8; depth++) {
        init(0, 0, depth); cs_isr();
        assert(pvspi.long_msgs == 1 && !pvspi.short_msgs && !crc_reads && aborts == 1);
    }
    init(1, DMA_CH0_CTRL_TRIG_BUSY_BITS, 2); retire_at = 3; retired_fifo = 1; cs_isr();
    assert(pvspi.long_msgs == 1 && !pvspi.head && !crc_reads && !pvspi.deferred_msgs);
    init(1, DMA_CH0_CTRL_TRIG_BUSY_BITS, 0); now = UINT32_MAX - 1; cs_isr();
    assert(pvspi.short_msgs == 1 && pvspi.drain_timeouts == 1 && now == 2);
    init(0, DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS, 0); cs_isr();
    assert(pvspi.short_msgs == 1 && !pvspi.head && !crc_reads);
    init(0, 0, 0); cs = false; cs_isr();
    assert(pvspi.late_closes == 1 && pvspi.short_msgs == 1 && !pvspi.head && !crc_reads);
    init(1, DMA_CH0_CTRL_TRIG_BUSY_BITS, 1); retire_at = cs_fall_at = 3; cs_isr();
    assert(pvspi.late_closes == 1 && pvspi.short_msgs == 1 && !pvspi.head && !crc_reads);
    // CRC failure still reaches pv_accept as an explicitly bad residue, never as valid data.
    init(0, 0, 0); residue = 0; cs_isr(); assert(pvspi.head == 1 && slot_crc[0] == 0);
    init(0, 0, 0); admitting = false; cs_isr(); assert(!ready && armed);
    init(0, 0, 0); armed = false; cs_isr();
    assert(pvspi.overflows == 1 && !pvspi.head && !crc_reads);
    puts("SPI close: PASS (late retirement, true shorts, long FIFO, BUSY/AHB, late CS, CRC, READY)");
}
'''


def main():
    source = (ROOT / "firmware/src/pvspi.c").read_text()
    functions = [("static uint32_t *", "slot", "uint32_t n"),
                 ("static void", "arm_locked", "void"),
                 ("static bool", "close_rx", "bool late"),
                 ("static void", "cs_isr", "void")]
    code = FIXTURE + "\n".join(f"{result} {name}({args}) {{\n{function_body(source, name)}\n}}"
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
        subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    main()
