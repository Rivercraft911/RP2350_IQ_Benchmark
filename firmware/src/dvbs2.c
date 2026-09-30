#include "dvbs2.h"

#include <string.h>

#ifdef IQ_ON_DEVICE
#define HOT(f) __attribute__((section(".time_critical." #f))) f
static inline uint32_t rbit(uint32_t x) { __asm("rbit %0, %1" : "=r"(x) : "r"(x)); return x; }
#else
#define HOT(f) f
static inline uint32_t rbit(uint32_t x) {
    x = ((x >> 1) & 0x55555555u) | ((x & 0x55555555u) << 1);
    x = ((x >> 2) & 0x33333333u) | ((x & 0x33333333u) << 2);
    x = ((x >> 4) & 0x0F0F0F0Fu) | ((x & 0x0F0F0F0Fu) << 4);
    x = ((x >> 8) & 0x00FF00FFu) | ((x & 0x00FF00FFu) << 8);
    return (x >> 16) | (x << 16);
}
#endif

enum {
    MAX_BITS_W = 64800 / 32 + 2,     // FECFRAME words, plus padding for 64-bit reads
    MAX_Q = 135,                     // normal 1/4
    MAX_ADDR = 2048,
    MAX_BODY_W = (33282 - 90) / 16 + 2,
    BCH_W = 6,                       // 192-bit remainder
};

static const dvbs2_code_t *C;
static bool pilots;
static uint32_t M, bch_deg;          // parity bits: LDPC, BCH
static uint32_t bch_tab[256][BCH_W], bch_g[BCH_W];   // T[b] = b(x) x^deg mod g; g without x^deg
static uint32_t bb_prbs[MAX_BITS_W];
static uint16_t a_row[MAX_ADDR], a_off[MAX_ADDR];     // x mod q, 360 - x div q
static uint16_t grp[162 + 1];                         // group offsets, copied out of flash
static uint32_t P[MAX_Q + 32][12];                   // q x 360 parity matrix (+ zero rows)
static uint32_t fe[MAX_BITS_W];                      // FECFRAME under construction
static uint32_t body[MAX_BODY_W];                    // post-header symbols, shaper format
static uint32_t r0[MAX_BODY_W], r1[MAX_BODY_W];      // PL scrambling R(i) bit planes
static uint32_t hdr[6], body_syms;

// ------------------------------------------------------------------ bit-vector helpers

static inline uint32_t get32(const uint32_t *a, uint32_t bit) {   // a must have 1 word slack
    const uint32_t i = bit >> 5, s = bit & 31;
    return (uint32_t)((((uint64_t)a[i] << 32) | a[i + 1]) >> (32 - s));
}

static inline void or_bits(uint32_t *a, uint32_t bit, uint32_t v, uint32_t n) {  // v MSB-aligned
    const uint32_t i = bit >> 5, s = bit & 31;
    a[i] |= v >> s;
    if (s + n > 32) a[i + 1] |= v << (32 - s);
}

static void copy_bits(uint32_t *dst, uint32_t dbit, const uint32_t *src, uint32_t n) {
    for (uint32_t k = 0; k < n; k += 32) {
        const uint32_t len = n - k < 32 ? n - k : 32;
        const uint32_t v = src[k >> 5] & (len == 32 ? ~0u : ~(~0u >> len));
        or_bits(dst, dbit + k, v, len);
    }
}

static inline uint32_t bit_at(const uint32_t *a, uint32_t i) { return (a[i >> 5] >> (31 - (i & 31))) & 1; }

// ------------------------------------------------------------------ BCH (5.3.1)

// Minimal polynomials, Table 6a (normal, degree 16) and 6b (short, degree 14): exponents.
static const uint8_t G16[12][14] = {
    {0, 2, 3, 5, 16}, {0, 1, 4, 5, 6, 8, 16}, {0, 2, 3, 4, 5, 7, 8, 9, 10, 11, 16},
    {0, 2, 4, 6, 9, 11, 12, 14, 16}, {0, 1, 2, 3, 5, 8, 9, 10, 11, 12, 16},
    {0, 2, 4, 5, 7, 8, 9, 10, 12, 13, 14, 15, 16}, {0, 2, 5, 6, 8, 9, 10, 11, 13, 15, 16},
    {0, 1, 2, 5, 6, 8, 9, 12, 13, 14, 16}, {0, 5, 7, 9, 10, 11, 16},
    {0, 1, 2, 5, 7, 8, 10, 12, 13, 14, 16}, {0, 2, 3, 5, 9, 11, 12, 13, 16},
    {0, 1, 5, 6, 7, 9, 11, 12, 16}};
static const uint8_t G14[12][12] = {
    {0, 1, 3, 5, 14}, {0, 6, 8, 11, 14}, {0, 1, 2, 6, 9, 10, 14}, {0, 4, 7, 8, 10, 12, 14},
    {0, 2, 4, 6, 8, 9, 11, 13, 14}, {0, 3, 7, 8, 9, 13, 14}, {0, 2, 5, 6, 7, 10, 11, 13, 14},
    {0, 5, 8, 9, 10, 11, 14}, {0, 1, 2, 3, 9, 10, 14}, {0, 3, 6, 9, 11, 12, 14},
    {0, 4, 11, 12, 14}, {0, 1, 2, 3, 5, 6, 7, 8, 10, 13, 14}};

// Shift an MSB-first register of BCH_W words left by n (< 32) bits.
static inline void shl(uint32_t *r, uint32_t n) {
    for (int k = 0; k < BCH_W - 1; k++) r[k] = (r[k] << n) | (r[k + 1] >> (32 - n));
    r[BCH_W - 1] <<= n;
}

static void bch_setup(void) {
    // g(x) = product of the first t minimal polynomials, as LSB-first coefficient bits.
    uint32_t g[7] = {1}, tmp[7];
    const int d = C->is_short ? 14 : 16;
    for (int k = 0; k < C->t; k++) {
        const uint8_t *e = C->is_short ? G14[k] : G16[k];
        memset(tmp, 0, sizeof tmp);
        for (int j = 0; j == 0 || e[j]; j++)                       // tmp = g * g_k
            for (int b = 0; b + e[j] < 7 * 32; b++)
                if ((g[b >> 5] >> (b & 31)) & 1) tmp[(b + e[j]) >> 5] ^= 1u << ((b + e[j]) & 31);
        memcpy(g, tmp, sizeof g);
    }
    bch_deg = (uint32_t)(d * C->t);
    memset(bch_g, 0, sizeof bch_g);                               // MSB-first, x^deg dropped
    for (uint32_t k = 0; k < bch_deg; k++)
        if ((g[k >> 5] >> (k & 31)) & 1) {
            const uint32_t pos = bch_deg - 1 - k;
            bch_g[pos >> 5] |= 1u << (31 - (pos & 31));
        }
    for (int b = 0; b < 256; b++) {                               // bit-serial division of b
        uint32_t r[BCH_W] = {0};
        for (int i = 7; i >= 0; i--) {
            const uint32_t fb = ((b >> i) & 1) ^ (r[0] >> 31);
            shl(r, 1);
            if (fb)
                for (int k = 0; k < BCH_W; k++) r[k] ^= bch_g[k];
        }
        memcpy(bch_tab[b], r, sizeof r);
    }
}

void dvbs2_bch_serial(const uint32_t *m, uint32_t *parity) {
    uint32_t r[BCH_W] = {0};
    for (uint32_t i = 0; i < C->kbch; i++) {
        const uint32_t fb = bit_at(m, i) ^ (r[0] >> 31);
        shl(r, 1);
        if (fb)
            for (int k = 0; k < BCH_W; k++) r[k] ^= bch_g[k];
    }
    memcpy(parity, r, sizeof r);
}

// Byte-wise: R <- (R << 8) ^ T[top8(R) ^ byte]. kbch is a multiple of 8 for every code.
void HOT(dvbs2_bch)(const uint32_t *m, uint32_t *parity) {
    uint32_t r0_ = 0, r1_ = 0, r2 = 0, r3 = 0, r4 = 0, r5 = 0;
    const uint32_t nbytes = C->kbch / 8;
    for (uint32_t i = 0; i < nbytes; i++) {
        const uint32_t byte = (m[i >> 2] >> (24 - 8 * (i & 3))) & 0xFF;
        const uint32_t *t = bch_tab[(r0_ >> 24) ^ byte];
        r0_ = ((r0_ << 8) | (r1_ >> 24)) ^ t[0];
        r1_ = ((r1_ << 8) | (r2 >> 24)) ^ t[1];
        r2 = ((r2 << 8) | (r3 >> 24)) ^ t[2];
        r3 = ((r3 << 8) | (r4 >> 24)) ^ t[3];
        r4 = ((r4 << 8) | (r5 >> 24)) ^ t[4];
        r5 = (r5 << 8) ^ t[5];
    }
    parity[0] = r0_, parity[1] = r1_, parity[2] = r2, parity[3] = r3, parity[4] = r4, parity[5] = r5;
}

// ------------------------------------------------------------------ BB scrambling (5.2.2)

void HOT(dvbs2_bbscramble)(uint32_t *bb) {
    for (uint32_t k = 0; k < (C->kbch + 31) / 32; k++) bb[k] ^= bb_prbs[k];
}

// ------------------------------------------------------------------ LDPC (5.3.2)

void dvbs2_ldpc_serial(const uint32_t *info, uint32_t *parity) {
    const uint32_t q = C->q;
    memset(parity, 0, (M + 63) / 32 * 4);
    for (uint32_t i = 0; i < C->kldpc; i++) {
        if (!bit_at(info, i)) continue;
        const uint32_t g = i / 360, m = i % 360;
        for (uint32_t a = C->grp[g]; a < C->grp[g + 1]; a++) {
            const uint32_t p = (C->addr[a] + m * q) % M;
            parity[p >> 5] ^= 1u << (31 - (p & 31));
        }
    }
    for (uint32_t i = 1; i < M; i++)
        if (bit_at(parity, i - 1)) parity[i >> 5] ^= 1u << (31 - (i & 31));
}

// 32x32 bit-matrix transpose, MSB-first rows (Hacker's Delight, 2nd ed., fig. 7-6).
static void HOT(transpose32)(uint32_t *a) {
    uint32_t m = 0x0000FFFFu;
    for (uint32_t j = 16; j; j >>= 1, m ^= m << j)
        for (uint32_t k = 0; k < 32; k = (k + j + 1) & ~j) {
            const uint32_t t = (a[k] ^ (a[k + j] >> j)) & m;
            a[k] ^= t;
            a[k + j] ^= t << j;
        }
}

// Group form. With M = 360 q, address x for bit m of group g is (x + m q) mod M. Writing
// x = r + c q gives row r and column (c + m) mod 360 of a q x 360 matrix P (parity index
// i = r + column * q), so each table entry XORs the group's 360 bits, rotated right by c, into
// row r. The accumulation p_i ^= p_{i-1} in natural order is a cumulative XOR down the rows
// plus an exclusive prefix over whole columns. A 32x32 transpose then restores natural order.
void HOT(dvbs2_ldpc)(const uint32_t *info, uint32_t *parity) {
    const uint32_t q = C->q;
    memset(P, 0, sizeof P);
    for (uint32_t g = 0; g < C->n_groups; g++) {
        uint32_t u[13], u2[25];
        for (int k = 0; k < 12; k++) u[k] = get32(info, g * 360 + 32 * k);
        u[11] &= 0xFF000000u;                                  // 360 = 11 * 32 + 8
        u[12] = 0;
        for (int k = 0; k < 11; k++) u2[k] = u[k];
        for (int k = 11; k < 25; k++)                          // u2 = u || u (bit 360 on)
            u2[k] = (k < 12 ? u[k] : 0) | (k - 11 < 13 ? u[k - 11] >> 8 : 0) |
                    (k >= 12 && k - 12 < 13 ? u[k - 12] << 24 : 0);
        for (uint32_t a = grp[g]; a < grp[g + 1]; a++) {
            uint32_t *row = P[a_row[a]];
            const uint32_t off = a_off[a];
            for (int k = 0; k < 12; k++) row[k] ^= get32(u2, off + 32 * k);
        }
    }
    for (uint32_t r = 1; r < q; r++)
        for (int k = 0; k < 12; k++) P[r][k] ^= P[r - 1][k];
    uint32_t cx[13], carry = 0;                                // exclusive column prefix
    for (int k = 0; k < 12; k++) {
        uint32_t x = P[q - 1][k] & (k == 11 ? 0xFF000000u : ~0u);
        x ^= x >> 1, x ^= x >> 2, x ^= x >> 4, x ^= x >> 8, x ^= x >> 16;
        if (carry) x = ~x;
        cx[k] = x;
        carry = x & 1;
    }
    for (int k = 11; k >= 0; k--) cx[k] = (cx[k] >> 1) | (k ? cx[k - 1] << 31 : 0);
    for (uint32_t r = 0; r < q; r++)
        for (int k = 0; k < 12; k++) P[r][k] ^= cx[k];
    for (uint32_t r = q; r < q + 32; r++) memset(P[r], 0, sizeof P[r]);

    memset(parity, 0, (M + 63) / 32 * 4);
    for (uint32_t rb = 0; rb < q; rb += 32) {
        const uint32_t n = q - rb < 32 ? q - rb : 32;
        for (int k = 0; k < 12; k++) {
            uint32_t blk[32];
            for (int j = 0; j < 32; j++) blk[j] = P[rb + j][k];
            transpose32(blk);                                  // blk[m] = column 32k+m
            for (int mm = 0; mm < 32 && 32 * k + mm < 360; mm++)
                or_bits(parity, (32 * k + mm) * q + rb, blk[mm], n);
        }
    }
}

// ------------------------------------------------------------------ mapping and PL framing

// FECFRAME word (MSB first: I0 Q0 I1 Q1 ... I15 Q15) -> shaper word (I_j at bit j, Q_j at
// bit 16+j): bit-reverse, then outer unshuffle (even bits to [15:0], odd bits to [31:16]).
static inline uint32_t map16(uint32_t w) {
    uint32_t x = rbit(w), t;
    t = (x ^ (x >> 1)) & 0x22222222u, x ^= t ^ (t << 1);
    t = (x ^ (x >> 2)) & 0x0C0C0C0Cu, x ^= t ^ (t << 2);
    t = (x ^ (x >> 4)) & 0x00F000F0u, x ^= t ^ (t << 4);
    t = (x ^ (x >> 8)) & 0x0000FF00u, x ^= t ^ (t << 8);
    return x;
}

void HOT(symstream_put)(symstream_t *s, uint32_t w, uint32_t nsym) {
    const uint32_t mask = nsym == 16 ? 0xFFFFu : (1u << nsym) - 1;
    s->acc_i |= (uint64_t)(w & mask) << s->fill;
    s->acc_q |= (uint64_t)((w >> 16) & mask) << s->fill;
    s->fill += nsym;
    if (s->fill >= 16) {
        s->out[s->n++] = (uint32_t)(s->acc_i & 0xFFFF) | (uint32_t)(s->acc_q & 0xFFFF) << 16;
        s->acc_i >>= 16, s->acc_q >>= 16, s->fill -= 16;
    }
}

void symstream_flush(symstream_t *s) {
    if (s->fill) symstream_put(s, 0, 16 - s->fill);
}

static void pl_setup(void) {
    const uint32_t s_slots = C->is_short ? 90 : 360;           // Table 11, QPSK
    const uint32_t npil = pilots ? (s_slots - 1) / 16 : 0;
    body_syms = s_slots * 90 + 36 * npil;
    // Gold sequence n = 0 (5.5.4): z(i) = x(i) + y(i); R(i) = 2 z(i + 131072) + z(i).
    uint32_t xa = 1, ya = 0x3FFFF, xb = 1, yb = 0x3FFFF;       // bit k = x(i + k)
    for (uint32_t i = 0; i < 131072; i++) {
        xb = (xb >> 1) | ((((xb >> 7) ^ xb) & 1) << 17);
        yb = (yb >> 1) | ((((yb >> 10) ^ (yb >> 7) ^ (yb >> 5) ^ yb) & 1) << 17);
    }
    memset(r0, 0, sizeof r0), memset(r1, 0, sizeof r1);
    for (uint32_t i = 0; i < body_syms; i++) {
        r0[i >> 4] |= ((xa ^ ya) & 1) << (i & 15);
        r1[i >> 4] |= ((xb ^ yb) & 1) << (i & 15);
        xa = (xa >> 1) | ((((xa >> 7) ^ xa) & 1) << 17);
        ya = (ya >> 1) | ((((ya >> 10) ^ (ya >> 7) ^ (ya >> 5) ^ ya) & 1) << 17);
        xb = (xb >> 1) | ((((xb >> 7) ^ xb) & 1) << 17);
        yb = (yb >> 1) | ((((yb >> 10) ^ (yb >> 7) ^ (yb >> 5) ^ yb) & 1) << 17);
    }
    // PLHEADER (5.5.2): SOF 0x18D2E82, then PLS code; pi/2-BPSK onto QPSK points:
    // odd symbols (1-based) bI = bQ = y, even symbols bI = !y, bQ = y.
    const uint32_t b = (uint32_t)C->modcod << 2 | (uint32_t)C->is_short << 1 | pilots;  // b1..b7
    static const uint32_t G[6] = {0x55555555u, 0x33333333u, 0x0F0F0F0Fu,
                                  0x00FF00FFu, 0x0000FFFFu, 0xFFFFFFFFu};
    uint32_t y = 0;
    for (int k = 0; k < 6; k++)
        if ((b >> (6 - k)) & 1) y ^= G[k];
    uint64_t pls = 0;
    for (int k = 0; k < 32; k++) {
        const uint64_t yk = (y >> (31 - k)) & 1;
        pls |= yk << (63 - 2 * k) | (yk ^ (b & 1)) << (62 - 2 * k);
    }
    pls ^= 0x719D83C953422DFAull;
    memset(hdr, 0, sizeof hdr);
    for (int k = 0; k < 90; k++) {
        const uint32_t bitv = k < 26 ? (0x18D2E82u >> (25 - k)) & 1 : (uint32_t)(pls >> (63 - (k - 26))) & 1;
        const uint32_t bi = (k & 1) ? !bitv : bitv;            // k even = 1-based odd symbol
        hdr[k >> 4] |= bi << (k & 15) | bitv << (16 + (k & 15));
    }
}

uint32_t dvbs2_plframe_symbols(void) { return 90 + body_syms; }

void dvbs2_init(const dvbs2_code_t *c, bool pil) {
    C = c, pilots = pil, M = c->nldpc - c->kldpc;
    bch_setup();
    memcpy(grp, c->grp, (c->n_groups + 1u) * sizeof grp[0]);
    for (uint32_t a = 0; a < c->grp[c->n_groups]; a++) {
        a_row[a] = c->addr[a] % c->q;
        a_off[a] = (uint16_t)(360 - c->addr[a] / c->q);
    }
    uint32_t sr = 0x4A80;                                      // 100101010000000 (5.2.2)
    memset(bb_prbs, 0, sizeof bb_prbs);
    for (uint32_t i = 0; i < c->kbch; i++) {
        const uint32_t bit = (sr ^ (sr >> 1)) & 1;
        sr = (sr >> 1) | (bit << 14);
        bb_prbs[i >> 5] |= bit << (31 - (i & 31));
    }
    pl_setup();
}

void HOT(dvbs2_frame)(uint32_t *bb, symstream_t *s) {
    const uint32_t nw = (C->nldpc + 31) / 32;
    dvbs2_bbscramble(bb);
    memset(fe, 0, sizeof fe);
    memcpy(fe, bb, (C->kbch + 31) / 32 * 4);
    uint32_t par[BCH_W + 1];
    dvbs2_bch(bb, par);
    copy_bits(fe, C->kbch, par, bch_deg);
    static uint32_t lpar[MAX_BITS_W];
    dvbs2_ldpc(fe, lpar);
    copy_bits(fe, C->kldpc, lpar, M);

    // Body: data symbols with pilot blocks after every 16 slots, then PL scrambling.
    symstream_t b = {.out = body};
    const uint32_t nsym = C->nldpc / 2;
    for (uint32_t w = 0, sym = 0; w < nw; w++, sym += 16) {
        symstream_put(&b, map16(fe[w]), nsym - sym < 16 ? nsym - sym : 16);
        if (pilots && (sym + 16) % 1440 == 0 && sym + 16 < nsym) {
            symstream_put(&b, 0, 16), symstream_put(&b, 0, 16), symstream_put(&b, 0, 4);
        }
    }
    symstream_flush(&b);
    for (uint32_t k = 0; k < (body_syms + 15) / 16; k++) {
        const uint32_t w = body[k], bi = w & 0xFFFF, bq = w >> 16, a = r0[k], c = r1[k];
        const uint32_t ni = ((a & bq) | (~a & bi)) ^ (a ^ c);   // R: 1 (-Q, I) 2 (-I,-Q) 3 (Q,-I)
        const uint32_t nq = ((a & bi) | (~a & bq)) ^ c;
        body[k] = (ni & 0xFFFF) | nq << 16;
    }
    for (int k = 0; k < 6; k++) symstream_put(s, hdr[k], k < 5 ? 16 : 10);
    for (uint32_t k = 0, left = body_syms; left; k++) {
        const uint32_t n = left < 16 ? left : 16;
        symstream_put(s, body[k], n);
        left -= n;
    }
}
