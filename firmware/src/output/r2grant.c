#include "r2grant.h"

bool r2grant_plan(uint32_t *counts, unsigned capacity, unsigned published_words) {
    if (!counts || published_words > R2_GRANT_RING_WORDS ||
        published_words % R2_GRANT_WORDS || capacity < published_words / R2_GRANT_WORDS + 1u)
        return false;
    const unsigned batches = published_words / R2_GRANT_WORDS;
    for (unsigned i = 0; i < batches; ++i) counts[i] = R2_GRANT_WORDS;
    counts[batches] = 0;
    return true;
}

uint32_t r2grant_source(unsigned axis, unsigned word) {
    // Unique within each axis, with different axis prefixes and byte patterns.
    return (axis ? 0xa5c30000u : 0x5a3c0000u) ^ word * 0x9e3779b9u;
}
