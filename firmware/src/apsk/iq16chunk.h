// Exact native-orientation DVB-S2 16APSK shaping component, outer radius R2=1.
// L9 / 2 samples per symbol / four-symbol output blocks. No FEC or RF qualification.
#pragma once
#include <stdbool.h>
#include <stdint.h>

enum {
    IQ16CHUNK_SPAN = 9,
    IQ16CHUNK_SPS = 2,
    IQ16CHUNK_SYMBOLS = 4,
    IQ16CHUNK_ROWS = 4096,
    IQ16CHUNK_ROW_WORDS = 4,
    IQ16CHUNK_TABLE_WORDS = IQ16CHUNK_ROWS * IQ16CHUNK_ROW_WORDS,
    IQ16CHUNK_DEFAULT_GAMMA100 = 270,
};
typedef struct {
    uint32_t *edge, *middle; // 64 KiB each; tables are shared by I and Q
    const uint16_t *reverse; // optional caller-owned 8 KiB key-reversal map
} iq16chunk_tables_t;

#define IQ16CHUNK_EDGE_CRC 0x1cb4542du
#define IQ16CHUNK_MIDDLE_CRC 0x938da81fu
#define IQ16CHUNK_FIXTURE_SEED 0x1234abcdu
#define IQ16CHUNK_FIXTURE_BLOCKS 8192u // 32768 symbols, 32 KiB packed input
#define IQ16CHUNK_FIXTURE_INPUT_CRC 787286031u
#define IQ16CHUNK_FIXTURE_RAW_PAIRS_CRC 2951358184u
#define IQ16CHUNK_FIXTURE_PACKED_CRC 606382727u // 14-bit masked, no IQ flag

// Coordinate code [sign,magnitude1,magnitude0], with positive magnitudes:
// 0=1/sqrt(2), 1=cos(pi/12), 2=sin(pi/12), 3=1/(gamma*sqrt(2)).
// Codes4..7 negate codes0..3. Code0/4 includes exact unit-radius header/pilots.
// Each input word contains four chronological codes at I bits0..11 and Q16..27.
// Ignored bits12..15 and28..31 may be nonzero. Each word represents four symbols.

// gamma100 must be315,285,275,270,260 or257 (ETSI code-rate-specific radii).
// No heap or persistent buffers. False leaves tables unchanged.
bool iq16chunk_build(const iq16chunk_tables_t *tables, unsigned gamma100);
void iq16chunk_build_reverse(uint16_t *reverse); // 4096 entries, no table dependency
const int32_t *iq16chunk_coefficients(void); // 18 Q8 pre-equalized taps
void iq16chunk_fill(uint32_t *input, uint32_t blocks, uint32_t *seed);

// prev points to two preceding packed words, oldest first (eight symbols).
// NULL prev means repeated unit pilot (+1+j)/sqrt(2). Preserve the last two
// complete input words across calls; zero blocks reads/writes nothing.
// Output is blocks*8 words in RAW_PAIRS order: I01,Q01,I23,Q23,..., signed16
// with neither a14-bit mask nor IQ flag. PIO must discard upper two lane bits
// and supply IQ_FLAG, as the raw 8PSK output component does.
void iq16chunk_kernel_c(uint32_t *out, const uint32_t *input, uint32_t blocks,
                        const uint32_t *prev, const iq16chunk_tables_t *tables);

#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
// Same contract as the C leaf; four-word table bursts, no peripheral state.
// Uses a non-NULL key-reversal map; NULL selects the C leaf. The first at most
// two blocks use C to establish contiguous history, then the burst loop runs.
// Kept in flash for this isolated comparison; 48-byte AAPCS stack frame.
void iq16chunk_kernel_asm(uint32_t *out, const uint32_t *input, uint32_t blocks,
                          const uint32_t *prev, const iq16chunk_tables_t *tables);
#endif
