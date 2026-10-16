#include "bench32out.h"

#include <stdio.h>

#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/pio.h"
#include "hardware/sync.h"
#include "iq32native8.h"
#include "iq32keys.h"
#include "iqout.h"
#include "pico/stdlib.h"
#include "tx.h"

enum {
    RAW_WORDS = IN_WORDS / 2, INPUT_MASK = IN_WORDS - 1,
    BLOCK_INPUT = 512, BLOCK_SYMBOLS = 2 * BLOCK_INPUT,
    OUT_WORDS = 4 * BLOCK_INPUT, OUT_BLOCKS = 4, CAP_WORDS = 4096,
    ERR_UNDERRUN = 1, ERR_OWNERSHIP = 2, ERR_TXSTALL = 4, ERR_CAPTURE = 8,
};
_Static_assert((IN_WORDS & INPUT_MASK) == 0 && IN_WORDS % BLOCK_SYMBOLS == 0, "cyclic source blocks");
_Static_assert(3 * IQ32NATIVE8_TABLE_WORDS <= N_BLOCKS * BLOCK_WORDS, "native32 table arena");
_Static_assert(RAW_WORDS <= TABLE_WORDS, "coordinate source arena");
_Static_assert(OUT_BLOCKS * OUT_WORDS + CAP_WORDS <= CAP_WORDS_MAX, "output/capture arena");

typedef void (*kernel_fn)(uint32_t *, const uint32_t *, uint32_t, const uint32_t *,
                          const iq32native8_tables_t *);
static const struct { const char *name; kernel_fn run; } kernels[] = {
    {"keys_c", iq32keys_kernel_c}, {"keys_burst", iq32keys_kernel_asm},
};

static void shape_block(uint32_t *output, uint32_t sequence, kernel_fn run,
                         const iq32native8_tables_t *tables) {
    const unsigned input = (sequence * BLOCK_SYMBOLS) & INPUT_MASK;
    run(output, in_buf + input, BLOCK_INPUT,
        in_buf + ((input - 5u) & INPUT_MASK), tables);
}

static bool resources_available(void) {
    unsigned dma = 0, sms = 0;
    for (unsigned ch = 0; ch < NUM_DMA_CHANNELS; ++ch) dma += !dma_channel_is_claimed(ch);
    for (unsigned sm = 0; sm < NUM_PIO_STATE_MACHINES; ++sm) sms += !pio_sm_is_claimed(pio0, sm);
    // Conservative even when iqout retains its own three DMA/two SM claims.
    return dma >= 3 && sms >= 2;
}

static void restore_buffers(void) {
    iq_build_tables(&cfg);
    uint32_t seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
}

void bench_32out(int cpw, int ms, int kernel) {
    const uint32_t hz = clock_get_hz(clk_sys);
    if ((cpw != 4 && cpw != 6 && cpw != 8) || ms < 100 || ms > 60000 ||
        kernel < 0 || kernel >= (int)(sizeof kernels / sizeof kernels[0]) ||
        (hz != 128000000u && hz != 150000000u) ||
        !resources_available()) {
        printf("@{\"cmd\":\"bench32out\",\"error\":\"needs cpw4/6/8,100..60000ms,kernel0..1,128/150MHz and idle output resources\"}\n");
        return;
    }
    const iq32native8_tables_t tables = {ring_buf, ring_buf + IQ32NATIVE8_TABLE_WORDS,
                                         ring_buf + 2 * IQ32NATIVE8_TABLE_WORDS};
    if (!iq32native8_build(&tables, IQ32NATIVE8_DEFAULT_RATE)) {
        restore_buffers();
        printf("@{\"cmd\":\"bench32out\",\"error\":\"32APSK table construction failed\"}\n");
        return;
    }
    const uint32_t old_crc = crc32_update(0, tables.old, 4 * IQ32NATIVE8_TABLE_WORDS);
    const uint32_t middle_crc = crc32_update(0, tables.middle, 4 * IQ32NATIVE8_TABLE_WORDS);
    const uint32_t future_crc = crc32_update(0, tables.future, 4 * IQ32NATIVE8_TABLE_WORDS);
    if (old_crc != 3255586384u || middle_crc != 916828675u || future_crc != 1817262751u) {
        restore_buffers();
        printf("@{\"cmd\":\"bench32out\",\"error\":\"32APSK table CRC mismatch\"}\n");
        return;
    }
    uint32_t seed = SEED;
    iq32native8_fill(cfg.ti, RAW_WORDS, &seed);
    const uint32_t coordinate_crc = crc32_update(0, cfg.ti, RAW_WORDS * 4);
    const uint32_t encode_start = cycles();
    iq32keys_encode(in_buf, cfg.ti, RAW_WORDS, cfg.ti + RAW_WORDS - 1);
    const uint32_t encode_cycles = cycles() - encode_start;
    const uint32_t input_crc = crc32_update(0, in_buf, sizeof in_buf);
    if (coordinate_crc != 2979756864u || input_crc != 2091885109u) {
        restore_buffers();
        printf("@{\"cmd\":\"bench32out\",\"error\":\"32APSK cyclic source CRC mismatch\"}\n");
        return;
    }
    const kernel_fn run = kernels[kernel].run;
    uint32_t *const output = cap_buf, *const capture = cap_buf + OUT_BLOCKS * OUT_WORDS;
    iqout_init(output, OUT_WORDS, OUT_BLOCKS, cpw, IQ_LAYOUT_RAW_PAIRS);
    uint32_t sequence = 0;
    for (; sequence < OUT_BLOCKS; ++sequence) {
        shape_block(output + sequence * OUT_WORDS, sequence, run, &tables);
        ring.ready[sequence] = sequence + 1u;
    }
    __dmb();
    uint32_t errors = 0, txstalls = 0, blocks = 0, shape_calls = 0, worst_shape = 0;
    uint32_t cap_done = 0, cap_sequence = 0;
    uint64_t shape_cycles = 0, work_cycles = 0;
    bool captured = false;
    const uint64_t start = time_us_64(), deadline = start + (uint64_t)ms * 1000u;
    iqout_start();
    while (time_us_64() < deadline) {
        if (iqout_take_txstall()) ++txstalls;
        if (txstalls) errors |= ERR_TXSTALL;
        if (ring.underruns) errors |= ERR_UNDERRUN;
        if (ring.own_errors) errors |= ERR_OWNERSHIP;
        if (errors) break;
        if (!captured && time_us_64() - start >= (uint64_t)ms * 500u) {
            cap_sequence = sequence; cap_done = ring.done;
            iqout_capture_start(capture, CAP_WORDS);
            captured = true;
        }
        if (sequence - ring.done >= OUT_BLOCKS) { tight_loop_contents(); continue; }
        const uint32_t begin = cycles();
        shape_block(output + (sequence % OUT_BLOCKS) * OUT_WORDS, sequence, run, &tables);
        const uint32_t dt = cycles() - begin;
        shape_cycles += dt;
        ++shape_calls;
        if (dt > worst_shape) worst_shape = dt;
        // A deadline miss makes iqout substitute idle, never publish an unfinished
        // buffer. Reject it even if the kernel finished before our next poll.
        if (ring.underruns || ring.own_errors || ring.next_arm > sequence) {
            work_cycles += (uint32_t)(cycles() - begin);
            errors |= ring.own_errors ? ERR_OWNERSHIP : ERR_UNDERRUN;
            break;
        }
        __dmb();
        ring.ready[sequence % OUT_BLOCKS] = sequence + 1u;
        ++sequence; ++blocks;
        work_cycles += (uint32_t)(cycles() - begin);
    }
    const uint64_t elapsed_us = time_us_64() - start;
    const bool cap_ok = captured && !iqout_capture_busy();
    iqout_stop();
    if (iqout_take_txstall()) ++txstalls;
    if (txstalls) errors |= ERR_TXSTALL;
    if (ring.underruns) errors |= ERR_UNDERRUN;
    if (ring.own_errors) errors |= ERR_OWNERSHIP;
    if (!cap_ok && !errors) errors |= ERR_CAPTURE;
    const uint32_t cap_crc = cap_ok ? crc32_update(0, capture, 4 * CAP_WORDS) : 0;
    const uint32_t input_after_crc = crc32_update(0, in_buf, sizeof in_buf);
    const double budget = (double)elapsed_us * hz / 1e6;
    printf("@{\"cmd\":\"bench32out\",\"git\":\"%s\",\"clk_hz\":%lu,\"ms\":%d,"
           "\"kernel\":%d,\"kernel_name\":\"%s\",\"code_placement\":\"flash_cached\","
           "\"cpw\":%d,\"L\":8,\"sps\":2,\"rate_index\":2,\"gamma1_100\":264,\"gamma2_100\":464,\"outer_radius\":1,"
           "\"row_profile\":\"%s\",\"pulse_profile\":\"%s\",\"coefficient_sha256\":\"%s\","
           "\"quantizer\":\"native_3_3_3\",\"table_bytes\":117912,\"reverse_bytes\":0,"
           "\"old_crc\":%lu,\"middle_crc\":%lu,\"future_crc\":%lu,"
           "\"seed\":%lu,\"in_words\":%u,\"input_crc\":%lu,\"input_after_crc\":%lu,\"coordinate_words\":%u,\"coordinate_crc\":%lu,\"input_bytes_per_symbol\":4,\"input_format\":\"rolling_radix17_triples\",\"setup_encode_cycles\":%lu,\"setup_encode_cyc_per_sym\":%.4f,\"runtime_encode_included\":false,"
           "\"history\":\"cyclic_source_tail\",\"sym_rate\":%.1f,\"core1_idle\":true,"
           "\"idle_wait\":\"spin\",\"interrupts_enabled\":true,\"elapsed_us\":%llu,"
           "\"errors\":%lu,\"underruns\":%lu,\"own_errors\":%lu,\"idle_sent\":%lu,\"txstalls\":%lu,"
           "\"blocks\":%lu,\"shape_calls\":%lu,\"prefill_blocks\":%u,\"done_blocks\":%lu,\"generated_blocks\":%lu,"
           "\"block_symbols\":%u,\"out_blocks\":%u,\"out_words\":%u,\"shape_cycles\":%llu,"
           "\"gen_cyc_per_sym\":%.4f,\"gen_busy\":%.6f,\"worst_block_cyc_per_sym\":%.4f,"
           "\"producer_work_cycles\":%llu,\"producer_work_busy\":%.6f,"
           "\"cap_words\":%u,\"cap_crc\":%lu,\"cap_stride\":1,\"cap_symbol_cursor\":%llu,"
           "\"cap_search_symbols\":%u,\"cap_done_blocks\":%lu}\n",
           GIT_REV, (unsigned long)hz, ms, kernel, kernels[kernel].name, cpw,
           IQ32NATIVE8_PROFILE, IQ32NATIVE8_PULSE_PROFILE, IQ32NATIVE8_COEFFICIENT_SHA256,
           (unsigned long)old_crc, (unsigned long)middle_crc, (unsigned long)future_crc,
           (unsigned long)SEED, IN_WORDS, (unsigned long)input_crc, (unsigned long)input_after_crc,
           RAW_WORDS, (unsigned long)coordinate_crc, (unsigned long)encode_cycles,
           (double)encode_cycles / IN_WORDS,
           hz / (4.0 * cpw), (unsigned long long)elapsed_us,
           (unsigned long)errors, (unsigned long)ring.underruns, (unsigned long)ring.own_errors,
           (unsigned long)ring.idle_sent, (unsigned long)txstalls, (unsigned long)blocks,
           (unsigned long)shape_calls, OUT_BLOCKS, (unsigned long)ring.done, (unsigned long)sequence,
           BLOCK_SYMBOLS, OUT_BLOCKS, OUT_WORDS, (unsigned long long)shape_cycles,
           shape_calls ? (double)shape_cycles / (shape_calls * BLOCK_SYMBOLS) : 0, budget ? shape_cycles / budget : 0,
           (double)worst_shape / BLOCK_SYMBOLS, (unsigned long long)work_cycles,
           budget ? work_cycles / budget : 0, cap_ok ? CAP_WORDS : 0, (unsigned long)cap_crc,
           (unsigned long long)cap_sequence * BLOCK_SYMBOLS, OUT_BLOCKS * BLOCK_SYMBOLS + 32,
           (unsigned long)cap_done);
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
