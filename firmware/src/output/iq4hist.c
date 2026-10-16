#include "iq4hist.h"
#include <stddef.h>

// Frozen Q8 bytes SHA256 d23d0e76d95169efb8b96466f8db7f81e0b5e3158849f594a419641586ca4796.
// Passes the Annex A amplitude-mask model with ideal DAC hold; the unheld
// digital response fails. Physical AFE response and images remain unqualified.
static const int32_t coefficients[IQ4HIST_SPAN * IQ4HIST_SPS] = {
    -40558, -43572, -14753, 39237, 100386, 94551, 13532, -127225,
    -219473, -172708, 74855, 466885, 875097, 1132555, 1132555, 875097,
    466885, 74855, -172708, -219473, -127225, 13532, 94551, 100386,
    39237, -14753, -43572, -40558,
};

// Independently rounded Q30 cos/sin constants, not rounded plane partial sums.
static const int32_t levels_q30[4] = {992008094, -410903207, 410903207, -992008094};

const int32_t *iq4hist_coefficients(void) { return coefficients; }

bool iq4hist_build(uint32_t *table, unsigned word_capacity) {
    if (!table || word_capacity < IQ4HIST_TABLE_WORDS) return false;
    for (uint32_t key = 0; key < IQ4HIST_ROWS; ++key) {
        for (unsigned pair = 0; pair < IQ4HIST_ROW_WORDS; ++pair) {
            uint32_t packed = 0;
            for (unsigned half = 0; half < 2; ++half) {
                int64_t sum = 0;
                for (unsigned age = 0; age < IQ4HIST_SPAN; ++age) {
                    const int32_t level = levels_q30[(key >> (2 * age)) & 3u];
                    sum += (int64_t)coefficients[age * IQ4HIST_SPS + 2 * pair + half] * level;
                }
                // Arithmetic right shift has the same GCC/Clang convention as iqgen.c.
                const uint32_t sample = (uint32_t)((sum + ((int64_t)1 << 37)) >> 38) & 0x3fffu;
                packed |= sample << (16 * half);
            }
            table[key * IQ4HIST_ROW_WORDS + pair] = packed;
        }
    }
    return true;
}

bool iq4hist8_build(uint32_t *table, unsigned word_capacity) {
    if (!table || word_capacity < IQ4HIST8_TABLE_WORDS) return false;
    const int64_t denominator = (int64_t)IQ4HIST8_SCALE_DEN << 32;
    for (uint32_t key = 0; key < IQ4HIST_ROWS; ++key) {
        uint32_t packed = 0;
        for (unsigned phase = 0; phase < IQ4HIST_SPS; ++phase) {
            int64_t sum = 0;
            for (unsigned age = 0; age < IQ4HIST_SPAN; ++age) {
                const int32_t level = levels_q30[(key >> (2 * age)) & 3u];
                sum += (int64_t)coefficients[age * IQ4HIST_SPS + phase] * level;
            }
            // C division truncates; explicitly implement floor for negative sums.
            sum += denominator / 2;
            const int64_t rounded = sum >= 0 ? sum / denominator :
                -((-sum + denominator - 1) / denominator);
            packed |= ((uint32_t)rounded & 0xffu) << (8 * phase);
        }
        table[key] = packed;
    }
    return true;
}

static inline uint32_t spread16(uint32_t bits) {
    bits &= 0xffffu;
    bits = (bits | bits << 8) & 0x00ff00ffu;
    bits = (bits | bits << 4) & 0x0f0f0f0fu;
    bits = (bits | bits << 2) & 0x33333333u;
    return (bits | bits << 1) & 0x55555555u;
}

void iq4hist_pack_group(const iq8_input_t *group, uint32_t *i, uint32_t *q) {
    // Interleave the A/B sign bits without a loop over symbols.
    *i = spread16(group->a) | spread16(group->b) << 1;
    *q = spread16(group->a >> 16) | spread16(group->b >> 16) << 1;
}
