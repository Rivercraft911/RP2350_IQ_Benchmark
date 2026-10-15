#include "dvbs2.h"

#include <stddef.h>
#include <string.h>

#ifdef IQ_ON_DEVICE
#define HOT(f) __attribute__((section(".time_critical." #f))) f
#define HI __attribute__((section(".sram_hi")))                 // SRAM4-7, away from DMA banks
#define PROF(i) (dvbs2_prof[i] = *(volatile uint32_t *)0xE0001004u)   // DWT_CYCCNT
#define COMPACT __attribute__((optimize("O2", "no-unroll-loops", "no-peel-loops")))  // GCC only
static inline uint32_t rbit(uint32_t x) { __asm("rbit %0, %1" : "=r"(x) : "r"(x)); return x; }
#else
#define HOT(f) f
#define HI
#define PROF(i) ((void)0)
#define COMPACT
static inline uint32_t rbit(uint32_t x) {
    x = ((x >> 1) & 0x55555555u) | ((x & 0x55555555u) << 1);
    x = ((x >> 2) & 0x33333333u) | ((x & 0x33333333u) << 2);
    x = ((x >> 4) & 0x0F0F0F0Fu) | ((x & 0x0F0F0F0Fu) << 4);
    x = ((x >> 8) & 0x00FF00FFu) | ((x & 0x00FF00FFu) << 8);
    return (x >> 16) | (x << 16);
}
#endif

enum {
    MAX_BITS_W = DVBS2_FE_WORDS,     // FECFRAME words, plus padding for 64-bit reads
    MAX_Q = 135,                     // normal 1/4
    MAX_ADDR = 2048,
    MAX_BODY_W = (33282 - 90) / 16 + 2,
    BCH_W = 6,                       // 192-bit remainder
};

uint32_t dvbs2_prof[8];
static const dvbs2_code_t *C;
static bool pilots;
static uint32_t M, bch_deg;          // parity bits: LDPC, BCH
static uint32_t bch_tab[4][256][8] HI, bch_g[BCH_W];      // T_k[b] = b(x) x^(deg+8k) mod g; 8-word rows
static uint32_t bb_prbs[MAX_BITS_W] HI;
static uint16_t a_row[MAX_ADDR] HI, a_off[MAX_ADDR] HI;     // x mod q, 360 - x div q
static uint16_t grp[162 + 1];                         // group offsets, copied out of flash
static uint32_t P[MAX_Q + 32][12] HI;                   // q x 360 parity matrix (+ zero rows)
static uint32_t fe[MAX_BITS_W] HI;                      // FECFRAME under construction
static uint32_t r0[MAX_BODY_W] HI, r1[MAX_BODY_W] HI;      // PL scrambling R(i) bit planes (init)
static uint32_t hdr[6], body_syms;
static dvbs2_bch_kernel_t bch_kernel = S2_BCH_BASELINE;
static unsigned map_kernel, ldpc_pack, frontend_bank;

// ------------------------------------------------------------------ bit-vector helpers

static inline void or_bits(uint32_t *a, uint32_t bit, uint32_t v, uint32_t n) {  // v MSB-aligned
    const uint32_t i = bit >> 5, s = bit & 31;
    a[i] |= v >> s;
    if (s + n > 32) a[i + 1] |= v << (32 - s);
}

static void copy_bits(uint32_t *dst, uint32_t dbit, const uint32_t *src, uint32_t n) {
    if (!(dbit & 31)) {                                        // word-aligned destination
        uint32_t *d = dst + (dbit >> 5);
        for (uint32_t k = 0; k < n / 32; k++) d[k] = src[k];
        if (n & 31) d[n / 32] |= src[n / 32] & ~(~0u >> (n & 31));
        return;
    }
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
            memcpy(bch_tab[k][b], r, sizeof r);                  // words 6, 7 are padding
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
static void HOT(bch_baseline)(const uint32_t *m, uint32_t *parity) {
    uint32_t r0_ = 0, r1_ = 0, r2 = 0, r3 = 0, r4 = 0, r5 = 0;
    const uint32_t nw = C->kbch / 32u, tail = (C->kbch % 32u) / 8;
    const uint32_t(*t0)[8] = bch_tab[0], (*t1)[8] = bch_tab[1];
    const uint32_t(*t2)[8] = bch_tab[2], (*t3)[8] = bch_tab[3];
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

// A degree-160 remainder has five live words; the sixth word in the general kernel is always
// zero. Keep the padded table layout so this experiment consumes no extra table SRAM.
static void HOT(bch160_words)(const uint32_t *m, uint32_t *parity) {
    uint32_t r0_ = 0, r1_ = 0, r2 = 0, r3 = 0, r4 = 0;
    const uint32_t nw = C->kbch / 32u, tail = (C->kbch % 32u) / 8;
    for (uint32_t i = 0; i < nw; i++) {
        const uint32_t x = r0_ ^ m[i];
        const uint32_t *a = bch_tab[3][x >> 24], *b = bch_tab[2][(x >> 16) & 0xFF],
                       *c = bch_tab[1][(x >> 8) & 0xFF], *d = bch_tab[0][x & 0xFF];
        r0_ = r1_ ^ a[0] ^ b[0] ^ c[0] ^ d[0];
        r1_ = r2 ^ a[1] ^ b[1] ^ c[1] ^ d[1];
        r2 = r3 ^ a[2] ^ b[2] ^ c[2] ^ d[2];
        r3 = r4 ^ a[3] ^ b[3] ^ c[3] ^ d[3];
        r4 = a[4] ^ b[4] ^ c[4] ^ d[4];
    }
    for (uint32_t i = 0; i < tail; i++) {
        const uint32_t *t = bch_tab[0][(r0_ >> 24) ^ ((m[nw] >> (24 - 8 * i)) & 0xFF)];
        r0_ = ((r0_ << 8) | (r1_ >> 24)) ^ t[0];
        r1_ = ((r1_ << 8) | (r2 >> 24)) ^ t[1];
        r2 = ((r2 << 8) | (r3 >> 24)) ^ t[2];
        r3 = ((r3 << 8) | (r4 >> 24)) ^ t[3];
        r4 = (r4 << 8) ^ t[4];
    }
    parity[0] = r0_, parity[1] = r1_, parity[2] = r2, parity[3] = r3, parity[4] = r4;
    parity[5] = 0;
}

#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
// Process complete input words with five remainder registers and four table pointers.
// r1 is scratch inside the loop (the output pointer is saved once). Alternating r1/lr loads
// separates each table load from its dependent XOR; there are no inner-loop stack spills.
// Arguments: input, six-word output, table base, input word count. All AAPCS callee-saved
// registers are restored. Remaining bytes are handled by the caller using the same T0 table.
void dvbs2_bch160_stream(const uint32_t *m, uint32_t *parity, const void *tab, uint32_t nw);
void dvbs2_bch160_continue(const uint32_t *m, uint32_t *parity, const void *tab, uint32_t nw);
void dvbs2_bch160_continue_y(const uint32_t *m, uint32_t *parity, const void *tab, uint32_t nw);
#endif

bool dvbs2_frontend_set_bank(unsigned bank) {
    if (bank > 1) return false;
    frontend_bank = bank;
    return true;
}

unsigned dvbs2_frontend_get_bank(void) { return frontend_bank; }

bool dvbs2_bch_set_kernel(dvbs2_bch_kernel_t kernel) {
    if ((unsigned)kernel > S2_BCH_STREAM_ARM) return false;
    bch_kernel = kernel;
    return true;
}

dvbs2_bch_kernel_t dvbs2_bch_get_kernel(void) { return bch_kernel; }

void HOT(dvbs2_bch)(const uint32_t *m, uint32_t *parity) {
    if (bch_kernel == S2_BCH_BASELINE || bch_deg != 160) {
        bch_baseline(m, parity);
        return;
    }
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    if (bch_kernel == S2_BCH_STREAM_ARM) {
        const uint32_t nw = C->kbch / 32u, tail = (C->kbch % 32u) / 8;
        dvbs2_bch160_stream(m, parity, bch_tab, nw);
        for (uint32_t i = 0; i < tail; i++) {
            const uint32_t *t = bch_tab[0][(parity[0] >> 24) ^ ((m[nw] >> (24 - 8 * i)) & 0xFF)];
            for (int k = 0; k < 4; k++)
                parity[k] = ((parity[k] << 8) | (parity[k + 1] >> 24)) ^ t[k];
            parity[4] = (parity[4] << 8) ^ t[4];
        }
        return;
    }
#endif
    bch160_words(m, parity);
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

// 32×32 bit-matrix transpose, MSB-first rows (Hacker's Delight, fig. 7-6). Fuse stages 16/8
// in four-register tiles and stages 4/2/1 in eight-register tiles: each matrix word is loaded
// and stored twice, instead of once for each of five stages. The Arm leaf fits core 1's
// scratch bank, avoiding instruction fetches through the output DMA's striped SRAM banks.
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
void dvbs2_transpose32(uint32_t *a);
#define transpose32 dvbs2_transpose32
#else
#define TB(A, B, SHIFT, MASK) do { \
    const uint32_t t = ((A) ^ ((B) >> (SHIFT))) & (MASK); \
    (A) ^= t; (B) ^= t << (SHIFT); \
} while (0)
static void __attribute__((noinline)) transpose32(uint32_t *a) {
    for (unsigned k = 0; k < 8; k++) {
        uint32_t x = a[k], y = a[k + 8], z = a[k + 16], w = a[k + 24];
        TB(x, z, 16, 0x0000ffffu); TB(y, w, 16, 0x0000ffffu);
        TB(x, y, 8, 0x00ff00ffu); TB(z, w, 8, 0x00ff00ffu);
        a[k] = x; a[k + 8] = y; a[k + 16] = z; a[k + 24] = w;
    }
    for (unsigned k = 0; k < 32; k += 8) {
        uint32_t v[8];
        memcpy(v, a + k, sizeof v);
        TB(v[0], v[4], 4, 0x0f0f0f0fu); TB(v[1], v[5], 4, 0x0f0f0f0fu);
        TB(v[2], v[6], 4, 0x0f0f0f0fu); TB(v[3], v[7], 4, 0x0f0f0f0fu);
        TB(v[0], v[2], 2, 0x33333333u); TB(v[1], v[3], 2, 0x33333333u);
        TB(v[4], v[6], 2, 0x33333333u); TB(v[5], v[7], 2, 0x33333333u);
        TB(v[0], v[1], 1, 0x55555555u); TB(v[2], v[3], 1, 0x55555555u);
        TB(v[4], v[5], 1, 0x55555555u); TB(v[6], v[7], 1, 0x55555555u);
        memcpy(a + k, v, sizeof v);
    }
}
#undef TB
#endif

// Normal 2/3 has q = 60: each transposed column contributes 60 consecutive parity bits.
// The unused tail of the worst-case P workspace can hold 360 pairs of words while
// the live rows and final padded transpose tile remain intact below that workspace.
// Packing groups of eight columns produces 15 complete words, with no scattered OR stores.
static void HOT(ldpc_pack60)(uint32_t *parity) {
    uint32_t (*columns)[2] = (uint32_t (*)[2])(P + 60 + 32);
    for (unsigned block = 0; block < 2; block++) {
        for (unsigned k = 0; k < 12; k++) {
            uint32_t bits[32];
            for (unsigned row = 0; row < 32; row++) bits[row] = P[32 * block + row][k];
            transpose32(bits);
            const unsigned n = k == 11 ? 8 : 32;
            for (unsigned col = 0; col < n; col++) columns[32 * k + col][block] = bits[col];
        }
    }
    uint32_t *out = parity;
    for (unsigned col = 0; col < 360; col += 8) {
        *out++ = columns[col][0];
        uint32_t carry = columns[col][1];
        _Pragma("GCC unroll 7")
        for (unsigned j = 1; j < 8; j++) {
            const uint32_t lo = columns[col + j][0], hi = columns[col + j][1];
            const unsigned shift = 4 * j;
            *out++ = carry | (lo >> (32 - shift));
            *out++ = (lo << shift) | (hi >> (32 - shift));
            carry = hi << shift;
        }
    }
    parity[21600 / 32] = 0; // preserve the generic parity buffer's extra zero guard word
}

// row[0..11] ^= 12 words of src starting sh bits in (0 <= sh < 32).
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
// Streaming order in asm: GCC otherwise hoists all 24 loads and spills (measured 2x slower).
#define XW(K, LO, HI)                                                                      \
    "ldr  %[" #HI "], [%[src], #(4*" #K "+4)]\n"                                            \
    "lsl  %[t], %[" #LO "], %[sh]\n"                                                        \
    "lsr  %[u], %[" #HI "], %[rs]\n"                                                        \
    "orr  %[t], %[t], %[u]\n"                                                               \
    "ldr  %[u], [%[row], #(4*" #K ")]\n"                                                    \
    "eor  %[u], %[u], %[t]\n"                                                               \
    "str  %[u], [%[row], #(4*" #K ")]\n"
static inline void xor_rot(uint32_t *row, const uint32_t *src, uint32_t sh) {
    if (!sh) {
        for (int k = 0; k < 12; k++) row[k] ^= src[k];
        return;
    }
    uint32_t a, b, t, u;
    __asm volatile("ldr %[a], [%[src]]\n" XW(0, a, b) XW(1, b, a) XW(2, a, b) XW(3, b, a)
                   XW(4, a, b) XW(5, b, a) XW(6, a, b) XW(7, b, a) XW(8, a, b) XW(9, b, a)
                   XW(10, a, b) XW(11, b, a)
                   : [a] "=&r"(a), [b] "=&r"(b), [t] "=&r"(t), [u] "=&r"(u)
                   : [src] "r"(src), [row] "r"(row), [sh] "r"(sh), [rs] "r"(32 - sh)
                   : "memory");
}
#else
static inline void xor_rot(uint32_t *row, const uint32_t *src, uint32_t sh) {
    for (int k = 0; k < 12; k++) row[k] ^= sh ? src[k] << sh | src[k + 1] >> (32 - sh) : src[k];
}
#endif

// Column-major streaming pack for q <= 64 (all normal codes from 2/3 up, every short code
// except 1/4). Column c supplies parity bits c*q .. c*q+q-1, i.e. rows 0..q-1 of P after a
// 32x32 transpose: one full 32-row tile then the remaining q-32 rows when q > 32. Output is
// appended MSB-first starting `bit` (0..31) bits into dst[0]; the bits above it are preserved,
// so FEC parity lands directly after an unaligned Kldpc (normal 5/6, 3/4, 9/10) with no
// intermediate buffer, scattered OR or copy_bits pass. Words through (bit + M + 63) / 32 are
// written, matching the zeroed guard of the generic path. Unused rows of each tile are zero.
// Placement: SRAM0-3 has only a few hundred bytes free (pvflight least), so this 0.4 KiB leaf
// runs from flash through the XIP cache, once per frame; misses are charged to its measurement.
static void __attribute__((noinline)) COMPACT ldpc_pack_stream(uint32_t *dst, uint32_t bit) {
    const uint32_t q = C->q, rem = q > 32 ? q - 32 : q, end = (bit + M + 63u) / 32u;
    uint32_t (*tile)[32] = (uint32_t (*)[32])P[(q + 31u) & ~31u];   // workspace above live tiles
    uint32_t *out = dst, acc = dst[0] & ~(~0u >> bit), fill = bit;
    for (unsigned k = 0; k < 12; k++) {
        for (unsigned row = 0; row < 32; row++) tile[0][row] = P[row][k];
        transpose32(tile[0]);
        if (q > 32) {
            for (unsigned row = 0; row < 32; row++) tile[1][row] = P[32 + row][k];
            transpose32(tile[1]);
        }
        const unsigned columns = k == 11 ? 8 : 32;
        // rem MSB-aligned bits per column; v << (rem - fill) is split so that fill may be 0.
#define APPEND_REM(v) do { acc |= (v) >> fill; fill += rem;                                   \
        if (fill >= 32) { *out++ = acc; fill -= 32; acc = ((v) << 1) << (rem - 1u - fill); } } while (0)
        if (q > 32) {
            for (unsigned col = 0; col < columns; col++) {
                const uint32_t v = tile[0][col];                // a full 32-row chunk first
                *out++ = acc | v >> fill;
                acc = (v << 1) << (31u - fill);                 // v << (32 - fill)
                APPEND_REM(tile[1][col]);
            }
        } else {
            for (unsigned col = 0; col < columns; col++) APPEND_REM(tile[0][col]);
        }
#undef APPEND_REM
    }
    if (fill) *out++ = acc;
    while (out < dst + end) *out++ = 0;
}

// Group form. With M = 360 q, address x for bit m of group g is (x + m q) mod M. Writing
// x = r + c q gives row r and column (c + m) mod 360 of a q x 360 matrix P (parity index
// i = r + column * q), so each table entry XORs the group's 360 bits, rotated right by c, into
// row r. The accumulation p_i ^= p_{i-1} in natural order is a cumulative XOR down the rows
// plus an exclusive prefix over whole columns. A 32x32 transpose then restores natural order.
static void __attribute__((noinline)) HOT(ldpc_matrix)(const uint32_t *info) {
    const uint32_t q = C->q;
    PROF(0);
    // Only live rows and the last 32-row transpose tile need initialization. Every edge
    // addresses a live row; the remaining worst-case workspace may serve as output scratch.
    memset(P, 0, ((q + 31u) & ~31u) * sizeof P[0]);
    for (uint32_t g = 0; g < C->n_groups; g++) {
        // u = the group's 360 bits (word-aligned copy); u2 = u || u, so rotation is extraction.
        uint32_t u[13], u2[25];
        const uint32_t *in = info + (g * 360 >> 5), s0 = g * 360 & 31;
        for (int k = 0; k < 12; k++) u[k] = s0 ? in[k] << s0 | in[k + 1] >> (32 - s0) : in[k];
        u[11] &= 0xFF000000u;                                  // 360 = 11 * 32 + 8
        u[12] = 0;
        for (int k = 0; k < 11; k++) u2[k] = u[k];
        u2[11] = u[11] | u[0] >> 8;
        for (int k = 0; k < 11; k++) u2[12 + k] = u[k] << 24 | u[k + 1] >> 8;
        u2[23] = u2[24] = 0;
        for (uint32_t a = grp[g]; a < grp[g + 1]; a++)
            xor_rot(P[a_row[a]], u2 + (a_off[a] >> 5), a_off[a] & 31);
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
    PROF(2);
}

bool dvbs2_ldpc_set_pack(unsigned pack) {
    if (pack > 1) return false;
    ldpc_pack = pack;
    return true;
}

unsigned dvbs2_ldpc_get_pack(void) { return ldpc_pack; }

static inline bool stream_pack(void) { return ldpc_pack && C->q <= 64; }

void HOT(dvbs2_ldpc)(const uint32_t *info, uint32_t *parity) {
    const uint32_t q = C->q;
    ldpc_matrix(info);
    if (q == 60) {
        ldpc_pack60(parity);
        PROF(3);
        return;
    }
    if (stream_pack()) {
        ldpc_pack_stream(parity, 0);
        PROF(3);
        return;
    }
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

bool dvbs2_map_set_kernel(unsigned kernel) {
    if (kernel > 1) return false;
    map_kernel = kernel;
    return true;
}

unsigned dvbs2_map_get_kernel(void) { return map_kernel; }

// FECFRAME word (MSB first: I0 Q0 I1 Q1 ... I15 Q15) -> shaper word (I_j at bit j, Q_j at
// bit 16+j): bit-reverse, then outer unshuffle (even bits to [15:0], odd bits to [31:16]).
static inline uint32_t unshuffle(uint32_t x) {
    uint32_t t;
    t = (x ^ (x >> 1)) & 0x22222222u, x ^= t ^ (t << 1);
    t = (x ^ (x >> 2)) & 0x0C0C0C0Cu, x ^= t ^ (t << 2);
    t = (x ^ (x >> 4)) & 0x00F000F0u, x ^= t ^ (t << 4);
    t = (x ^ (x >> 8)) & 0x0000FF00u, x ^= t ^ (t << 8);
    return x;
}

// Map, scramble and convert one FECFRAME word: swap I/Q where r0, flip signs, unshuffle.
static inline uint32_t map_scramble16(uint32_t w, uint32_t swap, uint32_t flip) {
    uint32_t x = rbit(w);
    const uint32_t t = (x ^ (x >> 1)) & swap;
    x ^= t | t << 1;
    x ^= flip;
    // pl_setup has finished consuming r0's Gold bit plane, so its first 1 KiB now holds
    // byte gathers: four I bits in [3:0], four Q bits in [19:16]. No extra table SRAM.
    return r0[x & 255u] | r0[(x >> 8) & 255u] << 4 |
           r0[(x >> 16) & 255u] << 8 | r0[x >> 24] << 12;
}

#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
// Complete 16-symbol words leave the stream's partial-word fill unchanged. Keep its state
// in registers for the whole run and fetch this small leaf from core 1's scratch bank.
_Static_assert(sizeof(symstream_t) == 24 && offsetof(symstream_t, fill) == 20 &&
               offsetof(symstream_t, acc_i) == 12, "map_words requires the six-word stream layout");
void dvbs2_map_words(const uint32_t *frame, const uint32_t *swap, const uint32_t *flip,
                     uint32_t nwords, symstream_t *stream, const uint32_t *table);
void dvbs2_map_words_rotate_chunks_scheduled(const uint32_t *frame, const uint32_t *swap,
                     const uint32_t *flip, uint32_t nwords, symstream_t *stream, const uint32_t *table);
static inline void map_words(const uint32_t *frame, const uint32_t *swap, const uint32_t *flip,
                             uint32_t nwords, symstream_t *stream, const uint32_t *table) {
    if (map_kernel) dvbs2_map_words_rotate_chunks_scheduled(frame, swap, flip, nwords, stream, table);
    else dvbs2_map_words(frame, swap, flip, nwords, stream, table);
}
#else
static void map_words(const uint32_t *frame, const uint32_t *swap, const uint32_t *flip,
                      uint32_t nwords, symstream_t *stream, const uint32_t *table) {
    (void)table;
    if (!map_kernel) {
        for (uint32_t k = 0; k < nwords; k++)
            symstream_put16(stream, map_scramble16(frame[k], swap[k], flip[k]));
        return;
    }
    // Rotation puts overflow in the opposite halfword's low bits. Select it with
    // the repeated fill mask, then swap halves to retain the next I/Q carry.
    const uint32_t fill = stream->fill, mask = ((1u << fill) - 1u) * 0x10001u;
    uint32_t carry = stream->acc_i | stream->acc_q << 16;
    while (nwords) {
        const uint32_t offset = stream->n & stream->mask;
        const uint32_t limit_minus_one = stream->mask - offset;
        uint32_t count = nwords;
        if (count - 1 > limit_minus_one) count = limit_minus_one + 1;
        uint32_t *out = stream->out + offset;
        nwords -= count;
        stream->n += count;
        do {
            const uint32_t w = map_scramble16(*frame++, *swap++, *flip++);
            const uint32_t rotated = (w << fill) | (w >> ((0u - fill) & 31u));
            *out++ = (rotated & ~mask) | carry;
            const uint32_t overflow = rotated & mask;
            carry = (overflow << 16) | (overflow >> 16);
        } while (--count);
    }
    stream->acc_i = carry & 0xFFFFu;
    stream->acc_q = carry >> 16;
}
#endif

// PL scrambling of 16 symbols by R = 2 r1 + r0 (5.5.4): R=1 (-Q, I), R=2 (-I, -Q), R=3 (Q, -I).
// In sign bits: I' = (r0 ? bQ : bI) ^ (r0 ^ r1), Q' = (r0 ? bI : bQ) ^ r1. rm = r0 | r1 << 16.
// Used at init for the pilot blocks; data words are scrambled in the interleaved domain below.
static inline uint32_t scramble16(uint32_t w, uint32_t rm) {
    const uint32_t bi = w & 0xFFFFu, bq = w >> 16, a = rm & 0xFFFFu, c = rm >> 16;
    const uint32_t ni = ((a & bq) | (~a & bi)) ^ (a ^ c), nq = ((a & bi) | (~a & bq)) ^ c;
    return (ni & 0xFFFFu) | nq << 16;
}

void symstream_flush(symstream_t *s) {
    if (s->fill) symstream_put(s, 0, 16 - s->fill);
}

// Precomputed per code: for each 16-symbol data word (a data word never straddles a pilot block,
// since 1440 = 90 x 16) the scrambling masks in the bit-reversed interleaved domain (bit 2j = I_j,
// bit 2j+1 = Q_j): swap[] has bit 2j set where r0_j = 1, flip[] has r0^r1 at 2j and r1 at 2j+1.
// Also the scrambled pilot blocks, as 16+16+4 symbols.
static uint32_t swapm[64800 / 32 + 1] HI, flipm[64800 / 32 + 1] HI, pil[22][3] HI;
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
        const uint32_t a = plane16(r0, pos), c = plane16(r1, pos);
        swapm[j] = flipm[j] = 0;
        for (int b = 0; b < 16; b++) {
            const uint32_t ra = (a >> b) & 1, rc = (c >> b) & 1;
            swapm[j] |= ra << (2 * b);
            flipm[j] |= (ra ^ rc) << (2 * b) | rc << (2 * b + 1);
        }
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
    for (uint32_t k = 0; k < 256; k++) r0[k] = unshuffle(k);
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
uint32_t *dvbs2_frame_buffer(void) { return fe; }

bool HOT(dvbs2_frontend_begin)(const uint32_t *bb, uint32_t *frame, dvbs2_frontend_t *state) {
    if (bch_deg != 160 || bch_kernel == S2_BCH_BASELINE) return false;
    PROF(4);
    const uint32_t kw = (C->kbch + 31u) / 32;
    for (uint32_t k = 0; k < kw; k++) frame[k] = bb[k] ^ bb_prbs[k];
    if (C->kbch % 32u) frame[kw - 1] &= ~(~0u >> (C->kbch % 32u));
    memset(frame + kw, 0, (DVBS2_FE_WORDS - kw) * sizeof frame[0]);
    *state = (dvbs2_frontend_t){.frame = frame, .kernel = bch_kernel};
    return true;
}

static void HOT(bch160_continue_c)(const uint32_t *m, uint32_t *parity, uint32_t nw) {
    uint32_t r0_ = parity[0], r1_ = parity[1], r2 = parity[2], r3 = parity[3], r4 = parity[4];
    for (uint32_t i = 0; i < nw; i++) {
        const uint32_t x = r0_ ^ m[i];
        const uint32_t *a = bch_tab[3][x >> 24], *b = bch_tab[2][(x >> 16) & 0xFF],
                       *c = bch_tab[1][(x >> 8) & 0xFF], *d = bch_tab[0][x & 0xFF];
        r0_ = r1_ ^ a[0] ^ b[0] ^ c[0] ^ d[0];
        r1_ = r2 ^ a[1] ^ b[1] ^ c[1] ^ d[1];
        r2 = r3 ^ a[2] ^ b[2] ^ c[2] ^ d[2];
        r3 = r4 ^ a[3] ^ b[3] ^ c[3] ^ d[3];
        r4 = a[4] ^ b[4] ^ c[4] ^ d[4];
    }
    parity[0] = r0_; parity[1] = r1_; parity[2] = r2; parity[3] = r3; parity[4] = r4;
    parity[5] = 0;
}

bool HOT(dvbs2_frontend_step)(dvbs2_frontend_t *state, uint32_t max_words) {
    if (state->done) return true;
    if (!max_words) return false;
    const uint32_t nw = C->kbch / 32u;
    const uint32_t left = nw - state->cursor;
    const uint32_t take = left < max_words ? left : max_words;
    const uint32_t *m = state->frame + state->cursor;
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    if (state->kernel == S2_BCH_STREAM_ARM) {
        if (frontend_bank) dvbs2_bch160_continue_y(m, state->parity, bch_tab, take);
        else dvbs2_bch160_continue(m, state->parity, bch_tab, take);
    } else
#endif
        bch160_continue_c(m, state->parity, take);
    state->cursor += take;
    if (state->cursor != nw) return false;

    for (uint32_t i = 0; i < (C->kbch % 32u) / 8; i++) {
        uint32_t *p = state->parity;
        const uint32_t *t = bch_tab[0][(p[0] >> 24) ^ ((state->frame[nw] >> (24 - 8 * i)) & 0xFF)];
        for (unsigned k = 0; k < 4; k++) p[k] = ((p[k] << 8) | (p[k + 1] >> 24)) ^ t[k];
        p[4] = (p[4] << 8) ^ t[4];
    }
    copy_bits(state->frame, C->kbch, state->parity, bch_deg);
    state->done = true;
    PROF(5);
    return true;
}

void HOT(dvbs2_frame_begin_into)(const uint32_t *bb, uint32_t *frame) {
    const uint32_t kw = (C->kbch + 31u) / 32;
    PROF(4);
    for (uint32_t k = 0; k < kw; k++) frame[k] = bb[k] ^ bb_prbs[k];
    if (C->kbch % 32u) frame[kw - 1] &= ~(~0u >> (C->kbch % 32u));
    memset(frame + kw, 0, (DVBS2_FE_WORDS - kw) * sizeof frame[0]);
    uint32_t par[BCH_W + 1];
    dvbs2_bch(frame, par);
    copy_bits(frame, C->kbch, par, bch_deg);
    PROF(5);
}

void __attribute__((noinline)) HOT(dvbs2_frame_ldpc_into)(uint32_t *frame) {
    static uint32_t lpar[MAX_BITS_W] HI;
    if (!(C->kldpc & 31u)) {
        // LDPC consumes all information before writing parity, so a word-aligned tail
        // can be the destination directly. FE storage includes the parity guard words.
        dvbs2_ldpc(frame, frame + C->kldpc / 32u);
    } else if (stream_pack()) {
        // The matrix has consumed every information bit; the packer keeps the high bits of
        // the shared word and appends parity in place (guard <= DVBS2_FE_WORDS).
        ldpc_matrix(frame);
        ldpc_pack_stream(frame + C->kldpc / 32u, C->kldpc & 31u);
        PROF(3);
    } else {
        dvbs2_ldpc(frame, lpar);
        copy_bits(frame, C->kldpc, lpar, M);
    }
    PROF(6);
}

void HOT(dvbs2_frame_finish_from)(const uint32_t *frame, symstream_t *out) {
    const uint32_t nsym = C->nldpc / 2u;
    symstream_t s = *out;                                      // local copy keeps state in registers
    for (int k = 0; k < 5; k++) symstream_put16(&s, hdr[k]);
    symstream_put(&s, hdr[5], 10);
    const uint32_t *f = frame, *sw = swapm, *fl = flipm;
    for (uint32_t left = nsym, blk = 0; left;) {               // runs of 1440 data symbols
        const uint32_t run = blk < n_pil && left > 1440 ? 1440 : left;
        const uint32_t words = run / 16;
        map_words(f, sw, fl, words, &s, r0);
        f += words; sw += words; fl += words;
        if (run % 16) symstream_put(&s, map_scramble16(*f, *sw, *fl), run % 16);
        if (blk < n_pil && left > 1440) {
            symstream_put16(&s, pil[blk][0]), symstream_put16(&s, pil[blk][1]);
            symstream_put(&s, pil[blk][2], 4);
            blk++;
        }
        left -= run;
    }
    *out = s;
    PROF(7);
}

void HOT(dvbs2_frame_begin)(const uint32_t *bb) { dvbs2_frame_begin_into(bb, fe); }

void HOT(dvbs2_frame_ldpc)(void) { dvbs2_frame_ldpc_into(fe); }

void HOT(dvbs2_frame_finish)(symstream_t *out) { dvbs2_frame_finish_from(fe, out); }

void HOT(dvbs2_frame)(const uint32_t *bb, symstream_t *out) {
    dvbs2_frame_begin(bb);
    dvbs2_frame_ldpc();
    dvbs2_frame_finish(out);
}
