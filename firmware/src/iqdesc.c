#include "iqdesc.h"

#ifdef IQ_ON_DEVICE
#define IQDESC_HOT __attribute__((section(".time_critical.iqdesc_c")))
#define IQDESC_PAIR_HOT __attribute__((section(".time_critical.iqdesc_pair_c")))
#else
#define IQDESC_HOT
#define IQDESC_PAIR_HOT
#endif

void IQDESC_HOT iqdesc_c(uint32_t *di, uint32_t *dq, const uint32_t *in, uint32_t nwords,
                        uint32_t prev, uint32_t base_i, uint32_t base_q) {
    for (uint32_t w = 0; w < nwords; ++w) {
        const uint32_t cur = in[w];
        const uint32_t ri = (prev & 0xffffu) | cur << 16;
        const uint32_t rq = prev >> 16 | (cur & 0xffff0000u);
        prev = cur;
#pragma GCC unroll 16
        for (unsigned j = 0; j < 16; ++j) {
            *di++ = base_i + (((ri >> (5 + j)) & 0xfffu) << 3);
            *dq++ = base_q + (((rq >> (5 + j)) & 0xfffu) << 3);
        }
    }
}

void IQDESC_PAIR_HOT iqdesc_pair_c(uint32_t *di, uint32_t *dq, const uint32_t *in, uint32_t nwords,
              uint32_t prev, uint32_t base_i, uint32_t base_q) {
    for (uint32_t w = 0; w < nwords; ++w) {
        const uint32_t cur = in[w];
        const uint32_t ri = (prev & 0xffffu) | cur << 16;
        const uint32_t rq = prev >> 16 | (cur & 0xffff0000u);
        prev = cur;
#pragma GCC unroll 8
        for (unsigned j = 0; j < 8; ++j) {
            *di++ = base_i + (((ri >> (5 + 2 * j)) & 0x1fffu) << 4);
            *dq++ = base_q + (((rq >> (5 + 2 * j)) & 0x1fffu) << 4);
        }
    }
}

#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
#include "hardware/structs/interp.h"

// RP2350 SHIFT is right-rotate (RP2350 datasheet register descriptions / erratum E1).
// POP0 returns the lane-0 address and simultaneously feeds lane-1 ROR1 back to ACCUM1.
// Its next result therefore selects the next bit window, without four per-symbol I/O writes.
void __attribute__((section(".time_critical.iqdesc_interp")))
iqdesc_interp(uint32_t *di, uint32_t *dq, const uint32_t *in, uint32_t nwords,
              uint32_t prev, uint32_t base_i, uint32_t base_q) {
    if (!nwords) return;
    uint32_t saved[2][7];
    for (unsigned axis = 0; axis < 2; ++axis) {
        interp_hw_t *p = &interp_hw_array[axis];
        saved[axis][0] = p->accum[0]; saved[axis][1] = p->accum[1];
        saved[axis][2] = p->base[0]; saved[axis][3] = p->base[1]; saved[axis][4] = p->base[2];
        saved[axis][5] = p->ctrl[0]; saved[axis][6] = p->ctrl[1];
        p->ctrl[0] = 2u | (3u << SIO_INTERP0_CTRL_LANE0_MASK_LSB_LSB) |
                     (14u << SIO_INTERP0_CTRL_LANE0_MASK_MSB_LSB) |
                     SIO_INTERP0_CTRL_LANE0_CROSS_INPUT_BITS;
        p->ctrl[1] = 1u | (31u << SIO_INTERP0_CTRL_LANE1_MASK_MSB_LSB);
        p->base[0] = axis ? base_q : base_i;
        p->base[1] = 0;
    }
    for (uint32_t w = 0; w < nwords; ++w) {
        const uint32_t cur = in[w];
        interp0_hw->accum[1] = (prev & 0xffffu) | cur << 16;
        interp1_hw->accum[1] = prev >> 16 | (cur & 0xffff0000u);
        prev = cur;
#pragma GCC unroll 16
        for (unsigned j = 0; j < 16; ++j) {
            *di++ = interp0_hw->pop[0];
            *dq++ = interp1_hw->pop[0];
        }
    }
    for (unsigned axis = 0; axis < 2; ++axis) {
        interp_hw_t *p = &interp_hw_array[axis];
        p->base[0] = saved[axis][2]; p->base[1] = saved[axis][3]; p->base[2] = saved[axis][4];
        p->ctrl[0] = saved[axis][5]; p->ctrl[1] = saved[axis][6];
        p->accum[0] = saved[axis][0]; p->accum[1] = saved[axis][1];
    }
}

void __attribute__((section(".scratch_y.iqdesc_pair_interp")))
iqdesc_pair_interp(uint32_t *di, uint32_t *dq, const uint32_t *in, uint32_t nwords,
                   uint32_t prev, uint32_t base_i, uint32_t base_q) {
    if (!nwords) return;
    uint32_t saved[2][7];
    for (unsigned axis = 0; axis < 2; ++axis) {
        interp_hw_t *p = &interp_hw_array[axis];
        saved[axis][0] = p->accum[0]; saved[axis][1] = p->accum[1];
        saved[axis][2] = p->base[0]; saved[axis][3] = p->base[1]; saved[axis][4] = p->base[2];
        saved[axis][5] = p->ctrl[0]; saved[axis][6] = p->ctrl[1];
        // ROR1 then bits4..16 selects source window bits5..17, shifted left by4.
        p->ctrl[0] = 1u | (4u << SIO_INTERP0_CTRL_LANE0_MASK_LSB_LSB) |
                     (16u << SIO_INTERP0_CTRL_LANE0_MASK_MSB_LSB) |
                     SIO_INTERP0_CTRL_LANE0_CROSS_INPUT_BITS;
        p->ctrl[1] = 2u | (31u << SIO_INTERP0_CTRL_LANE1_MASK_MSB_LSB);
        p->base[0] = axis ? base_q : base_i;
        p->base[1] = 0;
    }
    for (uint32_t w = 0; w < nwords; ++w) {
        const uint32_t cur = in[w];
        interp0_hw->accum[1] = (prev & 0xffffu) | cur << 16;
        interp1_hw->accum[1] = prev >> 16 | (cur & 0xffff0000u);
        prev = cur;
#pragma GCC unroll 8
        for (unsigned j = 0; j < 8; ++j) {
            *di++ = interp0_hw->pop[0];
            *dq++ = interp1_hw->pop[0];
        }
    }
    for (unsigned axis = 0; axis < 2; ++axis) {
        interp_hw_t *p = &interp_hw_array[axis];
        p->base[0] = saved[axis][2]; p->base[1] = saved[axis][3]; p->base[2] = saved[axis][4];
        p->ctrl[0] = saved[axis][5]; p->ctrl[1] = saved[axis][6];
        p->accum[0] = saved[axis][0]; p->accum[1] = saved[axis][1];
    }
}
#endif
