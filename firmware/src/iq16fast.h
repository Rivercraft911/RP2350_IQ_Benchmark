// Exact alternative schedules for iq16native8's existing 3+4+4 tables.
#pragma once
#include "iq16native8.h"

#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
// Same history, zero-block, output and rounding contract as the native kernel.
// The old3 row's last packed sample pair is identically zero and is omitted.
void iq16fast_sparse_asm(uint32_t *out, const uint32_t *input, uint32_t blocks,
                          const uint32_t *prev, const iq16native8_tables_t *tables);
// Same sparse schedule, processing two input words per branch. At most three
// initial words use the existing C leaf to establish history and an even tail.
void iq16fast_pair_asm(uint32_t *out, const uint32_t *input, uint32_t blocks,
                        const uint32_t *prev, const iq16native8_tables_t *tables);
#endif
