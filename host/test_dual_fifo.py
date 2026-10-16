"""Compile actual DMA source setup/cursor functions against native register shims.

Checks configuration and ownership validation, descriptor regressions, fixed RX
sources, independent DREQs and byte-swap. The shims do not establish arbitration,
PIO/DMA latency, electrical timing or throughput; those need the device bench.
"""
from pathlib import Path
import re
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
#include "dual_dma.h"
typedef unsigned uint;
#define DMA_SIZE_32 2
#define DREQ_FORCE 63
#define DMA_CH0_CTRL_TRIG_INCR_READ_REV_BITS 0x20u
#define clk_sys 0
typedef struct { volatile uint32_t txf[4], rxf[4]; bool claimed[4]; unsigned index; } pio_t;
typedef pio_t *PIO;
static pio_t ports[3];
static PIO pio0 = &ports[0], pio1 = &ports[1], pio2 = &ports[2];
static struct { struct { uintptr_t read_addr; uint32_t al3_read_addr_trig, al2_write_addr_trig; } ch[16]; } dma;
static __typeof__(dma) *dma_hw = &dma;
typedef struct {
    uint32_t ctrl;
    unsigned size, dreq, chain, ring_bits;
    bool read_inc, write_inc, ring_write, quiet, high, bswap;
} dma_channel_config;
static struct {
    dma_channel_config config;
    const volatile void *read;
    volatile void *write;
    uint32_t count;
    unsigned configurations;
} channels[16];
static dma_channel_config dma_channel_get_default_config(unsigned ch) {
    return (dma_channel_config){.dreq = DREQ_FORCE, .chain = ch};
}
static void channel_config_set_transfer_data_size(dma_channel_config *c, unsigned n) { c->size = n; }
static void channel_config_set_read_increment(dma_channel_config *c, bool b) { c->read_inc = b; }
static void channel_config_set_write_increment(dma_channel_config *c, bool b) { c->write_inc = b; }
static void channel_config_set_dreq(dma_channel_config *c, unsigned n) { c->dreq = n; }
static void channel_config_set_chain_to(dma_channel_config *c, unsigned n) { c->chain = n; }
static void channel_config_set_irq_quiet(dma_channel_config *c, bool b) { c->quiet = b; }
static void channel_config_set_high_priority(dma_channel_config *c, bool b) { c->high = b; }
static void channel_config_set_bswap(dma_channel_config *c, bool b) { c->bswap = b; }
static void channel_config_set_ring(dma_channel_config *c, bool w, unsigned n) {
    c->ring_write = w; c->ring_bits = n;
}
static void dma_channel_configure(unsigned ch, const dma_channel_config *c,
                                  volatile void *write, const volatile void *read,
                                  uint32_t count, bool trigger) {
    assert(!trigger && ch < 16);
    channels[ch].config = *c; channels[ch].write = write; channels[ch].read = read;
    channels[ch].count = count; channels[ch].configurations++;
}
static unsigned pio_get_dreq(PIO p, unsigned sm, bool tx) { return 8 * p->index + sm + (tx ? 0 : 4); }
static bool pio_sm_is_claimed(PIO p, unsigned sm) { assert(sm < 4); return p->claimed[sm]; }
static uint32_t clock_get_hz(unsigned clk) { assert(clk == clk_sys); return 128000000; }
static uint32_t now, tick;
static uint32_t time_us_32(void) { uint32_t result = now; now += tick; return result; }
static struct {
    PIO output_pio;
    int sm[4], control[4], data[4], sm_cap, cap_dma, clock_sm;
    bool owned, fifo_source, control_bswap;
#ifdef IQ4HIST_EXPERIMENT
    bool shared_axis_row, quad_rows, quad_fifo;
    unsigned sample_bits;
    const uint32_t *quad_desc[4];
    const volatile uint32_t *quad_rx[4];
    unsigned quad_dreq[4];
#endif
    uint32_t fifo_targets[2][2] __attribute__((aligned(8)));
    uint32_t count, mask, divider256, schedule, row_words, writers, sys_hz;
    uint32_t last[4], last_poll_us, poll_limit_us;
    uint64_t rows[4];
    const uint32_t *desc_i, *desc_q;
    const volatile uint32_t *rx_i, *rx_q;
    unsigned rx_dreq_i, rx_dreq_q;
    dual_dma_progress_t result;
} state;
static const char *last_error;
static unsigned selected_schedule, selected_row_words = 2, selected_writers = 2;
#ifdef IQ4HIST_EXPERIMENT
static unsigned selected_sample_bits = 14, selected_dac_clock = 1;
#endif
static unsigned starts, stopped, divider_arg, log_arg;
static bool start_failed(const char *message, uint32_t error) {
    last_error = message; state.result.errors |= error; state.owned = false; stopped++; return false;
}
static bool start_output(unsigned divider, unsigned log) {
    starts++; divider_arg = divider; log_arg = log; return true;
}
'''

CHECKS = r'''
static void reset(void) {
    memset(&state, 0, sizeof state); memset(&dma, 0, sizeof dma);
    memset(channels, 0, sizeof channels); memset(ports, 0, sizeof ports);
    for (unsigned p = 0; p < 3; p++) ports[p].index = p;
    selected_writers = 2; selected_row_words = 8; selected_schedule = 0;
#ifdef IQ4HIST_EXPERIMENT
    selected_sample_bits = 14; selected_dac_clock = 1;
#endif
    starts = stopped = now = tick = 0; last_error = "";
}
static void config_checks(void) {
    for (unsigned writers = 2; writers <= 4; writers += 2)
    for (unsigned words = 2; words <= 8; words *= 2)
    for (unsigned schedule = 0; schedule < 4; schedule++)
    for (unsigned log = 5; log <= 13; log++) {
        reset(); state.output_pio = pio2; state.writers = writers;
        state.row_words = words; state.schedule = schedule;
        const uint32_t *desc = (const uint32_t *)(uintptr_t)0x20010000u;
        axis_dma(3, 9, 2, desc, log + 2);
        dma_channel_config c = channels[3].config, d = channels[9].config;
        assert(channels[3].read == desc && channels[3].write == &dma.ch[9].al3_read_addr_trig);
        assert(channels[3].count == 1 && c.size == DMA_SIZE_32 && !c.write_inc);
        assert(c.read_inc == (writers == 2));
        assert(!!(c.ctrl & DMA_CH0_CTRL_TRIG_INCR_READ_REV_BITS) == (writers == 4));
        assert(c.ring_bits == log + 2 && !c.ring_write && c.dreq == DREQ_FORCE && c.chain == 3);
        assert(c.quiet && c.high == (schedule != 2) && !c.bswap);
        assert(channels[9].read == NULL && channels[9].write == &pio2->txf[2]);
        assert(channels[9].count == words && d.size == DMA_SIZE_32 && d.read_inc && !d.write_inc);
        assert(d.dreq == 18 && d.chain == 3 && d.quiet && d.high == (schedule != 1));
        assert(!d.ring_bits && !d.bswap);
    }
    for (unsigned swap = 0; swap < 2; swap++)
    for (unsigned schedule = 0; schedule < 4; schedule++)
    for (unsigned p = 0; p < 3; p++)
    for (unsigned sm = 0; sm < 4; sm++) {
        reset(); state.output_pio = pio0; state.row_words = 8;
        state.schedule = schedule; state.control_bswap = swap;
        const volatile uint32_t *rx = &ports[p].rxf[sm];
        unsigned dreq = 8 * p + sm + 4;
        axis_fifo_dma(1, 8, 3, rx, dreq);
        dma_channel_config c = channels[1].config, d = channels[8].config;
        assert(channels[1].read == rx && channels[1].write == &dma.ch[8].al3_read_addr_trig);
        assert(channels[1].count == 1 && c.size == DMA_SIZE_32);
        assert(!c.read_inc && !c.write_inc && !c.ctrl && !c.ring_bits && !c.ring_write);
        assert(c.dreq == dreq && c.chain == 1 && c.quiet && c.bswap == !!swap);
        assert(c.high == (schedule != 2));
        assert(channels[8].count == 8 && channels[8].write == &pio0->txf[3]);
        assert(d.dreq == 3 && d.chain == 1 && d.read_inc && !d.write_inc && d.quiet);
        assert(d.high == (schedule != 1) && !d.bswap);
    }
    for (unsigned swap = 0; swap < 2; swap++)
    for (unsigned schedule = 0; schedule < 4; schedule++)
    for (unsigned p = 0; p < 3; p++)
    for (unsigned sm = 0; sm < 4; sm++) {
        reset(); state.output_pio = pio2; state.row_words = 8; state.writers = 4;
        state.schedule = schedule; state.control_bswap = swap;
        state.rx_i = &ports[p].rxf[sm]; state.rx_q = &ports[p].rxf[(sm + 1) % 4];
        state.rx_dreq_i = 8 * p + sm + 4; state.rx_dreq_q = 8 * p + (sm + 1) % 4 + 4;
        for (unsigned s = 0; s < 4; s++) {
            state.control[s] = s; state.data[s] = s + 4; state.sm[s] = s;
        }
        axis_alternating_fifo_dma(0); axis_alternating_fifo_dma(1);
        for (unsigned axis = 0; axis < 2; axis++) {
            const unsigned control = axis, router = axis + 2, even = axis + 4, odd = axis + 6;
            dma_channel_config c = channels[control].config, r = channels[router].config;
            assert(channels[control].read == (axis ? state.rx_q : state.rx_i));
            assert(channels[control].write == &dma.ch[even].al3_read_addr_trig);
            assert(channels[control].count == 1 && c.size == DMA_SIZE_32);
            assert(!c.read_inc && !c.write_inc && !c.ctrl && !c.ring_bits && !c.ring_write);
            assert(c.dreq == (axis ? state.rx_dreq_q : state.rx_dreq_i));
            assert(c.chain == control && c.quiet && c.bswap == !!swap && c.high == (schedule != 2));
            assert(!((uintptr_t)state.fifo_targets[axis] & 7));
            assert(state.fifo_targets[axis][0] == (uint32_t)(uintptr_t)&dma.ch[odd].al3_read_addr_trig);
            assert(state.fifo_targets[axis][1] == (uint32_t)(uintptr_t)&dma.ch[even].al3_read_addr_trig);
            assert(channels[router].read == state.fifo_targets[axis]);
            assert(channels[router].write == &dma.ch[control].al2_write_addr_trig);
            assert(channels[router].count == 1 && r.size == DMA_SIZE_32);
            assert(r.read_inc && !r.write_inc && r.ring_bits == 3 && !r.ring_write && !r.ctrl);
            assert(r.dreq == DREQ_FORCE && r.chain == router && r.quiet && !r.bswap);
            assert(r.high == (schedule != 2));
            unsigned consumers = 0;
            for (unsigned ch = 0; ch < 8; ch++) consumers += channels[ch].config.dreq == c.dreq;
            assert(consumers == 1);
            for (unsigned parity = 0; parity < 2; parity++) {
                unsigned data = parity ? odd : even, writer = axis + 2 * parity;
                dma_channel_config d = channels[data].config;
                assert(channels[data].read == NULL && channels[data].write == &pio2->txf[writer]);
                assert(channels[data].count == 8 && d.size == DMA_SIZE_32);
                assert(d.dreq == 16 + writer && d.chain == router && d.read_inc && !d.write_inc);
                assert(!d.ctrl && !d.ring_bits && !d.ring_write && d.quiet && !d.bswap);
                assert(d.high == (schedule != 1));
            }
        }
    }
}
static void validation_checks(void) {
    for (unsigned claimed = 0; claimed < 2; claimed++)
    for (unsigned p = 0; p < 3; p++)
    for (unsigned sm = 0; sm < 4; sm++)
    for (unsigned dreq = 0; dreq < 64; dreq++) {
        reset(); ports[p].claimed[sm] = claimed;
        assert(valid_rx_source(&ports[p].rxf[sm], dreq) ==
                (claimed && dreq == 8 * p + sm + 4));
        assert(!valid_rx_source(&ports[p].txf[sm], dreq));
    }
    assert(!valid_rx_source(NULL, DREQ_FORCE));
    for (unsigned writers = 2; writers <= 4; writers += 2)
    for (unsigned a = 0; a < 12; a++)
    for (unsigned b = 0; b < 12; b++) {
        reset(); selected_writers = writers;
        unsigned ap = a / 4, as = a % 4, bp = b / 4, bs = b % 4;
        ports[ap].claimed[as] = ports[bp].claimed[bs] = true;
        bool ok = dual_dma_start_fifo(&ports[ap].rxf[as], &ports[bp].rxf[bs],
                                      8 * ap + as + 4, 8 * bp + bs + 4, true, 1);
        assert(ok == (a != b));
        if (ok) {
            assert(starts == 1 && !stopped && state.owned && state.fifo_source);
            assert(state.output_pio == (writers == 4 ? pio2 : pio0));
            assert(state.writers == writers && state.row_words == 8);
            assert(state.control_bswap && !state.desc_i && !state.desc_q);
            assert(!state.count && !state.mask && !state.poll_limit_us && !log_arg && divider_arg == 256);
            assert(state.rx_i == &ports[ap].rxf[as] && state.rx_q == &ports[bp].rxf[bs]);
        } else assert(!starts && stopped && state.result.errors == DUAL_DMA_ERR_CONFIG);
    }
    for (unsigned writers = 2; writers <= 4; writers += 2)
    for (unsigned divider = 0; divider <= 1280; divider++) {
        reset(); selected_writers = writers; pio1->claimed[0] = pio1->claimed[1] = true;
        bool ok = dual_dma_start_fifo_div256(&pio1->rxf[0], &pio1->rxf[1], 12, 13, false, divider);
        bool want = divider == 256 || divider == 384 || divider == 512 || divider == 768 || divider == 1024;
        assert(ok == want && starts == (unsigned)want);
    }
    for (unsigned bad = 0; bad < 8; bad++) {
        reset(); pio1->claimed[0] = pio1->claimed[1] = true;
        if (bad == 0) selected_writers = 3;
        if (bad == 1) selected_row_words = 4;
        if (bad == 2) pio1->claimed[1] = false;
        bool ok = dual_dma_start_fifo(bad == 3 ? NULL : &pio1->rxf[0],
                &pio1->rxf[1], bad == 4 ? 8 : 12, bad == 5 ? 12 : 13,
                false, bad == 6 ? 0 : bad == 7 ? 5 : 4);
        assert(!ok && !starts && stopped == 1 && !state.owned);
    }
    reset(); state.owned = true;
    assert(!dual_dma_start_fifo(&pio1->rxf[0], &pio1->rxf[1], 12, 13, false, 1));
    assert(state.owned && !starts && !stopped);
    // Descriptor validation/setup remains independently exercised at all supported sizes.
    for (unsigned writers = 2; writers <= 4; writers += 2)
    for (unsigned words = 2; words <= 8; words *= 2)
    for (unsigned div_index = 0; div_index < 7; div_index++)
    for (unsigned log = 5; log <= 13; log++) {
        const unsigned dividers[] = {256,384,512,768,1024,1536,2048};
        const unsigned divider = dividers[div_index];
        reset(); selected_writers = writers; selected_row_words = words;
        const uint32_t *a = (const uint32_t *)(uintptr_t)0x20010000u;
        const uint32_t *b = (const uint32_t *)(uintptr_t)0x20020000u;
        bool want = writers == 2 || words >= 4;
        assert(dual_dma_start_div256(a, b, log, divider) == want);
        if (want) {
            assert(!state.fifo_source && state.count == (1u << log));
            assert(state.mask == (1u << log) / (writers / 2) - 1);
            assert(state.output_pio == (writers == 4 ? pio2 : pio0));
            assert(state.desc_i == a && state.desc_q == b && log_arg == log && divider_arg == divider);
            uint32_t period = (uint32_t)((uint64_t)((1u << log) - 8) * 4 * words * divider * 1000000u /
                                       ((uint64_t)128000000 * 256));
            assert(state.poll_limit_us == period && period > 0);
        }
    }
    for (unsigned bad = 0; bad < 9; bad++) {
        reset(); const uint32_t *a = (const uint32_t *)(uintptr_t)(0x20010000u + (bad == 0 ? 4 : 0));
        assert(!dual_dma_start_div256(bad == 1 ? NULL : a,
                (const uint32_t *)(uintptr_t)0x20020000u, bad == 2 ? 4 : bad == 3 ? 14 : 8,
                bad == 4 ? 257 : bad == 5 ? 0 : bad == 6 ? 1280 : bad == 7 ? 1792 : bad == 8 ? 4096 : 256));
        assert(!starts && stopped == 1 && !state.owned);
    }
}
static uint32_t random32(uint32_t *s) { *s ^= *s << 13; *s ^= *s >> 17; *s ^= *s << 5; return *s; }
static void cursor_checks(void) {
    for (unsigned writers = 2; writers <= 4; writers += 2)
    for (unsigned log = 5; log <= 13; log++) {
        reset(); state.writers = writers; state.mask = (1u << log) / (writers / 2) - 1;
        state.desc_i = (const uint32_t *)(uintptr_t)0x20010000u;
        state.desc_q = (const uint32_t *)(uintptr_t)0x20020000u;
        state.poll_limit_us = 1000; tick = 1;
        uint64_t expected[4] = {0}; uint32_t rng = 0x8749ac1f;
        for (unsigned s = 0; s < writers; s++) state.control[s] = s;
        for (unsigned step = 0; step < 2000; step++) {
            for (unsigned s = 0; s < writers; s++) {
                expected[s] += random32(&rng) & (state.mask >> 2);
                const uint32_t *base = (s & 1 ? state.desc_q : state.desc_i) + (s >> 1);
                dma.ch[s].read_addr = (uintptr_t)base + ((expected[s] & state.mask) << (writers == 4 ? 3 : 2));
            }
            sample_reads(true);
            for (unsigned s = 0; s < writers; s++) assert(state.rows[s] == expected[s]);
            assert(!state.result.errors);
            for (unsigned axis = 0; axis < 2; axis++) {
                uint64_t lo = expected[axis], hi = lo;
                if (writers == 4) {
                    uint64_t e = 2 * expected[axis], o = 2 * expected[axis + 2];
                    lo = e < o + 1 ? e : o + 1;
                    hi = (e ? e - 1 : 0) > o ? (e ? e - 1 : 0) : o;
                }
                assert((axis ? state.result.read_q : state.result.read_i) == lo);
                assert((axis ? state.result.issued_q : state.result.issued_i) == hi);
            }
        }
        now = state.last_poll_us + state.poll_limit_us; sample_reads(true);
        assert(state.result.errors == DUAL_DMA_ERR_POLL_GAP);
    }
}
#ifdef IQ4HIST_EXPERIMENT
static void quad_checks(void) {
    const uint32_t *desc[4];
    for (unsigned s = 0; s < 4; s++) desc[s] = (const uint32_t *)(uintptr_t)(0x20000000u + s * 32768u);
    for (unsigned bits = 8; bits <= 14; bits += 6)
    for (unsigned log = 5; log <= 13; log++)
    for (unsigned cpw = 0; cpw <= 5; cpw++)
    for (unsigned schedule = 0; schedule < 4; schedule++) {
        reset(); selected_writers = 4; selected_row_words = bits == 8 ? 1 : 2;
        selected_sample_bits = bits; selected_schedule = schedule;
        bool want = cpw == 1 || cpw == 2 || cpw == 4;
        assert(dual_dma_start_quad_rows(desc, log, cpw) == want);
        if (!want) { assert(stopped == 1 && !starts && !state.owned); continue; }
        assert(starts == 1 && !stopped && state.owned && state.quad_rows && state.shared_axis_row);
        assert(!state.fifo_source && state.sample_bits == bits && state.writers == 4);
        assert(state.output_pio == pio2 && state.count == (1u << log));
        assert(!state.mask && !state.poll_limit_us && !state.desc_i && !state.desc_q);
        assert(divider_arg == cpw * 256 && log_arg == log);
        assert(state.schedule == schedule && state.sys_hz == 128000000);
        for (unsigned s = 0; s < 4; s++) {
            assert(state.quad_desc[s] == desc[s]);
            axis_dma(s, s + 4, s, state.quad_desc[s], log + 2);
            dma_channel_config c = channels[s].config, d = channels[s + 4].config;
            assert(channels[s].read == desc[s] && channels[s].write == &dma.ch[s + 4].al3_read_addr_trig);
            assert(c.read_inc && !c.ctrl && !c.write_inc && !c.ring_write && c.ring_bits == log + 2);
            assert(c.dreq == DREQ_FORCE && c.chain == s && c.quiet && !c.bswap);
            assert(channels[s].count == 1 && channels[s + 4].count == (bits == 8 ? 1u : 2u));
            assert(channels[s + 4].write == &pio2->txf[s]);
            assert(d.read_inc && !d.write_inc && d.dreq == 16 + s && d.chain == s && d.quiet);
        }
        // Each naturally aligned contiguous control ring wraps independently; no
        // stride2 interpretation or shared pointer-RX peripheral DREQ is involved.
        for (unsigned s = 0; s < 4; s++) {
            uintptr_t base = (uintptr_t)desc[s], mask = ((uintptr_t)1 << (log + 2)) - 1;
            for (unsigned n = 0; n < (3u << log); n++)
                assert((base | ((4u * n) & mask)) == base + 4u * (n & ((1u << log) - 1)));
        }
    }
    for (unsigned bad = 0; bad < 12; bad++) {
        reset(); selected_writers = 4; selected_row_words = 1; selected_sample_bits = 8;
        const uint32_t *bad_desc[4]; memcpy(bad_desc, desc, sizeof bad_desc);
        if (bad == 0) selected_writers = 2;
        if (bad == 1) selected_row_words = 2;
        if (bad == 2) selected_dac_clock = 0;
        if (bad == 3) bad_desc[0] = NULL;
        if (bad == 4) bad_desc[1]++;
        if (bad == 5) bad_desc[3] = bad_desc[1];
        if (bad == 6) bad_desc[3] = NULL;
        if (bad == 7) selected_sample_bits = 14;
        assert(!dual_dma_start_quad_rows(bad == 8 ? NULL : bad_desc,
                                         bad == 9 ? 4 : bad == 10 ? 14 : 11, bad == 11 ? 3 : 1));
        assert(stopped == 1 && !starts && !state.owned && state.result.errors == DUAL_DMA_ERR_CONFIG);
    }
    reset(); state.owned = true;
    assert(!dual_dma_start_quad_rows(desc, 11, 1));
    assert(state.owned && !starts && !stopped);
}
static void quad_fifo_checks(void) {
    for (unsigned p = 0; p < 3; p++)
    for (unsigned rotation = 0; rotation < 4; rotation++)
    for (unsigned cpw = 0; cpw <= 5; cpw++)
    for (unsigned schedule = 0; schedule < 4; schedule++) {
        reset(); selected_writers = 4; selected_row_words = 1; selected_sample_bits = 8;
        selected_schedule = schedule;
        const volatile uint32_t *rx[4]; unsigned dreq[4];
        for (unsigned s = 0; s < 4; s++) {
            const unsigned sm = (s + rotation) % 4;
            ports[p].claimed[sm] = true;
            rx[s] = &ports[p].rxf[sm]; dreq[s] = pio_get_dreq(&ports[p], sm, false);
        }
        const bool want = cpw == 1 || cpw == 2 || cpw == 4;
        assert(dual_dma_start_quad_fifo(rx, dreq, cpw) == want);
        if (!want) { assert(stopped == 1 && !starts && !state.owned); continue; }
        assert(starts == 1 && !stopped && state.owned && state.fifo_source && state.quad_fifo);
        assert(state.quad_rows && state.shared_axis_row && state.sample_bits == 8 && state.writers == 4);
        assert(state.output_pio == pio2 && state.row_words == 1 && state.schedule == schedule);
        assert(!state.control_bswap && !state.count && !state.mask && !state.poll_limit_us);
        assert(!state.desc_i && !state.desc_q && !log_arg && divider_arg == cpw * 256);
        for (unsigned s = 0; s < 4; s++) {
            state.control[s] = s; state.data[s] = s + 4; state.sm[s] = s;
            assert(state.quad_rx[s] == rx[s] && state.quad_dreq[s] == dreq[s]);
        }
        // Run the actual start_output dispatch/start-mask loop. All four RX
        // controls start; no alternating router DMA is configured or started.
        assert(output_controls(0) == 15);
        for (unsigned s = 0; s < 4; s++) {
            const dma_channel_config c = channels[s].config, d = channels[s + 4].config;
            assert(channels[s].configurations == 1 && channels[s + 4].configurations == 1);
            assert(channels[s].read == rx[s] && channels[s].write == &dma.ch[s + 4].al3_read_addr_trig);
            assert(channels[s].count == 1 && c.size == DMA_SIZE_32 && c.dreq == dreq[s]);
            assert(!c.read_inc && !c.write_inc && !c.ring_bits && !c.ring_write && !c.ctrl && !c.bswap);
            assert(c.quiet && c.chain == s && c.high == (schedule != 2));
            assert(channels[s + 4].read == NULL && channels[s + 4].write == &pio2->txf[s]);
            assert(channels[s + 4].count == 1 && d.size == DMA_SIZE_32 && d.dreq == 16 + s);
            assert(d.read_inc && !d.write_inc && !d.ring_bits && !d.ring_write && !d.ctrl && !d.bswap);
            assert(d.quiet && d.chain == s && d.high == (schedule != 1));
            unsigned consumers = 0;
            for (unsigned ch = 0; ch < 8; ch++) consumers += channels[ch].config.dreq == dreq[s];
            assert(consumers == 1);
        }
        for (unsigned ch = 8; ch < 16; ch++) assert(!channels[ch].configurations);
    }
    for (unsigned slot = 0; slot < 4; slot++)
    for (unsigned bad = 0; bad < 17; bad++) {
        reset(); selected_writers = 4; selected_row_words = 1; selected_sample_bits = 8;
        const volatile uint32_t *rx[4]; unsigned dreq[4];
        for (unsigned s = 0; s < 4; s++) {
            pio1->claimed[s] = true; rx[s] = &pio1->rxf[s]; dreq[s] = 12 + s;
        }
        const unsigned other = (slot + 1) % 4;
        if (bad == 0) rx[slot] = NULL;
        if (bad == 1) rx[slot] = &pio1->txf[slot];
        if (bad == 2) pio1->claimed[slot] = false;
        if (bad == 3) dreq[slot] = 8 + slot;
        if (bad == 4) rx[slot] = rx[other];
        if (bad == 5) dreq[slot] = dreq[other];
        if (bad == 6) { rx[slot] = rx[other]; dreq[slot] = dreq[other]; }
        if (bad == 7) dreq[slot] = DREQ_FORCE;
        if (bad == 10) selected_writers = 2;
        if (bad == 11) selected_row_words = 2;
        if (bad == 12) selected_sample_bits = 14;
        if (bad == 13) selected_dac_clock = 0;
        assert(!dual_dma_start_quad_fifo(bad == 8 ? NULL : rx, bad == 9 ? NULL : dreq,
                                         bad == 14 ? 0 : bad == 15 ? 3 : bad == 16 ? 5 : 1));
        assert(!starts && stopped == 1 && !state.owned && state.result.errors == DUAL_DMA_ERR_CONFIG);
    }
    reset(); state.owned = true;
    assert(!dual_dma_start_quad_fifo(NULL, NULL, 1));
    assert(state.owned && !starts && !stopped);
}
#endif
int main(void) {
    config_checks(); validation_checks(); cursor_checks();
#ifdef IQ4HIST_EXPERIMENT
    quad_checks(); quad_fifo_checks();
#endif
    puts("dual FIFO setup: PASS (fixed RX+BSWAP, alternating targets/sole RX DREQ, exact claims, old DMA configs/start validation, 36000 wrapped descriptor snapshots)");
}
'''

STOP_FIXTURE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "dual_dma.h"
typedef unsigned uint;
typedef struct { uint32_t input_sync_bypass; bool claimed[4]; } pio_t;
typedef pio_t *PIO;
typedef struct { const uint16_t *instructions; unsigned length; int origin; } pio_program_t;
pio_t output, capture, dacclk;
PIO pio1 = &capture;
struct dma_t {
    struct { uint32_t al1_ctrl, ctrl_trig, transfer_count; } ch[16];
    uint32_t abort;
} dma, *dma_hw = &dma;
struct { struct { uint32_t ctrl; } io[32]; } io_bank, *io_bank0_hw = &io_bank;
struct { uint32_t io[32]; } pads, *pads_bank0_hw = &pads;
struct { uint32_t csr, bit[8], expand_shift, expand_tmds; } hstx, *hstx_ctrl_hw = &hstx;
struct { struct { uint32_t ctrl, div; } clk[1]; } clocks, *clocks_hw = &clocks;
#define DMA_CH0_CTRL_TRIG_CHAIN_TO_LSB 13
#define DMA_CH0_CTRL_TRIG_CHAIN_TO_BITS (15u << 13)
#define DMA_CH0_CTRL_TRIG_EN_BITS 1u
#define DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS (1u << 31)
#define RESETS_RESET_HSTX_BITS 1u
#define clk_hstx 0
#define PIN_D0 0
#define PIN_CLKIO 16
#define PIN_DACCLK 28
static const pio_program_t ddr_clock_program;  // DDR clock fields stay zero/unclaimed here
'''

STOP_STUBS = r'''
static unsigned reads, faults, halted, cleaned, unclaimed, drained, aborts, fifo_level;
static uint32_t owned_dma_mask, cleaned_dma_mask, unclaimed_dma_mask;
static const char *last_error;
static void sample_reads(bool check_time) {
    assert(!state.fifo_source); reads++;
    if (check_time) assert(!state.capture_frozen);
    state.result.read_i++;
}
static void halt_outputs(void) { halted++; state.running = state.result.running = false; }
static void sample_faults(void) {
    faults++;
    if (state.capture_started && !state.capture_frozen)
        state.captured = state.capture_fifo_only ? fifo_level :
            state.capture_requested - dma.ch[state.cap_dma].transfer_count;
}
static void pio_sm_set_enabled(PIO p, unsigned sm, bool enabled) {
    (void)p; (void)sm; assert(!enabled);
}
static void hw_write_masked(uint32_t *reg, uint32_t value, uint32_t mask) {
    *reg = (*reg & ~mask) | (value & mask);
}
static void tight_loop_contents(void) {
    assert(state.capture_frozen && dma.abort == owned_dma_mask);
    for (unsigned ch = 0; ch < 16; ch++) if (owned_dma_mask & (1u << ch)) {
        assert(!(dma.ch[ch].al1_ctrl & DMA_CH0_CTRL_TRIG_EN_BITS));
        assert(((dma.ch[ch].al1_ctrl >> 13) & 15) == ch);
    }
    aborts++; dma.abort = 0;
    for (unsigned ch = 0; ch < 16; ch++) dma.ch[ch].transfer_count = 0;
}
static uint32_t pio_sm_get(PIO p, unsigned sm) {
    (void)p; (void)sm; assert(state.capture_frozen && !state.running); return 0xa000u + drained++;
}
static void dma_channel_cleanup(unsigned ch) {
    assert(state.capture_frozen && !dma.abort);
    assert((owned_dma_mask & (1u << ch)) && !(cleaned_dma_mask & (1u << ch)));
    cleaned_dma_mask |= 1u << ch;
    assert(!(dma.ch[ch].al1_ctrl & DMA_CH0_CTRL_TRIG_EN_BITS));
    assert(((dma.ch[ch].al1_ctrl >> 13) & 15) == ch);
    dma.ch[ch].ctrl_trig = 0; cleaned++;
}
static bool apply_bus_priority(uint32_t priority) { (void)priority; return true; }
static void pio_sm_set_pins_with_mask(PIO p, unsigned sm, uint32_t value, uint32_t mask) {
    (void)p; (void)sm; (void)value; (void)mask;
}
static void pio_sm_set_pindirs_with_mask(PIO p, unsigned sm, uint32_t value, uint32_t mask) {
    (void)p; (void)sm; (void)value; (void)mask;
}
static void gpio_put_masked(uint32_t mask, uint32_t value) { (void)mask; (void)value; }
static void gpio_set_dir_masked(uint32_t mask, uint32_t value) { (void)mask; (void)value; }
static void reset_block(uint32_t mask) { (void)mask; }
static void unreset_block_wait(uint32_t mask) { (void)mask; }
static void clock_stop(unsigned clk) { (void)clk; }
static void clock_set_reported_hz(unsigned clk, uint32_t hz) { (void)clk; (void)hz; }
static void dma_channel_unclaim(unsigned ch) {
    assert((cleaned_dma_mask & (1u << ch)) && !(unclaimed_dma_mask & (1u << ch)));
    unclaimed_dma_mask |= 1u << ch; unclaimed++;
}
static const pio_program_t *i_output_program(void) { static const pio_program_t p = {0}; return &p; }
static const pio_program_t *q_output_program(void) { return i_output_program(); }
static void pio_remove_program(PIO p, const pio_program_t *program, unsigned offset) {
    (void)p; (void)program; (void)offset;
}
static void pio_sm_unclaim(PIO p, unsigned sm) { assert(p->claimed[sm]); p->claimed[sm] = false; }
'''

STOP_CHECKS = r'''
int main(void) {
    for (unsigned writers = 2; writers <= 4; writers += 2)
    for (unsigned fifo = 0; fifo < 2; fifo++)
#ifdef IQ4HIST_EXPERIMENT
    for (unsigned quad = 0; quad <= (writers == 4 && !fifo); quad++)
#endif
    for (unsigned running = 0; running < 2; running++)
    for (unsigned cap = 0; cap < 3; cap++)
    for (unsigned ahb = 0; ahb < 4; ahb++)
    for (unsigned ddr = 0; ddr < 2; ddr++) {
        memset(&state, 0, sizeof state); memset(&dma, 0, sizeof dma);
        memset(&output, 0, sizeof output); memset(&capture, 0, sizeof capture);
        memset(&dacclk, 0, sizeof dacclk); memset(&io_bank, 0, sizeof io_bank); memset(&pads, 0, sizeof pads);
        // DDR clock SM 1 is released and GPIO28 restored only when this engine claimed it;
        // with memset-only state (no clock_pio) an external claim of SM1 must survive.
        dacclk.claimed[1] = true;
        if (ddr) {
            state.dac_clock = 1; state.clock_pio = &dacclk; state.clock_sm = 1;
            state.clock_added = state.clock_saved = true;
            state.clock_gpio_ctrl = 0x1f; state.clock_gpio_pad = 0x56;
        }
        dma_hw = &dma; reads = faults = halted = cleaned = unclaimed = drained = aborts = 0;
        owned_dma_mask = cleaned_dma_mask = unclaimed_dma_mask = 0;
        state.owned = state.saved = true; state.fifo_source = fifo;
#ifdef IQ4HIST_EXPERIMENT
        state.quad_rows = quad;
#endif
        state.running = state.result.running = running;
        state.writers = writers; state.output_pio = &output;
        for (unsigned s = 0; s < writers; s++) {
            state.control[s] = s + 2; state.data[s] = s + 2 + writers; state.sm[s] = s;
            output.claimed[s] = true;
        }
        state.cap_dma = 2 + 2 * writers; state.sm_cap = 2; capture.claimed[2] = true;
        // External pointer producer claims must survive output stop at every state.
        capture.claimed[0] = capture.claimed[1] = true;
        state.out_added = state.out_q_added = state.cap_added = true;
        state.hstx_changed = state.priority_changed = true;
        state.capture_started = cap != 0; state.capture_fifo_only = cap == 2;
        state.capture_requested = 16; dma.ch[state.cap_dma].transfer_count = 5;
        uint32_t buffer[16] = {0}; state.capture_buffer = buffer; fifo_level = 5;
        for (unsigned ch = 2; ch <= (unsigned)state.cap_dma; ch++) {
            dma.ch[ch].al1_ctrl = 1 | (((ch + 1) % 16) << 13);
            owned_dma_mask |= 1u << ch;
        }
        if (ahb) dma.ch[ahb == 1 ? state.control[0] : ahb == 2 ? state.cap_dma :
                            state.control[writers - 1]].ctrl_trig = DMA_CH0_CTRL_TRIG_AHB_ERROR_BITS;
        dual_dma_stop();
        assert(!state.owned && !state.running && !state.result.running && state.capture_frozen);
        assert(reads == (fifo
#ifdef IQ4HIST_EXPERIMENT
                         || quad
#endif
                         ? 0 : 1 + running));
        assert(faults == 2 && halted == running && aborts == 1);
        assert(cleaned == 2 * writers + 1 && unclaimed == 2 * writers + 1);
        assert(cleaned_dma_mask == owned_dma_mask && unclaimed_dma_mask == owned_dma_mask);
        for (unsigned s = 0; s < writers; s++) assert(!output.claimed[s]);
        assert(!capture.claimed[2]);
        assert(capture.claimed[0] && capture.claimed[1]);
        assert(dacclk.claimed[1] == !ddr);
        assert(io_bank.io[28].ctrl == (ddr ? 0x1fu : 0u) && pads.io[28] == (ddr ? 0x56u : 0u));
        assert(state.result.errors == (ahb == 2 ? DUAL_DMA_ERR_CAPTURE_DMA :
                                      ahb ? DUAL_DMA_ERR_OUTPUT_DMA : 0));
        assert(state.captured == (cap == 1 ? 11 : cap == 2 ? 5 : 0));
        assert(drained == (cap == 2 ? 5 : 0));
        if (cap == 2) for (unsigned n = 0; n < 5; n++) assert(buffer[n] == 0xa000u + n);
        dma_hw = NULL; dual_dma_stop(); // no register access after ownership release
    }
    puts("dual DMA stop: PASS (two/four writers including routers, both sources, running/halted, partial DMA/FIFO capture, AHB faults, external pointer ownership, DDR clock release, no FIFO cursor reads)");
}
'''


def compile_run(code, stem, experiment=False):
    with tempfile.TemporaryDirectory(prefix=f"dual-{stem}-") as directory:
        test = Path(directory) / f"{stem}.c"
        executable = Path(directory) / stem
        test.write_text(code)
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        *(["-DIQ4HIST_EXPERIMENT=1"] if experiment else []),
                        f"-I{ROOT / 'firmware/src'}", str(test), "-o", str(executable)], check=True)
        subprocess.run([str(executable)], check=True)


def main():
    source = (ROOT / "firmware/src/dual_dma.c").read_text()
    functions = [
        ("static void", "claim_init", "void"),
        ("static void", "axis_data_dma", "int control, int data, uint sm"),
        ("static void", "axis_dma", "int control, int data, uint sm, const uint32_t *descriptors, uint ring_bits"),
        ("static void", "axis_fifo_dma", "int control, int data, uint sm, const volatile uint32_t *rx, unsigned dreq"),
        ("static void", "axis_alternating_fifo_dma", "unsigned axis"),
        ("static bool", "valid_rx_source", "const volatile uint32_t *rx, unsigned dreq"),
        ("bool", "dual_dma_start_div256", "const uint32_t *desc_i, const uint32_t *desc_q, unsigned count_log2, unsigned divider256"),
        ("bool", "dual_dma_start_fifo_div256", "const volatile uint32_t *rx_i, const volatile uint32_t *rx_q, unsigned dreq_i, unsigned dreq_q, bool control_bswap, unsigned divider256"),
        ("bool", "dual_dma_start_fifo", "const volatile uint32_t *rx_i, const volatile uint32_t *rx_q, unsigned dreq_i, unsigned dreq_q, bool control_bswap, unsigned cpw"),
        ("static void", "sample_reads", "bool check_time"),
    ]
    code = FIXTURE + "\n".join(f"{result} {name}({args}) {{\n{function_body(source, name)}\n}}"
                                for result, name, args in functions)
    compile_run(code + CHECKS, "fifo")
    quad = function_body(source, "dual_dma_start_quad_rows")
    quad_fifo = function_body(source, "dual_dma_start_quad_fifo")
    output = function_body(source, "start_output")
    controls = output[output.index("uint32_t control_mask = 0;"):output.index("const uint32_t priority =")]
    quad_code = f"\nbool dual_dma_start_quad_rows(const uint32_t *const desc[4], unsigned count_log2, unsigned cpw) {{\n{quad}\n}}\n"
    quad_code += f"\nbool dual_dma_start_quad_fifo(const volatile uint32_t *const rx[4], const unsigned dreq[4], unsigned cpw) {{\n{quad_fifo}\n}}\n"
    quad_code += f"\nstatic uint32_t output_controls(unsigned count_log2) {{\n{controls}\nreturn control_mask;\n}}\n"
    compile_run(code + quad_code + CHECKS,
                "quad-rows", experiment=True)
    state_type = re.search(r"typedef struct \{[\s\S]+?\} engine_t;", source)
    assert state_type
    code = STOP_FIXTURE + state_type[0] + "\nstatic engine_t state;\n" + STOP_STUBS
    for result, name, args in [("static unsigned", "channels_in_use", "int channels[9]"),
                               ("void", "dual_dma_stop", "void")]:
        code += f"{result} {name}({args}) {{\n{function_body(source, name)}\n}}\n"
    compile_run(code + STOP_CHECKS, "stop")
    compile_run(code + STOP_CHECKS, "quad-stop", experiment=True)


if __name__ == "__main__":
    main()
