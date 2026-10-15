// Native two-symbol coordinates directly to rolling byte offsets.
#pragma once
#include <stdint.h>
// out needs 5+2*blocks words: five preceding keys, then current per-symbol keys.
// previous is four chronological native history words, or NULL for unit pilots.
// Valid native coordinate codes 0..16; input/output must not overlap.
// Zero blocks touches no pointers and writes no history prefix.
void iq32offsets_from_native(uint32_t *out, const uint32_t *input, uint32_t blocks,
                             const uint32_t previous[4]);
