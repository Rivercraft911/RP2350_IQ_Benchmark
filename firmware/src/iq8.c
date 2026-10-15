#include "iq8.h"
#include "coeffs.h"
#include "iqgen.h"
#include <stddef.h>

#ifdef IQ_ON_DEVICE
#define IQ8_HOT __attribute__((section(".time_critical.iq8")))
#else
#define IQ8_HOT
#endif

// Q30 nearest constants; signed shifts follow the same GCC/Clang convention as iqgen.c.
enum { PLANE_A_Q30 = 701455651, PLANE_B_Q30 = 290552444 };

void iq8_build_tables(const int32_t *coef, const iq8_tables_t *t) {
    int32_t ca[IQ8_L * IQ8_SPS], cb[IQ8_L * IQ8_SPS];
    for (int k = 0; k < IQ8_L * IQ8_SPS; ++k) {
        ca[k] = (int32_t)(((int64_t)coef[k] * PLANE_A_Q30 + ((int64_t)1 << 29)) >> 30);
        cb[k] = (int32_t)(((int64_t)coef[k] * PLANE_B_Q30 + ((int64_t)1 << 29)) >> 30);
    }
    for (unsigned h = 0; h < 1u << IQ8_L; ++h) {
        for (unsigned p = 0; p < IQ8_SPS; p += 2) {
            uint32_t wa = 0, wb = 0;
            for (unsigned z = 0; z < 2; ++z) {
                int32_t sa = 0, sb = 0;
                for (unsigned age = 0; age < IQ8_L; ++age) {
                    int sign = (h >> (IQ8_L - 1 - age)) & 1u ? -1 : 1;
                    sa += sign * ca[age * IQ8_SPS + p + z];
                    sb += sign * cb[age * IQ8_SPS + p + z];
                }
                int32_t ra = (sa + (1 << (COEF_FRAC_BITS - 1))) >> COEF_FRAC_BITS;
                int32_t rb = (sb + (1 << (COEF_FRAC_BITS - 1))) >> COEF_FRAC_BITS;
                wa |= (uint32_t)(uint16_t)ra << (16 * z);
                wb |= (uint32_t)(uint16_t)rb << (16 * z);
            }
            t->a[h * (IQ8_SPS / 2) + p / 2] = wa;
            t->b[h * (IQ8_SPS / 2) + p / 2] = wb;
        }
    }
}

void iq8_fill(iq8_input_t *in, uint32_t groups, uint32_t *seed) {
    // Angles pi/8+k*pi/4, k=0..7. Bits: I-A,I-B,Q-A,Q-B sign.
    static const uint8_t signs[8] = {8, 2, 1, 11, 7, 13, 14, 4};
    uint32_t x = *seed;
    for (uint32_t g = 0; g < groups; ++g) {
        uint32_t a = 0, b = 0;
        for (unsigned j = 0; j < 16; ++j) {
            x ^= x << 13; x ^= x >> 17; x ^= x << 5;
            unsigned s = signs[x & 7];
            a |= (s & 1u) << j | ((s >> 2) & 1u) << (j + 16);
            b |= ((s >> 1) & 1u) << j | ((s >> 3) & 1u) << (j + 16);
        }
        in[g] = (iq8_input_t){a, b};
    }
    *seed = x;
}

static inline uint32_t packed_sum(uint32_t a, uint32_t b, int i_axis) {
    int32_t lo = (int16_t)a + (int16_t)b;
    int32_t hi = (int16_t)(a >> 16) + (int16_t)(b >> 16);
    return ((uint32_t)lo & 0x3fffu) | (((uint32_t)hi & 0x3fffu) << 16) |
           (i_axis ? 0x40004000u : 0);
}

void IQ8_HOT iq8_kernel_c(uint32_t *out, const iq8_input_t *in, uint32_t groups,
                         const iq8_input_t *prev, const iq8_tables_t *t) {
    iq8_input_t last = prev ? *prev : (iq8_input_t){0, 0xffff0000u};
    for (uint32_t g = 0; g < groups; ++g) {
        iq8_input_t cur = in[g];
        uint32_t ria = (last.a & 0xffffu) | cur.a << 16;
        uint32_t rqa = last.a >> 16 | (cur.a & 0xffff0000u);
        uint32_t rib = (last.b & 0xffffu) | cur.b << 16;
        uint32_t rqb = last.b >> 16 | (cur.b & 0xffff0000u);
        last = cur;
        for (unsigned j = 0; j < 16; ++j) {
            unsigned ia = ((ria >> (17 + j - IQ8_L)) & ((1u << IQ8_L) - 1)) * 2;
            unsigned qa = ((rqa >> (17 + j - IQ8_L)) & ((1u << IQ8_L) - 1)) * 2;
            unsigned ib = ((rib >> (17 + j - IQ8_L)) & ((1u << IQ8_L) - 1)) * 2;
            unsigned qb = ((rqb >> (17 + j - IQ8_L)) & ((1u << IQ8_L) - 1)) * 2;
            out[0] = packed_sum(t->a[ia], t->b[ib], 1);
            out[1] = packed_sum(t->a[qa], t->b[qb], 0);
            out[2] = packed_sum(t->a[ia + 1], t->b[ib + 1], 1);
            out[3] = packed_sum(t->a[qa + 1], t->b[qb + 1], 0);
            out += 4;
        }
    }
}
