// Binary fixtures for independent Python tests; no device or USB access.
#include "iq4hist.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { CANARY = 0x7ad4c781 };

int main(int argc, char **argv) {
    if (argc > 1 && !strcmp(argv[1], "pack")) {
        uint32_t seed = argc > 2 ? (uint32_t)strtoul(argv[2], NULL, 0) : 0x1234abcdu;
        unsigned groups = argc > 3 ? (unsigned)strtoul(argv[3], NULL, 0) : 137;
        for (unsigned g = 0; g < groups; ++g) {
            iq8_input_t in;
            uint32_t pair[2];
            iq8_fill(&in, 1, &seed);
            iq4hist_pack_group(&in, pair, pair + 1);
            if (fwrite(pair, sizeof pair, 1, stdout) != 1) return 4;
        }
        return 0;
    }
    static uint32_t memory[IQ4HIST_TABLE_WORDS + 2];
    for (unsigned k = 0; k < IQ4HIST_TABLE_WORDS + 2; ++k) memory[k] = CANARY;
    if (iq4hist_build(NULL, IQ4HIST_TABLE_WORDS) ||
        iq4hist_build(memory + 1, IQ4HIST_TABLE_WORDS - 1)) return 2;
    for (unsigned k = 0; k < IQ4HIST_TABLE_WORDS + 2; ++k)
        if (memory[k] != CANARY) return 3;
    if (!iq4hist_build(memory + 1, IQ4HIST_TABLE_WORDS) ||
        memory[0] != CANARY || memory[IQ4HIST_TABLE_WORDS + 1] != CANARY) return 3;
    return fwrite(memory + 1, sizeof memory[0], IQ4HIST_TABLE_WORDS, stdout) == IQ4HIST_TABLE_WORDS ? 0 : 4;
}
