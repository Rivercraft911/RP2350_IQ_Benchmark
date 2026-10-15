#include "bench8live.h"

#include <stdio.h>
#include <string.h>

#include "dvbs2_8psk.h"
#include "dvbs2_qpsk8.h"
#include "dvbs2_codes.h"
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/pio.h"
#include "hardware/sync.h"
#include "iq8quad.h"
#include "iqout.h"
#include "link.h"
#include "pico/multicore.h"
#include "pico/stdlib.h"
#include "tx.h"

enum {
    INPUT_GROUPS = IN_WORDS / 2, INPUT_MASK = INPUT_GROUPS - 1,
    BLOCK_GROUPS = 64, BLOCK_SYMBOLS = 16 * BLOCK_GROUPS,
    OUT_WORDS = 32 * BLOCK_GROUPS, OUT_BLOCKS = 4, CAP_WORDS = 4096,
    BB_BITS = 53840, BB_BYTES = BB_BITS / 8, BB_WORDS_USED = (BB_BITS + 31) / 32,
    PREFILL_FRAMES = 2, PREFILL_MESSAGES = 8, PREFILL_TIMEOUT_US = 100000,
    GCS_MATYPE1 = 0x72, GCS_SYNC = 0xb9,
    STAGE_BB = 0, STAGE_BCH, STAGE_LDPC, STAGE_MAP, STAGES,
    ERR_INPUT = 1u << 0, ERR_UNDERRUN = 1u << 1, ERR_OWNERSHIP = 1u << 2,
    ERR_TXSTALL = 1u << 3, ERR_PV = 1u << 4, ERR_CAPTURE = 1u << 5,
};
_Static_assert((INPUT_GROUPS & INPUT_MASK) == 0, "input group ring power of two");
_Static_assert(2 * IQ8QUAD_TABLE_WORDS == N_BLOCKS * BLOCK_WORDS, "8PSK table arena");
_Static_assert(OUT_BLOCKS * OUT_WORDS + CAP_WORDS <= CAP_WORDS_MAX, "output/capture arena");
_Static_assert(PV_SLOTS * PV_MSG_WORDS <= LINK_RING_WORDS, "PV receiver arena");
_Static_assert(PV_EMU_WORDS <= TABLE_WORDS, "PV emulator arena");
_Static_assert(BB_WORDS_USED <= BB_WORDS, "BBFRAME arena");

static struct {
    volatile bool stop;
    bool vcm;
    volatile uint32_t total, consumed, encoder_errors;
    uint32_t frames, bb_frames, bb_crc[4], frame_groups;
    uint32_t qpsk_frames, psk8_frames;
    uint32_t *qpsk_scratch;
    uint32_t max_frame_cycles, max_stage_cycles[STAGES];
    uint64_t stage_cycles[STAGES], start_us, end_us;
    dvbs2_8psk_cfg_t profile;
    dvbs2_8psk_stream_t stream;
} live;

static inline uint64_t now_us(void) {
    timer_hw_t *timer = PICO_DEFAULT_TIMER_INSTANCE();
    for (uint32_t high = timer->timerawh;;) {
        const uint32_t low = timer->timerawl, after = timer->timerawh;
        if (high == after) return (uint64_t)high << 32 | low;
        high = after;
    }
}

static uint64_t vcm_boundary_after(uint64_t symbol) {
    const uint32_t n8 = live.profile.frame_symbols, nq = dvbs2_plframe_symbols();
    const uint32_t period = n8 + nq;
    return symbol / period * period + (symbol % period < n8 ? n8 : period);
}

static bool transport_failed(void) {
    return pvspi.errors || pvspi.short_msgs || pvspi.long_msgs || pvspi.overflows ||
           pvspi.drain_timeouts || pvspi.late_closes;
}

static bool encode_frame(void) {
    uint32_t stage[STAGES];
    const uint32_t begin = cycles();
    // VCM is signalled as ACM in MATYPE-1 (EN302307-1,5.1.6/Table3).
    const uint8_t matype = live.vcm ? GCS_MATYPE1 & ~0x10u : GCS_MATYPE1;
    if (!pv_gcs_bbframe((uint8_t *)bb_buf, BB_BITS, matype, GCS_SYNC, pvspi_next_packet)) {
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
    if (live.vcm && (live.frames & 1u)) {
        if (!dvbs2_qpsk8_frame_from(dvbs2_frame_buffer(), &live.stream,
                                   live.qpsk_scratch, DVBS2_QPSK8_SCRATCH_WORDS)) {
            live.encoder_errors |= ERR_INPUT;
            return false;
        }
        ++live.qpsk_frames;
    } else {
        dvbs2_8psk_frame(&live.profile, dvbs2_frame_buffer(), &live.stream);
        ++live.psk8_frames;
    }
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

static void __not_in_flash_func(encoder_task)(uint32_t unused) {
    live.start_us = now_us();
    while (!live.stop) {
        const uint32_t consumed = live.consumed;
        __dmb(); // acquire the shaper's release, including its retained history group
        if (live.stream.n + live.frame_groups + 1u - consumed > INPUT_GROUPS) continue;
        if (!encode_frame()) break;
    }
    live.end_us = now_us();
    __dmb();
}

static bool resources_available(void) {
    unsigned free_dma = 0, free_pio0 = 0;
    for (unsigned channel = 0; channel < NUM_DMA_CHANNELS; ++channel)
        free_dma += !dma_channel_is_claimed(channel);
    for (unsigned sm = 0; sm < NUM_PIO_STATE_MACHINES; ++sm) {
        if (pio_sm_is_claimed(pio2, sm)) return false;
        free_pio0 += !pio_sm_is_claimed(pio0, sm);
    }
    // Conservative whether iqout already retains its own3 DMA/two SM claims or not.
    return free_dma >= 6 && free_pio0 >= 2;
}

static void restore_buffers(void) {
    iq_build_tables(&cfg);
    uint32_t seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
    memset(link_ring, 0, sizeof link_ring);
}

static void capture_print(const uint32_t *capture) {
    for (unsigned word = 0; word < CAP_WORDS; word += 16) {
        printf("@cap %u", word);
        for (unsigned n = 0; n < 16; ++n) printf(" %08lx", (unsigned long)capture[word + n]);
        putchar('\n');
    }
    printf("@capend\n");
}

static void bench_psk_live(int ms, bool vcm) {
    const uint32_t hz = clock_get_hz(clk_sys);
    if (ms < 10 || ms > 60000 || (hz != 128000000u && hz != 150000000u)) {
        printf("@{\"error\":\"bench8live needs10..60000ms at128 or150MHz\"}\n");
        return;
    }
    if (!resources_available()) {
        printf("@{\"error\":\"bench8live needs idle PIO2, two PIO0 SMs and six free DMA channels\"}\n");
        return;
    }
    const iq_kernel_info_t *unused;
    bool tables_ok;
    if (tx_shaper("lut_asm_p", 4, 12, &unused, &tables_ok) || !tables_ok) {
        printf("@{\"error\":\"bench8live initial LUTs unavailable\"}\n");
        return;
    }
    const iq8quad_tables_t tables = {ring_buf, ring_buf + IQ8QUAD_TABLE_WORDS};
    iq8quad_build(&tables);
    const uint32_t table_crc_a = crc32_update(0, tables.a, 4 * IQ8QUAD_TABLE_WORDS);
    const uint32_t table_crc_b = crc32_update(0, tables.b, 4 * IQ8QUAD_TABLE_WORDS);
    if (table_crc_a != IQ8QUAD_TABLE_A_CRC || table_crc_b != IQ8QUAD_TABLE_B_CRC) {
        restore_buffers();
        printf("@{\"error\":\"bench8live quad table CRC mismatch\"}\n");
        return;
    }
    const dvbs2_code_t *code = &DVBS2_CODES[S2_N5_6];
    tx_code(S2_N5_6, true);
    live = (typeof(live)){.vcm = vcm,
        .stream = {.out = (iq8_input_t *)in_buf, .mask = INPUT_MASK}};
    if (!dvbs2_8psk_init(&live.profile, code, true, 0, cfg.ti, TABLE_WORDS)) {
        restore_buffers();
        printf("@{\"error\":\"bench8live mapper initialization failed\"}\n");
        return;
    }
    const uint32_t qpsk_symbols = dvbs2_plframe_symbols();
    live.frame_groups = ((vcm ? qpsk_symbols : live.profile.frame_symbols) + 15u) / 16u;
    const uint32_t gold_words = dvbs2_8psk_arena_words(code, true);
    if (vcm && gold_words + DVBS2_QPSK8_SCRATCH_WORDS > TABLE_WORDS) {
        restore_buffers();
        printf("@{\"error\":\"VCM mapper scratch arena is too small\"}\n");
        return;
    }
    live.qpsk_scratch = cfg.ti + gold_words;
    const int spi_half = hz == 128000000u ? 2 : 3;
    pv_init(PV_EMU_MSGS);
    pvspi_start(pio2, link_ring, true, cfg.tq, spi_half);
    const uint64_t prefill_start = now_us();
    while (pvspi.head < PREFILL_MESSAGES && now_us() - prefill_start < PREFILL_TIMEOUT_US)
        tight_loop_contents();
    bool prefilled = pvspi.head >= PREFILL_MESSAGES;
    for (unsigned frame = 0; prefilled && frame < PREFILL_FRAMES; ++frame)
        prefilled = encode_frame();
    if (!prefilled || transport_failed()) {
        pvspi_stop();
        restore_buffers();
        printf("@{\"error\":\"bench8live SPI/encoder startup prefill failed\"}\n");
        return;
    }
    const uint32_t prefill_groups = live.total, prefill_fill = live.stream.fill;
    const uint32_t prefill_crc = crc32_update(0, in_buf, prefill_groups * sizeof(iq8_input_t));
    uint32_t *output = cap_buf, *capture = cap_buf + OUT_BLOCKS * OUT_WORDS;
    const iq8_input_t *input = (const iq8_input_t *)in_buf;
    iqout_init(output, OUT_WORDS, OUT_BLOCKS, 4, IQ_LAYOUT_RAW_PAIRS);
    uint32_t sequence = 0;
    for (; sequence < OUT_BLOCKS; ++sequence) {
        const unsigned group = sequence * BLOCK_GROUPS;
        // The first stream history is repeated pi/8, never the cyclic ring's tail.
        iq8quad_kernel_interp(output + sequence * OUT_WORDS, input + group, BLOCK_GROUPS,
                             group ? input + group - 1 : NULL, &tables);
        ring.ready[sequence] = sequence + 1;
    }
    __dmb();
    live.consumed = sequence * BLOCK_GROUPS - 1u;
    memset(live.stage_cycles, 0, sizeof live.stage_cycles);
    memset(live.max_stage_cycles, 0, sizeof live.max_stage_cycles);
    live.max_frame_cycles = 0;
    const uint32_t bb_prefill = live.bb_frames, spi_head_start = pvspi.head;
    uint32_t errors = 0, txstalls = 0, blocks = 0, input_waits = 0, worst_shape = 0;
    uint32_t min_input = INPUT_GROUPS, cap_input_groups = 0, cap_done_blocks = 0;
    uint64_t shape_cycles = 0, cap_symbol_cursor = 0;
    bool captured = false, waiting = false;
    uint64_t capture_boundary = 0;
    if (vcm) {
        capture_boundary = vcm_boundary_after((uint64_t)hz * (unsigned)ms / 32000u);
        // Start at the first completed block within two blocks of the boundary.
        // Leave at least256 symbols after it in the2048-symbol capture window.
        while (capture_boundary % BLOCK_SYMBOLS > 768u)
            capture_boundary = vcm_boundary_after(capture_boundary);
    }
    const uint64_t start = now_us(), deadline = start + (uint64_t)ms * 1000;
    iqout_start();
    multicore_fifo_push_blocking((uint32_t)encoder_task);
    multicore_fifo_push_blocking(0);
    while (now_us() < deadline) {
        if (iqout_take_txstall()) ++txstalls;
        if (txstalls) errors |= ERR_TXSTALL;
        if (ring.underruns) errors |= ERR_UNDERRUN;
        if (ring.own_errors) errors |= ERR_OWNERSHIP;
        errors |= live.encoder_errors;
        if (transport_failed()) errors |= ERR_PV;
        if (errors) break;
        const bool capture_due = vcm ? (uint64_t)ring.done * BLOCK_SYMBOLS + 2u * BLOCK_SYMBOLS >= capture_boundary :
                                       now_us() - start >= (uint64_t)ms * 500;
        if (!captured && capture_due) {
            // Generated samples bound output from above. Four blocks plus a small FIFO
            // allowance bound the preceding host search; this is not an exact bus cursor.
            cap_symbol_cursor = (uint64_t)sequence * BLOCK_SYMBOLS;
            cap_input_groups = live.total;
            cap_done_blocks = ring.done;
            iqout_capture_start(capture, CAP_WORDS);
            captured = true;
        }
        if (sequence - ring.done >= OUT_BLOCKS) { tight_loop_contents(); continue; }
        const uint32_t need = (sequence + 1u) * BLOCK_GROUPS, total = live.total;
        if ((int32_t)(total - need) < 0) {
            if (!waiting) ++input_waits;
            waiting = true;
            tight_loop_contents();
            continue;
        }
        const uint32_t ahead = total - need;
        if (ahead < min_input) min_input = ahead;
        __dmb(); // acquire the whole-frame publication before any input/history load
        const unsigned group = (sequence * BLOCK_GROUPS) & INPUT_MASK;
        const uint32_t stamp = cycles();
        iq8quad_kernel_interp(output + (sequence % OUT_BLOCKS) * OUT_WORDS,
                             input + group, BLOCK_GROUPS,
                             input + ((group - 1u) & INPUT_MASK), &tables);
        const uint32_t elapsed = cycles() - stamp;
        shape_cycles += elapsed;
        if (elapsed > worst_shape) worst_shape = elapsed;
        // The IRQ may have asked for this block while it was being shaped. It sent
        // idle rather than the unfinished buffer; reject that deadline miss now.
        if (ring.underruns || ring.own_errors || ring.next_arm > sequence) {
            errors |= ring.own_errors ? ERR_OWNERSHIP : ERR_UNDERRUN;
            break;
        }
        __dmb();
        ring.ready[sequence % OUT_BLOCKS] = sequence + 1u;
        ++sequence;
        ++blocks;
        __dmb(); // retain the last input group as history for the next shaper block
        live.consumed = sequence * BLOCK_GROUPS - 1u;
        waiting = false;
    }
    const uint64_t elapsed_us = now_us() - start;
    live.stop = true;
    __dmb();
    const bool cap_ok = captured && !iqout_capture_busy();
    iqout_stop(); // stop the bus before waiting for the last core1 frame
    if (iqout_take_txstall()) ++txstalls;
    multicore_fifo_pop_blocking();
    __dmb();
    pvspi_pause();
    pvspi_stop();
    if (txstalls) errors |= ERR_TXSTALL;
    if (ring.underruns) errors |= ERR_UNDERRUN;
    if (ring.own_errors) errors |= ERR_OWNERSHIP;
    errors |= live.encoder_errors;
    if (transport_failed() || pv_stats.lost || pv_stats.bad_hdr || pv_stats.bad_crc ||
        pv_stats.bad_sync || pv_stats.gcs_underflows) errors |= ERR_PV;
    if (!cap_ok && !errors) errors |= ERR_CAPTURE;
    const uint32_t cap_crc = cap_ok ? crc32_update(0, capture, CAP_WORDS * 4) : 0;
    const uint64_t cleanup_us = now_us() - start - elapsed_us;
    const double budget = (double)elapsed_us * hz / 1e6;
    const double encoder_budget = (double)(live.end_us - live.start_us) * hz / 1e6;
    uint64_t encoder_cycles = 0;
    for (unsigned n = 0; n < STAGES; ++n) encoder_cycles += live.stage_cycles[n];
    const double mean_frame_symbols = vcm ? (qpsk_symbols + live.profile.frame_symbols) / 2.0 : live.profile.frame_symbols;
    printf("@{\"cmd\":\"%s\",\"git\":\"%s\",\"clk_hz\":%lu,\"ms\":%d,"
           "\"waveform_plan\":\"%s\",\"bb_matype1\":%u,\"qpsk_frames\":%lu,\"psk8_frames\":%lu,"
           "\"frame_symbols_qpsk\":%lu,\"frame_symbols_8psk\":%lu,"
           "\"code_index\":8,\"s2_code\":\"normal 5/6\",\"modcod\":%u,\"kbch\":53840,"
           "\"pilots\":true,\"gold\":0,\"rotation_pi\":0.125,\"L\":9,\"sps\":2,"
           "\"row_symbols\":4,\"cpw\":4,\"sym_rate\":%.1f,\"bb_payload_bps\":%.1f,"
           "\"frame_symbols\":%lu,\"frame_groups_max\":%lu,\"input_groups\":%u,"
           "\"elapsed_us\":%llu,\"cleanup_us\":%llu,\"errors\":%lu,"
           "\"table_crc_a\":%lu,\"table_crc_b\":%lu,\"prefill_frames\":%u,"
           "\"prefill_groups\":%lu,\"prefill_fill\":%lu,\"prefill_crc\":%lu,"
           "\"blocks\":%lu,\"gen_cyc_per_sym\":%.4f,\"gen_busy\":%.4f,\"worst_block_cyc_per_sym\":%.4f,"
           "\"input_waits\":%lu,\"min_input_groups\":%lu,\"input_published_groups\":%lu,\"input_consumed_groups\":%lu,"
           "\"underruns\":%lu,\"own_errors\":%lu,\"idle_sent\":%lu,\"txstalls\":%lu,"
           "\"encoder_frames\":%lu,\"encoder_runtime_frames\":%lu,\"encoder_busy\":%.4f,"
           "\"encoder_elapsed_us\":%llu,\"encoder_max_frame_cyc\":%lu,\"bch_kernel\":%d,"
           "\"encoder_stage_cycles\":{\"bb\":%llu,\"bb_bch\":%llu,\"ldpc\":%llu,\"map\":%llu},"
           "\"encoder_stage_max_cyc\":{\"bb\":%lu,\"bb_bch\":%lu,\"ldpc\":%lu,\"map\":%lu},"
           "\"cap_words\":%u,\"cap_crc\":%lu,\"cap_stride\":1,\"cap_symbol_cursor\":%llu,"
           "\"cap_search_symbols\":%u,\"cap_input_groups\":%lu,\"cap_done_blocks\":%lu,\"cap_mode_boundary\":%llu,"
           "\"pv_spi\":{\"bb_mode\":\"gcs\",\"sck_hz\":%lu,\"bbframes\":%lu,\"runtime_bbframes\":%lu,"
           "\"runtime_messages\":%lu,\"crc_chain\":%lu,\"bb_crc\":[%lu,%lu,%lu,%lu],"
           "\"ok\":%lu,\"lost\":%lu,\"bad_hdr\":%lu,\"bad_crc\":%lu,\"bad_sync\":%lu,"
           "\"ts_packets\":%lu,\"null_packets\":%lu,\"gcs_packets\":%lu,\"gcs_bytes\":%lu,\"gcs_underflows\":%lu,"
           "\"short_msgs\":%lu,\"long_msgs\":%lu,\"overflows\":%lu,\"deferred_msgs\":%lu,"
           "\"drain_timeouts\":%lu,\"late_closes\":%lu}}\n",
           vcm ? "benchvcm" : "bench8live", GIT_REV, (unsigned long)hz, ms,
           vcm ? "8psk_qpsk_alternating" : "8psk_ccm", vcm ? GCS_MATYPE1 & ~0x10u : GCS_MATYPE1,
           (unsigned long)live.qpsk_frames, (unsigned long)live.psk8_frames,
           (unsigned long)qpsk_symbols, (unsigned long)live.profile.frame_symbols, vcm ? 0 : 15,
           hz / 16.0, (hz / 16.0) * (BB_BITS - 80) / mean_frame_symbols,
           (unsigned long)(vcm ? 0 : live.profile.frame_symbols), (unsigned long)live.frame_groups, INPUT_GROUPS,
           (unsigned long long)elapsed_us, (unsigned long long)cleanup_us, (unsigned long)errors,
           (unsigned long)table_crc_a, (unsigned long)table_crc_b, PREFILL_FRAMES,
           (unsigned long)prefill_groups, (unsigned long)prefill_fill, (unsigned long)prefill_crc,
           (unsigned long)blocks, blocks ? (double)shape_cycles / (blocks * BLOCK_SYMBOLS) : 0,
           shape_cycles / budget, (double)worst_shape / BLOCK_SYMBOLS,
           (unsigned long)input_waits, (unsigned long)min_input, (unsigned long)live.total, (unsigned long)live.consumed,
           (unsigned long)ring.underruns, (unsigned long)ring.own_errors,
           (unsigned long)ring.idle_sent, (unsigned long)txstalls,
           (unsigned long)live.frames, (unsigned long)(live.frames - PREFILL_FRAMES),
           encoder_budget ? encoder_cycles / encoder_budget : 0,
           (unsigned long long)(live.end_us - live.start_us), (unsigned long)live.max_frame_cycles,
           (int)dvbs2_bch_get_kernel(),
           (unsigned long long)live.stage_cycles[STAGE_BB], (unsigned long long)live.stage_cycles[STAGE_BCH],
           (unsigned long long)live.stage_cycles[STAGE_LDPC], (unsigned long long)live.stage_cycles[STAGE_MAP],
           (unsigned long)live.max_stage_cycles[STAGE_BB], (unsigned long)live.max_stage_cycles[STAGE_BCH],
           (unsigned long)live.max_stage_cycles[STAGE_LDPC], (unsigned long)live.max_stage_cycles[STAGE_MAP],
           cap_ok ? CAP_WORDS : 0, (unsigned long)cap_crc, (unsigned long long)cap_symbol_cursor,
           OUT_BLOCKS * BLOCK_SYMBOLS + 32, (unsigned long)cap_input_groups, (unsigned long)cap_done_blocks,
           (unsigned long long)capture_boundary,
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
    if (cap_ok) capture_print(capture);
    restore_buffers();
}

void bench_8live(int ms) { bench_psk_live(ms, false); }
void bench_vcm(int ms) { bench_psk_live(ms, true); }
