#include "bench32bus.h"

#include <stdio.h>
#include <string.h>

#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/regs/busctrl.h"
#include "hardware/structs/busctrl.h"
#include "hardware/sync.h"
#include "iq32keys.h"
#include "iq32mapkeys.h"
#include "pico/stdlib.h"
#include "tx.h"

enum { SYMBOLS = 1024, BLOCKS = SYMBOLS / 2, OUT_WORDS = SYMBOLS * 2,
       SOURCE_BLOCKS = IN_WORDS / SYMBOLS, WINDOW_US = 20000 };
_Static_assert(3 * IQ32NATIVE8_TABLE_WORDS <= N_BLOCKS * BLOCK_WORDS, "table arena");
_Static_assert(2 * OUT_WORDS <= CAP_WORDS_MAX, "disjoint output arenas");
_Static_assert(IN_WORDS % SYMBOLS == 0 && SYMBOLS <= TABLE_WORDS, "fixed source chunks");

static struct {
    iq32native8_tables_t tables;
    volatile uint32_t go, stop;
    uint32_t calls, worst;
    uint64_t cycles;
} load __attribute__((aligned(16))); // stop at offset16 selects striped SRAM0.

static const uint32_t events[2][4] = {
    {BUSCTRL_PERFSEL0_VALUE_SRAM0_ACCESS, BUSCTRL_PERFSEL0_VALUE_SRAM0_STALL_UPSTREAM,
     BUSCTRL_PERFSEL0_VALUE_XIP_MAIN0_ACCESS, BUSCTRL_PERFSEL0_VALUE_XIP_MAIN0_STALL_UPSTREAM},
    {BUSCTRL_PERFSEL0_VALUE_SRAM4_ACCESS, BUSCTRL_PERFSEL0_VALUE_SRAM4_STALL_UPSTREAM,
     BUSCTRL_PERFSEL0_VALUE_XIP_MAIN1_ACCESS, BUSCTRL_PERFSEL0_VALUE_XIP_MAIN1_STALL_UPSTREAM},
};
static const char *const modes[] = {"sio_idle", "shared_flag_poll", "timer_1us_poll",
                                    "weighted_mapper", "rolling_key_shaper"};

static void shape(uint32_t *output, unsigned offset) {
    iq32keys_kernel_asm(output, in_buf + offset, BLOCKS,
                        in_buf + ((offset + IN_WORDS - 5u) % IN_WORDS), &load.tables);
}

static void worker(uint32_t mode) {
    while (!load.go) tight_loop_contents();
    __dmb();
    if (mode == 1) {
        while (!load.stop) tight_loop_contents();
        return;
    }
    if (mode == 2) {
        while (!load.stop) busy_wait_us_32(1);
        return;
    }
    while (!load.stop) {
        const uint32_t start = cycles();
        if (mode == 3) {
            iq32mapkeys_state_t state;
            const uint8_t *const tokens = (const uint8_t *)cfg.ti;
            iq32mapkeys_init(&state, tokens);
            iq32mapkeys_encode(cfg.tq, tokens + 2, SYMBOLS, &state);
        } else {
            shape(cap_buf + OUT_WORDS, 0);
        }
        const uint32_t dt = cycles() - start;
        load.cycles += dt;
        ++load.calls;
        if (dt > load.worst) load.worst = dt;
    }
}

static void restore_buffers(void) {
    iq_build_tables(&cfg);
    uint32_t seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
}

void bench_32bus(int mode, int group) {
    const uint32_t hz = clock_get_hz(clk_sys);
    if (mode < 0 || mode > 4 || group < 0 || group > 1 ||
        (hz != 128000000u && hz != 150000000u)) {
        printf("@{\"cmd\":\"bench32bus\",\"error\":\"needs mode0..4,group0..1,128/150MHz\"}\n");
        return;
    }
    memset(&load, 0, sizeof load);
    load.tables = (iq32native8_tables_t){ring_buf, ring_buf + IQ32NATIVE8_TABLE_WORDS,
                                         ring_buf + 2 * IQ32NATIVE8_TABLE_WORDS};
    if (!iq32native8_build(&load.tables, IQ32NATIVE8_DEFAULT_RATE)) {
        restore_buffers();
        printf("@{\"cmd\":\"bench32bus\",\"error\":\"table construction failed\"}\n");
        return;
    }
    uint32_t seed = SEED;
    iq32native8_fill(cfg.ti, IN_WORDS / 2, &seed);
    iq32keys_encode(in_buf, cfg.ti, IN_WORDS / 2, cfg.ti + IN_WORDS / 2 - 1);
    const uint32_t input_crc = crc32_update(0, in_buf, sizeof in_buf);
    const uint32_t table_crc[3] = {
        crc32_update(0, load.tables.old, IQ32NATIVE8_TABLE_WORDS * 4),
        crc32_update(0, load.tables.middle, IQ32NATIVE8_TABLE_WORDS * 4),
        crc32_update(0, load.tables.future, IQ32NATIVE8_TABLE_WORDS * 4),
    };
    if (input_crc != 2091885109u || table_crc[0] != 3255586384u ||
        table_crc[1] != 916828675u || table_crc[2] != 1817262751u) {
        restore_buffers();
        printf("@{\"cmd\":\"bench32bus\",\"error\":\"fixture CRC mismatch\"}\n");
        return;
    }
    uint8_t *const tokens = (uint8_t *)cfg.ti;
    seed = SEED;
    for (unsigned i = 0; i < IN_WORDS; ++i) {
        seed ^= seed << 13; seed ^= seed >> 17; seed ^= seed << 5;
        tokens[i + 2] = seed & 31u;
    }
    tokens[0] = tokens[IN_WORDS]; tokens[1] = tokens[IN_WORDS + 1];
    const uint32_t label_crc = crc32_update(0, tokens + 2, IN_WORDS);
    iq32mapkeys_state_t state;
    iq32mapkeys_init(&state, tokens);
    iq32mapkeys_encode(cfg.tq, tokens + 2, SYMBOLS, &state);
    const uint32_t map_crc = crc32_update(0, cfg.tq, SYMBOLS * 4);
    if (label_crc != 1233905124u || memcmp(cfg.tq, in_buf, SYMBOLS * 4)) {
        restore_buffers();
        printf("@{\"cmd\":\"bench32bus\",\"error\":\"mapper fixture mismatch\"}\n");
        return;
    }
    uint32_t expected[SOURCE_BLOCKS];
    for (unsigned i = 0; i < SOURCE_BLOCKS; ++i) {
        shape(cap_buf, i * SYMBOLS);
        expected[i] = crc32_update(0, cap_buf, OUT_WORDS * 4);
    }
    shape(cap_buf + OUT_WORDS, 0);

    uint32_t prior[4], counts[4];
    const uint32_t enabled = busctrl_hw->perfctr_en;
    busctrl_hw->perfctr_en = 0;
    for (unsigned i = 0; i < 4; ++i) {
        prior[i] = busctrl_hw->counter[i].sel;
        busctrl_hw->counter[i].sel = events[group][i];
        busctrl_hw->counter[i].value = 0;
    }
    if (mode) tx_worker_start(worker, (uint32_t)mode);
    uint32_t calls = 0, worst = 0;
    uint64_t shape_cycles = 0;
    __dmb();
    const uint32_t start_us = time_us_32(), start_cycles = cycles();
    busctrl_hw->perfctr_en = 1;
    load.go = 1;
    __dmb();
    do {
        const uint32_t begin = cycles();
        shape(cap_buf, (calls % SOURCE_BLOCKS) * SYMBOLS);
        const uint32_t dt = cycles() - begin;
        shape_cycles += dt;
        if (dt > worst) worst = dt;
        ++calls;
    } while ((uint32_t)(time_us_32() - start_us) < WINDOW_US);
    busctrl_hw->perfctr_en = 0;
    const uint32_t elapsed_cycles = cycles() - start_cycles;
    const uint32_t elapsed_us = time_us_32() - start_us;
    for (unsigned i = 0; i < 4; ++i) counts[i] = busctrl_hw->counter[i].value;
    load.stop = 1;
    __dmb();
    if (mode) tx_worker_wait();
    const uint32_t join_tail_us = time_us_32() - start_us - elapsed_us;
    for (unsigned i = 0; i < 4; ++i) busctrl_hw->counter[i].sel = prior[i];
    busctrl_hw->perfctr_en = enabled;

    const uint32_t actual_crc = crc32_update(0, cap_buf, OUT_WORDS * 4);
    const uint32_t worker_crc = mode == 3 ? crc32_update(0, cfg.tq, SYMBOLS * 4) :
        mode == 4 ? crc32_update(0, cap_buf + OUT_WORDS, OUT_WORDS * 4) : 0;
    bool correct = calls && actual_crc == expected[(calls - 1) % SOURCE_BLOCKS] &&
        crc32_update(0, in_buf, sizeof in_buf) == input_crc &&
        crc32_update(0, tokens + 2, IN_WORDS) == label_crc;
    if (mode == 3) correct &= load.calls && worker_crc == map_crc;
    if (mode == 4) correct &= load.calls && worker_crc == expected[0];
    bool saturated = false;
    for (unsigned i = 0; i < 4; ++i) saturated |= counts[i] == BUSCTRL_PERFCTR0_BITS;
    correct &= !saturated && elapsed_us <= 50000u && join_tail_us <= 1000u;
    printf("@{\"cmd\":\"bench32bus\",\"git\":\"%s\",\"clk_hz\":%lu,\"mode\":%d,\"mode_name\":\"%s\",\"group\":%d,"
           "\"dma_output\":false,\"interrupts_enabled\":true,\"shared_flag_addr\":%lu,\"window_requested_us\":%u,\"elapsed_us\":%lu,"
           "\"elapsed_cycles\":%lu,\"join_tail_us\":%lu,\"symbols_per_call\":%u,\"core0_calls\":%lu,"
           "\"core0_shape_cycles\":%llu,\"core0_shape_cyc_per_sym\":%.4f,\"core0_worst_cyc_per_sym\":%.4f,"
           "\"core1_calls\":%lu,\"core1_complete_call_cycles\":%llu,\"core1_cyc_per_sym\":%.4f,"
           "\"core1_worst_cyc_per_sym\":%.4f,\"core1_timing_includes_stop_tail\":true,"
           "\"input_crc\":%lu,\"label_crc\":%lu,\"output_crc\":%lu,\"worker_crc\":%lu,\"crc_ok\":%s,"
           "\"counter_saturated\":%s,\"previous_counter_counts_discarded\":true,\"events\":[",
           GIT_REV, (unsigned long)hz, mode, modes[mode], group,
           (unsigned long)(uintptr_t)&load.stop, WINDOW_US,
           (unsigned long)elapsed_us, (unsigned long)elapsed_cycles, (unsigned long)join_tail_us,
           SYMBOLS, (unsigned long)calls, (unsigned long long)shape_cycles,
           calls ? (double)shape_cycles / (calls * SYMBOLS) : 0, (double)worst / SYMBOLS,
           (unsigned long)load.calls, (unsigned long long)load.cycles,
           load.calls ? (double)load.cycles / (load.calls * SYMBOLS) : 0, (double)load.worst / SYMBOLS,
           (unsigned long)input_crc, (unsigned long)label_crc, (unsigned long)actual_crc,
           (unsigned long)worker_crc, correct ? "true" : "false", saturated ? "true" : "false");
    for (unsigned i = 0; i < 4; ++i)
        printf("%s{\"selector\":%lu,\"count\":%lu}", i ? "," : "",
               (unsigned long)events[group][i], (unsigned long)counts[i]);
    printf("]}\n");
    restore_buffers();
}
