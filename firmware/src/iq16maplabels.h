// Exact native16 coordinate packing from iq16chunk_fill's four-bit labels.
#pragma once
#include <stdint.h>
// Four chronological labels0..15 per output word. Bits12..15/28..31 clear.
// Zero blocks dereferences no pointer. Input/output must not overlap.
void iq16maplabels_encode(uint32_t *out, const uint8_t *labels, uint32_t blocks);
