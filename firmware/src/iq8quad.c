#include "iq8quad.h"
#include <stddef.h>

#ifdef IQ_ON_DEVICE
#define IQ8QUAD_HOT __attribute__((section(".time_critical.iq8quad_c")))
#else
#define IQ8QUAD_HOT
#endif

// Offline alpha=.20 RRC, L9, N2, Kaiser beta1.16; symmetric cropped pre-emphasis
// [-.0455, 1.091, -.0455], then exact worst-phase L1 scaling to 1dB headroom.
// Q8 quantization. Baseband + DAC-hold mask model only; RF images are unqualified.
static const int32_t coefficients[IQ8QUAD_SPAN * IQ8QUAD_SPS] = {
    26061, 1353, -59453, 20157, 117236, -77279, -242951, 262833, 1061537,
    1061537, 262833, -242951, -77279, 117236, 20157, -59453, 1353, 26061,
};
enum { PLANE_A_Q30 = 701455651, PLANE_B_Q30 = 290552444 };

const int32_t *iq8quad_coefficients(void) { return coefficients; }

void iq8quad_build(const iq8quad_tables_t *tables) {
    int32_t ca[IQ8QUAD_SPAN * IQ8QUAD_SPS], cb[IQ8QUAD_SPAN * IQ8QUAD_SPS];
    for (unsigned k = 0; k < IQ8QUAD_SPAN * IQ8QUAD_SPS; k++) {
        ca[k] = (int32_t)(((int64_t)coefficients[k] * PLANE_A_Q30 + ((int64_t)1 << 29)) >> 30);
        cb[k] = (int32_t)(((int64_t)coefficients[k] * PLANE_B_Q30 + ((int64_t)1 << 29)) >> 30);
    }
    for (uint32_t key = 0; key < IQ8QUAD_ROWS; key++) {
        for (unsigned symbol = 0; symbol < IQ8QUAD_SYMBOLS; symbol++) {
            uint32_t a = 0, b = 0;
            for (unsigned phase = 0; phase < IQ8QUAD_SPS; phase++) {
                int32_t sa = 0, sb = 0;
                for (unsigned age = 0; age < IQ8QUAD_SPAN; age++) {
                    int sign = (key >> (IQ8QUAD_SPAN - 1 + symbol - age)) & 1u ? -1 : 1;
                    sa += sign * ca[age * IQ8QUAD_SPS + phase];
                    sb += sign * cb[age * IQ8QUAD_SPS + phase];
                }
                a |= (uint32_t)(uint16_t)((sa + 128) >> 8) << (16 * phase);
                b |= (uint32_t)(uint16_t)((sb + 128) >> 8) << (16 * phase);
            }
            tables->a[key * IQ8QUAD_ROW_WORDS + symbol] = a;
            tables->b[key * IQ8QUAD_ROW_WORDS + symbol] = b;
        }
    }
}

static inline uint32_t add_signed_lanes(uint32_t a, uint32_t b) {
    int32_t lo = (int16_t)a + (int16_t)b;
    int32_t hi = (int16_t)(a >> 16) + (int16_t)(b >> 16);
    return (uint32_t)(uint16_t)lo | (uint32_t)(uint16_t)hi << 16;
}

void IQ8QUAD_HOT iq8quad_kernel_c(uint32_t *out, const iq8_input_t *in, uint32_t groups,
                                 const iq8_input_t *prev, const iq8quad_tables_t *tables) {
    iq8_input_t last = prev ? *prev : (iq8_input_t){0, 0xffff0000u};
    for (uint32_t g = 0; g < groups; g++) {
        const iq8_input_t cur = in[g];
        const uint32_t ia = (last.a & 0xffffu) | cur.a << 16;
        const uint32_t qa = last.a >> 16 | (cur.a & 0xffff0000u);
        const uint32_t ib = (last.b & 0xffffu) | cur.b << 16;
        const uint32_t qb = last.b >> 16 | (cur.b & 0xffff0000u);
        last = cur;
        for (unsigned j = 0; j < 16; j += 4) {
            const unsigned ai = ((ia >> (8 + j)) & 0xfffu) * IQ8QUAD_ROW_WORDS;
            const unsigned aq = ((qa >> (8 + j)) & 0xfffu) * IQ8QUAD_ROW_WORDS;
            const unsigned bi = ((ib >> (8 + j)) & 0xfffu) * IQ8QUAD_ROW_WORDS;
            const unsigned bq = ((qb >> (8 + j)) & 0xfffu) * IQ8QUAD_ROW_WORDS;
            for (unsigned w = 0; w < IQ8QUAD_ROW_WORDS; w++) {
                out[2 * w] = add_signed_lanes(tables->a[ai + w], tables->b[bi + w]);
                out[2 * w + 1] = add_signed_lanes(tables->a[aq + w], tables->b[bq + w]);
            }
            out += 2 * IQ8QUAD_ROW_WORDS;
        }
    }
}

#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
#include "hardware/structs/interp.h"

void __attribute__((section(".time_critical.iq8quad_interp")))
iq8quad_kernel_interp(uint32_t *out, const iq8_input_t *in, uint32_t groups,
                     const iq8_input_t *prev, const iq8quad_tables_t *tables) {
    if (!groups) return;
    uint32_t saved[2][7];
    for (unsigned plane = 0; plane < 2; plane++) {
        interp_hw_t *p = &interp_hw_array[plane];
        saved[plane][0] = p->accum[0]; saved[plane][1] = p->accum[1];
        saved[plane][2] = p->base[0]; saved[plane][3] = p->base[1]; saved[plane][4] = p->base[2];
        saved[plane][5] = p->ctrl[0]; saved[plane][6] = p->ctrl[1];
        // ROR4 and mask4..15 selects sourcebits8..19 scaled by16bytes/row.
        p->ctrl[0] = 4u | (4u << SIO_INTERP0_CTRL_LANE0_MASK_LSB_LSB) |
                     (15u << SIO_INTERP0_CTRL_LANE0_MASK_MSB_LSB) |
                     SIO_INTERP0_CTRL_LANE0_CROSS_INPUT_BITS;
        p->ctrl[1] = 4u | (31u << SIO_INTERP0_CTRL_LANE1_MASK_MSB_LSB);
        p->base[0] = (uint32_t)(plane ? tables->b : tables->a);
        p->base[1] = 0;
    }
    iq8quad_kernel_interp_asm(out, in, groups, prev);
    for (unsigned plane = 0; plane < 2; plane++) {
        interp_hw_t *p = &interp_hw_array[plane];
        p->base[0] = saved[plane][2]; p->base[1] = saved[plane][3]; p->base[2] = saved[plane][4];
        p->ctrl[0] = saved[plane][5]; p->ctrl[1] = saved[plane][6];
        p->accum[0] = saved[plane][0]; p->accum[1] = saved[plane][1];
    }
}
#endif
