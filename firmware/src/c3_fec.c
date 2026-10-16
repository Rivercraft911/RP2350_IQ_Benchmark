#include "c3_fec.h"
#include <string.h>
#include "pvproto.h"

uint32_t c3_gcs_slice(uint32_t *bb, const uint8_t *pattern, const dvbs2_code_t *code,
                      uint32_t frame, uint32_t cursor, uint32_t max_words) {
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
            bb[cursor] = v;
        }
    }
    const uint32_t whole = bytes / 4u, stop = end < whole ? end : whole;
    uint32_t offset = (origin + 4u * cursor - 10u) & 255u;
    for (; cursor < stop; ++cursor) {
        uint32_t v;
        memcpy(&v, pattern + offset, sizeof v);
        bb[cursor] = __builtin_bswap32(v);
        offset = (offset + 4u) & 255u;
    }
    if (cursor < end) {
        uint32_t v = 0;
        for (uint32_t n = 0; n < bytes % 4u; ++n)
            v |= (uint32_t)pattern[(offset + n) & 255u] << (24u - 8u * n);
        bb[cursor++] = v;
    }
    return cursor;
}

void c3_job_begin(c3_job_t *job, const dvbs2_8psk_cfg_t *cfg, uint32_t index,
                  uint32_t *i, uint32_t *q) {
    job->cfg = cfg;
    job->index = index;
    job->stage = C3_GCS;
    job->cursor = 0;
    const uint32_t symbols = index * C3_FRAME_SYMBOLS;
    job->stream = (dvbs2_8psk_codes_stream_t){.i = i, .q = q,
        .mask = C3_RING_GROUPS - 1u, .n = symbols / 16u,
        .fill = symbols & 15u, .defer_first = true};
}

bool c3_job_step(c3_job_t *job, const uint8_t *pattern) {
    const dvbs2_code_t *code = job->cfg->code;
    switch (job->stage) {
        case C3_GCS:
            job->cursor = c3_gcs_slice(job->bb, pattern, code, job->index, job->cursor, 64);
            if (job->cursor == (code->kbch + 31u) / 32u) {
                if (!dvbs2_frontend_begin_reentrant(job->bb, job->frame, &job->front)) return false;
                job->stage = C3_BCH;
            }
            break;
        case C3_BCH:
            if (dvbs2_frontend_step(&job->front, 64)) {
                if (!dvbs2_ldpc_begin_reentrant(job->frame, job->rows, C3_ROWS_WORDS, &job->ldpc)) return false;
                job->stage = C3_LDPC;
            }
            break;
        case C3_LDPC:
            if (dvbs2_ldpc_step(&job->ldpc, 1)) {
                dvbs2_8psk_codes_begin(&job->map, job->cfg, job->frame, &job->stream);
                job->stage = C3_MAP;
            }
            break;
        case C3_MAP:
            if (dvbs2_8psk_codes_step(&job->map, 8)) job->stage = C3_DONE;
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
