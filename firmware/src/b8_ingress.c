#include "b8_ingress.h"
#include <string.h>

void b8_ingress_init(b8_ingress_t *source) {
    *source = (b8_ingress_t){.packet_off = PV_TS};
}

static void begin(b8_ingress_t *source, c3_job_t *job) {
    const uint32_t dfl = job->cfg->code->kbch - 80u;
    const uint8_t header[10] = {0x72, 0, 0, 0, (uint8_t)(dfl >> 8),
        (uint8_t)dfl, 0xb9, 0, 0, 0};
    memcpy(source->header, header, sizeof header);
    source->header[9] = pv_crc8(source->header, 9);
    source->active = job;
    source->phase = 1;
    source->partial = source->partial_bytes = source->crc = 0;
}

b8_ingress_result_t b8_ingress_step(b8_ingress_t *source, c3_job_t *job,
                                    pv_packet_fn next, uint32_t max_words) {
    if (!source || !job || !job->cfg || !job->cfg->code || !next ||
        job->stage != C3_GCS || (job->options & ~C4_ALL) ||
        (source->active && source->active != job)) return B8_INGRESS_ERROR;
    const uint32_t bytes = job->cfg->code->kbch / 8u, words = (bytes + 3u) / 4u;
    if ((job->cfg->code->kbch & 7u) || bytes < 10u || words > DVBS2_FE_WORDS ||
        (!source->active && job->cursor)) return B8_INGRESS_ERROR;
    if (!max_words) return B8_INGRESS_MORE;
    if (max_words > 64u) max_words = 64u;
    if (!source->active) begin(source, job);
    const uint32_t *scramble = dvbs2_bb_scrambler();
    if (source->phase == 2u) {
        uint32_t end = source->prepare_cursor + max_words;
        if (end > DVBS2_FE_WORDS) end = DVBS2_FE_WORDS;
        for (; source->prepare_cursor < end; ++source->prepare_cursor) {
            const uint32_t at = source->prepare_cursor;
            uint32_t v = at < words ? job->bb[at] ^ scramble[at] : 0;
            if (at + 1u == words && (bytes & 3u)) v &= ~0u << (32u - 8u * (bytes & 3u));
            job->frame[at] = v;
        }
        if (end != DVBS2_FE_WORDS) return B8_INGRESS_MORE;
        if (!dvbs2_frontend_begin_prepared(job->frame, &job->front,
                                            job->options & C4_BCH)) return B8_INGRESS_ERROR;
        if (source->frames < 4u) source->bb_crc[source->frames] = source->crc;
        ++source->frames;
        ++pv_stats.bbframes;
        source->active = NULL;
        source->phase = 0;
        job->stage = C3_BCH;
        return B8_INGRESS_READY;
    }
    uint32_t end = job->cursor + max_words;
    if (end > words) end = words;
    while (job->cursor < end) {
        const uint32_t at = job->cursor * 4u;
        const uint32_t take = bytes - at < 4u ? bytes - at : 4u;
        if (!source->partial_bytes && at >= 10u && take == 4u &&
            source->packet_off + 4u <= PV_TS) {
            uint32_t count = end - job->cursor;
            if (count > (PV_TS - source->packet_off) / 4u)
                count = (PV_TS - source->packet_off) / 4u;
            if (count > (bytes - at) / 4u) count = (bytes - at) / 4u;
            const uint8_t *input = source->packet + source->packet_off;
            uint32_t *output = (job->options & C4_BB ? job->frame : job->bb) + job->cursor;
            if (job->options & C4_BB) {
                const uint32_t *prbs = scramble + job->cursor;
                for (uint32_t n = 0; n < count; ++n) {
                    uint32_t raw;
                    memcpy(&raw, input + 4u * n, sizeof raw);
                    output[n] = __builtin_bswap32(raw) ^ prbs[n];
                }
            } else {
                for (uint32_t n = 0; n < count; ++n) {
                    uint32_t raw;
                    memcpy(&raw, input + 4u * n, sizeof raw);
                    output[n] = __builtin_bswap32(raw);
                }
            }
            if (source->frames < 4u) source->crc = pv_crc32(source->crc, input, 4u * count);
            source->packet_off += 4u * count;
            pv_stats.gcs_bytes += 4u * count;
            job->cursor += count;
            continue;
        }
        while (source->partial_bytes < take) {
            const uint32_t pos = at + source->partial_bytes;
            uint8_t b;
            if (pos < 10u) b = source->header[pos];
            else {
                if (source->packet_off == PV_TS) {
                    source->packet = next();
                    if (!source->packet) return B8_INGRESS_WAIT;
                    source->packet_off = 0;
                    ++pv_stats.gcs_packets;
                }
                b = source->packet[source->packet_off++];
                ++pv_stats.gcs_bytes;
            }
            source->partial |= (uint32_t)b << (24u - 8u * source->partial_bytes++);
        }
        uint32_t v = source->partial;
        if (source->frames < 4u) {
            const uint32_t raw = __builtin_bswap32(v);
            source->crc = pv_crc32(source->crc, &raw, take);
        }
        if (job->options & C4_BB) {
            v ^= scramble[job->cursor];
            if (take < 4u) v &= ~0u << (32u - 8u * take);
            job->frame[job->cursor] = v;
        } else job->bb[job->cursor] = v;
        source->partial = source->partial_bytes = 0;
        ++job->cursor;
    }
    if (job->cursor == words) {
        source->phase = 2;
        source->prepare_cursor = job->options & C4_BB ? words : 0;
    }
    return B8_INGRESS_MORE;
}
