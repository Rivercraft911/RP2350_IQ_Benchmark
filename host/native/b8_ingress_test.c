// Actual B8 ingress + C3 jobs. The Python driver checks every physical output
// word against independent GCS/scalar BCH/LDPC/8PSK references, without hardware.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "b8_ingress.h"
#include "dvbs2_codes.h"

enum { GUARD = 0xB859AC31, FRAMES = 8 };
static struct { uint32_t before; c3_job_t job; uint32_t after; } jobs[2];
static struct { uint32_t before, words[C3_RING_GROUPS], after; } rings[2];
static uint32_t gold[5536 / 4], lut[DVBS2_8PSK_CODES_LUT_WORDS];
static uint8_t message[PV_MSG_BYTES], last_copy[PV_TS], *last_packet;
static uint32_t packet_number, retries, mutations, waits;
static bool stalled;

static uint8_t *packets(void) {
    if (last_packet) {
        if (memcmp(last_packet, last_copy, PV_TS)) ++mutations;
        // A packet's lifetime ends at this callback, even on a temporary WAIT.
        memset(last_packet, 0xD3, PV_TS);
        last_packet = NULL;
    }
    if (stalled && packet_number % 3u == 1u && !retries) {
        retries = 1;
        ++waits;
        return NULL;
    }
    retries = 0;
    if (!(packet_number % 7u)) {
        pv_pattern_message(message, (packet_number / 7u) % 16u, 16);
        if (pv_accept(message, -1) != 7) abort();
    }
    last_packet = message + 12u + PV_TS * (packet_number++ % 7u);
    memcpy(last_copy, last_packet, PV_TS);
    return last_packet;
}

static int fill(b8_ingress_t *source, c3_job_t *job, unsigned budget) {
    unsigned calls = 0;
    while (job->stage == C3_GCS) {
        const b8_ingress_t saved = *source;
        const uint32_t cursor = job->cursor, prepare = source->prepare_cursor;
        if (b8_ingress_step(source, job, packets, 0) != B8_INGRESS_MORE ||
            memcmp(source, &saved, sizeof saved) || job->cursor != cursor) return 1;
        const b8_ingress_result_t result = b8_ingress_step(source, job, packets, budget);
        const uint32_t limit = budget < 64u ? budget : 64u;
        if (job->cursor - cursor > limit ||
            (saved.phase == 2u && source->prepare_cursor - prepare > limit) ||
            result == B8_INGRESS_ERROR || ++calls > 20000u) return 2;
        if (result == B8_INGRESS_READY && job->stage != C3_BCH) return 3;
        if (result == B8_INGRESS_WAIT && (!stalled || source->packet_off != PV_TS)) return 4;
    }
    if (job->stage != C3_BCH || job->front.cursor || job->front.done || job->front.profile) return 5;
    const uint32_t words = (job->cfg->code->kbch + 31u) / 32u;
    for (unsigned k = words; k < DVBS2_FE_WORDS; ++k) if (job->frame[k]) return 6;
    if (job->cfg->code->kbch & 31u) {
        const uint32_t mask = ~0u >> (job->cfg->code->kbch & 31u);
        if (job->frame[words - 1u] & mask) return 7;
    }
    return 0;
}

static int complete(c3_job_t *job) {
    unsigned calls = 0;
    while (job->stage != C3_DONE)
        if (!c3_job_step(job, NULL) || ++calls > 4096u) return 0;
    return 1;
}

int main(int argc, char **argv) {
    const unsigned options = argc > 1 ? (unsigned)atoi(argv[1]) : 27;
    const unsigned budget = argc > 2 ? (unsigned)atoi(argv[2]) : 64;
    stalled = argc > 3 && atoi(argv[3]);
    if (options > C4_ALL || !budget || !dvbs2_ldpc_prepare(NULL, 0)) return 10;
    const unsigned codes[] = {S2_N2_3, S2_N3_4, S2_N5_6};
    for (unsigned ci = 0; ci < sizeof codes / sizeof codes[0]; ++ci) {
        const dvbs2_code_t *code = &DVBS2_CODES[codes[ci]];
        pv_init(16);
        packet_number = retries = mutations = waits = 0;
        last_packet = NULL;
        b8_ingress_t source;
        b8_ingress_init(&source);
        dvbs2_bch_set_kernel(S2_BCH_WORDS_C);
        dvbs2_ldpc_set_pack(1);
        dvbs2_init(code, true);
        dvbs2_8psk_cfg_t cfg;
        if (!dvbs2_8psk_init(&cfg, code, true, 0, gold, sizeof gold / sizeof gold[0]) ||
            !dvbs2_8psk_codes_lut_init(&cfg, lut, DVBS2_8PSK_CODES_LUT_WORDS)) return 11;
        memset(rings, 0, sizeof rings);
        uint32_t carry[2] = {0};
        for (unsigned axis = 0; axis < 2; ++axis) rings[axis].before = rings[axis].after = GUARD;
        for (unsigned f = 0; f < FRAMES; f += 2u) {
            for (unsigned worker = 0; worker < 2; ++worker) {
                jobs[worker].before = jobs[worker].after = GUARD;
                memset(&jobs[worker].job, 0xA5, sizeof(c3_job_t));
                jobs[worker].job.ldpc_group = NULL;
                c3_job_begin_options(&jobs[worker].job, &cfg, f + worker,
                                     rings[0].words, rings[1].words, options);
                const int error = fill(&source, &jobs[worker].job, budget);
                if (error) { fprintf(stderr, "fill failed %s f%u error%d\n", code->name, f + worker, error); return 12; }
            }
            // Frame1 finishes first; publication must still merge frame0's tail.
            const uint32_t boundary = ((f + 1u) * C3_FRAME_SYMBOLS / 16u) & (C3_RING_GROUPS - 1u);
            const uint32_t old_i = rings[0].words[boundary], old_q = rings[1].words[boundary];
            if (!complete(&jobs[1].job) || rings[0].words[boundary] != old_i ||
                rings[1].words[boundary] != old_q || !complete(&jobs[0].job)) return 13;
            c3_job_publish(&jobs[0].job, carry);
            const uint32_t groups = c3_job_publish(&jobs[1].job, carry);
            for (unsigned worker = 0; worker < 2; ++worker)
                if (jobs[worker].before != GUARD || jobs[worker].after != GUARD) return 14;
            for (unsigned axis = 0; axis < 2; ++axis)
                if (rings[axis].before != GUARD || rings[axis].after != GUARD) return 15;
            if (source.frames != f + 2u || pv_stats.bbframes != source.frames ||
                pv_stats.gcs_bytes != source.frames * (code->kbch / 8u - 10u) ||
                pv_stats.gcs_packets != (pv_stats.gcs_bytes + PV_TS - 1u) / PV_TS ||
                pv_stats.gcs_underflows || pv_stats.null_packets || pv_stats.ts_packets || mutations ||
                (last_packet && memcmp(last_packet, last_copy, PV_TS))) return 16;
            printf("{\"code_index\":%u,\"frames\":%u,\"published_groups\":%u,"
                   "\"pending_fill\":%u,\"pending_codes\":[%u,%u],\"bb_crc\":[%u,%u,%u,%u],"
                   "\"waits\":%u,\"rings\":[", codes[ci], f + 2u, groups,
                   jobs[1].job.stream.fill, carry[0], carry[1], source.bb_crc[0], source.bb_crc[1],
                   source.bb_crc[2], source.bb_crc[3], waits);
            for (unsigned axis = 0; axis < 2; ++axis) {
                printf("%s[", axis ? "," : "");
                for (unsigned k = 0; k < C3_RING_GROUPS; ++k) printf("%s%u", k ? "," : "", rings[axis].words[k]);
                printf("]");
            }
            puts("]}");
        }
    }
    return 0;
}
