// Exhaustive N5 table fixture, including padding and failure guards; no device.
#include "iq5hist.h"
#include <stdio.h>

enum { CANARY = 0x7ad4c781 };

int main(void) {
    static uint32_t memory[IQ5HIST_TABLE_WORDS + 2];
    for (unsigned k = 0; k < IQ5HIST_TABLE_WORDS + 2; ++k) memory[k] = CANARY;
    if (iq5hist_build(NULL, IQ5HIST_TABLE_WORDS) ||
        iq5hist_build(memory + 1, IQ5HIST_TABLE_WORDS - 1)) return 2;
    for (unsigned k = 0; k < IQ5HIST_TABLE_WORDS + 2; ++k)
        if (memory[k] != CANARY) return 3;
    if (!iq5hist_build(memory + 1, IQ5HIST_TABLE_WORDS) ||
        memory[0] != CANARY || memory[IQ5HIST_TABLE_WORDS + 1] != CANARY) return 3;
    uint32_t crc = 0xffffffffu;
    for (unsigned k = 0; k < IQ5HIST_TABLE_WORDS; ++k) {
        if ((k & 1u) && (memory[k + 1] & 0xffffff00u)) return 4;
        for (unsigned byte = 0; byte < 4; ++byte) {
            crc ^= (memory[k + 1] >> (8 * byte)) & 255u;
            for (unsigned bit = 0; bit < 8; ++bit)
                crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
        }
    }
    if ((crc ^ 0xffffffffu) != IQ5HIST_TABLE_CRC) return 5;
    return fwrite(memory + 1, sizeof memory[0], IQ5HIST_TABLE_WORDS, stdout) ==
        IQ5HIST_TABLE_WORDS ? 0 : 6;
}
