// B8: bounded single-consumer SPI ingress, frame-parallel FEC, ordered packed
// publication and B7's self-rearming two-axis OUT8 graph. All arenas are borrowed.
#include "b8_lean8.h"
#include "b8_ingress.h"
#include "c3_fec.h"
#include "dual_dma.h"
#include "dvbs2_codes.h"
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/pio.h"
#include "hardware/sync.h"
#include "iq4hist.h"
#include "iq4ptr.h"
#include "link.h"
#include "pico/stdlib.h"
#include "tx.h"
#include <limits.h>
#include <stdio.h>
#include <string.h>

enum { IDLE, FILL, RUN, READY, SERVICE_US = 64, MAX_SERVICE_US = 128,
       CAP_WORDS = 4096, PREFILL = 2, ERR_JOB = 1u << 16, ERR_SPI = 1u << 17,
       ERR_SERVICE = 1u << 18, ERR_SLICE = 1u << 19, ERR_SETUP = 1u << 20 };
typedef struct {
    c3_job_t job;
    volatile uint32_t status, error;
    uint32_t frames, max_slice_cycles, max_frame_cycles, frame_work;
    uint64_t busy_cycles, stage_cycles[4];
} b8_worker_t;
typedef struct {
    b8_worker_t core[2];
    dvbs2_8psk_cfg_t cfg;
    uint32_t gold[1384], map_lut[DVBS2_8PSK_CODES_LUT_WORDS], ldpc_code[1792];
    b8_ingress_t ingress;
    volatile uint32_t stop;
    uint32_t next, published, groups, carry[2], options, hz;
    uint64_t supervise_cycles;
} b8_context_t;
static b8_context_t *context(void) { return (b8_context_t *)(ring_buf + 16384); }
#define live (*context())
_Static_assert(sizeof(b8_context_t) <= 65536, "B8 uses only the OUT8 table tail");
_Static_assert(IN_WORDS == 2 * C3_RING_GROUPS, "paired axis rings");
_Static_assert(PV_SLOTS * PV_MSG_WORDS <= LINK_RING_WORDS, "SPI slots fit link ring");

static bool transport_failed(void) {
    return pvspi.errors || pvspi.short_msgs || pvspi.long_msgs || pvspi.overflows ||
        pvspi.drain_timeouts || pvspi.late_closes || pv_stats.lost || pv_stats.bad_hdr ||
        pv_stats.bad_crc || pv_stats.bad_sync;
}

static void charge(b8_worker_t *w, unsigned stage, uint32_t spent) {
    w->busy_cycles += spent;
    w->stage_cycles[stage] += spent;
    w->frame_work += spent;
    if (spent > w->max_slice_cycles) w->max_slice_cycles = spent;
    if (spent > live.hz / 1000000u * MAX_SERVICE_US) w->error |= ERR_SLICE;
}

static void work(unsigned core) {
    b8_worker_t *w = &live.core[core];
    const unsigned stage = w->job.stage;
    const uint32_t start = cycles();
    // The source stage belongs solely to core0's bounded real-SPI adapter.
    const bool ok = stage >= C3_BCH && stage < C3_DONE && c3_job_step(&w->job, NULL);
    charge(w, stage >= C3_BCH && stage < C3_DONE ? stage : C3_BCH, cycles() - start);
    if (!ok) w->error |= ERR_JOB;
    if (!ok || w->job.stage == C3_DONE) {
        if (ok) ++w->frames;
        if (w->frame_work > w->max_frame_cycles) w->max_frame_cycles = w->frame_work;
        __dmb();
        w->status = READY;
    }
}

static void core1(uint32_t unused) {
    (void)unused;
    enable_cyccnt();
    while (!live.stop) {
        if (live.core[1].status == RUN) {
            __dmb();
            const uint32_t start = cycles();
            const uint64_t before = live.core[1].busy_cycles;
            work(1);
            live.core[1].busy_cycles = before + (uint32_t)(cycles() - start);
        } else __wfe();
    }
    __dmb();
}

static void begin_frame(unsigned index) {
    b8_worker_t *w = &live.core[index & 1u];
    c3_job_begin_options(&w->job, &live.cfg, index, in_buf, in_buf + C3_RING_GROUPS, live.options);
    w->frame_work = 0;
    w->status = FILL;
    ++live.next;
}

static void feed(b8_worker_t *w) {
    const uint32_t start = cycles();
    const int result = b8_ingress_step(&live.ingress, &w->job, pvspi_next_packet, 64);
    charge(&live.core[0], C3_GCS, cycles() - start);
    if (result == B8_INGRESS_ERROR) w->error |= ERR_SPI;
    if (result == B8_INGRESS_READY) {
        __dmb();
        w->status = RUN;
        __sev();
    }
}

static void publish(void) {
    while (live.published < live.next) {
        b8_worker_t *w = &live.core[live.published & 1u];
        if (w->status != READY || w->error) break;
        __dmb();
        if (w->job.index != live.published) { w->error |= ERR_JOB; break; }
        live.groups = c3_job_publish(&w->job, live.carry);
        __dmb();
        ++live.published;
        w->status = IDLE;
    }
}

static bool resources_available(void) {
    unsigned dma = 0, sm[3] = {0};
    const PIO pios[] = {pio0, pio1, pio2};
    for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ++ch) dma += !dma_channel_is_claimed(ch);
    for (unsigned p = 0; p < 3; ++p)
        for (unsigned n = 0; n < 4; ++n) sm[p] += !pio_sm_is_claimed(pios[p], n);
    return dma >= 12 && sm[0] >= 2 && sm[1] == 4 && sm[2] >= 3;
}

void bench_b8_lean8(int code_index, int ms, int cpw, int invert, int priority, int options, int starve) {
    const uint32_t hz = clock_get_hz(clk_sys);
    if ((code_index != S2_N2_3 && code_index != S2_N3_4 && code_index != S2_N5_6) ||
        ms < 100 || ms > 60000 || (cpw != 1 && cpw != 2 && cpw != 4) ||
        invert < 0 || invert > 1 || priority < 0 || priority > 15 || options < 0 || options > C4_ALL ||
        starve < 0 || starve > 1 || hz != 128000000u ||
        ((uintptr_t)ring_buf & 131071u) || !resources_available()) {
        printf("@{\"cmd\":\"b8lean8\",\"error\":\"needs idle SYS128 experiment, code5/6/8, ms100..60000, cpw1/2/4, invert0/1, prio0..15, options0..31, starve0/1\"}\n");
        return;
    }
    const iq_kernel_info_t *unused;
    bool tables_ok;
    if (tx_shaper("lut_asm_p", 4, 12, &unused, &tables_ok) || !tables_ok) {
        printf("@{\"cmd\":\"b8lean8\",\"error\":\"initial tables unavailable\"}\n"); return;
    }
    const unsigned saved_writers = dual_dma_get_writers(), saved_rows = dual_dma_get_row_words();
    const unsigned saved_bits = dual_dma_get_sample_bits(), saved_schedule = dual_dma_get_schedule();
    const unsigned saved_priority = dual_dma_get_bus_priority(), saved_clock = dual_dma_get_dac_clock();
    const dvbs2_bch_kernel_t saved_bch = dvbs2_bch_get_kernel();
    const unsigned saved_bank = dvbs2_frontend_get_bank();
    memset(context(), 0, sizeof live);
    memset(in_buf, 0, sizeof in_buf);
    live.options = (unsigned)options; live.hz = hz;
    b8_ingress_init(&live.ingress);
    uint32_t errors = 0, elapsed = 0, max_gap = 0, table_crc = 0, capture_count = 0;
    uint32_t bb_prefill = 0, spi_head_start = 0, cap_crc = 0;
    uint64_t cap_lo = 0, cap_hi = 0;
    int64_t min_runway = INT64_MAX;
    bool spi_started = false, worker_started = false, pointer_attempted = false;
    bool output_attempted = false, captured = false;
    iq4ptr_progress_t p = {0};
    dual_dma_faults_t output = {0};
    const char *reason = "complete";
    if (!dual_dma_set_writers(2) || !dual_dma_set_row_words(1) || !dual_dma_set_sample_bits(8) ||
        !dual_dma_set_schedule(0) || !dual_dma_set_bus_priority((unsigned)priority) ||
        !dual_dma_set_dac_clock(invert ? DUAL_DMA_DAC_CLOCK_DDR_PIO_INVERTED : DUAL_DMA_DAC_CLOCK_DDR_PIO) ||
        !iq4hist8_build(ring_buf, N_BLOCKS * BLOCK_WORDS)) {
        errors |= ERR_SETUP; reason = "output/table setup"; goto cleanup;
    }
    table_crc = crc32_update(0, ring_buf, IQ4HIST8_TABLE_WORDS * 4u);
    if (table_crc != IQ4HIST8_TABLE_CRC) { errors |= ERR_SETUP; reason = "table CRC"; goto cleanup; }
    dvbs2_bch_set_kernel(S2_BCH_STREAM_ARM);
    dvbs2_frontend_set_bank(0);
    dvbs2_init(&DVBS2_CODES[code_index], true);
    pv_init(PV_EMU_MSGS);
    if (!dvbs2_8psk_init(&live.cfg, &DVBS2_CODES[code_index], true, 0, live.gold, 1384) ||
        ((options & C4_MAP) && !dvbs2_8psk_codes_lut_init(&live.cfg, live.map_lut, DVBS2_8PSK_CODES_LUT_WORDS)) ||
        ((options & C4_LDPC) && !dvbs2_ldpc_prepare(live.ldpc_code, sizeof live.ldpc_code))) {
        errors |= ERR_SETUP; reason = "FEC/mapper setup"; goto cleanup;
    }
    pvspi_start(pio1, link_ring, true, cfg.tq, 2);
    spi_started = true;
    enable_cyccnt();
    // Startup is outside utilization. Prepare two complete continuous frames,
    // preserving the partial boundary in carry; no padded frame is emitted.
    for (unsigned frame = 0; frame < PREFILL; ++frame) {
        begin_frame(frame);
        b8_worker_t *w = &live.core[frame & 1u];
        const uint64_t timeout = time_us_64() + 100000u;
        while (w->status == FILL && !w->error && time_us_64() < timeout) feed(w);
        while (w->status == RUN && !w->error) work(frame & 1u);
        if (w->status != READY || w->error || transport_failed()) {
            errors |= ERR_SPI | w->error; reason = "SPI/FEC prefill"; goto cleanup;
        }
        publish();
    }
    bb_prefill = pv_stats.bbframes; spi_head_start = pvspi.head;
    for (unsigned c = 0; c < 2; ++c) {
        b8_worker_t *w = &live.core[c];
        w->frames = w->max_slice_cycles = w->max_frame_cycles = w->frame_work = 0;
        w->busy_cycles = 0; memset(w->stage_cycles, 0, sizeof w->stage_cycles);
    }
    pointer_attempted = true;
    if (!iq4ptr_prepare_live8(pio2, in_buf, in_buf + C3_RING_GROUPS, 12, ring_buf, 0, 0xaaaaaaaau, live.groups) ||
        !iq4ptr_start()) { errors |= ERR_SETUP; reason = iq4ptr_last_error(); goto cleanup; }
    output_attempted = true;
    if (!dual_dma_start_axis_fifo(iq4ptr_rx_fifo(0), iq4ptr_rx_fifo(1), iq4ptr_rx_dreq(0), iq4ptr_rx_dreq(1), cpw)) {
        errors |= ERR_SETUP; reason = dual_dma_last_error(); goto cleanup;
    }
    p = iq4ptr_progress();
    tx_worker_start(core1, 0); worker_started = true;
    const uint32_t start = time_us_32();
    uint32_t previous = start, next_service = start;
    while ((uint32_t)(time_us_32() - start) < (uint32_t)ms * 1000u) {
        const uint32_t loop_begin = cycles();
        const uint64_t work_before = live.core[0].busy_cycles;
        const uint32_t now = time_us_32();
        if ((int32_t)(now - next_service) >= 0) {
            const uint32_t gap = now - previous;
            if (gap > max_gap) max_gap = gap;
            previous = now; next_service = now + SERVICE_US;
            if (gap > MAX_SERVICE_US) errors |= ERR_SERVICE;
            publish();
            p = iq4ptr_publish(live.groups);
            output = dual_dma_faults();
            const uint64_t high = p.input_words[0] > p.input_words[1] ? p.input_words[0] : p.input_words[1];
            const int64_t runway = (int64_t)p.granted_words - (int64_t)high;
            if (runway < min_runway) min_runway = runway;
            errors |= p.errors | output.errors | live.core[0].error | live.core[1].error;
            if (transport_failed()) errors |= ERR_SPI;
            if (!errors && !captured && now - start >= (uint32_t)ms * 500u) {
                const uint64_t low = p.input_words[0] < p.input_words[1] ? p.input_words[0] : p.input_words[1];
                cap_lo = low * 16u;
                captured = dual_dma_capture_pins(cap_buf, CAP_WORDS);
                if (!captured) errors |= DUAL_DMA_ERR_CAPTURE_DMA;
                const iq4ptr_progress_t after = iq4ptr_progress();
                cap_hi = (after.input_words[0] > after.input_words[1] ? after.input_words[0] : after.input_words[1]) * 16u;
            }
        }
        // Reserve the entire future frame before the mapper may write any word.
        // Ceiling includes the deferred boundary/tail; reuse is guarded retirement.
        const uint32_t end = ((live.next + 1u) * C3_FRAME_SYMBOLS + 15u) / 16u;
        b8_worker_t *next = &live.core[live.next & 1u];
        bool filling = live.core[0].status == FILL || live.core[1].status == FILL;
        if (!errors && !starve && !filling && next->status == IDLE &&
            end <= p.reusable_words + C3_RING_GROUPS) { begin_frame(live.next); filling = true; }
        bool worked = false;
        if (!errors && filling) {
            b8_worker_t *w = live.core[0].status == FILL ? &live.core[0] : &live.core[1];
            feed(w);
            worked = true;
        } else if (!errors && live.core[0].status == RUN) { work(0); worked = true; }
        live.supervise_cycles += (uint32_t)(cycles() - loop_begin) -
            (uint32_t)(live.core[0].busy_cycles - work_before);
        if (errors) { reason = starve && (p.errors & IQ4PTR_ERR_SOURCE_EMPTY) ? "SOURCE_EMPTY" : "live fault"; break; }
        // Charge actual service/dispatch, but exclude reserved idle time from
        // utilization. Never sleep through the next64us output service.
        const uint32_t after = time_us_32();
        if (!worked && (int32_t)(next_service - after) > 0) busy_wait_us_32(next_service - after);
    }
    elapsed = time_us_32() - start;
cleanup:
    // Quiesce pins before draining admitted jobs. No storage is reused while a
    // worker or DMA can still reference it; all mapped frames publish in order.
    if (output_attempted) {
        dual_dma_stop(); output = dual_dma_faults(); errors |= output.errors;
        capture_count = captured ? dual_dma_capture_count() : 0;
        errors |= dual_dma_capture_errors();
    }
    if (pointer_attempted) { iq4ptr_stop(); p = iq4ptr_progress(); errors |= p.errors; }
    if (worker_started) {
        const uint64_t timeout = time_us_64() + 100000u;
        for (unsigned c = 0; c < 2; ++c) {
            while (live.core[c].status == FILL && !live.core[c].error && time_us_64() < timeout) feed(&live.core[c]);
            if (live.core[c].status == FILL) errors |= ERR_SPI;
        }
        while (live.core[0].status == RUN) work(0);
        while (live.core[1].status == RUN) tight_loop_contents();
        live.stop = 1; __dmb(); __sev(); tx_worker_wait();
        publish();
    }
    if (spi_started) { pvspi_pause(); pvspi_stop(); if (transport_failed()) errors |= ERR_SPI; }
    errors |= live.core[0].error | live.core[1].error;
    const bool selectors_ok = dual_dma_set_writers(2) && dual_dma_set_row_words(saved_rows) &&
        dual_dma_set_sample_bits(saved_bits) && dual_dma_set_writers(saved_writers) &&
        dual_dma_set_schedule(saved_schedule) && dual_dma_set_bus_priority(saved_priority) && dual_dma_set_dac_clock(saved_clock);
    dvbs2_bch_set_kernel(saved_bch); dvbs2_frontend_set_bank(saved_bank);
    if (!selectors_ok) errors |= ERR_SETUP;
    const bool restored = !(p.errors & IQ4PTR_ERR_STOP);
    if (capture_count == CAP_WORDS) cap_crc = crc32_update(0, cap_buf, CAP_WORDS * 4u);
    const uint32_t crc_i = pv_crc32(0, in_buf, C3_RING_GROUPS * 4u);
    const uint32_t crc_q = pv_crc32(0, in_buf + C3_RING_GROUPS, C3_RING_GROUPS * 4u);
    const double budget = (double)elapsed * hz / 1000000.0;
    printf("@{\"cmd\":\"b8lean8\",\"git\":\"%s\",\"clk_hz\":%lu,\"cpw\":%d,\"ms\":%d,\"invert\":%d,"
           "\"bus_priority_mask\":%d,\"options\":%d,\"starve\":%d,\"code_index\":%d,\"s2_code\":\"%s\",\"kbch\":%u,"
           "\"pilots\":true,\"gold\":0,\"source\":6,\"row_profile\":\"%s\",\"L\":7,\"sps\":4,\"row_symbols\":1,"
           "\"row_words\":1,\"sample_bits\":8,\"writers\":2,\"frame_symbols\":%u,\"table_crc\":%lu,"
           "\"errors\":%lu,\"pointer_errors\":%lu,\"output_errors\":%lu,\"txstalls_i\":%lu,\"txstalls_q\":%lu,"
           "\"pointer_txstall_mask\":%lu,\"null_trigger_mask\":%lu,\"reason\":\"%s\",\"restored\":%s,\"selectors_restored\":%s,"
           "\"elapsed_us\":%lu,\"prefill_frames\":2,\"published_frames\":%lu,\"published_groups\":%lu,"
           "\"published_words\":%llu,\"granted_words\":%llu,\"reusable_words\":%llu,\"input_issue_words\":[%llu,%llu],"
           "\"min_credit_runway_words\":%lld,\"max_service_gap_us\":%lu,\"service_gap_limit_us\":128,"
           "\"context_bytes\":%u,\"code_crc\":[%lu,%lu],\"core\":[",
           GIT_REV, (unsigned long)hz, cpw, ms, invert, priority, options, starve, code_index,
           DVBS2_CODES[code_index].name, DVBS2_CODES[code_index].kbch, IQ4HIST_PROFILE, C3_FRAME_SYMBOLS,
           (unsigned long)table_crc, (unsigned long)errors, (unsigned long)p.errors, (unsigned long)output.errors,
           (unsigned long)output.txstalls_i, (unsigned long)output.txstalls_q, (unsigned long)p.txstall_mask,
           (unsigned long)p.null_trigger_mask, reason, restored ? "true" : "false", selectors_ok ? "true" : "false",
           (unsigned long)elapsed, (unsigned long)live.published, (unsigned long)live.groups,
           (unsigned long long)p.published_words, (unsigned long long)p.granted_words, (unsigned long long)p.reusable_words,
           (unsigned long long)p.input_words[0], (unsigned long long)p.input_words[1],
           (long long)(min_runway == INT64_MAX ? -1 : min_runway), (unsigned long)max_gap,
           (unsigned)sizeof live, (unsigned long)crc_i, (unsigned long)crc_q);
    for (unsigned c = 0; c < 2; ++c) {
        const b8_worker_t *w = &live.core[c];
        const uint64_t busy = w->busy_cycles + (c ? 0 : live.supervise_cycles);
        printf("%s{\"frames\":%lu,\"busy_cycles\":%llu,\"busy\":%.6f,\"max_frame_cycles\":%lu,\"max_slice_cycles\":%lu,"
               "\"stage_cycles\":{\"gcs\":%llu,\"bch\":%llu,\"ldpc\":%llu,\"map\":%llu,\"delay\":0}}",
               c ? "," : "", (unsigned long)w->frames, (unsigned long long)busy, budget ? busy / budget : 0,
               (unsigned long)w->max_frame_cycles, (unsigned long)w->max_slice_cycles,
               (unsigned long long)w->stage_cycles[0], (unsigned long long)w->stage_cycles[1],
               (unsigned long long)w->stage_cycles[2], (unsigned long long)w->stage_cycles[3]);
    }
    printf("],\"cap_format\":\"pins32\",\"cap_words\":%lu,\"cap_crc\":%lu,\"cap_stride\":17,\"cap_symbol_lo\":%llu,"
           "\"cap_symbol_hi\":%llu,\"cap_margin_symbols\":256,\"pv_spi\":{\"bb_mode\":\"gcs\",\"sck_hz\":32000000,"
           "\"bbframes\":%lu,\"runtime_bbframes\":%lu,\"runtime_messages\":%lu,\"crc_chain\":%lu,"
           "\"bb_crc\":[%lu,%lu,%lu,%lu],\"ok\":%lu,\"lost\":%lu,\"bad_hdr\":%lu,\"bad_crc\":%lu,\"bad_sync\":%lu,"
           "\"ts_packets\":%lu,\"null_packets\":%lu,\"gcs_packets\":%lu,\"gcs_bytes\":%lu,\"gcs_underflows\":%lu,"
           "\"short_msgs\":%lu,\"long_msgs\":%lu,\"overflows\":%lu,\"deferred_msgs\":%lu,\"drain_timeouts\":%lu,\"late_closes\":%lu}}\n",
           (unsigned long)capture_count, (unsigned long)cap_crc, (unsigned long long)cap_lo, (unsigned long long)cap_hi,
           (unsigned long)pv_stats.bbframes, (unsigned long)(pv_stats.bbframes - bb_prefill), (unsigned long)(pvspi.head - spi_head_start),
           (unsigned long)pv_stats.crc_chain, (unsigned long)live.ingress.bb_crc[0], (unsigned long)live.ingress.bb_crc[1],
           (unsigned long)live.ingress.bb_crc[2], (unsigned long)live.ingress.bb_crc[3],
           (unsigned long)pv_stats.ok, (unsigned long)pv_stats.lost, (unsigned long)pv_stats.bad_hdr,
           (unsigned long)pv_stats.bad_crc, (unsigned long)pv_stats.bad_sync, (unsigned long)pv_stats.ts_packets,
           (unsigned long)pv_stats.null_packets, (unsigned long)pv_stats.gcs_packets, (unsigned long)pv_stats.gcs_bytes,
           (unsigned long)pv_stats.gcs_underflows, (unsigned long)pvspi.short_msgs, (unsigned long)pvspi.long_msgs,
           (unsigned long)pvspi.overflows, (unsigned long)pvspi.deferred_msgs, (unsigned long)pvspi.drain_timeouts, (unsigned long)pvspi.late_closes);
    if (capture_count == CAP_WORDS) {
        for (unsigned n = 0; n < CAP_WORDS; n += 16) {
            printf("@cap %u", n);
            for (unsigned j = 0; j < 16; ++j) printf(" %08lx", (unsigned long)cap_buf[n + j]);
            putchar('\n');
        }
        printf("@capend\n");
    }
    // Print from the borrowed context before rebuilding ordinary tables.
    iq_build_tables(&cfg);
    if (restored) { uint32_t seed = SEED; prbs_fill(in_buf, IN_WORDS, &seed); memset(link_ring, 0, sizeof link_ring); }
}
