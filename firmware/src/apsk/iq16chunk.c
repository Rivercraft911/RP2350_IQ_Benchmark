#include "iq16chunk.h"
#include "iqlanes.h"
#include <stddef.h>

// Same offline filter as iq8quad: alpha=.20, L9, N2, Kaiser beta1.16,
// cropped [-.0455,1.091,-.0455] pre-emphasis, 1dB worst-history headroom, Q8.
static const int32_t coefficients[IQ16CHUNK_SPAN * IQ16CHUNK_SPS] = {
    26061, 1353, -59453, 20157, 117236, -77279, -242951, 262833, 1061537,
    1061537, 262833, -242951, -77279, 117236, 20157, -59453, 1353, 26061,
};

const int32_t *iq16chunk_coefficients(void) { return coefficients; }

static inline uint32_t reverse_key(uint32_t key) {
    return (key & 7u) << 9 | (key & 0x38u) << 3 | (key >> 3 & 0x38u) | (key >> 9 & 7u);
}

void iq16chunk_build_reverse(uint16_t *reverse) {
    for (uint32_t key = 0; key < IQ16CHUNK_ROWS; key++) reverse[key] = (uint16_t)reverse_key(key);
}

bool iq16chunk_build(const iq16chunk_tables_t *tables, unsigned gamma100) {
    if (!tables || !tables->edge || !tables->middle) return false;
    switch (gamma100) {
        case 315: case 285: case 275: case 270: case 260: case 257: break;
        default: return false;
    }
    const int32_t diagonal = 759250125, cosine15 = 1037154959, sine15 = 277904834;
    const int32_t inner = (int32_t)(((uint64_t)diagonal * 100u + gamma100 / 2u) / gamma100);
    const int32_t coordinate[8] = {diagonal, cosine15, sine15, inner,
                                 -diagonal, -cosine15, -sine15, -inner};
    int32_t scaled[8][IQ16CHUNK_SPAN * IQ16CHUNK_SPS];
    for (unsigned code = 0; code < 8; code++)
        for (unsigned tap = 0; tap < IQ16CHUNK_SPAN * IQ16CHUNK_SPS; tap++)
            scaled[code][tap] = (int32_t)(((int64_t)coordinate[code] * coefficients[tap] + ((int64_t)1 << 29)) >> 30);
    for (uint32_t key = 0; key < IQ16CHUNK_ROWS; key++) {
        for (unsigned chunk = 0; chunk < 2; chunk++) {
            uint32_t *table = chunk ? tables->middle : tables->edge;
            for (unsigned symbol = 0; symbol < IQ16CHUNK_SYMBOLS; symbol++) {
                uint32_t packed = 0;
                for (unsigned phase = 0; phase < IQ16CHUNK_SPS; phase++) {
                    int32_t sum = 0;
                    for (unsigned j = 0; j < 4; j++) {
                        const int age = 8 + (int)symbol - (int)(4 * chunk + j);
                        if (age >= 0 && age < IQ16CHUNK_SPAN)
                            sum += scaled[(key >> (3 * j)) & 7u][age * IQ16CHUNK_SPS + phase];
                    }
                    packed |= (uint32_t)(uint16_t)((sum + 128) >> 8) << (16 * phase);
                }
                table[key * IQ16CHUNK_ROW_WORDS + symbol] = packed;
            }
        }
    }
    return true;
}

void iq16chunk_fill(uint32_t *input, uint32_t blocks, uint32_t *seed) {
    uint32_t x = *seed;
    for (uint32_t block = 0; block < blocks; block++) {
        uint32_t packed = 0;
        for (unsigned j = 0; j < 4; j++) {
            x ^= x << 13; x ^= x >> 17; x ^= x << 5;
            const uint32_t label = x & 15u;
            const uint32_t i = ((label >> 1) & 1u) << 2 | ((label >> 3) & 1u) << 1 | ((label >> 2) & 1u);
            const uint32_t q = (label & 1u) << 2 | ((label >> 2) & 1u) << 1 | ((label >> 3) & 1u);
            packed |= i << (3 * j) | q << (16 + 3 * j);
        }
        input[block] = packed;
    }
    *seed = x;
}

static inline uint32_t swap_lanes(uint32_t word) { return word << 16 | word >> 16; }

// Kept in flash for the first isolated component: the experimental image's RAM
// code region is already tight. Benchmark results must record final placement.
void iq16chunk_kernel_c(uint32_t *out, const uint32_t *input, uint32_t blocks,
                        const uint32_t *prev, const iq16chunk_tables_t *tables) {
    if (!blocks) return;
    uint32_t oldest = prev ? prev[0] : 0, middle = prev ? prev[1] : 0;
    for (uint32_t block = 0; block < blocks; block++) {
        const uint32_t newest = input[block];
        const unsigned ni = newest & 0xfffu, nq = newest >> 16 & 0xfffu;
        const unsigned ri = tables->reverse ? tables->reverse[ni] : reverse_key(ni);
        const unsigned rq = tables->reverse ? tables->reverse[nq] : reverse_key(nq);
        const uint32_t *ei = tables->edge + (oldest & 0xfffu) * 4;
        const uint32_t *eq = tables->edge + (oldest >> 16 & 0xfffu) * 4;
        const uint32_t *mi = tables->middle + (middle & 0xfffu) * 4;
        const uint32_t *mq = tables->middle + (middle >> 16 & 0xfffu) * 4;
        const uint32_t *fi = tables->edge + ri * 4;
        const uint32_t *fq = tables->edge + rq * 4;
#pragma GCC unroll 4
        for (unsigned w = 0; w < IQ16CHUNK_ROW_WORDS; w++) {
            out[2 * w] = add_lanes(add_lanes(ei[w], mi[w]), swap_lanes(fi[3 - w]));
            out[2 * w + 1] = add_lanes(add_lanes(eq[w], mq[w]), swap_lanes(fq[3 - w]));
        }
        oldest = middle; middle = newest;
        out += 8;
    }
}
