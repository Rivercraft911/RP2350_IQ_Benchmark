#include "iq32offsets_native.h"

static inline uint32_t offset(uint32_t a, uint32_t b, uint32_t c) {
    return (a + 17u * (b + 17u * c)) << 3;
}

void iq32offsets_from_native(uint32_t *out, const uint32_t *input, uint32_t blocks,
                             const uint32_t previous[4]) {
    if (!blocks) return;
    const uint32_t mask = 0x001f001fu;
    const uint32_t pilot = 6u | 6u << 5 | 6u << 16 | 6u << 21;
    uint32_t last = pilot;
    if (previous) {
        uint32_t current = previous[1];
        out[0] = offset((previous[0] >> 5) & mask, current & mask, (current >> 5) & mask);
        last = current;
        for (unsigned word = 2; word < 4; ++word) {
            current = previous[word];
            const uint32_t a = current & mask, b = (current >> 5) & mask;
            out[2*word-3] = offset(last & mask, (last >> 5) & mask, a);
            out[2*word-2] = offset((last >> 5) & mask, a, b);
            last = current;
        }
    } else {
        for (unsigned word = 0; word < 5; ++word) out[word] = 14736u | 14736u << 16;
    }
    out += 5;
    do {
        const uint32_t current = *input++;
        const uint32_t a = current & mask, b = (current >> 5) & mask;
        const uint32_t before = (last >> 5) & mask;
        out[0] = offset(last & mask, before, a);
        out[1] = offset(before, a, b);
        last = current;
        out += 2;
    } while (--blocks);
}
