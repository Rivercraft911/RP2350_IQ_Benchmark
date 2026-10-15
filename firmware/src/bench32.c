#include "bench.h"
#include "iq32native8.h"
#include "iq32keys.h"
#include "iq32pairkey.h"
#include "tx.h"

#include <stdio.h>
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/sync.h"

enum { CHUNK = 256, WORDS = 4 * CHUNK, MODES = 6 };
typedef void (*kernel_fn)(uint32_t *, const uint32_t *, uint32_t,
                          const uint32_t *, const iq32native8_tables_t *);
_Static_assert(3 * IQ32NATIVE8_TABLE_WORDS <= N_BLOCKS * BLOCK_WORDS, "32APSK table arena");
_Static_assert(WORDS <= CAP_WORDS_MAX, "32APSK output arena");
_Static_assert(2 * CHUNK <= TABLE_WORDS, "32APSK converted input arena");

static const uint32_t *prepare(unsigned mode, unsigned offset, uint32_t previous[5],
                                const uint32_t **history) {
    const uint32_t *raw = in_buf + offset, *prev = offset ? raw - 4 : NULL;
    *history = prev;
    if (mode < 2) return raw;
    if (mode < 4) {
        iq32keys_history(previous, prev);
        iq32keys_encode((uint32_t *)cfg.ti, raw, CHUNK, offset ? raw - 1 : NULL);
    } else {
        if (prev) iq32pairkey_convert(previous, prev, 4);
        iq32pairkey_convert((uint32_t *)cfg.ti, raw, CHUNK);
    }
    *history = mode < 4 || prev ? previous : NULL;
    return (uint32_t *)cfg.ti;
}

void bench_32native8(int reps) {
    const iq32native8_tables_t tables = {ring_buf, ring_buf + IQ32NATIVE8_TABLE_WORDS,
                                        ring_buf + 2 * IQ32NATIVE8_TABLE_WORDS};
    if (!iq32native8_build(&tables, IQ32NATIVE8_DEFAULT_RATE)) {
        printf("@{\"cmd\":\"bench32native8\",\"error\":\"32APSK table construction failed\"}\n");
        return;
    }
    uint32_t seed = SEED;
    iq32native8_fill(in_buf, IN_WORDS, &seed);
    const uint32_t ci = crc32_update(0, in_buf, sizeof in_buf);
    const uint32_t co = crc32_update(0, tables.old, IQ32NATIVE8_TABLE_WORDS * 4);
    const uint32_t cm = crc32_update(0, tables.middle, IQ32NATIVE8_TABLE_WORDS * 4);
    const uint32_t cf = crc32_update(0, tables.future, IQ32NATIVE8_TABLE_WORDS * 4);
    const kernel_fn kernels[MODES] = {iq32native8_kernel_c, iq32native8_kernel_asm,
        iq32keys_kernel_c, iq32keys_kernel_asm, iq32pairkey_kernel_c, iq32pairkey_kernel_asm};
    const char *const names[MODES] = {"native_c", "native_burst", "keys_c", "keys_burst", "pair_c", "pair_burst"};
    struct { uint32_t raw, packed, worst; uint64_t elapsed, conversion; bool correct; } measured[MODES];
    for (unsigned mode = 0; mode < MODES; ++mode) {
        uint32_t raw = 0, packed = 0, worst = 0;
        uint64_t elapsed = 0, conversion = 0;
        uint32_t previous[5];
        const uint32_t *history;
        for (unsigned b = 0; b < IN_WORDS; b += CHUNK) {
            const uint32_t *input = prepare(mode, b, previous, &history);
            kernels[mode](cap_buf, input, CHUNK, history, &tables);
            raw = crc32_update(raw, cap_buf, WORDS * 4);
            for (unsigned j = 0; j < WORDS; j += 2) {
                const uint32_t bus[2] = {
                    (cap_buf[j] & 0x3fffu) | (cap_buf[j + 1] & 0x3fffu) << 16,
                    ((cap_buf[j] >> 16) & 0x3fffu) | (cap_buf[j + 1] & 0x3fff0000u),
                };
                packed = crc32_update(packed, bus, sizeof bus);
            }
        }
        for (int r = 0; r < reps; ++r) {
            for (unsigned b = 0; b < IN_WORDS; b += CHUNK) {
                const uint32_t saved = save_and_disable_interrupts(), begin = cycles();
                const uint32_t *input = prepare(mode, b, previous, &history);
                const uint32_t start = cycles();
                if (mode >= 2) conversion += start - begin;
                kernels[mode](cap_buf, input, CHUNK, history, &tables);
                const uint32_t dt = cycles() - start;
                restore_interrupts(saved);
                elapsed += dt;
                if (dt > worst) worst = dt;
            }
        }
        // Independent geometric/FIR fixture; no DVB bit-label mapper is implied.
        const bool correct = ci == 1106835987u && co == 3255586384u && cm == 916828675u &&
            cf == 1817262751u && raw == 2048936984u && packed == 1960087962u;
        measured[mode] = (typeof(measured[0])){raw, packed, worst, elapsed, conversion, correct};
    }
    printf("@{\"cmd\":\"bench32native8\",\"git\":\"%s\",\"clk_hz\":%lu,\"reps\":%d,"
           "\"L\":8,\"sps\":2,\"gamma1_100\":264,\"gamma2_100\":464,\"outer_radius\":1,"
           "\"table_bytes\":117912,\"row_profile\":\"%s\",\"pulse_profile\":\"%s\","
           "\"coefficient_sha256\":\"%s\",\"quantizer\":\"native_3_3_3\","
           "\"input_crc\":%lu,\"old_crc\":%lu,\"middle_crc\":%lu,\"future_crc\":%lu,"
           "\"code_placement\":\"flash_cached\",\"output_layout\":\"raw_pairs\",\"kernels\":[",
           GIT_REV, (unsigned long)clock_get_hz(clk_sys), reps, IQ32NATIVE8_PROFILE,
           IQ32NATIVE8_PULSE_PROFILE, IQ32NATIVE8_COEFFICIENT_SHA256,
           (unsigned long)ci, (unsigned long)co, (unsigned long)cm, (unsigned long)cf);
    for (unsigned mode = 0; mode < MODES; ++mode) {
        printf("%s{\"name\":\"%s\",\"input_bytes_per_sym\":%u,\"raw_crc\":%lu,\"packed_crc\":%lu,\"crc_ok\":%s,"
               "\"cyc_per_sym\":%.4f,\"worst_block_cyc_per_sym\":%.4f,\"convert_cyc_per_sym\":%.4f,"
               "\"convert_plus_shape_cyc_per_sym\":%.4f}",
               mode ? "," : "", names[mode], mode >= 2 && mode < 4 ? 4u : 2u,
               (unsigned long)measured[mode].raw, (unsigned long)measured[mode].packed,
               measured[mode].correct ? "true" : "false",
               (double)measured[mode].elapsed / (reps * IN_WORDS * 2),
               (double)measured[mode].worst / (CHUNK * 2),
               (double)measured[mode].conversion / (reps * IN_WORDS * 2),
               (double)(measured[mode].elapsed + measured[mode].conversion) / (reps * IN_WORDS * 2));
    }
    printf("]}\n");
    iq_build_tables(&cfg);
    seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
}
