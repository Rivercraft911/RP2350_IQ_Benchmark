// QPSK RRC waveform kernels. Portable C: builds for the RP2350 and natively for host tests.
// Data formats are defined in reference/iqlut.py.
#pragma once
#include <stddef.h>
#include <stdint.h>

#define IQ_FLAG_BIT (1u << 14)

typedef struct {
    int sps, L;               // samples/symbol (2 or 4), LUT span in symbols (<= 16)
    const int32_t *coef;      // coef[a*sps + p], scaled by 2^COEF_FRAC_BITS
    uint32_t *ti, *tq;        // tables: [2^L][sps/2] words, phase 2k | phase 2k+1 << 16
} iq_cfg_t;

// Generate nwords*16 symbols -> nwords*16*sps output words (one per complex sample).
// prev is the input word preceding in[0] (filter history across block boundaries).
typedef void (*iq_kernel_t)(uint32_t *out, const uint32_t *in, uint32_t nwords,
                            uint32_t prev, const iq_cfg_t *cfg);

typedef struct {
    const char *name;         // version tag logged with every measurement
    int sps, L;               // 0 = any
    iq_kernel_t fn;
} iq_kernel_info_t;

extern const iq_kernel_info_t IQ_KERNELS[];
extern const int IQ_N_KERNELS;

const iq_kernel_info_t *iq_find_kernel(const char *name, int sps, int L);
void iq_build_tables(const iq_cfg_t *cfg);
uint32_t crc32_update(uint32_t crc, const void *data, size_t nbytes);  // zlib-compatible
void prbs_fill(uint32_t *w, size_t n, uint32_t *state);                // xorshift32
