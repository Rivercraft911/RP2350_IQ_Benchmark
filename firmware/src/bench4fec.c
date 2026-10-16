// Live SPI/GCS 8PSK 5/6 FEC -> four-level L7/N4 hardware shaping. Opt-in experiment.
//
// Core 1 runs the bench8live encoder (PV-SPI GCS -> BBFRAME -> BCH/LDPC -> rotated
// 8PSK PL map) into a ring of signed-plane groups. Core 0 converts each group into one
// 2-bit code word per axis, publishes complete words to the credit-gated PIO pointer
// producer (iq4ptr live mode) and supervises. Two DMA/PIO writers emit one-symbol rows
// with the GPIO28 DDR clock. No CPU sample arithmetic or descriptor generation remains.
#include "bench4fec.h"

#include <stdio.h>
#include <string.h>

#include "dual_dma.h"
#include "dvbs2_8psk.h"
#include "dvbs2_codes.h"
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/pio.h"
#include "hardware/sync.h"
#include "iq4hist.h"
#include "iq4ptr.h"
#include "link.h"
#include "pico/multicore.h"
#include "pico/stdlib.h"
#include "tx.h"

enum {
    AXIS_WORDS = IN_WORDS / 2, AXIS_MASK = AXIS_WORDS - 1, CAP_WORDS = 4096,
    GROUPS = 4096, GROUP_MASK = GROUPS - 1, CHUNK_GROUPS = 64,
    POLL_US = 128, MAX_POLL_US = 256, CAP_MARGIN_SYMBOLS = 256,
    BB_BITS = 53840, BB_BYTES = BB_BITS / 8, BB_WORDS_USED = (BB_BITS + 31) / 32,
    PREFILL_FRAMES = 2, PREFILL_MESSAGES = 8, PREFILL_TIMEOUT_US = 100000,
    GCS_MATYPE1 = 0x72, GCS_SYNC = 0xb9,
    STAGE_BB = 0, STAGE_BCH, STAGE_LDPC, STAGE_MAP, STAGES,
    ERR_INPUT = 1u << 16, ERR_PV = 1u << 17, ERR_POINTER = 1u << 18, ERR_CAPTURE = 1u << 19,
    ERR_TABLE = 1u << 20, ERR_CONFIG = 1u << 21, ERR_POLL = 1u << 22,
};
_Static_assert(AXIS_WORDS == 4096, "two aligned16KiB code rings in in_buf");
_Static_assert(CAP_WORDS + 2 * GROUPS <= CAP_WORDS_MAX, "capture plus signed-plane group ring");
_Static_assert(IQ4HIST_TABLE_WORDS == N_BLOCKS * BLOCK_WORDS, "borrow the whole output ring");
_Static_assert(PV_SLOTS * PV_MSG_WORDS <= LINK_RING_WORDS, "PV receiver arena");
_Static_assert(PV_EMU_WORDS <= TABLE_WORDS, "PV emulator arena");
_Static_assert(BB_WORDS_USED <= BB_WORDS, "BBFRAME arena");

// SRAM4-7 (NOLOAD): lower SRAM is full in the experiment image. Fully assigned before use.
static struct {
    volatile bool stop;
    volatile uint32_t total, consumed, encoder_errors;
    uint32_t frames, bb_frames, bb_crc[4], frame_groups;
    uint32_t max_frame_cycles, max_stage_cycles[STAGES];
    uint64_t stage_cycles[STAGES], start_us, end_us;
    dvbs2_8psk_cfg_t profile;
    dvbs2_8psk_stream_t stream;
} live SRAM_HI;

static inline uint64_t now_us(void) {
    timer_hw_t *timer = PICO_DEFAULT_TIMER_INSTANCE();
    for (uint32_t high = timer->timerawh;;) {
        const uint32_t low = timer->timerawl, after = timer->timerawh;
        if (high == after) return (uint64_t)high << 32 | low;
        high = after;
    }
}

static bool transport_failed(void) {
    return pvspi.errors || pvspi.short_msgs || pvspi.long_msgs || pvspi.overflows ||
           pvspi.drain_timeouts || pvspi.late_closes;
}

// The bench8live CCM encoder, unchanged apart from dropping its VCM branch.
static bool encode_frame(void) {
    uint32_t stage[STAGES];
    const uint32_t begin = cycles();
    if (!pv_gcs_bbframe((uint8_t *)bb_buf, BB_BITS, GCS_MATYPE1, GCS_SYNC, pvspi_next_packet)) {
        live.encoder_errors |= ERR_INPUT;
        return false;
    }
    for (unsigned byte = BB_BYTES; byte < 4 * BB_WORDS_USED; ++byte)
        ((uint8_t *)bb_buf)[byte] = 0;
    if (live.bb_frames < 4) live.bb_crc[live.bb_frames] = pv_crc32(0, bb_buf, BB_BYTES);
    live.bb_frames++;
    for (unsigned word = 0; word < BB_WORDS_USED; ++word)
        bb_buf[word] = __builtin_bswap32(bb_buf[word]);
    uint32_t start = cycles();
    stage[STAGE_BB] = start - begin;
    if (pv_stats.lost || pv_stats.bad_hdr || pv_stats.bad_crc || pv_stats.bad_sync ||
        pv_stats.gcs_underflows) {
        live.encoder_errors |= ERR_PV;
        return false;
    }
    dvbs2_frame_begin(bb_buf);
    uint32_t end = cycles();
    stage[STAGE_BCH] = end - start;
    start = end;
    dvbs2_frame_ldpc();
    end = cycles();
    stage[STAGE_LDPC] = end - start;
    start = end;
    // Preserve partial groups across frames. Never flush a continuous CCM stream.
    dvbs2_8psk_frame(&live.profile, dvbs2_frame_buffer(), &live.stream);
    end = cycles();
    stage[STAGE_MAP] = end - start;
    for (unsigned n = 0; n < STAGES; ++n) {
        live.stage_cycles[n] += stage[n];
        if (stage[n] > live.max_stage_cycles[n]) live.max_stage_cycles[n] = stage[n];
    }
    const uint32_t elapsed = end - begin;
    if (elapsed > live.max_frame_cycles) live.max_frame_cycles = elapsed;
    live.frames++;
    __dmb(); // publish complete groups only, after every frame store has retired
    live.total = live.stream.n;
    return true;
}

// Runs from flash: the wait loop is tiny and the encoder stages it calls are SRAM-resident.
static void encoder_task(uint32_t unused) {
    live.start_us = now_us();
    while (!live.stop) {
        const uint32_t consumed = live.consumed;
        __dmb(); // acquire core 0's release of converted groups
        if (live.stream.n + live.frame_groups + 1u - consumed > GROUPS) continue;
        if (!encode_frame()) break;
    }
    live.end_us = now_us();
    __dmb();
}

static bool resources_available(void) {
    unsigned free_dma = 0, sms[3] = {0};
    const PIO pios[] = {pio0, pio1, pio2};
    for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ++ch) free_dma += !dma_channel_is_claimed(ch);
    for (unsigned p = 0; p < 3; ++p)
        for (unsigned sm = 0; sm < NUM_PIO_STATE_MACHINES; ++sm) sms[p] += !pio_sm_is_claimed(pios[p], sm);
    // Writers PIO0; PV-SPI (3) + capture PIO1; two pointers + DDR clock PIO2.
    // DMA: 4 output + 1 capture + 4 pointer/credit + 3 PV-SPI.
    return free_dma >= 12 && sms[0] >= 2 && sms[1] == 4 && sms[2] >= 3;
}

static void restore_buffers(bool inputs) {
    iq_build_tables(&cfg);
    memset(link_ring, 0, sizeof link_ring);
    if (inputs) { uint32_t seed = SEED; prbs_fill(in_buf, IN_WORDS, &seed); }
}

void bench4fec(int cpw, int ms, int invert) {
    const uint32_t hz = clock_get_hz(clk_sys);
    if ((cpw != 2 && cpw != 4) || ms < 100 || ms > 60000 || (invert != 0 && invert != 1) ||
        hz != 128000000u || ((uintptr_t)ring_buf & (128u * 1024u - 1u)) || !resources_available()) {
        printf("@{\"cmd\":\"bench4fec\",\"error\":\"needs SYS128, aligned experiment build, cpw2/4, 100..60000ms, invert0/1 and idle PIO1/PIO2 resources\"}\n");
        return;
    }
    const iq_kernel_info_t *unused;
    bool tables_ok;
    if (tx_shaper("lut_asm_p", 4, 12, &unused, &tables_ok) || !tables_ok) {
        printf("@{\"cmd\":\"bench4fec\",\"error\":\"initial LUTs unavailable\"}\n");
        return;
    }
    const unsigned saved_writers = dual_dma_get_writers(), saved_rows = dual_dma_get_row_words();
    const unsigned saved_schedule = dual_dma_get_schedule(), saved_priority = dual_dma_get_bus_priority();
    const unsigned saved_clock = dual_dma_get_dac_clock();
    if (!dual_dma_set_writers(2) || !dual_dma_set_row_words(2) ||
        !dual_dma_set_schedule(0) || !dual_dma_set_bus_priority(0) ||
        !dual_dma_set_dac_clock(invert ? DUAL_DMA_DAC_CLOCK_DDR_PIO_INVERTED : DUAL_DMA_DAC_CLOCK_DDR_PIO)) {
        printf("@{\"cmd\":\"bench4fec\",\"error\":\"output selectors unavailable\"}\n");
        return;
    }
    uint32_t *code_i = in_buf, *code_q = in_buf + AXIS_WORDS;
    uint32_t *capture = cap_buf;
    iq8_input_t *groups = (iq8_input_t *)(cap_buf + CAP_WORDS);
    // Prehistory: the repeated rotated point pi/8 (I code 0 = +cos, Q code 2 = +sin), as
    // in the existing shaper. The producer seeds six symbols from the top of these words.
    const uint32_t previous_i = 0, previous_q = 0xaaaaaaaau;
    uint32_t errors = 0, table_crc = 0, elapsed = 0, max_gap = 0, polls = 0, chunks = 0;
    uint32_t capture_count = 0, cap_crc = 0, converted = 0, prefill_groups = 0, prefill_fill = 0;
    uint32_t prefill_crc = 0, bb_prefill = 0, spi_head_start = 0, cap_published = 0;
    uint64_t supervise_cycles = 0, convert_cycles = 0, cap_lo = 0, cap_hi = 0;
    int64_t min_runway = INT64_MAX;
    bool spi_started = false, encoder_started = false, pointer_attempted = false;
    bool output_attempted = false, captured = false;
    iq4ptr_progress_t p = {0};
    dual_dma_faults_t output = {0};
    const char *reason = "complete";
    const int spi_half = 2; // 32 MHz on-chip SCK at SYS128, as bench8live
    const dvbs2_code_t *code = &DVBS2_CODES[S2_N5_6];

    if (!iq4hist_build(ring_buf, N_BLOCKS * BLOCK_WORDS)) {
        errors |= ERR_TABLE; reason = "table build failed"; goto cleanup;
    }
    table_crc = crc32_update(0, ring_buf, sizeof ring_buf);
    if (table_crc != IQ4HIST_TABLE_CRC) { errors |= ERR_TABLE; reason = "table CRC mismatch"; goto cleanup; }
    tx_code(S2_N5_6, true);
    live = (typeof(live)){.stream = {.out = groups, .mask = GROUP_MASK}};
    if (!dvbs2_8psk_init(&live.profile, code, true, 0, cfg.ti, TABLE_WORDS)) {
        errors |= ERR_CONFIG; reason = "8PSK mapper initialization failed"; goto cleanup;
    }
    live.frame_groups = (live.profile.frame_symbols + 15u) / 16u;
    pv_init(PV_EMU_MSGS);
    pvspi_start(pio1, link_ring, true, cfg.tq, spi_half);
    spi_started = true;
    const uint64_t prefill_start = now_us();
    while (pvspi.head < PREFILL_MESSAGES && now_us() - prefill_start < PREFILL_TIMEOUT_US)
        tight_loop_contents();
    bool prefilled = pvspi.head >= PREFILL_MESSAGES;
    for (unsigned frame = 0; prefilled && frame < PREFILL_FRAMES; ++frame)
        prefilled = encode_frame();
    if (!prefilled || transport_failed()) {
        errors |= ERR_INPUT; reason = "SPI/encoder startup prefill failed"; goto cleanup;
    }
    prefill_groups = live.total;
    prefill_fill = live.stream.fill;
    prefill_crc = crc32_update(0, groups, prefill_groups * sizeof(iq8_input_t));
    for (; converted < prefill_groups; ++converted)
        iq4hist_pack_group(&groups[converted & GROUP_MASK], &code_i[converted & AXIS_MASK],
                           &code_q[converted & AXIS_MASK]);
    live.consumed = converted;
    memset(capture, 0xa5, CAP_WORDS * 4);
    memset(live.stage_cycles, 0, sizeof live.stage_cycles);
    memset(live.max_stage_cycles, 0, sizeof live.max_stage_cycles);
    live.max_frame_cycles = 0;
    bb_prefill = live.bb_frames;
    spi_head_start = pvspi.head;
    pointer_attempted = true;
    if (!iq4ptr_prepare_live(pio2, code_i, code_q, 12, (const uint16_t *)ring_buf,
                            previous_i, previous_q, converted) || !iq4ptr_start()) {
        errors |= ERR_POINTER; reason = iq4ptr_last_error(); goto cleanup;
    }
    output_attempted = true;
    if (!dual_dma_start_axis_fifo(iq4ptr_rx_fifo(0), iq4ptr_rx_fifo(1),
                                  iq4ptr_rx_dreq(0), iq4ptr_rx_dreq(1), cpw)) {
        errors |= ERR_CONFIG; reason = dual_dma_last_error(); goto cleanup;
    }
    multicore_fifo_push_blocking((uint32_t)encoder_task);
    multicore_fifo_push_blocking(0);
    encoder_started = true;
    const uint32_t start = time_us_32();
    uint32_t last_poll = start;
    while ((uint32_t)(time_us_32() - start) < (uint32_t)ms * 1000u) {
        const uint32_t now = time_us_32(), gap = now - last_poll;
        if (gap >= POLL_US) {
            const uint32_t begin = cycles();
            last_poll = now; polls++;
            if (gap > max_gap) max_gap = gap;
            if (gap > MAX_POLL_US) { errors |= ERR_POLL; reason = "supervision poll deadline"; }
            p = iq4ptr_publish(converted);
            output = dual_dma_faults();
            const uint64_t high = p.input_words[0] > p.input_words[1] ? p.input_words[0] : p.input_words[1];
            const int64_t runway = (int64_t)p.granted_words - (int64_t)high;
            if (runway < min_runway) min_runway = runway;
            if (p.errors) { errors |= ERR_POINTER; reason = "pointer producer fault"; }
            if (output.errors) { errors |= output.errors; reason = "output fault"; }
            errors |= live.encoder_errors;
            if (transport_failed()) { errors |= ERR_PV; reason = "PV-SPI transport fault"; }
            if (!errors && !captured && now - start >= (uint32_t)ms * 500u) {
                // The first snapshot lies between issued-low output (less FIFO/OSR depth)
                // and the issued-high cursor read after the capture starts.
                const uint64_t low = p.input_words[0] < p.input_words[1] ? p.input_words[0] : p.input_words[1];
                cap_lo = low * 16u;
                cap_published = converted;
                captured = dual_dma_capture_pins(capture, CAP_WORDS);
                if (!captured) { errors |= ERR_CAPTURE; reason = dual_dma_last_error(); }
                const iq4ptr_progress_t after = iq4ptr_progress();
                cap_hi = (after.input_words[0] > after.input_words[1] ?
                          after.input_words[0] : after.input_words[1]) * 16u;
            }
            supervise_cycles += (uint32_t)(cycles() - begin);
            if (errors) break;
            continue;
        }
        // Convert at most one chunk per loop pass so supervision stays within its period.
        const uint32_t available = live.total - converted;
        const uint64_t space = p.reusable_words + AXIS_WORDS - converted;
        uint32_t n = available < CHUNK_GROUPS ? available : CHUNK_GROUPS;
        if (n > space) n = (uint32_t)space;
        if (!n) { tight_loop_contents(); continue; }
        __dmb(); // acquire the encoder's whole-frame group publication
        const uint32_t begin = cycles();
        for (uint32_t k = 0; k < n; ++k, ++converted)
            iq4hist_pack_group(&groups[converted & GROUP_MASK], &code_i[converted & AXIS_MASK],
                               &code_q[converted & AXIS_MASK]);
        convert_cycles += (uint32_t)(cycles() - begin);
        chunks++;
        __dmb(); // release converted group slots to the encoder
        live.consumed = converted;
    }
    elapsed = time_us_32() - start;
    p = iq4ptr_progress(); output = dual_dma_faults();
    if (p.errors) errors |= ERR_POINTER;
    errors |= output.errors;
cleanup:
    live.stop = true;
    __dmb();
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
    if (encoder_started) multicore_fifo_pop_blocking();
    __dmb();
    errors |= live.encoder_errors;
    if (spi_started) {
        pvspi_pause();
        pvspi_stop();
        if (transport_failed() || pv_stats.lost || pv_stats.bad_hdr || pv_stats.bad_crc ||
            pv_stats.bad_sync || pv_stats.gcs_underflows) errors |= ERR_PV;
    }
    if (capture_count != CAP_WORDS && !(errors & ~ERR_CAPTURE)) errors |= ERR_CAPTURE;
    const bool selectors_ok = dual_dma_set_writers(2) && dual_dma_set_row_words(saved_rows) &&
        dual_dma_set_writers(saved_writers) && dual_dma_set_schedule(saved_schedule) &&
        dual_dma_set_bus_priority(saved_priority) && dual_dma_set_dac_clock(saved_clock);
    if (!selectors_ok) errors |= ERR_CONFIG;
    const bool restored = !(p.errors & IQ4PTR_ERR_STOP);
    restore_buffers(restored);
    const uint32_t cap_words = capture_count == CAP_WORDS ? CAP_WORDS : 0;
    if (cap_words) cap_crc = crc32_update(0, capture, cap_words * 4);
    const double budget = (double)elapsed * hz / 1e6;
    const double encoder_budget = (double)(live.end_us - live.start_us) * hz / 1e6;
    uint64_t encoder_cycles = 0;
    for (unsigned n = 0; n < STAGES; ++n) encoder_cycles += live.stage_cycles[n];
    const double rs = hz / (8.0 * cpw);
    printf("@{\"cmd\":\"bench4fec\",\"git\":\"%s\",\"clk_hz\":%lu,\"cpw\":%d,\"divider256\":%d,"
           "\"ms\":%d,\"invert\":%d,\"source\":6,\"waveform_plan\":\"8psk_ccm\",\"code_index\":8,"
           "\"s2_code\":\"normal 5/6\",\"modcod\":15,\"kbch\":53840,\"pilots\":true,\"gold\":0,"
           "\"rotation_pi\":0.125,\"L\":7,\"sps\":4,\"row_profile\":\"%s\",\"row_symbols\":1,"
           "\"row_words\":2,\"writers\":2,\"sym_rate\":%.1f,\"bb_payload_bps\":%.1f,"
           "\"frame_symbols\":%lu,\"frame_groups_max\":%lu,\"group_ring\":%u,\"code_ring_words\":%u,"
           "\"table_crc\":%lu,\"errors\":%lu,\"pointer_errors\":%lu,\"output_errors\":%lu,"
           "\"txstalls_i\":%lu,\"txstalls_q\":%lu,\"pointer_txstall_mask\":%lu,"
           "\"pointer_rxstall_mask\":%lu,\"null_trigger_mask\":%lu,\"reason\":\"%s\","
           "\"restored\":%s,\"selectors_restored\":%s,\"elapsed_us\":%lu,"
           "\"prefill_frames\":%u,\"prefill_groups\":%lu,\"prefill_fill\":%lu,\"prefill_crc\":%lu,"
           "\"converted_groups\":%lu,\"input_published_groups\":%lu,\"convert_chunks\":%lu,"
           "\"published_words\":%llu,\"granted_words\":%llu,\"reusable_words\":%llu,"
           "\"input_issue_words\":[%llu,%llu],\"min_credit_runway_words\":%lld,"
           "\"core0_work_busy\":%.6f,\"supervision_busy\":%.6f,\"convert_busy\":%.6f,"
           "\"convert_cyc_per_sym\":%.4f,\"sample_math_busy\":0.0,\"descriptor_gen_busy\":0.0,"
           "\"polls\":%lu,\"poll_interval_us\":%u,\"max_poll_interval_us\":%u,\"max_poll_gap_us\":%lu,"
           "\"encoder_frames\":%lu,\"encoder_runtime_frames\":%lu,\"encoder_busy\":%.4f,"
           "\"encoder_elapsed_us\":%llu,\"encoder_max_frame_cyc\":%lu,\"bch_kernel\":%d,\"ldpc_pack\":%u,"
           "\"encoder_stage_cycles\":{\"bb\":%llu,\"bb_bch\":%llu,\"ldpc\":%llu,\"map\":%llu},"
           "\"encoder_stage_max_cyc\":{\"bb\":%lu,\"bb_bch\":%lu,\"ldpc\":%lu,\"map\":%lu},"
           "\"cap_format\":\"pins32\",\"cap_words\":%lu,\"capture_count\":%lu,\"cap_crc\":%lu,"
           "\"cap_stride\":17,\"cap_symbol_lo\":%llu,\"cap_symbol_hi\":%llu,\"cap_margin_symbols\":%u,"
           "\"cap_published_groups\":%lu,"
           "\"pv_spi\":{\"bb_mode\":\"gcs\",\"sck_hz\":%lu,\"bbframes\":%lu,\"runtime_bbframes\":%lu,"
           "\"runtime_messages\":%lu,\"crc_chain\":%lu,\"bb_crc\":[%lu,%lu,%lu,%lu],"
           "\"ok\":%lu,\"lost\":%lu,\"bad_hdr\":%lu,\"bad_crc\":%lu,\"bad_sync\":%lu,"
           "\"ts_packets\":%lu,\"null_packets\":%lu,\"gcs_packets\":%lu,\"gcs_bytes\":%lu,\"gcs_underflows\":%lu,"
           "\"short_msgs\":%lu,\"long_msgs\":%lu,\"overflows\":%lu,\"deferred_msgs\":%lu,"
           "\"drain_timeouts\":%lu,\"late_closes\":%lu}}\n",
           GIT_REV, (unsigned long)hz, cpw, cpw * 256, ms, invert, IQ4HIST_PROFILE,
           rs, rs * (BB_BITS - 80) / live.profile.frame_symbols,
           (unsigned long)live.profile.frame_symbols, (unsigned long)live.frame_groups, GROUPS, AXIS_WORDS,
           (unsigned long)table_crc, (unsigned long)errors, (unsigned long)p.errors, (unsigned long)output.errors,
           (unsigned long)output.txstalls_i, (unsigned long)output.txstalls_q,
           (unsigned long)p.txstall_mask, (unsigned long)p.rxstall_mask, (unsigned long)p.null_trigger_mask,
           reason, restored ? "true" : "false", selectors_ok ? "true" : "false", (unsigned long)elapsed,
           PREFILL_FRAMES, (unsigned long)prefill_groups, (unsigned long)prefill_fill, (unsigned long)prefill_crc,
           (unsigned long)converted, (unsigned long)live.total, (unsigned long)chunks,
           (unsigned long long)p.published_words, (unsigned long long)p.granted_words,
           (unsigned long long)p.reusable_words,
           (unsigned long long)p.input_words[0], (unsigned long long)p.input_words[1],
           (long long)(min_runway == INT64_MAX ? -1 : min_runway),
           budget ? (supervise_cycles + convert_cycles) / budget : 0, budget ? supervise_cycles / budget : 0,
           budget ? convert_cycles / budget : 0,
           converted > prefill_groups ? (double)convert_cycles / (16.0 * (converted - prefill_groups)) : 0,
           (unsigned long)polls, POLL_US, MAX_POLL_US, (unsigned long)max_gap,
           (unsigned long)live.frames, (unsigned long)(live.frames > PREFILL_FRAMES ? live.frames - PREFILL_FRAMES : 0),
           encoder_budget ? encoder_cycles / encoder_budget : 0,
           (unsigned long long)(live.end_us - live.start_us), (unsigned long)live.max_frame_cycles,
           (int)dvbs2_bch_get_kernel(), dvbs2_ldpc_get_pack(),
           (unsigned long long)live.stage_cycles[STAGE_BB], (unsigned long long)live.stage_cycles[STAGE_BCH],
           (unsigned long long)live.stage_cycles[STAGE_LDPC], (unsigned long long)live.stage_cycles[STAGE_MAP],
           (unsigned long)live.max_stage_cycles[STAGE_BB], (unsigned long)live.max_stage_cycles[STAGE_BCH],
           (unsigned long)live.max_stage_cycles[STAGE_LDPC], (unsigned long)live.max_stage_cycles[STAGE_MAP],
           (unsigned long)cap_words, (unsigned long)capture_count, (unsigned long)cap_crc,
           (unsigned long long)cap_lo, (unsigned long long)cap_hi, CAP_MARGIN_SYMBOLS,
           (unsigned long)cap_published,
           (unsigned long)(hz / (2 * spi_half)), (unsigned long)live.bb_frames,
           (unsigned long)(live.bb_frames - bb_prefill), (unsigned long)(pvspi.head - spi_head_start),
           (unsigned long)pv_stats.crc_chain,
           (unsigned long)live.bb_crc[0], (unsigned long)live.bb_crc[1],
           (unsigned long)live.bb_crc[2], (unsigned long)live.bb_crc[3],
           (unsigned long)pv_stats.ok, (unsigned long)pv_stats.lost, (unsigned long)pv_stats.bad_hdr,
           (unsigned long)pv_stats.bad_crc, (unsigned long)pv_stats.bad_sync,
           (unsigned long)pv_stats.ts_packets, (unsigned long)pv_stats.null_packets,
           (unsigned long)pv_stats.gcs_packets, (unsigned long)pv_stats.gcs_bytes, (unsigned long)pv_stats.gcs_underflows,
           (unsigned long)pvspi.short_msgs, (unsigned long)pvspi.long_msgs, (unsigned long)pvspi.overflows,
           (unsigned long)pvspi.deferred_msgs, (unsigned long)pvspi.drain_timeouts, (unsigned long)pvspi.late_closes);
    if (cap_words) {
        for (unsigned word = 0; word < cap_words; word += 16) {
            printf("@cap %u", word);
            for (unsigned j = 0; j < 16; ++j) printf(" %08lx", (unsigned long)capture[word + j]);
            putchar('\n');
        }
        printf("@capend\n");
    }
}
