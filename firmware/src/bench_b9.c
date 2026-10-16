#include "bench_b9.h"
#include <stdio.h>
#include <string.h>
#include "c3_fec.h"
#include "dual_dma.h"
#include "dvbs2_codes.h"
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/sync.h"
#include "iq4hist.h"
#include "iq4prod_dma.h"
#include "pico/stdlib.h"
#include "tx.h"

enum { IDLE, RUN, READY, CAP_WORDS = 4096, CAP_OFFSET = 256, SERVICE_US = 32,
       TABLE4_OFFSET = CAP_OFFSET + CAP_WORDS, TABLE4_BYTES = 24 * 1024,
       ERR_JOB = 1u << 16, ERR_SERVICE = 1u << 17, ERR_CAPTURE = 1u << 18,
       ERR_CONFIG = 1u << 19, ERR_POINTER = 1u << 20, ERR_SOURCE = 1u << 21 };
typedef struct {
    c3_job_t job;
    volatile uint32_t status;
    uint32_t frames, delay_left, frame_work, max_frame_cycles, max_slice_cycles;
    uint32_t stage_max_cycles[5];
    uint64_t busy_cycles, stage_cycles[5];
} worker_t;
typedef struct {
    worker_t core[2];
    dvbs2_8psk_cfg_t cfg;
    uint32_t gold[1384], map_lut[DVBS2_8PSK_CODES_C6_WORDS], ldpc_code[2432];
    uint32_t place_code[256];
    dvbs2_bch_leaf_t bch_leaf[2];
    dvbs2_transpose_fn transpose[2];
    uint8_t pattern[512];
    volatile uint32_t stop, errors, reusable;
    uint32_t published, groups, carry[2], slow_us, options;
    bool draining;
    uint64_t timed_begin[2], timed_busy[2];
    uint32_t hot_code[512];
    dvbs2_gcs_words_fn gcs_words;
    dvbs2_ldpc_step_fn ldpc_step;
} context_t;
static context_t *context(void) { return (context_t *)(ring_buf + 16384); }
#define live (*context())
_Static_assert(sizeof(context_t) <= 65536, "B9 context fits OUT8 reservation tail");
_Static_assert(CAP_OFFSET + CAP_WORDS <= CAP_WORDS_MAX, "capture avoids private BCH code");
_Static_assert(TABLE4_OFFSET * 4 + TABLE4_BYTES <= sizeof cap_buf,
               "compact BCH table follows capture and private code");
_Static_assert(IN_WORDS == 2 * C3_RING_GROUPS, "paired packed code rings");

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
static void release(unsigned core, uint32_t index) {
    worker_t *w = &live.core[core];
    c3_job_begin_options(&w->job, &live.cfg, index, in_buf, in_buf + C3_RING_GROUPS, live.options);
    w->job.bch_leaf = live.bch_leaf[core];
    if (live.options & C9_BCH_TABLE4) w->job.bch_table = cap_buf + TABLE4_OFFSET;
    w->job.transpose = live.transpose[core];
    w->frame_work = 0;
    w->delay_left = index < 2 ? 0 : live.slow_us;
    __dmb();
    w->status = RUN;
    __sev();
}
static bool map_credit(const c3_job_t *j) {
    // GCS/BCH/LDPC touch only private job storage. The mapper is the first
    // writer of the shared output ring. Reserve its complete interval, including
    // the partial tail, against ALL four retired DMA readers before any store.
    const uint32_t end = ((j->index + 1u) * C3_FRAME_SYMBOLS + 15u) / 16u;
    const uint32_t retired = live.reusable;
    __dmb();
    return live.draining || end - retired <= C3_RING_GROUPS;
}
static bool work(unsigned core) {
    worker_t *w = &live.core[core];
    if (w->job.stage == C3_MAP && !map_credit(&w->job)) return false;
    const uint32_t begin = cycles();
    unsigned stage = w->job.stage;
    bool ok = true;
    if (stage != C3_DONE) ok = c3_job_step_b9(&w->job, live.pattern, live.gcs_words, live.ldpc_step);
    else {
        stage = 4;
        const uint32_t take = w->delay_left > 16 ? 16 : w->delay_left;
        busy_wait_us_32(take); w->delay_left -= take;
    }
    const uint32_t spent = cycles() - begin;
    w->stage_cycles[stage] += spent; w->busy_cycles += spent; w->frame_work += spent;
    if (spent > w->stage_max_cycles[stage]) w->stage_max_cycles[stage] = spent;
    if (spent > w->max_slice_cycles) w->max_slice_cycles = spent;
    if (!ok) live.errors |= ERR_JOB;
    if (!ok || (w->job.stage == C3_DONE && !w->delay_left)) {
        ++w->frames;
        if (w->frame_work > w->max_frame_cycles) w->max_frame_cycles = w->frame_work;
        __dmb(); w->status = READY;
    }
    return true;
}
static void core1(uint32_t ignored) {
    (void)ignored; enable_cyccnt();
    while (!live.stop) {
        if (live.core[1].status == RUN) { __dmb(); if (!work(1)) __wfe(); }
        else __wfe();
    }
    __dmb();
}
static void publish(void) {
    for (;;) {
        worker_t *w = &live.core[live.published & 1u];
        if (w->status != READY) return;
        __dmb();
        if (w->job.index != live.published) { live.errors |= ERR_JOB; return; }
        live.groups = c3_job_publish(&w->job, live.carry);
        ++live.published;
        __dmb(); w->status = IDLE;
    }
}

void bench_b9(int cpw, int ms, int invert, int priority, int slow_us, int options, int code_index, int sram_hot) {
    const uint32_t hz = clock_get_hz(clk_sys);
    const claims_t before = claims();
    if (hz != 128000000u || (cpw != 1 && cpw != 2) || ms < 100 || ms > 60000 ||
        invert < 0 || invert > 1 || priority < 0 || priority > 15 || slow_us < 0 || slow_us > 10000 ||
        options < 0 || options > C9_ALL || (options & C7_TABLE3) ||
        ((options & C9_BCH_TABLE4) && (options & C7_BCH_LDM)) ||
        (code_index != S2_N2_3 && code_index != S2_N5_6 && code_index != S2_N3_4) ||
        sram_hot < 0 || sram_hot > 1 ||
        before.sm[0] || before.sm[2] ||
        __builtin_popcount(before.sm[1]) > 2 || __builtin_popcount(before.dma) > NUM_DMA_CHANNELS - 13 ||
        ((uintptr_t)ring_buf & 65535u) || ((uintptr_t)in_buf & 16383u)) {
        printf("@{\"cmd\":\"b9\",\"error\":\"needs idle SYS128,cpw1/2,100..60000ms,invert0/1,prio0..15,slow0..10000,code5/8/6,options0..16383 excluding table3; table4 excludes LDM\"}\n");
        return;
    }
    const dvbs2_code_t *code = &DVBS2_CODES[code_index];
    const unsigned saved_writers = dual_dma_get_writers(), saved_rows = dual_dma_get_row_words();
    const unsigned saved_bits = dual_dma_get_sample_bits(), saved_schedule = dual_dma_get_schedule();
    const unsigned saved_priority = dual_dma_get_bus_priority(), saved_clock = dual_dma_get_dac_clock();
    const dvbs2_bch_kernel_t saved_bch = dvbs2_bch_get_kernel();
    const unsigned saved_bank = dvbs2_frontend_get_bank();
    uint32_t errors = 0, table_crc = 0, polls = 0, max_gap = 0, elapsed = 0, accounting_elapsed = 0;
    uint32_t capture_count = 0, cap_crc = 0, stream_frames = 0, stream_groups = 0;
    uint32_t max_service_cycles = 0, gaps_over_96 = 0, max_gap_at_us = 0;
    uint64_t supervise = 0, cap_source_word = 0, start = 0, startup_us = time_us_64();
    bool worker = false, producer = false, output = false, captured = false;
    const char *reason = "complete";
    iq4prod_progress_t p = {0};
    dual_dma_faults_t out = {0};
    memset(context(), 0, sizeof live); memset(in_buf, 0, sizeof in_buf);
    live.options = (uint32_t)options; live.slow_us = (uint32_t)slow_us;
    for (unsigned n = 0; n < sizeof live.pattern; ++n) live.pattern[n] = (uint8_t)(29u * n + 7u);
    if (!dual_dma_set_writers(4) || !dual_dma_set_row_words(1) || !dual_dma_set_sample_bits(8) ||
        !dual_dma_set_schedule(0) || !dual_dma_set_bus_priority((unsigned)priority) ||
        !dual_dma_set_dac_clock(invert ? DUAL_DMA_DAC_CLOCK_DDR_PIO_INVERTED : DUAL_DMA_DAC_CLOCK_DDR_PIO)) {
        errors |= ERR_CONFIG; reason = dual_dma_last_error(); goto cleanup;
    }
    dvbs2_bch_set_kernel(S2_BCH_STREAM_ARM); dvbs2_frontend_set_bank(0);
    dvbs2_init(code, true); pv_init(65536);
    for (unsigned core = 0; core < 2; ++core) {
        uint32_t *arena = core ? cap_buf : live.place_code;
        if (options & (C7_BCH_PLACE | C9_BCH_TABLE4)) {
            live.bch_leaf[core] = dvbs2_bch_copy(arena, 512,
                options & C9_BCH_TABLE4 ? 3 : !!(options & C7_BCH_LDM));
            if (!live.bch_leaf[core]) { errors |= ERR_CONFIG; reason = "BCH placement"; goto cleanup; }
        }
        if (options & C7_TRANSPOSE) {
            live.transpose[core] = dvbs2_transpose_copy(arena + 128, 512);
            if (!live.transpose[core]) { errors |= ERR_CONFIG; reason = "transpose placement"; goto cleanup; }
        }
    }
    if ((options & C9_BCH_TABLE4) && !dvbs2_bch_table4_init(cap_buf + TABLE4_OFFSET, TABLE4_BYTES)) {
        errors |= ERR_CONFIG; reason = "BCH table"; goto cleanup;
    }
    if (sram_hot) {
        const uint32_t gcs_bytes = (dvbs2_gcs_words_code_bytes() + 3u) & ~3u;
        if (gcs_bytes > sizeof live.hot_code) { errors |= ERR_CONFIG; reason = "hot arena"; goto cleanup; }
        live.gcs_words = dvbs2_gcs_words_copy(live.hot_code, gcs_bytes);
        live.ldpc_step = dvbs2_ldpc_schedule_copy((uint8_t *)live.hot_code + gcs_bytes,
                                                 sizeof live.hot_code - gcs_bytes);
        if (!live.gcs_words || !live.ldpc_step) { errors |= ERR_CONFIG; reason = "hot placement"; goto cleanup; }
    }
    if (!iq4hist8_build(ring_buf, N_BLOCKS * BLOCK_WORDS) ||
        (table_crc = crc32_update(0, ring_buf, 65536)) != IQ4HIST8_TABLE_CRC ||
        !dvbs2_8psk_init(&live.cfg, code, true, 0, live.gold, 1384) ||
        ((options & (C4_MAP | C6_MAP)) && !dvbs2_8psk_codes_c6_init(&live.cfg, live.map_lut, DVBS2_8PSK_CODES_C6_WORDS)) ||
        ((options & (C4_LDPC | C5_LDPC | C9_LDPC_PACK)) && !dvbs2_ldpc_prepare_mode(live.ldpc_code, sizeof live.ldpc_code,
                                  options & C9_LDPC_PACK ? 3 : options & C5_LDPC ? 2 : 1))) {
        errors |= ERR_CONFIG; reason = "table or FEC setup"; goto cleanup;
    }
    enable_cyccnt(); release(0, 0); release(1, 1); tx_worker_start(core1, 0); worker = true;
    // Prefill: publish frames 0 and 1, and start frames 2 and 3 as soon as each core is free.
    // Output starts once core 0's frame 2 reaches its credit-gated mapper, so frames 2/3 get
    // their GCS/BCH/LDPC done before the 2-frame output runway begins to drain. On c87c54dfbf
    // (frames 2/3 released after output start) 16 Msym/s ran dry after the two prefill frames.
    while ((live.published < 2 || live.core[0].job.index < 2 || live.core[0].job.stage < C3_MAP) &&
           !live.errors) {
        if (live.core[0].status == RUN) work(0);
        publish();
        for (unsigned c = 0; c < 2; ++c)
            if (live.core[c].status == IDLE && live.core[c].job.index < 2) release(c, live.core[c].job.index + 2u);
    }
    if (live.errors) { errors |= live.errors; reason = "prefill FEC"; goto cleanup; }
    producer = true;
    if (!iq4prod_dma_prepare_irq(in_buf, in_buf + C3_RING_GROUPS, ring_buf, 0, 0, live.groups) ||
        !iq4prod_dma_start()) { errors |= ERR_POINTER; reason = iq4prod_dma_last_error(); goto cleanup; }
    const volatile uint32_t *rx[4]; unsigned dreq[4];
    for (unsigned s = 0; s < 4; ++s) { rx[s] = iq4prod_dma_rx_fifo(s); dreq[s] = iq4prod_dma_rx_dreq(s); }
    output = true;
    if (!dual_dma_start_quad_fifo(rx, dreq, (unsigned)cpw)) { errors |= ERR_CONFIG; reason = dual_dma_last_error(); goto cleanup; }
    start = time_us_64(); startup_us = start - startup_us;
    for (unsigned c = 0; c < 2; ++c) live.timed_begin[c] = live.core[c].busy_cycles;
    uint64_t next_service = start, previous = start;
    while (time_us_64() - start < (uint32_t)ms * 1000u) {
        const uint32_t begin = cycles(); const uint64_t work_before = live.core[0].busy_cycles;
        bool worked = false, serviced = false;
        const uint64_t now = time_us_64();
        if (now >= next_service) {
            const uint32_t service_begin = cycles();
            serviced = true;
            const uint32_t gap = (uint32_t)(now - previous);
            if (gap > max_gap) { max_gap = gap; max_gap_at_us = (uint32_t)(now - start); }
            if (gap > 96) ++gaps_over_96;
            previous = now; next_service = now + SERVICE_US; ++polls;
            if (gap > 128) { errors |= ERR_SERVICE; reason = "supervision gap"; }
            // Register fault sampling (PIO FDEBUG, DMA INTR) every fourth service, about
            // 128 us; the other services read only memory. On c87c54dfbf a full service cost
            // about 840 cycles at 16 Msym/s, about 20% of core 0 at a 32-us cadence.
            const bool full = (polls & 3u) == 1u;
            const iq4prod_irq_poll_t q = full ? iq4prod_dma_poll_irq() : iq4prod_dma_poll_light();
            uint32_t pointer_errors = q.errors;
            if (full) out = dual_dma_faults();
            live.reusable = q.reusable_words; __dmb(); __sev();
            publish();
            if (live.groups != q.published_words) {
                p = iq4prod_dma_publish(live.groups);
                pointer_errors |= p.errors;
            }
            errors |= live.errors | out.errors;
            if (pointer_errors) { errors |= ERR_POINTER; reason = "producer fault"; }
            if (pointer_errors & IQ4PROD_ERR_SOURCE_EMPTY) { errors |= ERR_SOURCE; reason = "SOURCE_EMPTY"; }
            if (out.errors && !(errors & ERR_SOURCE)) reason = "output fault";
            if (!errors) for (unsigned c = 0; c < 2; ++c)
                if (live.core[c].status == IDLE) release(c, live.core[c].job.index + 2u);
            const uint32_t capture_at = slow_us ? 500u : (uint32_t)ms * 500u;
            if (!errors && !captured && now - start >= capture_at) {
                cap_source_word = q.reusable_words;
                captured = dual_dma_capture_pins(cap_buf + CAP_OFFSET, CAP_WORDS);
                if (!captured) { errors |= ERR_CAPTURE; reason = dual_dma_last_error(); }
            }
            const uint32_t service_spent = cycles() - service_begin;
            if (service_spent > max_service_cycles) max_service_cycles = service_spent;
        }
        // Recheck time after service before starting an encoder slice: publication,
        // release or capture setup can themselves consume the remaining service runway.
        else worked = live.core[0].status == RUN && work(0);
        supervise += (uint32_t)(cycles() - begin) - (uint32_t)(live.core[0].busy_cycles - work_before);
        if (errors) break;
        if (serviced) continue;
        if (!worked) { // idle until the next service; excluded from supervise, IRQs stay enabled
            const uint64_t t = time_us_64();
            busy_wait_us_32(t < next_service ? (uint32_t)(next_service - t) : 1u);
        }
    }
    elapsed = (uint32_t)(time_us_64() - start);
cleanup:
    // No source storage or private code can be reused until BOTH output and
    // producer DMA readers have stopped. Core1 is joined before deterministic drain.
    if (output) { dual_dma_stop(); out = dual_dma_faults(); errors |= out.errors;
        capture_count = captured ? dual_dma_capture_count() : 0; errors |= dual_dma_capture_errors(); }
    if (producer) { iq4prod_dma_stop(); p = iq4prod_dma_progress();
        if (p.errors) errors |= ERR_POINTER;
        if (p.errors & IQ4PROD_ERR_SOURCE_EMPTY) { errors |= ERR_SOURCE; reason = "SOURCE_EMPTY"; } }
    if (worker) { live.stop = 1; __dmb(); __sev(); tx_worker_wait(); }
    for (unsigned c = 0; c < 2; ++c) live.timed_busy[c] = live.core[c].busy_cycles - live.timed_begin[c];
    if (start) accounting_elapsed = (uint32_t)(time_us_64() - start);
    stream_frames = live.published; stream_groups = live.groups;
    const claims_t after = claims();
    const bool released = before.dma == after.dma && before.sm[0] == after.sm[0] &&
        before.sm[1] == after.sm[1] && before.sm[2] == after.sm[2];
    if (!released) errors |= ERR_CONFIG;
    if (released && worker && !(live.errors & ERR_JOB)) {
        // Outside the measured interval, finish the two admitted frames without
        // new releases. This makes the final ring CRC independently reproducible.
        live.draining = true;
        for (unsigned c = 0; c < 2; ++c) while (live.core[c].status == RUN) work(c);
        publish();
    }
    const uint32_t crc_i = pv_crc32(0, in_buf, C3_RING_GROUPS * 4);
    const uint32_t crc_q = pv_crc32(0, in_buf + C3_RING_GROUPS, C3_RING_GROUPS * 4);
    if (capture_count != CAP_WORDS) errors |= ERR_CAPTURE;
    if (capture_count == CAP_WORDS) cap_crc = crc32_update(0, cap_buf + CAP_OFFSET, CAP_WORDS * 4);
    const bool selected = dual_dma_set_writers(2) && dual_dma_set_row_words(saved_rows) &&
        dual_dma_set_sample_bits(saved_bits) && dual_dma_set_writers(saved_writers) &&
        dual_dma_set_schedule(saved_schedule) && dual_dma_set_bus_priority(saved_priority) && dual_dma_set_dac_clock(saved_clock);
    if (!selected) errors |= ERR_CONFIG;
    dvbs2_bch_set_kernel(saved_bch); dvbs2_frontend_set_bank(saved_bank);
    const double budget = (double)accounting_elapsed * hz / 1e6;
    if (released && selected) { uint32_t seed = SEED; prbs_fill(in_buf, IN_WORDS, &seed); }
    printf("@{\"cmd\":\"b9\",\"git\":\"%s\",\"clk_hz\":%lu,\"cpw\":%d,\"ms\":%d,\"invert\":%d,\"bus_priority\":%d,\"slow_us\":%d,\"options\":%d,"
           "\"s2_code\":\"%s\",\"sps\":4,\"L\":7,\"sample_bits\":8,\"row_profile\":\"%s\",\"row_symbols\":1,\"row_words\":1,\"writers\":4,\"producers\":4,\"chains_per_axis\":2,\"pointer_source\":\"pio\",\"table_bytes\":65536,\"previous_words\":[0,0],\"frame_cycles\":%u,\"sym_rate\":%.1f,"
           "\"source\":\"memory_gcs\",\"code_index\":%d,\"kbch\":%u,\"payload_bits\":%u,\"payload_bps\":%.3f,\"source_period_bytes\":256,\"pilots\":true,\"gold\":0,\"frame_symbols\":22194,"
           "\"ring_groups\":4096,\"published_frames\":%lu,\"published_groups\":%lu,\"pending_fill\":%lu,\"stream_frames\":%lu,\"stream_groups\":%lu,"
           "\"code_crc\":[%lu,%lu],\"table_crc\":%lu,\"table_base\":%lu,\"context_base\":%lu,\"context_bytes\":%u,\"input_base\":%lu,\"capture_base\":%lu,"
           "\"errors\":%lu,\"pointer_errors\":%lu,\"output_errors\":%lu,\"txstalls_i\":%lu,\"txstalls_q\":%lu,\"pointer_txstall_mask\":%lu,\"pointer_rxstall_mask\":%lu,"
           "\"source_empty\":%s,\"reason\":\"%s\",\"grant_mode\":\"finite_irq_rearm\",\"batch_words\":512,\"cursor_kind\":\"completed_batch_words\",\"published_words\":%llu,"
           "\"reusable_words\":%llu,\"granted_words\":[%llu,%llu,%llu,%llu],\"completed_words\":[%llu,%llu,%llu,%llu],\"input_issue_words\":[%llu,%llu,%llu,%llu],"
           "\"cap_source_word\":%llu,\"cap_format\":\"pins32\",\"cap_stride\":17,\"cap_words\":%u,\"capture_count\":%lu,\"cap_crc\":%lu,"
           "\"elapsed_us\":%lu,\"accounting_elapsed_us\":%lu,\"startup_us\":%llu,\"polls\":%lu,\"max_service_gap_us\":%lu,\"supervise_cycles\":%llu,\"supervise_busy\":%.6f,"
           "\"grant_irq_calls\":%lu,\"grant_irq_cycles\":%llu,\"grant_irq_max_cycles\":%lu,\"grant_irq_index\":%d,\"worker_polls\":%lu,\"worker_max_gap_cycles\":%lu,\"worker_service_cycles\":%llu,\"resources_released\":%s,\"selectors_restored\":%s,\"restored\":%s,",
           GIT_REV, (unsigned long)hz, cpw, ms, invert, priority, slow_us, options,
           code->name, IQ4HIST_PROFILE, C3_FRAME_CYCLES * cpw, (double)hz / (8 * cpw),
           code_index, code->kbch, code->kbch - 80u,
           (double)(code->kbch - 80u) * hz / (C3_FRAME_CYCLES * cpw),
           (unsigned long)live.published, (unsigned long)live.groups, (unsigned long)(live.published * C3_FRAME_SYMBOLS & 15u),
           (unsigned long)stream_frames, (unsigned long)stream_groups, (unsigned long)crc_i, (unsigned long)crc_q,
           (unsigned long)table_crc, (unsigned long)(uintptr_t)ring_buf, (unsigned long)(uintptr_t)context(), (unsigned)sizeof live,
           (unsigned long)(uintptr_t)in_buf, (unsigned long)(uintptr_t)(cap_buf + CAP_OFFSET),
           (unsigned long)errors, (unsigned long)p.errors, (unsigned long)out.errors, (unsigned long)out.txstalls_i, (unsigned long)out.txstalls_q,
           (unsigned long)p.txstall_mask, (unsigned long)p.rxstall_mask, errors & ERR_SOURCE ? "true" : "false", reason,
           (unsigned long long)p.published_words, (unsigned long long)p.reusable_words,
           (unsigned long long)p.granted_input_words[0], (unsigned long long)p.granted_input_words[1], (unsigned long long)p.granted_input_words[2], (unsigned long long)p.granted_input_words[3],
           (unsigned long long)p.input_words[0], (unsigned long long)p.input_words[1], (unsigned long long)p.input_words[2], (unsigned long long)p.input_words[3],
           (unsigned long long)p.input_words[0], (unsigned long long)p.input_words[1], (unsigned long long)p.input_words[2], (unsigned long long)p.input_words[3],
           (unsigned long long)cap_source_word, capture_count == CAP_WORDS ? CAP_WORDS : 0, (unsigned long)capture_count, (unsigned long)cap_crc,
           (unsigned long)elapsed, (unsigned long)accounting_elapsed, (unsigned long long)startup_us, (unsigned long)polls, (unsigned long)max_gap,
           (unsigned long long)supervise, budget ? supervise / budget : 0,
           (unsigned long)p.irq_calls, (unsigned long long)p.irq_service_cycles,
           (unsigned long)p.irq_max_service_cycles, p.irq_index, (unsigned long)p.worker_polls, (unsigned long)p.worker_max_gap_cycles, (unsigned long long)p.worker_service_cycles,
           released ? "true" : "false", selected ? "true" : "false", released && selected ? "true" : "false");
    printf("\"sram_hot\":%d,\"hot_base\":%lu,\"hot_bytes\":%u,"
           "\"max_service_cycles\":%lu,\"gaps_over_96\":%lu,\"max_gap_at_us\":%lu,"
           "\"gold_base\":%lu,\"map_base\":%lu,\"ldpc_base\":%lu,\"ldpc_bytes\":%u,"
           "\"bch_base\":[%lu,%lu],\"bch_table_base\":%lu,\"bch_table_bytes\":%lu,\"core\":[",
           sram_hot, (unsigned long)(uintptr_t)live.hot_code, (unsigned)sizeof live.hot_code,
           (unsigned long)max_service_cycles, (unsigned long)gaps_over_96, (unsigned long)max_gap_at_us,
           (unsigned long)(uintptr_t)live.gold, (unsigned long)(uintptr_t)live.map_lut,
           (unsigned long)(uintptr_t)live.ldpc_code, (unsigned)sizeof live.ldpc_code,
           (unsigned long)(uintptr_t)live.place_code, (unsigned long)(uintptr_t)cap_buf,
           (unsigned long)(uintptr_t)(cap_buf + TABLE4_OFFSET),
           (unsigned long)((options & C9_BCH_TABLE4) ? dvbs2_bch_table4_bytes() : 0));
    for (unsigned c = 0; c < 2; ++c) {
        const worker_t *w = &live.core[c];
        printf("%s{\"frames\":%lu,\"busy_cycles\":%llu,\"busy\":%.6f,\"max_frame_cycles\":%lu,\"max_slice_cycles\":%lu,\"timed_busy_cycles\":%llu,\"stage_max_cycles\":[%lu,%lu,%lu,%lu,%lu],\"stage_cycles\":{\"gcs\":%llu,\"bch\":%llu,\"ldpc\":%llu,\"map\":%llu,\"delay\":%llu}}",
               c ? "," : "", (unsigned long)w->frames, (unsigned long long)w->busy_cycles,
               budget ? (live.timed_busy[c] + (c ? 0 : supervise)) / budget : 0,
               (unsigned long)w->max_frame_cycles, (unsigned long)w->max_slice_cycles, (unsigned long long)live.timed_busy[c],
               (unsigned long)w->stage_max_cycles[0], (unsigned long)w->stage_max_cycles[1],
               (unsigned long)w->stage_max_cycles[2], (unsigned long)w->stage_max_cycles[3], (unsigned long)w->stage_max_cycles[4],
               (unsigned long long)w->stage_cycles[0], (unsigned long long)w->stage_cycles[1],
               (unsigned long long)w->stage_cycles[2], (unsigned long long)w->stage_cycles[3], (unsigned long long)w->stage_cycles[4]);
    }
    printf("]}\n");
    if (capture_count == CAP_WORDS) {
        for (unsigned n = 0; n < CAP_WORDS; n += 16) {
            printf("@cap %u", n);
            for (unsigned j = 0; j < 16; ++j) printf(" %08lx", (unsigned long)cap_buf[CAP_OFFSET + n + j]);
            putchar('\n');
        }
        printf("@capend\n");
    }
}
