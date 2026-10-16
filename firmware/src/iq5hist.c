#include "iq5hist.h"
#include <stddef.h>

// Symmetric 35-tap alpha0.20 pulse optimized with the ideal DAC hold.
// The actual rounded table passes the held iid-ensemble mask model only.
static const int32_t coefficients[IQ5HIST_SPAN * IQ5HIST_SPS] = {
    -39073, -46397, -27862, -7399, 53396, 87889, 107821,
    59124, -24906, -139647, -210765, -204025, -67585, 180230,
    508721, 829370, 1070150, 1154856, 1070150, 829370, 508721,
    180230, -67585, -204025, -210765, -139647, -24906, 59124,
    107821, 87889, 53396, -7399, -27862, -46397, -39073,
};
static const int32_t levels_q30[4] = {992008094, -410903207, 410903207, -992008094};

const int32_t *iq5hist_coefficients(void) { return coefficients; }

bool iq5hist_build(uint32_t *table, unsigned word_capacity) {
    if (!table || word_capacity < IQ5HIST_TABLE_WORDS) return false;
    const int64_t denominator = (int64_t)IQ5HIST_SCALE_DEN << 32;
    for (uint32_t key = 0; key < IQ5HIST_ROWS; ++key) {
        uint32_t packed[IQ5HIST_ROW_WORDS] = {0, 0};
        for (unsigned phase = 0; phase < IQ5HIST_SPS; ++phase) {
            int64_t sum = 0;
            for (unsigned age = 0; age < IQ5HIST_SPAN; ++age) {
                const int32_t level = levels_q30[(key >> (2 * age)) & 3u];
                sum += (int64_t)coefficients[age * IQ5HIST_SPS + phase] * level;
            }
            sum += denominator / 2;
            const int64_t rounded = sum >= 0 ? sum / denominator :
                -((-sum + denominator - 1) / denominator);
            packed[phase / 4] |= ((uint32_t)rounded & 0xffu) << (8 * (phase % 4));
        }
        table[2 * key] = packed[0];
        table[2 * key + 1] = packed[1];
    }
    return true;
}
