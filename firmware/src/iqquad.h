// Experimental QPSK pulse profiles, alpha0.20 and four samples/symbol.
// Four chronological symbols per unflagged row; output PIO supplies IQ_FLAG.
#pragma once
#include <stdbool.h>
#include <stdint.h>

enum {
    IQQUAD_SPAN = 9,
    IQQUAD_SPS = 4,
    IQQUAD_HISTORY_BITS = 12,
    IQQUAD_ROWS = 1u << IQQUAD_HISTORY_BITS,
    IQQUAD_ROW_WORDS = 8,
    IQQUAD_WORDS = IQQUAD_ROWS * IQQUAD_ROW_WORDS,
    IQQUAD_L8_SPAN = 8,
    IQQUAD_L8_HISTORY_BITS = 11,
    IQQUAD_L8_ROWS = 1u << IQQUAD_L8_HISTORY_BITS,
    IQQUAD_L8_WORDS = IQQUAD_L8_ROWS * IQQUAD_ROW_WORDS,
};

// Independent direct-FIR model, little-endian unflagged [4096][8] words.
#define IQQUAD_TABLE_CRC 0xd0cb695fu
#define IQQUAD_L8_TABLE_CRC 0x2581bd84u

typedef enum {
    IQQUAD_PROFILE_L9_KAISER = 0,
    IQQUAD_PROFILE_L8_OPTIMIZED_BOTH = 1,
} iqquad_profile_t;

typedef struct {
    unsigned span;
    unsigned history_bits;
    unsigned rows;
    unsigned words;
    uint32_t table_crc;
    const char *id;
} iqquad_profile_info_t;

// Unknown profiles return NULL. Invalid/undersized builds leave the output untouched.
// L8 uses eleven-bit descriptor windows; existing iqdesc_quad backends use L9.
const iqquad_profile_info_t *iqquad_profile_info(iqquad_profile_t profile);
bool iqquad_build_profile(uint32_t *quad, unsigned word_capacity, iqquad_profile_t profile);

// Key bit(span-1) is the first symbol, followed by three successors; bit0 is
// oldest. Each word packs two chronological 14-bit samples. L9 remains default.
void iqquad_build(uint32_t *quad);
