// Rolling32APSK keys stored as byte offsets, eliminating address scaling.
#pragma once
#include "iq32native8.h"

// Convert count per-symbol radix17 key words to packed byte offsets. Each valid
// key lane0..4912 becomes offset0..39296, a multiple of8. Exact in-place use is
// supported; other overlapping input/output is not. Zero count touches nothing.
void iq32offsets_encode(uint32_t *out, const uint32_t *keys, uint32_t count);

// Same blocks/tables/output/history contract as iq32keys_kernel_c, but both
// input and five-word prev contain BYTE OFFSETS in their16-bit lanes. NULL
// prev means repeated unit-pilot offset14736. Valid offsets are required.
void iq32offsets_kernel_c(uint32_t *out, const uint32_t *input, uint32_t blocks,
                          const uint32_t *prev, const iq32native8_tables_t *tables);
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
void iq32offsets_kernel_asm(uint32_t *out, const uint32_t *input, uint32_t blocks,
                            const uint32_t *prev, const iq32native8_tables_t *tables);
// Generated identical leaves in separate per-core scratch banks.
void iq32offsets_kernel_sram_x(uint32_t *out, const uint32_t *input, uint32_t blocks,
                               const uint32_t *prev, const iq32native8_tables_t *tables);
void iq32offsets_kernel_sram_y(uint32_t *out, const uint32_t *input, uint32_t blocks,
                               const uint32_t *prev, const iq32native8_tables_t *tables);
#endif
