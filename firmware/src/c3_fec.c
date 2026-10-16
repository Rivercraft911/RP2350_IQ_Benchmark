#include "c3_fec.h"
#include <string.h>
#include "pvproto.h"

static uint32_t gcs_slice(uint32_t *bb, const uint8_t *pattern, const dvbs2_code_t *code,
                          uint32_t frame, uint32_t cursor, uint32_t max_words,
                          const uint32_t *scramble) {
    const uint32_t bytes = code->kbch / 8u, data_bytes = bytes - 10u;
    const uint32_t words = (bytes + 3u) / 4u;
    uint32_t end = cursor + max_words;
    if (end > words || end < cursor) end = words;
    const uint32_t origin = frame * data_bytes;
    if (cursor < 3u && cursor < end) {
        uint8_t header[10] = {0x72, 0, 0, 0, (uint8_t)((code->kbch - 80u) >> 8),
            (uint8_t)(code->kbch - 80u), 0xb9, 0, 0, 0};
        header[9] = pv_crc8(header, 9);
        for (; cursor < 3u && cursor < end; ++cursor) {
            uint32_t v = 0;
            for (uint32_t n = 0; n < 4; ++n) {
                const uint32_t at = 4u * cursor + n;
                const uint8_t b = at < 10u ? header[at] :
                    pattern[(origin + at - 10u) & 255u];
                v = (v << 8) | b;
            }
            bb[cursor] = v ^ (scramble ? scramble[cursor] : 0);
        }
    }
    const uint32_t whole = bytes / 4u, stop = end < whole ? end : whole;
    uint32_t offset = (origin + 4u * cursor - 10u) & 255u;
    for (; cursor < stop; ++cursor) {
        uint32_t v;
        memcpy(&v, pattern + offset, sizeof v);
        bb[cursor] = __builtin_bswap32(v) ^ (scramble ? scramble[cursor] : 0);
        offset = (offset + 4u) & 255u;
    }
    if (cursor < end) {
        uint32_t v = 0;
        for (uint32_t n = 0; n < bytes % 4u; ++n)
            v |= (uint32_t)pattern[(offset + n) & 255u] << (24u - 8u * n);
        bb[cursor] = v ^ (scramble ? scramble[cursor] : 0);
        if (scramble) bb[cursor] &= ~0u << (32u - 8u * (bytes % 4u));
        ++cursor;
    }
    return cursor;
}

uint32_t c3_gcs_slice(uint32_t *bb, const uint8_t *pattern, const dvbs2_code_t *code,
                      uint32_t frame, uint32_t cursor, uint32_t max_words) {
    return gcs_slice(bb, pattern, code, frame, cursor, max_words, NULL);
}

void c3_job_begin(c3_job_t *job, const dvbs2_8psk_cfg_t *cfg, uint32_t index,
                  uint32_t *i, uint32_t *q) {
    c3_job_begin_options(job, cfg, index, i, q, 0);
}

void c3_job_begin_options(c3_job_t *job, const dvbs2_8psk_cfg_t *cfg, uint32_t index,
                          uint32_t *i, uint32_t *q, uint32_t options) {
    job->cfg = cfg;
    job->options = options;
    job->index = index;
    job->stage = C3_GCS;
    job->cursor = 0;
    job->ldpc_group = NULL;
    job->bch_leaf = NULL;
    job->bch_table = NULL;
    job->transpose = NULL;
    const uint32_t symbols = index * C3_FRAME_SYMBOLS;
    job->stream = (dvbs2_8psk_codes_stream_t){.i = i, .q = q,
        .mask = C3_RING_GROUPS - 1u, .n = symbols / 16u,
        .fill = symbols & 15u, .defer_first = true};
}

bool __attribute__((noinline)) c3_job_step(c3_job_t *job, const uint8_t *pattern) {
    const dvbs2_code_t *code = job->cfg->code;
    // C9 amortizes BCH/LDPC accounting over two original units. GCS and mapping
    // retain their bounds: cold slices already reached 8k/12k cycles.
    // Command bits and the frontend's compact option byte are separate domains.
    const unsigned frontend_options = (job->options & C4_BCH ? S2_FE_FAST_BCH : 0u) |
        (job->options & C7_BCH_LDM ? S2_FE_LDM_BCH : 0u);
    switch (job->stage) {
        case C3_GCS:
            if (job->options & C4_BB) {
                const uint32_t words = (code->kbch + 31u) / 32u;
                if (job->cursor < words)
                    job->cursor = gcs_slice(job->frame, pattern, code, job->index,
                                             job->cursor, 64, dvbs2_bb_scrambler());
                else {
                    uint32_t end = job->cursor + 64u;
                    if (end > DVBS2_FE_WORDS) end = DVBS2_FE_WORDS;
                    memset(job->frame + job->cursor, 0, (end - job->cursor) * sizeof(uint32_t));
                    job->cursor = end;
                }
                if (job->cursor == DVBS2_FE_WORDS) {
                    if (!dvbs2_frontend_begin_prepared(job->frame, &job->front,
                            frontend_options)) return false;
                    job->front.leaf = job->bch_leaf ? job->bch_leaf : job->front.leaf;
                    job->front.table = job->bch_table;
                    job->stage = C3_BCH;
                }
                break;
            }
            job->cursor = c3_gcs_slice(job->bb, pattern, code, job->index, job->cursor, 64);
            if (job->cursor == (code->kbch + 31u) / 32u) {
                if (!dvbs2_frontend_begin_reentrant_options(job->bb, job->frame, &job->front,
                                                            frontend_options)) return false;
                job->front.leaf = job->bch_leaf ? job->bch_leaf : job->front.leaf;
                job->front.table = job->bch_table;
                job->stage = C3_BCH;
            }
            break;
        case C3_BCH:
            if (dvbs2_frontend_step(&job->front, job->options & C9_SCHEDULE ? 128 : 64)) {
                if (!dvbs2_ldpc_begin_reentrant_mode(job->frame, job->rows, C3_ROWS_WORDS,
                        &job->ldpc, job->options & C9_LDPC_PACK ? 3 :
                            job->options & C5_LDPC ? 2 : !!(job->options & C4_LDPC))) return false;
                if (job->ldpc_group && job->ldpc.mode) {
                    typedef void (*group_fn)(uint32_t *, const uint32_t *, uint32_t, const uint16_t *,
                                             const uint16_t *, uint32_t);
                    job->ldpc.group = (group_fn)job->ldpc_group;
                }
                if (job->transpose) job->ldpc.transpose = job->transpose;
                job->stage = C3_LDPC;
            }
            break;
        case C3_LDPC:
            if (dvbs2_ldpc_step(&job->ldpc, job->options & C9_SCHEDULE ? 2 : 1)) {
                if (job->options & C6_MAP) {
                    if (!dvbs2_8psk_codes_begin_c6(&job->map, job->cfg, job->frame, &job->stream)) return false;
                } else if (job->options & C4_MAP) {
                    if (!dvbs2_8psk_codes_begin_fast(&job->map, job->cfg, job->frame, &job->stream)) return false;
                } else dvbs2_8psk_codes_begin(&job->map, job->cfg, job->frame, &job->stream);
                job->stage = C3_MAP;
            }
            break;
        case C3_MAP:
            if (dvbs2_8psk_codes_step(&job->map, job->options & C6_MAP ? 32 : 8)) job->stage = C3_DONE;
            break;
        default: break;
    }
    return true;
}

uint32_t c3_job_publish(const c3_job_t *job, uint32_t carry[2]) {
    const dvbs2_8psk_codes_stream_t *s = &job->stream;
    const uint32_t first = (job->index * C3_FRAME_SYMBOLS / 16u) & s->mask;
    s->i[first] = s->first_i | carry[0];
    s->q[first] = s->first_q | carry[1];
    carry[0] = s->acc_i;
    carry[1] = s->acc_q;
    return s->n;
}

static bool __attribute__((noinline)) b9_gcs_step(c3_job_t *job, const uint8_t *pattern) {
    // The baseline's final GCS call used to construct the whole scrambled BB
    // frame and zero tail in frontend_begin. Use cursor >= words for a second,
    // bounded pass. No extra per-job storage or frontend option is needed.
    const dvbs2_code_t *code = job->cfg->code;
    const uint32_t words = (code->kbch + 31u) / 32u;
    if (job->cursor < words) {
        job->cursor = c3_gcs_slice(job->bb, pattern, code, job->index, job->cursor, 64);
        return true;
    }
    const uint32_t first = job->cursor - words;
    uint32_t end = first + 64u;
    if (end > DVBS2_FE_WORDS) end = DVBS2_FE_WORDS;
    const uint32_t *scramble = dvbs2_bb_scrambler();
    for (uint32_t n = first; n < end; ++n) {
        uint32_t v = n < words ? job->bb[n] ^ scramble[n] : 0;
        if (n + 1u == words && code->kbch % 32u) v &= ~0u << (32u - code->kbch % 32u);
        job->frame[n] = v;
    }
    job->cursor = words + end;
    if (end == DVBS2_FE_WORDS) {
        const unsigned frontend_options = (job->options & C4_BCH ? S2_FE_FAST_BCH : 0u) |
            (job->options & C7_BCH_LDM ? S2_FE_LDM_BCH : 0u);
        if (!dvbs2_frontend_begin_prepared(job->frame, &job->front, frontend_options)) return false;
        job->front.leaf = job->bch_leaf ? job->bch_leaf : job->front.leaf;
        job->front.table = job->bch_table;
        job->stage = C3_BCH;
    }
    return true;
}

static bool __attribute__((noinline)) b9_map_step(c3_job_t *job) {
    if (dvbs2_8psk_codes_step(&job->map, job->options & C6_MAP ? 16 : 4)) job->stage = C3_DONE;
    return true;
}

static bool __attribute__((noinline)) b9_gcs_fused_step(c3_job_t *job, const uint8_t *pattern,
                                                      c3_gcs_words_fn copy) {
    const dvbs2_code_t *code = job->cfg->code;
    const uint32_t *scramble = dvbs2_bb_scrambler();
    const uint32_t words = (code->kbch + 31u) / 32u;
    uint32_t cursor = job->cursor, end = cursor + 64u;
    if (end > DVBS2_FE_WORDS) end = DVBS2_FE_WORDS;
    // Keep header generation and a possible partial final word in the existing
    // independently checked GCS builder. Only complete payload words enter PIC.
    if (cursor < 3u) {
        const uint32_t first = cursor;
        cursor = c3_gcs_slice(job->frame, pattern, code, job->index, cursor, 3u - cursor);
        for (uint32_t n = first; n < cursor; ++n) job->frame[n] ^= scramble[n];
    }
    const uint32_t whole = code->kbch / 32u;
    const uint32_t stop = end < whole ? end : whole;
    if (cursor < stop) {
        const uint32_t origin = job->index * (code->kbch / 8u - 10u);
        const uint32_t offset = (origin + 4u * cursor - 10u) & 255u;
        copy(job->frame + cursor, pattern + offset, scramble + cursor, stop - cursor);
        cursor = stop;
    }
    if (cursor < words && cursor < end) {
        cursor = c3_gcs_slice(job->frame, pattern, code, job->index, cursor, 1);
        job->frame[cursor - 1u] ^= scramble[cursor - 1u];
        job->frame[cursor - 1u] &= ~0u << (32u - code->kbch % 32u);
    }
    if (cursor < end) memset(job->frame + cursor, 0, (end - cursor) * sizeof(uint32_t));
    job->cursor = end;
    if (end == DVBS2_FE_WORDS) {
        const unsigned frontend_options = (job->options & C4_BCH ? S2_FE_FAST_BCH : 0u) |
            (job->options & C7_BCH_LDM ? S2_FE_LDM_BCH : 0u);
        if (!dvbs2_frontend_begin_prepared(job->frame, &job->front, frontend_options)) return false;
        job->front.leaf = job->bch_leaf ? job->bch_leaf : job->front.leaf;
        job->front.table = job->bch_table;
        job->stage = C3_BCH;
    }
    return true;
}

static bool __attribute__((noinline)) b9_ldpc_step(c3_job_t *job, dvbs2_ldpc_step_fn step) {
    if (step(&job->ldpc, job->options & C9_SCHEDULE ? 2 : 1)) {
        if (job->options & C6_MAP) {
            if (!dvbs2_8psk_codes_begin_c6(&job->map, job->cfg, job->frame, &job->stream)) return false;
        } else if (job->options & C4_MAP) {
            if (!dvbs2_8psk_codes_begin_fast(&job->map, job->cfg, job->frame, &job->stream)) return false;
        } else dvbs2_8psk_codes_begin(&job->map, job->cfg, job->frame, &job->stream);
        job->stage = C3_MAP;
    }
    return true;
}

static bool __attribute__((noinline)) b9_bch_step(c3_job_t *job) {
    // Retain most of C9's dispatch amortization while shortening its longest
    // provisional BCH128 slices by a quarter for the unchanged 128-us gate.
    if (dvbs2_frontend_step(&job->front, 96)) {
        if (!dvbs2_ldpc_begin_reentrant_mode(job->frame, job->rows, C3_ROWS_WORDS,
                &job->ldpc, job->options & C9_LDPC_PACK ? 3 :
                    job->options & C5_LDPC ? 2 : !!(job->options & C4_LDPC))) return false;
        if (job->ldpc_group && job->ldpc.mode) {
            typedef void (*group_fn)(uint32_t *, const uint32_t *, uint32_t, const uint16_t *,
                                     const uint16_t *, uint32_t);
            job->ldpc.group = (group_fn)job->ldpc_group;
        }
        if (job->transpose) job->ldpc.transpose = job->transpose;
        job->stage = C3_LDPC;
    }
    return true;
}

// Retain the original job-step body out of line: copying its GCS/LDPC switch
// into this dispatcher would enlarge B9's flash working set and register frame.
bool c3_job_step_b9(c3_job_t *job, const uint8_t *pattern,
                    c3_gcs_words_fn gcs_words, dvbs2_ldpc_step_fn ldpc_step) {
    // Tail-call the selected body so the uncommon second BB pass does not add
    // its register-save frame to every original GCS/BCH/LDPC slice.
    if (job->stage == C3_MAP) return b9_map_step(job);
    if (job->stage == C3_BCH && (job->options & C9_SCHEDULE)) return b9_bch_step(job);
    if (job->stage == C3_LDPC && job->ldpc.mode >= 2 && ldpc_step) return b9_ldpc_step(job, ldpc_step);
    if (job->stage == C3_GCS && (job->options & C4_BB) && gcs_words)
        return b9_gcs_fused_step(job, pattern, gcs_words);
    if (job->stage == C3_GCS && !(job->options & C4_BB)) return b9_gcs_step(job, pattern);
    return c3_job_step(job, pattern);
}
