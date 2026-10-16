// Experimental native-coordinate 16APSK L8 / two samples per symbol shaper.
// Three input-aligned chunks remove key/sample reversals. Digital component only.
#pragma once
#include <stdbool.h>
#include <stdint.h>

enum {
    IQ16NATIVE8_SPAN = 8,
    IQ16NATIVE8_SPS = 2,
    IQ16NATIVE8_SYMBOLS = 4,
    IQ16NATIVE8_ROW_WORDS = 4,
    IQ16NATIVE8_OLD_ROWS = 512,
    IQ16NATIVE8_FULL_ROWS = 4096,
    IQ16NATIVE8_OLD_WORDS = IQ16NATIVE8_OLD_ROWS * IQ16NATIVE8_ROW_WORDS,
    IQ16NATIVE8_FULL_WORDS = IQ16NATIVE8_FULL_ROWS * IQ16NATIVE8_ROW_WORDS,
    IQ16NATIVE8_DEFAULT_GAMMA100 = 270,
};
typedef struct {
    uint32_t *old, *middle, *future; // caller-owned 8 KiB, 64 KiB, 64 KiB
} iq16native8_tables_t;

#define IQ16NATIVE8_PROFILE "apsk16_l8_n2_hold_m010_far2_native344"
#define IQ16NATIVE8_PULSE_PROFILE "psk8_l8_n2_hold_m010_far2"
#define IQ16NATIVE8_COEFFICIENT_SHA256 "16936cb50f5de62e68b10367624b868afea4ad49f8dfc4481526de4712fda5ae"
#define IQ16NATIVE8_OLD_CRC 1009880281u
#define IQ16NATIVE8_MIDDLE_CRC 2648433961u
#define IQ16NATIVE8_FUTURE_CRC 3030949170u
// iq16chunk_fill, seed0x1234abcd, 8192 input words, repeated unit-pilot history.
// Independently checked scalar FIR/chunk-rounding fixture, no14-bit mask/IQ flag.
#define IQ16NATIVE8_FIXTURE_RAW_PAIRS_CRC 2596089319u
#define IQ16NATIVE8_FIXTURE_PACKED_CRC 4290221674u

// Coordinate code, input packing and prev are identical to iq16chunk.h:
// code0..3 positive [1/sqrt2,cos15deg,sin15deg,1/(gamma*sqrt2)], code4..7 negate;
// four chronological 3-bit codes at I bits0..11 / Q bits16..27 of each word.
// prev points to the two preceding packed words (eight symbols), oldest first;
// NULL means repeated code0. Ignore bits12..15 and28..31. Zero blocks touches nothing.
// The oldest of those eight history symbols is outside the L8 filter support.
//
// Each output is the sum of separately rounded 3/4/4-coordinate partials. This
// has the same linear Q8 FIR as the72KiB symmetric4/3/4 layout, but can differ by
// one LSB per axis due to a different rounding boundary. It is a separate profile.
// Output blocks*8 words are RAW_PAIRS: I01,Q01,I23,Q23,... signed16 lanes.
// The output PIO must discard each lane's upper2 bits and supply IQ_FLAG.
//
// gamma100 must be315,285,275,270,260 or257. Invalid builds leave tables unchanged.
bool iq16native8_build(const iq16native8_tables_t *tables, unsigned gamma100);
const int32_t *iq16native8_coefficients(void); // 16 signed Q8 taps, frozen profile
void iq16native8_kernel_c(uint32_t *out, const uint32_t *input, uint32_t blocks,
                          const uint32_t *prev, const iq16native8_tables_t *tables);
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
// No peripheral state. FLASH leaf, 48-byte AAPCS frame; first at most two blocks
// use C to establish contiguous history, then six four-word row bursts per block.
void iq16native8_kernel_asm(uint32_t *out, const uint32_t *input, uint32_t blocks,
                            const uint32_t *prev, const iq16native8_tables_t *tables);
#endif
