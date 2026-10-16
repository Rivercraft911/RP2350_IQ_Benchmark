#include "bench_apsk_live.h"

#include <stdio.h>
#include <string.h>

#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/pio.h"
#include "hardware/sync.h"
#include "iq16fast.h"
#include "iq16maplabels.h"
#include "iq32keys.h"
#include "iq32mapkeys.h"
#include "iq32mapkeys_pair.h"
#include "iq32offsets.h"
#include "iqout.h"
#include "pico/stdlib.h"
#include "tx.h"

enum {
    SOURCE_SYMBOLS = 8192, SOURCE_MASK = SOURCE_SYMBOLS - 1,
    BLOCK_SYMBOLS = 1024, KEY_HISTORY = 5, KEY_WORDS = BLOCK_SYMBOLS + KEY_HISTORY,
    OUT_WORDS = BLOCK_SYMBOLS * 2, OUT_BLOCKS = 4, CAP_WORDS = 4096,
    ERR_UNDERRUN = 1, ERR_OWNERSHIP = 2, ERR_TXSTALL = 4, ERR_CAPTURE = 8,
};
_Static_assert(3 * IQ32NATIVE8_TABLE_WORDS <= N_BLOCKS * BLOCK_WORDS, "table arena");
_Static_assert(SOURCE_SYMBOLS + 7 <= 16384, "32 labels precede pair table");
_Static_assert(16384 + 4 * IQ32MAPKEYS_PAIR_WORDS <= 4 * TABLE_WORDS, "pair table arena");
_Static_assert(4 * IQ16NATIVE8_OLD_WORDS + 8 + SOURCE_SYMBOLS <= 4 * TABLE_WORDS, "16 table/labels arena");
_Static_assert(2 * IQ16NATIVE8_FULL_WORDS == N_BLOCKS * BLOCK_WORDS, "16 full tables arena");
_Static_assert(OUT_BLOCKS * KEY_WORDS <= TABLE_WORDS, "key arena");
_Static_assert(BLOCK_SYMBOLS % 2 == 0, "scaled pair mapper has no odd weighted tail");
_Static_assert(OUT_BLOCKS * OUT_WORDS + CAP_WORDS <= CAP_WORDS_MAX, "output/capture arena");

typedef struct {
    uint64_t map_cycles, shape_cycles, work_cycles;
    uint32_t blocks, map_calls, shape_calls, worst_map, worst_shape, errors;
} work_t;

static struct {
    union { iq16native8_tables_t t16; iq32native8_tables_t t32; } tables;
    uint32_t mapper, modulation, placement;
    volatile uint32_t stop, started, schedule, shaped;
    volatile uint32_t next[2], key_ready[OUT_BLOCKS];
    work_t work[2];
} live;

typedef void (*offset_kernel_fn)(uint32_t *, const uint32_t *, uint32_t,
                                  const uint32_t *, const iq32native8_tables_t *);
static offset_kernel_fn offset_kernel(unsigned core) {
    if (!live.placement) return iq32offsets_kernel_asm;
    return core ? iq32offsets_kernel_sram_x : iq32offsets_kernel_sram_y;
}

static uint8_t *labels(void) {
    return (uint8_t *)cfg.ti + (live.modulation == 16 ? 4 * IQ16NATIVE8_OLD_WORDS + 8 : 7);
}
static uint32_t *map_table(void) { return cfg.ti + 4096; }
static uint32_t *keys(uint32_t sequence) { return cfg.tq + (sequence % OUT_BLOCKS) * KEY_WORDS; }
static uint32_t *output(uint32_t sequence) { return cap_buf + (sequence % OUT_BLOCKS) * OUT_WORDS; }

static void map_block(uint32_t sequence, work_t *work) {
    const uint8_t *const source = labels() + ((sequence * BLOCK_SYMBOLS) & SOURCE_MASK);
    const uint32_t begin = cycles();
    if (live.modulation == 16) {
        iq16maplabels_encode(keys(sequence), source - 8, BLOCK_SYMBOLS / 4 + 2);
    } else {
        iq32mapkeys_state_t state;
        iq32mapkeys_init(&state, source - 7);
        if (live.mapper == 3) {
            uint32_t *const dest = keys(sequence);
            iq32mapkeys_encode(dest, source - KEY_HISTORY, KEY_HISTORY, &state);
            for (unsigned i = 0; i < KEY_HISTORY; ++i) dest[i] <<= 3;
            state.last <<= 3; state.pair <<= 3;
            // The scaled pair table requires even chunks: its odd tail uses
            // the unscaled weighted leaf. This runtime block is exactly1024.
            iq32mapkeys_pair_canonical(dest + KEY_HISTORY, source, BLOCK_SYMBOLS, &state, map_table());
        } else if (live.mapper == 1)
            iq32mapkeys_pair_encode(keys(sequence), source - KEY_HISTORY, KEY_WORDS, &state, map_table());
        else if (live.mapper == 2)
            iq32mapkeys_pair_canonical(keys(sequence), source - KEY_HISTORY, KEY_WORDS, &state, map_table());
        else
            iq32mapkeys_encode(keys(sequence), source - KEY_HISTORY, KEY_WORDS, &state);
    }
    const uint32_t dt = cycles() - begin;
    work->map_cycles += dt;
    ++work->map_calls;
    if (dt > work->worst_map) work->worst_map = dt;
}

static void shape_block(uint32_t sequence, work_t *work) {
    const uint32_t *const input = keys(sequence);
    const uint32_t begin = cycles();
    if (live.modulation == 16)
        iq16fast_pair_asm(output(sequence), input + 2, BLOCK_SYMBOLS / 4, input, &live.tables.t16);
    else if (live.mapper == 3)
        offset_kernel(work == &live.work[1])(output(sequence), input + KEY_HISTORY, BLOCK_SYMBOLS / 2,
                                             input, &live.tables.t32);
    else
        iq32keys_kernel_asm(output(sequence), input + KEY_HISTORY, BLOCK_SYMBOLS / 2,
                            input, &live.tables.t32);
    const uint32_t dt = cycles() - begin;
    work->shape_cycles += dt;
    ++work->shape_calls;
    if (dt > work->worst_shape) work->worst_shape = dt;
}

static bool publish(uint32_t sequence, work_t *work) {
    if (ring.underruns || ring.own_errors || ring.next_arm > sequence) {
        work->errors |= ring.own_errors ? ERR_OWNERSHIP : ERR_UNDERRUN;
        live.stop = 1;
        return false;
    }
    __dmb();
    ring.ready[sequence % OUT_BLOCKS] = sequence + 1u;
    ++work->blocks;
    return true;
}

static void worker(uint32_t unused) {
    (void)unused;
    work_t *const work = &live.work[1];
    uint32_t sequence = live.schedule == 1 ? OUT_BLOCKS : OUT_BLOCKS + 1;
    while (!live.started && !live.stop) tight_loop_contents();
    __dmb();
    while (!live.stop) {
        if (live.schedule == 1) {
            if (sequence - live.shaped >= OUT_BLOCKS) { busy_wait_us_32(1); continue; }
            __dmb();
            const uint32_t begin = cycles();
            map_block(sequence, work);
            __dmb();
            live.key_ready[sequence % OUT_BLOCKS] = sequence + 1u;
            ++sequence;
            live.next[1] = sequence;
            work->work_cycles += (uint32_t)(cycles() - begin);
        } else {
            if (sequence - ring.done >= OUT_BLOCKS) { busy_wait_us_32(1); continue; }
            __dmb();
            const uint32_t begin = cycles();
            map_block(sequence, work);
            shape_block(sequence, work);
            if (publish(sequence, work)) {
                sequence += 2;
                live.next[1] = sequence;
            }
            work->work_cycles += (uint32_t)(cycles() - begin);
        }
    }
}

static bool resources_available(void) {
    unsigned dma = 0, sms = 0;
    for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ++ch) dma += !dma_channel_is_claimed(ch);
    for (unsigned sm = 0; sm < NUM_PIO_STATE_MACHINES; ++sm) sms += !pio_sm_is_claimed(pio0, sm);
    return dma >= 3 && sms >= 2;
}

static void restore_buffers(void) {
    iq_build_tables(&cfg);
    uint32_t seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
}

static uint32_t output_cursor(int schedule) {
    const uint32_t a = live.next[0], b = live.next[1];
    return schedule == 2 && b < a ? b : a;
}

void bench_apsk_live(int cpw, int ms, int schedule, int mapper, int modulation, int placement) {
    const uint32_t hz = clock_get_hz(clk_sys);
    if (cpw < 3 || cpw > 8 || ms < 100 || ms > 60000 || schedule < 0 || schedule > 2 ||
        mapper < 0 || mapper > 3 || placement < 0 || placement > 1 ||
        (placement && (mapper != 3 || modulation != 32)) || (modulation != 16 && modulation != 32) ||
        (modulation == 16 && mapper != 0) || (hz != 128000000u && hz != 150000000u) || !resources_available()) {
        printf("@{\"cmd\":\"benchapsklive\",\"error\":\"needs cpw3..8,100..60000ms,schedule0..2,mapper0..3,modulation16/32 (16 needs mapper0),placement0/1 (SRAM needs32/mapper3),128/150MHz and idle output resources\"}\n");
        return;
    }
    memset(&live, 0, sizeof live);
    live.mapper = mapper;
    live.modulation = modulation;
    live.placement = placement;
    bool built;
    const uint32_t *table_base[3];
    uint32_t table_words[3];
    if (modulation == 16) {
        live.tables.t16 = (iq16native8_tables_t){cfg.ti, ring_buf, ring_buf + IQ16NATIVE8_FULL_WORDS};
        built = iq16native8_build(&live.tables.t16, IQ16NATIVE8_DEFAULT_GAMMA100);
        table_base[0] = live.tables.t16.old; table_base[1] = live.tables.t16.middle; table_base[2] = live.tables.t16.future;
        table_words[0] = IQ16NATIVE8_OLD_WORDS;
        table_words[1] = table_words[2] = IQ16NATIVE8_FULL_WORDS;
    } else {
        live.tables.t32 = (iq32native8_tables_t){ring_buf, ring_buf + IQ32NATIVE8_TABLE_WORDS,
                                                ring_buf + 2 * IQ32NATIVE8_TABLE_WORDS};
        built = iq32native8_build(&live.tables.t32, IQ32NATIVE8_DEFAULT_RATE);
        table_base[0] = live.tables.t32.old; table_base[1] = live.tables.t32.middle; table_base[2] = live.tables.t32.future;
        table_words[0] = table_words[1] = table_words[2] = IQ32NATIVE8_TABLE_WORDS;
    }
    if (!built) {
        restore_buffers();
        printf("@{\"cmd\":\"benchapsklive\",\"error\":\"table construction failed\"}\n");
        return;
    }
    uint32_t table_crc[3];
    const uint32_t expected_crc[2][3] = {
        {IQ16NATIVE8_OLD_CRC, IQ16NATIVE8_MIDDLE_CRC, IQ16NATIVE8_FUTURE_CRC},
        {3255586384u, 916828675u, 1817262751u},
    };
    for (unsigned i = 0; i < 3; ++i) {
        table_crc[i] = crc32_update(0, table_base[i], 4 * table_words[i]);
        if (table_crc[i] != expected_crc[modulation == 32][i]) {
            restore_buffers();
            printf("@{\"cmd\":\"benchapsklive\",\"error\":\"table CRC mismatch\"}\n");
            return;
        }
    }
    uint32_t mapper_crc = 0;
    if (mapper) {
        iq32mapkeys_pair_build(map_table());
        if (mapper == 3)
            for (unsigned i = 0; i < IQ32MAPKEYS_PAIR_WORDS; ++i) map_table()[i] <<= 3;
        mapper_crc = crc32_update(0, map_table(), 4 * IQ32MAPKEYS_PAIR_WORDS);
        if (mapper_crc != (mapper == 3 ? 3614349055u : 2680011457u)) {
            restore_buffers();
            printf("@{\"cmd\":\"benchapsklive\",\"error\":\"mapper table CRC mismatch\"}\n");
            return;
        }
    }
    uint32_t seed = SEED;
    for (unsigned i = 0; i < SOURCE_SYMBOLS; ++i) {
        seed ^= seed << 13; seed ^= seed >> 17; seed ^= seed << 5;
        labels()[i] = seed & (modulation - 1u);
    }
    const unsigned source_history = modulation == 16 ? 8 : 7;
    memcpy(labels() - source_history, labels() + SOURCE_SYMBOLS - source_history, source_history);
    const uint32_t label_crc = crc32_update(0, labels(), SOURCE_SYMBOLS);
    iqout_init(cap_buf, OUT_WORDS, OUT_BLOCKS, cpw, IQ_LAYOUT_RAW_PAIRS);
    work_t prefill = {0};
    for (unsigned sequence = 0; sequence < OUT_BLOCKS; ++sequence) {
        map_block(sequence, &prefill);
        shape_block(sequence, &prefill);
        ring.ready[sequence] = sequence + 1u;
    }
    live.schedule = schedule;
    live.shaped = OUT_BLOCKS;
    live.next[0] = OUT_BLOCKS;
    live.next[1] = schedule == 2 ? OUT_BLOCKS + 1 : OUT_BLOCKS;
    __dmb();
    if (schedule) tx_worker_start(worker, 0);
    uint32_t errors = 0, txstalls = 0, cap_sequence = 0, cap_done = 0;
    bool captured = false;
    const uint64_t start = time_us_64(), deadline = start + (uint64_t)ms * 1000u;
    live.started = 1;
    iqout_start();
    work_t *const work = &live.work[0];
    uint32_t sequence = OUT_BLOCKS;
    while (time_us_64() < deadline && !live.stop) {
        if (iqout_take_txstall()) ++txstalls;
        if (txstalls) errors |= ERR_TXSTALL;
        if (ring.underruns) errors |= ERR_UNDERRUN;
        if (ring.own_errors) errors |= ERR_OWNERSHIP;
        if (errors) break;
        if (!captured && time_us_64() - start >= (uint64_t)ms * 500u) {
            cap_sequence = output_cursor(schedule); cap_done = ring.done;
            iqout_capture_start(cap_buf + OUT_BLOCKS * OUT_WORDS, CAP_WORDS);
            captured = true;
        }
        if (sequence - ring.done >= OUT_BLOCKS ||
            (schedule == 1 && live.key_ready[sequence % OUT_BLOCKS] != sequence + 1u)) {
            busy_wait_us_32(1); continue;
        }
        __dmb();
        const uint32_t begin = cycles();
        if (schedule != 1) map_block(sequence, work);
        shape_block(sequence, work);
        if (publish(sequence, work)) {
            sequence += schedule == 2 ? 2 : 1;
            live.next[0] = sequence;
            live.shaped = sequence;
        }
        work->work_cycles += (uint32_t)(cycles() - begin);
    }
    live.stop = 1;
    __dmb();
    if (schedule) tx_worker_wait();
    const uint64_t elapsed_us = time_us_64() - start;
    const bool cap_ok = captured && !iqout_capture_busy();
    iqout_stop();
    if (iqout_take_txstall()) ++txstalls;
    if (txstalls) errors |= ERR_TXSTALL;
    if (ring.underruns) errors |= ERR_UNDERRUN;
    if (ring.own_errors) errors |= ERR_OWNERSHIP;
    errors |= live.work[0].errors | live.work[1].errors;
    if (!cap_ok && !errors) errors |= ERR_CAPTURE;
    const uint32_t *const capture = cap_buf + OUT_BLOCKS * OUT_WORDS;
    const uint32_t cap_crc = cap_ok ? crc32_update(0, capture, 4 * CAP_WORDS) : 0;
    const uint32_t label_after_crc = crc32_update(0, labels(), SOURCE_SYMBOLS);
    const uint32_t mapper_after_crc = mapper ? crc32_update(0, map_table(), 4 * IQ32MAPKEYS_PAIR_WORDS) : 0;
    const double budget = (double)elapsed_us * hz / 1e6;
    const uint32_t blocks = live.work[0].blocks + live.work[1].blocks;
    printf("@{\"cmd\":\"benchapsklive\",\"git\":\"%s\",\"clk_hz\":%lu,\"ms\":%d,\"cpw\":%d,\"schedule\":%d,"
           "\"modulation\":\"%dAPSK\",\"mapper\":%d,\"mapper_table_bytes\":%u,\"mapper_table_crc\":%lu,\"mapper_table_after_crc\":%lu,"
           "\"L\":8,\"sps\":2,\"rate_index\":%u,\"gamma100\":%u,\"gamma1_100\":%u,\"gamma2_100\":%u,\"outer_radius\":1,"
           "\"row_profile\":\"%s\",\"pulse_profile\":\"%s\",\"coefficient_sha256\":\"%s\","
           "\"table_bytes\":%u,\"old_crc\":%lu,\"middle_crc\":%lu,\"future_crc\":%lu,"
           "\"source_symbols\":%u,\"label_crc\":%lu,\"label_after_crc\":%lu,\"seed\":%lu,\"history\":\"cyclic_source_tail\","
           "\"runtime_encode_included\":true,\"key_prefix_symbols\":%u,\"key_bytes_per_symbol\":%u,\"input_format\":\"%s\",\"placement\":%d,\"code_placement\":\"%s\",\"mapper_code_placement\":\"flash_cached\","
           "\"sym_rate\":%.1f,\"idle_wait\":\"timer_poll_1us\",\"interrupts_enabled\":true,\"elapsed_us\":%llu,"
           "\"errors\":%lu,\"underruns\":%lu,\"own_errors\":%lu,\"idle_sent\":%lu,\"txstalls\":%lu,"
           "\"blocks\":%lu,\"prefill_blocks\":%u,\"done_blocks\":%lu,\"generated_blocks\":%lu,"
           "\"block_symbols\":%u,\"out_blocks\":%u,\"out_words\":%u,\"cores\":[",
           GIT_REV, (unsigned long)hz, ms, cpw, schedule, modulation, mapper,
           mapper ? 4 * IQ32MAPKEYS_PAIR_WORDS : 0, (unsigned long)mapper_crc, (unsigned long)mapper_after_crc,
           modulation == 16 ? 3 : 2, modulation == 16 ? 270 : 0, modulation == 32 ? 264 : 0, modulation == 32 ? 464 : 0,
           modulation == 16 ? IQ16NATIVE8_PROFILE : IQ32NATIVE8_PROFILE,
           IQ32NATIVE8_PULSE_PROFILE, IQ32NATIVE8_COEFFICIENT_SHA256,
           modulation == 16 ? 139264 : 117912, (unsigned long)table_crc[0], (unsigned long)table_crc[1], (unsigned long)table_crc[2],
           SOURCE_SYMBOLS, (unsigned long)label_crc, (unsigned long)label_after_crc, (unsigned long)SEED,
           modulation == 16 ? 8 : KEY_HISTORY, modulation == 16 ? 1 : 4,
           modulation == 16 ? "native_packed_coordinates" : mapper == 3 ? "packed_radix17_byte_offsets" : "rolling_radix17_triples",
           placement, placement ? "per_core_sram" : "flash_cached", hz / (4.0 * cpw), (unsigned long long)elapsed_us,
           (unsigned long)errors, (unsigned long)ring.underruns, (unsigned long)ring.own_errors,
           (unsigned long)ring.idle_sent, (unsigned long)txstalls, (unsigned long)blocks,
           OUT_BLOCKS, (unsigned long)ring.done, (unsigned long)(blocks + OUT_BLOCKS),
           BLOCK_SYMBOLS, OUT_BLOCKS, OUT_WORDS);
    for (unsigned core = 0; core < 2; ++core) {
        const work_t *const w = &live.work[core];
        const uint32_t shaper_address = modulation == 16 ? (uint32_t)iq16fast_pair_asm :
            mapper == 3 ? (uint32_t)offset_kernel(core) : (uint32_t)iq32keys_kernel_asm;
        printf("%s{\"core\":%u,\"shaper_address\":%lu,\"blocks\":%lu,\"map_calls\":%lu,\"shape_calls\":%lu,"
               "\"map_cycles\":%llu,\"shape_cycles\":%llu,\"work_cycles\":%llu,\"work_busy\":%.6f,"
               "\"map_cyc_per_sym\":%.4f,\"shape_cyc_per_sym\":%.4f,"
               "\"worst_map_cyc_per_sym\":%.4f,\"worst_shape_cyc_per_sym\":%.4f}",
               core ? "," : "", core, (unsigned long)shaper_address, (unsigned long)w->blocks, (unsigned long)w->map_calls,
               (unsigned long)w->shape_calls, (unsigned long long)w->map_cycles,
               (unsigned long long)w->shape_cycles, (unsigned long long)w->work_cycles,
               budget ? w->work_cycles / budget : 0,
               w->map_calls ? (double)w->map_cycles / (w->map_calls * BLOCK_SYMBOLS) : 0,
               w->shape_calls ? (double)w->shape_cycles / (w->shape_calls * BLOCK_SYMBOLS) : 0,
               (double)w->worst_map / BLOCK_SYMBOLS, (double)w->worst_shape / BLOCK_SYMBOLS);
    }
    printf("],\"cap_words\":%u,\"cap_crc\":%lu,\"cap_stride\":1,\"cap_symbol_cursor\":%llu,"
           "\"cap_search_symbols\":%u,\"cap_done_blocks\":%lu}\n",
           cap_ok ? CAP_WORDS : 0, (unsigned long)cap_crc,
           (unsigned long long)cap_sequence * BLOCK_SYMBOLS,
           OUT_BLOCKS * BLOCK_SYMBOLS + 32, (unsigned long)cap_done);
    if (cap_ok) {
        for (unsigned word = 0; word < CAP_WORDS; word += 16) {
            printf("@cap %u", word);
            for (unsigned n = 0; n < 16; ++n) printf(" %08lx", (unsigned long)capture[word + n]);
            putchar('\n');
        }
        printf("@capend\n");
    }
    restore_buffers();
}
