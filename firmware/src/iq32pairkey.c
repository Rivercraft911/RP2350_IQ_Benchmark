#include "iq32pairkey.h"

void iq32pairkey_convert(uint32_t *out, const uint32_t *native, uint32_t blocks) {
    for (uint32_t b = 0; b < blocks; ++b) {
        const uint32_t first = native[b] & 0x001f001fu;
        const uint32_t last = (native[b] >> 5) & 0x001f001fu;
        out[b] = first + 17u*last + (last << 9);
    }
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

void iq32pairkey_kernel_c(uint32_t *out, const uint32_t *input, uint32_t blocks,
                         const uint32_t *prev, const iq32native8_tables_t *tables) {
    if (!blocks) return;
    const uint32_t pilot = 0x0c6c0c6cu; // first=last=unit-pilot code6
    uint32_t a = prev ? prev[0] : pilot, b = prev ? prev[1] : pilot;
    uint32_t c = prev ? prev[2] : pilot, d = prev ? prev[3] : pilot;
    for (uint32_t block = 0; block < blocks; ++block) {
        const uint32_t e = input[block];
        const uint32_t last_d = (d >> 9) & 0x001f001fu;
        const uint32_t first_d = (d & 0x01ff01ffu) - 17u*last_d;
        const uint32_t old = ((a >> 9) & 0x001f001fu) + 17u*(b & 0x01ff01ffu);
        const uint32_t mid = (c & 0x01ff01ffu) + 289u*first_d;
        const uint32_t fresh = last_d + 17u*(e & 0x01ff01ffu);
        const uint32_t *oi = tables->old + (old & 0xffffu)*2;
        const uint32_t *oq = tables->old + (old >> 16)*2;
        const uint32_t *mi = tables->middle + (mid & 0xffffu)*2;
        const uint32_t *mq = tables->middle + (mid >> 16)*2;
        const uint32_t *fi = tables->future + (fresh & 0xffffu)*2;
        const uint32_t *fq = tables->future + (fresh >> 16)*2;
        out[0] = add_lanes(add_lanes(oi[0], mi[0]), fi[0]);
        out[1] = add_lanes(add_lanes(oq[0], mq[0]), fq[0]);
        out[2] = add_lanes(add_lanes(oi[1], mi[1]), fi[1]);
        out[3] = add_lanes(add_lanes(oq[1], mq[1]), fq[1]);
        a=b; b=c; c=d; d=e;
        out += 4;
    }
}
