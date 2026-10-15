#include "iqquad.h"

// Generated with reference/iqlut.py: Lut(.20,4,9,.8,1.0), coefficients scaled2^8.
// Keep this experimental profile separate from the qualified L12 coefficient sets.
static const int32_t coefficients[IQQUAD_SPAN * IQQUAD_SPS] = {
    19899, 28610, 15004, -17924, -49187, -51350, -11220, 54355,
    101594, 84948, -8279, -136444, -213605, -151666, 86158, 452058,
    823796, 1057627, 1057627, 823796, 452058, 86158, -151666, -213605,
    -136444, -8279, 84948, 101594, 54355, -11220, -51350, -49187,
    -17924, 15004, 28610, 19899,
};

void iqquad_build(uint32_t *quad) {
    for (uint32_t key = 0; key < IQQUAD_ROWS; ++key) {
        for (unsigned symbol = 0; symbol < 4; ++symbol) {
            const uint32_t history = key >> symbol;
            for (unsigned pair = 0; pair < IQQUAD_SPS / 2; ++pair) {
                uint32_t packed = 0;
                for (unsigned half = 0; half < 2; ++half) {
                    int32_t sum = 0;
                    for (unsigned age = 0; age < IQQUAD_SPAN; ++age) {
                        const int32_t c = coefficients[age * IQQUAD_SPS + 2 * pair + half];
                        sum += (history >> (IQQUAD_SPAN - 1 - age)) & 1u ? -c : c;
                    }
                    const uint32_t sample = (uint32_t)((sum + 128) >> 8) & 0x3fffu;
                    packed |= sample << (16 * half);
                }
                quad[key * IQQUAD_ROW_WORDS + 2 * symbol + pair] = packed;
            }
        }
    }
}
