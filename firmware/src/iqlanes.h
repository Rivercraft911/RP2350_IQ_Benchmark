// Packed signed 16-bit lane arithmetic shared by the C shaper kernels and their host models.
#pragma once
#include <stdint.h>

// Lane-wise a + b on two int16 halves, wrapping modulo 2^16 per lane (one SADD16 on the M33).
static inline uint32_t add_lanes(uint32_t a, uint32_t b) {
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    uint32_t sum;
    __asm("sadd16 %0, %1, %2" : "=r"(sum) : "r"(a), "r"(b) : "cc");
    return sum;
#else
    return (uint32_t)(uint16_t)((int16_t)a + (int16_t)b) |
        (uint32_t)(uint16_t)((int16_t)(a >> 16) + (int16_t)(b >> 16)) << 16;
#endif
}
