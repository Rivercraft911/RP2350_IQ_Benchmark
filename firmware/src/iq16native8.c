#include "iq16native8.h"

// Frozen L8/N2 hold-aware alpha.20 pulse; Q8 DAC units. See coefficient hash in
// the header. No physical AFE/image or full16APSK-modem qualification is implied.
static const int32_t coefficients[IQ16NATIVE8_SPAN * IQ16NATIVE8_SPS] = {
    6658, -48202, 6455, 124501, -58274, -258197, 258053, 1108522,
    1108522, 258053, -258197, -58274, 124501, 6455, -48202, 6658,
};

const int32_t *iq16native8_coefficients(void) { return coefficients; }

bool iq16native8_build(const iq16native8_tables_t *tables, unsigned gamma100) {
    if (!tables || !tables->old || !tables->middle || !tables->future) return false;
    switch (gamma100) {
        case 315: case 285: case 275: case 270: case 260: case 257: break;
        default: return false;
    }
    const int32_t diagonal = 759250125, cosine15 = 1037154959, sine15 = 277904834;
    const int32_t inner = (int32_t)(((uint64_t)diagonal * 100u + gamma100 / 2u) / gamma100);
    const int32_t coordinate[8] = {diagonal, cosine15, sine15, inner,
                                 -diagonal, -cosine15, -sine15, -inner};
    int32_t scaled[8][IQ16NATIVE8_SPAN * IQ16NATIVE8_SPS];
    for (unsigned code = 0; code < 8; ++code)
        for (unsigned tap = 0; tap < IQ16NATIVE8_SPAN * IQ16NATIVE8_SPS; ++tap)
            scaled[code][tap] = (int32_t)(((int64_t)coordinate[code] * coefficients[tap] + ((int64_t)1 << 29)) >> 30);
    uint32_t *const output[3] = {tables->old, tables->middle, tables->future};
    const unsigned counts[3] = {3, 4, 4}, offsets[3] = {1, 4, 8};
    for (unsigned chunk = 0; chunk < 3; ++chunk) {
        for (uint32_t key = 0; key < (1u << (3 * counts[chunk])); ++key) {
            for (unsigned symbol = 0; symbol < IQ16NATIVE8_SYMBOLS; ++symbol) {
                uint32_t packed = 0;
                for (unsigned phase = 0; phase < IQ16NATIVE8_SPS; ++phase) {
                    int32_t sum = 0;
                    for (unsigned j = 0; j < counts[chunk]; ++j) {
                        const int age = 8 + (int)symbol - (int)(offsets[chunk] + j);
                        if (age >= 0 && age < IQ16NATIVE8_SPAN)
                            sum += scaled[(key >> (3 * j)) & 7u][age * IQ16NATIVE8_SPS + phase];
                    }
                    packed |= (uint32_t)(uint16_t)((sum + 128) >> 8) << (16 * phase);
                }
                output[chunk][key * IQ16NATIVE8_ROW_WORDS + symbol] = packed;
            }
        }
    }
    return true;
}

static inline uint32_t add_lanes(uint32_t a, uint32_t b) {
#if defined(IQ_ON_DEVICE) && defined(__ARM_ARCH_8M_MAIN__)
    uint32_t sum;
    __asm("sadd16 %0, %1, %2" : "=r"(sum) : "r"(a), "r"(b) : "cc");
    return sum;
#else
    return (uint32_t)(uint16_t)((int16_t)a + (int16_t)b) |
        (uint32_t)(uint16_t)((int16_t)(a >> 16) + (int16_t)(b >> 16)) << 16;
#endif
}

void iq16native8_kernel_c(uint32_t *out, const uint32_t *input, uint32_t blocks,
                          const uint32_t *prev, const iq16native8_tables_t *tables) {
    if (!blocks) return;
    uint32_t oldest = prev ? prev[0] : 0, middle = prev ? prev[1] : 0;
    for (uint32_t block = 0; block < blocks; ++block) {
        const uint32_t newest = input[block];
        const uint32_t *oi = tables->old + ((oldest >> 3) & 0x1ffu) * 4;
        const uint32_t *oq = tables->old + ((oldest >> 19) & 0x1ffu) * 4;
        const uint32_t *mi = tables->middle + (middle & 0xfffu) * 4;
        const uint32_t *mq = tables->middle + ((middle >> 16) & 0xfffu) * 4;
        const uint32_t *fi = tables->future + (newest & 0xfffu) * 4;
        const uint32_t *fq = tables->future + ((newest >> 16) & 0xfffu) * 4;
#pragma GCC unroll 4
        for (unsigned w = 0; w < IQ16NATIVE8_ROW_WORDS; ++w) {
            out[2 * w] = add_lanes(add_lanes(oi[w], mi[w]), fi[w]);
            out[2 * w + 1] = add_lanes(add_lanes(oq[w], mq[w]), fq[w]);
        }
        oldest = middle; middle = newest;
        out += 8;
    }
}
