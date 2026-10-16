#include "iq4prod_source.h"

#ifdef IQ_ON_DEVICE
#define SOURCE_HOT __attribute__((section(".time_critical.iq4prod_source")))
#else
#define SOURCE_HOT
#endif

void SOURCE_HOT iq4prod_source(uint32_t *i, uint32_t *q, unsigned words, uint32_t *seed) {
    uint32_t x = *seed;
    for (unsigned n = 0; n < words; ++n) {
        x ^= x << 13; x ^= x >> 17; x ^= x << 5;
        const uint32_t axis_i = x;
        x ^= x << 13; x ^= x >> 17; x ^= x << 5;
        // Code a|b<<1 has large magnitude iff a==b. Enforce opposite I/Q
        // magnitudes while retaining independent signs: d=~(a^b^c).
        i[n] = axis_i;
        q[n] = (x & 0x55555555u) | ((~(axis_i ^ (axis_i >> 1) ^ x) & 0x55555555u) << 1);
    }
    *seed = x;
}
