#include "bench_apsk_fec.h"

#include <stdio.h>
#include <string.h>

#include "dvbs2_apsk.h"
#include "dvbs2_codes.h"
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/pio.h"
#include "hardware/sync.h"
#include "iq16fast.h"
#include "iq32offsets_native.h"
#include "iq32offsets.h"
#include "iqout.h"
#include "link.h"
#include "pico/stdlib.h"
#include "tx.h"

enum {
    INPUT_MASK = IN_WORDS - 1, BLOCK_SYMBOLS = 1024,
    OUT_WORDS = 2 * BLOCK_SYMBOLS, OUT_BLOCKS = 4, CAP_WORDS = 4096,
    BB_BITS = 53840, BB_BYTES = BB_BITS / 8, BB_WORDS_USED = (BB_BITS + 31) / 32,
    PREFILL_MESSAGES = 8, PREFILL_TIMEOUT_US = 100000, PUBLICATION_WORDS = 64,
    GCS_MATYPE1 = 0x72, GCS_SYNC = 0xb9,
    STAGE_BB = 0, STAGE_BCH, STAGE_LDPC, STAGE_MAP, STAGES,
    ERR_INPUT = 1, ERR_UNDERRUN = 2, ERR_OWNERSHIP = 4,
    ERR_TXSTALL = 8, ERR_PV = 16, ERR_CAPTURE = 32,
};

typedef struct {
    union { iq16native8_tables_t t16; iq32native8_tables_t t32; } tables;
    dvbs2_apsk_cfg_t profile;
    dvbs2_apsk_stream_t stream;
    volatile uint32_t stop, total, consumed, encoder_errors;
    uint32_t modulation, words_per_block, retained_words, symbols_per_word;
    uint32_t frames, attempts, bb_crc[4], fe_crc[4], max_frame_cycles, max_stage_cycles[STAGES];
    uint64_t stage_cycles[STAGES], credit_wait_cycles, start_us, end_us;
} live_t;

// All arenas are borrowed while ordinary TX is idle. Context uses the8KiB tail
// beyond output and finite capture; it introduces no additional BSS allocation.
static inline live_t *context(void) {
    const uintptr_t tail = (uintptr_t)(cap_buf + OUT_BLOCKS * OUT_WORDS + CAP_WORDS);
    return (live_t *)((tail + _Alignof(live_t) - 1u) & ~(uintptr_t)(_Alignof(live_t) - 1u));
}
#define live (*context())
_Static_assert((IN_WORDS & INPUT_MASK) == 0, "native ring power of two");
_Static_assert((unsigned)PUBLICATION_WORDS == (unsigned)DVBS2_APSK_BATCH_WORDS, "mapper publication contract");
_Static_assert(sizeof(live_t) + _Alignof(live_t) - 1 <= 4 * (CAP_WORDS_MAX - OUT_BLOCKS * OUT_WORDS - CAP_WORDS), "context tail");
_Static_assert(PV_SLOTS * PV_MSG_WORDS <= LINK_RING_WORDS, "PV receiver arena");
_Static_assert(PV_EMU_WORDS + BLOCK_SYMBOLS + 5 <= TABLE_WORDS, "PV emulator and key scratch");
_Static_assert(3 * IQ32NATIVE8_TABLE_WORDS <= N_BLOCKS * BLOCK_WORDS, "32 tables");
_Static_assert(BB_WORDS_USED <= BB_WORDS, "BBFRAME arena");

static bool transport_failed(void) {
    return pvspi.errors || pvspi.short_msgs || pvspi.long_msgs || pvspi.overflows ||
        pvspi.drain_timeouts || pvspi.late_closes || pv_stats.lost || pv_stats.bad_hdr ||
        pv_stats.bad_crc || pv_stats.bad_sync || pv_stats.gcs_underflows;
}

static bool mapper_service(void *unused, uint32_t produced) {
    (void)unused;
    __dmb();
    live.total = produced;
    if (live.stop) return false;
    if (produced + PUBLICATION_WORDS - live.consumed <= IN_WORDS) {
        __dmb();
        return true;
    }
    const uint32_t start = cycles();
    do {
        if (live.stop) {
            live.credit_wait_cycles += (uint32_t)(cycles() - start);
            return false;
        }
        busy_wait_us_32(1);
    } while (produced + PUBLICATION_WORDS - live.consumed > IN_WORDS);
    live.credit_wait_cycles += (uint32_t)(cycles() - start);
    __dmb();
    return true;
}

static bool encode_frame(void) {
    uint32_t stage[STAGES];
    const uint32_t begin = cycles();
    if (!pv_gcs_bbframe((uint8_t *)bb_buf, BB_BITS, GCS_MATYPE1, GCS_SYNC, pvspi_next_packet)) {
        live.encoder_errors |= ERR_INPUT;
        return false;
    }
    if (live.attempts < 4) live.bb_crc[live.attempts] = pv_crc32(0, bb_buf, BB_BYTES);
    ++live.attempts;
    for (unsigned byte = BB_BYTES; byte < 4 * BB_WORDS_USED; ++byte) ((uint8_t *)bb_buf)[byte] = 0;
    for (unsigned word = 0; word < BB_WORDS_USED; ++word) bb_buf[word] = __builtin_bswap32(bb_buf[word]);
    uint32_t start = cycles();
    stage[STAGE_BB] = start - begin;
    if (transport_failed()) { live.encoder_errors |= ERR_PV; return false; }
    dvbs2_frame_begin(bb_buf);
    uint32_t end = cycles();
    stage[STAGE_BCH] = end - start;
    start = end;
    dvbs2_frame_ldpc();
    if (live.attempts <= 4) live.fe_crc[live.attempts-1] = pv_crc32(0, dvbs2_frame_buffer(), 64800 / 8);
    end = cycles();
    stage[STAGE_LDPC] = end - start;
    start = end;
    const uint64_t wait_before = live.credit_wait_cycles;
    const bool mapped = dvbs2_apsk_frame(&live.profile, dvbs2_frame_buffer(), &live.stream);
    end = cycles();
    const uint32_t elapsed = end - start;
    const uint32_t waited = (uint32_t)(live.credit_wait_cycles - wait_before);
    stage[STAGE_MAP] = elapsed - waited;
    uint32_t work = 0;
    for (unsigned n = 0; n < STAGES; ++n) {
        live.stage_cycles[n] += stage[n];
        if (stage[n] > live.max_stage_cycles[n]) live.max_stage_cycles[n] = stage[n];
        work += stage[n];
    }
    if (work > live.max_frame_cycles) live.max_frame_cycles = work;
    if (mapped) ++live.frames;
    else if (!live.stop) live.encoder_errors |= ERR_INPUT;
    __dmb();
    live.total = live.stream.n;
    return mapped;
}

static void encoder_task(uint32_t unused) {
    (void)unused;
    live.start_us = time_us_64();
    while (!live.stop && encode_frame()) {}
    live.end_us = time_us_64();
    __dmb();
}

static bool resources_available(void) {
    unsigned dma = 0, sms = 0;
    for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ++ch) dma += !dma_channel_is_claimed(ch);
    for (unsigned sm = 0; sm < NUM_PIO_STATE_MACHINES; ++sm) {
        if (pio_sm_is_claimed(pio2, sm)) return false;
        sms += !pio_sm_is_claimed(pio0, sm);
    }
    return dma >= 6 && sms >= 2;
}

static void restore_buffers(dvbs2_bch_kernel_t previous_bch) {
    dvbs2_bch_set_kernel(previous_bch);
    iq_build_tables(&cfg);
    uint32_t seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
    memset(link_ring, 0, sizeof link_ring);
}

static bool prepare_tables(uint32_t crc[3], uint32_t **gold, unsigned *gold_words) {
    if (live.modulation == 16) {
        live.tables.t16 = (iq16native8_tables_t){cfg.ti, ring_buf, ring_buf + IQ16NATIVE8_FULL_WORDS};
        if (!iq16native8_build(&live.tables.t16, IQ16NATIVE8_DEFAULT_GAMMA100)) return false;
        crc[0] = crc32_update(0, live.tables.t16.old, 4 * IQ16NATIVE8_OLD_WORDS);
        crc[1] = crc32_update(0, live.tables.t16.middle, 4 * IQ16NATIVE8_FULL_WORDS);
        crc[2] = crc32_update(0, live.tables.t16.future, 4 * IQ16NATIVE8_FULL_WORDS);
        *gold = cfg.ti + IQ16NATIVE8_OLD_WORDS;
        *gold_words = TABLE_WORDS - IQ16NATIVE8_OLD_WORDS;
        return crc[0] == IQ16NATIVE8_OLD_CRC && crc[1] == IQ16NATIVE8_MIDDLE_CRC && crc[2] == IQ16NATIVE8_FUTURE_CRC;
    }
    live.tables.t32 = (iq32native8_tables_t){ring_buf, ring_buf + IQ32NATIVE8_TABLE_WORDS,
                                            ring_buf + 2 * IQ32NATIVE8_TABLE_WORDS};
    if (!iq32native8_build(&live.tables.t32, IQ32NATIVE8_DEFAULT_RATE)) return false;
    crc[0] = crc32_update(0, live.tables.t32.old, 4 * IQ32NATIVE8_TABLE_WORDS);
    crc[1] = crc32_update(0, live.tables.t32.middle, 4 * IQ32NATIVE8_TABLE_WORDS);
    crc[2] = crc32_update(0, live.tables.t32.future, 4 * IQ32NATIVE8_TABLE_WORDS);
    *gold = cfg.ti;
    *gold_words = TABLE_WORDS;
    return crc[0] == 3255586384u && crc[1] == 916828675u && crc[2] == 1817262751u;
}

static uint32_t shape_block(uint32_t sequence, uint32_t *prepare_cycles) {
    const unsigned position = sequence * live.words_per_block & INPUT_MASK;
    const uint32_t *const input = in_buf + position;
    const uint32_t *const previous = sequence ? in_buf + ((position - live.retained_words) & INPUT_MASK) : NULL;
    uint32_t *const output = cap_buf + (sequence % OUT_BLOCKS) * OUT_WORDS;
    if (live.modulation == 16) {
        const uint32_t start = cycles();
        iq16fast_pair_asm(output, input, live.words_per_block, previous, &live.tables.t16);
        *prepare_cycles = 0;
        return cycles() - start;
    }
    uint32_t *const keys = cfg.tq + PV_EMU_WORDS;
    uint32_t start = cycles();
    iq32offsets_from_native(keys, input, live.words_per_block, previous);
    *prepare_cycles = cycles() - start;
    start = cycles();
    iq32offsets_kernel_sram_y(output, keys + 5, live.words_per_block, keys, &live.tables.t32);
    return cycles() - start;
}

static void capture_print(const uint32_t *capture) {
    for (unsigned word = 0; word < CAP_WORDS; word += 16) {
        printf("@cap %u", word);
        for (unsigned n = 0; n < 16; ++n) printf(" %08lx", (unsigned long)capture[word+n]);
        putchar('\n');
    }
    printf("@capend\n");
}

void bench_apsk_fec(int modulation, int cpw, int ms) {
    const uint32_t hz = clock_get_hz(clk_sys);
    if ((modulation != 16 && modulation != 32) || cpw < 3 || cpw > 32 || ms < 100 || ms > 60000 ||
        (hz != 128000000u && hz != 150000000u) || !resources_available()) {
        printf("@{\"cmd\":\"benchapskfec\",\"error\":\"needs16/32APSK,cpw3..32,100..60000ms,128/150MHz and idle PIO/SPI resources\"}\n");
        return;
    }
    const dvbs2_bch_kernel_t previous_bch = dvbs2_bch_get_kernel();
    memset(&live, 0, sizeof live);
    live.modulation = modulation;
    live.symbols_per_word = modulation == 16 ? 4 : 2;
    live.words_per_block = BLOCK_SYMBOLS / live.symbols_per_word;
    live.retained_words = 8 / live.symbols_per_word;
    live.stream = (dvbs2_apsk_stream_t){.out=in_buf, .mask=INPUT_MASK, .service=mapper_service};
    uint32_t table_crc[3], *gold;
    unsigned gold_words;
    if (!prepare_tables(table_crc, &gold, &gold_words)) {
        restore_buffers(previous_bch);
        printf("@{\"cmd\":\"benchapskfec\",\"error\":\"table construction/CRC failed\"}\n");
        return;
    }
    tx_code(S2_N5_6, true);
    dvbs2_bch_set_kernel(S2_BCH_STREAM_ARM);
    if (!dvbs2_apsk_init(&live.profile, &DVBS2_CODES[S2_N5_6], modulation, true, 0, gold, gold_words)) {
        restore_buffers(previous_bch);
        printf("@{\"cmd\":\"benchapskfec\",\"error\":\"PL mapper initialization failed\"}\n");
        return;
    }
    const int spi_half = hz == 128000000u ? 2 : 3;
    pv_init(PV_EMU_MSGS);
    pvspi_start(pio2, link_ring, true, cfg.tq, spi_half);
    const uint64_t prefill_start = time_us_64();
    while (pvspi.head < PREFILL_MESSAGES && time_us_64()-prefill_start < PREFILL_TIMEOUT_US)
        tight_loop_contents();
    if (pvspi.head < PREFILL_MESSAGES || !encode_frame() || transport_failed()) {
        pvspi_stop();
        restore_buffers(previous_bch);
        printf("@{\"cmd\":\"benchapskfec\",\"error\":\"SPI/encoder startup prefill failed\"}\n");
        return;
    }
    const uint32_t prefill_words = live.total, prefill_fill = live.stream.fill;
    const uint32_t prefill_crc = crc32_update(0, in_buf, 4 * prefill_words);
    if (prefill_crc != (modulation == 16 ? 1813402982u : 4248357742u)) {
        pvspi_stop();
        restore_buffers(previous_bch);
        printf("@{\"cmd\":\"benchapskfec\",\"error\":\"SPI/FEC/PL native prefill CRC mismatch\"}\n");
        return;
    }
    uint32_t *const capture = cap_buf + OUT_BLOCKS * OUT_WORDS;
    iqout_init(cap_buf, OUT_WORDS, OUT_BLOCKS, cpw, IQ_LAYOUT_RAW_PAIRS);
    uint32_t sequence = 0;
    for (; sequence < OUT_BLOCKS; ++sequence) {
        uint32_t ignored;
        shape_block(sequence, &ignored);
        ring.ready[sequence] = sequence+1;
    }
    live.consumed = sequence * live.words_per_block - live.retained_words;
    memset(live.stage_cycles, 0, sizeof live.stage_cycles);
    memset(live.max_stage_cycles, 0, sizeof live.max_stage_cycles);
    live.max_frame_cycles = 0;
    live.credit_wait_cycles = 0;
    const uint32_t spi_head_start = pvspi.head;
    uint32_t errors = 0, txstalls = 0, blocks = 0, input_waits = 0, min_input = IN_WORDS;
    uint32_t worst_prepare = 0, worst_shape = 0, cap_input_words = 0, cap_done_blocks = 0;
    uint64_t prepare_cycles = 0, shape_cycles = 0, work_cycles = 0, cap_symbol_cursor = 0;
    bool captured = false, waiting = false;
    const uint64_t frame_time_scale = 8000ull * (unsigned)cpw * live.profile.frame_symbols;
    const uint64_t capture_boundary = ((uint64_t)hz * (unsigned)ms + frame_time_scale - 1u) /
        frame_time_scale * live.profile.frame_symbols;
    // Aim one block before the header; DMA completion alone can be observed
    // late after shaping a block. The host still verifies the actual offset.
    const uint64_t capture_at_us = (capture_boundary - BLOCK_SYMBOLS) * 4u *
        (unsigned)cpw * 1000000u / hz;
    const uint64_t start = time_us_64(), deadline = start + (uint64_t)ms*1000;
    iqout_start();
    tx_worker_start(encoder_task, 0);
    while (time_us_64() < deadline) {
        if (iqout_take_txstall()) ++txstalls;
        if (txstalls) errors |= ERR_TXSTALL;
        if (ring.underruns) errors |= ERR_UNDERRUN;
        if (ring.own_errors) errors |= ERR_OWNERSHIP;
        errors |= live.encoder_errors;
        if (transport_failed()) errors |= ERR_PV;
        if (errors) break;
        if (!captured && time_us_64()-start >= capture_at_us) {
            cap_symbol_cursor = (uint64_t)sequence*BLOCK_SYMBOLS;
            cap_input_words = live.total; cap_done_blocks = ring.done;
            iqout_capture_start(capture, CAP_WORDS);
            captured = true;
        }
        if (sequence-ring.done >= OUT_BLOCKS) { busy_wait_us_32(1); continue; }
        const uint32_t need = (sequence+1)*live.words_per_block, total = live.total;
        if ((int32_t)(total-need) < 0) {
            if (!waiting) ++input_waits;
            waiting = true;
            busy_wait_us_32(1);
            continue;
        }
        if (total-need < min_input) min_input = total-need;
        __dmb();
        const uint32_t stamp = cycles();
        uint32_t prep;
        const uint32_t shape = shape_block(sequence, &prep);
        prepare_cycles += prep; shape_cycles += shape;
        if (prep > worst_prepare) worst_prepare = prep;
        if (shape > worst_shape) worst_shape = shape;
        if (ring.underruns || ring.own_errors || ring.next_arm > sequence) {
            work_cycles += (uint32_t)(cycles()-stamp);
            errors |= ring.own_errors ? ERR_OWNERSHIP : ERR_UNDERRUN;
            break;
        }
        __dmb();
        ring.ready[sequence % OUT_BLOCKS] = sequence+1;
        ++sequence; ++blocks;
        __dmb();
        live.consumed = sequence*live.words_per_block-live.retained_words;
        waiting = false;
        work_cycles += (uint32_t)(cycles()-stamp);
    }
    const uint64_t elapsed_us = time_us_64()-start;
    live.stop = 1;
    __dmb();
    const bool cap_ok = captured && !iqout_capture_busy();
    iqout_stop();
    if (iqout_take_txstall()) ++txstalls;
    tx_worker_wait();
    __dmb();
    pvspi_pause(); pvspi_stop();
    if (txstalls) errors |= ERR_TXSTALL;
    if (ring.underruns) errors |= ERR_UNDERRUN;
    if (ring.own_errors) errors |= ERR_OWNERSHIP;
    errors |= live.encoder_errors;
    if (transport_failed()) errors |= ERR_PV;
    if (!cap_ok && !errors) errors |= ERR_CAPTURE;
    const uint32_t cap_crc = cap_ok ? crc32_update(0, capture, CAP_WORDS*4) : 0;
    const uint64_t cleanup_us = time_us_64()-start-elapsed_us;
    const uint64_t encoder_elapsed = live.end_us-live.start_us;
    uint64_t encoder_cycles = 0;
    for (unsigned n = 0; n < STAGES; ++n) encoder_cycles += live.stage_cycles[n];
    const double budget = (double)elapsed_us*hz/1e6;
    const double encoder_budget = (double)encoder_elapsed*hz/1e6;
    const double rate = hz/(4.0*cpw);
    const char *const profile = modulation == 16 ? IQ16NATIVE8_PROFILE : IQ32NATIVE8_PROFILE;
    printf("@{\"cmd\":\"benchapskfec\",\"git\":\"%s\",\"clk_hz\":%lu,\"ms\":%d,\"modulation\":%d,"
           "\"code_index\":8,\"s2_code\":\"normal 5/6\",\"modcod\":%u,\"kbch\":53840,\"pilots\":true,\"gold\":0,"
           "\"bb_matype1\":114,\"outer_radius\":1,\"header_pilot_radius\":1,\"gamma100\":%u,\"gamma1_100\":%u,\"gamma2_100\":%u,"
           "\"normalization\":\"outer_radius_one_unit_header_pilots\",\"interrupts_enabled\":true,"
           "\"history\":\"continuous_frame_nativewords\",\"shape_placement\":\"%s\",\"prepare_kernel\":\"%s\","
           "\"L\":8,\"sps\":2,\"cpw\":%d,\"sym_rate\":%.1f,\"bb_payload_bps\":%.1f,",
           GIT_REV,(unsigned long)hz,ms,modulation,live.profile.modcod,
           modulation==16?270:0,modulation==32?264:0,modulation==32?464:0,
           modulation==16?"flash_cached":"scratch_y",modulation==16?"none":"native_to_offsets_fused",cpw,rate,rate*(BB_BITS-80)/live.profile.frame_symbols);
    printf("\"row_profile\":\"%s\",\"pulse_profile\":\"%s\",\"coefficient_sha256\":\"%s\","
           "\"table_bytes\":%u,\"table_crc\":[%lu,%lu,%lu],\"frame_symbols\":%lu,"
           "\"native_words\":%u,\"symbols_per_word\":%lu,\"words_per_block\":%lu,\"retained_words\":%lu,"
           "\"publication_words\":%u,\"prefill_frames\":1,\"prefill_words\":%lu,\"prefill_fill\":%lu,\"prefill_crc\":%lu,",
           profile,IQ32NATIVE8_PULSE_PROFILE,IQ32NATIVE8_COEFFICIENT_SHA256,modulation==16?139264:117912,
           (unsigned long)table_crc[0],(unsigned long)table_crc[1],(unsigned long)table_crc[2],(unsigned long)live.profile.frame_symbols,
           IN_WORDS,(unsigned long)live.symbols_per_word,(unsigned long)live.words_per_block,(unsigned long)live.retained_words,
           PUBLICATION_WORDS,(unsigned long)prefill_words,(unsigned long)prefill_fill,(unsigned long)prefill_crc);
    printf("\"elapsed_us\":%llu,\"cleanup_us\":%llu,\"errors\":%lu,\"blocks\":%lu,\"generated_blocks\":%lu,\"done_blocks\":%lu,"
           "\"block_symbols\":1024,\"out_blocks\":4,\"out_words\":2048,\"input_waits\":%lu,\"min_input_words\":%lu,"
           "\"input_published_words\":%lu,\"input_consumed_words\":%lu,\"stream_words\":%lu,\"stream_fill\":%lu,\"mapped_symbols\":%llu,"
           "\"underruns\":%lu,\"own_errors\":%lu,\"idle_sent\":%lu,\"txstalls\":%lu,",
           (unsigned long long)elapsed_us,(unsigned long long)cleanup_us,(unsigned long)errors,(unsigned long)blocks,
           (unsigned long)sequence,(unsigned long)ring.done,(unsigned long)input_waits,(unsigned long)min_input,
           (unsigned long)live.total,(unsigned long)live.consumed,(unsigned long)live.stream.n,(unsigned long)live.stream.fill,
           (unsigned long long)live.stream.n*live.symbols_per_word+live.stream.fill,
           (unsigned long)ring.underruns,(unsigned long)ring.own_errors,(unsigned long)ring.idle_sent,(unsigned long)txstalls);
    printf("\"prepare_cycles\":%llu,\"shape_cycles\":%llu,\"producer_work_cycles\":%llu,"
           "\"prepare_cyc_per_sym\":%.4f,\"gen_cyc_per_sym\":%.4f,\"producer_work_busy\":%.6f,\"gen_busy\":%.6f,"
           "\"worst_prepare_cyc_per_sym\":%.4f,\"worst_shape_cyc_per_sym\":%.4f,\"idle_wait\":\"timer_poll_1us\",",
           (unsigned long long)prepare_cycles,(unsigned long long)shape_cycles,(unsigned long long)work_cycles,
           blocks?(double)prepare_cycles/(blocks*BLOCK_SYMBOLS):0,blocks?(double)shape_cycles/(blocks*BLOCK_SYMBOLS):0,
           budget?work_cycles/budget:0,budget?shape_cycles/budget:0,
           (double)worst_prepare/BLOCK_SYMBOLS,(double)worst_shape/BLOCK_SYMBOLS);
    printf("\"encoder_frames\":%lu,\"encoder_runtime_frames\":%lu,\"encoder_attempted_frames\":%lu,\"encoder_partial_frame\":%s,"
           "\"encoder_elapsed_us\":%llu,\"encoder_busy\":%.6f,\"encoder_credit_wait_cycles\":%llu,\"encoder_max_frame_work_cyc\":%lu,"
           "\"bch_kernel\":%d,\"ldpc_pack\":%u,\"encoder_stage_cycles\":{\"bb\":%llu,\"bb_bch\":%llu,\"ldpc\":%llu,\"map\":%llu},"
           "\"encoder_stage_max_cyc\":{\"bb\":%lu,\"bb_bch\":%lu,\"ldpc\":%lu,\"map\":%lu},",
           (unsigned long)live.frames,(unsigned long)(live.frames-1),(unsigned long)live.attempts,
           live.attempts!=live.frames?"true":"false",(unsigned long long)encoder_elapsed,
           encoder_budget?encoder_cycles/encoder_budget:0,(unsigned long long)live.credit_wait_cycles,
           (unsigned long)live.max_frame_cycles,(int)dvbs2_bch_get_kernel(),dvbs2_ldpc_get_pack(),
           (unsigned long long)live.stage_cycles[STAGE_BB],(unsigned long long)live.stage_cycles[STAGE_BCH],
           (unsigned long long)live.stage_cycles[STAGE_LDPC],(unsigned long long)live.stage_cycles[STAGE_MAP],
           (unsigned long)live.max_stage_cycles[STAGE_BB],(unsigned long)live.max_stage_cycles[STAGE_BCH],
           (unsigned long)live.max_stage_cycles[STAGE_LDPC],(unsigned long)live.max_stage_cycles[STAGE_MAP]);
    printf("\"fe_crc\":[%lu,%lu,%lu,%lu],",(unsigned long)live.fe_crc[0],(unsigned long)live.fe_crc[1],
           (unsigned long)live.fe_crc[2],(unsigned long)live.fe_crc[3]);
    printf("\"cap_words\":%u,\"cap_crc\":%lu,\"cap_stride\":1,\"cap_symbol_cursor\":%llu,\"cap_search_symbols\":4128,"
           "\"cap_frame_boundary\":%llu,\"cap_input_words\":%lu,\"cap_done_blocks\":%lu,\"pv_spi\":{\"bb_mode\":\"gcs\",\"sck_hz\":%lu,"
           "\"bbframes\":%lu,\"runtime_bbframes\":%lu,\"runtime_messages\":%lu,\"crc_chain\":%lu,"
           "\"bb_crc\":[%lu,%lu,%lu,%lu],\"ok\":%lu,\"lost\":%lu,\"bad_hdr\":%lu,\"bad_crc\":%lu,\"bad_sync\":%lu,",
           cap_ok?CAP_WORDS:0,(unsigned long)cap_crc,(unsigned long long)cap_symbol_cursor,
           (unsigned long long)capture_boundary,(unsigned long)cap_input_words,(unsigned long)cap_done_blocks,(unsigned long)(hz/(2*spi_half)),
           (unsigned long)pv_stats.bbframes,(unsigned long)(pv_stats.bbframes-1),(unsigned long)(pvspi.head-spi_head_start),
           (unsigned long)pv_stats.crc_chain,(unsigned long)live.bb_crc[0],(unsigned long)live.bb_crc[1],
           (unsigned long)live.bb_crc[2],(unsigned long)live.bb_crc[3],(unsigned long)pv_stats.ok,
           (unsigned long)pv_stats.lost,(unsigned long)pv_stats.bad_hdr,(unsigned long)pv_stats.bad_crc,(unsigned long)pv_stats.bad_sync);
    printf("\"ts_packets\":%lu,\"null_packets\":%lu,\"gcs_packets\":%lu,\"gcs_bytes\":%lu,\"gcs_underflows\":%lu,"
           "\"short_msgs\":%lu,\"long_msgs\":%lu,\"overflows\":%lu,\"deferred_msgs\":%lu,\"drain_timeouts\":%lu,\"late_closes\":%lu}}\n",
           (unsigned long)pv_stats.ts_packets,(unsigned long)pv_stats.null_packets,(unsigned long)pv_stats.gcs_packets,
           (unsigned long)pv_stats.gcs_bytes,(unsigned long)pv_stats.gcs_underflows,(unsigned long)pvspi.short_msgs,
           (unsigned long)pvspi.long_msgs,(unsigned long)pvspi.overflows,(unsigned long)pvspi.deferred_msgs,
           (unsigned long)pvspi.drain_timeouts,(unsigned long)pvspi.late_closes);
    if (cap_ok) capture_print(capture);
    restore_buffers(previous_bch);
}
