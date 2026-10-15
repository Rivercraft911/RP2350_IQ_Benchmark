#include "bench32map.h"

#include <stdio.h>
#include <string.h>
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/sync.h"
#include "iq32keys.h"
#include "iq32mapkeys.h"
#include "iq32mapkeys_pair.h"
#include "tx.h"

enum { SYMBOLS = 8192, COORDINATE_WORDS = SYMBOLS / 2, MODES = 6 };
_Static_assert(SYMBOLS + 7 <= 4 * TABLE_WORDS, "label arena");
_Static_assert(COORDINATE_WORDS <= IN_WORDS, "coordinate arena");
_Static_assert(SYMBOLS <= CAP_WORDS_MAX, "key arena");
_Static_assert(IQ32MAPKEYS_PAIR_WORDS <= TABLE_WORDS, "pair table arena");

#define C(i,q) ((uint32_t)(i) | (uint32_t)(q)<<16)
static const uint32_t coordinates[32] = {
    C(1,1), C(9,1), C(9,9), C(1,9), C(4,2), C(3,3), C(2,4), C(10,4),
    C(11,3), C(12,2), C(12,10), C(11,11), C(10,12), C(2,12), C(3,11), C(4,10),
    C(8,0), C(7,5), C(6,6), C(5,7), C(0,8), C(13,7), C(14,6), C(15,5),
    C(16,0), C(15,13), C(14,14), C(13,15), C(0,16), C(5,15), C(6,14), C(7,13),
};
#undef C

// Keep the coordinate-only and combined baseline on the identical code path.
static __attribute__((noinline)) void map_coordinates(const uint8_t *labels) {
    for (unsigned word = 0; word < COORDINATE_WORDS; ++word)
        in_buf[word] = coordinates[labels[2*word]] | coordinates[labels[2*word+1]]<<5;
}

static void emit(unsigned mode, const uint8_t *labels) {
    if (mode < 3) {
        if (mode < 2) map_coordinates(labels);
        if (mode != 1)
            iq32keys_encode(cap_buf, in_buf, COORDINATE_WORDS,
                             in_buf + COORDINATE_WORDS - 1);
        return;
    }
    iq32mapkeys_state_t state;
    iq32mapkeys_init(&state, labels - 2);
    if (mode == 3) iq32mapkeys_encode(cap_buf, labels, SYMBOLS, &state);
    else if (mode == 4) iq32mapkeys_pair_encode(cap_buf, labels, SYMBOLS, &state, cfg.tq);
    else iq32mapkeys_pair_canonical(cap_buf, labels, SYMBOLS, &state, cfg.tq);
}

static void restore_buffers(void) {
    iq_build_tables(&cfg);
    uint32_t seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
}

void bench_32map(int reps) {
    if (reps < 1 || reps > 1000) {
        printf("@{\"cmd\":\"bench32map\",\"error\":\"needs 1..1000 reps\"}\n");
        return;
    }
    uint8_t *const labels = (uint8_t *)cfg.ti + 7;
    uint32_t seed = SEED;
    for (unsigned symbol = 0; symbol < SYMBOLS; ++symbol) {
        seed ^= seed << 13; seed ^= seed >> 17; seed ^= seed << 5;
        labels[symbol] = seed & 31u;
    }
    memcpy(labels - 7, labels + SYMBOLS - 7, 7);
    iq32mapkeys_pair_build(cfg.tq);
    map_coordinates(labels);
    const uint32_t label_crc = crc32_update(0, labels, SYMBOLS);
    const uint32_t coordinate_crc = crc32_update(0, in_buf, 4 * COORDINATE_WORDS);
    const uint32_t pair_crc = crc32_update(0, cfg.tq, 4 * IQ32MAPKEYS_PAIR_WORDS);
    bool baseline_table_ok = true;
    for (unsigned point = 0; point < 32; ++point)
        baseline_table_ok &= coordinates[point] == iq32mapkeys_coordinate(point);
    const bool fixture_ok = label_crc == 1233905124u && coordinate_crc == 2979756864u &&
        pair_crc == 2680011457u && baseline_table_ok;
    if (!fixture_ok) {
        restore_buffers();
        printf("@{\"cmd\":\"bench32map\",\"error\":\"fixture CRC mismatch\"}\n");
        return;
    }
    struct { uint64_t cycles; uint32_t worst, crc; bool correct; } results[MODES] = {0};
    for (unsigned mode = 0; mode < MODES; ++mode) {
        emit(mode, labels); // Warm caches and establish output before timing.
        const uint32_t crc = mode == 1 ? crc32_update(0, in_buf, 4 * COORDINATE_WORDS) :
                                         crc32_update(0, cap_buf, 4 * SYMBOLS);
        results[mode].crc = crc;
        results[mode].correct = crc == (mode == 1 ? 2979756864u : 2091885109u);
        for (int rep = 0; rep < reps; ++rep) {
            const uint32_t saved = save_and_disable_interrupts(), begin = cycles();
            emit(mode, labels);
            const uint32_t elapsed = cycles() - begin;
            restore_interrupts(saved);
            results[mode].cycles += elapsed;
            if (elapsed > results[mode].worst) results[mode].worst = elapsed;
        }
        const uint32_t after = mode == 1 ? crc32_update(0, in_buf, 4 * COORDINATE_WORDS) :
                                           crc32_update(0, cap_buf, 4 * SYMBOLS);
        results[mode].correct &= after == crc;
    }
    const uint32_t label_after = crc32_update(0, labels, SYMBOLS);
    const uint32_t coordinate_after = crc32_update(0, in_buf, 4 * COORDINATE_WORDS);
    const uint32_t pair_after = crc32_update(0, cfg.tq, 4 * IQ32MAPKEYS_PAIR_WORDS);
    const bool immutable = label_crc == label_after && coordinate_crc == coordinate_after &&
                           pair_crc == pair_after;
    printf("@{\"cmd\":\"bench32map\",\"git\":\"%s\",\"clk_hz\":%lu,\"reps\":%d,"
           "\"symbols_per_emission\":%u,\"history\":\"cyclic_source_tail\","
           "\"scope\":\"geometric_labels_to_keys_only\",\"core1_idle\":true,"
           "\"label_alphabet\":\"canonical_points_0_to_31\",\"output_and_fec_included\":false,"
           "\"interrupts_masked_per_emission\":true,\"history_init_included\":true,"
           "\"prbs_in_timed_region\":false,\"label_bytes\":%u,\"coordinate_bytes\":%u,"
           "\"key_bytes\":%u,\"label_crc\":%lu,\"label_after_crc\":%lu,"
           "\"coordinate_crc\":%lu,\"coordinate_after_crc\":%lu,"
           "\"pair_table_crc\":%lu,\"pair_table_after_crc\":%lu,"
           "\"fixture_ok\":%s,\"input_and_table_unchanged\":%s,\"kernels\":[",
           GIT_REV, (unsigned long)clock_get_hz(clk_sys), reps, SYMBOLS, SYMBOLS,
           4 * COORDINATE_WORDS, 4 * SYMBOLS, (unsigned long)label_crc,
           (unsigned long)label_after, (unsigned long)coordinate_crc,
           (unsigned long)coordinate_after, (unsigned long)pair_crc,
           (unsigned long)pair_after, fixture_ok ? "true" : "false", immutable ? "true" : "false");
    const char *const names[MODES] = {"coordinates_plus_keys", "coordinates_only", "keys_only",
                                     "weighted_fused", "pair_fused", "canonical_pair_fused"};
    const unsigned flash_bytes[MODES] = {128, 128, 0, 1584, 1716, 1584};
    const unsigned hot_flash_bytes[MODES] = {128, 128, 0, 1584, 132, 0};
    for (unsigned mode = 0; mode < MODES; ++mode) {
        printf("%s{\"name\":\"%s\",\"crc\":%lu,\"crc_ok\":%s,"
               "\"cycles\":%llu,\"cyc_per_sym\":%.4f,\"worst_emission_cyc_per_sym\":%.4f,"
               "\"lookup_flash_bytes\":%u,\"hot_lookup_flash_bytes\":%u,\"lookup_ram_bytes\":%u}",
               mode ? "," : "", names[mode], (unsigned long)results[mode].crc,
               results[mode].correct && immutable ? "true" : "false",
               (unsigned long long)results[mode].cycles,
               (double)results[mode].cycles / ((uint64_t)reps * SYMBOLS),
               (double)results[mode].worst / SYMBOLS, flash_bytes[mode], hot_flash_bytes[mode],
               mode >= 4 ? 4 * IQ32MAPKEYS_PAIR_WORDS : 0u);
    }
    printf("]}\n");
    restore_buffers();
}
