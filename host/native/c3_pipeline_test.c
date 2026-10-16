// Exercise the actual C3 portable job graph, including out-of-order completion.
// Physical rings and partial carries are emitted for the independent Python model.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "c3_fec.h"
#include "dvbs2_codes.h"
#include "pvproto.h"

enum { GUARD = 0xA1C35B79, FRAMES = 16 };
static struct { uint32_t before; c3_job_t job; uint32_t after; } jobs[2];
static struct { uint32_t before, words[C3_RING_GROUPS], after; } rings[2];
static uint32_t gold[5536 / 4], map_lut[DVBS2_8PSK_CODES_C6_WORDS], source_cursor;
static struct { uint32_t before, words[30720], after; } table3;
static uint8_t pattern[512], packet[188];
static unsigned transpose_calls;

static void private_transpose(uint32_t *matrix) {
    ++transpose_calls;
    dvbs2_transpose_fn transpose = dvbs2_transpose_copy(NULL, 0);
    if (!transpose) abort();
    transpose(matrix);
}

static uint8_t *source_packet(void) {
    for (unsigned k = 0; k < sizeof packet; k++) packet[k] = (uint8_t)(29u * source_cursor++ + 7u);
    return packet;
}

static int source_test(const dvbs2_code_t *code) {
    struct { uint32_t before, words[DVBS2_FE_WORDS], after; } bb;
    uint8_t expected[DVBS2_FE_WORDS * 4];
    pv_init(16);
    source_cursor = 0;
    for (unsigned f = 0; f < FRAMES; f++) {
        bb.before = bb.after = GUARD;
        memset(bb.words, 0xA5, sizeof bb.words);
        const uint32_t words = (code->kbch + 31u) / 32u;
        for (uint32_t cursor = 0; cursor < words;) {
            const uint32_t previous = bb.words[cursor];
            if (c3_gcs_slice(bb.words, pattern, code, f, cursor, 0) != cursor ||
                bb.words[cursor] != previous) return 0;
            const uint32_t next = c3_gcs_slice(bb.words, pattern, code, f, cursor, 1);
            if (next != cursor + 1) return 0;
            cursor = next;
        }
        if (c3_gcs_slice(bb.words, pattern, code, f, words, 1) != words) return 0;
        if (!pv_gcs_bbframe(expected, code->kbch, 0x72, 0xB9, source_packet)) return 0;
        for (unsigned at = 0; at < code->kbch / 8u; at++)
            if ((uint8_t)(bb.words[at / 4] >> (24u - 8u * (at & 3u))) != expected[at]) return 0;
        for (unsigned at = code->kbch / 8u; at < words * 4u; at++)
            if ((uint8_t)(bb.words[at / 4] >> (24u - 8u * (at & 3u)))) return 0;
        for (unsigned k = words; k < DVBS2_FE_WORDS; k++) if (bb.words[k] != 0xA5A5A5A5u) return 0;
        if (bb.before != GUARD || bb.after != GUARD) return 0;
    }
    return pv_stats.bbframes == FRAMES && pv_stats.gcs_underflows == 0;
}

static int complete(c3_job_t *job) {
    unsigned calls = 0;
    while (job->stage != C3_DONE) {
        if (!c3_job_step(job, pattern) || ++calls > 4096) return 0;
        if (job->stage == C3_BCH &&
            (job->front.leaf != (job->bch_leaf ? job->bch_leaf :
                (job->options & C7_BCH_LDM ? dvbs2_bch_copy(NULL, 0, 1) : NULL)) ||
             job->front.table != job->bch_table)) return 0;
        if (job->stage == C3_LDPC && job->ldpc.mode !=
            (job->options & C9_LDPC_PACK ? 3u : job->options & C5_LDPC ? 2u : !!(job->options & C4_LDPC))) return 0;
        if (job->stage == C3_MAP &&
            (job->map.c6 != !!(job->options & C6_MAP) ||
             job->map.fast != !!(job->options & (C4_MAP | C6_MAP)))) return 0;
    }
    const uint32_t n = job->stream.n, fill = job->stream.fill;
    return c3_job_step(job, pattern) && job->stage == C3_DONE &&
        job->stream.n == n && job->stream.fill == fill;
}

int main(int argc, char **argv) {
    const unsigned options = argc > 1 ? (unsigned)atoi(argv[1]) : 0;
    if (options > C9_ALL || ((options & C7_TABLE3) && (options & C7_BCH_LDM)) ||
        ((options & C9_BCH_TABLE4) && (options & (C7_TABLE3 | C7_BCH_LDM))) ||
        !dvbs2_ldpc_prepare(NULL, 0)) return 10;
    const unsigned codes[] = {S2_N2_3, S2_N5_6, S2_N3_4};
    for (unsigned k = 0; k < sizeof pattern; k++) pattern[k] = (uint8_t)(29u * k + 7u);
    for (unsigned ci = 0; ci < sizeof codes / sizeof codes[0]; ci++) {
        const dvbs2_code_t *code = &DVBS2_CODES[codes[ci]];
        if (!source_test(code)) { fprintf(stderr, "source mismatch %s\n", code->name); return 1; }
        dvbs2_bch_set_kernel(S2_BCH_WORDS_C);
        dvbs2_ldpc_set_pack(1);
        dvbs2_init(code, true);
        table3.before = table3.after = GUARD;
        const uint32_t table_bytes = options & C9_BCH_TABLE4 ? dvbs2_bch_table4_bytes() :
            options & C7_TABLE3 ? dvbs2_bch_table3_bytes() : 0;
        if ((options & C9_BCH_TABLE4) && !dvbs2_bch_table4_init(table3.words, sizeof table3.words)) return 11;
        if ((options & C7_TABLE3) && !dvbs2_bch_table3_init(table3.words, sizeof table3.words)) return 11;
        const uint32_t table_crc = pv_crc32(0, table3.words, table_bytes);
        dvbs2_bch_leaf_t bch_leaf = options & (C7_BCH_PLACE | C7_TABLE3 | C9_BCH_TABLE4) ?
            dvbs2_bch_copy(NULL, 0, options & C9_BCH_TABLE4 ? 3 : options & C7_TABLE3 ? 2 : !!(options & C7_BCH_LDM)) : NULL;
        if ((options & (C7_BCH_PLACE | C7_TABLE3 | C9_BCH_TABLE4)) && !bch_leaf) return 12;
        dvbs2_8psk_cfg_t cfg;
        if (!dvbs2_8psk_init(&cfg, code, true, 0, gold, sizeof gold / sizeof gold[0]) ||
            cfg.frame_symbols != C3_FRAME_SYMBOLS ||
            !dvbs2_8psk_codes_c6_init(&cfg, map_lut, DVBS2_8PSK_CODES_C6_WORDS)) return 2;
        memset(rings, 0, sizeof rings);
        uint32_t carry[2] = {0};
        for (unsigned axis = 0; axis < 2; axis++) rings[axis].before = rings[axis].after = GUARD;
        for (unsigned f = 0; f < FRAMES; f += 2) {
            for (unsigned worker = 0; worker < 2; worker++) {
                jobs[worker].before = jobs[worker].after = GUARD;
                c3_job_begin_options(&jobs[worker].job, &cfg, f + worker, rings[0].words, rings[1].words, options);
                jobs[worker].job.bch_leaf = bch_leaf;
                jobs[worker].job.bch_table = table_bytes ? table3.words : NULL;
                jobs[worker].job.transpose = options & C7_TRANSPOSE ? private_transpose : NULL;
            }
            const unsigned prior_transposes = transpose_calls;
            // Odd frame finishes all stages before the preceding even frame. Its
            // shared first word must remain deferred until the ordered publisher.
            const uint32_t boundary = ((f + 1u) * C3_FRAME_SYMBOLS / 16u) & (C3_RING_GROUPS - 1u);
            const uint32_t prior_i = rings[0].words[boundary], prior_q = rings[1].words[boundary];
            if (!complete(&jobs[1].job)) return 3;
            const dvbs2_8psk_codes_stream_t *odd = &jobs[1].job.stream;
            if (!odd->first_pending || rings[0].words[boundary] != prior_i ||
                rings[1].words[boundary] != prior_q || !complete(&jobs[0].job)) return 4;
            uint32_t groups = c3_job_publish(&jobs[0].job, carry);
            if (groups != (f + 1u) * C3_FRAME_SYMBOLS / 16u) return 5;
            groups = c3_job_publish(&jobs[1].job, carry);
            if (groups != (f + 2u) * C3_FRAME_SYMBOLS / 16u ||
                odd->fill != ((f + 2u) * C3_FRAME_SYMBOLS & 15u)) return 6;
            for (unsigned worker = 0; worker < 2; worker++)
                if (jobs[worker].before != GUARD || jobs[worker].after != GUARD) return 7;
            for (unsigned axis = 0; axis < 2; axis++)
                if (rings[axis].before != GUARD || rings[axis].after != GUARD) return 8;
            if ((!!(options & C7_TRANSPOSE) != (transpose_calls > prior_transposes)) ||
                table3.before != GUARD || table3.after != GUARD ||
                pv_crc32(0, table3.words, table_bytes) != table_crc) return 13;
            printf("{\"code_index\":%u,\"frames\":%u,\"published_groups\":%u,"
                   "\"pending_fill\":%u,\"pending_codes\":[%u,%u],\"rings\":[",
                   codes[ci], f + 2u, groups, odd->fill, carry[0], carry[1]);
            for (unsigned axis = 0; axis < 2; axis++) {
                printf("%s[", axis ? "," : "");
                for (unsigned k = 0; k < C3_RING_GROUPS; k++)
                    printf("%s%u", k ? "," : "", rings[axis].words[k]);
                printf("]");
            }
            puts("]}");
        }
    }
    return 0;
}
