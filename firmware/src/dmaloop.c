// DMA pointer-chase loop timing: how many SYS cycles does one control -> sample -> chain
// iteration take, alone and under load? Opt-in experiment (IQ4HIST_EXPERIMENT builds).
//
// Each chain is the quad-row graph without writers: a control channel reads the next row
// pointer from an SRAM ring into its sample channel's READ_ADDR_TRIG; the sample channel
// copies the row to a sink and chains back. Sinks never back-pressure, so the chain runs as
// fast as the fabric lets it and the measured period is the loop latency itself.
#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "pico/sync.h"
#include "hardware/dma.h"
#include "hardware/pio.h"
#include "hardware/clocks.h"
#include "hardware/structs/busctrl.h"
#include "git_rev.h"
#include "dmaloop.h"
#include "iqgen.h"
#include "tx.h"

#define RING_LOG2 13u                       // 8 KiB = 2048 pointers per chain, in in_buf
#define RING_BYTES (1u << RING_LOG2)
#define RING_WORDS (RING_BYTES / 4u)
#define WINDOW 1000u                        // SYS cycles between cursor samples
#define WINDOWS 4000u                       // 4 M cycles = 31 ms, interrupts off
#define HAMMER_WORDS 4096u                  // 16 KiB copies in ring_buf's upper half

static volatile bool hammer_stop;
static volatile uint32_t hammer_copies;

static void hammer_task(uint32_t words) {
    // Core 1 streams SRAM -> SRAM copies through the same striped banks as table and rings.
    uint32_t *a = ring_buf + 16384u, *b = a + words, n = 0;
    while (!hammer_stop) { memcpy(b, a, words * 4u); ++n; }
    hammer_copies = n;
}

// Snapshot a counting sink's X (decremented once per consumed word) through its RX FIFO.
static uint32_t sink_count(PIO pio, uint sm) {
    pio_sm_exec(pio, sm, pio_encode_in(pio_x, 32));
    pio_sm_exec(pio, sm, pio_encode_push(false, false));
    return pio->rxf[sm];
}

static void set_priority(unsigned mask) {
    busctrl_hw->priority = ((mask & 1u) << 0) | ((mask & 2u) << 3) | ((mask & 4u) << 6) | ((mask & 8u) << 9);
    while (!(busctrl_hw->priority_ack & 1u)) tight_loop_contents();
}

static void stop_channels(uint32_t mask) {
    for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ++ch)
        if (mask & (1u << ch)) hw_clear_bits(&dma_hw->ch[ch].al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);
    dma_hw->abort = mask;
    while (dma_hw->abort & mask) tight_loop_contents();
    for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ++ch)
        if (mask & (1u << ch)) while (dma_channel_is_busy(ch)) tight_loop_contents();
}

void dmaloop(int chains, int row_words, int sink, int load_div, int load_kind, int prio, int hammer) {
    int ctl[4] = {-1, -1, -1, -1}, smp[4] = {-1, -1, -1, -1}, sm[5] = {-1, -1, -1, -1, -1};
    int load = -1, timer = -1;
    uint offset = 0, load_offset = 0;
    bool added = false, load_added = false;
    const char *error = NULL;
    const uint16_t sink_code[1] = {(uint16_t)pio_encode_out(pio_null, 32)};
    const pio_program_t sink_program = {.instructions = sink_code, .length = 1, .origin = -1};
    // Mode 2 (PIO pointers, as B2/B4): sinks count consumed words in X, and one source SM per
    // chain pushes its preloaded row address every cycle the RX FIFO has space.
    const uint16_t count_code[2] = {(uint16_t)pio_encode_out(pio_null, 32), (uint16_t)pio_encode_jmp_x_dec(0)};
    const uint16_t source_code[1] = {(uint16_t)pio_encode_in(pio_x, 32)};
    const pio_program_t count_program = {.instructions = count_code, .length = 2, .origin = -1};
    const pio_program_t source_program = {.instructions = source_code, .length = 1, .origin = -1};
    const pio_program_t *chain_sink = sink == 2 ? &count_program : &sink_program;
    int src_sm[4] = {-1, -1, -1, -1};
    uint src_offset = 0;
    bool src_added = false;
    uint32_t x0[4] = {0};
    static uint32_t scratch[2], load_word = 0x5a5a5a5au;
    const unsigned saved_priority = busctrl_hw->priority;
    uint32_t total[4] = {0}, min_d[4], max_d[4], all = 0, chain_mask = 0, ctl_mask = 0;
    uint64_t elapsed = 0;

    // Pointers into the low 64 KiB of ring_buf, spread like table keys over the striped banks.
    uint32_t seed = SEED;
    const uint32_t rows = 65536u / (4u * (uint32_t)row_words);
    for (unsigned i = 0; i < 4u * RING_WORDS; ++i) {
        seed = seed * 1664525u + 1013904223u;
        in_buf[i] = (uint32_t)(uintptr_t)ring_buf + (seed >> 16) % rows * 4u * (uint32_t)row_words;
    }
    for (int c = 0; c < chains; ++c) {
        ctl[c] = dma_claim_unused_channel(false);
        smp[c] = dma_claim_unused_channel(false);
        if (ctl[c] < 0 || smp[c] < 0) { error = "DMA channels unavailable"; goto cleanup; }
    }
    if (sink) {
        if (!pio_can_add_program(pio2, chain_sink)) { error = "PIO2 instruction space unavailable"; goto cleanup; }
        offset = pio_add_program(pio2, chain_sink); added = true;
    }
    if (sink == 2) {
        if (load_div && !load_kind) { error = "mode 2 uses PIO1 for pointer sources; use load_kind 1"; goto cleanup; }
        if (!pio_can_add_program(pio1, &source_program)) { error = "PIO1 instruction space unavailable"; goto cleanup; }
        src_offset = pio_add_program(pio1, &source_program); src_added = true;
        for (int c = 0; c < chains; ++c) {
            src_sm[c] = pio_claim_unused_sm(pio1, false);
            if (src_sm[c] < 0) { error = "PIO1 source SMs unavailable"; goto cleanup; }
            pio_sm_config cc = pio_get_default_sm_config();
            sm_config_set_wrap(&cc, src_offset, src_offset);
            sm_config_set_in_shift(&cc, false, true, 32);
            pio_sm_init(pio1, (uint)src_sm[c], src_offset, &cc);
            pio_sm_put(pio1, (uint)src_sm[c], in_buf[(uint32_t)c * RING_WORDS]);
            pio_sm_exec(pio1, (uint)src_sm[c], pio_encode_pull(false, true));
            pio_sm_exec(pio1, (uint)src_sm[c], pio_encode_mov(pio_x, pio_osr));
            pio_sm_set_enabled(pio1, (uint)src_sm[c], true);
        }
    }
    // Chain sinks on PIO2, the load sink on PIO1: OUT NULL,32 with autopull drains a word
    // every cycle or two, so the TX DREQ never holds a channel back.
    for (int s = 0; s < (sink ? chains : 0) + (load_div && !load_kind ? 1 : 0); ++s) {
        const bool load_sink = s == (sink ? chains : 0);
        PIO pio = load_sink ? pio1 : pio2;
        if (load_sink) {
            if (!pio_can_add_program(pio1, &sink_program)) { error = "PIO1 instruction space unavailable"; goto cleanup; }
            load_offset = pio_add_program(pio1, &sink_program); load_added = true;
        }
        sm[s] = pio_claim_unused_sm(pio, false);
        if (sm[s] < 0) { error = "PIO sink SMs unavailable"; goto cleanup; }
        const uint at = load_sink ? load_offset : offset;
        pio_sm_config cc = pio_get_default_sm_config();
        sm_config_set_wrap(&cc, at, at + (!load_sink && sink == 2));
        sm_config_set_out_shift(&cc, true, true, 32);
        sm_config_set_in_shift(&cc, false, false, 32);
        pio_sm_init(pio, (uint)sm[s], at, &cc);
        if (!load_sink && sink == 2) pio_sm_exec(pio, (uint)sm[s], pio_encode_mov_not(pio_x, pio_null));
        pio_sm_set_enabled(pio, (uint)sm[s], true);
    }
    for (int c = 0; c < chains; ++c) {
        dma_channel_config dc = dma_channel_get_default_config((uint)smp[c]);
        channel_config_set_transfer_data_size(&dc, DMA_SIZE_32);
        channel_config_set_read_increment(&dc, true);
        channel_config_set_write_increment(&dc, false);
        channel_config_set_dreq(&dc, sink ? pio_get_dreq(pio2, (uint)sm[c], true) : DREQ_FORCE);
        channel_config_set_chain_to(&dc, (uint)ctl[c]);
        channel_config_set_irq_quiet(&dc, true);
        channel_config_set_high_priority(&dc, true);
        dma_channel_configure((uint)smp[c], &dc, sink ? (volatile void *)&pio2->txf[sm[c]] : (volatile void *)&scratch[0],
                              NULL, (uint)row_words, false);
        dc = dma_channel_get_default_config((uint)ctl[c]);
        channel_config_set_transfer_data_size(&dc, DMA_SIZE_32);
        channel_config_set_read_increment(&dc, sink != 2);
        channel_config_set_write_increment(&dc, false);
        if (sink != 2) channel_config_set_ring(&dc, false, RING_LOG2);
        channel_config_set_dreq(&dc, sink == 2 ? pio_get_dreq(pio1, (uint)src_sm[c], false) : DREQ_FORCE);
        channel_config_set_chain_to(&dc, (uint)ctl[c]);
        channel_config_set_irq_quiet(&dc, true);
        channel_config_set_high_priority(&dc, true);
        dma_channel_configure((uint)ctl[c], &dc, &dma_hw->ch[smp[c]].al3_read_addr_trig,
                              sink == 2 ? (const volatile void *)&pio1->rxf[src_sm[c]] : (const volatile void *)(in_buf + (uint32_t)c * RING_WORDS),
                              1, false);
        chain_mask |= (1u << ctl[c]) | (1u << smp[c]);
        ctl_mask |= 1u << ctl[c];
    }
    if (load_div) {
        // Background traffic paced at SYS/load_div: kind 0 = SRAM -> PIO FIFO (one fast-
        // peripheral access per transfer), kind 1 = SRAM -> SRAM (DMA slot and SRAM only).
        load = dma_claim_unused_channel(false);
        timer = dma_claim_unused_timer(false);
        if (load < 0 || timer < 0) { error = "load DMA channel or timer unavailable"; goto cleanup; }
        dma_timer_set_fraction((uint)timer, 1, (uint16_t)load_div);
        dma_channel_config dc = dma_channel_get_default_config((uint)load);
        channel_config_set_transfer_data_size(&dc, DMA_SIZE_32);
        channel_config_set_read_increment(&dc, false);
        channel_config_set_write_increment(&dc, false);
        channel_config_set_dreq(&dc, dma_get_timer_dreq((uint)timer));
        channel_config_set_irq_quiet(&dc, true);
        dma_channel_configure((uint)load, &dc, load_kind ? (volatile void *)&scratch[1] : (volatile void *)&pio1->txf[sm[sink ? chains : 0]],
                              &load_word, 0xf0000000u, false); // ENDLESS
        chain_mask |= 1u << load;
    }
    set_priority((unsigned)prio);
    if (hammer) { hammer_stop = false; tx_worker_start(hammer_task, HAMMER_WORDS); }
    if (load >= 0) dma_channel_start((uint)load);
    __dmb();
    dma_start_channel_mask(ctl_mask);
    busy_wait_us(200);

    uint32_t prev[4];
    const uint32_t irq = save_and_disable_interrupts();
    for (int c = 0; c < chains; ++c) { prev[c] = dma_hw->ch[ctl[c]].read_addr; min_d[c] = UINT32_MAX; max_d[c] = 0; }
    if (sink == 2) for (int c = 0; c < chains; ++c) x0[c] = sink_count(pio2, (uint)sm[c]);
    const uint32_t start = cycles();
    uint32_t t = start;
    for (unsigned w = 0; w < WINDOWS; ++w) {
        while ((uint32_t)(cycles() - t) < WINDOW) tight_loop_contents();
        t += WINDOW;
        for (int c = 0; c < chains && sink != 2; ++c) {
            const uint32_t a = dma_hw->ch[ctl[c]].read_addr;
            const uint32_t d = ((a - prev[c]) & (RING_BYTES - 1u)) >> 2;
            prev[c] = a; total[c] += d;
            if (d < min_d[c]) min_d[c] = d;
            if (d > max_d[c]) max_d[c] = d;
        }
    }
    elapsed = (uint32_t)(cycles() - start);
    if (sink == 2)
        for (int c = 0; c < chains; ++c) {
            total[c] = (x0[c] - sink_count(pio2, (uint)sm[c])) / (uint32_t)row_words;
            min_d[c] = max_d[c] = 0;
        }
    restore_interrupts(irq);

cleanup:
    if (chain_mask) stop_channels(chain_mask);
    if (hammer) { hammer_stop = true; tx_worker_wait(); }
    busctrl_hw->priority = saved_priority;
    while (!(busctrl_hw->priority_ack & 1u)) tight_loop_contents();
    for (int c = 0; c < 4; ++c) {
        if (ctl[c] >= 0) dma_channel_unclaim((uint)ctl[c]);
        if (smp[c] >= 0) dma_channel_unclaim((uint)smp[c]);
    }
    if (load >= 0) dma_channel_unclaim((uint)load);
    if (timer >= 0) dma_timer_unclaim((uint)timer);
    for (int s = 0; s < 5; ++s) {
        PIO pio = s == (sink ? chains : 0) ? pio1 : pio2;
        if (sm[s] >= 0) { pio_sm_set_enabled(pio, (uint)sm[s], false); pio_sm_unclaim(pio, (uint)sm[s]); }
    }
    for (int c = 0; c < 4; ++c)
        if (src_sm[c] >= 0) { pio_sm_set_enabled(pio1, (uint)src_sm[c], false); pio_sm_unclaim(pio1, (uint)src_sm[c]); }
    if (src_added) pio_remove_program(pio1, &source_program, src_offset);
    if (added) pio_remove_program(pio2, chain_sink, offset);
    if (load_added) pio_remove_program(pio1, &sink_program, load_offset);
    prbs_fill(in_buf, IN_WORDS, &(uint32_t){SEED});
    if (error) {
        printf("@{\"cmd\":\"dmaloop\",\"error\":\"%s\"}\n", error);
        return;
    }
    printf("@{\"cmd\":\"dmaloop\",\"git\":\"%s\",\"clk_hz\":%lu,\"chains\":%d,\"row_words\":%d,\"sink\":\"%s\","
           "\"load_div\":%d,\"load_kind\":\"%s\",\"bus_priority\":%d,\"hammer\":%d,\"window_cycles\":%u,"
           "\"elapsed_cycles\":%llu,\"iterations\":[",
           GIT_REV, (unsigned long)clock_get_hz(clk_sys), chains, row_words, sink == 2 ? "pio_pointers" : sink ? "pio" : "sram",
           load_div, load_div ? (load_kind ? "sram" : "pio") : "none", prio, hammer, WINDOW,
           (unsigned long long)elapsed);
    for (int c = 0; c < chains; ++c) { printf("%s%lu", c ? "," : "", (unsigned long)total[c]); all += total[c]; }
    printf("],\"cycles_per_iteration\":[");
    for (int c = 0; c < chains; ++c) printf("%s%.3f", c ? "," : "", total[c] ? (double)elapsed / total[c] : 0.0);
    printf("],\"window_min_iterations\":[");
    for (int c = 0; c < chains; ++c) printf("%s%lu", c ? "," : "", (unsigned long)min_d[c]);
    printf("],\"window_max_iterations\":[");
    for (int c = 0; c < chains; ++c) printf("%s%lu", c ? "," : "", (unsigned long)max_d[c]);
    printf("],\"rows_per_us\":%.3f,\"hammer_copies\":%lu}\n",
           elapsed ? all * (clock_get_hz(clk_sys) / 1e6) / (double)elapsed : 0.0,
           (unsigned long)(hammer ? hammer_copies : 0));
}
