// Experimental descriptor generation for the existing binary-axis QPSK L12,N4 tables.
// No sample data is read. Each descriptor addresses one contiguous two-word axis LUT row.
#pragma once
#include <stdint.h>

// Input is the iqgen.h format: sixteen I bits low, sixteen Q bits high, earliest bit first.
// Output arrays each receive nwords*16 addresses. prev is the preceding input word (0 for
// initial all-positive prehistory). uint32_t bases intentionally make host/device arithmetic
// identical, including wrap; these kernels neither dereference nor validate those addresses.
void iqdesc_c(uint32_t *di, uint32_t *dq, const uint32_t *in, uint32_t nwords,
              uint32_t prev, uint32_t base_i, uint32_t base_q);
// Paired variant: eight descriptors/axis/input word, each pointing to a 16-byte row
// containing two chronological symbols. key is 13 history bits: h0=key&4095,h1=key>>1.
// A shared unflagged table has 8192 rows of four uint32_t words (128 KiB).
void iqdesc_pair_c(uint32_t *di, uint32_t *dq, const uint32_t *in, uint32_t nwords,
                   uint32_t prev, uint32_t base_i, uint32_t base_q);
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
void iqdesc_asm(uint32_t *di, uint32_t *dq, const uint32_t *in, uint32_t nwords,
                uint32_t prev, uint32_t base_i, uint32_t base_q);
// Borrows BOTH interpolators on the calling core, then restores their writable state.
// Caller must prevent concurrent/interrupted use of those interpolators during the call.
// Uses one full interpolator per axis: lane 1 rotates the 32-bit window; lane 0 generates
// the lookup address from lane 1. No hardware lane is shared with another independent axis.
void iqdesc_interp(uint32_t *di, uint32_t *dq, const uint32_t *in, uint32_t nwords,
                   uint32_t prev, uint32_t base_i, uint32_t base_q);
void iqdesc_pair_asm(uint32_t *di, uint32_t *dq, const uint32_t *in, uint32_t nwords,
                     uint32_t prev, uint32_t base_i, uint32_t base_q);
// Same exclusive ownership / save-and-restore contract as iqdesc_interp above.
void iqdesc_pair_interp(uint32_t *di, uint32_t *dq, const uint32_t *in, uint32_t nwords,
                        uint32_t prev, uint32_t base_i, uint32_t base_q);
#endif
