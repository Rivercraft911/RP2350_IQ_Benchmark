#include "iq32keys.h"

void iq32keys_encode(uint32_t *out, const uint32_t *input, uint32_t blocks,
                     const uint32_t *prev_word) {
    if (!blocks) return;
    uint32_t previous = prev_word ? *prev_word : 6u | 6u<<5 | 6u<<16 | 6u<<21;
    const uint32_t mask = 0x001f001fu;
    for (uint32_t b = 0; b < blocks; ++b) {
        const uint32_t current = input[b];
        const uint32_t a = previous & mask, c = current & mask;
        const uint32_t d = (current >> 5) & mask;
        const uint32_t old = (previous >> 5) & mask;
        out[2*b] = a + 17u*(old + 17u*c);
        out[2*b+1] = old + 17u*(c + 17u*d);
        previous = current;
    }
}

void iq32keys_history(uint32_t out[5], const uint32_t *coordinate_prev) {
    if (!coordinate_prev) {
        const uint32_t pilot = 1842u | 1842u<<16;
        for (unsigned i = 0; i < 5; ++i) out[i] = pilot;
        return;
    }
    uint32_t encoded[8];
    iq32keys_encode(encoded, coordinate_prev, 4, 0);
    for (unsigned i = 0; i < 5; ++i) out[i] = encoded[i+3];
}

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

void iq32keys_kernel_c(uint32_t *out, const uint32_t *input, uint32_t blocks,
                       const uint32_t *prev, const iq32native8_tables_t *tables) {
    if (!blocks) return;
    const uint32_t pilot = 1842u | 1842u<<16;
    uint32_t a = prev ? prev[0] : pilot;
    uint32_t c = prev ? prev[2] : pilot, d = prev ? prev[3] : pilot;
    uint32_t e = prev ? prev[4] : pilot;
    for (uint32_t block = 0; block < blocks; ++block) {
        const uint32_t first = input[2*block], last = input[2*block+1];
        const uint32_t *oi = tables->old + (a & 0xffffu)*2;
        const uint32_t *oq = tables->old + (a >> 16)*2;
        const uint32_t *mi = tables->middle + (d & 0xffffu)*2;
        const uint32_t *mq = tables->middle + (d >> 16)*2;
        const uint32_t *fi = tables->future + (last & 0xffffu)*2;
        const uint32_t *fq = tables->future + (last >> 16)*2;
        out[0] = add_lanes(add_lanes(oi[0],mi[0]),fi[0]);
        out[1] = add_lanes(add_lanes(oq[0],mq[0]),fq[0]);
        out[2] = add_lanes(add_lanes(oi[1],mi[1]),fi[1]);
        out[3] = add_lanes(add_lanes(oq[1],mq[1]),fq[1]);
        a=c; c=e; d=first; e=last;
        out += 4;
    }
}
