// Cyclic full-history row pointers for four independent sample DMA chains.
#pragma once
#include <stdbool.h>
#include <stdint.h>

enum { IQ4QUAD_STREAMS = 4 };

// Stream order is I-even,Q-even,I-odd,Q-odd. Each ring has ring_words entries
// and is naturally aligned to ring_words*4 bytes for DMA read-address wrapping.
// Packed source words hold16 chronological two-bit codes, newest symbol low2
// within the L7 table key. The cyclic period is axis_words*16 symbols; its last
// six symbols supply the initial history, so ring wrap introduces no transient.
// ring_words must equal axis_words*8 and be a power of two. row_words=1 selects
// B4's8-bit rows; row_words=2 selects14-bit rows. table_base is a numerical
// 32-bit bus address, not a pointer dereferenced by this helper.
// Sources and output rings must not overlap. Invalid arguments return false
// before any output writes. Caller owns all storage; no allocation is performed.
bool iq4quad_build(uint32_t *const rings[IQ4QUAD_STREAMS], unsigned ring_words,
                  const uint32_t *axis_i, const uint32_t *axis_q,
                  unsigned axis_words, uint32_t table_base, unsigned row_words);
