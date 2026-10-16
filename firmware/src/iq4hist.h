// Opt-in four-level full-history shaper, globally pi/8-rotated QPSK/8PSK.
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "iq8.h"

enum {
    IQ4HIST_SPAN = 7,
    IQ4HIST_SPS = 4,
    IQ4HIST_HISTORY_BITS = 2 * IQ4HIST_SPAN,
    IQ4HIST_ROWS = 1u << IQ4HIST_HISTORY_BITS,
    IQ4HIST_ROW_WORDS = IQ4HIST_SPS / 2,
    IQ4HIST_TABLE_WORDS = IQ4HIST_ROWS * IQ4HIST_ROW_WORDS,
    IQ4HIST8_ROW_WORDS = 1,
    IQ4HIST8_TABLE_WORDS = IQ4HIST_ROWS,
};

#define IQ4HIST_PROFILE "qpsk_l7_n4_hold_m010_far2"
// Independent scalar FIR, little-endian unflagged sample pairs.
#define IQ4HIST_TABLE_CRC 0x19bf3c1du
#define IQ4HIST8_TABLE_CRC 0x5e13245cu
#define IQ4HIST8_SCALE_DEN 3399u

// Caller owns 128 KiB. Key bits2k..2k+1 hold symbol age k (newest in low2).
// Code = sign_A | sign_B<<1, using iq8.h's planes: codes0..3 are
// +cos(pi/8), -sin(pi/8), +sin(pi/8), -cos(pi/8). One final rounding follows
// the Q8-coefficient x Q30-level FIR sum. Words pack samples01 then23 as
// 14-bit two's-complement halfwords, with neither IQ_FLAG nor spare bits.
// Invalid/undersized builds return false without touching the output.
bool iq4hist_build(uint32_t *table, unsigned word_capacity);
const int32_t *iq4hist_coefficients(void); // 28 frozen Q8 taps

// B4: caller owns 64 KiB. Four chronological signed 8-bit samples per word,
// least-significant byte first. Same FIR, scaled by 64/3399 relative to the
// 14-bit samples, rounded once to nearest (ties toward +infinity). Peak +/-127.
// Drive bytes on AFE D13..D6 in two's-complement mode, with D5..D0 held zero.
// Invalid/undersized builds return false without touching the output.
bool iq4hist8_build(uint32_t *table, unsigned word_capacity);

// Convert one existing 16-symbol signed-plane group into one word per axis.
// Chronological symbols occupy successive low-to-high 2-bit fields, for PIO
// OUT2 with a right-shifting OSR. No constellation remapping or history reset.
void iq4hist_pack_group(const iq8_input_t *group, uint32_t *i, uint32_t *q);
