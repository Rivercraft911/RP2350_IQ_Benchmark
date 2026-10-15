#include "bench_quadlive.h"

#include <stdio.h>
#include <string.h>

#include "dual_dma.h"
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "iqquadptr.h"
#include "pico/stdlib.h"
#include "tx.h"

enum {
    CAP_WORDS = 4096, POLL_US = 128, MAX_POLL_US = 256,
    ERR_POINTER = 1u << 16, ERR_CAPTURE = 1u << 17, ERR_TABLE = 1u << 18,
    ERR_CONFIG = 1u << 19, ERR_BOUNDARY = 1u << 20, ERR_POLL = 1u << 21,
    ERR_CLEANUP = 1u << 22,
};
_Static_assert(IN_WORDS == 8192, "raw source ring alignment and size");
_Static_assert(CAP_WORDS <= CAP_WORDS_MAX, "sparse capture arena");
_Static_assert(2 * IQQUAD_L8_WORDS <= N_BLOCKS * BLOCK_WORDS, "64KiB-aligned table in ring arena");

static uint32_t poll_issue_budget(uint32_t clock_hz, unsigned cycles_per_word) {
    const uint32_t denominator = cycles_per_word * 1000000u;
    // Integer timer deltas <=MAX_POLL_US admit a real span <MAX_POLL_US+1.
    return IQQUADPTR_REUSE_GUARD_WORDS +
        ((uint64_t)clock_hz * (MAX_POLL_US + 1u) + denominator - 1u) / denominator;
}

static bool credit_runway_ok(const iqquadptr_progress_t *p, uint32_t budget, uint64_t *minimum) {
    const uint64_t remaining = p->granted_words >= p->input_high_words ?
        p->granted_words - p->input_high_words : 0;
    if (remaining < *minimum) *minimum = remaining;
    return remaining >= 2u * budget;
}

static bool resources_available(void) {
    unsigned dma = 0, sms[3] = {0};
    const PIO pios[] = {pio0, pio1, pio2};
    for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ++ch) dma += !dma_channel_is_claimed(ch);
    for (unsigned p = 0; p < 3; ++p)
        for (unsigned sm = 0; sm < NUM_PIO_STATE_MACHINES; ++sm) sms[p] += !pio_sm_is_claimed(pios[p], sm);
    return dma >= 9 && sms[0] >= 2 && sms[1] >= 1 && sms[2] >= 2;
}

static int source_channel(unsigned axis) {
    const volatile uint32_t *rx = iqquadptr_rx_fifo(axis);
    for (unsigned sm = 0; sm < NUM_PIO_STATE_MACHINES; ++sm) if (rx == &pio2->rxf[sm])
        for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ++ch)
            if (dma_channel_is_claimed(ch) && dma_hw->ch[ch].read_addr == (uintptr_t)in_buf + 2 * axis &&
                dma_hw->ch[ch].write_addr == (uintptr_t)&pio2->txf[sm]) return (int)ch;
    return -1;
}

static uint64_t observed_issues(int channel, unsigned axis, uint64_t previous, uint32_t *address) {
    *address = dma_hw->ch[channel].read_addr;
    const uint32_t cursor = ((*address - (uintptr_t)in_buf - 2 * axis) >> 2) & (IN_WORDS - 1u);
    return previous + ((cursor - (uint32_t)previous) & (IN_WORDS - 1u));
}

void bench_quadlive(int cpw, int ms, int fault) {
    if ((cpw != 1 && cpw != 2) || ms < 100 || ms > 60000 || (fault != 0 && fault != 1) ||
        iqquadptr_rx_fifo(0) || iqquadptr_rx_fifo(1) || !resources_available()) {
        printf("@{\"cmd\":\"quadlive\",\"error\":\"needs cpw1/2,100..60000ms,fault0/1 and idle pointer/output resources\"}\n");
        return;
    }
    const unsigned saved_writers = dual_dma_get_writers(), saved_rows = dual_dma_get_row_words();
    const unsigned saved_schedule = dual_dma_get_schedule(), saved_priority = dual_dma_get_bus_priority();
    const iqquad_profile_t pulse = IQQUAD_PROFILE_L8_OPTIMIZED_BOTH;
    const iqquadptr_profile_info_t *profile = iqquadptr_profile_info(pulse);
    const uint32_t clock_hz = clock_get_hz(clk_sys);
    const uint32_t issue_budget = poll_issue_budget(clock_hz, profile->cycles_per_input_word);
    const uint32_t cursor_deadline = (uint64_t)(IN_WORDS - IQQUADPTR_REUSE_GUARD_WORDS) *
        profile->cycles_per_input_word * 1000000u / clock_hz;
    const uintptr_t arena = (uintptr_t)ring_buf, arena_end = arena + sizeof ring_buf;
    const uintptr_t base = (arena + profile->table_alignment - 1u) & ~(uintptr_t)(profile->table_alignment - 1u);
    uint32_t errors = 0, seed = SEED, table_crc = 0, input_crc = 0, previous = 0;
    uint32_t elapsed = 0, max_gap = 0, max_snapshot_span = 0, max_work = 0, polls = 0, capture_count = 0;
    uint32_t source_addr[2] = {0}, cap_crc = 0, pointer_sm_mask = 0, pointer_other_pio_flags = 0;
    uint64_t work_cycles = 0, frozen_granted = 0, cap_high_words = 0, cap_low_words = 0;
    uint64_t min_credit_runway = UINT64_MAX;
    uint64_t initial_issues[2] = {0}, pre_abort_issues[2] = {0}, final_issues[2] = {0};
    int input_dma[2] = {-1, -1};
    bool pointer_attempted = false, pointer_started = false, output_attempted = false;
    bool captured = false, grants_frozen = false, beyond_grants = false, restored = false, source_stable = false;
    iqquadptr_progress_t p = {0}, live = {0};
    dual_dma_faults_t output = {0};
    const char *reason = "complete";
    if (base + 4 * IQQUAD_L8_WORDS > arena_end ||
        !dual_dma_set_writers(2) || !dual_dma_set_row_words(8) ||
        !dual_dma_set_schedule(0) || !dual_dma_set_bus_priority(0)) {
        errors |= ERR_CONFIG; reason = "L8 arena or output selectors unavailable"; goto cleanup;
    }
    prbs_fill(in_buf, IN_WORDS, &seed);
    input_crc = crc32_update(0, in_buf, sizeof in_buf);
    previous = in_buf[IN_WORDS - 1];
    memset(cap_buf, 0xa5, 4 * CAP_WORDS);
    if (!iqquadptr_build_table_profile((uint32_t *)base, (arena_end - base) / 4, pulse)) {
        errors |= ERR_TABLE; reason = "pointer table build failed"; goto cleanup;
    }
    table_crc = crc32_update(0, (const void *)base, 4 * IQQUAD_L8_WORDS);
    if (table_crc != profile->table_crc) { errors |= ERR_TABLE; reason = "pointer table CRC mismatch"; goto cleanup; }
    pointer_attempted = true;
    if (!iqquadptr_prepare_profile(pio2, in_buf, 13, (const uint32_t *)base, previous, IN_WORDS, pulse)) {
        errors |= ERR_POINTER; reason = iqquadptr_last_error(); goto cleanup;
    }
    for (unsigned axis = 0; axis < 2; ++axis) {
        input_dma[axis] = source_channel(axis);
        if (input_dma[axis] < 0) { errors |= ERR_CONFIG; reason = "input DMA identification failed"; goto cleanup; }
        for (unsigned sm = 0; sm < NUM_PIO_STATE_MACHINES; ++sm)
            if (iqquadptr_rx_fifo(axis) == &pio2->rxf[sm]) pointer_sm_mask |= 1u << sm;
    }
    if (!iqquadptr_start()) { errors |= ERR_POINTER; reason = iqquadptr_last_error(); goto cleanup; }
    pointer_started = true;
    output_attempted = true;
    if (!dual_dma_start_fifo(iqquadptr_rx_fifo(0), iqquadptr_rx_fifo(1),
                             iqquadptr_rx_dreq(0), iqquadptr_rx_dreq(1), profile->control_bswap, cpw)) {
        errors |= ERR_CONFIG; reason = dual_dma_last_error(); goto cleanup;
    }
    uint32_t last_snapshot_begin = time_us_32();
    p = iqquadptr_progress();
    initial_issues[0] = p.input_i_words; initial_issues[1] = p.input_q_words;
    if (!credit_runway_ok(&p, issue_budget, &min_credit_runway)) {
        errors |= ERR_BOUNDARY; reason = "insufficient startup source credit runway"; goto cleanup;
    }
    const uint32_t start = time_us_32();
    uint32_t last_poll = start - POLL_US;
    while ((uint32_t)(time_us_32() - start) < (uint32_t)ms * 1000u) {
        const uint32_t now = time_us_32(), gap = now - last_poll;
        if (gap < POLL_US) { tight_loop_contents(); continue; }
        const uint32_t begin = cycles();
        last_poll = now; polls++;
        if (gap > max_gap) max_gap = gap;
        if (gap > MAX_POLL_US) { errors |= ERR_POLL; reason = "supervision poll exceeded derived budget"; }
        if (fault && !grants_frozen && (uint32_t)(now - start) >= (uint32_t)ms * 250u) {
            grants_frozen = true; frozen_granted = p.granted_words;
        }
        // The source is immutable and cyclic. Publication uses the prior guarded
        // reuse bound; publish performs this poll's single live cursor snapshot.
        const uint32_t snapshot_begin = time_us_32();
        p = grants_frozen ? iqquadptr_progress() : iqquadptr_publish(p.reusable_words + IN_WORDS);
        const uint32_t snapshot_span = time_us_32() - last_snapshot_begin;
        last_snapshot_begin = snapshot_begin;
        if (snapshot_span > max_snapshot_span) max_snapshot_span = snapshot_span;
        if (snapshot_span > MAX_POLL_US) { errors |= ERR_POLL; reason = "source cursor snapshot deadline"; }
        if (!grants_frozen && !credit_runway_ok(&p, issue_budget, &min_credit_runway)) {
            errors |= ERR_BOUNDARY; reason = "insufficient source credit runway";
        }
        output = dual_dma_faults();
        beyond_grants |= p.input_high_words > p.granted_words ||
                         (grants_frozen && p.input_high_words > frozen_granted);
        if (p.errors) { errors |= ERR_POINTER; reason = "pointer producer fault"; }
        if (output.errors) { errors |= output.errors; reason = "FIFO output fault"; }
        if (beyond_grants) { errors |= ERR_BOUNDARY; reason = "source read exceeds granted prefix"; }
        if (!errors && !captured && (uint32_t)(now - start) >= (uint32_t)ms * 500u) {
            cap_high_words = p.input_high_words; cap_low_words = p.input_low_words;
            captured = dual_dma_capture(cap_buf, CAP_WORDS);
            if (!captured) { errors |= ERR_CAPTURE; reason = dual_dma_last_error(); }
        }
        const uint32_t work = cycles() - begin;
        work_cycles += work; if (work > max_work) max_work = work;
        if (errors) break;
    }
    elapsed = time_us_32() - start;
    // Latch the last active interval before output is stopped; no descriptor
    // progress API is valid for this source mode.
    p = iqquadptr_progress(); output = dual_dma_faults(); live = p;
    const uint32_t final_snapshot_span = time_us_32() - last_snapshot_begin;
    if (final_snapshot_span > max_snapshot_span) max_snapshot_span = final_snapshot_span;
    if (final_snapshot_span > MAX_POLL_US) { errors |= ERR_POLL; reason = "final source cursor snapshot deadline"; }
    if (p.errors) errors |= ERR_POINTER;
    errors |= output.errors;
cleanup:
    if (output_attempted) dual_dma_stop(); // inherited backend ABORT wait; no new timeout claim
    if (pointer_started) {
        // Output no longer drains RX. Backpressure bounds additional issue by the
        // pipeline; these source channels are still owned until producer stop.
        const uint32_t settle = time_us_32();
        do {
            const uint32_t i = dma_hw->ch[input_dma[0]].read_addr;
            const uint32_t q = dma_hw->ch[input_dma[1]].read_addr;
            busy_wait_us_32(2);
            source_stable = i == dma_hw->ch[input_dma[0]].read_addr && q == dma_hw->ch[input_dma[1]].read_addr;
        } while (!source_stable && (uint32_t)(time_us_32() - settle) < 100u);
        if (!source_stable) errors |= ERR_BOUNDARY;
        pre_abort_issues[0] = observed_issues(input_dma[0], 0, p.input_i_words, &source_addr[0]);
        pre_abort_issues[1] = observed_issues(input_dma[1], 1, p.input_q_words, &source_addr[1]);
        pointer_other_pio_flags = pio2->fdebug & ((pointer_sm_mask << PIO_FDEBUG_TXOVER_LSB) |
                                                 (pointer_sm_mask << PIO_FDEBUG_RXUNDER_LSB));
    }
    if (pointer_attempted) { iqquadptr_stop(); p = iqquadptr_progress(); }
    if (output_attempted) {
        output = dual_dma_faults(); errors |= output.errors;
        capture_count = captured ? dual_dma_capture_count() : 0;
        errors |= dual_dma_capture_errors();
    }
    final_issues[0] = p.input_i_words; final_issues[1] = p.input_q_words;
    if (p.errors) errors |= ERR_POINTER;
    beyond_grants |= final_issues[0] > p.granted_words || final_issues[1] > p.granted_words ||
        (grants_frozen && (final_issues[0] > frozen_granted || final_issues[1] > frozen_granted));
    if (beyond_grants) errors |= ERR_BOUNDARY;
    if (!fault && capture_count != CAP_WORDS) errors |= ERR_CAPTURE;
    if (fault && (!(p.errors & IQQUADPTR_ERR_SOURCE_EMPTY) || !grants_frozen)) errors |= ERR_BOUNDARY;
    const bool selectors_ok = dual_dma_set_writers(2) && dual_dma_set_row_words(saved_rows) &&
        dual_dma_set_writers(saved_writers) && dual_dma_set_schedule(saved_schedule) &&
        dual_dma_set_bus_priority(saved_priority);
    if (!selectors_ok || (p.errors & IQQUADPTR_ERR_STOP)) errors |= ERR_CLEANUP;
    const uint32_t input_after_crc = crc32_update(0, in_buf, sizeof in_buf);
    if (input_crc && input_crc != input_after_crc) errors |= ERR_BOUNDARY;
    if (!(errors & ERR_CLEANUP)) {
        if (cfg.coef) iq_build_tables(&cfg);
        seed = SEED; prbs_fill(in_buf, IN_WORDS, &seed); restored = true;
    }
    const uint32_t cap_words = capture_count == CAP_WORDS ? CAP_WORDS : 0;
    if (cap_words) cap_crc = crc32_update(0, cap_buf, 4 * cap_words);
    const double budget = (double)elapsed * clock_get_hz(clk_sys) / 1e6;
    printf("@{\"cmd\":\"quadlive\",\"git\":\"%s\",\"clk_hz\":%lu,\"source\":1,\"sps\":4,\"L\":8,"
           "\"row_profile\":\"qpsk_l8_n4_both_m010_far2\",\"filter_beta\":null,\"row_symbols\":4,"
           "\"writers\":2,\"schedule\":0,\"bus_priority_mask\":0,\"cpw\":%d,\"divider256\":%d,"
           "\"ms\":%d,\"fault\":%d,\"elapsed_us\":%lu,\"sym_rate\":%.1f,\"seed\":%lu,\"in_words\":%u,"
           "\"previous\":%lu,\"input_base\":%lu,\"input_crc\":%lu,\"input_after_crc\":%lu,"
           "\"ring_base\":%lu,\"ring_bytes\":%u,\"table_base\":%lu,\"table_bytes\":%u,\"table_crc\":%lu,"
           "\"errors\":%lu,\"pointer_errors\":%lu,\"live_pointer_errors\":%lu,\"output_errors\":%lu,"
           "\"reason\":\"%s\",\"restored\":%s,\"selectors_restored\":%s,\"core1_idle\":true,"
           "\"descriptor_gen_busy\":0.0,\"sample_math_busy\":0.0,\"supervision_cycles\":%llu,"
           "\"supervision_busy\":%.6f,\"core0_work_busy\":%.6f,\"idle_wait\":\"spin\","
           "\"polls\":%lu,\"poll_interval_us\":%u,\"max_poll_gap_us\":%lu,\"max_supervision_cycles\":%lu,"
           "\"max_source_snapshot_span_us\":%lu,"
           "\"max_poll_interval_us\":%u,\"poll_issue_budget_words\":%lu,\"required_credit_runway_words\":%lu,"
           "\"min_credit_runway_words\":%llu,\"source_cursor_deadline_us\":%lu,"
           "\"txstalls_i\":%lu,\"txstalls_q\":%lu,\"pointer_sm_mask\":%lu,\"pointer_other_pio_flags\":%lu,"
           "\"pointer_txstall_mask\":%lu,\"pointer_rxstall_mask\":%lu,"
           "\"null_trigger_mask\":%lu,\"start_input_issue_words\":[%llu,%llu],"
           "\"input_issue_words\":[%llu,%llu],\"final_input_issue_words\":[%llu,%llu],"
           "\"pre_abort_input_issue_words\":[%llu,%llu],\"pre_abort_input_read_addr\":[%lu,%lu],"
           "\"pre_abort_input_read_addr_stable\":%s,"
           "\"reusable_words\":%llu,\"published_words\":%llu,\"granted_words\":%llu,"
           "\"grants_frozen\":%s,\"frozen_granted_words\":%llu,\"read_beyond_grants\":%s,"
           "\"cap_input_issue_low\":%llu,\"cap_input_issue_high\":%llu,\"cap_symbol_cursor\":%llu,"
           "\"capture_started\":%s,\"capture_count\":%lu,\"cap_words\":%lu,\"cap_crc\":%lu,"
           "\"cap_stride\":17,\"cap_divider256\":%d,\"cap_fifo_only\":false}\n",
           GIT_REV, (unsigned long)clock_get_hz(clk_sys), cpw, cpw * 256, ms, fault,
           (unsigned long)elapsed, (double)clock_get_hz(clk_sys) / (8 * cpw), (unsigned long)SEED, IN_WORDS,
           (unsigned long)previous, (unsigned long)(uintptr_t)in_buf, (unsigned long)input_crc,
           (unsigned long)input_after_crc, (unsigned long)arena, (unsigned)sizeof ring_buf,
           (unsigned long)base, 4 * IQQUAD_L8_WORDS, (unsigned long)table_crc,
           (unsigned long)errors, (unsigned long)p.errors, (unsigned long)live.errors, (unsigned long)output.errors,
           reason, restored ? "true" : "false", selectors_ok ? "true" : "false",
           (unsigned long long)work_cycles, budget ? work_cycles / budget : 0, budget ? work_cycles / budget : 0,
           (unsigned long)polls, POLL_US, (unsigned long)max_gap, (unsigned long)max_work,
           (unsigned long)max_snapshot_span,
           MAX_POLL_US, (unsigned long)issue_budget, (unsigned long)(2u * issue_budget),
           (unsigned long long)(min_credit_runway == UINT64_MAX ? 0 : min_credit_runway), (unsigned long)cursor_deadline,
           (unsigned long)output.txstalls_i, (unsigned long)output.txstalls_q,
           (unsigned long)pointer_sm_mask, (unsigned long)pointer_other_pio_flags,
           (unsigned long)p.txstall_mask, (unsigned long)p.rxstall_mask, (unsigned long)p.null_trigger_mask,
           (unsigned long long)initial_issues[0], (unsigned long long)initial_issues[1],
           (unsigned long long)live.input_i_words, (unsigned long long)live.input_q_words,
           (unsigned long long)final_issues[0], (unsigned long long)final_issues[1],
           (unsigned long long)pre_abort_issues[0], (unsigned long long)pre_abort_issues[1],
           (unsigned long)source_addr[0], (unsigned long)source_addr[1], source_stable ? "true" : "false",
           (unsigned long long)p.reusable_words, (unsigned long long)p.published_words, (unsigned long long)p.granted_words,
           grants_frozen ? "true" : "false", (unsigned long long)frozen_granted, beyond_grants ? "true" : "false",
           (unsigned long long)cap_low_words, (unsigned long long)cap_high_words,
           (unsigned long long)(cap_high_words * 16), captured ? "true" : "false",
           (unsigned long)capture_count, (unsigned long)cap_words, (unsigned long)cap_crc, cpw * 256);
    if (cap_words) {
        for (unsigned word = 0; word < CAP_WORDS; word += 16) {
            printf("@cap %u", word);
            for (unsigned j = 0; j < 16; ++j) printf(" %08lx", (unsigned long)cap_buf[word + j]);
            putchar('\n');
        }
        printf("@capend\n");
    }
}
