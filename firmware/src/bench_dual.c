// USB-only digital experiment: direct LUT gather DMA, never an RF-qualified transmitter.
#include "bench.h"

#include <stdio.h>
#include <string.h>

#include "dual_dma.h"
#include "dvbs2.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/sync.h"
#include "iqdesc.h"
#include "iqpair.h"
#include "iqquad.h"
#include "link.h"
#include "pico/multicore.h"
#include "pico/stdlib.h"
#include "pvspi.h"
#include "tx.h"

#include "git_rev.h"

enum dual_source {
    SOURCE_CYCLIC = 0, SOURCE_PRBS = 1, SOURCE_DVBS2_CORE1 = 2, SOURCE_DVBS2_CORE0 = 3,
    SOURCE_DVBS2_SPLIT = 4, SOURCE_PV_SPI = 5
};
enum { SYMBOLS_PER_WORD = 16,
       DESC_LOG2 = 13, DESC_COUNT = 1 << DESC_LOG2, DESC_RING_BYTES = DESC_COUNT * 4,
       DESC_BLOCK_SHIFT = 10, DESC_BLOCK = 1 << DESC_BLOCK_SHIFT, SPLIT_BLOCK_SHIFT = 11,
       MAX_BLOCKS = 4 * DESC_COUNT / DESC_BLOCK, INPUT_BLOCK = DESC_BLOCK / SYMBOLS_PER_WORD,
       FRAME_SLOTS = 2, FRAME_SLOT_MASK = FRAME_SLOTS - 1,
       CAPTURE_WORDS = 4096, BCH_SLICE_WORDS = 128, BB_TOGGLE_STRIDE = 64,
       TS_BB_BITS = 43040, TS_BB_BYTES = TS_BB_BITS / 8, TS_BB_WORDS = TS_BB_BITS / 32,
       TS_MATYPE1 = 0xF2, SPI_HALF_CYCLES = 2,
       SPI_PREFILL_MESSAGES = PV_SLOTS / 2, SPI_PREFILL_TIMEOUT_US = 10000, SPI_TRIAL_DMA_CHANNELS = 12,
       REUSE_GUARD = 8, POLL_US = 32,
       ERR_UNDERRUN = 1u << 16, ERR_OWNERSHIP = 1u << 17 };
_Static_assert(IN_WORDS + CAPTURE_WORDS + DVBS2_FE_WORDS <= CAP_WORDS_MAX, "split FE overlay");
_Static_assert(PV_EMU_WORDS + FRAME_SLOTS * TS_BB_WORDS <= TABLE_WORDS, "SPI BB overlay");
_Static_assert(IQQUAD_WORDS == N_BLOCKS * BLOCK_WORDS, "quad table must fit existing sample ring");
typedef void (*desc_fn)(uint32_t *, uint32_t *, const uint32_t *, uint32_t,
                        uint32_t, uint32_t, uint32_t);
static const desc_fn kernels[] = {iqdesc_c, iqdesc_asm, iqdesc_interp};
static const desc_fn pair_kernels[] = {iqdesc_pair_c, iqdesc_pair_asm, iqdesc_pair_interp};
static const desc_fn quad_kernels[] = {iqdesc_quad_c, iqdesc_quad_asm, iqdesc_quad_interp};
static const char *names[] = {"c", "asm", "interp"};

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
    (row_symbols == 4 ? quad_kernels : row_symbols == 2 ? pair_kernels : kernels)[kernel](
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
        printf("%s{\"name\":\"%s\",\"cyc_per_sym\":%.4f,\"worst_block_cyc_per_sym\":%.4f,"
               "\"crc_i\":%lu,\"crc_q\":%lu,\"crc_ok\":%u}", kernel ? "," : "", names[kernel],
               cps, (double)worst / DESC_BLOCK, (unsigned long)ci, (unsigned long)cq,
               ci == 491185096u && cq == 845440890u);
    }
    printf("]}\n");
}

static struct {
    volatile bool stop;
    volatile uint32_t total, consumed;
    uint32_t frames, prefill, frame_words, max_frame_cyc, max_slice_cyc, max_stage_cyc[3];
    uint64_t busy_cyc, start_us, end_us, bb_cyc;
    uint32_t bb_frames, bb_crc[4], bb_max_cyc;
    volatile uint32_t fe_produced, fe_consumed;
    uint32_t *fe_slots[FRAME_SLOTS];
    volatile uint32_t bb_produced, bb_consumed;
    uint32_t *bb_slots[FRAME_SLOTS];
    bool spi;
    dvbs2_frontend_t front;
    symstream_t stream;
} encoder;

static void build_ts_frame(uint32_t *bb) {
    const uint32_t start = cycles();
    pv_bbframe((uint8_t *)bb, TS_BB_BITS, TS_MATYPE1, pvspi_next_packet);
    if (encoder.bb_frames < 4)
        encoder.bb_crc[encoder.bb_frames] = pv_crc32(0, bb, TS_BB_BYTES);
    encoder.bb_frames++;
    for (unsigned word = 0; word < TS_BB_WORDS; word++)
        bb[word] = __builtin_bswap32(bb[word]);
    const uint32_t elapsed = cycles() - start;
    encoder.bb_cyc += elapsed;
    if (elapsed > encoder.bb_max_cyc) encoder.bb_max_cyc = elapsed;
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
    build_ts_frame(encoder.bb_slots[produced & FRAME_SLOT_MASK]);
    __dmb();
    encoder.bb_produced = produced + 1;
    return true;
}

static void __not_in_flash_func(split_backend)(uint32_t unused) {
    encoder.start_us = bench_now();
    while (!encoder.stop) {
        if (refill_bb_queue()) continue;
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

static void prefill_encoder(enum dual_source source, uint32_t *input, unsigned target_words) {
    const bool spi = source == SOURCE_PV_SPI, split = source >= SOURCE_DVBS2_SPLIT;
    tx_code(5, true);
    encoder = (typeof(encoder)){.stream = {.out = input, .mask = IN_WORDS - 1},
        .frame_words = (dvbs2_plframe_symbols() + 31u) / SYMBOLS_PER_WORD};
    while (split ? encoder.frames < FRAME_SLOTS : encoder.stream.n < target_words) {
        if (split) {
            if (spi) build_ts_frame(bb_buf); else split_flip_bb(encoder.frames);
            dvbs2_frame(bb_buf, &encoder.stream);
            if (!spi) split_flip_bb(encoder.frames);
        } else dvbs2_frame(bb_buf, &encoder.stream);
        encoder.frames++;
    }
    encoder.total = encoder.stream.n;
    encoder.prefill = encoder.frames;
    if (!split) return;

    // Input, capture and FE1 occupy disjoint portions of cap_buf; FE0 is the encoder's buffer.
    encoder.fe_slots[0] = dvbs2_frame_buffer();
    encoder.fe_slots[1] = cap_buf + IN_WORDS + CAPTURE_WORDS;
    for (unsigned slot = 0; slot < FRAME_SLOTS; slot++) {
        if (spi) {
            build_ts_frame(bb_buf);
            dvbs2_frame_begin_into(bb_buf, encoder.fe_slots[slot]);
        } else split_begin_frame(encoder.fe_slots[slot], encoder.prefill + slot);
    }
    encoder.fe_produced = FRAME_SLOTS;
    if (!spi) return;

    // The SPI emulator occupies the first PV_EMU_WORDS of the borrowed Q table.
    encoder.spi = true;
    for (unsigned slot = 0; slot < FRAME_SLOTS; slot++) {
        encoder.bb_slots[slot] = (uint32_t *)cfg.tq + PV_EMU_WORDS + slot * TS_BB_WORDS;
        build_ts_frame(encoder.bb_slots[slot]);
    }
    encoder.bb_produced = FRAME_SLOTS;
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
    printf(",\"ts_busy\":%.4f,\"core1_work_busy\":%.4f,\"pv_spi\":{\"sck_hz\":%lu,\"bb_max_cyc\":%lu,\"bbframes\":%lu,"
           "\"runtime_bbframes\":%lu,\"runtime_messages\":%lu,\"crc_chain\":%lu,"
           "\"bb_crc\":[%lu,%lu,%lu,%lu],\"ok\":%lu,\"lost\":%lu,\"bad_hdr\":%lu,\"bad_crc\":%lu,"
           "\"bad_sync\":%lu,\"ts_packets\":%lu,\"null_packets\":%lu,"
           "\"short_msgs\":%lu,\"long_msgs\":%lu,\"overflows\":%lu,"
           "\"deferred_msgs\":%lu,\"drain_timeouts\":%lu,\"late_closes\":%lu,"
           "\"last_short_words\":%lu,\"last_short_fifo\":%lu,\"last_short_ctrl\":%lu,"
           "\"last_short_pc\":%lu,\"last_short_cs\":%lu}",
           encoder.bb_cyc / encoder_budget, (encoder.busy_cyc + encoder.bb_cyc) / encoder_budget,
           (unsigned long)clock_get_hz(clk_sys) / 4,
           (unsigned long)encoder.bb_max_cyc, (unsigned long)encoder.bb_frames,
           (unsigned long)(encoder.bb_frames - bb_prefill), (unsigned long)(pvspi.head - spi_head_start),
           (unsigned long)pv_stats.crc_chain,
           (unsigned long)encoder.bb_crc[0], (unsigned long)encoder.bb_crc[1],
           (unsigned long)encoder.bb_crc[2], (unsigned long)encoder.bb_crc[3],
           (unsigned long)pv_stats.ok, (unsigned long)pv_stats.lost, (unsigned long)pv_stats.bad_hdr,
           (unsigned long)pv_stats.bad_crc, (unsigned long)pv_stats.bad_sync,
           (unsigned long)pv_stats.ts_packets, (unsigned long)pv_stats.null_packets,
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

// fault:1=wrong valid Q row (reference must reject),2=stop publishing (ownership must reject).
void bench_dual_stream(int cpw, int ms, int kernel, int source, int cap_words, int fault,
                       int schedule, int divider256, int row_symbols) {
    const bool spi = source == SOURCE_PV_SPI, split = source >= SOURCE_DVBS2_SPLIT;
    const bool paired = row_symbols >= 2, quad = row_symbols == 4;
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
        if (quad) iqquad_build(ring_buf); else iqpair_build(ring_buf, cfg.tq);
        if (quad && crc32_update(0, ring_buf, IQQUAD_WORDS * sizeof ring_buf[0]) != IQQUAD_TABLE_CRC) {
            restore_trial_buffers(false, paired);
            printf("@{\"error\":\"L9 quad table CRC mismatch\"}\n");
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
    if (source >= SOURCE_DVBS2_CORE1)
        prefill_encoder(source, input, (blocks + 1) * input_block);
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
            if (!dual_dma_capture(capture, (uint32_t)cap_words)) errors |= DUAL_DMA_ERR_CAPTURE_DMA;
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
    if (spi) pvspi_stop();
    const uint64_t cleanup_us = bench_now() - start - elapsed;
    const double budget = elapsed * 1e-6 * clock_get_hz(clk_sys);
    const double encoder_budget = (encoder.end_us - encoder.start_us) * 1e-6 * clock_get_hz(clk_sys);
    const uint32_t generated = gen_blocks * block_syms;
    printf("@{\"cmd\":\"dualstream\",\"git\":\"%s\",\"clk_hz\":%lu,\"sps\":4,\"L\":%u,"
           "\"filter_beta\":%.1f,\"row_profile\":\"%s\","
           "\"kernel\":\"%s\",\"source\":%d,\"cpw\":%d,\"divider256\":%d,\"schedule\":%d,\"row_symbols\":%d,\"writers\":%u,\"bus_priority_mask\":%u,"
           "\"ms\":%d,\"elapsed_us\":%llu,\"cleanup_us\":%llu,"
           "\"seed\":%lu,\"in_words\":%lu,\"sym_rate\":%.1f,\"tables_ok\":%u,\"fault\":%d,"
           "\"errors\":%lu,\"underruns\":%lu,\"own_errors\":%lu,\"txstalls_i\":%lu,\"txstalls_q\":%lu,"
           "\"read_i\":%llu,\"read_q\":%llu,\"issued_i\":%llu,\"issued_q\":%llu,\"axis_gap_max\":%llu,\"gen_syms\":%lu,"
           "\"gen_cyc_per_sym\":%.4f,\"gen_busy\":%.4f,\"control_busy\":%.4f,\"core0_work_busy\":%.4f,\"core0_active_busy\":%.4f,"
           "\"max_gen_cyc\":%lu,\"max_poll_gap_us\":%lu,\"poll_interval_us\":%u,\"min_lead_syms\":%lld,"
           "\"block_syms\":%u,\"split_bb_stride_words\":%u,\"split_bch_frames\":%lu,\"split_bch_busy\":%.4f,\"split_bch_max_cyc\":%lu,\"split_bch_max_slice_cyc\":%lu,\"split_bch_slices\":%lu,\"split_bch_slice_words\":%u,\"input_waits\":%lu,\"s2_runtime_frames\":%lu,\"s2_busy\":%.4f,\"bch_kernel\":%d,"
           "\"one_core\":%s,\"s2_core\":%d,\"s2_max_frame_cyc\":%lu,\"s2_max_slice_cyc\":%lu,"
           "\"s2_stage_max_cyc\":{\"bb_bch\":%lu,\"ldpc\":%lu,\"map\":%lu},\"s2_incomplete_stage\":%u,"
           "\"cap_symbol_cursor\":%llu,\"cap_words\":%lu,\"cap_crc\":%lu,\"cap_stride\":17,\"cap_divider256\":%u,\"cap_fifo_only\":%u",
           GIT_REV, (unsigned long)clock_get_hz(clk_sys), quad ? IQQUAD_SPAN : 12u,
           quad ? 0.8 : 1.0, quad ? "l9_quad" : paired ? "l12_pair" : "l12_single",
           names[kernel], source, cpw,
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
           (int)dvbs2_bch_get_kernel(), source == SOURCE_DVBS2_CORE0 ? "true" : "false", source >= SOURCE_DVBS2_CORE1 ? (source == SOURCE_DVBS2_CORE0 ? 0 : 1) : -1,
           (unsigned long)(source >= SOURCE_DVBS2_CORE1 ? encoder.max_frame_cyc : 0),
           (unsigned long)(source == SOURCE_DVBS2_CORE0 ? encoder.max_slice_cyc : source == SOURCE_DVBS2_CORE1 ? encoder.max_frame_cyc : 0),
           (unsigned long)(source == SOURCE_DVBS2_CORE0 ? encoder.max_stage_cyc[0] : 0),
           (unsigned long)(source == SOURCE_DVBS2_CORE0 ? encoder.max_stage_cyc[1] : 0),
           (unsigned long)(source == SOURCE_DVBS2_CORE0 ? encoder.max_stage_cyc[2] : 0), encoder_stage,
           (unsigned long long)cap_symbol_cursor, (unsigned long)count,
           (unsigned long)crc32_update(0, capture, count * 4),
           divider256 == 384 ? 256 : divider256 ? (unsigned)divider256 : (unsigned)cpw * 256,
           dual_dma_capture_fifo_only());
    if (spi) print_spi_stats(encoder_budget, bb_prefill, spi_head_start);
    printf("}\n");
    capture_print(capture, count);
    restore_trial_buffers(spi, paired);
}
