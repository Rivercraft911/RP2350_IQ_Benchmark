#include "dvbs2_8psk.h"
#include <stddef.h>
#include <string.h>
#ifdef IQ_R2_HAZARD3
#include "r2_hazard3_bits.h"
#endif

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
    *cfg = (dvbs2_8psk_cfg_t){.code = code, .gold = (dvbs2_8psk_gold_t *)arena,
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
    uint32_t data = 0, pilot = cfg->data_symbols;
    for (uint32_t i = 0; i < cfg->body_symbols; i++) {
        // Advance Gold in transmission order, but store the data bits first and
        // pilot bits last. Normal FE columns and both data Gold planes then have
        // the same word boundaries, including across 36-symbol pilot blocks.
        const bool is_pilot = pilots && i % 1476u >= 1440u;
        const uint32_t bit = is_pilot ? pilot++ : data++;
        cfg->gold[bit >> 5].r0 |= ((xa ^ ya) & 1u) << (bit & 31);
        cfg->gold[bit >> 5].r1 |= ((xb ^ yb) & 1u) << (bit & 31);
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
#if defined(IQ_R2_HAZARD3)
    return r2_hazard3_reverse32(x);
#elif defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
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

static inline dvbs2_8psk_gold_t gold32(const dvbs2_8psk_gold_t *gold, uint32_t position) {
    const uint32_t word = position >> 5, shift = position & 31u;
    const dvbs2_8psk_gold_t a = gold[word];
    if (!shift) return a;
    const dvbs2_8psk_gold_t b = gold[word + 1];
    return (dvbs2_8psk_gold_t){a.r0 >> shift | b.r0 << (32 - shift),
                             a.r1 >> shift | b.r1 << (32 - shift)};
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

static inline void put_pilot(const dvbs2_8psk_cfg_t *cfg, dvbs2_8psk_stream_t *s, uint32_t block) {
    const uint32_t position = cfg->data_symbols + 36u * block;
    const dvbs2_8psk_gold_t a = gold32(cfg->gold, position), b = gold32(cfg->gold, position + 32);
    put32(s, map32(0, 0, 0, a.r0, a.r1));
    put_small(s, map32(0, 0, 0, b.r0, b.r1), 4);
}

// Short columns are 5400 bits long, so their FE boundaries need the general
// extractor. Keep this complete, infrequently used path in flash on the Pico.
static void __attribute__((noinline)) map_short(const dvbs2_8psk_cfg_t *cfg, const uint32_t *frame,
                                               dvbs2_8psk_stream_t *s) {
    uint32_t data = 0, block = 0;
    const uint32_t col0 = cfg->column_reverse ? 2 * cfg->data_symbols : 0;
    const uint32_t col2 = cfg->column_reverse ? 0 : 2 * cfg->data_symbols;
    while (data < cfg->data_symbols) {
        uint32_t run = cfg->data_symbols - data;
        if (cfg->pilots && run > 1440) run = 1440;
        const uint32_t end = data + run;
        while (data < end) {
            const uint32_t count = end - data < 32 ? end - data : 32;
            const dvbs2_8psk_gold_t gold = gold32(cfg->gold, data);
            planes_t p = map32(column32(frame, col0 + data), column32(frame, cfg->data_symbols + data),
                              column32(frame, col2 + data), gold.r0, gold.r1);
            put(s, p, count); data += count;
        }
        if (block < cfg->pilot_blocks && data < cfg->data_symbols) {
            put_pilot(cfg, s, block++);
        }
    }
}

static inline uint32_t rotate_left(uint32_t word, uint32_t shift) {
    return (word << shift) | (word >> ((0u - shift) & 31u));
}

// Append both I/Q lanes together. Rotating a packed 16-symbol word by fill
// leaves the overflow in the opposite lane; ROR16 brings it back into place.
static inline uint32_t packed_append(uint32_t *carry, uint32_t word, uint32_t fill, uint32_t mask) {
    const uint32_t v = rotate_left(word, fill), spill = v & mask;
    const uint32_t out = (v & ~mask) | *carry;
    *carry = (spill << 16) | (spill >> 16);
    return out;
}

static void MAP_HOT __attribute__((noinline)) map_normal(const dvbs2_8psk_cfg_t *cfg, const uint32_t *frame,
                                                        dvbs2_8psk_stream_t *s) {
    // 64800/3 = 21600 bits/column = 675 whole words. Reverse 3/5 once.
    const uint32_t *a = frame + (cfg->column_reverse ? 1350 : 0), *b = frame + 675,
                   *c = frame + (cfg->column_reverse ? 0 : 1350);
    uint32_t word = 0, block = 0;
    while (word < 675) {
        const uint32_t end = cfg->pilots && word + 45 < 675 ? word + 45 : 675;
        const uint32_t fill = s->fill, mask = ((1u << fill) - 1u) * 0x10001u;
        uint32_t carry_a = s->acc_ai | (s->acc_aq << 16), carry_b = s->acc_bi | (s->acc_bq << 16);
        uint32_t n = s->n;
        while (word < end) {
            const uint32_t index = n & s->mask, after = s->mask - index;
            // floor((after+1)/2), without wrapping for a linear buffer's ~0 mask.
            uint32_t count = (after >> 1) + (after & 1u);
            const bool wrap_pair = count == 0;
            iq8_input_t crossing[2], *out = wrap_pair ? crossing : s->out + index;
            if (wrap_pair) count = 1;
            if (count > end - word) count = end - word;
            const uint32_t stop = word + count;
            do {
                const dvbs2_8psk_gold_t gold = cfg->gold[word];
                const planes_t p = map32(reverse32(a[word]), reverse32(b[word]), reverse32(c[word]),
                                          gold.r0, gold.r1);
                // Pack the earlier and later halves of each I/Q amplitude plane.
                out[0].a = packed_append(&carry_a, (p.ai & 0xffffu) | (p.aq << 16), fill, mask);
                out[0].b = packed_append(&carry_b, (p.bi & 0xffffu) | (p.bq << 16), fill, mask);
                out[1].a = packed_append(&carry_a, (p.ai >> 16) | (p.aq & 0xffff0000u), fill, mask);
                out[1].b = packed_append(&carry_b, (p.bi >> 16) | (p.bq & 0xffff0000u), fill, mask);
                out += 2;
            } while (++word < stop);
            if (wrap_pair) {
                s->out[index] = crossing[0];
                s->out[0] = crossing[1];
            }
            n += 2 * count;
        }
        s->n = n;
        s->acc_ai = carry_a & 0xffffu; s->acc_aq = carry_a >> 16;
        s->acc_bi = carry_b & 0xffffu; s->acc_bq = carry_b >> 16;
        if (block < cfg->pilot_blocks && word < 675) put_pilot(cfg, s, block++);
    }
}

void MAP_HOT dvbs2_8psk_frame(const dvbs2_8psk_cfg_t *cfg, const uint32_t *frame,
                             dvbs2_8psk_stream_t *stream) {
    dvbs2_8psk_stream_t s = *stream;
    for (unsigned j = 0; j < 6; j++) {
        const iq8_input_t h = cfg->header[j];
        put_small(&s, (planes_t){h.a & 0xffffu, h.a >> 16, h.b & 0xffffu, h.b >> 16}, j == 5 ? 10 : 16);
    }
    if (cfg->is_short) map_short(cfg, frame, &s);
    else map_normal(cfg, frame, &s);
    *stream = s;
}

void dvbs2_8psk_flush(dvbs2_8psk_stream_t *stream) {
    if (stream->fill) put_small(stream, (planes_t){0, 0, 0, ~0u}, 16u - stream->fill);
}

static inline uint32_t codes_spread16(uint32_t bits) {
#ifdef IQ_R2_HAZARD3
    return r2_hazard3_spread16(bits);
#else
    bits &= 0xffffu;
    bits = (bits | bits << 8) & 0x00ff00ffu;
    bits = (bits | bits << 4) & 0x0f0f0f0fu;
    bits = (bits | bits << 2) & 0x33333333u;
    return (bits | bits << 1) & 0x55555555u;
#endif
}

static inline uint32_t codes_spread_lut(uint32_t bits, const uint16_t *lut) {
    return (uint32_t)lut[bits & 255u] | (uint32_t)lut[(bits >> 8) & 255u] << 16;
}

static inline uint32_t codes_zip_lut(uint32_t lo, uint32_t hi, const uint16_t *lut) {
    return codes_spread_lut(lo, lut) | codes_spread_lut(hi, lut) << 1;
}

// Perfect shuffle of two 16-bit lanes into successive two-bit codes. Four
// swap stages avoid separately spreading both signs and then combining them.
static inline uint32_t codes_zip16(uint32_t lo, uint32_t hi) {
#ifdef IQ_R2_HAZARD3
    return r2_hazard3_zip16(lo, hi);
#else
    uint32_t v = (lo & 0xffffu) | (hi << 16), t;
    t = (v ^ (v >> 8)) & 0x0000ff00u; v ^= t ^ (t << 8);
    t = (v ^ (v >> 4)) & 0x00f000f0u; v ^= t ^ (t << 4);
    t = (v ^ (v >> 2)) & 0x0c0c0c0cu; v ^= t ^ (t << 2);
    t = (v ^ (v >> 1)) & 0x22222222u; return v ^ t ^ (t << 1);
#endif
}

static inline void codes_emit(dvbs2_8psk_codes_stream_t *s, uint32_t i, uint32_t q) {
    const uint32_t index = s->n++ & s->mask;
    if (s->defer_first && !s->first_pending) {
        s->first_i = i; s->first_q = q; s->first_pending = true; return;
    }
    s->i[index] = i; s->q[index] = q;
}

static inline void codes_put16(dvbs2_8psk_codes_stream_t *s, uint32_t i, uint32_t q) {
    const uint32_t shift = 2u * s->fill;
    codes_emit(s, s->acc_i | i << shift, s->acc_q | q << shift);
    // Split the shift so an aligned stream never shifts by 32.
    s->acc_i = (i >> (31u - shift)) >> 1;
    s->acc_q = (q >> (31u - shift)) >> 1;
}

static inline void codes_put_small(dvbs2_8psk_codes_stream_t *s,
                                   uint32_t i, uint32_t q, uint32_t count) {
    const uint32_t mask = (1u << (2u * count)) - 1u, shift = 2u * s->fill;
    s->acc_i |= (i & mask) << shift; s->acc_q |= (q & mask) << shift;
    if (s->fill + count < 16) { s->fill += count; return; }
    codes_emit(s, s->acc_i, s->acc_q);
    s->acc_i = (i & mask) >> (32u - shift);
    s->acc_q = (q & mask) >> (32u - shift);
    s->fill += count - 16u;
}

static inline void codes_put(dvbs2_8psk_codes_stream_t *s,
                             uint32_t i, uint32_t q, uint32_t count) {
    if (count == 16) codes_put16(s, i, q);
    else codes_put_small(s, i, q, count);
}

// The mapped signs obey ai^aq=d and bi^bq=~d, d=a^r0. Therefore one
// packed Q word and one spread d plane give I directly, with three rather
// than four independently spread planes. Gold stays bit-parallel throughout.
static inline void codes_map_put(dvbs2_8psk_codes_stream_t *s, uint32_t a,
                                  uint32_t b, uint32_t c,
                                  dvbs2_8psk_gold_t gold, uint32_t count) {
    const uint32_t w = (a & (b ^ c ^ gold.r0)) ^ gold.r1, d = a ^ gold.r0;
    const uint32_t qa = b ^ w, qb = c ^ w ^ gold.r0;
    const uint32_t q0 = codes_zip16(qa, qb);
    const uint32_t i0 = q0 ^ (3u * codes_spread16(d)) ^ 0xaaaaaaaau;
    codes_put(s, i0, q0, count < 16 ? count : 16);
    if (count > 16) {
        const uint32_t q1 = codes_zip16(qa >> 16, qb >> 16);
        const uint32_t i1 = q1 ^ (3u * codes_spread16(d >> 16)) ^ 0xaaaaaaaau;
        codes_put(s, i1, q1, count - 16u);
    }
}

static void codes_header(const dvbs2_8psk_cfg_t *cfg, dvbs2_8psk_codes_stream_t *s) {
    for (unsigned j = 0; j < 6; j++) {
        const iq8_input_t h = cfg->header[j];
        codes_put(s, codes_zip16(h.a, h.b), codes_zip16(h.a >> 16, h.b >> 16),
                  j == 5 ? 10 : 16);
    }
}

static void codes_pilot(const dvbs2_8psk_cfg_t *cfg, dvbs2_8psk_codes_stream_t *s,
                         uint32_t block) {
    const uint32_t position = cfg->data_symbols + 36u * block;
    codes_map_put(s, 0, 0, 0, gold32(cfg->gold, position), 32);
    codes_map_put(s, 0, 0, 0, gold32(cfg->gold, position + 32u), 4);
}

typedef struct { uint32_t i, q; } codes_pair_t;

typedef void (*codes_run_t)(const uint32_t *, const uint32_t *, const uint32_t *,
                            const dvbs2_8psk_gold_t *, const uint16_t *,
                            dvbs2_8psk_codes_stream_t *, uint32_t);
static void codes_lut_run(const uint32_t *a, const uint32_t *b, const uint32_t *c,
                          const dvbs2_8psk_gold_t *gold, const uint16_t *lut,
                          dvbs2_8psk_codes_stream_t *s, uint32_t count);
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
extern const uint8_t __start_c4_mapper[], __stop_c4_mapper[];
#endif

bool dvbs2_8psk_codes_lut_init(dvbs2_8psk_cfg_t *cfg, uint32_t *arena, uint32_t words) {
    if (!cfg || !cfg->code || !cfg->gold || !arena || words < DVBS2_8PSK_CODES_LUT_WORDS) return false;
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    const uint32_t bytes = (uint32_t)((uintptr_t)__stop_c4_mapper - (uintptr_t)__start_c4_mapper);
    if (bytes > 1024u) return false;
#endif
    uint16_t *lut = (uint16_t *)arena;
    for (unsigned i = 0; i < 256; i++) lut[i] = (uint16_t)codes_spread16(i);
    codes_pair_t *packed = (codes_pair_t *)(arena + 128);
    for (unsigned j = 0; j < 6; j++) {
        const iq8_input_t h = cfg->header[j];
        packed[j] = (codes_pair_t){codes_zip16(h.a, h.b), codes_zip16(h.a >> 16, h.b >> 16)};
    }
    for (unsigned block = 0; block < cfg->pilot_blocks; block++) {
        uint32_t i[2], q[2];
        dvbs2_8psk_codes_stream_t s = {.i = i, .q = q, .mask = ~0u};
        codes_pilot(cfg, &s, block);
        packed[6 + 3 * block] = (codes_pair_t){i[0], q[0]};
        packed[7 + 3 * block] = (codes_pair_t){i[1], q[1]};
        packed[8 + 3 * block] = (codes_pair_t){s.acc_i, s.acc_q};
    }
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    memcpy(arena + 228, __start_c4_mapper, bytes);
    __asm volatile("dsb\n\tisb" ::: "memory");
    const uintptr_t run = (uintptr_t)(arena + 228) | 1u;
#else
    const uintptr_t run = (uintptr_t)codes_lut_run;
#endif
    memcpy(arena + 224, &run, sizeof run);
    cfg->codes_lut = lut;
    return true;
}

static void codes_header_fast(const dvbs2_8psk_cfg_t *cfg, dvbs2_8psk_codes_stream_t *s) {
    const codes_pair_t *packed = (const codes_pair_t *)((const uint32_t *)cfg->codes_lut + 128);
    for (unsigned j = 0; j < 6; j++) codes_put(s, packed[j].i, packed[j].q, j == 5 ? 10 : 16);
}

static void codes_pilot_fast(const dvbs2_8psk_cfg_t *cfg, dvbs2_8psk_codes_stream_t *s,
                             uint32_t block) {
    const codes_pair_t *packed = (const codes_pair_t *)((const uint32_t *)cfg->codes_lut + 128) + 6 + 3 * block;
    codes_put16(s, packed[0].i, packed[0].q);
    codes_put16(s, packed[1].i, packed[1].q);
    codes_put_small(s, packed[2].i, packed[2].q, 4);
}

// The normal-frame columns and pilot intervals are whole 32-bit words. Keep
// their inner loop free of general extractors, short tails and stream checks.
// The header has already emitted the deferred first word before this runs.
static void __attribute__((noinline)) codes_normal(const dvbs2_8psk_cfg_t *cfg,
                                                  const uint32_t *frame,
                                                  dvbs2_8psk_codes_stream_t *s,
                                                  uint32_t *data, uint32_t *block,
                                                  uint32_t budget) {
    const uint32_t *a = frame + (cfg->column_reverse ? 1350 : 0), *b = frame + 675,
                   *c = frame + (cfg->column_reverse ? 0 : 1350);
    uint32_t word = *data >> 5;
    while (budget && word < 675) {
        uint32_t end = *block < cfg->pilot_blocks ? (*block + 1u) * 45u : 675;
        if (end - word > budget) end = word + budget;
        budget -= end - word;
        const uint32_t shift = 2u * s->fill, reverse = 31u - shift;
        uint32_t carry_i = s->acc_i, carry_q = s->acc_q, n = s->n;
        do {
            const dvbs2_8psk_gold_t gold = cfg->gold[word];
            const uint32_t av = reverse32(a[word]), bv = reverse32(b[word]), cv = reverse32(c[word]);
            const uint32_t w = (av & (bv ^ cv ^ gold.r0)) ^ gold.r1;
            const uint32_t qa = bv ^ w, qb = cv ^ w ^ gold.r0, d = av ^ gold.r0;
            const uint32_t q0 = codes_zip16(qa, qb);
            const uint32_t i0 = q0 ^ (3u * codes_spread16(d)) ^ 0xaaaaaaaau;
            uint32_t index = n++ & s->mask;
            s->i[index] = carry_i | i0 << shift; s->q[index] = carry_q | q0 << shift;
            carry_i = (i0 >> reverse) >> 1; carry_q = (q0 >> reverse) >> 1;
            const uint32_t q1 = codes_zip16(qa >> 16, qb >> 16);
            const uint32_t i1 = q1 ^ (3u * codes_spread16(d >> 16)) ^ 0xaaaaaaaau;
            index = n++ & s->mask;
            s->i[index] = carry_i | i1 << shift; s->q[index] = carry_q | q1 << shift;
            carry_i = (i1 >> reverse) >> 1; carry_q = (q1 >> reverse) >> 1;
        } while (++word < end);
        s->acc_i = carry_i; s->acc_q = carry_q; s->n = n;
        if (*block < cfg->pilot_blocks && word == (*block + 1u) * 45u) {
            codes_pilot(cfg, s, (*block)++);
        }
    }
    *data = word << 5;
}

// This section is copied complete into the caller's arena on M33. It contains
// no calls, external data references or branch targets outside the section.
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
#define CODES_LEAF __attribute__((section("c4_mapper"), noinline, noclone, used))
#else
#define CODES_LEAF __attribute__((noinline))
#endif
static void CODES_LEAF codes_lut_run(const uint32_t *a, const uint32_t *b, const uint32_t *c,
                                     const dvbs2_8psk_gold_t *gold, const uint16_t *lut,
                                     dvbs2_8psk_codes_stream_t *s, uint32_t count) {
    const uint32_t shift = 2u * s->fill, reverse = 31u - shift, mask = s->mask;
    uint32_t *out_i = s->i, *out_q = s->q;
    uint32_t carry_i = s->acc_i, carry_q = s->acc_q, n = s->n, word = 0;
    do {
        const dvbs2_8psk_gold_t g = gold[word];
        const uint32_t av = reverse32(a[word]), bv = reverse32(b[word]), cv = reverse32(c[word]);
        const uint32_t w = (av & (bv ^ cv ^ g.r0)) ^ g.r1;
        const uint32_t qa = bv ^ w, qb = cv ^ w ^ g.r0, d = av ^ g.r0;
        const uint32_t q0 = codes_zip_lut(qa, qb, lut);
        const uint32_t i0 = q0 ^ (3u * codes_spread_lut(d, lut)) ^ 0xaaaaaaaau;
        uint32_t index = n++ & mask;
        out_i[index] = carry_i | i0 << shift; out_q[index] = carry_q | q0 << shift;
        carry_i = (i0 >> reverse) >> 1; carry_q = (q0 >> reverse) >> 1;
        const uint32_t q1 = codes_zip_lut(qa >> 16, qb >> 16, lut);
        const uint32_t i1 = q1 ^ (3u * codes_spread_lut(d >> 16, lut)) ^ 0xaaaaaaaau;
        index = n++ & mask;
        out_i[index] = carry_i | i1 << shift; out_q[index] = carry_q | q1 << shift;
        carry_i = (i1 >> reverse) >> 1; carry_q = (q1 >> reverse) >> 1;
    } while (++word < count);
    s->acc_i = carry_i; s->acc_q = carry_q; s->n = n;
}

// Twelve LUT reads per 32 symbols trade SRAM bandwidth against ALU work.
static void __attribute__((noinline)) codes_normal_fast(const dvbs2_8psk_cfg_t *cfg,
                                                       const uint32_t *frame,
                                                       dvbs2_8psk_codes_stream_t *s,
                                                       uint32_t *data, uint32_t *block,
                                                       uint32_t budget) {
    const uint32_t *a = frame + (cfg->column_reverse ? 1350 : 0), *b = frame + 675,
                   *c = frame + (cfg->column_reverse ? 0 : 1350);
    uint32_t word = *data >> 5;
    while (budget && word < 675) {
        uint32_t end = *block < cfg->pilot_blocks ? (*block + 1u) * 45u : 675;
        if (end - word > budget) end = word + budget;
        const uint32_t count = end - word;
        budget -= count;
        codes_run_t run;
        memcpy(&run, (const uint32_t *)cfg->codes_lut + 224, sizeof run);
        run(a + word, b + word, c + word, cfg->gold + word, cfg->codes_lut, s, count);
        word = end;
        if (*block < cfg->pilot_blocks && word == (*block + 1u) * 45u) {
            codes_pilot_fast(cfg, s, (*block)++);
        }
    }
    *data = word << 5;
}

void dvbs2_8psk_codes_begin(dvbs2_8psk_codes_state_t *state,
                          const dvbs2_8psk_cfg_t *cfg, const uint32_t *frame,
                          dvbs2_8psk_codes_stream_t *stream) {
    *state = (dvbs2_8psk_codes_state_t){.cfg = cfg, .frame = frame, .stream = stream};
}

bool dvbs2_8psk_codes_begin_fast(dvbs2_8psk_codes_state_t *state,
                               const dvbs2_8psk_cfg_t *cfg, const uint32_t *frame,
                               dvbs2_8psk_codes_stream_t *stream) {
    if (!cfg || !cfg->codes_lut) return false;
    dvbs2_8psk_codes_begin(state, cfg, frame, stream);
    state->fast = true;
    return true;
}

bool __attribute__((noinline)) dvbs2_8psk_codes_step(dvbs2_8psk_codes_state_t *state, uint32_t max_words) {
    const dvbs2_8psk_cfg_t *cfg = state->cfg;
    if (state->data == cfg->data_symbols) return true;
    if (!max_words) return false;
    dvbs2_8psk_codes_stream_t s = *state->stream;
    if (!state->header_done) {
        if (state->fast) codes_header_fast(cfg, &s);
        else codes_header(cfg, &s);
        state->header_done = true;
    }
    uint32_t data = state->data, block = state->pilot;
    if (!cfg->is_short) {
        if (state->fast) codes_normal_fast(cfg, state->frame, &s, &data, &block, max_words);
        else codes_normal(cfg, state->frame, &s, &data, &block, max_words);
        state->data = data; state->pilot = block; *state->stream = s;
        return data == cfg->data_symbols;
    }
    const uint32_t col0 = cfg->column_reverse ? 2u * cfg->data_symbols : 0;
    const uint32_t col2 = cfg->column_reverse ? 0 : 2u * cfg->data_symbols;
    while (max_words-- && data < cfg->data_symbols) {
        uint32_t count = cfg->data_symbols - data;
        if (count > 32) count = 32;
        if (cfg->pilots) {
            const uint32_t remaining = 1440u - data % 1440u;
            if (count > remaining) count = remaining;
        }
        codes_map_put(&s, column32(state->frame, col0 + data),
                       column32(state->frame, cfg->data_symbols + data),
                       column32(state->frame, col2 + data), gold32(cfg->gold, data), count);
        data += count;
        if (block < cfg->pilot_blocks && data == (block + 1u) * 1440u) {
            if (state->fast) codes_pilot_fast(cfg, &s, block++);
            else codes_pilot(cfg, &s, block++);
        }
    }
    state->data = data; state->pilot = block; *state->stream = s;
    return data == cfg->data_symbols;
}

void dvbs2_8psk_codes_frame(const dvbs2_8psk_cfg_t *cfg, const uint32_t *frame,
                           dvbs2_8psk_codes_stream_t *stream) {
    dvbs2_8psk_codes_state_t state;
    dvbs2_8psk_codes_begin(&state, cfg, frame, stream);
    while (!dvbs2_8psk_codes_step(&state, 64)) {}
}

void dvbs2_8psk_codes_flush(dvbs2_8psk_codes_stream_t *stream) {
    if (stream->fill) codes_put_small(stream, 0, 0xaaaaaaaau, 16u - stream->fill);
}
