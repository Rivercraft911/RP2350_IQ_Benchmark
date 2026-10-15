// Native binary descriptor output. No descriptor is dereferenced as a host pointer.
#include "iqdesc.h"
#include <stdio.h>
#include <stdlib.h>

enum { WORDS = 137 };
int main(int argc, char **argv) {
    uint32_t seed = argc > 1 ? (uint32_t)strtoul(argv[1], 0, 0) : 0x1234abcdu;
    unsigned block = argc > 2 ? (unsigned)strtoul(argv[2], 0, 0) : WORDS;
    uint32_t bi = argc > 3 ? (uint32_t)strtoul(argv[3], 0, 0) : 0x20040000u;
    uint32_t bq = argc > 4 ? (uint32_t)strtoul(argv[4], 0, 0) : 0x20048000u;
    unsigned paired = argc > 5 ? (unsigned)strtoul(argv[5], 0, 0) : 0;
    if (paired > 1) return 2;
    const unsigned rows_per_word = paired ? 8 : 16;
    void (*kernel)(uint32_t *, uint32_t *, const uint32_t *, uint32_t, uint32_t, uint32_t, uint32_t) =
        paired ? iqdesc_pair_c : iqdesc_c;
    if (!block || block > WORDS) return 2;
    uint32_t in[WORDS], di[WORDS * 16 + 1], dq[WORDS * 16 + 1];
    const unsigned count = WORDS * rows_per_word;
    di[count] = 0x13579bdfu; dq[count] = 0x2468ace0u;
    for (unsigned w = 0; w < WORDS; ++w) {
        seed ^= seed << 13; seed ^= seed >> 17; seed ^= seed << 5;
        in[w] = seed;
    }
    // Guard words check that the zero-length call leaves both arrays unchanged.
    di[0] = 0x10203040u; dq[0] = 0xabcdef01u;
    kernel(di, dq, in, 0, 0, bi, bq);
    if (di[0] != 0x10203040u || dq[0] != 0xabcdef01u) return 3;
    for (unsigned pos = 0; pos < WORDS; pos += block) {
        unsigned n = WORDS - pos;
        if (n > block) n = block;
        kernel(di + pos * rows_per_word, dq + pos * rows_per_word, in + pos, n,
               pos ? in[pos - 1] : 0, bi, bq);
    }
    if (di[count] != 0x13579bdfu || dq[count] != 0x2468ace0u) return 6;
    if (fwrite(di, sizeof di[0], WORDS * rows_per_word, stdout) != WORDS * rows_per_word) return 4;
    return fwrite(dq, sizeof dq[0], WORDS * rows_per_word, stdout) == WORDS * rows_per_word ? 0 : 5;
}
