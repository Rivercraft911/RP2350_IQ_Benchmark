#include "bench.h"
#include "iq16chunk.h"
#include "iq16native8.h"
#include "tx.h"

#include <stdio.h>
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/sync.h"

enum { BLOCKS = IN_WORDS, CHUNK = 256, WORDS = CHUNK * 8, MODES = 3 };
typedef void (*kernel_fn)(uint32_t *, const uint32_t *, uint32_t,
                          const uint32_t *, const iq16chunk_tables_t *);
static const struct { const char *name; kernel_fn run; bool reverse_map; } kernels[MODES] = {
    {"reverse_bits", iq16chunk_kernel_c, false},
    {"reverse_map", iq16chunk_kernel_c, true},
    {"burst_map", iq16chunk_kernel_asm, true},
};
_Static_assert(2 * IQ16CHUNK_TABLE_WORDS == N_BLOCKS * BLOCK_WORDS, "16APSK table arena");
_Static_assert(WORDS <= CAP_WORDS_MAX, "16APSK output arena");
_Static_assert(2 * IQ16CHUNK_ROWS <= 4 * TABLE_WORDS, "key reversal arena");

static uint32_t masked_crc(uint32_t crc, const uint32_t *pairs) {
    for (unsigned k = 0; k < WORDS; k += 2) {
        const uint32_t packed[2] = {
            (pairs[k] & 0x3fffu) | (pairs[k + 1] & 0x3fffu) << 16,
            ((pairs[k] >> 16) & 0x3fffu) | (pairs[k + 1] & 0x3fff0000u),
        };
        crc = crc32_update(crc, packed, sizeof packed);
    }
    return crc;
}

void bench_16chunk(int reps) {
    iq16chunk_tables_t tables = {ring_buf, ring_buf + IQ16CHUNK_TABLE_WORDS, NULL};
    uint16_t *reverse = (uint16_t *)cfg.ti;
    if (!iq16chunk_build(&tables, IQ16CHUNK_DEFAULT_GAMMA100)) {
        printf("@{\"error\":\"16APSK table construction failed\"}\n");
        return;
    }
    iq16chunk_build_reverse(reverse);
    uint32_t seed = SEED;
    iq16chunk_fill(in_buf, BLOCKS, &seed);
    const uint32_t ci = crc32_update(0, in_buf, BLOCKS * 4);
    const uint32_t ce = crc32_update(0, tables.edge, IQ16CHUNK_TABLE_WORDS * 4);
    const uint32_t cm = crc32_update(0, tables.middle, IQ16CHUNK_TABLE_WORDS * 4);
    struct measurement { uint32_t raw, crc, worst; uint64_t elapsed; bool correct; } measured[MODES];
    for (unsigned mode = 0; mode < MODES; ++mode) {
        tables.reverse = kernels[mode].reverse_map ? reverse : NULL;
        uint32_t crc = 0, raw = 0, worst = 0;
        uint64_t elapsed = 0;
        for (unsigned b = 0; b < BLOCKS; b += CHUNK) {
            kernels[mode].run(cap_buf, in_buf + b, CHUNK, b ? in_buf + b - 2 : NULL, &tables);
            crc = masked_crc(crc, cap_buf);
            raw = crc32_update(raw, cap_buf, WORDS * 4);
        }
        for (int r = 0; r < reps; ++r) {
            for (unsigned b = 0; b < BLOCKS; b += CHUNK) {
                const uint32_t saved = save_and_disable_interrupts(), start = cycles();
                kernels[mode].run(cap_buf, in_buf + b, CHUNK, b ? in_buf + b - 2 : NULL, &tables);
                const uint32_t dt = cycles() - start;
                restore_interrupts(saved);
                elapsed += dt;
                if (dt > worst) worst = dt;
            }
        }
        const bool correct = ci == IQ16CHUNK_FIXTURE_INPUT_CRC && ce == IQ16CHUNK_EDGE_CRC &&
            cm == IQ16CHUNK_MIDDLE_CRC && raw == IQ16CHUNK_FIXTURE_RAW_PAIRS_CRC &&
            crc == IQ16CHUNK_FIXTURE_PACKED_CRC;
        measured[mode] = (struct measurement){raw, crc, worst, elapsed, correct};
    }
    printf("@{\"cmd\":\"bench16chunk\",\"git\":\"%s\",\"clk_hz\":%lu,\"reps\":%d,"
           "\"L\":9,\"sps\":2,\"gamma100\":270,\"table_bytes\":131072,"
           "\"input_crc\":%lu,\"edge_crc\":%lu,\"middle_crc\":%lu,"
           "\"code_placement\":\"flash_cached\",\"kernels\":[",
           GIT_REV, (unsigned long)clock_get_hz(clk_sys), reps,
           (unsigned long)ci, (unsigned long)ce, (unsigned long)cm);
    for (unsigned mode = 0; mode < MODES; ++mode) {
        const struct measurement *r = &measured[mode];
        printf("%s{\"name\":\"%s\",\"raw_crc\":%lu,\"packed_crc\":%lu,\"crc_ok\":%s,"
               "\"reverse_bytes\":%u,\"cyc_per_sym\":%.4f,\"worst_block_cyc_per_sym\":%.4f}",
               mode ? "," : "", kernels[mode].name,
               (unsigned long)r->raw, (unsigned long)r->crc, r->correct ? "true" : "false",
               kernels[mode].reverse_map ? IQ16CHUNK_ROWS * 2 : 0, (double)r->elapsed / (reps * BLOCKS * 4),
               (double)r->worst / (CHUNK * 4));
    }
    printf("]}\n");
    iq_build_tables(&cfg);
    seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
}

// Separate opt-in profile; preserve the L9 benchmark and its exact CRCs above.
void bench_16native8(int reps) {
    _Static_assert(IQ16NATIVE8_OLD_WORDS <= TABLE_WORDS, "native L8 history arena");
    _Static_assert(2 * IQ16NATIVE8_FULL_WORDS == N_BLOCKS * BLOCK_WORDS, "native L8 table arena");
    iq16native8_tables_t tables = {(uint32_t *)cfg.ti, ring_buf,
                                   ring_buf + IQ16NATIVE8_FULL_WORDS};
    if (!iq16native8_build(&tables, IQ16NATIVE8_DEFAULT_GAMMA100)) {
        printf("@{\"error\":\"16APSK native L8 table construction failed\"}\n");
        return;
    }
    uint32_t seed = SEED;
    iq16chunk_fill(in_buf, BLOCKS, &seed);
    const uint32_t ci = crc32_update(0, in_buf, BLOCKS * 4);
    const uint32_t co = crc32_update(0, tables.old, IQ16NATIVE8_OLD_WORDS * 4);
    const uint32_t cm = crc32_update(0, tables.middle, IQ16NATIVE8_FULL_WORDS * 4);
    const uint32_t cf = crc32_update(0, tables.future, IQ16NATIVE8_FULL_WORDS * 4);
    typedef void (*native_fn)(uint32_t *, const uint32_t *, uint32_t,
                              const uint32_t *, const iq16native8_tables_t *);
    const native_fn native[2] = {iq16native8_kernel_c, iq16native8_kernel_asm};
    struct { uint32_t raw, packed, worst; uint64_t elapsed; bool correct; } measured[2];
    for (unsigned mode = 0; mode < 2; ++mode) {
        uint32_t raw = 0, packed = 0, worst = 0;
        uint64_t elapsed = 0;
        for (unsigned b = 0; b < BLOCKS; b += CHUNK) {
            native[mode](cap_buf, in_buf + b, CHUNK, b ? in_buf + b - 2 : NULL, &tables);
            packed = masked_crc(packed, cap_buf);
            raw = crc32_update(raw, cap_buf, WORDS * 4);
        }
        for (int r = 0; r < reps; ++r) {
            for (unsigned b = 0; b < BLOCKS; b += CHUNK) {
                const uint32_t saved = save_and_disable_interrupts(), start = cycles();
                native[mode](cap_buf, in_buf + b, CHUNK, b ? in_buf + b - 2 : NULL, &tables);
                const uint32_t dt = cycles() - start;
                restore_interrupts(saved);
                elapsed += dt;
                if (dt > worst) worst = dt;
            }
        }
        const bool correct = ci == IQ16CHUNK_FIXTURE_INPUT_CRC && co == IQ16NATIVE8_OLD_CRC &&
            cm == IQ16NATIVE8_MIDDLE_CRC && cf == IQ16NATIVE8_FUTURE_CRC &&
            raw == IQ16NATIVE8_FIXTURE_RAW_PAIRS_CRC && packed == IQ16NATIVE8_FIXTURE_PACKED_CRC;
        measured[mode] = (typeof(measured[0])){raw, packed, worst, elapsed, correct};
    }
    printf("@{\"cmd\":\"bench16native8\",\"git\":\"%s\",\"clk_hz\":%lu,\"reps\":%d,"
           "\"L\":8,\"sps\":2,\"gamma100\":270,\"table_bytes\":139264,\"reverse_bytes\":0,"
           "\"row_profile\":\"%s\",\"pulse_profile\":\"%s\",\"coefficient_sha256\":\"%s\","
           "\"quantizer\":\"native_3_4_4\",\"input_crc\":%lu,\"old_crc\":%lu,"
           "\"middle_crc\":%lu,\"future_crc\":%lu,\"code_placement\":\"flash_cached\",\"kernels\":[",
           GIT_REV, (unsigned long)clock_get_hz(clk_sys), reps, IQ16NATIVE8_PROFILE,
           IQ16NATIVE8_PULSE_PROFILE, IQ16NATIVE8_COEFFICIENT_SHA256,
           (unsigned long)ci, (unsigned long)co, (unsigned long)cm, (unsigned long)cf);
    for (unsigned mode = 0; mode < 2; ++mode) {
        printf("%s{\"name\":\"%s\",\"raw_crc\":%lu,\"packed_crc\":%lu,\"crc_ok\":%s,"
               "\"cyc_per_sym\":%.4f,\"worst_block_cyc_per_sym\":%.4f}",
               mode ? "," : "", mode ? "native_burst" : "native_c",
               (unsigned long)measured[mode].raw, (unsigned long)measured[mode].packed,
               measured[mode].correct ? "true" : "false",
               (double)measured[mode].elapsed / (reps * BLOCKS * 4),
               (double)measured[mode].worst / (CHUNK * 4));
    }
    printf("]}\n");
    iq_build_tables(&cfg);
    seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
}
