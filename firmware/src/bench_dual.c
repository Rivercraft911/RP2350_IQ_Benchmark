// USB-only digital experiment: direct LUT gather DMA, never an RF-qualified transmitter.
#include "bench.h"

#include <stdio.h>
#include <string.h>

#include "dual_dma.h"
#include "dvbs2.h"
#include "dvbs2_codes.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/sync.h"
#include "iqdesc.h"
#include "iqpair.h"
#include "iqquad.h"
#include "iqquadptr.h"
#include "link.h"
#include "pico/multicore.h"
#include "pico/stdlib.h"
#include "pvspi.h"
#include "tx.h"

#include "git_rev.h"

enum dual_source {
    SOURCE_CYCLIC = 0, SOURCE_PRBS = 1, SOURCE_DVBS2_CORE1 = 2, SOURCE_DVBS2_CORE0 = 3,
    SOURCE_DVBS2_SPLIT = 4, SOURCE_PV_SPI = 5, SOURCE_PV_GCS = 6
};
enum { SYMBOLS_PER_WORD = 16,
       DESC_LOG2 = 13, DESC_COUNT = 1 << DESC_LOG2, DESC_RING_BYTES = DESC_COUNT * 4,
       DESC_BLOCK_SHIFT = 10, DESC_BLOCK = 1 << DESC_BLOCK_SHIFT, SPLIT_BLOCK_SHIFT = 11,
       MAX_BLOCKS = 4 * DESC_COUNT / DESC_BLOCK, INPUT_BLOCK = DESC_BLOCK / SYMBOLS_PER_WORD,
       FRAME_SLOTS = 2, FRAME_SLOT_MASK = FRAME_SLOTS - 1,
       CAPTURE_WORDS = 4096, BCH_SLICE_WORDS = 128, BB_TOGGLE_STRIDE = 64,
       SPI_BB_MAX_WORDS = (58192 + 31) / 32, // largest normal-frame BBFRAME
       TS_MATYPE1 = 0xF2, GCS_MATYPE1 = 0x72, GCS_SYNC = 0xB9, SPI_HALF_CYCLES = 2,
       SPI_PREFILL_MESSAGES = PV_SLOTS / 2, SPI_PREFILL_TIMEOUT_US = 10000, SPI_TRIAL_DMA_CHANNELS = 12,
       REUSE_GUARD = 8, POLL_US = 32,
       ERR_UNDERRUN = 1u << 16, ERR_OWNERSHIP = 1u << 17, ERR_INPUT = 1u << 18 };
_Static_assert(IN_WORDS + CAPTURE_WORDS + DVBS2_FE_WORDS <= CAP_WORDS_MAX, "split FE overlay");
_Static_assert(PV_SLOTS * PV_MSG_WORDS <= TABLE_WORDS, "SPI receive slots fit borrowed I table");
_Static_assert(PV_EMU_WORDS + SPI_BB_MAX_WORDS <= TABLE_WORDS, "SPI emulator plus one BB slot");
_Static_assert(SPI_BB_MAX_WORDS <= BB_WORDS, "second SPI BB slot fits bb_buf");
_Static_assert(IQQUAD_WORDS == N_BLOCKS * BLOCK_WORDS, "quad table must fit existing sample ring");
typedef void (*desc_fn)(uint32_t *, uint32_t *, const uint32_t *, uint32_t,
                        uint32_t, uint32_t, uint32_t);
static const desc_fn kernels[] = {iqdesc_c, iqdesc_asm, iqdesc_interp};
static const desc_fn pair_kernels[] = {iqdesc_pair_c, iqdesc_pair_asm, iqdesc_pair_interp};
static const desc_fn quad_kernels[] = {iqdesc_quad_c, iqdesc_quad_asm, iqdesc_quad_interp};
static const desc_fn quad8_kernels[] = {iqdesc_quad8_c, NULL, iqdesc_quad8_interp};
static const char *names[] = {"c", "asm", "interp"};
// Indexed by DUAL_DMA_DAC_CLOCK_*; the DDR modes capture full GPIO0..31 snapshots.
static const char *const dac_clock_names[] = {"hstx_gp16", "ddr_pio_gp28", "ddr_pio_gp28_inverted"};
static iqquad_profile_t quad_profile = IQQUAD_PROFILE_L9_KAISER;

bool bench_dual_set_quad_profile(unsigned profile) {
    if (!iqquad_profile_info((iqquad_profile_t)profile)) return false;
    quad_profile = (iqquad_profile_t)profile;
    return true;
}

static bool spi_source(enum dual_source source) {
    return source == SOURCE_PV_SPI || source == SOURCE_PV_GCS;
}

static uint32_t *descriptors(void) {
    // Each hardware read ring needs 32-KiB alignment. Two rings plus alignment slop fit
    // inside the existing 128-KiB sample ring; no permanent sample/descriptor RAM is added.
    return (uint32_t *)(((uintptr_t)ring_buf + DESC_RING_BYTES - 1u) & ~(uintptr_t)(DESC_RING_BYTES - 1u));
}

static inline uint64_t bench_now(void) {
    timer_hw_t *t = PICO_DEFAULT_TIMER_INSTANCE();
    for (uint32_t hi = t->timerawh;;) {
        const uint32_t lo = t->timerawl, hi2 = t->timerawh;
        if (hi == hi2) return (uint64_t)hi << 32 | lo;
        hi = hi2;
    }
}

static inline dual_dma_progress_t progress_symbols(unsigned row_symbols) {
    dual_dma_progress_t p = dual_dma_progress();
    p.read_i *= row_symbols;
    p.read_q *= row_symbols;
    p.issued_i *= row_symbols;
    p.issued_q *= row_symbols;
    return p;
}

static void generate(unsigned kernel, uint32_t *di, uint32_t *dq, const uint32_t *input,
                     uint32_t prev, unsigned row_symbols, uint32_t base_i, uint32_t base_q, uint32_t nwords) {
    // This bench owns both core0 interpolators; USB and SPI handlers do not use them.
    const desc_fn *quad = quad_profile == IQQUAD_PROFILE_L8_OPTIMIZED_BOTH ? quad8_kernels : quad_kernels;
    (row_symbols == 4 ? quad : row_symbols == 2 ? pair_kernels : kernels)[kernel](
        di, dq, input, nwords, prev, base_i, base_q);
}

void bench_descriptors(int reps) {
    const iq_kernel_info_t *unused;
    bool tables_ok;
    const char *e = tx_shaper("lut_asm_p", 4, 12, &unused, &tables_ok);
    if (e || !tables_ok) { printf("@{\"error\":\"descriptor tables unavailable\"}\n"); return; }
    uint32_t *di = descriptors(), *dq = di + DESC_COUNT;
    printf("@{\"cmd\":\"dualbench\",\"git\":\"%s\",\"clk_hz\":%lu,\"seed\":%lu,"
           "\"in_words\":%d,\"normalized_base_i\":537133056,\"normalized_base_q\":537165824,"
           "\"reps\":%d,\"tables_ok\":%u,\"kernels\":[", GIT_REV,
           (unsigned long)clock_get_hz(clk_sys), (unsigned long)SEED, IN_WORDS, reps, tables_ok);
    for (unsigned kernel = 0; kernel < 3; kernel++) {
        uint32_t ci = 0, cq = 0, worst = 0;
        uint64_t elapsed = 0;
        for (uint32_t off = 0; off < IN_WORDS; off += INPUT_BLOCK) {
            generate(kernel, di, dq, in_buf + off, off ? in_buf[off - 1] : 0,
                     1, (uint32_t)cfg.ti, (uint32_t)cfg.tq, INPUT_BLOCK);
            // Canonical numerical bases make the CRC reproducible across linker placement.
            for (uint32_t j = 0; j < DESC_BLOCK; j++) {
                di[j] += 0x20040000u - (uint32_t)cfg.ti;
                dq[j] += 0x20048000u - (uint32_t)cfg.tq;
            }
            ci = crc32_update(ci, di, DESC_BLOCK * 4);
            cq = crc32_update(cq, dq, DESC_BLOCK * 4);
        }
        const uint32_t save = save_and_disable_interrupts();
        for (int r = 0; r < reps; r++)
            for (uint32_t off = 0; off < IN_WORDS; off += INPUT_BLOCK) {
                const uint32_t t = cycles();
                generate(kernel, di, dq, in_buf + off, off ? in_buf[off - 1] : 0,
                     1, (uint32_t)cfg.ti, (uint32_t)cfg.tq, INPUT_BLOCK);
                const uint32_t dt = cycles() - t;
                elapsed += dt;
                if (dt > worst) worst = dt;
            }
        restore_interrupts(save);
        const double cps = (double)elapsed / ((double)reps * IN_WORDS * 16);
        printf("%s{\"name\":\"%s\",\"code_placement\":\"%s\",\"cyc_per_sym\":%.4f,\"worst_block_cyc_per_sym\":%.4f,"
               "\"crc_i\":%lu,\"crc_q\":%lu,\"crc_ok\":%u}", kernel ? "," : "", names[kernel],
               kernel ? "sram" : "flash_cached", cps, (double)worst / DESC_BLOCK, (unsigned long)ci, (unsigned long)cq,
               ci == 491185096u && cq == 845440890u);
    }
    printf("]}\n");
}

static struct {
    volatile bool stop;
    volatile uint32_t total, consumed;
    uint32_t frames, prefill, frame_words, max_frame_cyc, max_slice_cyc, max_stage_cyc[3];
    uint64_t busy_cyc, start_us, end_us, bb_cyc;
    uint32_t bb_frames, bb_crc[4], bb_max_cyc, bb_bits, bb_bytes, bb_words;
    volatile uint32_t fe_produced, fe_consumed;
    uint32_t *fe_slots[FRAME_SLOTS];
    volatile uint32_t bb_produced, bb_consumed;
    uint32_t *bb_slots[FRAME_SLOTS];
    bool spi, gcs;
    volatile bool input_failed;
    bool ts_words;                      // TS via pv_bbframe_words, context at cap_buf (dualptr only)
    dvbs2_frontend_t front;
    symstream_t stream;
} encoder;

static bool build_spi_frame(uint32_t *bb) {
    const uint32_t start = cycles();
    // Opt-in TS path: pv_bbframe_words already writes swapped words with a zeroed tail.
    const bool words = encoder.ts_words && !encoder.gcs;
    if (encoder.gcs) {
        if (!pv_gcs_bbframe((uint8_t *)bb, encoder.bb_bits, GCS_MATYPE1, GCS_SYNC, pvspi_next_packet)) {
            encoder.input_failed = true;
            return false;
        }
    } else if (words) pv_bbframe_words(bb, encoder.bb_bits, TS_MATYPE1, pvspi_next_packet, (pv_words_t *)cap_buf);
    else pv_bbframe((uint8_t *)bb, encoder.bb_bits, TS_MATYPE1, pvspi_next_packet);
    if (pvspi.errors || pv_stats.lost || pv_stats.bad_hdr || pv_stats.bad_crc ||
        pv_stats.bad_sync || pv_stats.gcs_underflows) {
        encoder.input_failed = true;
        return false;
    }
    // The encoder consumes MSB-first words. A partial final word must have zero low bits
    // after the byte swap; e.g. normal 5/6 has two valid bytes in that last word.
    if (!words)
        for (unsigned byte = encoder.bb_bytes; byte < 4 * encoder.bb_words; byte++)
            ((uint8_t *)bb)[byte] = 0;
    if (encoder.bb_frames < 4) {
        // Provenance CRC over transmission-order bytes; only prefill frames reach here.
        uint32_t crc = 0;
        if (words)
            for (unsigned word = 0; word < encoder.bb_words; word++) {
                const uint32_t bytes = __builtin_bswap32(bb[word]);
                crc = pv_crc32(crc, &bytes, 4 * word + 4 <= encoder.bb_bytes ? 4 : encoder.bb_bytes - 4 * word);
            }
        else crc = pv_crc32(0, bb, encoder.bb_bytes);
        encoder.bb_crc[encoder.bb_frames] = crc;
    }
    encoder.bb_frames++;
    if (!words)
        for (unsigned word = 0; word < encoder.bb_words; word++)
            bb[word] = __builtin_bswap32(bb[word]);
    const uint32_t elapsed = cycles() - start;
    encoder.bb_cyc += elapsed;
    if (elapsed > encoder.bb_max_cyc) encoder.bb_max_cyc = elapsed;
    return true;
}

static void __not_in_flash_func(dual_encoder)(uint32_t unused) {
    encoder.start_us = bench_now();
    while (!encoder.stop) {
        if (encoder.stream.n + encoder.frame_words + 1u - encoder.consumed > IN_WORDS) continue;
        const uint32_t t = cycles();
        dvbs2_frame(bb_buf, &encoder.stream);
        const uint32_t dt = cycles() - t;
        encoder.busy_cyc += dt;
        if (dt > encoder.max_frame_cyc) encoder.max_frame_cyc = dt;
        encoder.frames++;
        __dmb();
        encoder.total = encoder.stream.n;
    }
    encoder.end_us = bench_now();
}

static void split_flip_bb(uint32_t index) {
    if (index & 1u)
        for (unsigned word = 0; word < BB_WORDS; word += BB_TOGGLE_STRIDE) bb_buf[word] ^= 0x80000000u;
}

static void split_begin_frame(uint32_t *frame, uint32_t index) {
    split_flip_bb(index);
    dvbs2_frame_begin_into(bb_buf, frame);
    split_flip_bb(index);
}

static inline bool refill_bb_queue(void) {
    if (!encoder.spi || encoder.bb_produced - encoder.bb_consumed >= FRAME_SLOTS) return false;
    const uint32_t produced = encoder.bb_produced;
    __dmb(); // acquire the frontend's release before overwriting its BB slot
    if (!build_spi_frame(encoder.bb_slots[produced & FRAME_SLOT_MASK])) return false;
    __dmb();
    encoder.bb_produced = produced + 1;
    return true;
}

// dualptr pvkernel 3: core 0 builds the TS BB slots itself, in bounded packet steps with the
// fused adapter, and is then their only producer and consumer; core 1 runs LDPC/map only
// (encoder.spi is cleared after prefill, so refill_bb_queue never runs there). The FE-slot
// handoff between the cores is unchanged. Returns false when there is nothing to build.
// Input health is checked at each frame end, as build_spi_frame does.
static bool ingress_step(unsigned max_packets) {
    pv_words_t *ctx = (pv_words_t *)cap_buf;
    const uint32_t produced = encoder.bb_produced;
    if (encoder.input_failed) return false;
    if (!ctx->active) {
        if (produced - encoder.bb_consumed >= FRAME_SLOTS) return false;
        pv_bbframe_words_begin(ctx, encoder.bb_slots[produced & FRAME_SLOT_MASK], encoder.bb_bits, TS_MATYPE1);
    }
    if (!pv_bbframe_words_step(ctx, pvspi_next_packet, max_packets)) return true;
    if (pvspi.errors || pv_stats.lost || pv_stats.bad_hdr || pv_stats.bad_crc || pv_stats.bad_sync) {
        encoder.input_failed = true;
        return true;
    }
    encoder.bb_frames++;
    __dmb(); // complete slot before begin_frontend acquires it (same core; kept for clarity)
    encoder.bb_produced = produced + 1;
    return true;
}

static void __not_in_flash_func(split_backend)(uint32_t unused) {
    encoder.start_us = bench_now();
    while (!encoder.stop) {
        if (refill_bb_queue()) continue;
        if (encoder.input_failed) break;
        const uint32_t taken = encoder.fe_consumed;
        if (taken == encoder.fe_produced ||
            encoder.stream.n + encoder.frame_words + 1u - encoder.consumed > IN_WORDS) continue;
        __dmb(); // acquire the complete BB+BCH slot
        uint32_t *frame = encoder.fe_slots[taken & FRAME_SLOT_MASK];
        const uint32_t t = cycles();
        dvbs2_frame_ldpc_into(frame);
        dvbs2_frame_finish_from(frame, &encoder.stream);
        const uint32_t dt = cycles() - t;
        encoder.busy_cyc += dt;
        if (dt > encoder.max_frame_cyc) encoder.max_frame_cyc = dt;
        encoder.frames++;
        __dmb();
        encoder.total = encoder.stream.n;
        __dmb(); // release the FE slot only after its output and total are published
        encoder.fe_consumed = taken + 1;
    }
    encoder.end_us = bench_now();
}

static bool spi_resources_available(void) {
    unsigned free_dma = 0;
    for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ch++) free_dma += !dma_channel_is_claimed(ch);
    for (unsigned sm = 0; sm < NUM_PIO_STATE_MACHINES; sm++)
        if (pio_sm_is_claimed(pio1, sm) || pio_sm_is_claimed(pio2, sm)) return false;
    return free_dma >= SPI_TRIAL_DMA_CHANNELS;
}

static bool prefill_spi(void) {
    pv_init(PV_EMU_MSGS);
    pvspi_start(pio1, (uint32_t *)cfg.ti, true, (uint32_t *)cfg.tq, SPI_HALF_CYCLES);
    const uint32_t deadline = time_us_32() + SPI_PREFILL_TIMEOUT_US;
    while (pvspi.head < SPI_PREFILL_MESSAGES && (int32_t)(time_us_32() - deadline) < 0)
        tight_loop_contents();
    return pvspi.head >= SPI_PREFILL_MESSAGES;
}

static void restore_trial_buffers(bool spi, bool paired) {
    if (spi) iq_build_tables(&cfg);
    if (paired) {
        uint32_t seed = SEED;
        prbs_fill(in_buf, IN_WORDS, &seed);
    }
}

static bool prefill_encoder(enum dual_source source, uint32_t *input, unsigned target_words,
                            int code_index, unsigned bb_bits) {
    const bool spi = spi_source(source), split = source >= SOURCE_DVBS2_SPLIT;
    const bool ts_words = encoder.ts_words;      // chosen by the caller before prefill
    tx_code(code_index, true);
    encoder = (typeof(encoder)){.stream = {.out = input, .mask = IN_WORDS - 1},
        .frame_words = (dvbs2_plframe_symbols() + 31u) / SYMBOLS_PER_WORD,
        .gcs = source == SOURCE_PV_GCS, .ts_words = ts_words,
        .bb_bits = bb_bits, .bb_bytes = bb_bits / 8, .bb_words = (bb_bits + 31) / 32};
    while (split ? encoder.frames < FRAME_SLOTS : encoder.stream.n < target_words) {
        if (split) {
            if (spi) {
                if (!build_spi_frame(bb_buf)) return false;
            } else split_flip_bb(encoder.frames);
            dvbs2_frame(bb_buf, &encoder.stream);
            if (!spi) split_flip_bb(encoder.frames);
        } else dvbs2_frame(bb_buf, &encoder.stream);
        encoder.frames++;
    }
    encoder.total = encoder.stream.n;
    encoder.prefill = encoder.frames;
    if (!split) return true;

    // Input, capture and FE1 occupy disjoint portions of cap_buf; FE0 is the encoder's buffer.
    encoder.fe_slots[0] = dvbs2_frame_buffer();
    encoder.fe_slots[1] = cap_buf + IN_WORDS + CAPTURE_WORDS;
    for (unsigned slot = 0; slot < FRAME_SLOTS; slot++) {
        if (spi) {
            if (!build_spi_frame(bb_buf)) return false;
            dvbs2_frame_begin_into(bb_buf, encoder.fe_slots[slot]);
        } else split_begin_frame(encoder.fe_slots[slot], encoder.prefill + slot);
    }
    encoder.fe_produced = FRAME_SLOTS;
    if (!spi) return true;

    // The emulator occupies the first PV_EMU_WORDS of the borrowed Q table. Keep the
    // original two Q-table slots for 2/3. Larger BBFRAMEs borrow bb_buf for slot one:
    // only prefill used it above, and begin_frontend copies a slot before releasing it.
    encoder.spi = true;
    for (unsigned slot = 0; slot < FRAME_SLOTS; slot++) {
        encoder.bb_slots[slot] = slot && PV_EMU_WORDS + FRAME_SLOTS * encoder.bb_words > TABLE_WORDS
            ? bb_buf : (uint32_t *)cfg.tq + PV_EMU_WORDS + slot * encoder.bb_words;
        if (!build_spi_frame(encoder.bb_slots[slot])) return false;
    }
    encoder.bb_produced = FRAME_SLOTS;
    return true;
}

static inline bool begin_frontend(bool spi, uint32_t produced) {
    __dmb(); // acquire both the free FE slot and the complete BB slot
    if (!spi) split_flip_bb(encoder.prefill + produced);
    const uint32_t *bb = spi ? encoder.bb_slots[encoder.bb_consumed & FRAME_SLOT_MASK] : bb_buf;
    uint32_t *frame = encoder.fe_slots[produced & FRAME_SLOT_MASK];
    const bool incremental = dvbs2_frontend_begin(bb, frame, &encoder.front);
    if (!incremental) dvbs2_frame_begin_into(bb, frame);
    if (!spi) split_flip_bb(encoder.prefill + produced);
    if (spi) { __dmb(); encoder.bb_consumed++; } // begin copied all BB input into the FE slot
    return incremental;
}

static void print_spi_stats(double encoder_budget, uint32_t bb_prefill, uint32_t spi_head_start) {
    printf(",\"ts_busy\":%.4f,\"core1_work_busy\":%.4f,\"pv_spi\":{\"bb_mode\":\"%s\",\"sck_hz\":%lu,\"bb_max_cyc\":%lu,\"bbframes\":%lu,"
           "\"runtime_bbframes\":%lu,\"runtime_messages\":%lu,\"crc_chain\":%lu,"
           "\"bb_crc\":[%lu,%lu,%lu,%lu],\"ok\":%lu,\"lost\":%lu,\"bad_hdr\":%lu,\"bad_crc\":%lu,"
           "\"bad_sync\":%lu,\"ts_packets\":%lu,\"null_packets\":%lu,"
           "\"gcs_packets\":%lu,\"gcs_bytes\":%lu,\"gcs_underflows\":%lu,"
           "\"short_msgs\":%lu,\"long_msgs\":%lu,\"overflows\":%lu,"
           "\"deferred_msgs\":%lu,\"drain_timeouts\":%lu,\"late_closes\":%lu,"
           "\"last_short_words\":%lu,\"last_short_fifo\":%lu,\"last_short_ctrl\":%lu,"
           "\"last_short_pc\":%lu,\"last_short_cs\":%lu}",
           encoder.bb_cyc / encoder_budget, (encoder.busy_cyc + encoder.bb_cyc) / encoder_budget,
           encoder.gcs ? "gcs" : "ts",
           (unsigned long)clock_get_hz(clk_sys) / 4,
           (unsigned long)encoder.bb_max_cyc, (unsigned long)encoder.bb_frames,
           (unsigned long)(encoder.bb_frames - bb_prefill), (unsigned long)(pvspi.head - spi_head_start),
           (unsigned long)pv_stats.crc_chain,
           (unsigned long)encoder.bb_crc[0], (unsigned long)encoder.bb_crc[1],
           (unsigned long)encoder.bb_crc[2], (unsigned long)encoder.bb_crc[3],
           (unsigned long)pv_stats.ok, (unsigned long)pv_stats.lost, (unsigned long)pv_stats.bad_hdr,
           (unsigned long)pv_stats.bad_crc, (unsigned long)pv_stats.bad_sync,
           (unsigned long)pv_stats.ts_packets, (unsigned long)pv_stats.null_packets,
           (unsigned long)pv_stats.gcs_packets, (unsigned long)pv_stats.gcs_bytes,
           (unsigned long)pv_stats.gcs_underflows,
           (unsigned long)pvspi.short_msgs, (unsigned long)pvspi.long_msgs, (unsigned long)pvspi.overflows,
           (unsigned long)pvspi.deferred_msgs, (unsigned long)pvspi.drain_timeouts,
           (unsigned long)pvspi.late_closes, (unsigned long)pvspi.last_short_words,
           (unsigned long)pvspi.last_short_fifo, (unsigned long)pvspi.last_short_ctrl,
           (unsigned long)pvspi.last_short_pc, (unsigned long)pvspi.last_short_cs);
}

static void capture_print(const uint32_t *capture, uint32_t count) {
    for (uint32_t i = 0; i < count; i += 16) {
        printf("@cap %lu", (unsigned long)i);
        for (uint32_t j = i; j < i + 16 && j < count; j++) printf(" %08lx", (unsigned long)capture[j]);
        printf("\n");
    }
    if (count) printf("@capend\n");
}

static bool trial_code(int code_index, const char **code_name, unsigned *bb_bits) {
    // Constant member reads let the compiler use the generated metadata without
    // retaining another copy of every LDPC address table in this translation unit.
    switch (code_index) {
    case S2_N2_3:
        *code_name = DVBS2_CODES[S2_N2_3].name;
        *bb_bits = DVBS2_CODES[S2_N2_3].kbch;
        break;
    case S2_N3_4:
        *code_name = DVBS2_CODES[S2_N3_4].name;
        *bb_bits = DVBS2_CODES[S2_N3_4].kbch;
        break;
    case S2_N5_6:
        *code_name = DVBS2_CODES[S2_N5_6].name;
        *bb_bits = DVBS2_CODES[S2_N5_6].kbch;
        break;
    case S2_N8_9:
        *code_name = DVBS2_CODES[S2_N8_9].name;
        *bb_bits = DVBS2_CODES[S2_N8_9].kbch;
        break;
    case S2_N9_10:
        *code_name = DVBS2_CODES[S2_N9_10].name;
        *bb_bits = DVBS2_CODES[S2_N9_10].kbch;
        break;
    default:
        return false;
    }
    return true;
}

// fault:1=wrong valid Q row (reference must reject),2=stop publishing (ownership must reject).
void bench_dual_stream(int cpw, int ms, int kernel, int source, int cap_words, int fault,
                       int schedule, int divider256, int row_symbols, int code_index) {
    const char *code_name;
    unsigned bb_bits;
    if (!trial_code(code_index, &code_name, &bb_bits)) {
        printf("@{\"error\":\"dual trial code_index must be5,6,8,9 or10\"}\n");
        return;
    }
    const bool spi = spi_source(source), split = source >= SOURCE_DVBS2_SPLIT;
    const bool paired = row_symbols >= 2, quad = row_symbols == 4;
    const bool short_pulse = quad && quad_profile == IQQUAD_PROFILE_L8_OPTIMIZED_BOTH;
    const iqquad_profile_info_t *profile = iqquad_profile_info(quad_profile);
    if (short_pulse && kernel == 1) {
        printf("@{\"error\":\"L8 profile supports C or interpolator descriptors\"}\n");
        return;
    }
    encoder.input_failed = false;
    encoder.ts_words = false;                    // the word-output TS adapter is dualptr-only
    if (spi && !spi_resources_available()) {
        printf("@{\"error\":\"PV-SPI trial needs free PIO1/PIO2 and twelve DMA channels\"}\n");
        return;
    }
    const iq_kernel_info_t *unused;
    bool tables_ok;
    const char *e = tx_shaper("lut_asm_p", 4, 12, &unused, &tables_ok);
    if (e || !tables_ok) { printf("@{\"error\":\"dual tables unavailable\"}\n"); return; }
    const unsigned block_shift = split ? SPLIT_BLOCK_SHIFT : DESC_BLOCK_SHIFT;
    const unsigned block_syms = 1u << block_shift;
    const unsigned input_block = block_syms / SYMBOLS_PER_WORD;
    const unsigned rows_per_block = block_syms / row_symbols;
    const unsigned blocks = DESC_COUNT / rows_per_block;
    const unsigned reuse_guard = REUSE_GUARD * row_symbols;
    uint32_t *di = paired ? in_buf : descriptors();
    uint32_t *dq = paired ? link_ring : di + DESC_COUNT;
    uint32_t ready[MAX_BLOCKS];
    uint32_t *capture = paired ? cap_buf + IN_WORDS : cap_buf;
    uint32_t *input = paired ? cap_buf : source >= SOURCE_DVBS2_CORE1 ? link_ring : in_buf;
    const uint32_t input_words = source == SOURCE_CYCLIC ? DESC_COUNT * row_symbols / SYMBOLS_PER_WORD : IN_WORDS;
    const uint32_t base_i = paired ? (uint32_t)ring_buf : (uint32_t)cfg.ti;
    const uint32_t base_q = paired ? (uint32_t)ring_buf : (uint32_t)cfg.tq;
    if (paired) {
        if (quad) iqquad_build_profile(ring_buf, IQQUAD_WORDS, quad_profile);
        else iqpair_build(ring_buf, cfg.tq);
        if (quad && crc32_update(0, ring_buf, profile->words * sizeof ring_buf[0]) != profile->table_crc) {
            restore_trial_buffers(false, paired);
            printf("@{\"error\":\"quad pulse table CRC mismatch\"}\n");
            return;
        }
        uint32_t seed = SEED;
        prbs_fill(input, IN_WORDS, &seed);
    }
    if (spi && !prefill_spi()) {
        pvspi_stop();
        restore_trial_buffers(spi, paired);
        printf("@{\"error\":\"PV-SPI startup prefill timed out\"}\n");
        return;
    }
    if (source >= SOURCE_DVBS2_CORE1 &&
        !prefill_encoder(source, input, (blocks + 1) * input_block, code_index, bb_bits)) {
        if (spi) pvspi_stop();
        restore_trial_buffers(spi, paired);
        printf("@{\"error\":\"PV-SPI generic stream underflow during prefill\"}\n");
        return;
    }
    for (uint32_t b = 0; b < blocks; b++) {
        const uint32_t off = b * input_block;
        generate(kernel, di + b * rows_per_block, dq + b * rows_per_block, input + off,
                 source >= SOURCE_DVBS2_CORE1 && !b ? 0 : input[(off + input_words - 1) & (input_words - 1)],
                 row_symbols, base_i, base_q, input_block);
        if (fault == 1) for (uint32_t j = 0; j < rows_per_block; j++)
            dq[b * rows_per_block + j] = base_q;
        ready[b] = b + 1;
    }
    encoder.consumed = blocks * input_block - 1;
    __dmb();
    if (!dual_dma_set_row_words(2u * row_symbols) ||
        !dual_dma_set_schedule((unsigned)schedule) ||
        !dual_dma_start_div256(di, dq, DESC_LOG2, divider256 ? (unsigned)divider256 : (unsigned)cpw * 256)) {
        printf("@{\"error\":\"%s\"}\n", dual_dma_last_error());
        if (spi) pvspi_stop();
        restore_trial_buffers(spi, paired);
        return;
    }
    const uint64_t start = bench_now(), end = start + (uint64_t)ms * 1000;
    encoder.bb_cyc = 0;
    encoder.bb_max_cyc = 0;
    const uint32_t bb_prefill = encoder.bb_frames, spi_head_start = pvspi.head;
    if (source == SOURCE_DVBS2_CORE1 || split) {
        multicore_fifo_push_blocking((uint32_t)(split ? split_backend : dual_encoder));
        multicore_fifo_push_blocking(0);
    }
    if (source == SOURCE_DVBS2_CORE0) encoder.start_us = start;
    uint64_t gen_cyc = 0, control_cyc = 0, bch_cyc = 0, active_cyc = 0;
    uint64_t last_read = 0, last_poll = start;
    uint64_t axis_gap_max = 0, cap_symbol_cursor = 0;
    uint32_t next = blocks, errors = 0, underruns = 0, own_errors = 0;
    uint32_t gen_blocks = 0, max_gen_cyc = 0, bch_frames = 0, max_bch_cyc = 0, max_bch_slice = 0, bch_slices = 0, max_poll_gap = 0, input_waits = 0;
    int64_t min_lead = DESC_COUNT * row_symbols;
    bool captured = false, input_wait = false;
    unsigned encoder_stage = 0;
    uint32_t encoder_frame_cyc = 0, bch_frame_cyc = 0;
    bool bch_active = false;
    dual_dma_progress_t p = {0};
    for (;;) {
        const uint32_t control_start = cycles();
        const uint64_t now = bench_now();
        if (now >= end) break;
        const uint32_t gap = (uint32_t)(now - last_poll);
        if (gap > max_poll_gap) max_poll_gap = gap;
        last_poll = now;
        p = progress_symbols(row_symbols);
        errors |= p.errors;
        if (encoder.input_failed || (spi && pvspi.errors)) errors |= ERR_INPUT;
        const uint64_t low = p.read_i < p.read_q ? p.read_i : p.read_q;
        const uint64_t high = p.issued_i > p.issued_q ? p.issued_i : p.issued_q;
        if (high - low > axis_gap_max) axis_gap_max = high - low;
        if (source != SOURCE_CYCLIC) {
            for (uint64_t b = (last_read >> block_shift); b <= ((high ? high - 1 : 0) >> block_shift) && high > last_read; b++)
                if (ready[(uint32_t)b & (blocks - 1)] != b + 1) { errors |= ERR_UNDERRUN; underruns++; }
            const int64_t lead = (int64_t)next * block_syms - (int64_t)high;
            if (lead < min_lead) min_lead = lead;
        }
        last_read = high;
        if (cap_words && !captured && now - start >= (uint64_t)ms * 500) {
            cap_symbol_cursor = high;
            const bool started = dual_dma_get_dac_clock() ?
                dual_dma_capture_pins(capture, (uint32_t)cap_words) : dual_dma_capture(capture, (uint32_t)cap_words);
            if (!started) errors |= DUAL_DMA_ERR_CAPTURE_DMA;
            captured = true;
        }
        const bool space = low >= (uint64_t)(next - blocks + 1) * block_syms + reuse_guard;
        const uint32_t need = (next + 1) * input_block;
        const bool available = source < SOURCE_DVBS2_CORE1 || (int32_t)(encoder.total - need) >= 0;
        if (space && !available && !input_wait) { input_waits++; input_wait = true; }
        const bool starving = fault == 2 && now - start >= (uint64_t)ms * 250;
        const bool produce = source != SOURCE_CYCLIC && space && available && !starving;
        control_cyc += cycles() - control_start;
        if (errors || !p.running) { active_cyc += cycles() - control_start; break; }
        if (produce) {
            const uint32_t slot = next & (blocks - 1), off = (next * input_block) & (input_words - 1);
            __dmb(); // acquire published encoder words before descriptor generation reads them
            const uint32_t t = cycles();
            generate(kernel, di + slot * rows_per_block, dq + slot * rows_per_block, input + off,
                     input[(off + input_words - 1) & (input_words - 1)], row_symbols, base_i, base_q, input_block);
            if (fault == 1) for (uint32_t j = 0; j < rows_per_block; j++) dq[slot * rows_per_block + j] = base_q;
            const uint32_t dt = cycles() - t;
            gen_cyc += dt;
            if (dt > max_gen_cyc) max_gen_cyc = dt;
            const uint32_t check_start = cycles();
            p = progress_symbols(row_symbols);
            errors |= p.errors;
            const uint64_t after = p.issued_i > p.issued_q ? p.issued_i : p.issued_q;
            // Publishing after a consumer reached this generation would conceal stale reads.
            if (after + reuse_guard >= (uint64_t)next * block_syms) {
                errors |= ERR_OWNERSHIP; own_errors++;
            } else {
                __dmb();
                ready[slot] = next + 1;
                next++;
                gen_blocks++;
                if (source >= SOURCE_DVBS2_CORE1) { __dmb(); encoder.consumed = next * input_block - 1; }
            }
            control_cyc += cycles() - check_start;
            input_wait = false;
            if (errors) { active_cyc += cycles() - control_start; break; }
        } else if (split &&
                   (bch_active || (encoder.fe_produced - encoder.fe_consumed < FRAME_SLOTS &&
                    (!spi || encoder.bb_consumed != encoder.bb_produced)))) {
            const uint32_t produced = encoder.fe_produced;
            const uint32_t t = cycles();
            bool done;
            if (!bch_active) {
                const bool incremental = begin_frontend(spi, produced);
                done = !incremental;
                bch_active = incremental;
            } else {
                done = dvbs2_frontend_step(&encoder.front, BCH_SLICE_WORDS);
                if (done) bch_active = false;
            }
            const uint32_t dt = cycles() - t;
            bch_cyc += dt;
            bch_frame_cyc += dt;
            bch_slices++;
            if (dt > max_bch_slice) max_bch_slice = dt;
            if (done) {
                if (bch_frame_cyc > max_bch_cyc) max_bch_cyc = bch_frame_cyc;
                bch_frame_cyc = 0;
                bch_frames++;
                __dmb();
                encoder.fe_produced = produced + 1;
            }
            // The next loop checks issued cursors before any descriptor refill; stop also
            // validates the final cursor interval if this slice crosses the run deadline.
        } else if (source == SOURCE_DVBS2_CORE0 &&
                   (encoder_stage || encoder.stream.n + encoder.frame_words + 1u -
                    encoder.consumed <= IN_WORDS)) {
            // A complete frame, including the prior input-history word, is reserved before
            // begin. No encoder output is published until finish returns. Refill takes
            // precedence above; otherwise execute only one stage before checking DMA again.
            const uint32_t t = cycles();
            if (encoder_stage == 0) dvbs2_frame_begin(bb_buf);
            else if (encoder_stage == 1) dvbs2_frame_ldpc();
            else dvbs2_frame_finish(&encoder.stream);
            const uint32_t dt = cycles() - t;
            encoder.busy_cyc += dt;
            encoder_frame_cyc += dt;
            if (dt > encoder.max_slice_cyc) encoder.max_slice_cyc = dt;
            if (dt > encoder.max_stage_cyc[encoder_stage]) encoder.max_stage_cyc[encoder_stage] = dt;
            if (++encoder_stage == 3) {
                encoder_stage = 0;
                if (encoder_frame_cyc > encoder.max_frame_cyc)
                    encoder.max_frame_cyc = encoder_frame_cyc;
                encoder_frame_cyc = 0;
                encoder.frames++;
                __dmb();
                encoder.total = encoder.stream.n;
            }
        } else {
            // Idle wait is excluded from compute utilization, as in the existing producer.
            // Required progress/ownership polling is timed separately above.
            active_cyc += cycles() - control_start;
            const uint64_t until = bench_now() + POLL_US;
            while (bench_now() < until) tight_loop_contents();
            continue;
        }
        active_cyc += cycles() - control_start;
    }
    const uint64_t elapsed = bench_now() - start;
    if (source == SOURCE_DVBS2_CORE0) encoder.end_us = start + elapsed;
    encoder.stop = true;
    if (spi) pvspi_pause();
    dual_dma_stop();
    p = progress_symbols(row_symbols);
    errors |= p.errors | dual_dma_capture_errors();
    const uint64_t final_high = p.issued_i > p.issued_q ? p.issued_i : p.issued_q;
    if (source != SOURCE_CYCLIC) {
        for (uint64_t b = (last_read >> block_shift);
             b <= ((final_high ? final_high - 1 : 0) >> block_shift) && final_high > last_read; b++)
            if (ready[(uint32_t)b & (blocks - 1)] != b + 1) { errors |= ERR_UNDERRUN; underruns++; }
        const int64_t lead = (int64_t)next * block_syms - (int64_t)final_high;
        if (lead < min_lead) min_lead = lead;
    }
    const uint32_t count = captured ? dual_dma_capture_count() : 0;
    if (source == SOURCE_DVBS2_CORE1 || split) multicore_fifo_pop_blocking();
    if (encoder.input_failed || (spi && pvspi.errors)) errors |= ERR_INPUT;
    if (spi) pvspi_stop();
    const uint64_t cleanup_us = bench_now() - start - elapsed;
    const double budget = elapsed * 1e-6 * clock_get_hz(clk_sys);
    const double encoder_budget = (encoder.end_us - encoder.start_us) * 1e-6 * clock_get_hz(clk_sys);
    const uint32_t generated = gen_blocks * block_syms;
    printf("@{\"cmd\":\"dualstream\",\"git\":\"%s\",\"clk_hz\":%lu,\"sps\":4,\"L\":%u,"
           "\"filter_beta\":%s,\"row_profile\":\"%s\","
           "\"code_index\":%d,\"s2_code\":\"%s\",\"kbch\":%u,"
           "\"kernel\":\"%s\",\"descriptor_code_placement\":\"%s\",\"source\":%d,\"cpw\":%d,\"divider256\":%d,\"schedule\":%d,\"row_symbols\":%d,\"writers\":%u,\"bus_priority_mask\":%u,"
           "\"ms\":%d,\"elapsed_us\":%llu,\"cleanup_us\":%llu,"
           "\"seed\":%lu,\"in_words\":%lu,\"sym_rate\":%.1f,\"tables_ok\":%u,\"fault\":%d,"
           "\"errors\":%lu,\"underruns\":%lu,\"own_errors\":%lu,\"txstalls_i\":%lu,\"txstalls_q\":%lu,"
           "\"read_i\":%llu,\"read_q\":%llu,\"issued_i\":%llu,\"issued_q\":%llu,\"axis_gap_max\":%llu,\"gen_syms\":%lu,"
           "\"gen_cyc_per_sym\":%.4f,\"gen_busy\":%.4f,\"control_busy\":%.4f,\"core0_work_busy\":%.4f,\"core0_active_busy\":%.4f,"
           "\"max_gen_cyc\":%lu,\"max_poll_gap_us\":%lu,\"poll_interval_us\":%u,\"min_lead_syms\":%lld,"
           "\"block_syms\":%u,\"split_bb_stride_words\":%u,\"split_bch_frames\":%lu,\"split_bch_busy\":%.4f,\"split_bch_max_cyc\":%lu,\"split_bch_max_slice_cyc\":%lu,\"split_bch_slices\":%lu,\"split_bch_slice_words\":%u,\"input_waits\":%lu,\"s2_runtime_frames\":%lu,\"s2_busy\":%.4f,\"bch_kernel\":%d,"
           "\"map_kernel\":%u,\"ldpc_pack\":%u,\"bch_bank\":%u,\"one_core\":%s,\"s2_core\":%d,\"s2_max_frame_cyc\":%lu,\"s2_max_slice_cyc\":%lu,"
           "\"s2_stage_max_cyc\":{\"bb_bch\":%lu,\"ldpc\":%lu,\"map\":%lu},\"s2_incomplete_stage\":%u,"
           "\"cap_symbol_cursor\":%llu,\"cap_words\":%lu,\"cap_crc\":%lu,\"cap_stride\":17,\"cap_divider256\":%u,\"cap_fifo_only\":%u,"
           "\"dac_clock\":\"%s\",\"dac_clock_pio\":%d,\"dac_clock_sm\":%d,\"cap_format\":\"%s\"",
           GIT_REV, (unsigned long)clock_get_hz(clk_sys), quad ? profile->span : 12u,
           short_pulse ? "null" : quad ? "0.8" : "1.0",
           short_pulse ? profile->id : quad ? "l9_quad" : paired ? "l12_pair" : "l12_single",
           code_index, code_name, bb_bits,
           names[kernel], kernel ? "sram" : "flash_cached", source, cpw,
           divider256 ? divider256 : cpw * 256, schedule, row_symbols, dual_dma_get_writers(), dual_dma_get_bus_priority(), ms,
           (unsigned long long)elapsed, (unsigned long long)cleanup_us,
           (unsigned long)SEED, (unsigned long)input_words,
           (double)clock_get_hz(clk_sys) * 256 / (8 * (divider256 ? divider256 : cpw * 256)),
           tables_ok, fault,
           (unsigned long)errors, (unsigned long)underruns, (unsigned long)own_errors,
           (unsigned long)p.txstalls_i, (unsigned long)p.txstalls_q,
           (unsigned long long)p.read_i, (unsigned long long)p.read_q,
           (unsigned long long)p.issued_i, (unsigned long long)p.issued_q,
           (unsigned long long)axis_gap_max, (unsigned long)generated,
           generated ? (double)gen_cyc / generated : 0, gen_cyc / budget,
           control_cyc / budget, (gen_cyc + control_cyc + (source == SOURCE_DVBS2_CORE0 ? encoder.busy_cyc : bch_cyc)) / budget,
           active_cyc / budget,
           (unsigned long)max_gen_cyc, (unsigned long)max_poll_gap, POLL_US,
           (long long)min_lead, block_syms, source == SOURCE_DVBS2_SPLIT ? BB_TOGGLE_STRIDE : 0u, (unsigned long)bch_frames, bch_cyc / budget,
           (unsigned long)max_bch_cyc, (unsigned long)max_bch_slice,
           (unsigned long)bch_slices, BCH_SLICE_WORDS, (unsigned long)input_waits,
           (unsigned long)(source >= SOURCE_DVBS2_CORE1 ? encoder.frames - encoder.prefill : 0),
           source >= SOURCE_DVBS2_CORE1 && encoder_budget ? encoder.busy_cyc / encoder_budget : 0,
           (int)dvbs2_bch_get_kernel(), dvbs2_map_get_kernel(), dvbs2_ldpc_get_pack(), dvbs2_frontend_get_bank(),
           source == SOURCE_DVBS2_CORE0 ? "true" : "false", source >= SOURCE_DVBS2_CORE1 ? (source == SOURCE_DVBS2_CORE0 ? 0 : 1) : -1,
           (unsigned long)(source >= SOURCE_DVBS2_CORE1 ? encoder.max_frame_cyc : 0),
           (unsigned long)(source == SOURCE_DVBS2_CORE0 ? encoder.max_slice_cyc : source == SOURCE_DVBS2_CORE1 ? encoder.max_frame_cyc : 0),
           (unsigned long)(source == SOURCE_DVBS2_CORE0 ? encoder.max_stage_cyc[0] : 0),
           (unsigned long)(source == SOURCE_DVBS2_CORE0 ? encoder.max_stage_cyc[1] : 0),
           (unsigned long)(source == SOURCE_DVBS2_CORE0 ? encoder.max_stage_cyc[2] : 0), encoder_stage,
           (unsigned long long)cap_symbol_cursor, (unsigned long)count,
           (unsigned long)crc32_update(0, capture, count * 4),
           divider256 == 384 ? 256 : divider256 ? (unsigned)divider256 : (unsigned)cpw * 256,
           dual_dma_capture_fifo_only(),
           dac_clock_names[dual_dma_get_dac_clock()], dual_dma_dac_clock_pio(), dual_dma_dac_clock_sm(),
           dual_dma_get_dac_clock() ? "pins32" : "bus16x2");
    if (spi) print_spi_stats(encoder_budget, bb_prefill, spi_head_start);
    printf("}\n");
    capture_print(capture, count);
    restore_trial_buffers(spi, paired);
}

// The pv_words_t context lives in cap_buf's input portion, which this path never reads.
_Static_assert(sizeof(pv_words_t) <= IN_WORDS * sizeof(uint32_t), "TS word context fits before capture");

// Live SPI/FEC with hardware-only shaping. Core0 performs bounded BB+BCH slices,
// core1 retains ordered LDPC/map. Four writers retain one RX consumer per axis.
// pv_kernel (TS only): 0 pv_bbframe + swap pass (default), 1 pv_bbframe_words with its
// Arm leaf copied into the unused part of the pointer-table arena, 2 the same with the
// portable C leaf from flash (an A/B control for placement and for the Arm leaf itself),
// 3 as 1 but core 0 builds the BB frames in bounded packet steps (ingress_step).
static void bench_dual_pointer_writers(int cpw, int ms, int source, int fault, int code_index,
                                       unsigned writers, int pv_kernel) {
    const char *command = writers == 4 ? "dualptr4" : "dualptr";
    const PIO pointer_pio = writers == 4 ? pio0 : pio2;
    bool resources = spi_resources_available();
    if (writers == 4) {
        for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ch++)
            resources &= !dma_channel_is_claimed(ch);
        for (unsigned sm = 0; sm < NUM_PIO_STATE_MACHINES; sm++)
            resources &= !pio_sm_is_claimed(pio0, sm);
    }
    // A packet chunk costs about 1.1k cycles; four stay well under the 128 us poll interval.
    enum { PTR_POLL_US = 128, PTR_MAX_POLL_US = 256, PTR_BCH_SLICE_WORDS = 96, PTR_INGRESS_PACKETS = 4,
           ERR_POINTER = 1u << 19, ERR_CAPTURE = 1u << 20, ERR_POLL = 1u << 21,
           ERR_CONFIG = 1u << 22, ERR_CREDIT = 1u << 23, ERR_CLEANUP = 1u << 24 };
    const char *code_name;
    unsigned bb_bits;
    if (!trial_code(code_index, &code_name, &bb_bits) || code_index == S2_N3_4 ||
        (cpw != 1 && cpw != 2) || ms < 100 || ms > 60000 ||
        !spi_source(source) || (fault != 0 && fault != 1) ||
        (dvbs2_bch_get_kernel() != S2_BCH_STREAM_ARM && dvbs2_bch_get_kernel() != S2_BCH_T8_ARM) || dvbs2_map_get_kernel() != 1 ||
        dvbs2_ldpc_get_pack() != 1 || !resources ||
        iqquadptr_rx_fifo(0) || iqquadptr_rx_fifo(1) ||
        pv_kernel < 0 || pv_kernel > 3 || (pv_kernel && source != SOURCE_PV_SPI)) {
        printf("@{\"cmd\":\"%s\",\"error\":\"needs cpw1/2,100..60000ms,source5/6,fault0/1,code5/8/9/10,bchkernel2/3,mapkernel1,ldpcpack1,pvkernel0..3 (1-3 TS only) and idle resources\"}\n", command);
        return;
    }
    const unsigned saved_writers = dual_dma_get_writers(), saved_rows = dual_dma_get_row_words();
    const unsigned saved_schedule = dual_dma_get_schedule(), saved_priority = dual_dma_get_bus_priority();
    const iqquad_profile_t pulse = IQQUAD_PROFILE_L8_OPTIMIZED_BOTH;
    const iqquadptr_profile_info_t *profile = iqquadptr_profile_info(pulse);
    const uint32_t clock_hz = clock_get_hz(clk_sys);
    const uint32_t denominator = profile->cycles_per_input_word * 1000000u;
    const uint32_t issue_budget = IQQUADPTR_REUSE_GUARD_WORDS +
        ((uint64_t)clock_hz * (PTR_MAX_POLL_US + 1u) + denominator - 1u) / denominator;
    const uintptr_t arena = (uintptr_t)ring_buf, arena_end = arena + sizeof ring_buf;
    const uintptr_t base = (arena + profile->table_alignment - 1u) & ~(uintptr_t)(profile->table_alignment - 1u);
    uint32_t *capture = cap_buf + IN_WORDS;
    uint32_t errors = 0, table_crc = 0, elapsed = 0, max_gap = 0, count = 0;
    uint32_t bch_frames = 0, bch_slices = 0, max_bch_slice = 0, polls = 0;
    uint32_t bb_prefill = 0, spi_head_start = 0, pointer_sm_mask = 0, other_pio_flags = 0;
    uintptr_t pv_leaf = 0;
    uint64_t control_cyc = 0, bch_cyc = 0, cap_symbol_cursor = 0, frozen_granted = 0, ingress_cyc = 0;
    uint32_t ingress_steps = 0, max_ingress_step = 0;
    const bool core0_ingress = pv_kernel == 3;
    uint64_t min_runway = UINT64_MAX;
    bool spi_started = false, pointer_attempted = false, output_attempted = false, worker_started = false;
    bool captured = false, frozen = false, bch_active = false, selectors_ok = false, restored = false;
    const char *reason = "complete";
    iqquadptr_progress_t p = {0};
    dual_dma_faults_t output = {0};
    const iq_kernel_info_t *unused;
    bool tables_ok;
    const char *e = tx_shaper("lut_asm_p", 4, 12, &unused, &tables_ok);
    if (e || !tables_ok || base + 4 * IQQUAD_L8_WORDS > arena_end ||
        !dual_dma_set_writers(writers) || !dual_dma_set_row_words(8) ||
        !dual_dma_set_schedule(0) || !dual_dma_set_bus_priority(saved_priority)) {
        errors |= ERR_CONFIG; reason = "tables, arena or output selectors unavailable"; goto cleanup;
    }
    if (!iqquadptr_build_table_profile((uint32_t *)base, (arena_end - base) / 4, pulse) ||
        (table_crc = crc32_update(0, (const void *)base, 4 * IQQUAD_L8_WORDS)) != profile->table_crc) {
        errors |= ERR_CONFIG; reason = "pointer table CRC mismatch"; goto cleanup;
    }
    encoder.ts_words = false;
    if (pv_kernel) {
        // Kernel 1 runs the leaf from striped SRAM0-3, where core 1's other hot code lives,
        // using whichever side of the 64 KiB table the arena leaves free.
        const uintptr_t table_end = base + 4 * IQQUAD_L8_WORDS, need = pv_words_code_bytes();
        if (pv_kernel != 2)
            pv_leaf = arena_end - table_end >= need ? table_end : base - arena >= need ? arena : 0;
        if ((pv_kernel != 2 && !pv_leaf) ||
            !pv_words_prepare((pv_words_t *)cap_buf, (void *)pv_leaf, (uint32_t)need)) {
            errors |= ERR_CONFIG; reason = "TS word leaf arena"; goto cleanup;
        }
        encoder.ts_words = true;
    }
    spi_started = true;
    if (!prefill_spi() || !prefill_encoder(source, in_buf, 0, code_index, bb_bits)) {
        errors |= ERR_INPUT; reason = "SPI/encoder prefill failed"; goto cleanup;
    }
    // Prefill filled both BB slots on this core. From here on core 1 must not refill them.
    if (core0_ingress) encoder.spi = false;
    // in_buf is a naturally32KiB-aligned ring. The old descriptor path used it
    // for descriptors; cap_buf's input portion is no longer read by this path.
    encoder.consumed = 0;
    pointer_attempted = true;
    if (!iqquadptr_prepare_profile(pointer_pio, in_buf, 13, (const uint32_t *)base, 0, encoder.total, pulse)) {
        errors |= ERR_POINTER; reason = iqquadptr_last_error(); goto cleanup;
    }
    for (unsigned sm = 0; sm < NUM_PIO_STATE_MACHINES; sm++)
        if (iqquadptr_rx_fifo(0) == &pointer_pio->rxf[sm] || iqquadptr_rx_fifo(1) == &pointer_pio->rxf[sm])
            pointer_sm_mask |= 1u << sm;
    if (!iqquadptr_start()) { errors |= ERR_POINTER; reason = iqquadptr_last_error(); goto cleanup; }
    output_attempted = true;
    if (!dual_dma_start_fifo(iqquadptr_rx_fifo(0), iqquadptr_rx_fifo(1),
                            iqquadptr_rx_dreq(0), iqquadptr_rx_dreq(1), profile->control_bswap, cpw)) {
        errors |= ERR_CONFIG; reason = dual_dma_last_error(); goto cleanup;
    }
    encoder.bb_cyc = 0; encoder.bb_max_cyc = 0;
    bb_prefill = encoder.bb_frames; spi_head_start = pvspi.head;
    const uint32_t start = time_us_32();
    uint32_t last_poll = start;
    tx_worker_start(split_backend, 0); worker_started = true;
    while ((uint32_t)(time_us_32() - start) < (uint32_t)ms * 1000u) {
        const uint32_t now = time_us_32();
        if ((uint32_t)(now - last_poll) >= PTR_POLL_US || !polls) {
            const uint32_t t = cycles(), gap = now - last_poll;
            last_poll = now; polls++;
            if (gap > max_gap) max_gap = gap;
            if (gap > PTR_MAX_POLL_US) { errors |= ERR_POLL; reason = "source supervision deadline"; }
            if (fault && !frozen && (uint32_t)(now - start) >= (uint32_t)ms * 250u) {
                frozen = true; frozen_granted = p.granted_words;
            }
            const uint32_t total = encoder.total;
            __dmb(); // acquire backend output before releasing its prefix to DMA
            p = frozen ? iqquadptr_progress() : iqquadptr_publish(total);
            output = dual_dma_faults();
            const uint64_t runway = p.granted_words >= p.input_high_words ?
                p.granted_words - p.input_high_words : 0;
            if (runway < min_runway) min_runway = runway;
            // Unlike the cyclic source, a live encoder publishes once per frame.
            // Reserve two worst-case poll intervals before the next source poll.
            if (!frozen && runway < 2u * issue_budget) { errors |= ERR_CREDIT; reason = "source credit runway"; }
            if (p.errors) { errors |= ERR_POINTER; reason = "pointer producer fault"; }
            errors |= output.errors;
            if (encoder.input_failed || pvspi.errors) { errors |= ERR_INPUT; reason = "SPI input fault"; }
            // Only core1 writes symbols. This guarded low watermark releases words
            // read by BOTH axis DMAs, never the high issue cursor or output timing.
            __dmb(); encoder.consumed = (uint32_t)p.reusable_words;
            if (!errors && !captured && (uint32_t)(now - start) >= (uint32_t)ms * 500u) {
                cap_symbol_cursor = p.input_high_words * SYMBOLS_PER_WORD;
                captured = dual_dma_get_dac_clock() ? dual_dma_capture_pins(capture, CAPTURE_WORDS) :
                    dual_dma_capture(capture, CAPTURE_WORDS);
                if (!captured) { errors |= ERR_CAPTURE; reason = dual_dma_last_error(); }
            }
            control_cyc += cycles() - t;
            if (errors) break;
        }
        if (bch_active || (encoder.fe_produced - encoder.fe_consumed < FRAME_SLOTS &&
                           encoder.bb_consumed != encoder.bb_produced)) {
            const uint32_t produced = encoder.fe_produced, t = cycles();
            bool done;
            if (!bch_active) {
                done = !begin_frontend(true, produced);
                bch_active = !done;
            } else {
                done = dvbs2_frontend_step(&encoder.front, PTR_BCH_SLICE_WORDS);
                if (done) bch_active = false;
            }
            const uint32_t dt = cycles() - t;
            bch_cyc += dt; bch_slices++;
            if (dt > max_bch_slice) max_bch_slice = dt;
            if (done) { __dmb(); encoder.fe_produced = produced + 1; bch_frames++; }
        } else if (core0_ingress) {
            const uint32_t t = cycles();
            if (ingress_step(PTR_INGRESS_PACKETS)) {
                const uint32_t dt = cycles() - t;
                ingress_cyc += dt; ingress_steps++;
                if (dt > max_ingress_step) max_ingress_step = dt;
            } else tight_loop_contents();
        } else tight_loop_contents();
    }
    elapsed = time_us_32() - start;
    if ((uint32_t)(time_us_32() - last_poll) > PTR_MAX_POLL_US) errors |= ERR_POLL;
    p = iqquadptr_progress(); output = dual_dma_faults();
    if (p.errors) errors |= ERR_POINTER;
    errors |= output.errors;
cleanup:
    encoder.stop = true;
    if (spi_started) pvspi_pause();
    if (output_attempted) dual_dma_stop(); // stop writers and DDR clock before pointer producer
    if (pointer_attempted) {
        other_pio_flags = pointer_pio->fdebug & ((pointer_sm_mask << PIO_FDEBUG_TXOVER_LSB) |
                                         (pointer_sm_mask << PIO_FDEBUG_RXUNDER_LSB));
        iqquadptr_stop(); p = iqquadptr_progress();
    }
    if (worker_started) tx_worker_wait();
    encoder.ts_words = false;
    if (output_attempted) {
        output = dual_dma_faults(); errors |= output.errors | dual_dma_capture_errors();
        count = captured ? dual_dma_capture_count() : 0;
    }
    if (encoder.input_failed || (spi_started && pvspi.errors)) errors |= ERR_INPUT;
    if (spi_started) pvspi_stop();
    const bool beyond = p.input_high_words > p.granted_words ||
        (frozen && p.input_high_words > frozen_granted);
    if (beyond) errors |= ERR_OWNERSHIP;
    if (p.errors) errors |= ERR_POINTER;
    if (!fault && count != CAPTURE_WORDS) errors |= ERR_CAPTURE;
    selectors_ok = dual_dma_set_writers(2) && dual_dma_set_row_words(saved_rows) &&
        dual_dma_set_writers(saved_writers) && dual_dma_set_schedule(saved_schedule) &&
        dual_dma_set_bus_priority(saved_priority);
    if (!selectors_ok || (p.errors & IQQUADPTR_ERR_STOP)) errors |= ERR_CLEANUP;
    if (!(errors & ERR_CLEANUP)) { restore_trial_buffers(true, true); restored = true; }
    const double budget = (double)elapsed * clock_hz / 1e6;
    const double encoder_budget = worker_started ? (encoder.end_us - encoder.start_us) * clock_hz / 1e6 : 1;
    printf("@{\"cmd\":\"%s\",\"git\":\"%s\",\"clk_hz\":%lu,\"source\":%d,\"cpw\":%d,\"divider256\":%d,"
           "\"ms\":%d,\"fault\":%d,\"elapsed_us\":%lu,\"sym_rate\":%.1f,\"sps\":4,\"L\":8,"
           "\"row_profile\":\"qpsk_l8_n4_both_m010_far2\",\"filter_beta\":null,\"row_symbols\":4,\"writers\":%u,"
           "\"pointer_pio\":%u,\"pointer_routing\":\"%s\","
           "\"schedule\":0,\"bus_priority_mask\":%u,\"code_index\":%d,\"s2_code\":\"%s\",\"kbch\":%u,"
           "\"bch_kernel\":%d,\"map_kernel\":1,\"ldpc_pack\":1,\"bch_bank\":%u,\"frame_parallel\":false,"
           "\"pv_kernel\":%d,\"pv_leaf_base\":%lu,\"descriptor_gen_busy\":0.0,\"sample_math_busy\":0.0,\"table_base\":%lu,\"table_bytes\":65536,\"table_crc\":%lu,"
           "\"input_base\":%lu,\"in_words\":8192,\"errors\":%lu,\"pointer_errors\":%lu,\"output_errors\":%lu,\"reason\":\"%s\","
           "\"txstalls_i\":%lu,\"txstalls_q\":%lu,\"pointer_sm_mask\":%lu,\"pointer_txstall_mask\":%lu,"
           "\"pointer_rxstall_mask\":%lu,\"null_trigger_mask\":%lu,\"pointer_other_pio_flags\":%lu,"
           "\"input_issue_words\":[%llu,%llu],\"published_words\":%llu,\"granted_words\":%llu,\"reusable_words\":%llu,"
           "\"read_beyond_grants\":%s,\"grants_frozen\":%s,\"frozen_granted_words\":%llu,"
           "\"max_poll_gap_us\":%lu,\"poll_interval_us\":%u,\"max_poll_interval_us\":%u,\"polls\":%lu,"
           "\"min_credit_runway_words\":%llu,\"required_credit_runway_words\":%lu,\"source_cursor_deadline_us\":%lu,"
           "\"core0_work_busy\":%.6f,\"supervision_busy\":%.6f,\"split_bch_busy\":%.6f,"
           "\"split_bch_frames\":%lu,\"split_bch_slices\":%lu,\"split_bch_max_slice_cyc\":%lu,\"s2_runtime_frames\":%lu,"
           "\"ingress_core\":%d,\"core0_ingress_busy\":%.6f,\"ingress_steps\":%lu,\"ingress_max_step_cyc\":%lu,"
           "\"ingress_step_packets\":%u,"
           "\"restored\":%s,\"selectors_restored\":%s,\"capture_started\":%s,\"capture_count\":%lu,"
           "\"cap_symbol_cursor\":%llu,\"cap_words\":%lu,\"cap_crc\":%lu,\"cap_stride\":17,\"cap_divider256\":%d,"
           "\"cap_fifo_only\":false,\"cap_format\":\"%s\",\"dac_clock\":\"%s\",\"dac_clock_pio\":%d,\"dac_clock_sm\":%d",
           command, GIT_REV, (unsigned long)clock_hz, source, cpw, cpw * 256, ms, fault, (unsigned long)elapsed,
           (double)clock_hz / (8 * cpw), writers, pio_get_index(pointer_pio),
           writers == 4 ? "alternating_dma" : "paired_dma", dual_dma_get_bus_priority(),
           code_index, code_name, bb_bits, (int)dvbs2_bch_get_kernel(),
           dvbs2_frontend_get_bank(), pv_kernel, (unsigned long)pv_leaf, (unsigned long)base, (unsigned long)table_crc,
           (unsigned long)(uintptr_t)in_buf, (unsigned long)errors, (unsigned long)p.errors, (unsigned long)output.errors, reason,
           (unsigned long)output.txstalls_i, (unsigned long)output.txstalls_q, (unsigned long)pointer_sm_mask,
           (unsigned long)p.txstall_mask, (unsigned long)p.rxstall_mask, (unsigned long)p.null_trigger_mask,
           (unsigned long)other_pio_flags, (unsigned long long)p.input_i_words, (unsigned long long)p.input_q_words,
           (unsigned long long)p.published_words, (unsigned long long)p.granted_words, (unsigned long long)p.reusable_words,
           beyond ? "true" : "false", frozen ? "true" : "false", (unsigned long long)frozen_granted,
           (unsigned long)max_gap, PTR_POLL_US, PTR_MAX_POLL_US, (unsigned long)polls,
           (unsigned long long)(min_runway == UINT64_MAX ? 0 : min_runway), (unsigned long)(2 * issue_budget),
           (unsigned long)((uint64_t)(IN_WORDS - IQQUADPTR_REUSE_GUARD_WORDS) * denominator / clock_hz),
           budget ? (control_cyc + bch_cyc + ingress_cyc) / budget : 0, budget ? control_cyc / budget : 0,
           budget ? bch_cyc / budget : 0, (unsigned long)bch_frames, (unsigned long)bch_slices,
           (unsigned long)max_bch_slice, (unsigned long)(encoder.frames - encoder.prefill),
           core0_ingress ? 0 : 1, budget ? ingress_cyc / budget : 0, (unsigned long)ingress_steps,
           (unsigned long)max_ingress_step, core0_ingress ? (unsigned)PTR_INGRESS_PACKETS : 0u,
           restored ? "true" : "false", selectors_ok ? "true" : "false", captured ? "true" : "false", (unsigned long)count,
           (unsigned long long)cap_symbol_cursor, (unsigned long)count,
           (unsigned long)(count ? crc32_update(0, capture, 4 * count) : 0), cpw * 256,
           dual_dma_get_dac_clock() ? "pins32" : "bus16x2", dac_clock_names[dual_dma_get_dac_clock()],
           dual_dma_dac_clock_pio(), dual_dma_dac_clock_sm());
    print_spi_stats(encoder_budget, bb_prefill, spi_head_start);
    printf("}\n");
    capture_print(capture, count);
}

void bench_dual_pointer(int cpw, int ms, int source, int fault, int code_index, int pv_kernel) {
    bench_dual_pointer_writers(cpw, ms, source, fault, code_index, 2, pv_kernel);
}

void bench_dual_pointer4(int cpw, int ms, int source, int fault, int code_index, int pv_kernel) {
    bench_dual_pointer_writers(cpw, ms, source, fault, code_index, 4, pv_kernel);
}
