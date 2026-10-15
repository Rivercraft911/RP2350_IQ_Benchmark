#include "iq32offsets.h"
#include "iqlanes.h"

void iq32offsets_encode(uint32_t *out, const uint32_t *keys, uint32_t count) {
    for (uint32_t i = 0; i < count; ++i) out[i] = keys[i] << 3;
}

static inline const uint32_t *row(const uint32_t *table, uint32_t offset) {
    return (const uint32_t *)((const uint8_t *)table + offset);
}

void iq32offsets_kernel_c(uint32_t *out, const uint32_t *input, uint32_t blocks,
                          const uint32_t *prev, const iq32native8_tables_t *tables) {
    if (!blocks) return;
    const uint32_t pilot = 14736u | 14736u << 16;
    uint32_t a = prev ? prev[0] : pilot;
    uint32_t c = prev ? prev[2] : pilot, d = prev ? prev[3] : pilot;
    uint32_t e = prev ? prev[4] : pilot;
    for (uint32_t block = 0; block < blocks; ++block) {
        const uint32_t first = input[2 * block], last = input[2 * block + 1];
        const uint32_t *oi = row(tables->old, a & 0xffffu);
        const uint32_t *oq = row(tables->old, a >> 16);
        const uint32_t *mi = row(tables->middle, d & 0xffffu);
        const uint32_t *mq = row(tables->middle, d >> 16);
        const uint32_t *fi = row(tables->future, last & 0xffffu);
        const uint32_t *fq = row(tables->future, last >> 16);
        out[0] = add_lanes(add_lanes(oi[0], mi[0]), fi[0]);
        out[1] = add_lanes(add_lanes(oq[0], mq[0]), fq[0]);
        out[2] = add_lanes(add_lanes(oi[1], mi[1]), fi[1]);
        out[3] = add_lanes(add_lanes(oq[1], mq[1]), fq[1]);
        a = c; c = e; d = first; e = last;
        out += 4;
    }
}
