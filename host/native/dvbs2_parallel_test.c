// Real host threads exercise two caller-owned frontends and reduced LDPC matrices.
// Python independently checks the emitted FEC bits; host timing says nothing about M33.
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "dvbs2_codes.h"

enum { GUARD = 0x5A2C91B3, PATTERNS = 4 };
typedef struct {
    uint32_t before, frame[DVBS2_FE_WORDS], after;
    uint32_t bb[DVBS2_FE_WORDS], expected[DVBS2_FE_WORDS];
    uint32_t *allocation, *workspace, words, chunk, units, mode, options, info_bits;
    int ok;
} job_t;

static uint32_t random_word(uint32_t *seed) {
    *seed ^= *seed << 13; *seed ^= *seed >> 17; *seed ^= *seed << 5;
    return *seed;
}

static int begin(job_t *job, dvbs2_ldpc_state_t *ldpc) {
    dvbs2_frontend_t front;
    job->before = job->after = GUARD;
    memset(job->frame, 0xA5, sizeof job->frame);
    memset(job->workspace, 0xA5, job->words * sizeof(uint32_t));
    job->ok = 1;
    if (job->info_bits) {
        // Short BCH has degree168 and uses the serial-checked prepared info.
        // The concurrent frontend supports only normal degree128/160/192.
        const uint32_t words = (job->info_bits + 31u) / 32u;
        memcpy(job->frame, job->expected, words * sizeof(uint32_t));
        if (job->info_bits & 31u)
            job->frame[words - 1] &= ~(~0u >> (job->info_bits & 31u));
        memset(job->frame + words, 0, (DVBS2_FE_WORDS - words) * sizeof(uint32_t));
    } else {
        job->ok = dvbs2_frontend_begin_reentrant_options(
            job->bb, job->frame, &front, job->options) && !front.profile;
        if (!job->ok) return 0;
        dvbs2_frontend_t front_saved = front;
        job->ok &= !dvbs2_frontend_step(&front, 0) && !memcmp(&front, &front_saved, sizeof front);
        while (!dvbs2_frontend_step(&front, job->chunk)) {}
    }
    job->ok &= dvbs2_ldpc_begin_reentrant_mode(job->frame, job->workspace, job->words,
                                              ldpc, job->mode);
    if (!job->ok) return 0;
    dvbs2_ldpc_state_t untouched = *ldpc;
    job->ok &= ldpc->mode == job->mode && !dvbs2_ldpc_step(ldpc, 0) &&
        !memcmp(ldpc, &untouched, sizeof *ldpc);
    return job->ok;
}

static void finish(job_t *job, dvbs2_ldpc_state_t *ldpc) {
    const dvbs2_ldpc_state_t untouched = *ldpc;
    job->ok &= dvbs2_ldpc_step(ldpc, 0) && dvbs2_ldpc_step(ldpc, UINT32_MAX);
    job->ok &= !memcmp(ldpc, &untouched, sizeof *ldpc);
    job->ok &= !memcmp(job->frame, job->expected, sizeof job->frame);
    job->ok &= job->before == GUARD && job->after == GUARD;
    job->ok &= job->allocation[0] == GUARD && job->workspace[job->words] == GUARD;
}

static void *encode(void *arg) {
    job_t *job = arg;
    dvbs2_ldpc_state_t ldpc;
    if (begin(job, &ldpc)) {
        while (!dvbs2_ldpc_step(&ldpc, job->units)) {}
        finish(job, &ldpc);
    }
    return NULL;
}

static int interleaved(job_t jobs[2]) {
    dvbs2_ldpc_state_t states[2];
    const uint32_t schedule[] = {0, 1, 3, 0, 17, 128};
    if (!begin(&jobs[0], &states[0]) || !begin(&jobs[1], &states[1])) return 0;
    for (unsigned round = 0; !states[0].done || !states[1].done; ++round) {
        if (round > 4096) return 0;
        // Reverse the worker order on every round, crossing clear/group/prefix/
        // pack boundaries with zero, tiny and multi-stage work grants.
        for (unsigned at = 0; at < 2; ++at) {
            const unsigned worker = at ^ (round & 1u);
            dvbs2_ldpc_state_t *state = &states[worker];
            const uint32_t grant = schedule[(round + worker) % 6u];
            const dvbs2_ldpc_state_t previous = *state;
            const int done = dvbs2_ldpc_step(state, grant);
            if (!grant && !previous.done &&
                (done || memcmp(state, &previous, sizeof *state))) return 0;
        }
    }
    for (unsigned worker = 0; worker < 2; ++worker) finish(&jobs[worker], &states[worker]);
    return jobs[0].ok && jobs[1].ok;
}

int main(void) {
    const unsigned codes[] = {S2_N2_3, S2_N3_4, S2_N4_5, S2_N5_6, S2_N8_9, S2_N9_10,
        S2_S1_4, S2_S1_3, S2_S2_5, S2_S1_2, S2_S3_5, S2_S2_3, S2_S3_4, S2_S4_5, S2_S5_6, S2_S8_9};
    const uint32_t chunks[] = {1, 7, 128, 511}, units[] = {1, 3, 17, 128, UINT32_MAX};
    job_t jobs[2];
    if (dvbs2_ldpc_code_bytes() || !dvbs2_ldpc_prepare(NULL, 0) ||
        dvbs2_ldpc_code_bytes_mode(2) || !dvbs2_ldpc_prepare_mode(NULL, 0, 2) ||
        dvbs2_ldpc_copy_mode(NULL, 0, 3) || dvbs2_ldpc_prepare_mode(NULL, 0, 3)) return 10;
    for (unsigned ci = 0; ci < sizeof codes / sizeof codes[0]; ci++) {
        const dvbs2_code_t *code = &DVBS2_CODES[codes[ci]];
        dvbs2_init(code, true);
        dvbs2_ldpc_set_pack(1);
        const uint32_t words = dvbs2_ldpc_workspace_words();
        if (words != code->q * 12u) return 2;
        for (unsigned p = 0; p < PATTERNS; p++) {
            memset(jobs, 0, sizeof jobs);
            for (unsigned j = 0; j < 2; j++) {
                job_t *job = &jobs[j];
                uint32_t seed = 0xCA38D219u ^ codes[ci] ^ (j * 0x9E3779B9u);
                for (unsigned k = 0; k < DVBS2_FE_WORDS; k++)
                    job->bb[k] = p == 0 ? 0 : p == 1 ? ~0u : p == 2 ? 0xAAAAAAAAu : random_word(&seed);
                job->words = words;
                job->info_bits = code->is_short ? code->kldpc : 0;
                job->allocation = malloc((words + 2u) * sizeof(uint32_t));
                if (!job->allocation) return 3;
                job->workspace = job->allocation + 1;
                // A dirty, minimum-sized matrix exposes stale/padded-row dependencies.
                memset(job->workspace, 0xA5, words * sizeof(uint32_t));
                job->allocation[0] = job->workspace[words] = GUARD;
                dvbs2_frame_begin_into(job->bb, job->expected);
                // Check BCH against the slow recurrence before comparing LDPC output.
                uint32_t scrambled[DVBS2_FE_WORDS], parity[7];
                memcpy(scrambled, job->bb, sizeof scrambled);
                dvbs2_bbscramble(scrambled);
                dvbs2_bch_serial(scrambled, parity);
                const unsigned degree = code->t * (code->is_short ? 14u : 16u);
                for (unsigned bit = 0; bit < degree; bit++) {
                    const unsigned at = code->kbch + bit;
                    if (((job->expected[at / 32] >> (31 - at % 32)) & 1u) !=
                        ((parity[bit / 32] >> (31 - bit % 32)) & 1u)) return 4;
                }
                dvbs2_frame_ldpc_into(job->expected);
                dvbs2_ldpc_serial(job->expected, scrambled);
                for (unsigned bit = 0; bit < code->nldpc - code->kldpc; bit++) {
                    const unsigned at = code->kldpc + bit;
                    if (((job->expected[at / 32] >> (31 - at % 32)) & 1u) !=
                        ((scrambled[bit / 32] >> (31 - bit % 32)) & 1u)) return 5;
                }
            }
            int ok = 1;
            for (unsigned kernel = S2_BCH_WORDS_C; kernel <= S2_BCH_T8_ARM; kernel++) {
                dvbs2_bch_set_kernel((dvbs2_bch_kernel_t)kernel);
                for (unsigned slice = 0; slice < sizeof units / sizeof units[0]; slice++) {
                    for (unsigned mode = 0; mode < 3; ++mode) {
                        pthread_t threads[2];
                        for (unsigned j = 0; j < 2; j++) {
                            jobs[j].chunk = chunks[(slice + j) % 4];
                            jobs[j].units = units[(slice + j) % 5];
                            jobs[j].mode = (mode + j) % 3;
                            jobs[j].options = slice & 1 ? S2_FE_FAST_BCH : 0;
                            if (pthread_create(&threads[j], NULL, encode, &jobs[j])) return 6;
                        }
                        for (unsigned j = 0; j < 2; j++) {
                            if (pthread_join(threads[j], NULL)) return 7;
                            ok &= jobs[j].ok;
                        }
                    }
                }
                for (unsigned mode = 0; mode < 3; ++mode) {
                    for (unsigned j = 0; j < 2; ++j) {
                        jobs[j].mode = (mode + j) % 3;
                        jobs[j].chunk = chunks[j];
                        jobs[j].options = mode & 1 ? S2_FE_FAST_BCH : 0;
                    }
                    ok &= interleaved(jobs);
                }
            }
            if (!ok) { fprintf(stderr, "parallel mismatch %s pattern%u\n", code->name, p); return 1; }
            for (unsigned j = 0; j < 2; j++) {
                printf("{\"code\":\"%s\",\"ci\":%u,\"pattern\":%u,\"worker\":%u,\"words\":[",
                       code->name, codes[ci], p, j);
                for (unsigned k = 0; k < (code->nldpc + 31u) / 32u; k++)
                    printf("%s%u", k ? "," : "", jobs[j].frame[k]);
                puts("]}");
                free(jobs[j].allocation);
            }
        }
    }
    // Unsupported q and undersized matrix must leave caller state/storage untouched.
    dvbs2_init(&DVBS2_CODES[S2_N1_4], false);
    dvbs2_ldpc_state_t state, saved;
    memset(&state, 0xA5, sizeof state); saved = state;
    if (dvbs2_ldpc_workspace_words() ||
        dvbs2_ldpc_begin_reentrant(jobs[0].frame, jobs[0].bb, DVBS2_FE_WORDS, &state) ||
        memcmp(&state, &saved, sizeof state)) return 8;
    dvbs2_init(&DVBS2_CODES[S2_N2_3], false);
    if (dvbs2_ldpc_begin_reentrant(jobs[0].frame, jobs[0].bb, 60 * 12 - 1, &state) ||
        memcmp(&state, &saved, sizeof state)) return 9;
    if (dvbs2_ldpc_begin_reentrant_mode(jobs[0].frame, jobs[0].bb, 60 * 12, &state, 3) ||
        memcmp(&state, &saved, sizeof state)) return 11;
    dvbs2_bch_set_kernel(S2_BCH_T8_ARM);
    dvbs2_frontend_t front, front_saved;
    memset(&front, 0xA5, sizeof front); front_saved = front;
    uint32_t frame_saved[DVBS2_FE_WORDS];
    memcpy(frame_saved, jobs[0].frame, sizeof frame_saved);
    if (dvbs2_frontend_begin_reentrant_options(jobs[0].bb, jobs[0].frame, &front, 1u) ||
        dvbs2_frontend_begin_prepared(jobs[0].frame, &front, 1u) ||
        memcmp(&front, &front_saved, sizeof front) ||
        memcmp(frame_saved, jobs[0].frame, sizeof frame_saved)) return 12;
    // A prepared in-place frontend receives the same PRBS as the legacy adapter.
    dvbs2_frame_begin_into(jobs[0].bb, jobs[0].expected);
    const dvbs2_code_t *code = &DVBS2_CODES[S2_N2_3];
    const uint32_t kw = (code->kbch + 31u) / 32u;
    for (unsigned k = 0; k < kw; ++k)
        jobs[0].frame[k] = jobs[0].bb[k] ^ dvbs2_bb_scrambler()[k];
    if (code->kbch % 32u) jobs[0].frame[kw - 1] &= ~(~0u >> (code->kbch % 32u));
    memset(jobs[0].frame + kw, 0, (DVBS2_FE_WORDS - kw) * sizeof(uint32_t));
    if (!dvbs2_frontend_begin_prepared(jobs[0].frame, &front, S2_FE_FAST_BCH)) return 13;
    while (!dvbs2_frontend_step(&front, 64)) {}
    if (memcmp(jobs[0].frame, jobs[0].expected, sizeof frame_saved)) return 14;
    return 0;
}
