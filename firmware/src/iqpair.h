// Two-symbol rows for the existing binary-axis QPSK L12, N4 waveform.
// One shared unflagged table serves both axes; the output PIO supplies IQ_FLAG.
#pragma once
#include <stdint.h>

enum {
    IQPAIR_HISTORY_BITS = 13,
    IQPAIR_ROWS = 1u << IQPAIR_HISTORY_BITS,
    IQPAIR_ROW_WORDS = 4,
    IQPAIR_WORDS = IQPAIR_ROWS * IQPAIR_ROW_WORDS,
};

// axis_q is the existing unflagged [4096][2] table, with phase pairs packed low first.
// paired has IQPAIR_WORDS uint32_t entries (128 KiB), and must not overlap axis_q.
// Key bit12 is the second symbol, bit11 the first, and bit0 the oldest required bit.
// A row contains first-symbol phases0..3 followed by second-symbol phases0..3.
// The two L12 histories are therefore key&4095 and key>>1, respectively.
void iqpair_build(uint32_t *paired, const uint32_t *axis_q);
