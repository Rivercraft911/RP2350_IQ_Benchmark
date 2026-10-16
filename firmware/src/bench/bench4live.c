#include "bench4live.h"

#include <stdio.h>
#include <string.h>

#include "dual_dma.h"
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "iq4hist.h"
#include "iq4ptr.h"
#include "pico/stdlib.h"
#include "tx.h"

enum {
    AXIS_WORDS = IN_WORDS / 2, CAP_WORDS = 4096, POLL_US = 128, MAX_POLL_US = 256,
    ERR_POINTER = 1u << 16, ERR_CAPTURE = 1u << 17, ERR_TABLE = 1u << 18,
    ERR_CONFIG = 1u << 19, ERR_POLL = 1u << 20,
};
_Static_assert(AXIS_WORDS == 4096, "two aligned16KiB planar cyclic sources");
_Static_assert(IQ4HIST_TABLE_WORDS == N_BLOCKS * BLOCK_WORDS, "borrow the whole output ring");
_Static_assert(IQ4HIST8_TABLE_WORDS <= N_BLOCKS * BLOCK_WORDS, "byte table borrows the output ring");
_Static_assert(CAP_WORDS <= CAP_WORDS_MAX, "pin capture arena");

static bool resources_available(void) {
    unsigned free_dma = 0, sms[3] = {0};
    const PIO pios[] = {pio0, pio1, pio2};
    for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ++ch) free_dma += !dma_channel_is_claimed(ch);
    for (unsigned p = 0; p < 3; ++p)
        for (unsigned sm = 0; sm < NUM_PIO_STATE_MACHINES; ++sm) sms[p] += !pio_sm_is_claimed(pios[p], sm);
    return free_dma >= 7 && sms[0] >= 2 && sms[1] >= 1 && sms[2] >= 3;
}

static void bench4live_run(int cpw, int ms, int invert, unsigned priority, bool bytes) {
    const char *cmd = bytes ? "bench4live8" : "bench4live";
    const uint32_t sys_hz = clock_get_hz(clk_sys);
    const bool clock_ok = bytes ? (sys_hz == 128000000u || sys_hz == 256000000u) : sys_hz == 128000000u;
    const bool cpw_ok = cpw == 2 || cpw == 4 || (bytes && cpw == 1);
    if (!cpw_ok || ms < 100 || ms > 60000 || priority > 15 ||
        (invert != 0 && invert != 1) || !clock_ok ||
        (bytes && sys_hz / (2u * (unsigned)cpw) > 65000000u) ||
        ((uintptr_t)ring_buf & (128u * 1024u - 1u)) || !resources_available()) {
        printf("@{\"cmd\":\"%s\",\"error\":\"needs compatible SYS/cpw, DAC<=65MHz, aligned experiment build,100..60000ms,invert0/1,prio0..15 and idle resources\"}\n", cmd);
        return;
    }
    const unsigned saved_writers = dual_dma_get_writers(), saved_rows = dual_dma_get_row_words();
    const unsigned saved_schedule = dual_dma_get_schedule(), saved_priority = dual_dma_get_bus_priority();
    const unsigned saved_clock = dual_dma_get_dac_clock();
    const unsigned saved_sample_bits = dual_dma_get_sample_bits();
    // The first setter rejects an owned output before any borrowed source is changed.
    if (!dual_dma_set_writers(2) || !dual_dma_set_row_words(bytes ? 1 : 2) ||
        !dual_dma_set_sample_bits(bytes ? 8 : 14) ||
        !dual_dma_set_schedule(0) || !dual_dma_set_bus_priority(priority) ||
        !dual_dma_set_dac_clock(invert ? DUAL_DMA_DAC_CLOCK_DDR_PIO_INVERTED : DUAL_DMA_DAC_CLOCK_DDR_PIO)) {
        printf("@{\"cmd\":\"%s\",\"error\":\"output selectors unavailable\"}\n", cmd);
        return;
    }
    uint32_t seed = SEED;
    for (unsigned group = 0; group < AXIS_WORDS; ++group) {
        iq8_input_t planes;
        iq8_fill(&planes, 1, &seed);
        iq4hist_pack_group(&planes, in_buf + group, in_buf + AXIS_WORDS + group);
    }
    const uint32_t input_crc = crc32_update(0, in_buf, sizeof in_buf);
    const uint32_t previous[] = {in_buf[AXIS_WORDS - 1], in_buf[IN_WORDS - 1]};
    uint32_t errors = 0, table_crc = 0, elapsed = 0, max_gap = 0, polls = 0;
    uint32_t capture_count = 0, cap_crc = 0;
    uint64_t work_cycles = 0, initial[2] = {0};
    bool pointer_attempted = false, output_attempted = false, captured = false;
    bool restored = false;
    iq4ptr_progress_t p = {0};
    dual_dma_faults_t output = {0};
    const char *reason = "complete";
    const unsigned table_bytes = (bytes ? IQ4HIST8_TABLE_WORDS : IQ4HIST_TABLE_WORDS) * 4u;
    if (!(bytes ? iq4hist8_build(ring_buf, N_BLOCKS * BLOCK_WORDS) : iq4hist_build(ring_buf, N_BLOCKS * BLOCK_WORDS))) {
        errors |= ERR_TABLE; reason = "table build failed"; goto cleanup;
    }
    table_crc = crc32_update(0, ring_buf, table_bytes);
    if (table_crc != (bytes ? IQ4HIST8_TABLE_CRC : IQ4HIST_TABLE_CRC)) {
        errors |= ERR_TABLE; reason = "table CRC mismatch"; goto cleanup;
    }
    memset(cap_buf, 0xa5, CAP_WORDS * 4);
    pointer_attempted = true;
    if (!(bytes ? iq4ptr_prepare8(pio2, in_buf, in_buf + AXIS_WORDS, 12,
                                 ring_buf, previous[0], previous[1]) :
                  iq4ptr_prepare(pio2, in_buf, in_buf + AXIS_WORDS, 12,
                                 (const uint16_t *)ring_buf, previous[0], previous[1])) ||
        !iq4ptr_start()) {
        errors |= ERR_POINTER; reason = iq4ptr_last_error(); goto cleanup;
    }
    output_attempted = true;
    if (!dual_dma_start_axis_fifo(iq4ptr_rx_fifo(0), iq4ptr_rx_fifo(1),
                                  iq4ptr_rx_dreq(0), iq4ptr_rx_dreq(1), cpw)) {
        errors |= ERR_CONFIG; reason = dual_dma_last_error(); goto cleanup;
    }
    p = iq4ptr_progress();
    initial[0] = p.input_words[0]; initial[1] = p.input_words[1];
    const uint32_t start = time_us_32();
    uint32_t last_poll = start - POLL_US;
    while ((uint32_t)(time_us_32() - start) < (uint32_t)ms * 1000u) {
        const uint32_t now = time_us_32(), gap = now - last_poll;
        if (gap < POLL_US) { tight_loop_contents(); continue; }
        const uint32_t begin = cycles();
        last_poll = now; polls++;
        if (gap > max_gap) max_gap = gap;
        if (gap > MAX_POLL_US) { errors |= ERR_POLL; reason = "supervision poll deadline"; }
        p = iq4ptr_progress(); output = dual_dma_faults();
        if (p.errors) { errors |= ERR_POINTER; reason = "pointer producer fault"; }
        if (output.errors) { errors |= output.errors; reason = "output fault"; }
        if (!errors && !captured && now - start >= (uint32_t)ms * 500u) {
            captured = dual_dma_capture_pins(cap_buf, CAP_WORDS);
            if (!captured) { errors |= ERR_CAPTURE; reason = dual_dma_last_error(); }
        }
        work_cycles += (uint32_t)(cycles() - begin);
        if (errors) break;
    }
    elapsed = time_us_32() - start;
    p = iq4ptr_progress(); output = dual_dma_faults();
    if (p.errors) errors |= ERR_POINTER;
    errors |= output.errors;
cleanup:
    if (output_attempted) {
        dual_dma_stop();
        output = dual_dma_faults(); errors |= output.errors;
        capture_count = captured ? dual_dma_capture_count() : 0;
        errors |= dual_dma_capture_errors();
    }
    if (pointer_attempted) {
        iq4ptr_stop(); p = iq4ptr_progress();
        if (p.errors) errors |= ERR_POINTER;
    }
    if (capture_count != CAP_WORDS) errors |= ERR_CAPTURE;
    const bool selectors_ok = dual_dma_set_writers(2) && dual_dma_set_row_words(saved_rows) &&
        dual_dma_set_sample_bits(saved_sample_bits) &&
        dual_dma_set_writers(saved_writers) && dual_dma_set_schedule(saved_schedule) &&
        dual_dma_set_bus_priority(saved_priority) && dual_dma_set_dac_clock(saved_clock);
    if (!selectors_ok) errors |= ERR_CONFIG;
    const uint32_t input_after_crc = crc32_update(0, in_buf, sizeof in_buf);
    if (input_after_crc != input_crc) errors |= ERR_CONFIG;
    // Table construction borrows only the output ring. Existing cfg tables stay valid.
    if (!(p.errors & IQ4PTR_ERR_STOP)) { seed = SEED; prbs_fill(in_buf, IN_WORDS, &seed); restored = true; }
    const uint32_t cap_words = capture_count == CAP_WORDS ? CAP_WORDS : 0;
    if (cap_words) cap_crc = crc32_update(0, cap_buf, cap_words * 4);
    const double budget = (double)elapsed * clock_get_hz(clk_sys) / 1e6;
    printf("@{\"cmd\":\"%s\",\"git\":\"%s\",\"clk_hz\":%lu,\"cpw\":%d,\"divider256\":%d,"
           "\"ms\":%d,\"invert\":%d,\"source\":\"cyclic_8psk\",\"sps\":4,\"L\":7,"
           "\"row_profile\":\"%s\",\"row_symbols\":1,\"row_words\":%u,\"writers\":2,"
           "\"sample_bits\":%u,\"low_bits\":0,\"bus_priority\":%u,"
           "\"seed\":%lu,\"in_words\":%u,\"input_words\":%u,\"input_crc\":%lu,\"input_after_crc\":%lu,"
           "\"previous_words\":[%lu,%lu],\"input_base\":%lu,\"ring_base\":%lu,\"table_base\":%lu,"
           "\"ring_bytes\":%u,\"table_bytes\":%u,\"table_crc\":%lu,\"errors\":%lu,"
           "\"pointer_errors\":%lu,\"output_errors\":%lu,\"txstalls_i\":%lu,\"txstalls_q\":%lu,"
           "\"pointer_txstall_mask\":%lu,\"pointer_rxstall_mask\":%lu,\"reason\":\"%s\","
           "\"restored\":%s,\"selectors_restored\":%s,\"core1_idle\":true,"
           "\"elapsed_us\":%lu,\"sym_rate\":%.1f,\"supervision_cycles\":%llu,\"supervision_busy\":%.6f,"
           "\"sample_math_busy\":0.0,\"descriptor_gen_busy\":0.0,\"idle_wait\":\"spin\","
           "\"polls\":%lu,\"poll_interval_us\":%u,\"max_poll_interval_us\":%u,\"max_poll_gap_us\":%lu,"
           "\"start_input_issue_words\":[%llu,%llu],\"input_issue_words\":[%llu,%llu],"
           "\"cap_format\":\"pins32\",\"cap_words\":%lu,\"capture_count\":%lu,\"cap_crc\":%lu,"
           "\"cap_stride\":17,\"cap_divider256\":%d,\"cap_fifo_only\":false}\n",
           cmd, GIT_REV, (unsigned long)clock_get_hz(clk_sys), cpw, cpw * 256, ms, invert, IQ4HIST_PROFILE,
           bytes ? 1u : 2u, bytes ? 8u : 14u, priority,
           (unsigned long)SEED, AXIS_WORDS, IN_WORDS, (unsigned long)input_crc, (unsigned long)input_after_crc,
           (unsigned long)previous[0], (unsigned long)previous[1], (unsigned long)(uintptr_t)in_buf,
           (unsigned long)(uintptr_t)ring_buf, (unsigned long)(uintptr_t)ring_buf,
           (unsigned)sizeof ring_buf, table_bytes, (unsigned long)table_crc, (unsigned long)errors,
           (unsigned long)p.errors, (unsigned long)output.errors,
           (unsigned long)output.txstalls_i, (unsigned long)output.txstalls_q,
           (unsigned long)p.txstall_mask, (unsigned long)p.rxstall_mask, reason,
           restored ? "true" : "false", selectors_ok ? "true" : "false",
           (unsigned long)elapsed, (double)clock_get_hz(clk_sys) / (8 * cpw),
           (unsigned long long)work_cycles, budget ? work_cycles / budget : 0,
           (unsigned long)polls, POLL_US, MAX_POLL_US, (unsigned long)max_gap,
           (unsigned long long)initial[0], (unsigned long long)initial[1],
           (unsigned long long)p.input_words[0], (unsigned long long)p.input_words[1],
           (unsigned long)cap_words, (unsigned long)capture_count, (unsigned long)cap_crc, cpw * 256);
    if (cap_words) {
        for (unsigned word = 0; word < cap_words; word += 16) {
            printf("@cap %u", word);
            for (unsigned j = 0; j < 16; ++j) printf(" %08lx", (unsigned long)cap_buf[word + j]);
            putchar('\n');
        }
        printf("@capend\n");
    }
}

void bench4live(int cpw, int ms, int invert) {
    bench4live_run(cpw, ms, invert, 0, false);
}

void bench4live8(int cpw, int ms, int invert, int priority) {
    bench4live_run(cpw, ms, invert, (unsigned)priority, true);
}
