#include "bench.h"
#include "iq8quad.h"
#include "iqout.h"
#include "tx.h"

#include <stdio.h>
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/sync.h"
#include "pico/stdlib.h"

enum { GROUPS = IN_WORDS / 2, BLOCK_GROUPS = 64,
       OUT_WORDS = BLOCK_GROUPS * 32, OUT_BLOCKS = 4, CAP_WORDS = 4096 };
_Static_assert(2 * IQ8QUAD_TABLE_WORDS == N_BLOCKS * BLOCK_WORDS, "8PSK table arena");
_Static_assert(OUT_WORDS * OUT_BLOCKS + CAP_WORDS <= CAP_WORDS_MAX, "8PSK output/capture arena");

static uint32_t packed_crc(uint32_t crc, const uint32_t *pairs) {
    for (unsigned k = 0; k < OUT_WORDS; k += 2) {
        uint32_t packed[2] = {
            (pairs[k] & 0x3fffu) | (pairs[k + 1] & 0x3fffu) << 16,
            ((pairs[k] >> 16) & 0x3fffu) | (pairs[k + 1] & 0x3fff0000u),
        };
        crc = crc32_update(crc, packed, sizeof packed);
    }
    return crc;
}

void bench_8quad(int ms, int reps) {
    iq8_input_t *input = (iq8_input_t *)in_buf;
    const iq8quad_tables_t tables = {ring_buf, ring_buf + IQ8QUAD_TABLE_WORDS};
    uint32_t *output = cap_buf, *capture = cap_buf + OUT_WORDS * OUT_BLOCKS;
    uint32_t seed = SEED;
    iq8_fill(input, GROUPS, &seed);
    iq8quad_build(&tables);
    const uint32_t ca = crc32_update(0, tables.a, 4 * IQ8QUAD_TABLE_WORDS);
    const uint32_t cb = crc32_update(0, tables.b, 4 * IQ8QUAD_TABLE_WORDS);
    uint32_t crc_c = 0, crc_interp = 0, worst = 0;
    uint64_t kernel_cycles = 0;
    for (unsigned g = 0; g < GROUPS; g += BLOCK_GROUPS) {
        const iq8_input_t *prev = g ? input + g - 1 : NULL;
        iq8quad_kernel_c(output, input + g, BLOCK_GROUPS, prev, &tables);
        crc_c = packed_crc(crc_c, output);
        iq8quad_kernel_interp(output, input + g, BLOCK_GROUPS, prev, &tables);
        crc_interp = packed_crc(crc_interp, output);
    }
    for (int r = 0; r < reps; ++r) {
        for (unsigned g = 0; g < GROUPS; g += BLOCK_GROUPS) {
            const uint32_t saved = save_and_disable_interrupts(), t = cycles();
            iq8quad_kernel_interp(output, input + g, BLOCK_GROUPS,
                                 g ? input + g - 1 : NULL, &tables);
            const uint32_t elapsed = cycles() - t;
            restore_interrupts(saved);
            kernel_cycles += elapsed;
            if (elapsed > worst) worst = elapsed;
        }
    }
    const bool correct = ca == IQ8QUAD_TABLE_A_CRC && cb == IQ8QUAD_TABLE_B_CRC &&
                         crc_c == IQ8QUAD_FIXTURE_PACKED_CRC && crc_c == crc_interp;
    const double component_cps = (double)kernel_cycles / (reps * GROUPS * 16);
    if (!ms || !correct) printf("@{\"cmd\":\"bench8quad\",\"git\":\"%s\",\"clk_hz\":%lu,"
           "\"L\":9,\"sps\":2,\"row_symbols\":4,\"table_bytes\":131072,"
           "\"seed\":%lu,\"groups\":%u,\"reps\":%d,\"table_crc_a\":%lu,\"table_crc_b\":%lu,"
           "\"crc_c\":%lu,\"crc_interp\":%lu,\"crc_ok\":%s,\"cyc_per_sym\":%.4f,"
           "\"worst_block_cyc_per_sym\":%.4f}\n",
           GIT_REV, (unsigned long)clock_get_hz(clk_sys), (unsigned long)SEED, GROUPS, reps,
           (unsigned long)ca, (unsigned long)cb, (unsigned long)crc_c, (unsigned long)crc_interp,
           correct ? "true" : "false", component_cps, (double)worst / (BLOCK_GROUPS * 16));
    if (correct && ms) {
        iqout_init(output, OUT_WORDS, OUT_BLOCKS, 4, IQ_LAYOUT_RAW_PAIRS);
        uint32_t seq = 0, stalls = 0, generated = 0;
        for (; seq < OUT_BLOCKS; ++seq) {
            const unsigned g = seq * BLOCK_GROUPS;
            iq8quad_kernel_interp(output + seq * OUT_WORDS, input + g, BLOCK_GROUPS,
                                 input + ((g - 1) & (GROUPS - 1)), &tables);
            ring.ready[seq] = seq + 1;
        }
        kernel_cycles = 0;
        worst = 0;
        bool captured = false;
        iqout_start();
        const uint64_t start = time_us_64(), end = start + (uint64_t)ms * 1000;
        while (time_us_64() < end) {
            if (iqout_take_txstall()) ++stalls;
            if (stalls || ring.underruns || ring.own_errors) break;
            if (!captured && time_us_64() >= start + (uint64_t)ms * 500) {
                iqout_capture_start(capture, CAP_WORDS);
                captured = true;
            }
            if (seq - ring.done < OUT_BLOCKS) {
                const unsigned g = (seq * BLOCK_GROUPS) & (GROUPS - 1);
                const uint32_t t = cycles();
                iq8quad_kernel_interp(output + (seq % OUT_BLOCKS) * OUT_WORDS,
                                     input + g, BLOCK_GROUPS,
                                     input + ((g - 1) & (GROUPS - 1)), &tables);
                const uint32_t elapsed = cycles() - t;
                kernel_cycles += elapsed;
                if (elapsed > worst) worst = elapsed;
                __dmb();
                ring.ready[seq % OUT_BLOCKS] = seq + 1;
                ++seq;
                ++generated;
            } else {
                __wfe();
            }
        }
        const uint64_t elapsed_us = time_us_64() - start;
        const bool cap_ok = captured && !iqout_capture_busy();
        iqout_stop();
        if (iqout_take_txstall()) ++stalls;
        const uint32_t cap_crc = cap_ok ? crc32_update(0, capture, 4 * CAP_WORDS) : 0;
        printf("@{\"cmd\":\"stream8quad\",\"git\":\"%s\",\"clk_hz\":%lu,"
               "\"L\":9,\"sps\":2,\"row_symbols\":4,\"cpw\":4,\"ms\":%d,"
               "\"sym_rate\":%.1f,\"elapsed_us\":%llu,\"seed\":%lu,\"groups\":%u,"
               "\"periodic\":true,\"crc_ok\":true,\"component_cyc_per_sym\":%.4f,"
               "\"blocks\":%lu,\"gen_cyc_per_sym\":%.4f,\"gen_busy\":%.4f,"
               "\"worst_block_cyc_per_sym\":%.4f,\"underruns\":%lu,\"own_errors\":%lu,"
               "\"txstalls\":%lu,\"cap_words\":%u,\"cap_crc\":%lu,\"cap_stride\":1}\n",
               GIT_REV, (unsigned long)clock_get_hz(clk_sys), ms,
               clock_get_hz(clk_sys) / 16.0, (unsigned long long)elapsed_us,
               (unsigned long)SEED, GROUPS, component_cps, (unsigned long)generated,
               generated ? (double)kernel_cycles / (generated * BLOCK_GROUPS * 16) : 0,
               (double)kernel_cycles / (elapsed_us * clock_get_hz(clk_sys) / 1e6),
               (double)worst / (BLOCK_GROUPS * 16), (unsigned long)ring.underruns,
               (unsigned long)ring.own_errors, (unsigned long)stalls, cap_ok ? CAP_WORDS : 0,
               (unsigned long)cap_crc);
        if (cap_ok) {
            for (unsigned i = 0; i < CAP_WORDS; i += 16) {
                printf("@cap %u", i);
                for (unsigned j = 0; j < 16; ++j) printf(" %08lx", (unsigned long)capture[i + j]);
                putchar('\n');
            }
            printf("@capend\n");
        }
    }
    seed = SEED;
    prbs_fill(in_buf, IN_WORDS, &seed);
}
