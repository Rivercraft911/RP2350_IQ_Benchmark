#include "iqpair.h"

void iqpair_build(uint32_t *paired, const uint32_t *axis_q) {
    for (uint32_t key = 0; key < IQPAIR_ROWS; ++key) {
        const uint32_t first = 2u * (key & 0xfffu);
        const uint32_t second = 2u * (key >> 1);
        paired[4u * key] = axis_q[first];
        paired[4u * key + 1] = axis_q[first + 1];
        paired[4u * key + 2] = axis_q[second];
        paired[4u * key + 3] = axis_q[second + 1];
    }
}
