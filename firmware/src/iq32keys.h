// Alternative32APSK input format: one rolling radix17 triple/complex symbol.
#pragma once
#include "iq32native8.h"

// Writes2*blocks key words from blocks iq32native8 coordinate words. A key
// word has I0..4912 in bits0..15,Q0..4912 in bits16..31, representing the
// three chronological coordinates ending at that symbol. prev_word contains
// the preceding two-coordinate word; NULL means repeated unit-pilot code6.
// Valid coordinate inputs are required; out/input must not overlap.
void iq32keys_encode(uint32_t *out, const uint32_t *input, uint32_t blocks,
                     const uint32_t *prev_word);

// Build the five preceding per-symbol keys from the standard four-word
// coordinate history. NULL means repeated unit-pilot history. No heap/state.
void iq32keys_history(uint32_t out[5], const uint32_t *coordinate_prev);

// The same tables, quantizer and RAW_PAIRS output as iq32native8. input now
// contains2*blocks per-symbol key words. prev holds five preceding key words,
// oldest first; NULL means repeated unit-pilot history. blocks still counts
// two-symbol groups. This doubles stream storage from2 to4 bytes/symbol.
void iq32keys_kernel_c(uint32_t *out, const uint32_t *input, uint32_t blocks,
                       const uint32_t *prev, const iq32native8_tables_t *tables);
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
void iq32keys_kernel_asm(uint32_t *out, const uint32_t *input, uint32_t blocks,
                         const uint32_t *prev, const iq32native8_tables_t *tables);
#endif
