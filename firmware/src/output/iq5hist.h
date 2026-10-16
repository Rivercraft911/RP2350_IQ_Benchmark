// Opt-in L7/N5 four-level pulse table for a 64 MSPS / 12.8 Msym/s DAC.
#pragma once
#include <stdbool.h>
#include <stdint.h>

enum {
    IQ5HIST_SPAN = 7,
    IQ5HIST_SPS = 5,
    IQ5HIST_ROWS = 1u << (2 * IQ5HIST_SPAN),
    IQ5HIST_ROW_WORDS = 2,
    IQ5HIST_TABLE_WORDS = IQ5HIST_ROWS * IQ5HIST_ROW_WORDS,
};
#define IQ5HIST_PROFILE "8psk_l7_n5_hold_m010_far2"
#define IQ5HIST_SCALE_DEN 3388u
#define IQ5HIST_TABLE_CRC 0xa299c7c1u

// Caller owns 128 KiB. Newest two-bit coordinate code is in low bits.
// Codes0..3 select +cos(pi/8), -sin(pi/8), +sin(pi/8), -cos(pi/8).
// Five chronological signed bytes occupy word0 bytes0..3, word1 byte0;
// word1 bytes1..3 are zero padding and must never reach the DAC.
// Round once: floor((Q8*Q30 FIR sum + denominator/2)/denominator), where
// denominator = IQ5HIST_SCALE_DEN * 2^32. Ties round toward +infinity.
// Drive D13..D6, holding D5..D0 zero, as in the N4 8-bit table.
// Invalid/undersized builds return false without touching the output.
bool iq5hist_build(uint32_t *table, unsigned word_capacity);
const int32_t *iq5hist_coefficients(void); // 35 frozen Q8 taps
