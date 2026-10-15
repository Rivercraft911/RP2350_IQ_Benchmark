// Pin-free PIO pointer generator. Shared with the native instruction-model test.
#pragma once
#include <stdint.h>

enum { PTRPROG_PUBLISH = 0, PTRPROG_SKIP = 10, PTRPROG_LENGTH = 16,
       PTRPROG_PULL_BITS = 16, PTRPROG_HISTORY_BITS = 13 };

// IN shifts left; OUT shifts right, with manual PULL and empty threshold16.
// X holds the history, newest bit0. Y is a 128-KiB-aligned numerical base >>17.
// Axis input is pair-swapped: bits1,0 are chronological symbols0,1; bits3,2 are2,3.
// DMA16 writes replicate each halfword into both halves of a TX FIFO word; only16 are used.
// Start at PUBLISH or SKIP to produce even/odd two-symbol rows of the same axis stream.
static const uint16_t PTRPROG_CODE[PTRPROG_LENGTH] = {
    0x00e2, // publish: JMP !OSRE, publish_have
    0x80a0, //          PULL BLOCK
    0xa0c1, // publish_have: MOV ISR, X
    0x40e2, //          IN OSR, 2
    0x6062, //          OUT NULL, 2
    0xa026, //          MOV X, ISR
    0xa0c2, //          MOV ISR, Y
    0x402d, //          IN X, 13
    0x4064, //          IN NULL, 4
    0x8020, //          PUSH BLOCK
    0x00ec, // skip:    JMP !OSRE, skip_have
    0x80a0, //          PULL BLOCK
    0xa0c1, // skip_have: MOV ISR, X
    0x40e2, //          IN OSR, 2
    0x6062, //          OUT NULL, 2
    0xa026, //          MOV X, ISR; wrap to publish
};

static inline uint16_t ptrprog_pair_swap(uint16_t v) {
    return (uint16_t)(((v & 0x5555u) << 1) | ((v & 0xaaaau) >> 1));
}
