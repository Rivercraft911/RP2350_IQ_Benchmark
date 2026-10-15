#include "iqquad.h"
#include <stddef.h>

// Generated with reference/iqlut.py: Lut(.20,4,9,.8,1.0), coefficients scaled2^8.
// Keep this experimental profile separate from the qualified L12 coefficient sets.
static const int32_t l9_coefficients[IQQUAD_SPAN * IQQUAD_SPS] = {
    19899, 28610, 15004, -17924, -49187, -51350, -11220, 54355,
    101594, 84948, -8279, -136444, -213605, -151666, 86158, 452058,
    823796, 1057627, 1057627, 823796, 452058, 86158, -151666, -213605,
    -136444, -8279, 84948, 101594, 54355, -11220, -51350, -49187,
    -17924, 15004, 28610, 19899,
};

// Frozen convex design qpsk_l8_n4_both_m010_far2; Q8 coefficient bytes SHA256:
// 28a65e7881d8649dcae694866b798491a57b36d4c43528b58fe44a66b32a1dcd
// Host model passes Annex A amplitude masks both digitally and with ideal DAC hold.
static const int32_t l8_coefficients[IQQUAD_L8_SPAN * IQQUAD_SPS] = {
    18026, -9152, -32743, -43578, -16902, 44626, 101009, 97555,
    8822, -128261, -220622, -167118, 78231, 466713, 865736, 1117887,
    1117887, 865736, 466713, 78231, -167118, -220622, -128261, 8822,
    97555, 101009, 44626, -16902, -43578, -32743, -9152, 18026,
};

static const iqquad_profile_info_t profiles[] = {
    {IQQUAD_SPAN, IQQUAD_HISTORY_BITS, IQQUAD_ROWS, IQQUAD_WORDS,
     IQQUAD_TABLE_CRC, "qpsk_l9_n4_kaiser_b080"},
    {IQQUAD_L8_SPAN, IQQUAD_L8_HISTORY_BITS, IQQUAD_L8_ROWS, IQQUAD_L8_WORDS,
     IQQUAD_L8_TABLE_CRC, "qpsk_l8_n4_both_m010_far2"},
};

const iqquad_profile_info_t *iqquad_profile_info(iqquad_profile_t profile) {
    if ((unsigned)profile >= sizeof profiles / sizeof profiles[0]) return NULL;
    return &profiles[profile];
}

bool iqquad_build_profile(uint32_t *quad, unsigned word_capacity, iqquad_profile_t profile) {
    const iqquad_profile_info_t *info = iqquad_profile_info(profile);
    if (!quad || !info || word_capacity < info->words) return false;
    const int32_t *coefficients = profile == IQQUAD_PROFILE_L9_KAISER ?
                                  l9_coefficients : l8_coefficients;
    for (uint32_t key = 0; key < info->rows; ++key) {
        for (unsigned symbol = 0; symbol < 4; ++symbol) {
            const uint32_t history = key >> symbol;
            for (unsigned pair = 0; pair < IQQUAD_SPS / 2; ++pair) {
                uint32_t packed = 0;
                for (unsigned half = 0; half < 2; ++half) {
                    int32_t sum = 0;
                    for (unsigned age = 0; age < info->span; ++age) {
                        const int32_t c = coefficients[age * IQQUAD_SPS + 2 * pair + half];
                        sum += (history >> (info->span - 1 - age)) & 1u ? -c : c;
                    }
                    const uint32_t sample = (uint32_t)((sum + 128) >> 8) & 0x3fffu;
                    packed |= sample << (16 * half);
                }
                quad[key * IQQUAD_ROW_WORDS + 2 * symbol + pair] = packed;
            }
        }
    }
    return true;
}

void iqquad_build(uint32_t *quad) {
    (void)iqquad_build_profile(quad, IQQUAD_WORDS, IQQUAD_PROFILE_L9_KAISER);
}
