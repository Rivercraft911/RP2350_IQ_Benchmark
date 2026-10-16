// Alternate-row, pin-free PIO pointers from a complete packed2-bit axis stream.
#pragma once
#include <stdint.h>

enum {
    IQ4PROD_PROGRAM_LENGTH = 11,
    IQ4PROD_HISTORY_ENTRY = 7,
    IQ4PROD_SYMBOLS_PER_WORD = 16,
    IQ4PROD_CYCLES_PER_WORD = 8 * IQ4PROD_PROGRAM_LENGTH,
    IQ4PROD_TABLE_BYTES = 64 * 1024,
};

// SMs within each PIO block share one copy: I-even,Q-even start at0;
// I-odd,Q-odd start at7. Each occupied PIO block needs11 instruction words.
// Both parities consume the complete chronological stream,16 codes/word,
// earliest in bits1:0. X holds history (newest low2), Y is table_base>>16.
// IN shifts left with autopush16; OUT shifts right with autopull32. MOV ISR
// clears the IN counter before each history/address construction. OUT X is
// an autopull fence at every code: an empty source cannot reuse stale OSR bits
// (RP2350 section11.5.4, printed pp909-910). No MOV/IN reads OSR.
static const uint16_t IQ4PROD_PROGRAM[IQ4PROD_PROGRAM_LENGTH] = {
    0xa0c1, // MOV ISR,X
    0x6022, // OUT X,2
    0x4022, // IN X,2
    0xa026, // MOV X,ISR
    0xa0c2, // MOV ISR,Y
    0x402e, // IN X,14
    0x4062, // IN NULL,2; autopush16 (one four-byte row address)
    0xa0c1, // history: MOV ISR,X
    0x6022, // OUT X,2
    0x4022, // IN X,2; count2, so history-only never autopushes
    0xa026, // MOV X,ISR; wrap
};

// Seed the last six chronological codes of the immediately preceding word.
// For odd parity, entry7 consumes symbol0 before the first address (symbol1).
uint32_t iq4prod_seed(uint32_t previous);
// Invalid parity returns PROGRAM_LENGTH, which is never a valid entry point.
unsigned iq4prod_entry(unsigned parity);
