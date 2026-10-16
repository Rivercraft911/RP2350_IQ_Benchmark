// Exact native-coordinate 32APSK component: L8, two samples/symbol, K2.
#pragma once
#include <stdbool.h>
#include <stdint.h>

enum {
    IQ32NATIVE8_SPAN = 8, IQ32NATIVE8_SPS = 2, IQ32NATIVE8_SYMBOLS = 2,
    IQ32NATIVE8_LEVELS = 17, IQ32NATIVE8_ROWS = 4913,
    IQ32NATIVE8_ROW_WORDS = 2,
    IQ32NATIVE8_TABLE_WORDS = IQ32NATIVE8_ROWS * IQ32NATIVE8_ROW_WORDS,
    IQ32NATIVE8_DEFAULT_RATE = 2, // 5/6: gamma1=2.64, gamma2=4.64
};
typedef struct {
    uint32_t *old, *middle, *future; // each39,304 bytes; total117,912
} iq32native8_tables_t;

#define IQ32NATIVE8_PROFILE "apsk32_l8_n2_native333"
#define IQ32NATIVE8_PULSE_PROFILE "psk8_l8_n2_hold_m010_far2"
#define IQ32NATIVE8_COEFFICIENT_SHA256 "16936cb50f5de62e68b10367624b868afea4ad49f8dfc4481526de4712fda5ae"

// R3=1. Rate indices0..4 are3/4,4/5,5/6,8/9,9/10. Coordinates are
// code0=0;1..8=[inner diagonal,middle sin15,middle diagonal,middle cos15,
// outer sin22.5,outer diagonal,outer cos22.5,1];9..16 negate1..8.
// Unit-radius header/pilot coordinates use codes6/14. Gold quarter-turns
// only exchange/negate codes, so preserve this alphabet. Builds reject an
// invalid rate or missing table before writing any table memory.
bool iq32native8_build(const iq32native8_tables_t *tables, unsigned rate);
const int32_t *iq32native8_coefficients(void);

// Two chronological5-bit coordinates/axis: I bits0..9,Q bits16..25.
// Codes17..31 are invalid; the hot leaf requires valid input. Ignore bits
//10..15/26..31. prev is four preceding words, oldest first; NULL means
// repeated unit-pilot code6. The oldest of those eight symbols is unused.
// Output has blocks*4 RAW_PAIRS words: I01,Q01,I23,Q23 for each two-symbol
// group. PIO must strip each lane's upper2 bits and provide IQ_FLAG.
// Three independently rounded partial sums: ≤1.5LSB/axis chunk-rounding
// error versus the underlying Q8 FIR, plus product-rounding error.
void iq32native8_kernel_c(uint32_t *out, const uint32_t *input, uint32_t blocks,
                          const uint32_t *prev, const iq32native8_tables_t *tables);
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
// Cached-flash leaf; first at most four blocks use C for contiguous history.
void iq32native8_kernel_asm(uint32_t *out, const uint32_t *input, uint32_t blocks,
                            const uint32_t *prev, const iq32native8_tables_t *tables);
#endif

// Deterministic xorshift fixture over the native4+12+16 geometric points.
// Fixture order is ring/angle order, not a DVB-S2 bit-label mapper.
void iq32native8_fill(uint32_t *input, uint32_t blocks, uint32_t *seed);
