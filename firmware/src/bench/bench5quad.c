#include "bench5quad.h"

#include <stdio.h>
#include <string.h>

#include "dual_dma.h"
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "iq4hist.h"
#include "iq4quad.h"
#include "iq5hist.h"
#include "pico/stdlib.h"
#include "tx.h"

enum {
    RING_WORDS = IN_WORDS / 4, AXIS_WORDS = RING_WORDS / 8,
    CAP_WORDS = 4096, POLL_US = 128, MAX_POLL_US = 256,
    ERR_POINTER = 1u << 16, ERR_CAPTURE = 1u << 17, ERR_TABLE = 1u << 18,
    ERR_CONFIG = 1u << 19, ERR_POLL = 1u << 20,
};
_Static_assert(RING_WORDS == 2048 && AXIS_WORDS == 256, "4096-symbol cyclic pointer period");
_Static_assert(IQ5HIST_TABLE_WORDS == N_BLOCKS * BLOCK_WORDS, "borrow the output ring");
_Static_assert(2 * AXIS_WORDS <= CAP_WORDS && CAP_WORDS <= CAP_WORDS_MAX, "borrow capture for setup codes");

typedef struct { uint32_t dma, sm[3]; } claims_t;

static claims_t claims(void) {
    claims_t c = {0};
    const PIO pios[] = {pio0, pio1, pio2};
    for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ++ch)
        if (dma_channel_is_claimed(ch)) c.dma |= 1u << ch;
    for (unsigned p = 0; p < 3; ++p)
        for (unsigned sm = 0; sm < NUM_PIO_STATE_MACHINES; ++sm)
            if (pio_sm_is_claimed(pios[p], sm)) c.sm[p] |= 1u << sm;
    return c;
}

void bench5quad8(int cpw, int ms, int invert, int priority) {
    const char *cmd = "bench5quad8";
    const unsigned bits = 8;
    const claims_t before = claims();
    if ((cpw != 1 && cpw != 2) || ms < 100 || ms > 60000 ||
        (invert != 0 && invert != 1) || priority < 0 || priority > 15 ||
        clock_get_hz(clk_sys) != 128000000u ||
        ((uintptr_t)ring_buf & (128u * 1024u - 1u)) ||
        ((uintptr_t)in_buf & (4u * RING_WORDS - 1u)) ||
        __builtin_popcount(before.dma) > NUM_DMA_CHANNELS - 9 ||
        before.sm[2] || before.sm[0] == 15 || before.sm[1] == 15) {
        printf("@{\"cmd\":\"%s\",\"error\":\"needs SYS128,cpw1/2,100..60000ms,invert0/1,prio0..15,aligned experiment build and idle resources\"}\n", cmd);
        return;
    }
    const unsigned saved_writers = dual_dma_get_writers(), saved_rows = dual_dma_get_row_words();
    const unsigned saved_bits = dual_dma_get_sample_bits(), saved_schedule = dual_dma_get_schedule();
    const unsigned saved_priority = dual_dma_get_bus_priority(), saved_clock = dual_dma_get_dac_clock();
    // Fail before borrowing input storage if another output owns it.
    if (!dual_dma_set_writers(4)) {
        printf("@{\"cmd\":\"%s\",\"error\":\"output selectors unavailable\"}\n", cmd);
        return;
    }
    const unsigned row_words = 2;
    uint32_t *const rings[] = {in_buf, in_buf + RING_WORDS, in_buf + 2 * RING_WORDS, in_buf + 3 * RING_WORDS};
    const uint32_t *const descriptors[] = {rings[0], rings[1], rings[2], rings[3]};
    uint32_t errors = 0, table_crc = 0, pointer_crc = 0, pointer_after_crc = 0;
    uint32_t input_crc = 0, previous[2] = {0}, elapsed = 0, polls = 0, max_gap = 0;
    uint32_t capture_count = 0, cap_crc = 0;
    uint64_t work_cycles = 0;
    bool pointers_built = false, attempted = false, captured = false, restored = false;
    dual_dma_faults_t output = {0};
    int clock_pio = -1, clock_sm = -1;
    const char *reason = "complete";
    if (!dual_dma_set_row_words(row_words) || !dual_dma_set_sample_bits(bits) ||
        !dual_dma_set_schedule(0) || !dual_dma_set_bus_priority((unsigned)priority) ||
        !dual_dma_set_dac_clock(invert ? DUAL_DMA_DAC_CLOCK_DDR_PIO_INVERTED : DUAL_DMA_DAC_CLOCK_DDR_PIO)) {
        errors |= ERR_CONFIG; reason = dual_dma_last_error(); goto cleanup;
    }
    uint32_t seed = SEED;
    for (unsigned group = 0; group < AXIS_WORDS; ++group) {
        iq8_input_t planes;
        iq8_fill(&planes, 1, &seed);
        iq4hist_pack_group(&planes, cap_buf + group, cap_buf + AXIS_WORDS + group);
    }
    input_crc = crc32_update(0, cap_buf, 2 * AXIS_WORDS * 4);
    previous[0] = cap_buf[AXIS_WORDS - 1]; previous[1] = cap_buf[2 * AXIS_WORDS - 1];
    if (!iq5hist_build(ring_buf, N_BLOCKS * BLOCK_WORDS)) {
        errors |= ERR_TABLE; reason = "table build failed"; goto cleanup;
    }
    table_crc = crc32_update(0, ring_buf, IQ5HIST_ROWS * row_words * 4);
    if (table_crc != IQ5HIST_TABLE_CRC) {
        errors |= ERR_TABLE; reason = "table CRC mismatch"; goto cleanup;
    }
    if (!iq4quad_build(rings, RING_WORDS, cap_buf, cap_buf + AXIS_WORDS, AXIS_WORDS,
                      (uint32_t)(uintptr_t)ring_buf, row_words)) {
        errors |= ERR_POINTER; reason = "cyclic pointer build failed"; goto cleanup;
    }
    pointer_crc = crc32_update(0, in_buf, sizeof in_buf);
    pointers_built = true;
    memset(cap_buf, 0xa5, CAP_WORDS * 4);
    attempted = true;
    if (!dual_dma_start_quad5_rows(descriptors, 11, (unsigned)cpw)) {
        errors |= ERR_CONFIG; reason = dual_dma_last_error(); goto cleanup;
    }
    clock_pio = dual_dma_dac_clock_pio(); clock_sm = dual_dma_dac_clock_sm();
    const uint32_t start = time_us_32();
    uint32_t last_poll = start - POLL_US;
    while ((uint32_t)(time_us_32() - start) < (uint32_t)ms * 1000u) {
        const uint32_t now = time_us_32(), gap = now - last_poll;
        if (gap < POLL_US) { tight_loop_contents(); continue; }
        const uint32_t begin = cycles();
        last_poll = now; polls++;
        if (gap > max_gap) max_gap = gap;
        if (gap > MAX_POLL_US) { errors |= ERR_POLL; reason = "supervision poll deadline"; }
        output = dual_dma_faults();
        if (output.errors) { errors |= output.errors; reason = "output fault"; }
        if (!errors && !captured && now - start >= (uint32_t)ms * 500u) {
            captured = dual_dma_capture_pins(cap_buf, CAP_WORDS);
            if (!captured) { errors |= ERR_CAPTURE; reason = dual_dma_last_error(); }
        }
        work_cycles += (uint32_t)(cycles() - begin);
        if (errors) break;
    }
    elapsed = time_us_32() - start;
cleanup:
    if (attempted) {
        dual_dma_stop(); output = dual_dma_faults(); errors |= output.errors;
        capture_count = captured ? dual_dma_capture_count() : 0;
        errors |= dual_dma_capture_errors();
    }
    if (attempted && capture_count != CAP_WORDS) errors |= ERR_CAPTURE;
    if (pointers_built) {
        pointer_after_crc = crc32_update(0, in_buf, sizeof in_buf);
        if (pointer_after_crc != pointer_crc) errors |= ERR_POINTER;
    }
    const claims_t after = claims();
    const bool released = before.dma == after.dma && before.sm[0] == after.sm[0] &&
        before.sm[1] == after.sm[1] && before.sm[2] == after.sm[2];
    if (!released) errors |= ERR_CONFIG;
    const bool selectors_ok = dual_dma_set_writers(2) && dual_dma_set_row_words(saved_rows) &&
        dual_dma_set_sample_bits(saved_bits) && dual_dma_set_writers(saved_writers) &&
        dual_dma_set_schedule(saved_schedule) && dual_dma_set_bus_priority(saved_priority) &&
        dual_dma_set_dac_clock(saved_clock);
    if (!selectors_ok) errors |= ERR_CONFIG;
    if (released && selectors_ok) { seed = SEED; prbs_fill(in_buf, IN_WORDS, &seed); restored = true; }
    const unsigned cap_words = capture_count == CAP_WORDS ? CAP_WORDS : 0;
    if (cap_words) cap_crc = crc32_update(0, cap_buf, cap_words * 4);
    const double budget = (double)elapsed * clock_get_hz(clk_sys) / 1e6;
    printf("@{\"cmd\":\"%s\",\"git\":\"%s\",\"clk_hz\":%lu,\"cpw\":%d,\"divider256\":%d,"
           "\"ms\":%d,\"invert\":%d,\"source\":\"cyclic_8psk\",\"sps\":5,\"L\":7,"
           "\"row_profile\":\"%s\",\"row_symbols\":1,\"row_padding_bytes\":3,\"row_words\":%u,\"writers\":4,"
           "\"sample_bits\":%u,\"low_bits\":0,\"bus_priority\":%d,\"chains_per_axis\":2,"
           "\"pointer_source\":\"sram\",\"period_symbols\":%u,\"pointer_words\":%u,\"pointer_ring_words\":%u,"
           "\"seed\":%lu,\"in_words\":%u,\"input_words\":%u,\"input_crc\":%lu,\"previous_words\":[%lu,%lu],"
           "\"input_base\":%lu,\"source_base\":%lu,\"pointer_base\":%lu,\"pointer_bases\":[%lu,%lu,%lu,%lu],"
           "\"pointer_crc\":%lu,\"pointer_after_crc\":%lu,\"ring_base\":%lu,\"table_base\":%lu,"
           "\"ring_bytes\":%u,\"table_bytes\":%u,\"table_crc\":%lu,\"errors\":%lu,\"pointer_errors\":%lu,"
           "\"output_errors\":%lu,\"txstalls_i\":%lu,\"txstalls_q\":%lu,\"reason\":\"%s\","
           "\"restored\":%s,\"selectors_restored\":%s,\"resources_released\":%s,\"core1_idle\":true,"
           "\"dac_clock_pio\":%d,\"dac_clock_sm\":%d,\"elapsed_us\":%lu,\"sym_rate\":%.1f,"
           "\"supervision_cycles\":%llu,\"supervision_busy\":%.6f,\"sample_math_busy\":0.0,"
           "\"descriptor_gen_busy\":0.0,\"idle_wait\":\"spin\",\"polls\":%lu,\"poll_interval_us\":%u,"
           "\"max_poll_interval_us\":%u,\"max_poll_gap_us\":%lu,\"cap_format\":\"pins32\",\"cap_words\":%u,"
           "\"capture_count\":%lu,\"cap_crc\":%lu,\"cap_stride\":17,\"cap_divider256\":%d,\"cap_fifo_only\":false}\n",
           cmd, GIT_REV, (unsigned long)clock_get_hz(clk_sys), cpw, cpw * 256, ms, invert, IQ5HIST_PROFILE,
           row_words, bits, priority, AXIS_WORDS * 16, IN_WORDS, RING_WORDS, (unsigned long)SEED,
           AXIS_WORDS, 2 * AXIS_WORDS, (unsigned long)input_crc, (unsigned long)previous[0], (unsigned long)previous[1],
           (unsigned long)(uintptr_t)in_buf, (unsigned long)(uintptr_t)cap_buf, (unsigned long)(uintptr_t)in_buf,
           (unsigned long)(uintptr_t)rings[0], (unsigned long)(uintptr_t)rings[1],
           (unsigned long)(uintptr_t)rings[2], (unsigned long)(uintptr_t)rings[3],
           (unsigned long)pointer_crc, (unsigned long)pointer_after_crc, (unsigned long)(uintptr_t)ring_buf,
           (unsigned long)(uintptr_t)ring_buf, (unsigned)sizeof ring_buf, IQ5HIST_ROWS * row_words * 4,
           (unsigned long)table_crc, (unsigned long)errors, (unsigned long)(errors & ERR_POINTER),
           (unsigned long)output.errors, (unsigned long)output.txstalls_i, (unsigned long)output.txstalls_q,
           reason, restored ? "true" : "false", selectors_ok ? "true" : "false", released ? "true" : "false",
           clock_pio, clock_sm, (unsigned long)elapsed, (double)clock_get_hz(clk_sys) / (10 * cpw),
           (unsigned long long)work_cycles, budget ? work_cycles / budget : 0, (unsigned long)polls,
           POLL_US, MAX_POLL_US, (unsigned long)max_gap, cap_words, (unsigned long)capture_count,
           (unsigned long)cap_crc, cpw * 256);
    if (cap_words) {
        for (unsigned word = 0; word < cap_words; word += 16) {
            printf("@cap %u", word);
            for (unsigned j = 0; j < 16; ++j) printf(" %08lx", (unsigned long)cap_buf[word + j]);
            putchar('\n');
        }
        printf("@capend\n");
    }
}
