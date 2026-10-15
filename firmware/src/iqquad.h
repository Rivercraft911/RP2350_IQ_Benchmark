// Opt-in QPSK pulse profile: alpha0.20, L9, four samples/symbol, Kaiser beta0.8.
// Four chronological symbols per unflagged row; output PIO supplies IQ_FLAG.
#pragma once
#include <stdint.h>

enum {
    IQQUAD_SPAN = 9,
    IQQUAD_SPS = 4,
    IQQUAD_HISTORY_BITS = 12,
    IQQUAD_ROWS = 1u << IQQUAD_HISTORY_BITS,
    IQQUAD_ROW_WORDS = 8,
    IQQUAD_WORDS = IQQUAD_ROWS * IQQUAD_ROW_WORDS,
};

// Independent direct-FIR model, little-endian unflagged [4096][8] words.
#define IQQUAD_TABLE_CRC 0xd0cb695fu

// Key bit8 is the first symbol, bits9..11 are its three successors, and bit0
// is the oldest required bit. Each word packs two chronological 14-bit samples.
// The builder uses no temporary table or persistent sample storage.
void iqquad_build(uint32_t *quad);
