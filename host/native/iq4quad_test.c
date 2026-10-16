// Binary cyclic-pointer fixtures and argument guards; no device access.
#include "iq4quad.h"
#include "iq4hist.h"
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { AXIS_WORDS_MAX = 256, RING_WORDS_MAX = AXIS_WORDS_MAX * 8, CANARY = 0x7ad4c781 };
static _Alignas(8192) uint32_t memory[IQ4QUAD_STREAMS * RING_WORDS_MAX + 1];
static uint32_t source[2][AXIS_WORDS_MAX];

static bool untouched(void) {
    for (unsigned k = 0; k < sizeof memory / sizeof memory[0]; ++k)
        if (memory[k] != CANARY) return false;
    return true;
}

int main(int argc, char **argv) {
    if (argc == 2 && (!strcmp(argv[1], "table8") || !strcmp(argv[1], "table14"))) {
        static uint32_t table[IQ4HIST_TABLE_WORDS];
        const bool bytes = !strcmp(argv[1], "table8");
        const unsigned words = bytes ? IQ4HIST8_TABLE_WORDS : IQ4HIST_TABLE_WORDS;
        if (!(bytes ? iq4hist8_build(table, words) : iq4hist_build(table, words))) return 2;
        return fwrite(table, sizeof table[0], words, stdout) == words ? 0 : 3;
    }
    if (argc != 5) return 1;
    uint32_t seed = (uint32_t)strtoul(argv[1], NULL, 0);
    const unsigned axis_words = (unsigned)strtoul(argv[2], NULL, 0);
    const uint32_t table_base = (uint32_t)strtoul(argv[3], NULL, 0);
    const unsigned row_words = (unsigned)strtoul(argv[4], NULL, 0);
    if (!axis_words || axis_words > AXIS_WORDS_MAX || (axis_words & (axis_words - 1))) return 1;
    const unsigned ring_words = axis_words * 8;
    uint32_t *rings[IQ4QUAD_STREAMS];
    for (unsigned k = 0; k < IQ4QUAD_STREAMS; ++k) rings[k] = memory + k * ring_words;
    for (unsigned k = 0; k < sizeof memory / sizeof memory[0]; ++k) memory[k] = CANARY;
    for (unsigned word = 0; word < axis_words; ++word) {
        iq8_input_t group;
        iq8_fill(&group, 1, &seed);
        iq4hist_pack_group(&group, source[0] + word, source[1] + word);
    }
#define REJECT(r, n, i, q, w, b, rw) do { \
    if (iq4quad_build((r), (n), (i), (q), (w), (b), (rw)) || !untouched()) return 4; \
} while (0)
    REJECT(NULL, ring_words, source[0], source[1], axis_words, table_base, row_words);
    REJECT(rings, ring_words, NULL, source[1], axis_words, table_base, row_words);
    REJECT(rings, ring_words, source[0], NULL, axis_words, table_base, row_words);
    REJECT(rings, 0, source[0], source[1], 0, table_base, row_words);
    REJECT(rings, ring_words - 1, source[0], source[1], axis_words, table_base, row_words);
    REJECT(rings, 24, source[0], source[1], 3, table_base, row_words);
    REJECT(rings, ring_words, source[0], source[1], UINT_MAX, table_base, row_words);
    REJECT(rings, ring_words, source[0], source[1], axis_words, table_base, 0);
    REJECT(rings, ring_words, source[0], source[1], axis_words, table_base, 3);
    REJECT(rings, ring_words, source[0], source[1], axis_words, table_base | 1u, row_words);
    REJECT(rings, ring_words, source[0], source[1], axis_words, 0xffffff00u, row_words);
    REJECT(rings, ring_words, memory, source[1], axis_words, table_base, row_words);
    REJECT(rings, ring_words, source[0], (uint32_t *)((uintptr_t)source[1] + 1),
           axis_words, table_base, row_words);
    for (unsigned stream = 0; stream < IQ4QUAD_STREAMS; ++stream) {
        uint32_t *saved = rings[stream];
        rings[stream] = NULL;
        REJECT(rings, ring_words, source[0], source[1], axis_words, table_base, row_words);
        rings[stream] = saved + 1;
        REJECT(rings, ring_words, source[0], source[1], axis_words, table_base, row_words);
        rings[stream] = saved;
    }
    rings[3] = rings[0];
    REJECT(rings, ring_words, source[0], source[1], axis_words, table_base, row_words);
    rings[3] = memory + 3 * ring_words;
#undef REJECT
    if (!iq4quad_build(rings, ring_words, source[0], source[1], axis_words, table_base, row_words) ||
        !iq4quad_build(rings, ring_words, source[0], source[1], axis_words, table_base, row_words)) return 5;
    for (unsigned k = IQ4QUAD_STREAMS * ring_words; k < sizeof memory / sizeof memory[0]; ++k)
        if (memory[k] != CANARY) return 6;
    for (unsigned axis = 0; axis < 2; ++axis)
        if (fwrite(source[axis], sizeof source[0][0], axis_words, stdout) != axis_words) return 7;
    return fwrite(memory, sizeof memory[0], IQ4QUAD_STREAMS * ring_words, stdout) ==
        IQ4QUAD_STREAMS * ring_words ? 0 : 7;
}
