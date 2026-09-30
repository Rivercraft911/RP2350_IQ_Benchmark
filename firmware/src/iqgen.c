#include "iqgen.h"

#include "coeffs.h"

#ifdef IQ_ON_DEVICE
#define IQ_HOT(f) __attribute__((section(".time_critical." #f))) f
#else
#define IQ_HOT(f) f
#endif

// Signed right shifts below are arithmetic (GCC/Clang on Arm and x86), matching Python's >>.
static inline uint32_t slot(int32_t acc) {
    return (uint32_t)((acc + (1 << (COEF_FRAC_BITS - 1))) >> COEF_FRAC_BITS) & 0x3FFFu;
}

// History register h: bit L-1 = newest symbol (age 0), bit 0 = age L-1.
static inline uint32_t push(uint32_t h, uint32_t bit, int L) {
    return (h >> 1) | (bit << (L - 1));
}

static int32_t fir(uint32_t h, const int32_t *c, int sps, int L, int p) {
    int32_t acc = 0;
    for (int a = 0; a < L; a++)
        acc += ((h >> (L - 1 - a)) & 1) ? -c[a * sps + p] : c[a * sps + p];
    return acc;
}

void iq_build_tables(const iq_cfg_t *cfg) {
    const int half = cfg->sps / 2;
    for (uint32_t h = 0; h < (1u << cfg->L); h++)
        for (int k = 0; k < half; k++) {
            uint32_t w = slot(fir(h, cfg->coef, cfg->sps, cfg->L, 2 * k)) |
                         slot(fir(h, cfg->coef, cfg->sps, cfg->L, 2 * k + 1)) << 16;
            cfg->tq[h * half + k] = w;
            cfg->ti[h * half + k] = w | IQ_FLAG_BIT | IQ_FLAG_BIT << 16;
        }
}

// v0: direct convolution, L signed adds per sample per axis. Baseline only.
static void IQ_HOT(k_conv)(uint32_t *out, const uint32_t *in, uint32_t n, uint32_t prev,
                           const iq_cfg_t *cfg) {
    const int sps = cfg->sps, L = cfg->L;
    uint32_t hi = 0, hq = 0;
    for (int j = 0; j < 16; j++) {
        hi = push(hi, (prev >> j) & 1, L);
        hq = push(hq, (prev >> (16 + j)) & 1, L);
    }
    for (uint32_t w = 0; w < n; w++)
        for (int j = 0; j < 16; j++) {
            hi = push(hi, (in[w] >> j) & 1, L);
            hq = push(hq, (in[w] >> (16 + j)) & 1, L);
            for (int p = 0; p < sps; p++)
                *out++ = slot(fir(hi, cfg->coef, sps, L, p)) | IQ_FLAG_BIT |
                         slot(fir(hq, cfg->coef, sps, L, p)) << 16;
        }
}

// v1: table lookup with a shift-register history and per-slot halfword packing.
static void IQ_HOT(k_lut_shift)(uint32_t *out, const uint32_t *in, uint32_t n, uint32_t prev,
                                const iq_cfg_t *cfg) {
    const int half = cfg->sps / 2, L = cfg->L;
    uint32_t hi = 0, hq = 0;
    for (int j = 0; j < 16; j++) {
        hi = push(hi, (prev >> j) & 1, L);
        hq = push(hq, (prev >> (16 + j)) & 1, L);
    }
    for (uint32_t w = 0; w < n; w++)
        for (int j = 0; j < 16; j++) {
            hi = push(hi, (in[w] >> j) & 1, L);
            hq = push(hq, (in[w] >> (16 + j)) & 1, L);
            for (int k = 0; k < half; k++) {
                uint32_t a = cfg->ti[hi * half + k], b = cfg->tq[hq * half + k];
                *out++ = (a & 0xFFFFu) | (b << 16);
                *out++ = (a >> 16) | (b & 0xFFFF0000u);
            }
        }
}

// v2: 16-symbol window registers. ri holds I bits of prev in [15:0] and of the current word
// in [31:16]; the index for symbol j is the L-bit field ending at bit 16+j (UBFX). Packing
// maps to PKHBT/PKHTB. Specialised per (sps, L) so shifts and masks are immediates.
static inline __attribute__((always_inline)) void lut_win(
    uint32_t *restrict out, const uint32_t *restrict in, uint32_t n, uint32_t prev,
    const uint32_t *restrict ti, const uint32_t *restrict tq, const int sps, const int L) {
    const int half = sps / 2;
    const uint32_t mask = (1u << L) - 1;
    for (uint32_t w = 0; w < n; w++) {
        const uint32_t cur = in[w];
        const uint32_t ri = (prev & 0xFFFFu) | (cur << 16);
        const uint32_t rq = (prev >> 16) | (cur & 0xFFFF0000u);
        prev = cur;
#pragma GCC unroll 16
        for (int j = 0; j < 16; j++) {
            const uint32_t *ei = ti + ((ri >> (17 + j - L)) & mask) * half;
            const uint32_t *eq = tq + ((rq >> (17 + j - L)) & mask) * half;
            for (int k = 0; k < half; k++) {
                const uint32_t a = ei[k], b = eq[k];
                out[0] = (a & 0xFFFFu) | (b << 16);
                out[1] = (a >> 16) | (b & 0xFFFF0000u);
                out += 2;
            }
        }
    }
}

#define LUT_WIN(S, LL)                                                                     \
    static void IQ_HOT(k_win_##S##_##LL)(uint32_t *o, const uint32_t *i, uint32_t n,       \
                                         uint32_t p, const iq_cfg_t *c) {                  \
        lut_win(o, i, n, p, c->ti, c->tq, S, LL);                                          \
    }
LUT_WIN(2, 8) LUT_WIN(2, 10) LUT_WIN(2, 12) LUT_WIN(4, 8) LUT_WIN(4, 10) LUT_WIN(4, 12)

// v3: PAIRS layout. Table words are stored as they are, since the PIO does the I/Q interleave;
// per symbol only field extracts, loads and stores remain.
static inline __attribute__((always_inline)) void lut_pair(
    uint32_t *restrict out, const uint32_t *restrict in, uint32_t n, uint32_t prev,
    const uint32_t *restrict ti, const uint32_t *restrict tq, const int sps, const int L) {
    const int half = sps / 2;
    const uint32_t mask = (1u << L) - 1;
    for (uint32_t w = 0; w < n; w++) {
        const uint32_t cur = in[w];
        const uint32_t ri = (prev & 0xFFFFu) | (cur << 16);
        const uint32_t rq = (prev >> 16) | (cur & 0xFFFF0000u);
        prev = cur;
#pragma GCC unroll 16
        for (int j = 0; j < 16; j++) {
            const uint32_t *ei = ti + ((ri >> (17 + j - L)) & mask) * half;
            const uint32_t *eq = tq + ((rq >> (17 + j - L)) & mask) * half;
            for (int k = 0; k < half; k++) {
                out[0] = ei[k];
                out[1] = eq[k];
                out += 2;
            }
        }
    }
}

#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
// v4: v3 in hand-written Thumb-2. Per symbol: 2 UBFX, sps LDR (register offset, LSL), and
// sps/2 STRD with post-increment. ti1/tq1 point one word into the table for phase pair 1.
#define SYM_SPS2                                                                           \
    "ubfx %[hi], %[ri], #(17+\\j-%c[L]), #%c[L]\n"                                         \
    "ubfx %[hq], %[rq], #(17+\\j-%c[L]), #%c[L]\n"                                         \
    "ldr  %[a0], [%[ti0], %[hi], lsl #2]\n"                                                \
    "ldr  %[b0], [%[tq0], %[hq], lsl #2]\n"                                                \
    "strd %[a0], %[b0], [%[o]], #8\n"
#define SYM_SPS4                                                                           \
    "ubfx %[hi], %[ri], #(17+\\j-%c[L]), #%c[L]\n"                                         \
    "ubfx %[hq], %[rq], #(17+\\j-%c[L]), #%c[L]\n"                                         \
    "ldr  %[a0], [%[ti0], %[hi], lsl #3]\n"                                                \
    "ldr  %[b0], [%[tq0], %[hq], lsl #3]\n"                                                \
    "ldr  %[a1], [%[ti1], %[hi], lsl #3]\n"                                                \
    "ldr  %[b1], [%[tq1], %[hq], lsl #3]\n"                                                \
    "strd %[a0], %[b0], [%[o]], #8\n"                                                      \
    "strd %[a1], %[b1], [%[o]], #8\n"

#define LUT_ASM(S, LL)                                                                     \
    static void IQ_HOT(k_asm_##S##_##LL)(uint32_t *o, const uint32_t *in, uint32_t n,       \
                                         uint32_t prev, const iq_cfg_t *c) {               \
        const uint32_t *ti0 = c->ti, *tq0 = c->tq, *ti1 = ti0 + 1, *tq1 = tq0 + 1;          \
        for (const uint32_t *end = in + n; in != end; in++) {                              \
            const uint32_t cur = *in;                                                      \
            const uint32_t ri = (prev & 0xFFFFu) | (cur << 16);                            \
            const uint32_t rq = (prev >> 16) | (cur & 0xFFFF0000u);                        \
            prev = cur;                                                                    \
            uint32_t hi, hq, a0, b0, a1, b1;                                               \
            __asm volatile(".irp j,0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15\n" SYM_SPS##S     \
                           ".endr\n"                                                       \
                           : [o] "+r"(o), [hi] "=&r"(hi), [hq] "=&r"(hq), [a0] "=&r"(a0),  \
                             [b0] "=&r"(b0), [a1] "=&r"(a1), [b1] "=&r"(b1)                \
                           : [ri] "r"(ri), [rq] "r"(rq), [ti0] "r"(ti0), [tq0] "r"(tq0),   \
                             [ti1] "r"(ti1), [tq1] "r"(tq1), [L] "i"(LL)                   \
                           : "memory");                                                    \
            (void)a1, (void)b1;                                                            \
        }                                                                                  \
    }
LUT_ASM(2, 8) LUT_ASM(2, 10) LUT_ASM(2, 12) LUT_ASM(4, 8) LUT_ASM(4, 10) LUT_ASM(4, 12)
#define ASM_KERNELS                                                                        \
    {"lut_asm", 2, 8, 1, k_asm_2_8},   {"lut_asm", 2, 10, 1, k_asm_2_10},                    \
    {"lut_asm", 2, 12, 1, k_asm_2_12}, {"lut_asm", 4, 8, 1, k_asm_4_8},                      \
    {"lut_asm", 4, 10, 1, k_asm_4_10}, {"lut_asm", 4, 12, 1, k_asm_4_12},
#else
#define ASM_KERNELS
#endif

#define LUT_PAIR(S, LL)                                                                    \
    static void IQ_HOT(k_pair_##S##_##LL)(uint32_t *o, const uint32_t *i, uint32_t n,      \
                                          uint32_t p, const iq_cfg_t *c) {                 \
        lut_pair(o, i, n, p, c->ti, c->tq, S, LL);                                         \
    }
LUT_PAIR(2, 8) LUT_PAIR(2, 10) LUT_PAIR(2, 12) LUT_PAIR(4, 8) LUT_PAIR(4, 10) LUT_PAIR(4, 12)

const iq_kernel_info_t IQ_KERNELS[] = {
    {"conv", 0, 0, 0, k_conv},
    {"lut_shift", 0, 0, 0, k_lut_shift},
    {"lut_win", 2, 8, 0, k_win_2_8},   {"lut_win", 2, 10, 0, k_win_2_10},
    {"lut_win", 2, 12, 0, k_win_2_12}, {"lut_win", 4, 8, 0, k_win_4_8},
    {"lut_win", 4, 10, 0, k_win_4_10}, {"lut_win", 4, 12, 0, k_win_4_12},
    {"lut_pair", 2, 8, 1, k_pair_2_8},   {"lut_pair", 2, 10, 1, k_pair_2_10},
    {"lut_pair", 2, 12, 1, k_pair_2_12}, {"lut_pair", 4, 8, 1, k_pair_4_8},
    {"lut_pair", 4, 10, 1, k_pair_4_10}, {"lut_pair", 4, 12, 1, k_pair_4_12},
    ASM_KERNELS
};
const int IQ_N_KERNELS = sizeof IQ_KERNELS / sizeof IQ_KERNELS[0];

static int streq(const char *a, const char *b) {
    while (*a && *a == *b) a++, b++;
    return *a == *b;
}

const iq_kernel_info_t *iq_find_kernel(const char *name, int sps, int L) {
    for (int i = 0; i < IQ_N_KERNELS; i++) {
        const iq_kernel_info_t *k = &IQ_KERNELS[i];
        if (streq(k->name, name) && (!k->sps || (k->sps == sps && k->L == L))) return k;
    }
    return 0;
}

uint32_t crc32_update(uint32_t crc, const void *data, size_t nbytes) {
    static const uint32_t t[16] = {
        0x00000000, 0x1DB71064, 0x3B6E20C8, 0x26D930AC, 0x76DC4190, 0x6B6B51F4,
        0x4DB26158, 0x5005713C, 0xEDB88320, 0xF00F9344, 0xD6D6A3E8, 0xCB61B38C,
        0x9B64C2B0, 0x86D3D2D4, 0xA00AE278, 0xBDBDF21C};
    const uint8_t *p = data;
    crc = ~crc;
    while (nbytes--) {
        crc ^= *p++;
        crc = (crc >> 4) ^ t[crc & 15];
        crc = (crc >> 4) ^ t[crc & 15];
    }
    return ~crc;
}

void prbs_fill(uint32_t *w, size_t n, uint32_t *state) {
    uint32_t x = *state;
    while (n--) {
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
        *w++ = x;
    }
    *state = x;
}
