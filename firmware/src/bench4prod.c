#include "bench4prod.h"

#include <stdio.h>
#include <string.h>

#include "dual_dma.h"
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "iq4hist.h"
#include "iq4prod_dma.h"
#include "iq4prod_source.h"
#include "pico/stdlib.h"
#include "tx.h"

enum {
    AXIS_WORDS = IN_WORDS / 2, CAP_WORDS = 4096, POLL_US = 128, MAX_POLL_US = 256,
    ERR_POINTER = 1u << 16, ERR_CAPTURE = 1u << 17, ERR_TABLE = 1u << 18,
    ERR_CONFIG = 1u << 19, ERR_POLL = 1u << 20, ERR_SOURCE_EMPTY = 1u << 21,
};
_Static_assert((unsigned)AXIS_WORDS == IQ4PROD_INPUT_WORDS, "two16KiB source rings");
_Static_assert(IQ4HIST8_TABLE_WORDS <= N_BLOCKS * BLOCK_WORDS, "borrow output ring");
_Static_assert(CAP_WORDS <= CAP_WORDS_MAX, "borrow capture arena");

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

void bench4prod8(int cpw, int ms, int invert, int priority, bool live, int starve_ms) {
    const char *cmd = live ? "bench4prod8live" : "bench4prod8";
    const claims_t before = claims();
    const uint32_t sys_hz = clock_get_hz(clk_sys);
    if ((cpw != 1 && cpw != 2 && cpw != 4) || ms < 100 || ms > 60000 ||
        (invert != 0 && invert != 1) || priority < 0 || priority > 15 ||
        (sys_hz != 128000000u && sys_hz != 256000000u) ||
        sys_hz / (2u * (unsigned)cpw) > 65000000u ||
        starve_ms < 0 || (starve_ms && (!live || starve_ms < 10 || starve_ms >= ms)) ||
        ((uintptr_t)ring_buf & (65536u - 1u)) || ((uintptr_t)in_buf & (4u * AXIS_WORDS - 1u)) ||
        __builtin_popcount(before.dma) > NUM_DMA_CHANNELS - 13 ||
        before.sm[0] || before.sm[2] || __builtin_popcount(before.sm[1]) > 2) {
        printf("@{\"cmd\":\"%s\",\"error\":\"needs compatible SYS/cpw, DAC<=65MHz, aligned idle experiment,100..60000ms,invert0/1,prio0..15,optional live starvation10..ms-1\"}\n", cmd);
        return;
    }
    const unsigned saved_writers = dual_dma_get_writers(), saved_rows = dual_dma_get_row_words();
    const unsigned saved_bits = dual_dma_get_sample_bits(), saved_schedule = dual_dma_get_schedule();
    const unsigned saved_priority = dual_dma_get_bus_priority(), saved_clock = dual_dma_get_dac_clock();
    if (!dual_dma_set_writers(4)) {
        printf("@{\"cmd\":\"%s\",\"error\":\"output selectors unavailable\"}\n", cmd);
        return;
    }
    uint32_t seed = SEED, previous[2] = {0}, errors = 0, table_crc = 0;
    uint32_t elapsed = 0, max_gap = 0, polls = 0, capture_count = 0, cap_crc = 0;
    uint32_t input_crc = 0, input_after_crc = 0, producer_pios[4] = {0}, producer_sms[4] = {0};
    uint64_t work_cycles = 0, source_cycles = 0, generated = 0, published = AXIS_WORDS;
    uint64_t initial[4] = {0}, cap_source_word = 0;
    bool producer_attempted = false, output_attempted = false, captured = false, restored = false;
    iq4prod_progress_t p = {0};
    dual_dma_faults_t output = {0};
    int clock_pio = -1, clock_sm = -1;
    const char *reason = "complete";
    if (!dual_dma_set_row_words(1) || !dual_dma_set_sample_bits(8) || !dual_dma_set_schedule(0) ||
        !dual_dma_set_bus_priority((unsigned)priority) ||
        !dual_dma_set_dac_clock(invert ? DUAL_DMA_DAC_CLOCK_DDR_PIO_INVERTED : DUAL_DMA_DAC_CLOCK_DDR_PIO)) {
        errors |= ERR_CONFIG; reason = dual_dma_last_error(); goto cleanup;
    }
    if (live) {
        iq4prod_source(in_buf, in_buf + AXIS_WORDS, AXIS_WORDS, &seed);
    } else {
        for (unsigned group = 0; group < AXIS_WORDS; ++group) {
            iq8_input_t planes;
            iq8_fill(&planes, 1, &seed);
            iq4hist_pack_group(&planes, in_buf + group, in_buf + AXIS_WORDS + group);
        }
        previous[0] = in_buf[AXIS_WORDS - 1]; previous[1] = in_buf[IN_WORDS - 1];
    }
    input_crc = crc32_update(0, in_buf, sizeof in_buf);
    if (!iq4hist8_build(ring_buf, N_BLOCKS * BLOCK_WORDS)) {
        errors |= ERR_TABLE; reason = "table build failed"; goto cleanup;
    }
    table_crc = crc32_update(0, ring_buf, IQ4HIST8_TABLE_WORDS * 4);
    if (table_crc != IQ4HIST8_TABLE_CRC) { errors |= ERR_TABLE; reason = "table CRC mismatch"; goto cleanup; }
    memset(cap_buf, 0xa5, CAP_WORDS * 4);
    producer_attempted = true;
    if (!iq4prod_dma_prepare(in_buf, in_buf + AXIS_WORDS, ring_buf, previous[0], previous[1], live, published) ||
        !iq4prod_dma_start()) {
        errors |= ERR_POINTER; reason = iq4prod_dma_last_error(); goto cleanup;
    }
    const volatile uint32_t *rx[4];
    unsigned dreq[4];
    for (unsigned s = 0; s < 4; ++s) {
        rx[s] = iq4prod_dma_rx_fifo(s); dreq[s] = iq4prod_dma_rx_dreq(s);
        producer_pios[s] = (unsigned)iq4prod_dma_pio_index(s);
        producer_sms[s] = (unsigned)iq4prod_dma_sm(s);
    }
    output_attempted = true;
    if (!dual_dma_start_quad_fifo(rx, dreq, (unsigned)cpw)) {
        errors |= ERR_CONFIG; reason = dual_dma_last_error(); goto cleanup;
    }
    clock_pio = dual_dma_dac_clock_pio(); clock_sm = dual_dma_dac_clock_sm();
    p = iq4prod_dma_progress();
    for (unsigned s = 0; s < 4; ++s) initial[s] = p.input_words[s];
    const uint32_t start = time_us_32();
    uint32_t last_poll = start - POLL_US;
    while ((uint32_t)(time_us_32() - start) < (uint32_t)ms * 1000u) {
        const uint32_t now = time_us_32(), gap = now - last_poll;
        if (gap < POLL_US) { tight_loop_contents(); continue; }
        const uint32_t begin = cycles();
        last_poll = now; polls++;
        if (gap > max_gap) max_gap = gap;
        if (gap > MAX_POLL_US) { errors |= ERR_POLL; reason = "supervision poll deadline"; }
        p = iq4prod_dma_progress(); output = dual_dma_faults();
        if (p.errors & IQ4PROD_ERR_SOURCE_EMPTY) { errors |= ERR_SOURCE_EMPTY; reason = "SOURCE_EMPTY"; }
        if (p.errors) { errors |= ERR_POINTER; if (!(errors & ERR_SOURCE_EMPTY)) reason = "pointer producer fault"; }
        if (output.errors) { errors |= output.errors; if (!(errors & ERR_SOURCE_EMPTY)) reason = "output fault"; }
        if (!errors && live && (!starve_ms || now - start < (uint32_t)starve_ms * 1000u) &&
            published - p.reusable_words <= AXIS_WORDS - IQ4PROD_BATCH_WORDS) {
            const unsigned offset = (unsigned)published & (AXIS_WORDS - 1u);
            const uint32_t gen_begin = cycles();
            iq4prod_source(in_buf + offset, in_buf + AXIS_WORDS + offset, IQ4PROD_BATCH_WORDS, &seed);
            source_cycles += (uint32_t)(cycles() - gen_begin);
            generated += IQ4PROD_BATCH_WORDS;
            published += IQ4PROD_BATCH_WORDS;
            p = iq4prod_dma_publish(published);
            if (p.errors) { errors |= ERR_POINTER; reason = "publication fault"; }
        }
        const uint32_t capture_at = starve_ms ? (uint32_t)starve_ms * 500u : (uint32_t)ms * 500u;
        if (!errors && !captured && now - start >= capture_at) {
            cap_source_word = p.input_words[0];
            for (unsigned s = 1; s < 4; ++s)
                if (p.input_words[s] < cap_source_word) cap_source_word = p.input_words[s];
            captured = dual_dma_capture_pins(cap_buf, CAP_WORDS);
            if (!captured) { errors |= ERR_CAPTURE; reason = dual_dma_last_error(); }
        }
        work_cycles += (uint32_t)(cycles() - begin);
        if (errors) break;
    }
    elapsed = time_us_32() - start;
cleanup:
    if (output_attempted) {
        dual_dma_stop(); output = dual_dma_faults(); errors |= output.errors;
        capture_count = captured ? dual_dma_capture_count() : 0;
        errors |= dual_dma_capture_errors();
    }
    if (producer_attempted) {
        iq4prod_dma_stop(); p = iq4prod_dma_progress();
        if (p.errors) errors |= ERR_POINTER;
        if (p.errors & IQ4PROD_ERR_SOURCE_EMPTY) { errors |= ERR_SOURCE_EMPTY; reason = "SOURCE_EMPTY"; }
    }
    if (capture_count != CAP_WORDS) errors |= ERR_CAPTURE;
    const claims_t after = claims();
    const bool released = before.dma == after.dma && before.sm[0] == after.sm[0] &&
        before.sm[1] == after.sm[1] && before.sm[2] == after.sm[2];
    if (!released) errors |= ERR_CONFIG;
    const bool selectors_ok = dual_dma_set_writers(2) && dual_dma_set_row_words(saved_rows) &&
        dual_dma_set_sample_bits(saved_bits) && dual_dma_set_writers(saved_writers) &&
        dual_dma_set_schedule(saved_schedule) && dual_dma_set_bus_priority(saved_priority) && dual_dma_set_dac_clock(saved_clock);
    if (!selectors_ok) errors |= ERR_CONFIG;
    input_after_crc = crc32_update(0, in_buf, sizeof in_buf);
    if (!live && input_after_crc != input_crc) errors |= ERR_CONFIG;
    if (released && selectors_ok) { seed = SEED; prbs_fill(in_buf, IN_WORDS, &seed); restored = true; }
    const unsigned cap_words = capture_count == CAP_WORDS ? CAP_WORDS : 0;
    if (cap_words) cap_crc = crc32_update(0, cap_buf, cap_words * 4);
    const double budget = (double)elapsed * sys_hz / 1e6;
    printf("@{\"cmd\":\"%s\",\"git\":\"%s\",\"clk_hz\":%lu,\"cpw\":%d,\"divider256\":%d,"
           "\"ms\":%d,\"invert\":%d,\"bus_priority\":%d,\"source\":\"%s\",\"source_generator\":\"%s\","
           "\"sps\":4,\"L\":7,\"row_profile\":\"%s\",\"row_symbols\":1,\"row_words\":1,\"writers\":4,"
           "\"sample_bits\":8,\"low_bits\":0,\"chains_per_axis\":2,\"producers\":4,\"pointer_source\":\"pio\","
           "\"producer_pios\":[%lu,%lu,%lu,%lu],\"producer_sms\":[%lu,%lu,%lu,%lu],\"period_symbols\":%u,"
           "\"seed\":%lu,\"in_words\":%u,\"input_words\":%u,\"input_crc\":%lu,\"input_after_crc\":%lu,"
           "\"previous_words\":[%lu,%lu],\"input_base\":%lu,\"ring_base\":%lu,\"table_base\":%lu,"
           "\"ring_bytes\":%u,\"table_bytes\":65536,\"table_crc\":%lu,\"errors\":%lu,\"pointer_errors\":%lu,"
           "\"output_errors\":%lu,\"txstalls_i\":%lu,\"txstalls_q\":%lu,\"pointer_txstall_mask\":%lu,"
           "\"pointer_rxstall_mask\":%lu,\"reason\":\"%s\",\"source_empty\":%s,\"starve_ms\":%d,"
           "\"grant_mode\":\"%s\",\"batch_words\":512,\"cursor_kind\":\"%s\",\"published_words\":%llu,"
           "\"granted_words\":[%llu,%llu,%llu,%llu],\"completed_words\":[%llu,%llu,%llu,%llu],\"reusable_words\":%llu,"
           "\"runtime_gen_words\":%llu,\"source_gen_cycles\":%llu,\"source_gen_busy\":%.6f,"
           "\"worker_polls\":%lu,\"worker_max_gap_cycles\":%lu,\"worker_service_cycles\":%llu,"
           "\"cap_source_word\":%llu,\"restored\":%s,\"selectors_restored\":%s,\"resources_released\":%s,"
           "\"core1_idle\":%s,\"dac_clock_pio\":%d,\"dac_clock_sm\":%d,\"elapsed_us\":%lu,\"sym_rate\":%.1f,"
           "\"supervision_cycles\":%llu,\"supervision_busy\":%.6f,\"sample_math_busy\":0.0,\"descriptor_gen_busy\":0.0,"
           "\"idle_wait\":\"spin\",\"polls\":%lu,\"poll_interval_us\":128,\"max_poll_interval_us\":256,\"max_poll_gap_us\":%lu,"
           "\"start_input_issue_words\":[%llu,%llu,%llu,%llu],\"input_issue_words\":[%llu,%llu,%llu,%llu],"
           "\"cap_format\":\"pins32\",\"cap_words\":%u,\"capture_count\":%lu,\"cap_crc\":%lu,\"cap_stride\":17,"
           "\"cap_divider256\":%d,\"cap_fifo_only\":false}\n",
           cmd, GIT_REV, (unsigned long)sys_hz, cpw, cpw * 256, ms, invert, priority,
           live ? "runtime_8psk_prbs" : "cyclic_8psk", live ? "xorshift32_packed_v1" : "iq8_fill",
           IQ4HIST_PROFILE, (unsigned long)producer_pios[0], (unsigned long)producer_pios[1],
           (unsigned long)producer_pios[2], (unsigned long)producer_pios[3], (unsigned long)producer_sms[0],
           (unsigned long)producer_sms[1], (unsigned long)producer_sms[2], (unsigned long)producer_sms[3],
           live ? 0 : AXIS_WORDS * 16, (unsigned long)SEED, AXIS_WORDS, IN_WORDS,
           (unsigned long)input_crc, (unsigned long)input_after_crc, (unsigned long)previous[0], (unsigned long)previous[1],
           (unsigned long)(uintptr_t)in_buf, (unsigned long)(uintptr_t)ring_buf, (unsigned long)(uintptr_t)ring_buf,
           (unsigned)sizeof ring_buf, (unsigned long)table_crc, (unsigned long)errors, (unsigned long)p.errors,
           (unsigned long)output.errors, (unsigned long)output.txstalls_i, (unsigned long)output.txstalls_q,
           (unsigned long)p.txstall_mask, (unsigned long)p.rxstall_mask, reason,
           errors & ERR_SOURCE_EMPTY ? "true" : "false", starve_ms,
           live ? "finite_software_rearm" : "endless_immutable", live ? "completed_batch_words" : "issued_words",
           (unsigned long long)published, (unsigned long long)p.granted_input_words[0],
           (unsigned long long)p.granted_input_words[1], (unsigned long long)p.granted_input_words[2],
           (unsigned long long)p.granted_input_words[3], (unsigned long long)p.input_words[0],
           (unsigned long long)p.input_words[1], (unsigned long long)p.input_words[2], (unsigned long long)p.input_words[3],
           (unsigned long long)p.reusable_words, (unsigned long long)generated, (unsigned long long)source_cycles,
           budget ? source_cycles / budget : 0, (unsigned long)p.worker_polls, (unsigned long)p.worker_max_gap_cycles,
           (unsigned long long)p.worker_service_cycles, (unsigned long long)cap_source_word,
           restored ? "true" : "false", selectors_ok ? "true" : "false", released ? "true" : "false",
           live ? "false" : "true", clock_pio, clock_sm, (unsigned long)elapsed, (double)sys_hz / (8 * cpw),
           (unsigned long long)(work_cycles - source_cycles), budget ? (work_cycles - source_cycles) / budget : 0,
           (unsigned long)polls, (unsigned long)max_gap, (unsigned long long)initial[0], (unsigned long long)initial[1],
           (unsigned long long)initial[2], (unsigned long long)initial[3], (unsigned long long)p.input_words[0],
           (unsigned long long)p.input_words[1], (unsigned long long)p.input_words[2], (unsigned long long)p.input_words[3],
           cap_words, (unsigned long)capture_count, (unsigned long)cap_crc, cpw * 256);
    if (cap_words) {
        for (unsigned word = 0; word < cap_words; word += 16) {
            printf("@cap %u", word);
            for (unsigned j = 0; j < 16; ++j) printf(" %08lx", (unsigned long)cap_buf[word + j]);
            putchar('\n');
        }
        printf("@capend\n");
    }
}
