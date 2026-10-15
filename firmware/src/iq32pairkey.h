// Dense radix17 pairs: same input bytes/symbol as iq32native8, shared tables.
#pragma once
#include "iq32native8.h"

#define IQ32PAIRKEY_INPUT_PROFILE "radix17_pair9_last5"

// Per-axis halfword: bits0..8 = first+17*last; bits9..13 = last.
// Two symbols/word, I in the low halfword and Q in the high halfword.
// Valid coordinate codes0..16 are required; bits14..15/30..31 are ignored.
// Conversion can be in place. Mapping can produce this format directly.
void iq32pairkey_convert(uint32_t *out, const uint32_t *native, uint32_t blocks);

// Same output, tables, four-word history, default pilot and zero-block contract
// as iq32native8. The input AND any non-NULL history use the pair format above.
void iq32pairkey_kernel_c(uint32_t *out, const uint32_t *input, uint32_t blocks,
                         const uint32_t *prev, const iq32native8_tables_t *tables);
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
void iq32pairkey_kernel_asm(uint32_t *out, const uint32_t *input, uint32_t blocks,
                           const uint32_t *prev, const iq32native8_tables_t *tables);
#endif
