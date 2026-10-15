#include "dvbs2_8psk.h"
#include <stddef.h>
#include <string.h>

#ifdef IQ_ON_DEVICE
#define MAP_HOT __attribute__((section(".time_critical.dvbs2_8psk")))
#else
#define MAP_HOT
#endif

// Existing code->modcod identifies the QPSK rate. Keep that table unchanged.
static unsigned modcod8(const dvbs2_code_t *code) {
    if (!code || (code->nldpc != 64800 && code->nldpc != 16200) ||
        code->nldpc != (code->is_short ? 16200 : 64800)) return 0;
    switch (code->modcod) {
        case 5: return 12; // 3/5; reversed interleaver columns
        case 6: return 13; // 2/3
        case 7: return 14; // 3/4
        case 9: return 15; // 5/6
        case 10: return 16; // 8/9
        case 11: return code->is_short ? 0 : 17; // 9/10, normal only
        default: return 0;
    }
}

uint32_t dvbs2_8psk_arena_words(const dvbs2_code_t *code, bool pilots) {
    if (!modcod8(code)) return 0;
    const uint32_t data = code->nldpc / 3u;
    const uint32_t body = data + (pilots ? 36u * ((data / 90u - 1u) / 16u) : 0);
    return 2u * ((body + 31u) / 32u + 1u);
}

static uint32_t xstep(uint32_t x) { return (x >> 1) | ((((x >> 7) ^ x) & 1u) << 17); }
static uint32_t ystep(uint32_t y) { return (y >> 1) | ((((y >> 10) ^ (y >> 7) ^ (y >> 5) ^ y) & 1u) << 17); }

bool dvbs2_8psk_init(dvbs2_8psk_cfg_t *cfg, const dvbs2_code_t *code, bool pilots,
                   uint32_t gold_code, uint32_t *arena, uint32_t arena_words) {
    const uint32_t need = dvbs2_8psk_arena_words(code, pilots);
    if (!cfg || !arena || !need || arena_words < need || gold_code >= 262143u) return false;
    *cfg = (dvbs2_8psk_cfg_t){.code = code, .gold0 = arena, .gold1 = arena + need / 2,
        .data_symbols = code->nldpc / 3u, .gold_code = gold_code,
        .modcod = (uint8_t)modcod8(code), .pilots = pilots, .is_short = code->is_short,
        .column_reverse = code->modcod == 5};
    cfg->pilot_blocks = pilots ? (cfg->data_symbols / 90u - 1u) / 16u : 0;
    cfg->body_symbols = cfg->data_symbols + 36u * cfg->pilot_blocks;
    cfg->frame_symbols = 90u + cfg->body_symbols;
    memset(arena, 0, need * sizeof *arena);
    uint32_t xa = 1, ya = 0x3ffffu, yb = ya;
    for (uint32_t i = 0; i < gold_code; i++) xa = xstep(xa);
    uint32_t xb = xa;
    for (uint32_t i = 0; i < 131072u; i++) { xb = xstep(xb); yb = ystep(yb); }
    for (uint32_t i = 0; i < cfg->body_symbols; i++) {
        cfg->gold0[i >> 5] |= ((xa ^ ya) & 1u) << (i & 31);
        cfg->gold1[i >> 5] |= ((xb ^ yb) & 1u) << (i & 31);
        xa = xstep(xa); ya = ystep(ya); xb = xstep(xb); yb = ystep(yb);
    }
    // ETSI5.5.2: SOF and bi-orthogonal PLS, followed by pi/2-BPSK.
    static const uint32_t generator[6] = {0x55555555u, 0x33333333u, 0x0f0f0f0fu,
        0x00ff00ffu, 0x0000ffffu, 0xffffffffu};
    const uint32_t signalling = (uint32_t)cfg->modcod << 2 | (uint32_t)cfg->is_short << 1 | pilots;
    uint32_t y = 0;
    for (unsigned j = 0; j < 6; j++) if ((signalling >> (6 - j)) & 1u) y ^= generator[j];
    uint64_t pls = 0;
    for (unsigned j = 0; j < 32; j++) {
        const uint64_t bit = (y >> (31 - j)) & 1u;
        pls |= bit << (63 - 2 * j) | (bit ^ (signalling & 1u)) << (62 - 2 * j);
    }
    pls ^= 0x719d83c953422dfaull;
    for (unsigned j = 0; j < 90; j++) {
        const uint32_t bit = j < 26 ? (0x18d2e82u >> (25 - j)) & 1u : (uint32_t)(pls >> (89 - j)) & 1u;
        const uint32_t bi = bit ^ (j & 1u), bq = bit, shift = j & 15u;
        // Rotate QPSK by pi/8: A-signs=(bi,bq), B-signs=(!bq,bi).
        cfg->header[j >> 4].a |= bi << shift | bq << (16 + shift);
        cfg->header[j >> 4].b |= (bq ^ 1u) << shift | bi << (16 + shift);
    }
    return true;
}

static inline uint32_t reverse32(uint32_t x) {
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    __asm("rbit %0, %1" : "=r"(x) : "r"(x));
    return x;
#else
    x = ((x & 0x55555555u) << 1) | ((x >> 1) & 0x55555555u);
    x = ((x & 0x33333333u) << 2) | ((x >> 2) & 0x33333333u);
    x = ((x & 0x0f0f0f0fu) << 4) | ((x >> 4) & 0x0f0f0f0fu);
    x = (x << 24) | ((x & 0xff00u) << 8) | ((x >> 8) & 0xff00u) | (x >> 24);
    return x;
#endif
}

static inline uint32_t column32(const uint32_t *frame, uint32_t bit) {
    const uint32_t word = bit >> 5, shift = bit & 31u;
    const uint32_t v = shift ? frame[word] << shift | frame[word + 1] >> (32 - shift) : frame[word];
    return reverse32(v);
}

static inline uint32_t gold32(const uint32_t *plane, uint32_t position) {
    const uint32_t word = position >> 5, shift = position & 31u;
    return shift ? plane[word] >> shift | plane[word + 1] << (32 - shift) : plane[word];
}

typedef struct { uint32_t ai, aq, bi, bq; } planes_t;

// Three interleaver columns, a=labelMSB. Gold rotation is included algebraically.
static inline planes_t map32(uint32_t a, uint32_t b, uint32_t c, uint32_t r0, uint32_t r1) {
    const uint32_t w = (a & (b ^ c ^ r0)) ^ r1, d = a ^ r0;
    const uint32_t qa = b ^ w, qb = c ^ w ^ r0;
    return (planes_t){.ai = qa ^ d, .aq = qa, .bi = ~(qb ^ d), .bq = qb};
}

static inline void emit(dvbs2_8psk_stream_t *s, uint32_t ai, uint32_t aq, uint32_t bi, uint32_t bq) {
    s->out[s->n++ & s->mask] = (iq8_input_t){(ai & 0xffffu) | aq << 16, (bi & 0xffffu) | bq << 16};
}

static inline void put32(dvbs2_8psk_stream_t *s, planes_t p) {
    const uint32_t f = s->fill;
    emit(s, s->acc_ai | p.ai << f, s->acc_aq | p.aq << f,
         s->acc_bi | p.bi << f, s->acc_bq | p.bq << f);
    emit(s, p.ai >> (16 - f), p.aq >> (16 - f), p.bi >> (16 - f), p.bq >> (16 - f));
    // Two shifts produce zero for fill0 without an undefined shift by32.
    s->acc_ai = (p.ai >> (31 - f)) >> 1; s->acc_aq = (p.aq >> (31 - f)) >> 1;
    s->acc_bi = (p.bi >> (31 - f)) >> 1; s->acc_bq = (p.bq >> (31 - f)) >> 1;
}

static inline void put_small(dvbs2_8psk_stream_t *s, planes_t p, uint32_t count) {
    const uint32_t mask = (1u << count) - 1u;
    s->acc_ai |= (p.ai & mask) << s->fill; s->acc_aq |= (p.aq & mask) << s->fill;
    s->acc_bi |= (p.bi & mask) << s->fill; s->acc_bq |= (p.bq & mask) << s->fill;
    s->fill += count;
    if (s->fill >= 16) {
        emit(s, s->acc_ai, s->acc_aq, s->acc_bi, s->acc_bq);
        s->acc_ai >>= 16; s->acc_aq >>= 16; s->acc_bi >>= 16; s->acc_bq >>= 16;
        s->fill -= 16;
    }
}

static inline void put(dvbs2_8psk_stream_t *s, planes_t p, uint32_t count) {
    if (count == 32) { put32(s, p); return; }
    if (count > 16) {
        put_small(s, p, 16);
        p.ai >>= 16; p.aq >>= 16; p.bi >>= 16; p.bq >>= 16; count -= 16;
    }
    put_small(s, p, count);
}

void MAP_HOT dvbs2_8psk_frame(const dvbs2_8psk_cfg_t *cfg, const uint32_t *frame,
                             dvbs2_8psk_stream_t *stream) {
    dvbs2_8psk_stream_t s = *stream;
    for (unsigned j = 0; j < 6; j++) {
        const iq8_input_t h = cfg->header[j];
        put_small(&s, (planes_t){h.a & 0xffffu, h.a >> 16, h.b & 0xffffu, h.b >> 16}, j == 5 ? 10 : 16);
    }
    uint32_t data = 0, position = 0, block = 0;
    while (data < cfg->data_symbols) {
        uint32_t run = cfg->data_symbols - data;
        if (cfg->pilots && run > 1440) run = 1440;
        const uint32_t end = data + run;
        while (data < end) {
            const uint32_t count = end - data < 32 ? end - data : 32;
            const uint32_t col0 = cfg->column_reverse ? 2 * cfg->data_symbols : 0;
            const uint32_t col2 = cfg->column_reverse ? 0 : 2 * cfg->data_symbols;
            planes_t p = map32(column32(frame, col0 + data), column32(frame, cfg->data_symbols + data),
                              column32(frame, col2 + data), gold32(cfg->gold0, position), gold32(cfg->gold1, position));
            put(&s, p, count); data += count; position += count;
        }
        if (block < cfg->pilot_blocks && data < cfg->data_symbols) {
            put32(&s, map32(0, 0, 0, gold32(cfg->gold0, position), gold32(cfg->gold1, position)));
            position += 32;
            put_small(&s, map32(0, 0, 0, gold32(cfg->gold0, position), gold32(cfg->gold1, position)), 4);
            position += 4; block++;
        }
    }
    *stream = s;
}

void dvbs2_8psk_flush(dvbs2_8psk_stream_t *stream) {
    if (stream->fill) put_small(stream, (planes_t){0, 0, 0, ~0u}, 16u - stream->fill);
}
