#include "dvbs2.h"

#include <string.h>

#ifdef IQ_ON_DEVICE
#define HOT(f) __attribute__((section(".time_critical." #f))) f
#define HI __attribute__((section(".sram_hi")))                 // SRAM4-7, away from DMA banks
#define PROF(i) (dvbs2_prof[i] = *(volatile uint32_t *)0xE0001004u)   // DWT_CYCCNT
static inline uint32_t rbit(uint32_t x) { __asm("rbit %0, %1" : "=r"(x) : "r"(x)); return x; }
#else
#define HOT(f) f
#define HI
#define PROF(i) ((void)0)
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

uint32_t dvbs2_prof[8];
static const dvbs2_code_t *C;
static bool pilots;
static uint32_t M, bch_deg;          // parity bits: LDPC, BCH
static uint32_t bch_tab[4][256][BCH_W] HI, bch_g[BCH_W];  // T_k[b] = b(x) x^(deg+8k) mod g
static uint32_t bb_prbs[MAX_BITS_W] HI;
static uint16_t a_row[MAX_ADDR] HI, a_off[MAX_ADDR] HI;     // x mod q, 360 - x div q
static uint16_t grp[162 + 1];                         // group offsets, copied out of flash
static uint32_t P[MAX_Q + 32][12] HI;                   // q x 360 parity matrix (+ zero rows)
static uint32_t fe[MAX_BITS_W] HI;                      // FECFRAME under construction
static uint32_t r0[MAX_BODY_W] HI, r1[MAX_BODY_W] HI;      // PL scrambling R(i) bit planes (init)
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
    for (int k = 0; k < 4; k++)                                   // divide byte b then k zero bytes
        for (int b = 0; b < 256; b++) {
            uint32_t r[BCH_W] = {0};
            for (int i = 8 * k + 7; i >= 0; i--) {
                const uint32_t fb = (i >= 8 * k ? (b >> (i - 8 * k)) & 1 : 0) ^ (r[0] >> 31);
                shl(r, 1);
                if (fb)
                    for (int j = 0; j < BCH_W; j++) r[j] ^= bch_g[j];
            }
            memcpy(bch_tab[k][b], r, sizeof r);
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

// Slicing by 4: X = top32(R) ^ word; R <- (R << 32) ^ T3[X3] ^ T2[X2] ^ T1[X1] ^ T0[X0], since
// X(x) x^deg = sum_k X_k(x) x^(deg + 8k) mod g. Trailing bytes (Kbch mod 32 = 0, 8, 16 or 24 bits)
// use T0 one byte at a time: R <- (R << 8) ^ T0[top8(R) ^ byte].
void HOT(dvbs2_bch)(const uint32_t *m, uint32_t *parity) {
    uint32_t r0_ = 0, r1_ = 0, r2 = 0, r3 = 0, r4 = 0, r5 = 0;
    const uint32_t nw = C->kbch / 32u, tail = (C->kbch % 32u) / 8;
    const uint32_t(*t0)[BCH_W] = bch_tab[0], (*t1)[BCH_W] = bch_tab[1];
    const uint32_t(*t2)[BCH_W] = bch_tab[2], (*t3)[BCH_W] = bch_tab[3];
    for (uint32_t i = 0; i < nw; i++) {
        const uint32_t x = r0_ ^ m[i];
        const uint32_t *a = t3[x >> 24], *b = t2[(x >> 16) & 0xFF], *c = t1[(x >> 8) & 0xFF],
                       *d = t0[x & 0xFF];
        r0_ = r1_ ^ a[0] ^ b[0] ^ c[0] ^ d[0];
        r1_ = r2 ^ a[1] ^ b[1] ^ c[1] ^ d[1];
        r2 = r3 ^ a[2] ^ b[2] ^ c[2] ^ d[2];
        r3 = r4 ^ a[3] ^ b[3] ^ c[3] ^ d[3];
        r4 = r5 ^ a[4] ^ b[4] ^ c[4] ^ d[4];
        r5 = a[5] ^ b[5] ^ c[5] ^ d[5];
    }
    for (uint32_t i = 0; i < tail; i++) {
        const uint32_t *t = t0[(r0_ >> 24) ^ ((m[nw] >> (24 - 8 * i)) & 0xFF)];
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
    for (uint32_t k = 0; k < (C->kbch + 31u) / 32; k++) bb[k] ^= bb_prbs[k];
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
    PROF(0);
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
            const uint32_t *src = u2 + (a_off[a] >> 5), sh = a_off[a] & 31;
            if (!sh) {
                for (int k = 0; k < 12; k++) row[k] ^= src[k];
            } else {
                uint32_t lo = src[0];
                for (int k = 0; k < 12; k++) {
                    const uint32_t hi = src[k + 1];
                    row[k] ^= (lo << sh) | (hi >> (32 - sh));
                    lo = hi;
                }
            }
        }
    }
    PROF(1);
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

    PROF(2);
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
    PROF(3);
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

// PL scrambling of 16 symbols by R = 2 r1 + r0 (5.5.4): R=1 (-Q, I), R=2 (-I, -Q), R=3 (Q, -I).
// In sign bits: I' = (r0 ? bQ : bI) ^ (r0 ^ r1), Q' = (r0 ? bI : bQ) ^ r1. rm = r0 | r1 << 16.
static inline uint32_t scramble16(uint32_t w, uint32_t rm) {
    const uint32_t bi = w & 0xFFFFu, bq = w >> 16, a = rm & 0xFFFFu, c = rm >> 16;
    const uint32_t ni = ((a & bq) | (~a & bi)) ^ (a ^ c), nq = ((a & bi) | (~a & bq)) ^ c;
    return (ni & 0xFFFFu) | nq << 16;
}

void symstream_flush(symstream_t *s) {
    if (s->fill) symstream_put(s, 0, 16 - s->fill);
}

// Precomputed per code: scrambling masks for each 16-symbol data word (a data word never
// straddles a pilot block, since 1440 = 90 x 16) and the scrambled pilot blocks, as 16+16+4 symbols.
static uint32_t rmask[64800 / 32 + 1] HI, pil[22][3] HI;
static uint32_t n_pil;

static uint32_t plane16(const uint32_t *pl, uint32_t pos) {   // 16 bits of an R plane at pos
    const uint32_t i = pos >> 4, sh = pos & 15;
    return ((pl[i] >> sh) | (pl[i + 1] << (16 - sh))) & 0xFFFFu;
}

static void pl_setup(void) {
    const uint32_t s_slots = C->is_short ? 90 : 360;           // Table 11, QPSK
    n_pil = pilots ? (s_slots - 1) / 16 : 0;
    body_syms = s_slots * 90 + 36 * n_pil;
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
    const uint32_t nsym = C->nldpc / 2u;
    for (uint32_t j = 0; j * 16 < nsym; j++) {
        const uint32_t pos = 16 * j + (pilots ? 36 * (16 * j / 1440) : 0);
        rmask[j] = plane16(r0, pos) | plane16(r1, pos) << 16;
    }
    for (uint32_t k = 0; k < n_pil; k++)                       // pilots are (bI, bQ) = (0, 0)
        for (uint32_t w = 0; w < 3; w++) {
            const uint32_t pos = 1440 * (k + 1) + 36 * k + 16 * w;
            pil[k][w] = scramble16(0, plane16(r0, pos) | plane16(r1, pos) << 16);
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

void dvbs2_init(const dvbs2_code_t *c, bool pil_on) {
    C = c, pilots = pil_on, M = c->nldpc - c->kldpc;
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

// One BBFRAME to one PLFRAME appended to s. fe = [scrambled BBFRAME | BCH | LDPC parity].
void HOT(dvbs2_frame)(const uint32_t *bb, symstream_t *out) {
    const uint32_t kw = (C->kbch + 31u) / 32, nsym = C->nldpc / 2u;
    PROF(4);
    for (uint32_t k = 0; k < kw; k++) fe[k] = bb[k] ^ bb_prbs[k];
    if (C->kbch % 32u) fe[kw - 1] &= ~(~0u >> (C->kbch % 32u));
    memset(fe + kw, 0, sizeof fe - kw * 4);
    uint32_t par[BCH_W + 1];
    dvbs2_bch(fe, par);
    copy_bits(fe, C->kbch, par, bch_deg);
    PROF(5);
    static uint32_t lpar[MAX_BITS_W] HI;
    dvbs2_ldpc(fe, lpar);
    copy_bits(fe, C->kldpc, lpar, M);
    PROF(6);

    symstream_t s = *out;                                      // local copy keeps state in registers
    for (int k = 0; k < 6; k++) symstream_put(&s, hdr[k], k < 5 ? 16 : 10);
    for (uint32_t j = 0, sym = 0; sym < nsym; j++, sym += 16) {
        const uint32_t n = nsym - sym < 16 ? nsym - sym : 16;
        symstream_put(&s, scramble16(map16(fe[j]), rmask[j]), n);
        if (n_pil && (sym + 16) % 1440 == 0 && sym + 16 < nsym) {
            const uint32_t *p = pil[(sym + 16) / 1440 - 1];
            symstream_put(&s, p[0], 16), symstream_put(&s, p[1], 16), symstream_put(&s, p[2], 4);
        }
    }
    *out = s;
    PROF(7);
}
