// Isolated globally rotated 8PSK shaper, not a complete DVB-S2 transmitter.
// L9 / 2 samples per symbol / four-symbol rows; signed tables have no IQ flags.
#pragma once
#include <stdint.h>
#include "iq8.h"

enum {
    IQ8QUAD_SPAN = 9,
    IQ8QUAD_SPS = 2,
    IQ8QUAD_SYMBOLS = 4,
    IQ8QUAD_HISTORY_BITS = 12,
    IQ8QUAD_ROWS = 1u << IQ8QUAD_HISTORY_BITS,
    IQ8QUAD_ROW_WORDS = 4,
    IQ8QUAD_TABLE_WORDS = IQ8QUAD_ROWS * IQ8QUAD_ROW_WORDS,
};
typedef iq8_tables_t iq8quad_tables_t;

// Independent direct-FIR little-endian table CRCs, including signed upper bits.
#define IQ8QUAD_TABLE_A_CRC 0xcd2d4635u
#define IQ8QUAD_TABLE_B_CRC 0x135bd352u
// 65536 PRBS symbols, iq8_fill seed 0x1234abcd, default repeated pi/8 history.
// PACKED means (I & 0x3fff) | (Q & 0x3fff)<<16, with NO IQ flag.
#define IQ8QUAD_FIXTURE_SYMBOLS 65536u
#define IQ8QUAD_FIXTURE_SEED 0x1234abcdu
#define IQ8QUAD_FIXTURE_PACKED_CRC 3803865361u
#define IQ8QUAD_FIXTURE_RAW_PAIRS_CRC 850210923u
#define IQ8QUAD_FIXTURE_I_CRC 3417247612u
#define IQ8QUAD_FIXTURE_Q_CRC 3777218023u

// Both caller-owned arrays are 64 KiB. The key has oldest bit0, first output
// symbol bit8 and its successors bits9..11. Tables store four signed sample pairs.
void iq8quad_build(const iq8quad_tables_t *tables);
const int32_t *iq8quad_coefficients(void); // 18 Q8 taps, for independent checks

// Input/prev are exactly iq8.h's two-plane format (16 symbols / 8-byte group).
// Output is groups*32 words in PAIRS order: I01,Q01,I23,Q23,..., with signed16
// lane sums and NO mask or flag. PIO must select bits13:0, discard bits15:14
// per halfword, and supply IQ_FLAG separately. Never feed legacy 16-data-pin PIO.
// NULL prev means repeated pi/8 symbols before the stream. Zero groups writes nothing.
void iq8quad_kernel_c(uint32_t *out, const iq8_input_t *in, uint32_t groups,
                     const iq8_input_t *prev, const iq8quad_tables_t *tables);

#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
// Owns both calling-core interpolators for the call, saving/restoring their state.
// No interrupt handler or concurrent task may use those interpolators during it.
void iq8quad_kernel_interp(uint32_t *out, const iq8_input_t *in, uint32_t groups,
                          const iq8_input_t *prev, const iq8quad_tables_t *tables);
// Raw fused leaf; only call after exclusive configuration described in the .S file.
void iq8quad_kernel_interp_asm(uint32_t *out, const iq8_input_t *in, uint32_t groups,
                              const iq8_input_t *prev);
#endif
