#ifndef R2_HAZARD3_BITS_H
#define R2_HAZARD3_BITS_H

#include <stdint.h>

// R2 only: Zbkb pack/zip replace the portable mapper's shuffle/dilation.
// Emulation exists for native correctness and an Arm harness build, not timing.
#if !defined(__riscv) && !defined(IQ_R2_HAZARD3_EMULATE)
#error "IQ_R2_HAZARD3 needs RISC-V or explicit IQ_R2_HAZARD3_EMULATE"
#endif

static inline uint32_t r2_hazard3_reverse32(uint32_t x) {
#if defined(__riscv) && !defined(IQ_R2_HAZARD3_EMULATE)
    __asm__("brev8 %0, %0\n\trev8 %0, %0" : "+r"(x));
    return x;
#else
    uint32_t result = 0;
    for (unsigned i = 0; i < 32; i++) result |= ((x >> i) & 1u) << (31u - i);
    return result;
#endif
}

static inline uint32_t r2_hazard3_zip16(uint32_t lo, uint32_t hi) {
#if defined(__riscv) && !defined(IQ_R2_HAZARD3_EMULATE)
    uint32_t result;
    __asm__("pack %0, %1, %2\n\tzip %0, %0" : "=r"(result) : "r"(lo), "r"(hi));
    return result;
#else
    uint32_t result = 0;
    for (unsigned i = 0; i < 16; i++) {
        result |= ((lo >> i) & 1u) << (2u * i);
        result |= ((hi >> i) & 1u) << (2u * i + 1u);
    }
    return result;
#endif
}

static inline uint32_t r2_hazard3_spread16(uint32_t x) {
#if defined(__riscv) && !defined(IQ_R2_HAZARD3_EMULATE)
    uint32_t result;
    __asm__("pack %0, %1, zero\n\tzip %0, %0" : "=r"(result) : "r"(x));
    return result;
#else
    return r2_hazard3_zip16(x, 0);
#endif
}

#endif
