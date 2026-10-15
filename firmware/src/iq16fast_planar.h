// Optional output layout for a dual-axis DMA consumer; same 16APSK pulse/math.
#pragma once
#include "iq16native8.h"

#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
// Eight words per input word: I01,I23,I45,I67,Q01,Q23,Q45,Q67.
// Every word contains two signed16 samples. This is distinct from RAW_PAIRS.
// History and zero-block behavior match iq16native8; tables are unchanged.
void iq16fast_planar_asm(uint32_t *out, const uint32_t *input, uint32_t blocks,
                         const uint32_t *prev, const iq16native8_tables_t *tables);
#endif
