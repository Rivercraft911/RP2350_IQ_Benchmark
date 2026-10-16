// Native two-symbol coordinates directly to rolling byte offsets.
#pragma once
#include <stdint.h>
// out needs 5+2*blocks words: five preceding keys, then current per-symbol keys.
// previous is four chronological native history words, or NULL for unit pilots.
// Valid native coordinate codes 0..16; input/output must not overlap.
// Zero blocks touches no pointers and writes no history prefix.
void iq32offsets_from_native(uint32_t *out, const uint32_t *input, uint32_t blocks,
                             const uint32_t previous[4]);
// A3 opt-in: identical output for every input word, with the packed radix-17
// key built from one shared partial product (u = s[-1] + 17 s[0]) instead of two
// full Horner chains. On the M33 it runs from SCRATCH_Y beside the shaper.
void iq32offsets_from_native_mla(uint32_t *out, const uint32_t *input, uint32_t blocks,
                                 const uint32_t previous[4]);
