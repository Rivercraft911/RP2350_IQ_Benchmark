#include "bench16planar.h"

#include <stdio.h>
#include <string.h>

#include "dual_dma.h"
#include "git_rev.h"
#include "hardware/clocks.h"
#include "hardware/sync.h"
#include "iq16chunk.h"
#include "iq16fast_planar.h"
#include "link.h"
#include "pico/stdlib.h"
#include "tx.h"

enum {
    BLOCK_INPUT = 256, BLOCK_SYMBOLS = 4 * BLOCK_INPUT, OUT_BLOCKS = 4,
    ROWS = OUT_BLOCKS * BLOCK_INPUT, ROW_LOG2 = 10,
    OUT_WORDS = 8 * ROWS, CAP_WORDS = 4096, REUSE_GUARD = 8,
    ERR_UNDERRUN = 1u << 16, ERR_OWNERSHIP = 1u << 17,
    ERR_CAPTURE = 1u << 18, ERR_INPUT = 1u << 19, ERR_RESTORE = 1u << 20,
};
_Static_assert(IN_WORDS % BLOCK_INPUT == 0 && !(IN_WORDS & (IN_WORDS - 1)), "cyclic input blocks");
_Static_assert(2 * ROWS <= LINK_RING_WORDS, "two aligned descriptor rings");
_Static_assert(OUT_WORDS + CAP_WORDS <= CAP_WORDS_MAX, "planar output/capture arena");
_Static_assert(IQ16NATIVE8_OLD_WORDS <= TABLE_WORDS, "old-coordinate table arena");
_Static_assert(2 * IQ16NATIVE8_FULL_WORDS == N_BLOCKS * BLOCK_WORDS, "full-coordinate table arena");

static void shape_block(uint32_t *output, uint32_t sequence, const iq16native8_tables_t *tables) {
    const unsigned first = (sequence * BLOCK_INPUT) & (IN_WORDS - 1u);
    iq16fast_planar_asm(output, in_buf + first, BLOCK_INPUT,
                        in_buf + ((first - 2u) & (IN_WORDS - 1u)), tables);
}

static void descriptor_rows(uint32_t *di, uint32_t *dq, uint32_t *output) {
    for (unsigned row = 0; row < ROWS; ++row) {
        di[row] = (uintptr_t)(output + 8 * row);
        dq[row] = (uintptr_t)(output + 8 * row + 4);
    }
}

void bench_16planar(int cpw, int ms) {
    const uint32_t hz = clock_get_hz(clk_sys);
    if ((cpw != 4 && cpw != 6 && cpw != 8) || ms < 100 || ms > 60000 ||
        (hz != 128000000u && hz != 150000000u)) {
        printf("@{\"cmd\":\"bench16planar\",\"error\":\"needs cpw4/6/8,100..60000ms,128/150MHz and idle transmitter\"}\n");
        return;
    }
    const unsigned saved_rows = dual_dma_get_row_words(), saved_writers = dual_dma_get_writers();
    const unsigned saved_schedule = dual_dma_get_schedule(), saved_priority = dual_dma_get_bus_priority();
    const iq16native8_tables_t tables = {(uint32_t *)cfg.ti, ring_buf,
                                       ring_buf + IQ16NATIVE8_FULL_WORDS};
    uint32_t *const di = link_ring, *const dq = link_ring + ROWS;
    uint32_t *const output = cap_buf, *const capture = cap_buf + OUT_WORDS;
    uint32_t ready[OUT_BLOCKS], seed = SEED, errors = 0, blocks = 0, worst_shape = 0;
    uint32_t next = OUT_BLOCKS, max_poll_gap = 0, cap_crc = 0, cap_words = 0;
    uint64_t shape_cycles = 0, work_cycles = 0, elapsed = 0, cap_cursor = 0, cap_lower = 0;
    uint64_t min_lead = ROWS, max_axis_gap = 0;
    bool started = false, captured = false, restored = false, result_printed = false;
    dual_dma_progress_t p = {0};
    if (((uintptr_t)di & (4 * ROWS - 1u)) || ((uintptr_t)dq & (4 * ROWS - 1u)) ||
        !dual_dma_set_writers(2) || !dual_dma_set_row_words(4) ||
        !dual_dma_set_schedule(0) || !dual_dma_set_bus_priority(0)) {
        printf("@{\"cmd\":\"bench16planar\",\"error\":\"descriptor alignment or idle output selectors unavailable\"}\n");
        goto cleanup;
    }
    if (!iq16native8_build(&tables, IQ16NATIVE8_DEFAULT_GAMMA100)) {
        printf("@{\"cmd\":\"bench16planar\",\"error\":\"16APSK table construction failed\"}\n");
        goto cleanup;
    }
    const uint32_t old_crc = crc32_update(0, tables.old, 4 * IQ16NATIVE8_OLD_WORDS);
    const uint32_t middle_crc = crc32_update(0, tables.middle, 4 * IQ16NATIVE8_FULL_WORDS);
    const uint32_t future_crc = crc32_update(0, tables.future, 4 * IQ16NATIVE8_FULL_WORDS);
    if (old_crc != IQ16NATIVE8_OLD_CRC || middle_crc != IQ16NATIVE8_MIDDLE_CRC ||
        future_crc != IQ16NATIVE8_FUTURE_CRC) {
        printf("@{\"cmd\":\"bench16planar\",\"error\":\"16APSK table CRC mismatch\"}\n");
        goto cleanup;
    }
    iq16chunk_fill(in_buf, IN_WORDS, &seed);
    const uint32_t input_crc = crc32_update(0, in_buf, sizeof in_buf);
    descriptor_rows(di, dq, output);
    for (unsigned b = 0; b < OUT_BLOCKS; ++b) {
        shape_block(output + b * BLOCK_INPUT * 8, b, &tables);
        ready[b] = b + 1;
    }
    __dmb();
    if (!dual_dma_start_div256(di, dq, ROW_LOG2, (unsigned)cpw * 256)) {
        printf("@{\"cmd\":\"bench16planar\",\"error\":\"%s\"}\n", dual_dma_last_error());
        goto cleanup;
    }
    started = true;
    const uint64_t start = time_us_64(), deadline = start + (uint64_t)ms * 1000u;
    uint64_t last_poll = start;
    while (time_us_64() < deadline) {
        const uint32_t begin = cycles();
        const uint64_t now = time_us_64();
        const uint32_t gap = (uint32_t)(now - last_poll);
        if (gap > max_poll_gap) max_poll_gap = gap;
        last_poll = now;
        p = dual_dma_progress(); errors |= p.errors;
        const uint64_t low = p.read_i < p.read_q ? p.read_i : p.read_q;
        const uint64_t high = p.issued_i > p.issued_q ? p.issued_i : p.issued_q;
        const uint64_t axis_gap = high - low;
        if (axis_gap > max_axis_gap) max_axis_gap = axis_gap;
        const uint64_t published = (uint64_t)next * BLOCK_INPUT;
        if (high + REUSE_GUARD >= published) errors |= ERR_UNDERRUN;
        const uint64_t lead = high < published ? published - high : 0;
        if (lead < min_lead) min_lead = lead;
        if (errors || !p.running) break;
        if (!captured && now - start >= (uint64_t)ms * 500u) {
            // A conservative32-row lookbehind covers FIFO/OSR sample prefetch.
            // A second snapshot after arming bounds the first sampled bus value
            // despite CPU setup latency between the original snapshot and capture.
            cap_lower = low > 32 ? (low - 32) * 4 : 0;
            captured = dual_dma_capture(capture, CAP_WORDS);
            if (!captured) errors |= ERR_CAPTURE;
            const dual_dma_progress_t after = dual_dma_progress(); errors |= after.errors;
            cap_cursor = (after.issued_i > after.issued_q ? after.issued_i : after.issued_q) * 4;
        }
        const uint64_t replace_end = (uint64_t)(next - OUT_BLOCKS + 1u) * BLOCK_INPUT;
        if (low >= replace_end + REUSE_GUARD) {
            const unsigned slot = next & (OUT_BLOCKS - 1);
            if (ready[slot] != next - OUT_BLOCKS + 1u) { errors |= ERR_OWNERSHIP; break; }
            const uint32_t stamp = cycles();
            shape_block(output + slot * BLOCK_INPUT * 8, next, &tables);
            const uint32_t dt = cycles() - stamp;
            shape_cycles += dt; if (dt > worst_shape) worst_shape = dt;
            p = dual_dma_progress(); errors |= p.errors;
            const uint64_t after = p.issued_i > p.issued_q ? p.issued_i : p.issued_q;
            if (after + REUSE_GUARD >= published) { errors |= ERR_OWNERSHIP; break; }
            __dmb(); ready[slot] = next + 1; ++next; ++blocks;
        }
        work_cycles += (uint32_t)(cycles() - begin);
        if (errors) break;
        // Output pointers remain immutable. No sample copy, transpose or per-row
        // descriptor generation occurs while the stream runs.
        busy_wait_us_32(8);
    }
    elapsed = time_us_64() - start;
    p = dual_dma_progress(); errors |= p.errors;
    dual_dma_stop(); started = false;
    p = dual_dma_progress(); errors |= p.errors;
    cap_words = captured ? dual_dma_capture_count() : 0;
    errors |= dual_dma_capture_errors();
    if (cap_words != CAP_WORDS) errors |= ERR_CAPTURE;
    if (cap_words) cap_crc = crc32_update(0, capture, 4 * cap_words);
    const uint32_t input_after_crc = crc32_update(0, in_buf, sizeof in_buf);
    if (input_after_crc != input_crc) errors |= ERR_INPUT;
    restored = dual_dma_set_writers(2) && dual_dma_set_row_words(saved_rows) &&
        dual_dma_set_writers(saved_writers) && dual_dma_set_schedule(saved_schedule) &&
        dual_dma_set_bus_priority(saved_priority);
    if (!restored) errors |= ERR_RESTORE;
    const double budget = (double)elapsed * hz / 1e6;
    printf("@{\"cmd\":\"bench16planar\",\"git\":\"%s\",\"clk_hz\":%lu,\"ms\":%d,\"cpw\":%d,"
           "\"kernel_name\":\"planar_burst\",\"code_placement\":\"flash_cached\",\"L\":8,\"sps\":2,\"gamma100\":270,"
           "\"row_profile\":\"%s\",\"quantizer\":\"native_3_4_4\",\"table_bytes\":139264,"
           "\"old_crc\":%lu,\"middle_crc\":%lu,\"future_crc\":%lu,\"seed\":%lu,\"in_words\":%u,"
           "\"input_crc\":%lu,\"input_after_crc\":%lu,\"history\":\"cyclic_source_tail\","
           "\"sym_rate\":%.1f,\"core1_idle\":true,\"interrupts_enabled\":true,\"idle_wait\":\"spin_8us\","
           "\"writers\":2,\"row_words\":4,\"descriptor_rows\":%u,\"descriptor_bytes\":%u,"
           "\"output_base\":%lu,\"desc_i_base\":%lu,\"desc_q_base\":%lu,"
           "\"row_layout\":\"I4_Q4\",\"runtime_sample_copies\":0,\"runtime_descriptor_writes\":0,"
           "\"elapsed_us\":%llu,\"errors\":%lu,\"selectors_restored\":%s,\"output_errors\":%lu,\"txstalls_i\":%lu,\"txstalls_q\":%lu,"
           "\"read_i\":%llu,\"read_q\":%llu,\"issued_i\":%llu,\"issued_q\":%llu,"
           "\"blocks\":%lu,\"generated_blocks\":%lu,\"prefill_blocks\":%u,\"block_symbols\":%u,"
           "\"gen_cyc_per_sym\":%.4f,\"gen_busy\":%.6f,\"shape_cycles\":%llu,"
           "\"worst_block_cyc_per_sym\":%.4f,\"producer_work_cycles\":%llu,\"producer_work_busy\":%.6f,"
           "\"reuse_guard_rows\":%u,\"min_lead_rows\":%llu,\"max_axis_gap_rows\":%llu,\"max_poll_gap_us\":%lu,"
           "\"cap_words\":%lu,\"cap_crc\":%lu,\"cap_stride\":17,\"cap_symbol_cursor\":%llu,"
           "\"cap_lower_symbol_cursor\":%llu,\"cap_lookbehind_rows\":32}\n",
           GIT_REV, (unsigned long)hz, ms, cpw, IQ16NATIVE8_PROFILE,
           (unsigned long)old_crc, (unsigned long)middle_crc, (unsigned long)future_crc,
           (unsigned long)SEED, IN_WORDS, (unsigned long)input_crc, (unsigned long)input_after_crc,
           hz / (4.0 * cpw), ROWS, 8 * ROWS, (unsigned long)(uintptr_t)output,
           (unsigned long)(uintptr_t)di, (unsigned long)(uintptr_t)dq,
           (unsigned long long)elapsed, (unsigned long)errors, restored ? "true" : "false", (unsigned long)p.errors,
           (unsigned long)p.txstalls_i, (unsigned long)p.txstalls_q,
           (unsigned long long)p.read_i, (unsigned long long)p.read_q,
           (unsigned long long)p.issued_i, (unsigned long long)p.issued_q,
           (unsigned long)blocks, (unsigned long)next, OUT_BLOCKS, BLOCK_SYMBOLS,
           blocks ? (double)shape_cycles / (blocks * BLOCK_SYMBOLS) : 0, budget ? shape_cycles / budget : 0,
           (unsigned long long)shape_cycles, (double)worst_shape / BLOCK_SYMBOLS,
           (unsigned long long)work_cycles, budget ? work_cycles / budget : 0, REUSE_GUARD,
           (unsigned long long)min_lead, (unsigned long long)max_axis_gap, (unsigned long)max_poll_gap,
           (unsigned long)cap_words, (unsigned long)cap_crc, (unsigned long long)cap_cursor,
           (unsigned long long)cap_lower);
    result_printed = true;
    if (cap_words == CAP_WORDS) {
        for (unsigned word = 0; word < CAP_WORDS; word += 16) {
            printf("@cap %u", word);
            for (unsigned j = 0; j < 16; ++j) printf(" %08lx", (unsigned long)capture[word + j]);
            putchar('\n');
        }
        printf("@capend\n");
    }
cleanup:
    if (started) dual_dma_stop();
    if (!result_printed) restored = dual_dma_set_writers(2) && dual_dma_set_row_words(saved_rows) &&
        dual_dma_set_writers(saved_writers) && dual_dma_set_schedule(saved_schedule) &&
        dual_dma_set_bus_priority(saved_priority);
    if (!restored) {
        // The public engine retains ownership after a failed idle-only selector.
        // Keep arenas intact rather than rebuilding over potentially owned rows.
        if (!result_printed) printf("@{\"cmd\":\"bench16planar\",\"error\":\"output selector restoration failed\",\"errors\":%u}\n", ERR_RESTORE);
        return;
    }
    iq_build_tables(&cfg); seed = SEED; prbs_fill(in_buf, IN_WORDS, &seed);
    memset(link_ring, 0, sizeof link_ring);
}
