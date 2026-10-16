#include "iq4quad.h"
#include "iq4hist.h"
#include <limits.h>
#include <stddef.h>

static bool overlap(uintptr_t a, uintptr_t b, uintptr_t a_bytes, uintptr_t b_bytes) {
    return a < b + b_bytes && b < a + a_bytes;
}

static unsigned code_at(const uint32_t *source, unsigned symbol) {
    return (source[symbol / 16] >> (2 * (symbol % 16))) & 3u;
}

bool iq4quad_build(uint32_t *const rings[IQ4QUAD_STREAMS], unsigned ring_words,
                  const uint32_t *axis_i, const uint32_t *axis_q,
                  unsigned axis_words, uint32_t table_base, unsigned row_words) {
    if (!rings || !axis_i || !axis_q || !axis_words ||
        axis_words > UINT_MAX / 16u || ring_words != axis_words * 8u ||
        (ring_words & (ring_words - 1u)) || ring_words > UINT_MAX / 4u ||
        (row_words != 1 && row_words != 2) || (table_base & 3u) ||
        table_base > UINT32_MAX - (IQ4HIST_ROWS * row_words * 4u - 1u)) return false;
    const uintptr_t ring_bytes = (uintptr_t)ring_words * 4u;
    const uintptr_t source_bytes = (uintptr_t)axis_words * 4u;
    const uint32_t *source[] = {axis_i, axis_q};
    uint32_t *output[IQ4QUAD_STREAMS];
    for (unsigned axis = 0; axis < 2; ++axis) {
        const uintptr_t address = (uintptr_t)source[axis];
        if ((address & 3u) || address > UINTPTR_MAX - source_bytes) return false;
    }
    for (unsigned stream = 0; stream < IQ4QUAD_STREAMS; ++stream) {
        output[stream] = rings[stream];
        const uintptr_t address = (uintptr_t)output[stream];
        if (!address || (address & (ring_bytes - 1u)) ||
            address > UINTPTR_MAX - ring_bytes) return false;
        for (unsigned axis = 0; axis < 2; ++axis)
            if (overlap(address, (uintptr_t)source[axis], ring_bytes, source_bytes)) return false;
        for (unsigned previous = 0; previous < stream; ++previous)
            if (overlap(address, (uintptr_t)output[previous], ring_bytes, ring_bytes)) return false;
    }
    const unsigned symbols = axis_words * 16u;
    for (unsigned axis = 0; axis < 2; ++axis) {
        uint32_t key = 0;
        for (unsigned age = 0; age < IQ4HIST_SPAN - 1; ++age)
            key |= code_at(source[axis], symbols - 1u - age) << (2 * age);
        for (unsigned symbol = 0; symbol < symbols; ++symbol) {
            key = ((key << 2) | code_at(source[axis], symbol)) & (IQ4HIST_ROWS - 1u);
            output[2 * (symbol & 1u) + axis][symbol / 2] = table_base + key * row_words * 4u;
        }
    }
    return true;
}
