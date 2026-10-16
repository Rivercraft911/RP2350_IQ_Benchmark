// Binary fixtures for the exhaustive B4 model; no device access.
#include "iq4hist.h"
#include <stdio.h>

enum { CANARY = 0x7ad4c781 };

int main(void) {
    static uint32_t memory[IQ4HIST8_TABLE_WORDS + 2];
    for (unsigned k = 0; k < IQ4HIST8_TABLE_WORDS + 2; ++k) memory[k] = CANARY;
    if (iq4hist8_build(NULL, IQ4HIST8_TABLE_WORDS) ||
        iq4hist8_build(memory + 1, IQ4HIST8_TABLE_WORDS - 1)) return 2;
    for (unsigned k = 0; k < IQ4HIST8_TABLE_WORDS + 2; ++k)
        if (memory[k] != CANARY) return 3;
    if (!iq4hist8_build(memory + 1, IQ4HIST8_TABLE_WORDS) ||
        memory[0] != CANARY || memory[IQ4HIST8_TABLE_WORDS + 1] != CANARY) return 3;
    return fwrite(memory + 1, sizeof memory[0], IQ4HIST8_TABLE_WORDS, stdout) ==
        IQ4HIST8_TABLE_WORDS ? 0 : 4;
}
